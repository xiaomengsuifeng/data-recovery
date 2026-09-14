from __future__ import annotations

import hashlib
import re
import tempfile
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

from . import __version__
from .common import (RecoveryError, check_identity, image_identity, new_directory,
                     read_json, regular_file, sha256_file, write_json)
from .recycle_bin import InvalidRecycleMetadata, pair_key, parse_dollar_i
from .tsk import ATTRIBUTE_ID, Tsk
from .control import checkpoint, progress
from .windows import VolumeSource, ensure_safe_locations, read_volume_boot, volume_identity


def validate_geometry(image: Path, offset: int, sector_size: int) -> None:
    if image.suffix.lower() not in (".raw", ".dd", ".img"):
        raise RecoveryError("Use a single raw image with .raw, .dd, or .img extension; split/container images are unsupported.")
    if type(offset) is not int or offset < 0 or sector_size not in (512, 1024, 2048, 4096):
        raise RecoveryError("Invalid sector offset or sector size.")
    if offset * sector_size + 512 > image.stat().st_size:
        raise RecoveryError("The NTFS boot sector is outside the image.")
    with image.open("rb") as stream:
        stream.seek(offset * sector_size)
        boot = stream.read(512)
    if boot[3:11] != b"NTFS    " or boot[510:512] != b"\x55\xaa":
        raise RecoveryError("No NTFS boot sector at this offset. Use the partition's starting sector.")
    if int.from_bytes(boot[11:13], "little") != sector_size:
        raise RecoveryError("NTFS sector size differs from --sector-size.")


def _candidate_id(inode: str, path: str) -> str:
    return hashlib.sha256((inode + "\0" + path).encode()).hexdigest()[:24]


def _associate_recycle(candidates: list[dict], backend: Tsk, image: Path,
                       offset: int, sector_size: int, temp_dir: Path) -> None:
    indexes = defaultdict(list)
    payloads = defaultdict(list)
    for candidate in candidates:
        path = candidate["observed_path"]
        key = pair_key(path)
        if key is None:
            continue
        if path.replace("\\", "/").rsplit("/", 1)[-1][:2].lower() == "$i":
            candidate["kind"] = "recycle_metadata"
            indexes[key].append(candidate)
        else:
            candidate["original_path"] = None
            candidate["path_evidence"] = "recycle_name_only"
            payloads[key].append(candidate)
    for key, files in payloads.items():
        checkpoint()
        metadata_candidates = indexes.get(key, [])
        if len(metadata_candidates) != 1 or len(files) != 1:
            for item in files:
                item["warnings"].append("No unique deleted $I/$R pair; original path is unknown.")
            continue
        index, candidate = metadata_candidates[0], files[0]
        if not 28 <= index["size"] <= 65564:
            candidate["warnings"].append("Recycle metadata exceeds supported size limits.")
            continue
        try:
            with tempfile.TemporaryFile(dir=temp_dir) as stream:
                backend.extract(image, offset, sector_size, index["inode"], stream, index["size"])
                stream.seek(0)
                metadata = parse_dollar_i(stream.read())
            if metadata.original_size != candidate["size"]:
                raise InvalidRecycleMetadata("$I original size differs from the $R entry")
        except (RecoveryError, InvalidRecycleMetadata) as exc:
            candidate["warnings"].append(f"Recycle association rejected: {exc}")
            continue
        candidate["original_path"] = metadata.original_path
        candidate["path_evidence"] = "recycle_metadata_association_unverified"
        candidate["recycle_metadata"] = {
            "candidate_id": index["id"], "version": metadata.version,
            "deleted_at": metadata.deleted_at, "original_size": metadata.original_size,
        }
        candidate["warnings"].append("Matching recycle metadata is evidence, not proof against record reuse.")


def scan(image: Path | VolumeSource, output: Path, *, backend: Tsk, offset: int = 0,
         sector_size: int = 512, max_candidates: int = 10000) -> dict:
    if type(max_candidates) is not int or not 1 <= max_candidates <= 100000:
        raise RecoveryError("--max-candidates must be between 1 and 100000 for this prototype.")
    if isinstance(image, VolumeSource):
        identity = volume_identity(image.mount)
        ensure_safe_locations(identity, output)
        boot = read_volume_boot(identity)
        sector_size = int.from_bytes(boot[11:13], "little")
        if sector_size not in (512, 1024, 2048, 4096):
            raise RecoveryError("不支持此卷的扇区尺寸。")
        image, offset = Path(identity["path"]), 0
    else:
        image = regular_file(image)
        validate_geometry(image, offset, sector_size)
        progress("source", message="核对源镜像")
        identity = image_identity(image)
    directory = new_directory(output)
    candidates, warnings = backend.scan(image, offset, sector_size)
    if len(candidates) > max_candidates:
        raise RecoveryError(f"More than {max_candidates} candidates; increase --max-candidates explicitly.")
    for candidate in candidates:
        checkpoint()
        candidate["id"] = _candidate_id(candidate["inode"], candidate["observed_path"])
        root_name = candidate["observed_path"].replace("\\", "/").lstrip("/").split("/", 1)[0].lower()
        if root_name in ("$orphanfiles", "$recycle.bin"):
            candidate["original_path"] = None
            candidate["path_evidence"] = "generated_or_recycle_path_only"
            candidate["warnings"].append("Observed system/recovery path does not establish the original directory.")
    progress("recycle", message="关联回收站名称与内容")
    _associate_recycle(candidates, backend, image, offset, sector_size, directory)
    progress("source", message="复核扫描源")
    check_identity(identity)
    report = {
        "schema_version": 1, "prototype_version": __version__, "status": "scanned",
        "source": identity, "offset": offset, "sector_size": sector_size,
        "backend": {"name": "The Sleuth Kit", "versions": backend.versions},
        "candidate_count": len(candidates), "candidates": candidates, "warnings": warnings,
        "limitations": [
            "Only deleted NTFS unnamed data attributes are supported.",
            "Deleted directories are visited separately; damaged or reused directory records may remain incomplete.",
            "Deleted allocation pointers can reference reused content; export does not prove integrity.",
            "No carving, EFS/BitLocker decryption, or TRIM reversal is implemented.",
        ],
    }
    if identity.get("kind") == "windows_volume":
        report["limitations"].append("Live volume identity is checked, but the OS may change its contents during recovery.")
    write_json(directory / "session.json", report)
    return report


def safe_relative_path(original: str) -> Path:
    """Encode hostile/Windows-special components without trusting a recovered path."""
    original = re.sub(r"^[A-Za-z]:[/\\]", "", original)
    parts = original.replace("\\", "/").split("/")
    safe = []
    for part in parts:
        if not part:
            continue
        encoded = "".join(quote(char, safe="") if ord(char) < 32 or char in ':*?"<>|%' else char for char in part)
        if part in (".", ".."):
            encoded = part.replace(".", "%2E")
        if encoded.endswith((".", " ")):
            encoded = encoded.rstrip(". ") + "_"
        if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part):
            encoded = "_" + encoded
        if len(encoded) > 80:
            encoded = encoded[:60] + "_" + hashlib.sha256(part.encode()).hexdigest()[:12]
        safe.append(encoded or "unnamed")
    if len(safe) > 20:
        safe = ["deep_path_" + hashlib.sha256(original.encode()).hexdigest()[:12], safe[-1]]
    return Path(*(safe or ["unnamed"]))


def _validate_candidates(candidates) -> None:
    if not isinstance(candidates, list) or len(candidates) > 100000:
        raise RecoveryError("Invalid candidate list.")
    ids = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise RecoveryError("Invalid candidate.")
        identifier = candidate.get("id", "")
        if not isinstance(identifier, str) or not re.fullmatch("[0-9a-f]{24}", identifier) or identifier in ids:
            raise RecoveryError("Invalid or duplicate candidate ID.")
        ids.add(identifier)
        if not isinstance(candidate.get("inode"), str) or not ATTRIBUTE_ID.fullmatch(candidate["inode"]):
            raise RecoveryError("Invalid NTFS attribute ID.")
        if type(candidate.get("size")) is not int or not 0 <= candidate["size"] < 1 << 63:
            raise RecoveryError("Invalid candidate size.")
        if not isinstance(candidate.get("observed_path"), str) or not candidate["observed_path"]:
            raise RecoveryError("Missing observed path.")
        if candidate.get("original_path") is not None and not isinstance(candidate["original_path"], str):
            raise RecoveryError("Invalid original path.")
        if candidate.get("kind") not in ("file", "recycle_metadata"):
            raise RecoveryError("Unsupported candidate kind.")


def recover(session: Path, destination: Path, *, backend: Tsk,
            candidate_ids: list[str] | None = None, max_file_bytes: int = 1024 ** 3) -> dict:
    if type(max_file_bytes) is not int or not 1 <= max_file_bytes < 1 << 63:
        raise RecoveryError("Invalid --max-file-bytes value.")
    report = read_json(session / "session.json")
    if report.get("status") != "scanned":
        raise RecoveryError("Session did not complete scanning.")
    _validate_candidates(report.get("candidates"))
    source = report["source"]
    check_identity(source)
    live = source.get("kind") == "windows_volume"
    image = Path(source["path"]) if live else regular_file(Path(source["path"]))
    offset, sector_size = report["offset"], report["sector_size"]
    if live:
        ensure_safe_locations(source, session, destination)
        read_volume_boot(source)
    else:
        validate_geometry(image, offset, sector_size)
    if report["backend"]["versions"] != backend.versions:
        raise RecoveryError("TSK version differs from the scan; rescan with the current version.")
    selected = set(candidate_ids or [])
    if candidate_ids is not None and not selected:
        raise RecoveryError("请至少选择一个需要恢复的文件。")
    known = {c["id"] for c in report["candidates"]}
    if selected - known:
        raise RecoveryError("One or more requested candidate IDs are not in this session.")
    candidates = [c for c in report["candidates"]
                  if c["id"] in selected or (not selected and c["kind"] == "file")]
    directory = new_directory(destination)
    results = []
    saved_names = set()
    status = "completed"
    for candidate in candidates:
        # Handled through the existing cancellation/report path below.
        result = {
            "candidate_id": candidate["id"], "original_path": candidate["original_path"],
            "observed_path": candidate["observed_path"], "expected_size": candidate["size"],
            "actual_size": 0, "sha256": None, "saved_path": None,
            "status": "skipped", "warnings": list(candidate.get("warnings", [])),
        }
        results.append(result)
        if candidate["size"] > max_file_bytes:
            result["warnings"].append("File exceeds --max-file-bytes; it was not extracted.")
            continue
        import shutil
        if shutil.disk_usage(directory).free < candidate["size"] + 1024 * 1024:
            result["status"] = "failed"
            result["warnings"].append("目标磁盘可用空间不足，未开始写入此文件。")
            continue
        relative = Path("files") / safe_relative_path(
            candidate["original_path"] or candidate["observed_path"])
        original_relative = relative
        collision = 0
        while relative.as_posix().casefold() in saved_names:
            collision += 1
            suffix = "__" + candidate["id"] + (f"_{collision}" if collision > 1 else "")
            relative = original_relative.with_name(original_relative.stem + suffix + original_relative.suffix)
        saved_names.add(relative.as_posix().casefold())
        target = directory / relative
        try:
            progress("export", len(results) - 1, len(candidates), candidate["original_path"] or candidate["observed_path"])
            try:
                target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                with target.open("xb") as stream:
                    backend.extract(image, offset, sector_size, candidate["inode"], stream,
                                    candidate["size"])
                result["status"] = "exported_unverified"
            except (RecoveryError, OSError) as exc:
                result["warnings"].append(str(exc))
                result["status"] = "partial" if target.is_file() else "failed"
            if target.is_file():
                result["saved_path"] = relative.as_posix()
                result["actual_size"] = target.stat().st_size
                if result["actual_size"] != candidate["size"]:
                    result["status"] = "partial"
                    result["warnings"].append("Extracted length differs from the deleted metadata.")
                try:
                    result["sha256"] = sha256_file(target)
                except OSError as exc:
                    result["status"] = "partial"
                    result["warnings"].append(f"Could not hash saved bytes: {exc}")
        except KeyboardInterrupt:
            result["warnings"].append("Cancelled during extraction or hashing; file checks did not finish.")
            result["status"] = "partial" if target.is_file() else "failed"
            if target.is_file():
                result["saved_path"] = relative.as_posix()
                result["actual_size"] = target.stat().st_size
            status = "cancelled"
            break
    source_verification = "not_completed_due_to_cancellation"
    if status != "cancelled":
        try:
            check_identity(source)
            source_verification = "live_volume_identity_only" if live else "unchanged"
        except KeyboardInterrupt:
            status = "cancelled"
        except (RecoveryError, OSError):
            status = "source_changed"
            source_verification = "changed_or_unreadable"
    result_report = {
        "schema_version": 1, "prototype_version": __version__, "status": status,
        "source": source, "source_verification": source_verification,
        "session": str(session.resolve()), "results": results,
        "selected_count": len(candidates), "processed_count": len(results),
        "exported_unverified_count": sum(r["status"] == "exported_unverified" for r in results),
        "partial_count": sum(r["status"] == "partial" for r in results),
        "failed_count": sum(r["status"] == "failed" for r in results),
        "skipped_count": sum(r["status"] == "skipped" for r in results),
        "integrity_note": "Successful extraction and a computed SHA-256 do not prove original content.",
    }
    write_json(directory / "recovery.json", result_report)
    return result_report
