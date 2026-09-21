"""Validate a read-only, copied synthetic VHD through the actual Windows UI.

The PowerShell wrapper owns mounting and checks the file-backed disk. Only the
selected volume reaches recovery; originals and the raw image stay on the
verification side. This is not a physical-media or writable-volume test.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recovery_core.common import RecoveryError, image_identity, new_directory, read_json, write_json
from recovery_core.fixture_validation import load_fixture
from recovery_core.preview import preview
from recovery_core.service import recover, scan
from recovery_core.tsk import Tsk
from recovery_core.verification import verify
from recovery_core.windows import VolumeSource, check_volume, is_admin

FIXTURE_BYTES = 128 * 1024 * 1024


def copy_stage_vhd(image: Path, container: Path, target: Path, expected_hash: str, size=FIXTURE_BYTES):
    """Copy frozen raw bytes plus a validated fixed-VHD footer; never overwrite."""
    if image.stat().st_size != size or container.stat().st_size != size + 512:
        raise RecoveryError("Synthetic raw image or fixed VHD has an unexpected size.")
    with container.open("rb") as source:
        source.seek(size)
        footer = source.read(512)
    if (footer[:8] != b"conectix" or struct.unpack_from(">I", footer, 12)[0] != 0x10000
            or struct.unpack_from(">Q", footer, 16)[0] != (1 << 64) - 1
            or struct.unpack_from(">Q", footer, 48)[0] != size
            or struct.unpack_from(">I", footer, 60)[0] != 2
            or struct.unpack_from(">I", footer, 64)[0] != (~sum(footer[:64] + footer[68:]) & 0xffffffff)):
        raise RecoveryError("Invalid fixed-VHD footer; no mountable copy was created.")
    digest = hashlib.sha256()
    with image.open("rb") as source, target.open("xb") as output:
        remaining = size
        while remaining:
            data = source.read(min(remaining, 1024 * 1024))
            if not data:
                raise RecoveryError("Frozen image was truncated during copying.")
            output.write(data)
            digest.update(data)
            remaining -= len(data)
        if digest.hexdigest() != expected_hash:
            raise RecoveryError("Frozen image changed; incomplete copy has no VHD footer.")
        output.write(footer)
        output.flush()
        os.fsync(output.fileno())


def prepare(fixture: Path, output: Path, stage: str):
    bundle = load_fixture(fixture)
    root = Path(bundle["root"])
    if output.absolute().parent.resolve(strict=True).joinpath(output.name).is_relative_to(root):
        raise RecoveryError("Live validation output must be outside the original fixture.")
    selected = next(row for row in bundle["stages"] if row["stage"] == stage)
    if selected["bytes"] != FIXTURE_BYTES or selected["sector_size"] != 512:
        raise RecoveryError("Only the generator's 128 MiB synthetic fixture is supported.")
    original_vhd = image_identity(root / "fixture.vhd")
    directory = new_directory(output)
    clone = directory / "readonly-copy.vhd"
    copy_stage_vhd(Path(selected["source"]["path"]), Path(original_vhd["path"]), clone, selected["sha256"])
    plan = {"schema_version": 1, "fixture_id": bundle["fixture_id"], "stage": stage,
            "source_image": selected["source"], "original_vhd": original_vhd,
            "copy": image_identity(clone), "offset_sectors": selected["offset_sectors"],
            "marker": ".recovery-fixture-" + bundle["fixture_id"], "filesystem": bundle["filesystem"],
            "label": ("RC_" + bundle["fixture_id"][:8] if bundle["filesystem"] == "exFAT"
                      else "RECOV_" + bundle["fixture_id"][:12])}
    write_json(directory / "reference.manifest.json", selected["reference"])
    write_json(directory / "plan.json", plan)


def compare_results(image_report: dict, volume_report: dict):
    """Same frozen bytes must yield the same candidates, bytes and path claims."""
    def rows(report):
        return sorted((row["candidate_id"], row["status"], row["actual_size"], row["sha256"],
                       row["original_path"], row["observed_path"], row["saved_path"]) for row in report["results"])
    if (image_report["status"] != "completed" or image_report["source_verification"] != "unchanged"
            or volume_report["status"] != "completed"
            or volume_report["source_verification"] != "live_volume_identity_only"
            or rows(image_report) != rows(volume_report)):
        raise RecoveryError("Direct-volume exports differ from the same frozen raw image, or source checks failed.")


def expect_rejected(operation, forbidden_output: Path, message: str | None = None):
    if forbidden_output.exists():
        raise RecoveryError("Negative-check output already exists.")
    try:
        operation()
    except RecoveryError as error:
        if forbidden_output.exists() or (message and message not in str(error)):
            raise RecoveryError("Negative check failed for an unexpected reason or created output.") from error
        return str(error)
    raise RecoveryError("An operation expected to be rejected was allowed.")


def run_mounted(directory: Path, tsk_bin: Path):
    if not is_admin():
        raise RecoveryError("Run the read-only VHD wrapper in an administrator session.")
    plan, guard = read_json(directory / "plan.json"), read_json(directory / "mount.json")
    if guard.get("read_only") is not True or guard.get("copy") != plan["copy"]["path"]:
        raise RecoveryError("Missing read-only copied-VHD guard.")
    identity = check_volume(guard["source"])
    if identity["label"] != plan["label"] or identity["system"]:
        raise RecoveryError("Mounted volume is not the expected synthetic fixture.")
    from PySide6.QtWidgets import QApplication
    import recovery_core
    import recovery_desktop
    from recovery_desktop.app import RecoveryWindow, STYLE

    backend = Tsk(tsk_bin)
    raw = Path(plan["source_image"]["path"])
    baseline = directory / "image-session"
    scan(raw, baseline, backend=backend, offset=plan["offset_sectors"])
    image_result = recover(baseline, directory / "image-recovered", backend=backend)
    reference = directory / "reference.manifest.json"
    image_verification = verify(directory / "image-recovered", reference)
    write_json(directory / "image-verification.json", image_verification)

    app = QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = RecoveryWindow(tsk_bin)
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
                window.cancel_task()
                raise RecoveryError("Direct-volume desktop check timed out.")
        app.processEvents()
        if errors:
            raise RecoveryError("; ".join(errors))

    try:
        window.deep_png.setChecked(True)
        window.deep_log.setChecked(True)
        window.mode.setCurrentIndex(1)
        wait()
        choices = [i for i in range(window.partition.count())
                   if (row := window.partition.itemData(i)) and row["guid"] == identity["guid"]
                   and row["disk_numbers"] == identity["disk_numbers"] and row["mount"] == identity["mount"]]
        if len(choices) != 1:
            raise RecoveryError("Cannot uniquely select the copied fixture in the actual volume list.")
        window.partition.setCurrentIndex(choices[0])
        for checkbox in (window.deep_png, window.deep_log, window.deep_jpeg, window.reassemble_jpeg):
            if checkbox.isEnabled() or checkbox.isChecked():
                raise RecoveryError("Image-only scans remained enabled for a live volume.")
        window.grab().save(str(directory / "01-volume-source.png"))
        forbidden = Path(identity["mount"]) / ("recovery-rejected-" + plan["fixture_id"])
        rejected_scan = expect_rejected(
            lambda: scan(VolumeSource(identity["mount"]), forbidden, backend=backend), forbidden, "同一块")
        window.start_scan()
        wait()
        if window.report["candidates"] != read_json(baseline / "session.json")["candidates"]:
            raise RecoveryError("Device and image candidate lists differ.")
        if not window.model.items:
            raise RecoveryError("No deleted file candidates available for the desktop check.")
        previews = []
        for item in window.model.items:
            if len(previews) >= 2:
                break
            # Choose by candidate type, never by supplied original bytes/names.
            suffix = Path(item["original_path"] or item["observed_path"]).suffix.lower()
            kind = "image" if suffix == ".png" else "text"
            if kind in previews:
                continue
            window.search.setText(item["original_path"] or item["observed_path"])
            app.processEvents()
            window.table.setCurrentIndex(window.proxy.index(0, 1))
            window.request_preview()
            wait()
            if kind == "image":
                if window.preview_image.pixmap().isNull():
                    raise RecoveryError("Volume image preview could not decode.")
            elif not window.preview_text.toPlainText():
                raise RecoveryError("Volume text preview was empty.")
            previews.append(kind)
            window.grab().save(str(directory / ("02-preview-" + kind + ".png")))
        rejected_export = expect_rejected(
            lambda: recover(window.session, forbidden, backend=backend), forbidden, "同一块")
        window.search.clear()
        app.processEvents()
        window.select_visible()
        window.export_to(directory, sorted(window.model.checked))
        wait()
        saved = read_json(window.last_output / "recovery.json")
        compare_results(image_result, saved)
        volume_verification = verify(window.last_output, reference)
        write_json(directory / "volume-verification.json", volume_verification)
        if image_verification["targets"] != volume_verification["targets"]:
            raise RecoveryError("Device and image independent-original verification differ.")
        window.grab().save(str(directory / "03-exported.png"))
        session = window.session
        window.go_home()
        window.load_report(session, read_json(session / "session.json"))
        app.processEvents()
        window.table.setCurrentIndex(window.proxy.index(0, 1))
        window.request_preview()
        wait()
        window.grab().save(str(directory / "04-reopened.png"))
        # The wrapper has proved this is a new, read-only, file-backed fixture.
        # Exercise the physical-device desktop path and cancellable volume reads.
        window.mode.setCurrentIndex(2)
        wait()
        disk_choices = [i for i in range(window.partition.count())
                        if (row := window.partition.itemData(i)) and row["disk_numbers"] == identity["disk_numbers"]
                        and row["disk_ids"] == identity["disk_ids"]]
        if len(disk_choices) != 1 or window.scan_button.isEnabled():
            raise RecoveryError("Cannot uniquely select the synthetic disk for acquisition.")
        window.partition.setCurrentIndex(disk_choices[0])
        window.start_acquisition()
        wait()
        disk_capture = window.last_acquisition
        if disk_capture["status"] != "completed" or disk_capture["image_sha256"] != plan["source_image"]["sha256"]:
            raise RecoveryError("Physical-device acquisition differs from independent frozen image.")
        window.grab().save(str(directory / "05-disk-acquisition.png"))
        window.load_acquired()
        wait()
        if window.partition.count() != 1:
            raise RecoveryError("Acquired disk image partition could not be opened.")
        from recovery_core.acquisition import acquire, resume_acquisition
        from recovery_core.control import TaskControl, task_scope
        control = TaskControl()
        control.progress = lambda event: control.cancelled.set() if event["phase"] == "acquisition" and event["completed"] >= 16 * 1024 * 1024 else None
        volume_dir = directory / "volume-acquisition"
        with task_scope(control):
            paused = acquire(VolumeSource(identity["mount"]), volume_dir)
        if paused["status"] != "cancelled" or paused["good_bytes"] != 16 * 1024 * 1024:
            raise RecoveryError("Device acquisition did not preserve the completed range on cancellation.")
        resumed = resume_acquisition(volume_dir)
        digest = hashlib.sha256()
        with raw.open("rb") as stream:
            stream.seek(plan["offset_sectors"] * 512)
            remaining = identity["size"]
            while remaining:
                data = stream.read(min(1024 * 1024, remaining))
                if not data:
                    raise RecoveryError("Reference partition is truncated.")
                digest.update(data)
                remaining -= len(data)
        if resumed["status"] != "completed" or resumed["image_sha256"] != digest.hexdigest():
            raise RecoveryError("Resumed volume acquisition differs from independent raw partition bytes.")
        write_json(directory / "device-acquisition.json", {
            "status": "passed", "scope": "read_only_synthetic_vhd", "physical_media_tested": False,
            "disk_sha256": disk_capture["image_sha256"], "volume_sha256": resumed["image_sha256"],
            "volume_bytes": identity["size"], "cancelled_good_bytes": paused["good_bytes"],
            "disk_desktop_flow": True, "volume_resume": True, "acquired_image_opened": True})
        write_json(directory / "live-volume.json", {
            "schema_version": 1, "status": "passed", "scope": "read_only_synthetic_vhd", "stage": plan["stage"],
            "qt_platform": app.platformName(), "session": str(session), "recovered": str(window.last_output),
            "core_module": recovery_core.__file__, "desktop_module": recovery_desktop.__file__,
            "same_disk_scan_rejected": rejected_scan, "same_disk_export_rejected": rejected_export,
            "image_only_options_disabled": True, "image_volume_candidates_equal": True,
            "image_volume_exports_equal": True, "source_verification": saved["source_verification"],
            "total_targets": volume_verification["total_targets"],
            "exact_content_matches": volume_verification["exact_content_matches"],
            "correct_paths": volume_verification["correct_paths"], "previews": previews, "session_reopened": True})
    finally:
        if window.worker is not None:
            window.cancel_task()
            deadline = time.monotonic() + 35
            while window.worker is not None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(.01)
        window.close()
        app.processEvents()


def check_detached(directory: Path, tsk_bin: Path):
    record = read_json(directory / "live-volume.json")
    backend = Tsk(tsk_bin)
    session = Path(record["session"])
    item = next(c for c in read_json(session / "session.json")["candidates"] if c["kind"] == "file")
    output = directory / "disconnected-export"
    rejected_preview = expect_rejected(lambda: preview(session, item["id"], backend), output)
    rejected_export = expect_rejected(lambda: recover(session, output, backend=backend), output)
    write_json(directory / "disconnected.json", {"schema_version": 1, "status": "passed", "preview_rejected": rejected_preview,
                                                "export_rejected": rejected_export, "output_created": False})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--fixture", type=Path, required=True)
    prepare_parser.add_argument("--output", type=Path, required=True)
    prepare_parser.add_argument("--stage", choices=("after-direct-delete", "after-empty-recycle-bin"), required=True)
    for command in ("mounted", "detached"):
        child = commands.add_parser(command)
        child.add_argument("--output", type=Path, required=True)
        child.add_argument("--tsk-bin", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.fixture, args.output, args.stage)
    elif args.command == "mounted":
        run_mounted(args.output, args.tsk_bin)
    else:
        check_detached(args.output, args.tsk_bin)


if __name__ == "__main__":
    main()
