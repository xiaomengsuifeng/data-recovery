#!/usr/bin/env python3
"""Run the real NIST NTFS scan/export pipeline and its documented-extent check."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fetch_test_fixture import DELETED_FILE_SHA256, fetch
from recovery_core.common import RecoveryError, new_directory, write_json
from recovery_core.service import recover, scan
from recovery_core.tsk import Tsk
from recovery_core.verification import verify


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New directory for demo evidence")
    parser.add_argument("--fixture-cache", type=Path, default=Path("/tmp/data-recovery-fixtures"))
    parser.add_argument("--tsk-bin", type=Path)
    args = parser.parse_args()
    backend = Tsk(args.tsk_bin)
    image = fetch(args.fixture_cache)
    output = new_directory(args.output)
    session = scan(image, output / "session", backend=backend, offset=61)
    targets = [c for c in session["candidates"] if c["observed_path"] == "/Bunda.txt"]
    if len(targets) != 1 or targets[0]["size"] != 4296:
        raise RecoveryError("The documented deleted Bunda.txt target was not uniquely identified.")
    recover(output / "session", output / "recovered", backend=backend,
            candidate_ids=[targets[0]["id"]])
    manifest = {
        "schema_version": 1,
        "reference_kind": "documented_post_deletion_extent",
        "provenance": "NIST DFR-01 layout, absolute sector 107005, 4296 bytes. This is not a separate original-file copy.",
        "files": [{"original_path": "/Bunda.txt", "size": 4296, "sha256": DELETED_FILE_SHA256}],
    }
    write_json(output / "reference.manifest.json", manifest)
    result = verify(output / "recovered", output / "reference.manifest.json")
    result["reference_kind"] = manifest["reference_kind"]
    result["interpretation"] = "TSK export matches the documented fixture extent; not a consumer recovery-rate estimate or independent pre-deletion original proof."
    write_json(output / "verification.json", result)
    print(json.dumps({"output": str(output), "backend": backend.versions,
                      "reference_extent_match": result["all_targets_verified"],
                      "candidate_count": session["candidate_count"],
                      "interpretation": result["interpretation"]}, ensure_ascii=False, indent=2))
    return 0 if result["all_targets_verified"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RecoveryError, OSError, ValueError, RuntimeError) as exc:
        print(f"Demo failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
