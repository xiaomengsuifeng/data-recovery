#!/usr/bin/env python3
"""Exercise historical files and fragments through the real desktop workflow.

This checks bytes against scan descriptors, not original files. Recovery rates
must come from validate-fixture's independent original-copy verification.
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
    parser.add_argument("--installed-runtime", action="store_true")
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
        while window.worker is not None:
            app.processEvents()
            time.sleep(.01)
            if time.monotonic() >= deadline:
                window.cancel_task()
                while window.worker is not None:
                    app.processEvents()
                    time.sleep(.01)
                raise RuntimeError("Historical file desktop demo timed out")
        app.processEvents()
        if errors:
            raise RuntimeError("; ".join(errors))

    try:
        window.load_image(args.image)
        wait()
        choices = [i for i in range(window.partition.count())
                   if window.partition.itemData(i)["offset"] == args.offset]
        assert len(choices) == 1
        window.partition.setCurrentIndex(choices[0])
        window.deep_log.setChecked(True)
        window.resize(1020, 720)
        app.processEvents()
        for control in (window.mode, window.partition, window.source_button, window.scan_button):
            assert control.height() >= control.sizeHint().height()
        window.grab().save(str(output / "01-source.png"))
        window.home_scroll.ensureWidgetVisible(window.scan_button)
        app.processEvents()
        window.grab().save(str(output / "02-source-actions.png"))
        window.start_scan()
        wait()
        historical = [item for item in window.model.items if item.get("recovery_method") == "ntfs_log"]
        assert historical and any(i["content_status"] == "fragment" for i in historical)
        previews = []
        for item in historical:
            window.search.setText(item["original_path"] or item["observed_path"])
            app.processEvents()
            assert window.proxy.rowCount() == 1
            window.table.setCurrentIndex(window.proxy.index(0, 1))
            window.request_preview()
            wait()
            assert "旧日志" in window.preview_note.text()
            fragment = item["content_status"] == "fragment"
            if fragment:
                assert "不完整片段" in window.preview_note.text()
                assert "不完整片段" in window.proxy.data(window.proxy.index(0, 5))
            else:
                assert "历史名称" in window.proxy.data(window.proxy.index(0, 5))
            if Path(item["observed_path"]).suffix == ".png":
                assert not window.preview_image.pixmap().isNull()
            else:
                assert window.preview_text.toPlainText()
            window.select_visible()
            previews.append({"candidate_id": item["id"], "original_path": item["original_path"],
                             "fragment": fragment, "previewed": True,
                             "file_offset": item["ntfs_log"]["file_offset"]})
            window.grab().save(str(output / f"preview-{item['id']}.png"))
        window.export_to(output, sorted(window.model.checked))
        wait()
        saved = read_json(window.last_output / "recovery.json")
        assert saved["source_verification"] == "unchanged"
        expected = {item["id"]: item for item in historical}
        assert len(saved["results"]) == len(expected)
        for row in saved["results"]:
            item = expected[row["candidate_id"]]
            fragment = item["content_status"] == "fragment"
            assert row["status"] == ("partial" if fragment else "exported_unverified")
            if fragment:
                assert ".fragment-" in row["saved_path"]
            assert sha256_file(window.last_output / row["saved_path"]) == item["ntfs_log"]["sha256"]
        window.grab().save(str(output / "04-exported.png"))
        window.load_report(window.session, read_json(window.session / "session.json"))
        window.search.clear()
        app.processEvents()
        assert len([i for i in window.model.items if i.get("recovery_method") == "ntfs_log"]) == len(historical)
        window.grab().save(str(output / "05-reopened.png"))
        write_json(output / "desktop-ntfs-log.json", {
            "status": "passed", "platform": app.platformName(), "previews": previews, "reopened": True,
            "core_module": recovery_core.__file__, "desktop_module": recovery_desktop.__file__,
            "session": str(window.session), "recovered": str(window.last_output),
            "exported_unverified_count": saved["exported_unverified_count"], "partial_count": saved["partial_count"],
        })
    finally:
        window.close()
        app.processEvents()


if __name__ == "__main__":
    main()
