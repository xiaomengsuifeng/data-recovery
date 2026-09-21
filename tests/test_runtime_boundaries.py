import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from recovery_core import device_reader, windows
from recovery_core.common import RecoveryError
from tools.run_automated_tests import RecordedResult, summarize


class ReaderProtocolTests(unittest.TestCase):
    def run_reader(self, path, offset, size):
        with tempfile.TemporaryFile() as output, contextlib.redirect_stderr(io.StringIO()) as errors, \
                patch.object(sys, "argv", ["reader", str(path), str(offset), str(size)]), \
                patch.object(sys, "stdout", SimpleNamespace(buffer=output, fileno=output.fileno)):
            code = device_reader.main()
            output.seek(0)
            return code, output.read(), errors.getvalue()

    def test_binary_stdout_exact_slice_and_source_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.bin"
            data = bytes(range(256)) * 4097
            path.write_bytes(data)
            code, output, errors = self.run_reader(path, 17, len(data) - 17)
            self.assertEqual((code, output, errors), (0, data[17:], ""))
            self.assertEqual(path.read_bytes(), data)

    def test_bad_offsets_sizes_never_open_the_source(self):
        for offset, size in ((-1, 1), (0, 0), (0, -1), (0, 64 * 1024 * 1024 + 1)):
            with patch("builtins.open", side_effect=AssertionError("must validate first")):
                self.assertEqual(self.run_reader("unused", offset, size)[0], 2)

    def test_source_unavailable_and_eof_are_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.bin"
            code, _, error = self.run_reader(path, 0, 1)
            self.assertEqual(code, 3)
            self.assertIn("SOURCE_UNAVAILABLE", error)
            path.write_bytes(b"a")
            code, output, error = self.run_reader(path, 0, 2)
            self.assertEqual(code, 3)
            self.assertEqual(output, b"a")
            self.assertIn("READ_ERROR", error)

    def test_permission_error_is_fatal_not_bad_sector(self):
        with patch("builtins.open", side_effect=PermissionError("denied")):
            code, output, error = self.run_reader("unused", 0, 1)
        self.assertEqual((code, output), (3, b""))
        self.assertIn("SOURCE_UNAVAILABLE", error)


@unittest.skipUnless(os.name == "nt", "Windows identity and placement policy")
class WindowsIdentityBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.volume = dict(mount="Z:\\", label="fixture", guid="test-volume", size=4096,
                           disk_numbers=[72], disk_ids=["test-disk"], sector_size=512, system=False)
        self.disk = dict(number=72, label="fixture", size=4096, disk_numbers=[72],
                         disk_ids=["test-disk"], sector_size=512, system=False)

    def test_volume_letter_normalization_and_ambiguity(self):
        with patch.object(windows, "list_volumes", return_value=[self.volume]):
            for mount in ("Z:", "z:\\"):
                self.assertEqual(windows.volume_identity(mount)["path"], "\\\\.\\Z:")
            for mount in (None, "C:/", "\\\\server\\share", "72", "Z:\\folder"):
                with self.subTest(mount=mount), self.assertRaises(RecoveryError):
                    windows.volume_identity(mount)
        for rows in ([], [self.volume, self.volume], [dict(self.volume, guid="")],
                     [dict(self.volume, disk_ids=[])], [dict(self.volume, disk_numbers=[])]):
            with patch.object(windows, "list_volumes", return_value=rows), self.assertRaises(RecoveryError):
                windows.volume_identity("Z:")

    def test_disk_query_rejects_incomplete_or_wrongly_typed_identity(self):
        with patch.object(windows, "_query_storage", return_value=[self.disk]):
            self.assertEqual(windows.list_disks(), [self.disk])
        changes = {"number": True, "size": 0, "sector_size": 8192, "disk_numbers": [71],
                   "disk_ids": [" "], "label": None, "system": "false"}
        for key, value in changes.items():
            with self.subTest(key=key), patch.object(windows, "_query_storage", return_value=[dict(self.disk, **{key: value})]), \
                    self.assertRaises(RecoveryError):
                windows.list_disks()
        with patch.object(windows, "_query_storage", return_value={}), self.assertRaises(RecoveryError):
            windows.list_disks()

    def test_invalid_missing_and_duplicate_physical_device(self):
        for number in (-1, True, "72"):
            with self.assertRaises(RecoveryError):
                windows.disk_identity(number)
        for rows in ([], [self.disk, self.disk]):
            with patch.object(windows, "list_disks", return_value=rows), self.assertRaises(RecoveryError):
                windows.disk_identity(72)

    def test_destination_disk_is_verified_through_existing_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "new-parent" / "new-output"
            local_drive = target.drive.upper() + "\\"
            source = dict(self.volume, kind="windows_volume")
            row = dict(self.volume, mount=local_drive, disk_numbers=[73])
            with patch.object(windows, "_query_volumes", return_value=[row]):
                windows.ensure_other_disk(source, target)
            for rows in ([], [dict(row, disk_numbers=[])], [dict(row, disk_numbers=[72])], [row, row]):
                with patch.object(windows, "_query_volumes", return_value=rows), self.assertRaises(RecoveryError):
                    windows.ensure_other_disk(source, target)
            self.assertFalse(target.parent.exists())

    def test_valid_ntfs_boot_is_opened_read_only(self):
        from test_desktop_core import boot
        content = bytes(boot()) + bytes(4096 - 512)
        with patch("builtins.open", return_value=io.BytesIO(content)) as opened:
            self.assertEqual(windows.read_volume_boot({"path": "synthetic"}), content)
        opened.assert_called_once_with("synthetic", "rb", buffering=0)


class TestEvidenceTests(unittest.TestCase):
    def result(self, case):
        return unittest.TextTestRunner(stream=io.StringIO(), resultclass=RecordedResult).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(case))

    def test_subtest_failure_and_skip_are_never_reported_as_full_pass(self):
        class Cases(unittest.TestCase):
            def test_matrix(self):
                with self.subTest(case="passed"):
                    self.assertTrue(True)
                with self.subTest(case="failure"):
                    self.fail("expected injected failure")
            def test_skip(self):
                self.skipTest("privilege absent")
        report = summarize(self.result(Cases))
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failure_count"], 1)
        self.assertEqual(report["subtests_run"], 2)
        self.assertEqual(report["passed_tests"], 0)
        self.assertEqual(report["skipped"][0]["reason"], "privilege absent")

    def test_no_skip_gate_empty_suite_and_class_setup_failure(self):
        class Skipped(unittest.TestCase):
            def test_skip(self):
                self.skipTest("permission required")
        result = self.result(Skipped)
        self.assertEqual(summarize(result)["status"], "incomplete")
        self.assertEqual(summarize(result, require_no_skips=True)["exit_code"], 1)
        class Broken(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise RuntimeError("fixture failed")
            def test_case(self):
                pass
        report = summarize(self.result(Broken))
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error_count"], 1)
        self.assertIn("fixture failed", report["cases"][0]["detail"])
        self.assertEqual(summarize(RecordedResult(io.StringIO(), False, 1))["status"], "failed")

    def test_skipped_subtest_has_a_finished_parent_and_visible_reason(self):
        class Cases(unittest.TestCase):
            def test_matrix(self):
                with self.subTest(case="unavailable"):
                    self.skipTest("hardware absent")
        report = summarize(self.result(Cases), require_no_skips=True)
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["exit_code"], 1)
        self.assertNotEqual(report["cases"][0]["status"], "running")
        self.assertEqual(report["cases"][0]["subtests"][0]["detail"], "hardware absent")


try:
    from PySide6.QtCore import QModelIndex, Qt
    from PySide6.QtWidgets import QApplication
    from recovery_desktop.model import CandidateModel, CandidateFilter, category, format_size
    from recovery_desktop.worker import Worker
    QT = True
except ImportError:
    QT = False


@unittest.skipUnless(QT, "Install desktop dependencies")
class DesktopModelBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_sizes_types_numeric_sort_and_case_insensitive_filter(self):
        model = CandidateModel()
        items = [dict(id=str(n), size=size, kind="file", original_path=f"/目录/{name}",
                      observed_path=f"/{name}") for n, (size, name) in enumerate(
                          [(1024, "Photo.JPG"), (2, "中文.txt"), (10, "clip.MP4"), (0, "unknown.bin")])]
        model.reset(items + [dict(kind="recycle_metadata")])
        proxy = CandidateFilter()
        proxy.setSourceModel(model)
        proxy.sort(3)
        self.assertEqual([model.items[proxy.mapToSource(proxy.index(row, 0)).row()]["size"] for row in range(4)],
                         [0, 2, 10, 1024])
        for query, kind, count in (("photo", "图片", 1), ("  中文  ", "文档", 1),
                                   ("", "影音", 1), ("", "其他", 1), ("photo", "文档", 0)):
            proxy.configure(query, kind)
            self.assertEqual(proxy.rowCount(), count)
        for value, expected in ((0, "0 B"), (1023, "1,023 B"), (1024, "1.0 KiB"), (1024 ** 4, "1.0 TiB")):
            self.assertEqual(format_size(value), expected)

    def test_checkbox_uncheck_reset_invalid_indexes_and_roles(self):
        model = CandidateModel()
        item = dict(kind="file", id="1", original_path=None, observed_path="/image.jpg", size=4)
        model.reset([item])
        index = model.index(0, 0)
        self.assertTrue(model.setData(index, Qt.CheckState.Checked.value, Qt.ItemDataRole.CheckStateRole))
        self.assertEqual(model.checked, {"1"})
        self.assertTrue(model.setData(index, Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole))
        self.assertEqual(model.checked, set())
        self.assertFalse(model.setData(QModelIndex(), 1))
        self.assertIsNone(model.data(QModelIndex()))
        self.assertEqual(model.rowCount(index), 0)
        self.assertIsNone(model.headerData(0, Qt.Orientation.Vertical))
        self.assertIsNone(model.data(index, Qt.ItemDataRole.DecorationRole))
        self.assertIn("需", model.data(index, Qt.ItemDataRole.ToolTipRole))
        self.assertIsNotNone(model.data(model.index(0, 5), Qt.ItemDataRole.ForegroundRole))
        model.reset([])
        self.assertEqual(model.rowCount(), 0)

    def test_worker_success_error_and_cancellation_are_distinct_signals(self):
        from recovery_core.control import checkpoint
        for mode in ("success", "failure", "cancel"):
            def operation():
                if mode == "failure":
                    raise ValueError()
                checkpoint()
                return 42
            worker = Worker(operation)
            signals = []
            worker.result_ready.connect(lambda value: signals.append(("result", value)))
            worker.failed.connect(lambda value: signals.append(("error", value)))
            worker.cancelled.connect(lambda: signals.append(("cancelled", None)))
            if mode == "cancel":
                worker.cancel()
            worker.run()
            self.assertEqual(signals, [{"success": ("result", 42), "failure": ("error", "ValueError"),
                                       "cancel": ("cancelled", None)}[mode]])

    def test_progress_is_throttled_but_phase_changes_are_immediate(self):
        worker = Worker(lambda: None)
        events = []
        worker.changed.connect(events.append)
        with patch("recovery_desktop.worker.time.monotonic", side_effect=[1, 1.01, 1.02, 1.20]):
            for phase in ("scan", "scan", "copy", "copy"):
                worker.report_progress({"phase": phase})
        self.assertEqual([value["phase"] for value in events], ["scan", "copy", "copy"])
