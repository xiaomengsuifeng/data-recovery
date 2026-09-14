"""Validate staged synthetic NTFS fixtures against their supplied original copies.

Only the image and its geometry reach the recovery backend. Original copies,
stage manifests and recycle-bin ground truth belong to the verification side.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from .common import RecoveryError, image_identity, new_directory, write_json
from .control import checkpoint, progress
from .recycle_bin import pair_key
from .service import recover, scan, validate_geometry
from .verification import _digest, _is_link, _path_key, _safe_file, _saved_parts, _size, verify


STAGES = ("before-delete", "after-direct-delete", "after-move-to-recycle-bin", "after-empty-recycle-bin")
POST_WRITE_STAGE = "after-additional-writes"
SCENARIOS = {"direct-file", "direct-directory", "recycle-bin", "retained-control"}
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_ORIGINALS = 10000


def _json_file(root: Path, name: str):
    path = _safe_file(root, _saved_parts(name))
    if path.stat().st_size > MAX_METADATA_BYTES:
        raise RecoveryError(f"Fixture metadata exceeds 8 MiB: {name}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise RecoveryError(f"Invalid fixture JSON: {name}") from exc


def _manifest(root: Path, name: str, fixture_id: str) -> dict:
    value = _json_file(root, name)
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or value.get("fixture_id") != fixture_id
            or not isinstance(value.get("files"), list) or len(value["files"]) > MAX_ORIGINALS):
        raise RecoveryError(f"Invalid fixture manifest: {name}")
    return value


def _checked_bytes(root: Path, relative: str, size: int, digest: str) -> dict:
    path = _safe_file(root, _saved_parts(relative))
    identity = image_identity(path)
    _safe_file(root, _saved_parts(relative))
    if identity["size"] != size or identity["sha256"] != digest:
        raise RecoveryError(f"Fixture size/SHA-256 mismatch: {relative}")
    return identity


def _originals(root: Path, fixture_id: str) -> dict:
    manifest = _manifest(root, "originals.manifest.json", fixture_id)
    if not manifest["files"]:
        raise RecoveryError("Fixture original copies must not be empty.")
    originals = {}
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise RecoveryError("An original must be an object.")
        key = _path_key(item.get("original_path"))
        relative = item.get("relative_path")
        parts = _saved_parts(relative)
        if (key is None or key in originals or key != _path_key(relative)
                or not isinstance(item.get("scenario"), str) or item["scenario"] not in SCENARIOS
                or item.get("synthetic") is not True):
            raise RecoveryError("Invalid, duplicate or inconsistent original path/scenario.")
        size = _size(item.get("size"), "Original size")
        digest = _digest(item.get("sha256"), "Original sha256")
        _checked_bytes(root, "/".join(("originals", *parts)), size, digest)
        originals[key] = dict(item, size=size, sha256=digest)
    scenarios = {item["scenario"] for item in originals.values()}
    if not {"recycle-bin", "retained-control"}.issubset(scenarios) or not any(
            scenario.startswith("direct-") for scenario in scenarios):
        raise RecoveryError("Fixture needs direct-delete, recycle-bin and retained-control originals.")
    return originals


def _check_recycle_evidence(root: Path, originals: dict):
    evidence = _json_file(root, "recycle-evidence.json")
    expected = {key: item for key, item in originals.items() if item["scenario"] == "recycle-bin"}
    if not isinstance(evidence, list) or len(evidence) != len(expected):
        raise RecoveryError("Recycle evidence must cover all recycled originals.")
    seen, pairs = set(), set()
    for row in evidence:
        if not isinstance(row, dict):
            raise RecoveryError("Invalid recycle evidence record.")
        key = _path_key(row.get("original_path"))
        index, content = row.get("index_path"), row.get("content_path")
        if not isinstance(index, str) or not isinstance(content, str):
            raise RecoveryError("Invalid recycle evidence paths.")
        pair = pair_key(index)
        if (key not in expected or key in seen or pair is None or pair in pairs
                or pair != pair_key(content)
                or not index.replace("\\", "/").rsplit("/", 1)[-1].startswith("$I")
                or not content.replace("\\", "/").rsplit("/", 1)[-1].startswith("$R")
                or type(row.get("index_version")) is not int or row["index_version"] not in (1, 2)
                or _size(row.get("size"), "Recycle size") != expected[key]["size"]
                or _digest(row.get("sha256"), "Recycle sha256") != expected[key]["sha256"]):
            raise RecoveryError("Recycle evidence differs from the original copies or has duplicate pairs.")
        seen.add(key)
        pairs.add(pair)


def load_fixture(fixture: Path) -> dict:
    """Check the whole bundle before creating output or starting a recovery scan."""
    if _is_link(fixture.lstat()):
        raise RecoveryError("Fixture root must not be a symbolic link or reparse point.")
    root = fixture.resolve(strict=True)
    if not root.is_dir():
        raise RecoveryError("Fixture must be a directory.")
    result = _json_file(root, "result.json")
    if not isinstance(result, dict) or result.get("status") != "complete":
        raise RecoveryError("Fixture generation did not complete; keep its logs and generate a new fixture.")
    fixture_id = result.get("fixture_id")
    if not isinstance(fixture_id, str) or not re.fullmatch("[0-9a-f]{32}", fixture_id):
        raise RecoveryError("Invalid fixture ID.")
    stages = _json_file(root, "stages.json")
    if (not isinstance(stages, list) or len(stages) not in (len(STAGES), len(STAGES) + 1)
            or any(not isinstance(stage, dict) for stage in stages)
            or tuple(stage.get("stage") for stage in stages) not in (STAGES, STAGES + (POST_WRITE_STAGE,))
            or result.get("stages") != stages):
        raise RecoveryError("Fixture needs four ordered stages, optionally followed by the additional-writes stage.")
    environment = _json_file(root, "environment.json")
    if not isinstance(environment, dict) or environment.get("fixture_id") != fixture_id:
        raise RecoveryError("Fixture environment ID differs from its result.")
    if environment.get("profile", "basic") not in ("basic", "expanded"):
        raise RecoveryError("Unknown fixture profile.")
    pressure_mib = environment.get("write_pressure_mib", 0)
    if type(pressure_mib) is not int or not 0 <= pressure_mib <= 64 or bool(pressure_mib) != (len(stages) == 5):
        raise RecoveryError("Additional-writes stage differs from its configured write pressure.")
    if pressure_mib:
        pressure = _json_file(root, "write-pressure.json")
        if (not isinstance(pressure, dict) or pressure.get("fixture_id") != fixture_id
                or type(pressure.get("schema_version")) is not int or pressure["schema_version"] != 1
                or type(pressure.get("bulk_mib")) is not int
                or pressure.get("stage") != POST_WRITE_STAGE or pressure.get("bulk_mib") != pressure_mib
                or pressure.get("small_file_count") != 64 or not isinstance(pressure.get("files"), list)
                or len(pressure["files"]) != 64 + pressure_mib):
            raise RecoveryError("Invalid write-pressure evidence.")
        seen_pressure = set()
        for item in pressure["files"]:
            if not isinstance(item, dict):
                raise RecoveryError("Invalid additional-write file.")
            parts = _saved_parts(item.get("relative_path"))
            key = "/".join(parts).casefold()
            size = _size(item.get("size"), "Additional-write size")
            _digest(item.get("sha256"), "Additional-write sha256")
            if (len(parts) != 3 or parts[:2] != ("Samples", "additional-writes")
                    or key in seen_pressure or size not in (257, 1024 ** 2)):
                raise RecoveryError("Invalid or duplicate additional-write path/size.")
            seen_pressure.add(key)
        if sum(item["size"] == 257 for item in pressure["files"]) != 64:
            raise RecoveryError("Additional-write size distribution differs from the scenario.")
    generator_digest = _digest(environment.get("script_sha256"), "Generator sha256")
    originals = _originals(root, fixture_id)
    _check_recycle_evidence(root, originals)
    checked_stages, used_paths = [], set()
    for row in stages:
        checkpoint()
        name = row["stage"]
        for field in ("image", "manifest"):
            key = "/".join(_saved_parts(row.get(field))).casefold()
            if key in used_paths:
                raise RecoveryError("Fixture stages must use distinct image/manifest files.")
            used_paths.add(key)
        size = _size(row.get("bytes"), "Image size")
        digest = _digest(row.get("sha256"), "Image sha256")
        offset, sector = row.get("offset_sectors"), row.get("sector_size")
        if (type(offset) is not int or offset < 0 or type(sector) is not int
                or sector not in (512, 1024, 2048, 4096) or offset * sector + 512 > size):
            raise RecoveryError(f"Invalid fixture geometry: {name}")
        identity = _checked_bytes(root, row["image"], size, digest)
        validate_geometry(Path(identity["path"]), offset, sector)
        manifest = _manifest(root, row["manifest"], fixture_id)
        if (manifest.get("stage") != name or type(manifest.get("offset_sectors")) is not int
                or manifest["offset_sectors"] != offset or type(manifest.get("sector_size")) is not int
                or manifest["sector_size"] != sector):
            raise RecoveryError(f"Manifest stage/geometry differs from its image: {name}")
        expected = {key: item for key, item in originals.items()
                    if name != "before-delete" and (item["scenario"].startswith("direct-")
                       or (name in ("after-empty-recycle-bin", POST_WRITE_STAGE) and item["scenario"] == "recycle-bin"))}
        targets = {}
        for item in manifest["files"]:
            if not isinstance(item, dict):
                raise RecoveryError("Invalid stage target.")
            key = _path_key(item.get("original_path"))
            if (key not in expected or key in targets
                    or _size(item.get("size"), "Target size") != expected[key]["size"]
                    or _digest(item.get("sha256"), "Target sha256") != expected[key]["sha256"]
                    or item.get("scenario") != expected[key]["scenario"]):
                raise RecoveryError(f"Stage target differs from its deleted original: {name}")
            # Freeze verified references so later edits of the input manifest
            # cannot change the denominator while recovery is in progress.
            targets[key] = {field: expected[key][field] for field in ("original_path", "size", "sha256")}
        if (set(targets) != set(expected) or type(row.get("deleted_target_count")) is not int
                or row["deleted_target_count"] != len(expected)):
            raise RecoveryError(f"Stage target count/membership differs from the deletion scenario: {name}")
        checked_stages.append(dict(row, source=identity,
                                  target_scenarios={key: expected[key]["scenario"] for key in targets},
                                  reference={"schema_version": 1, "files": list(targets.values())}))
    return {"fixture_id": fixture_id, "root": str(root), "generator_sha256": generator_digest,
            "profile": environment.get("profile", "basic"), "write_pressure_mib": pressure_mib,
            "original_count": len(originals),
            "unique_deleted_targets": sum(item["scenario"] != "retained-control" for item in originals.values()),
            "stages": checked_stages}


def _breakdown(targets: list[dict], scenarios: dict) -> dict:
    """Count independently verified targets, never duplicate exported candidates.

    Exact byte sizes describe these samples; they do not infer NTFS residency.
    """
    groups = {"by_scenario": {}, "by_size_bytes": {}}
    for target in targets:
        labels = {"by_scenario": scenarios[_path_key(target["original_path"])],
                  "by_size_bytes": target["expected_size"]}
        for dimension, label in labels.items():
            counts = groups[dimension].setdefault(label, {"total_targets": 0, "exact_content_matches": 0, "correct_paths": 0})
            counts["total_targets"] += 1
            counts["exact_content_matches"] += int(target["content_matches"])
            counts["correct_paths"] += int(target["path_matches"])
    return {dimension: [{"scenario" if dimension == "by_scenario" else "size_bytes": label, **counts}
                        for label, counts in sorted(values.items())] for dimension, values in groups.items()}


def validate_fixture(fixture: Path, output: Path, *, backend, deep_png: bool = False, deep_log: bool = False) -> dict:
    bundle = load_fixture(fixture)
    root = Path(bundle["root"])
    destination = output.absolute().parent.resolve(strict=True) / output.name
    if destination.is_relative_to(root):
        raise RecoveryError("Validation output must be outside the fixture directory.")
    directory = new_directory(output)
    summary = {"schema_version": 1, "fixture_id": bundle["fixture_id"], "status": "passed",
               "scan_options": {"deep_png": deep_png, "deep_log": deep_log},
               "profile": bundle["profile"], "write_pressure_mib": bundle["write_pressure_mib"],
               "reference_kind": "fixture_original_copies", "original_copies_verified": bundle["original_count"],
               "unique_deleted_targets": bundle["unique_deleted_targets"],
               "generator_sha256": bundle["generator_sha256"], "backend": dict(backend.versions),
               "planned_target_observations": sum(row["deleted_target_count"] for row in bundle["stages"]),
               "all_deleted_targets_verified": False, "stages": []}
    # The checked input description is evidence only; it is not given to TSK.
    write_json(directory / "validated-inputs.json", dict(schema_version=1, **bundle))
    for index, stage in enumerate(bundle["stages"]):
        record = {"stage": stage["stage"], "status": "not_run", "total_targets": stage["deleted_target_count"],
                  "exact_content_matches": None, "correct_paths": None, "correct_filenames": None,
                  "correct_directories": None, "verification": None}
        summary["stages"].append(record)
        try:
            progress("fixture", index, len(bundle["stages"]), stage["stage"])
            stage_dir = new_directory(directory / stage["stage"])
            reference_path = stage_dir / "reference.manifest.json"
            write_json(reference_path, stage["reference"])
            image = _safe_file(root, _saved_parts(stage["image"]))
            report = scan(image, stage_dir / "session", backend=backend, offset=stage["offset_sectors"],
                          sector_size=stage["sector_size"], max_candidates=100000, deep_png=deep_png, deep_log=deep_log)
            if any(report["source"][key] != stage["source"][key] for key in ("size", "sha256")):
                raise RecoveryError("Fixture image changed after input validation.")
            recovered = recover(stage_dir / "session", stage_dir / "recovered", backend=backend)
            record["recovery_status"] = recovered["status"]
            record["candidate_count"] = report["candidate_count"]
            record["carving"] = report.get("carving")
            record["ntfs_log"] = report.get("ntfs_log")
            record["recovery_counts"] = {key: recovered[key] for key in (
                "exported_unverified_count", "partial_count", "failed_count", "skipped_count")}
            if recovered["status"] == "cancelled":
                raise KeyboardInterrupt
            result = verify(stage_dir / "recovered", reference_path)
            write_json(stage_dir / "verification.json", result)
            record.update({key: result[key] for key in ("exact_content_matches", "correct_paths",
                                                        "correct_filenames", "correct_directories")})
            record.update(_breakdown(result["targets"], stage["target_scenarios"]))
            record["verification"] = (stage_dir / "verification.json").relative_to(directory).as_posix()
            if recovered["source_verification"] != "unchanged":
                record["status"] = "failed"
                record["error"] = "Fixture image changed or could not be rechecked during recovery."
            elif stage["stage"] == "before-delete":
                record["status"] = "baseline"
            else:
                record["status"] = "passed" if result["all_targets_verified"] else "incomplete"
        except KeyboardInterrupt:
            record["status"] = "cancelled"
            summary["status"] = "cancelled"
            for pending in bundle["stages"][index + 1:]:
                summary["stages"].append({"stage": pending["stage"], "status": "not_run",
                                          "total_targets": pending["deleted_target_count"]})
            break
        except (RecoveryError, OSError, ValueError, KeyError, TypeError) as exc:
            record.update(status="failed", error=str(exc))
    if summary["status"] != "cancelled":
        if any(row["status"] == "failed" for row in summary["stages"]):
            summary["status"] = "failed"
        elif any(row["status"] == "incomplete" for row in summary["stages"]):
            summary["status"] = "incomplete"
    summary["all_deleted_targets_verified"] = summary["status"] == "passed"
    summary["exact_content_observations"] = sum(row.get("exact_content_matches") or 0 for row in summary["stages"])
    summary["correct_path_observations"] = sum(row.get("correct_paths") or 0 for row in summary["stages"])
    write_json(directory / "fixture-validation.json", summary)
    return summary
