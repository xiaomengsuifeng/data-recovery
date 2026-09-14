#!/usr/bin/env python3
"""Exercise PNG deep scanning, preview, export and reopening through real Qt.

Uses only the provided NTFS image. Output hashes are compared with scan bytes,
not independent originals; use validate-fixture separately for recovery rates.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
if "--installed-runtime" not in sys.argv:
    sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import QApplication
import recovery_core
import recovery_desktop
from recovery_core.common import new_directory, read_json, sha256_file, write_json
from recovery_desktop.app import RecoveryWindow, STYLE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tsk-bin", type=Path)
    parser.add_argument("--installed-runtime", action="store_true",
                        help="Use the invoking runtime's packages instead of workspace source (for portable-package tests)")
    args = parser.parse_args()
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
            # Let the worker release files before closing the window.
            while window.worker is not None:
                app.processEvents()
                time.sleep(.01)
            raise RuntimeError("PNG desktop demo timed out")
        if errors:
            raise RuntimeError("; ".join(errors))

    try:
        window.load_image(args.image)
        wait()
        choices = [i for i in range(window.partition.count())
                   if window.partition.itemData(i)["offset"] == args.offset]
        if len(choices) != 1:
            raise RuntimeError("Requested NTFS partition was not uniquely detected by the desktop")
        window.partition.setCurrentIndex(choices[0])
        window.deep_png.setChecked(True)
        window.grab().save(str(output / "01-source.png"))
        window.start_scan()
        wait()
        carved = [item for item in window.model.items if item.get("recovery_method") == "png_carving"]
        if not carved:
            raise RuntimeError("This demonstration requires at least one PNG carving candidate")
        previews = []
        for item in carved:
            window.search.setText(item["observed_path"])
            app.processEvents()
            assert window.proxy.rowCount() == 1
            window.table.setCurrentIndex(window.proxy.index(0, 1))
            window.request_preview()
            wait()
            pixmap = window.preview_image.pixmap()
            assert pixmap is not None and not pixmap.isNull()
            assert "原名与目录未知" in window.preview_note.text()
            assert window.proxy.data(window.proxy.index(0, 4)) == "原目录未知"
            assert window.proxy.data(window.proxy.index(0, 5)) == "深度扫描·生成名称"
            window.select_visible()
            previews.append({"candidate_id": item["id"], "image_offset": item["carving"]["image_offset"],
                             "decoded": True, "original_path": item["original_path"]})
            window.grab().save(str(output / f"preview-{item['id']}.png"))
        window.export_to(output, sorted(window.model.checked))
        wait()
        assert window.pages.currentIndex() == 2
        saved = read_json(window.last_output / "recovery.json")
        assert saved["source_verification"] == "unchanged"
        expected = {item["id"]: item["carving"]["sha256"] for item in carved}
        assert len(saved["results"]) == len(expected)
        for row in saved["results"]:
            assert row["status"] == "exported_unverified" and row["original_path"] is None
            assert sha256_file(window.last_output / row["saved_path"]) == expected[row["candidate_id"]]
        window.grab().save(str(output / "03-recovered.png"))
        window.go_home()
        window.load_report(window.session, read_json(window.session / "session.json"))
        window.search.setText("Carved/PNG/")
        assert window.proxy.rowCount() == len(carved)
        window.resize(1020, 720)
        app.processEvents()
        assert window.preview_image.geometry().bottom() < window.preview_note.geometry().top()
        assert window.preview_note.geometry().bottom() < window.preview_button.geometry().top()
        window.grab().save(str(output / "04-reopened-minimum-size.png"))
        summary = {"schema_version": 1, "status": "passed", "host": sys.platform,
                   "qt_platform": app.platformName(), "carved_count": len(carved), "previews": previews,
                   "core_module": recovery_core.__file__, "desktop_module": recovery_desktop.__file__,
                   "session": str(window.session), "recovered": str(window.last_output),
                   "scan_bytes_match": True, "original_content_verified": False,
                   "note": "GUI and scan-byte regression only; independent originals are checked by validate-fixture."}
        write_json(output / "desktop-png.json", summary)
        print(f"PNG desktop workflow passed: {len(carved)} previewed, exported and reopened")
    finally:
        window.close()
        app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
