"""Exercise JPEG reconstruction, scan resume and acquisition with Qt and real TSK.

Writes only a NEW copy of a supplied synthetic raw image and new output files.
A Qt-generated independent JPEG is scattered inside currently free zero bytes;
neither original bytes nor their manifest are given to the recovery engine.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import random
import shutil
import sys
import time

if "--installed-runtime" not in sys.argv:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tsk-bin", type=Path, required=True)
    parser.add_argument("--installed-runtime", action="store_true")
    args = parser.parse_args()
    os.environ["QT_QPA_PLATFORM"] = "windows"
    from PySide6.QtCore import QByteArray, QBuffer, QIODevice
    from PySide6.QtGui import QImage, QImageWriter
    from PySide6.QtWidgets import QApplication
    import recovery_core
    from recovery_core.carving import geometry, allocation_bitmap, free_runs
    from recovery_core.common import RecoveryError, new_directory, write_json, read_json, sha256_file
    from recovery_core.control import TaskControl, OperationCancelled, task_scope
    from recovery_core.service import scan, resume_scan
    from recovery_core.tsk import Tsk
    from recovery_core.verification import verify
    from recovery_desktop.app import RecoveryWindow, STYLE

    def require(condition, message):
        if not condition:
            raise RecoveryError(message)

    directory = new_directory(args.output)
    backend = Tsk(args.tsk_bin)
    volume = geometry(args.image, args.offset, 512)
    bitmap = allocation_bitmap(args.image, args.offset, 512, volume, backend)
    app = QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    picture = QImage(192, 160, QImage.Format.Format_RGB32)
    rng = random.Random(20260921)
    for y in range(picture.height()):
        for x in range(picture.width()):
            picture.setPixel(x, y, 0xff000000 | rng.randrange(1 << 24))
    encoded = QByteArray()
    buffer = QBuffer(encoded)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QImageWriter(buffer, b"jpeg")
    writer.setQuality(90)
    require(writer.write(picture), "Independent JPEG encoding failed.")
    original = bytes(encoded)
    require(volume.cluster_size < len(original), "Fixture cluster size is too large for this JPEG experiment.")
    (directory / "original.jpg").write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    needed = len(original) + volume.cluster_size
    start = None
    with args.image.open("rb") as source:
        for a, b in free_runs(bitmap, volume):
            position = a + 256 * volume.cluster_size
            if position + needed <= b:
                source.seek(position)
                if source.read(needed) == bytes(needed):
                    start = position
                    break
    require(start is not None, "No sufficiently large zero-filled free fixture range.")
    source_digest = sha256_file(args.image)
    image = directory / "fragmented.img"
    with args.image.open("rb") as source, image.open("xb") as output:
        shutil.copyfileobj(source, output)
    # Both fragments remain unallocated; one free cluster between them is absent.
    with image.open("r+b") as output:
        output.seek(start)
        output.write(original[:volume.cluster_size])
        output.seek(start + 2 * volume.cluster_size)
        output.write(original[volume.cluster_size:])
    control = TaskControl()
    control.progress = lambda event: control.cancelled.set() if event["phase"] == "jpeg" and event["completed"] >= 1024 * 1024 else None
    session = directory / "scan"
    try:
        with task_scope(control):
            scan(image, session, backend=backend, offset=args.offset, deep_jpeg=True, reassemble_jpeg=True)
    except OperationCancelled:
        pass
    else:
        raise RecoveryError("Scan did not interrupt at the requested block boundary.")
    saved = read_json(session / "scan-progress.json")
    require(saved["status"] == "paused" and "result" in saved["stages"]["metadata"], "Scan checkpoint was not durable.")
    def repeated_metadata(*args):
        raise RecoveryError("Resume unexpectedly rescanned completed metadata.")
    backend.scan = repeated_metadata
    report = resume_scan(session, backend=backend)
    matches = [c for c in report["candidates"] if c.get("recovery_method") == "jpeg_carving"]
    require(len(matches) == 1 and matches[0]["carving"]["reconstructed"], "Expected one fragmented JPEG.")
    require(matches[0]["carving"]["sha256"] == digest, "JPEG reconstruction does not match independent original.")
    window = RecoveryWindow(args.tsk_bin)
    errors = []
    window.show_error = errors.append
    window.workspace.setText(str(directory))
    window.show()
    def wait():
        deadline = time.monotonic() + 180
        while window.worker is not None:
            app.processEvents()
            time.sleep(.01)
            if time.monotonic() > deadline:
                raise RecoveryError("Extended desktop acceptance timed out.")
        app.processEvents()
        require(not errors, "; ".join(errors))
    try:
        window.resume_progress(session / "scan-progress.json")
        wait()
        window.search.setText(matches[0]["observed_path"])
        app.processEvents()
        window.table.setCurrentIndex(window.proxy.index(0, 1))
        window.request_preview()
        wait()
        require(not window.preview_image.pixmap().isNull(), "Reconstructed JPEG did not decode in Qt.")
        require("重组" in window.preview_note.text(), "Reconstruction uncertainty is not visible.")
        window.grab().save(str(directory / "01-jpeg-preview.png"))
        window.export_to(directory, [matches[0]["id"]])
        wait()
        reference = directory / "reference.manifest.json"
        write_json(reference, {"schema_version": 1, "files": [dict(original_path="Z:\\Independent\\original.jpg", size=len(original), sha256=digest)]})
        verification = verify(window.last_output, reference)
        write_json(directory / "verification.json", verification)
        require(verification["exact_content_matches"] == 1 and verification["correct_paths"] == 0,
                "JPEG content or unknown-path accounting is incorrect.")
        window.grab().save(str(directory / "02-jpeg-export.png"))
        window.go_home()
        window.load_image(image)
        wait()
        window.start_acquisition()
        wait()
        acquisition = window.last_acquisition
        require(acquisition["status"] == "completed" and acquisition["image_sha256"] == sha256_file(image),
                "Desktop file acquisition differs from its source.")
        window.grab().save(str(directory / "03-acquisition.png"))
        window.load_acquired()
        wait()
        require(window.partition.count() == 1, "Acquired image did not reopen.")
        require(sha256_file(args.image) == source_digest, "The supplied fixture was modified.")
        write_json(directory / "extended-acceptance.json", dict(schema_version=1, status="passed",
            core_module=recovery_core.__file__, version=recovery_core.__version__, qt_platform=app.platformName(),
            scope="synthetic_scattered_jpeg_on_real_filesystem", jpeg_sha256=digest, jpeg_size=len(original),
            extents=matches[0]["carving"]["extents"], exact_content_matches=1, correct_paths=0,
            scan_cancel_resume=True, completed_metadata_reused=True, source_unchanged=True,
            desktop_preview_export=True, desktop_file_acquisition=True, acquired_image_reopened=True,
            physical_media_tested=False))
    finally:
        if window.worker is not None:
            window.cancel_task()
            wait()
        window.close()
        app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
