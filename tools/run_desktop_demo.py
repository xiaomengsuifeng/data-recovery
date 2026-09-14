#!/usr/bin/env python3
"""Exercise the actual Qt window and TSK workers on the public NIST image.

Runs without a display when QT_QPA_PLATFORM=offscreen. Does not access live disks.
Screenshots and byte verification are written to a new output directory.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import QApplication
from recovery_desktop.app import RecoveryWindow, STYLE
from recovery_core.common import new_directory, write_json
from recovery_core.verification import verify
from fetch_test_fixture import DELETED_FILE_SHA256, fetch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture-cache", type=Path, default=ROOT / "artifacts/fixtures")
    parser.add_argument("--tsk-bin", type=Path)
    args = parser.parse_args()
    image = fetch(args.fixture_cache)
    output = new_directory(args.output)
    app = QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = RecoveryWindow(args.tsk_bin)
    errors = []
    window.show_error = errors.append
    window.workspace.setText(str(output))
    window.show()

    def wait():
        deadline = time.monotonic() + 900
        while window.worker is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        app.processEvents()
        if window.worker is not None:
            window.cancel_task()
            raise RuntimeError("Desktop demo timed out")
        if errors:
            raise RuntimeError("; ".join(errors))

    window.load_image(image)
    wait()
    assert window.partition.currentData()["offset"] == 61
    window.grab().save(str(output / "01-source.png"))
    window.start_scan()
    wait()
    window.search.setText("Bunda.txt")
    app.processEvents()
    assert window.proxy.rowCount() == 1
    window.table.setCurrentIndex(window.proxy.index(0, 1))
    window.request_preview()
    wait()
    assert window.preview_text.toPlainText()
    window.select_visible()
    window.grab().save(str(output / "02-preview.png"))
    window.export_to(output, sorted(window.model.checked))
    wait()
    assert window.pages.currentIndex() == 2
    window.grab().save(str(output / "03-recovered.png"))
    manifest = {
        "schema_version": 1,
        "reference_kind": "documented_post_deletion_extent",
        "files": [{"original_path": "/Bunda.txt", "size": 4296, "sha256": DELETED_FILE_SHA256}],
    }
    write_json(output / "reference.manifest.json", manifest)
    result = verify(window.last_output, output / "reference.manifest.json")
    result["reference_kind"] = manifest["reference_kind"]
    result["interpretation"] = "Matches the NIST documented post-deletion extent; not an independent pre-deletion original or consumer recovery-rate estimate."
    write_json(output / "verification.json", result)
    # Reopening must retain candidates without rerunning a disk scan.
    session, report = window.session, window.report
    window.go_home()
    window.load_report(session, report)
    assert window.pages.currentIndex() == 1
    window.close()
    app.processEvents()
    summary = {"output": str(output), "session": str(session), "recovered": str(window.last_output),
               "candidate_count": report["candidate_count"], "reference_extent_match": result["all_targets_verified"],
               "desktop_host": sys.platform, "host_platform": platform.platform(),
               "qt_platform": app.platformName(),
               "windows_image_workflow_verified": sys.platform == "win32" and result["all_targets_verified"],
               "windows_execution_verified": False}
    write_json(output / "desktop-demo.json", dict(schema_version=1, **summary))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result["all_targets_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
