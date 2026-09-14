"""Compare saved recovery bytes with an independent original-file manifest.

Report hashes are claims only. This module re-reads regular files under the
export root, then performs one-to-one matching, reserving exact path matches
before assigning identical content found under other names.
"""

from __future__ import annotations

from collections import defaultdict, deque
import os
from pathlib import Path
import re
import stat

from .common import RecoveryError, read_json, sha256_file


_SHA256 = re.compile(r"[0-9a-fA-F]{64}", re.ASCII)
_DRIVE = re.compile(r"^[A-Za-z]:/")
_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_RECOVERY_STATUSES = {"completed", "source_changed", "cancelled"}
_RESULT_STATUSES = {"exported_unverified", "partial", "failed", "skipped"}


def _path_key(value: object) -> str | None:
    """Compare volume paths conservatively; never infer a path from a basename."""
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        return None
    value = value.replace("\\", "/")
    if value.startswith("//"):
        return None  # A UNC/device namespace needs a separate volume identity.
    if _DRIVE.match(value):
        value = value[3:]
    elif value.startswith("/"):
        value = value[1:]
    if ":" in value:
        return None
    parts = value.split("/")
    if any(not part or part in (".", "..") for part in parts):
        return None
    return "/" + "/".join(parts).translate(_LOWER)


def _size(value: object, label: str, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if type(value) is not int or not 0 <= value <= (1 << 64) - 1:
        raise RecoveryError(f"{label} must be an unsigned 64-bit integer.")
    return value


def _digest(value: object, label: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise RecoveryError(f"{label} must contain exactly 64 hexadecimal characters.")
    return value.lower()


def _is_link(info: os.stat_result) -> bool:
    # Windows junctions and other reparse points must not bypass symlink checks.
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _saved_parts(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise RecoveryError("saved_path must be a nonempty relative export path.")
    value = value.replace("\\", "/")
    if value.startswith("/") or ":" in value:
        raise RecoveryError("Absolute, device, and alternate-stream saved_path values are forbidden.")
    parts = tuple(value.split("/"))
    if any(not part or part in (".", "..") or part.endswith((" ", ".")) for part in parts):
        raise RecoveryError("saved_path contains an ambiguous or escaping component.")
    return parts


def _safe_file(root: Path, parts: tuple[str, ...]) -> Path:
    current = root
    for index, part in enumerate(parts):
        current = current / part
        info = current.lstat()
        if _is_link(info):
            raise RecoveryError(f"Symbolic links and reparse points are forbidden: {'/'.join(parts)}")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise RecoveryError("A saved_path parent is not a directory.")
        if index == len(parts) - 1 and not stat.S_ISREG(info.st_mode):
            raise RecoveryError("A saved_path must identify a regular file.")
    try:
        current.resolve(strict=True).relative_to(root)
    except ValueError as exc:
        raise RecoveryError("saved_path resolves outside the export root.") from exc
    return current


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _load_json(path: Path, label: str) -> dict:
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise RecoveryError(f"{label} must be a regular JSON file.")
        result = read_json(path)
    except (OSError, ValueError) as exc:
        raise RecoveryError(f"Cannot read {label}: {exc}") from exc
    if type(result.get("schema_version")) is not int or result["schema_version"] != 1:
        raise RecoveryError(f"Unsupported {label} schema.")
    return result


def verify(recovery_dir: Path, manifest_path: Path) -> dict:
    """Verify actual exports against a schema-1 manifest, without writing files.

    Rates use all manifest targets as their denominator; zero targets give
    ``None``, never 100%. ``all_targets_verified`` additionally requires a
    completed recovery report. Matching bytes in a cancelled/source-changed
    export remain measurable, but do not establish completion of that task.

    Exact path, filename, and directory statistics are conditional on an exact
    content match. Unknown original paths do not borrow observed-path evidence.
    """
    try:
        if _is_link(Path(recovery_dir).lstat()):
            raise RecoveryError("The export root must not be a symbolic link or reparse point.")
        root = Path(recovery_dir).resolve(strict=True)
        if not root.is_dir():
            raise RecoveryError("The export root must be a directory.")
        report_path = _safe_file(root, ("recovery.json",))
    except OSError as exc:
        raise RecoveryError(f"Cannot open the export root/report: {exc}") from exc
    report = _load_json(report_path, "recovery report")
    manifest = _load_json(Path(manifest_path), "manifest")
    recovery_status = report.get("status")
    if not isinstance(recovery_status, str) or recovery_status not in _RECOVERY_STATUSES:
        raise RecoveryError("Recovery report has an unsupported status.")
    records = report.get("results")
    files = manifest.get("files")
    if not isinstance(records, list) or not isinstance(files, list):
        raise RecoveryError("Recovery results and manifest files must be arrays.")

    targets = []
    target_paths = set()
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            raise RecoveryError(f"Manifest target {index} must be an object.")
        key = _path_key(item.get("original_path"))
        if key is None:
            raise RecoveryError(f"Manifest target {index} needs an unambiguous original_path.")
        if key in target_paths:
            raise RecoveryError("Manifest contains duplicate original paths after volume-path normalization.")
        target_paths.add(key)
        targets.append({"original_path": item["original_path"], "path_key": key,
                        "expected_size": _size(item.get("size"), "Manifest size"),
                        "expected_sha256": _digest(item.get("sha256"), "Manifest sha256")})

    validated = []
    candidate_ids, saved_keys = set(), set()
    for index, item in enumerate(records):
        if not isinstance(item, dict):
            raise RecoveryError(f"Recovery result {index} must be an object.")
        candidate_id = item.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id or candidate_id in candidate_ids:
            raise RecoveryError("Recovery candidate IDs must be nonempty and unique strings.")
        candidate_ids.add(candidate_id)
        status = item.get("status")
        if not isinstance(status, str) or status not in _RESULT_STATUSES:
            raise RecoveryError("Recovery result has an unsupported status.")
        for field in ("original_path", "observed_path"):
            if item.get(field) is not None and not isinstance(item[field], str):
                raise RecoveryError(f"Recovery {field} must be a string or null.")
        _size(item.get("expected_size"), "Recovery expected_size", optional=True)
        _size(item.get("actual_size"), "Recovery actual_size", optional=True)
        _digest(item.get("sha256"), "Recovery sha256", optional=True)
        saved = item.get("saved_path")
        parts = _saved_parts(saved) if saved is not None else None
        if parts is not None:
            key = "/".join(parts).translate(_LOWER)
            if key in saved_keys:
                raise RecoveryError("Duplicate saved_path values would double-count an output.")
            saved_keys.add(key)
        elif status in {"exported_unverified", "partial"}:
            raise RecoveryError("Exported/partial results must identify their saved_path.")
        validated.append((item, parts))

    outputs = []
    usable = []
    inode_keys = set()
    for item, parts in validated:
        output = {"candidate_id": item["candidate_id"], "saved_path": item.get("saved_path"),
                  "reported_status": item["status"], "status": "not_exported",
                  "actual_size": None, "actual_sha256": None}
        outputs.append(output)
        if parts is None:
            continue
        try:
            path = _safe_file(root, parts)
            before = path.stat()
            # Prevent different hard-link names from multiplying one saved file.
            inode_key = (before.st_dev, before.st_ino)
            if before.st_ino and inode_key in inode_keys:
                raise RecoveryError("Multiple saved paths refer to the same output file.")
            if before.st_ino:
                inode_keys.add(inode_key)
            if item["status"] in {"failed", "skipped"}:
                continue
            digest = sha256_file(path)
            after = _safe_file(root, parts).stat()
            if _identity(before) != _identity(after):
                output["status"] = "changed_during_verification"
                continue
        except FileNotFoundError:
            output["status"] = "missing"
            continue
        except OSError as exc:
            output.update(status="unreadable", error=str(exc))
            continue
        output.update(status="read_verified", actual_size=after.st_size, actual_sha256=digest,
                      report_size_matches=(None if item.get("actual_size") is None else item["actual_size"] == after.st_size),
                      report_hash_matches=(None if item.get("sha256") is None else item["sha256"].lower() == digest))
        if item.get("content_status") == "fragment":
            output["excluded_reason"] = "known_fragment"
            continue
        usable.append({**output, "original_path": item.get("original_path"),
                       "path_key": _path_key(item.get("original_path"))})

    by_exact = defaultdict(deque)
    by_content = defaultdict(deque)
    by_path = defaultdict(list)
    for index, output in enumerate(usable):
        content_key = (output["actual_size"], output["actual_sha256"])
        by_content[content_key].append(index)
        by_exact[(output["path_key"], *content_key)].append(index)
        if output["path_key"] is not None:
            by_path[output["path_key"]].append(output["candidate_id"])
    assignments, consumed = {}, set()
    # Reserve all exact path matches first, regardless of manifest ordering.
    for index, target in enumerate(targets):
        queue = by_exact[(target["path_key"], target["expected_size"], target["expected_sha256"])]
        if queue:
            chosen = queue.popleft()
            assignments[index] = chosen
            consumed.add(chosen)
    for index, target in enumerate(targets):
        if index in assignments:
            continue
        queue = by_content[(target["expected_size"], target["expected_sha256"])]
        while queue and queue[0] in consumed:
            queue.popleft()
        if queue:
            chosen = queue.popleft()
            assignments[index] = chosen
            consumed.add(chosen)

    correct_paths = correct_filenames = correct_directories = 0
    for index, target in enumerate(targets):
        path_key = target.pop("path_key")
        output = usable[assignments[index]] if index in assignments else None
        path_matches = bool(output and output["path_key"] == path_key)
        filename_matches = bool(output and output["path_key"] and output["path_key"].rsplit("/", 1)[1] == path_key.rsplit("/", 1)[1])
        directory_matches = bool(output and output["path_key"] and output["path_key"].rsplit("/", 1)[0] == path_key.rsplit("/", 1)[0])
        correct_paths += path_matches
        correct_filenames += filename_matches
        correct_directories += directory_matches
        target.update(content_matches=output is not None, path_matches=path_matches,
                      filename_matches=filename_matches, directory_matches=directory_matches,
                      matched_candidate_id=output["candidate_id"] if output else None,
                      matched_saved_path=output["saved_path"] if output else None,
                      reported_original_path=output["original_path"] if output else None,
                      status="exact_content" if output else ("content_mismatch_or_consumed" if by_path[path_key] else "not_found"),
                      path_candidate_ids=by_path[path_key])
    total, exact = len(targets), len(assignments)
    return {"schema_version": 1, "recovery_status": recovery_status,
            "recovery_completed": recovery_status == "completed",
            "all_targets_verified": bool(total and exact == total and correct_paths == total and recovery_status == "completed"),
            "total_targets": total, "exact_content_matches": exact,
            "correct_paths": correct_paths, "correct_filenames": correct_filenames,
            "correct_directories": correct_directories,
            "content_match_rate": exact / total if total else None,
            "path_match_rate": correct_paths / total if total else None,
            "filename_match_rate": correct_filenames / total if total else None,
            "directory_match_rate": correct_directories / total if total else None,
            "verified_output_files": len(usable), "targets": targets, "outputs": outputs}
