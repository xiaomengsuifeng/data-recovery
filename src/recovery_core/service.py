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
from . import carving, ntfs_log, jpeg
from .common import (RecoveryError, check_identity, image_identity, new_directory,
                     read_json, regular_file, sha256_file, write_json)
from .recycle_bin import InvalidRecycleMetadata, pair_key, parse_dollar_i
from .tsk import ATTRIBUTE_ID, Tsk
from .control import checkpoint, progress
from .filesystems import identify, boot_format, exfat_geometry
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
    filesystem, actual_sector = boot_format(boot)
    if actual_sector != sector_size:
        raise RecoveryError("Filesystem sector size differs from --sector-size.")
    if filesystem == "exfat":
        exfat_geometry(image, offset, sector_size)


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
         deep_log: bool = False, deep_jpeg: bool = False, reassemble_jpeg: bool = False) -> dict:
    options = dict(max_candidates=max_candidates, deep_png=deep_png, deep_log=deep_log,
                   deep_jpeg=deep_jpeg, reassemble_jpeg=reassemble_jpeg)
    _validate_scan_options(options)
    if isinstance(image, VolumeSource):
        if deep_log or deep_png or deep_jpeg or reassemble_jpeg:
            raise RecoveryError("内容深度扫描和旧日志恢复仅支持镜像文件，请先采集镜像。")
        identity = volume_identity(image.mount)
        ensure_safe_locations(identity, output)
        _, sector_size = boot_format(read_volume_boot(identity))
        image, offset = Path(identity["path"]), 0
    else:
        image = regular_file(image)
        validate_geometry(image, offset, sector_size)
        progress("source", message="核对源镜像")
        identity = image_identity(image)
    filesystem = identify(image, offset, sector_size)
    if deep_log and filesystem != "ntfs":
        raise RecoveryError("exFAT 没有 NTFS 旧日志，请关闭旧日志选项。")
    directory = new_directory(output)
    state = {"schema_version": 1, "kind": "scan_checkpoint", "checkpoint_version": 1,
             "prototype_version": __version__, "status": "running", "source": identity,
             "offset": offset, "sector_size": sector_size, "filesystem": filesystem,
             "backend": {"name": "The Sleuth Kit", "versions": backend.versions},
             "options": options, "stages": {}}
    return _continue_scan(directory, state, backend)


def _validate_scan_options(options):
    if (not isinstance(options, dict) or set(options) !=
            {"max_candidates", "deep_png", "deep_log", "deep_jpeg", "reassemble_jpeg"}
            or type(options["max_candidates"]) is not int or not 1 <= options["max_candidates"] <= 100000
            or any(type(options[key]) is not bool for key in ("deep_png", "deep_log", "deep_jpeg", "reassemble_jpeg"))
            or (options["reassemble_jpeg"] and not options["deep_jpeg"])):
        raise RecoveryError("Invalid deep scan options or --max-candidates (1..100000).")


def resume_scan(session: Path, *, backend: Tsk) -> dict:
    directory = session.resolve(strict=True)
    state = read_json(directory / "scan-progress.json")
    if (state.get("kind") != "scan_checkpoint" or state.get("checkpoint_version") != 1
            or state.get("prototype_version") != __version__
            or not isinstance(state.get("source"), dict) or state["source"].get("kind") == "windows_volume"
            or not isinstance(state.get("stages"), dict)
            or set(state["stages"]) - {"metadata", "png", "jpeg", "log"}
            or state.get("backend", {}).get("versions") != backend.versions):
        raise RecoveryError("扫描断点格式或版本不匹配；实时卷无法断点续扫，请先采集镜像。")
    _validate_scan_options(state.get("options"))
    for stage in state["stages"].values():
        if not isinstance(stage, dict) or set(stage) not in ({"partial"}, {"result"}):
            raise RecoveryError("Invalid saved scan stage.")
    check_identity(state["source"])
    image = regular_file(Path(state["source"]["path"]))
    validate_geometry(image, state["offset"], state["sector_size"])
    if identify(image, state["offset"], state["sector_size"]) != state["filesystem"]:
        raise RecoveryError("扫描源文件系统与断点不一致。")
    return _continue_scan(directory, state, backend)


def _continue_scan(directory, state, backend):
    from .journal import atomic_json, exclusive_job, run_stage
    from . import jpeg
    identity, options = state["source"], state["options"]
    image, offset, sector_size = Path(identity["path"]), state["offset"], state["sector_size"]
    maximum = options["max_candidates"]
    path = None if identity.get("kind") == "windows_volume" else directory / "scan-progress.json"
    with exclusive_job(directory):
        # A crash after publishing the final session must not overwrite it.
        if (directory / "session.json").exists():
            report = read_json(directory / "session.json")
            _validate_candidates(report.get("candidates"))
            if report.get("source") != identity or report.get("status") != "scanned":
                raise RecoveryError("扫描目录已有其他结果。")
            return report
        state["status"] = "running"
        if path:
            atomic_json(path, state)
        try:
            def metadata():
                candidates, warnings = backend.scan(image, offset, sector_size)
                if len(candidates) > maximum:
                    raise RecoveryError(f"More than {maximum} candidates; increase --max-candidates explicitly.")
                for candidate in candidates:
                    checkpoint()
                    candidate["id"] = _candidate_id(candidate["inode"], candidate["observed_path"])
                    root = candidate["observed_path"].replace("\\", "/").lstrip("/").split("/", 1)[0].lower()
                    if root in ("$orphanfiles", "$recycle.bin"):
                        candidate["original_path"] = None
                        candidate["path_evidence"] = "generated_or_recycle_path_only"
                        candidate["warnings"].append("Observed system/recovery path does not establish the original directory.")
                progress("recycle", message="关联回收站名称与内容")
                _associate_recycle(candidates, backend, image, offset, sector_size, directory)
                return candidates, warnings
            candidates, warnings = run_stage(state, "metadata", path, metadata)
            _validate_candidates(candidates)
            summaries = {"carving": None, "jpeg": None, "ntfs_log": None}
            for enabled, stage, key, function, extra in (
                    (options["deep_png"], "png", "carving", carving.scan_png, {}),
                    (options["deep_jpeg"], "jpeg", "jpeg", jpeg.scan_jpeg, {"reassemble": options["reassemble_jpeg"]}),
                    (options["deep_log"], "log", "ntfs_log", ntfs_log.scan_log, {})):
                if not enabled:
                    continue
                recovered, summary = run_stage(state, stage, path, lambda: function(
                    image, offset, sector_size, backend=backend, max_candidates=maximum - len(candidates), **extra))
                for candidate in recovered:
                    if stage in ("png", "jpeg"):
                        fingerprint = f"{stage}:{candidate['carving']['image_offset']}"
                    else:
                        info = candidate["ntfs_log"]
                        fingerprint = f"log:{info['record']}:{info['sequence']}:{info['initialization_lsn']}:{info['file_offset']}"
                    candidate["id"] = _candidate_id(fingerprint, candidate["observed_path"])
                _validate_candidates(recovered)
                candidates.extend(recovered)
                summaries[key] = summary
                warnings.extend(summary.get("warnings", []))
                if stage == "jpeg":
                    warnings.append(f"JPEG 查找：{summary['candidate_count']} 项，其中碎片重组 "
                                    f"{summary['reconstructed_count']} 项；歧义跳过 {summary['ambiguous_headers']} 项，"
                                    f"搜索达到边界 {summary['limited_searches']} 次。重组结果需逐张核对。")
            _validate_candidates(candidates)
            if len(candidates) > maximum:
                raise RecoveryError("Saved scan exceeds the candidate limit.")
            progress("source", message="复核扫描源")
            check_identity(identity)
            report = {"schema_version": 1, "prototype_version": __version__, "status": "scanned",
                "source": identity, "offset": offset, "sector_size": sector_size, "filesystem": state["filesystem"],
                "backend": state["backend"], "candidate_count": len(candidates), "candidates": candidates,
                "warnings": warnings, "scan_options": {k: v for k, v in options.items() if k != "max_candidates"},
                **summaries,
                "limitations": [
                    "Deleted NTFS/exFAT records and allocation pointers may refer to reused content; export does not prove integrity.",
                    "Content carving generates names, may duplicate metadata candidates, and cannot establish original paths.",
                    "JPEG reconstruction is bounded and uncertain; unsupported fragmentation is not silently declared recovered.",
                    "NTFS log evidence is format-dependent; missing ranges remain explicit fragments.",
                    "No EFS/BitLocker decryption, overwritten-byte recovery, or TRIM reversal is provided."]}
            if identity.get("kind") == "windows_volume":
                report["limitations"].append("Live volume identity is checked, but the OS may change its contents during recovery.")
            write_json(directory / "session.json", report)
            state["status"] = "completed"
            if path:
                atomic_json(path, state)
            return report
        except BaseException as exc:
            state["status"] = "paused" if isinstance(exc, KeyboardInterrupt) else "failed"
            if path:
                try:
                    atomic_json(path, state)
                except (OSError, RecoveryError):
                    pass  # Preserve the earlier durable checkpoint and original failure.
            raise


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
        elif method == "jpeg_carving":
            jpeg.validate_candidate(candidate)
        elif method == "ntfs_log":
            ntfs_log.validate_candidate(candidate)
        elif method in ("ntfs_metadata", "exfat_metadata"):
            pattern = ATTRIBUTE_ID if method == "ntfs_metadata" else re.compile(r"[0-9]{1,20}")
            if (not isinstance(candidate.get("inode"), str) or not pattern.fullmatch(candidate["inode"])
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
    elif candidate.get("recovery_method") == "jpeg_carving":
        image = regular_file(image)
        jpeg.extract_jpeg(image, offset, sector_size, candidate, output, backend=backend)
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
