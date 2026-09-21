from __future__ import annotations

import hashlib
import os
import re
import tempfile
import uuid
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

from . import __version__
from . import carving, ntfs_log
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
         sector_size: int = 512, max_candidates: int = 10000, deep_png: bool = False,
         deep_log: bool = False) -> dict:
    if type(max_candidates) is not int or not 1 <= max_candidates <= 100000:
        raise RecoveryError("--max-candidates must be between 1 and 100000 for this prototype.")
    if type(deep_png) is not bool:
        raise RecoveryError("Invalid PNG deep-scan option.")
    if type(deep_log) is not bool:
        raise RecoveryError("Invalid NTFS log scan option.")
    if deep_log and isinstance(image, VolumeSource):
        raise RecoveryError("旧日志恢复目前仅支持 NTFS 镜像文件。")
    if deep_png and isinstance(image, VolumeSource):
        raise RecoveryError("PNG 深度扫描目前仅支持镜像文件，请先使用 NTFS 镜像。")
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
    carving_report = None
    if deep_png:
        carved, carving_report = carving.scan_png(image, offset, sector_size, backend=backend,
                                                  max_candidates=max_candidates - len(candidates))
        for candidate in carved:
            candidate["id"] = _candidate_id(f"png:{candidate['carving']['image_offset']}", candidate["observed_path"])
        candidates.extend(carved)
        warnings.append("PNG 深度扫描结果原名与目录未知，可能与文件记录结果重复；仅支持未分配空间中的连续静态 PNG，最大 256 MiB。")
    log_report = None
    if deep_log:
        historical, log_report = ntfs_log.scan_log(image, offset, sector_size, backend=backend,
                                                  max_candidates=max_candidates - len(candidates))
        for candidate in historical:
            info = candidate["ntfs_log"]
            candidate["id"] = _candidate_id(
                f"log:{info['record']}:{info['sequence']}:{info['initialization_lsn']}:{info['file_offset']}",
                candidate["observed_path"])
        candidates.extend(historical)
        warnings.append("旧日志恢复仅支持部分 NTFS 日志格式；历史名称和内容需核对，有缺口的数据单独标为片段。")
    progress("source", message="复核扫描源")
    check_identity(identity)
    report = {
        "schema_version": 1, "prototype_version": __version__, "status": "scanned",
        "source": identity, "offset": offset, "sector_size": sector_size,
        "backend": {"name": "The Sleuth Kit", "versions": backend.versions},
        "candidate_count": len(candidates), "candidates": candidates, "warnings": warnings,
        "scan_options": {"deep_png": deep_png, "deep_log": deep_log}, "carving": carving_report, "ntfs_log": log_report,
        "limitations": [
            "Metadata recovery supports deleted NTFS unnamed data attributes.",
            "Deleted directories are visited separately; damaged or reused directory records may remain incomplete.",
            "Deleted allocation pointers can reference reused content; export does not prove integrity.",
            "Optional PNG carving checks contiguous unallocated image bytes and chunk CRCs; original names and paths are unknown.",
            "Optional log evidence recovery reads historical nonresident runs in reused MFT generations; fragments are explicitly partial.",
            "No fragmented-file carving, other content formats, EFS/BitLocker decryption, or TRIM reversal is implemented.",
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
        method = candidate.get("recovery_method", "ntfs_metadata")
        if method == "png_carving":
            carving.validate_candidate(candidate)
        elif method == "ntfs_log":
            ntfs_log.validate_candidate(candidate)
        elif method == "ntfs_metadata":
            if (not isinstance(candidate.get("inode"), str) or not ATTRIBUTE_ID.fullmatch(candidate["inode"])
                    or "carving" in candidate or "ntfs_log" in candidate):
                raise RecoveryError("Invalid NTFS attribute ID or extraction descriptor.")
        else:
            raise RecoveryError("Unsupported recovery method.")
        if type(candidate.get("size")) is not int or not 0 <= candidate["size"] < 1 << 63:
            raise RecoveryError("Invalid candidate size.")
        if not isinstance(candidate.get("observed_path"), str) or not candidate["observed_path"]:
            raise RecoveryError("Missing observed path.")
        if candidate.get("original_path") is not None and not isinstance(candidate["original_path"], str):
            raise RecoveryError("Invalid original path.")
        if candidate.get("kind") not in ("file", "recycle_metadata"):
            raise RecoveryError("Unsupported candidate kind.")


def extract_candidate(image: Path, offset: int, sector_size: int, candidate: dict, output, *, backend) -> None:
    """One extraction path for preview and export; session descriptors are validated by callers."""
    if candidate.get("recovery_method") == "png_carving":
        image = regular_file(image)  # Carving must never read a live device.
        carving.extract_png(image, offset, sector_size, candidate, output, backend=backend)
    elif candidate.get("recovery_method") == "ntfs_log":
        image = regular_file(image)
        ntfs_log.extract_log(image, offset, sector_size, candidate, output, backend=backend)
    else:
        backend.extract(image, offset, sector_size, candidate["inode"], output, candidate["size"])


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
    source_error = None
    for candidate in candidates:
        try:
            checkpoint()
        except KeyboardInterrupt:
            status = "cancelled"
            break
        result = {
            "candidate_id": candidate["id"], "original_path": candidate["original_path"],
            "observed_path": candidate["observed_path"], "expected_size": candidate["size"],
            "recovery_method": candidate.get("recovery_method", "ntfs_metadata"),
            "path_evidence": candidate.get("path_evidence"),
            "actual_size": 0, "sha256": None, "saved_path": None,
            "status": "skipped", "warnings": list(candidate.get("warnings", [])),
        }
        if candidate.get("recovery_method") == "ntfs_log":
            result.update(content_status=candidate["content_status"],
                          original_size=candidate["ntfs_log"]["original_size"],
                          file_offset=candidate["ntfs_log"]["file_offset"])
        results.append(result)
        if candidate["size"] > max_file_bytes:
            result["warnings"].append("File exceeds --max-file-bytes; it was not extracted.")
            continue
        import shutil
        try:
            free = shutil.disk_usage(directory).free
        except OSError as exc:
            result["status"] = "failed"
            result["warnings"].append(f"无法读取目标磁盘剩余空间：{exc}")
            break
        if free < candidate["size"] + 1024 * 1024:
            result["status"] = "failed"
            result["warnings"].append("目标磁盘可用空间不足，未开始写入此文件。")
            continue
        relative = Path("files") / safe_relative_path(
            candidate["original_path"] or candidate["observed_path"])
        fragment = candidate.get("recovery_method") == "ntfs_log" and candidate["content_status"] == "fragment"
        if fragment:
            relative = relative.with_name(relative.stem + f".fragment-{candidate['ntfs_log']['file_offset']:08x}" + relative.suffix)
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
                # Inherit the private output-root ACL on Windows. mode=0o700
                # would replace the explicit user grant with OWNER RIGHTS.
                target.parent.mkdir(parents=True, mode=0o777 if os.name == "nt" else 0o700, exist_ok=True)
                with target.open("xb") as stream:
                    extract_candidate(image, offset, sector_size, candidate, stream, backend=backend)
                result["status"] = "partial" if fragment else "exported_unverified"
            except (RecoveryError, OSError) as exc:
                result["warnings"].append(str(exc))
                result["status"] = "partial" if target.is_file() else "failed"
                if live:
                    try:
                        check_identity(source)
                    except (RecoveryError, OSError) as identity_error:
                        source_error = str(identity_error)
                        status = "source_changed"
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
            if source_error is not None:
                break
        except OSError as exc:
            result["status"] = "partial" if result["saved_path"] else "failed"
            result["warnings"].append(f"无法继续读取或保存此文件：{exc}")
        except KeyboardInterrupt:
            result["warnings"].append("Cancelled during extraction or hashing; file checks did not finish.")
            result["status"] = "partial" if result["saved_path"] else "failed"
            try:
                if target.is_file():
                    result["status"] = "partial"
                    result["saved_path"] = relative.as_posix()
                    result["actual_size"] = target.stat().st_size
            except OSError as exc:
                result["warnings"].append(f"取消后无法检查已保存的文件：{exc}")
            status = "cancelled"
            break
    source_verification = "not_completed_due_to_cancellation"
    if status != "cancelled":
        try:
            check_identity(source)
            source_verification = "live_volume_identity_only" if live else "unchanged"
        except KeyboardInterrupt:
            status = "cancelled"
        except (RecoveryError, OSError) as exc:
            status = "source_changed"
            source_verification = "changed_or_unreadable"
            source_error = str(exc)
    if source_error is not None and status != "cancelled":
        status = "source_changed"
        source_verification = "changed_or_unreadable"
    result_report = {
        "schema_version": 1, "prototype_version": __version__, "status": status,
        "source": source, "source_verification": source_verification,
        "source_error": source_error,
        "session": str(session.resolve()), "destination": str(directory), "results": results,
        "selected_count": len(candidates), "processed_count": len(results),
        "exported_unverified_count": sum(r["status"] == "exported_unverified" for r in results),
        "partial_count": sum(r["status"] == "partial" for r in results),
        "failed_count": sum(r["status"] == "failed" for r in results),
        "skipped_count": sum(r["status"] == "skipped" for r in results),
        "integrity_note": "Successful extraction and a computed SHA-256 do not prove original content.",
    }
    try:
        write_json(directory / "recovery.json", result_report)
    except OSError as exc:
        # A full or detached output disk must not discard the in-memory report.
        # The existing session is on a separately checked safe working disk.
        fallback = session / ("recovery-" + uuid.uuid4().hex + ".json")
        result_report["report_path"] = str(fallback.resolve())
        result_report["report_warning"] = f"目标位置无法保存完整报告，已改存扫描记录目录：{exc}"
        try:
            write_json(fallback, result_report)
        except OSError as fallback_error:
            raise RecoveryError("恢复报告无法写入目标位置或扫描记录目录。请检查目标和工作磁盘的连接、空间及权限。") from fallback_error
    return result_report
