from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recovery_core import carving, jpeg
from recovery_core.common import RecoveryError, read_json
from recovery_core.control import OperationCancelled, TaskControl, task_scope
from recovery_core.service import scan, resume_scan
from recovery_core.journal import atomic_json, exclusive_job
from test_carving import make_image, png, BitmapBackend
from test_jpeg import minimal_jpeg


class ScanResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.image = make_image(self.root / "test.img", [(1021, png()), (8 * 512, minimal_jpeg())])
        self.output = self.root / "scan"
        self.backend = BitmapBackend()

    def tearDown(self):
        self.tmp.cleanup()

    def interrupt_png(self):
        control = TaskControl()
        def changed(event):
            if event["phase"] == "carving" and event["completed"] >= 1536:
                control.cancelled.set()
        control.progress = changed
        with task_scope(control), patch.object(carving, "SCAN_BLOCK", 512), self.assertRaises(OperationCancelled):
            scan(self.image, self.output, backend=self.backend, offset=3, deep_png=True, deep_jpeg=True)

    def test_resume_uses_saved_block_and_does_not_repeat_metadata(self):
        self.interrupt_png()
        state = read_json(self.output / "scan-progress.json")
        self.assertEqual(state["status"], "paused")
        self.assertGreater(state["stages"]["png"]["partial"]["cursor"], 3 * 512)
        with patch.object(self.backend, "scan", side_effect=AssertionError("metadata already completed")):
            result = resume_scan(self.output, backend=self.backend)
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(read_json(self.output / "scan-progress.json")["status"], "completed")
        with patch.object(self.backend, "scan", side_effect=AssertionError("already completed")):
            self.assertEqual(resume_scan(self.output, backend=self.backend), result)

    def test_modified_source_and_invalid_progress_reject(self):
        self.interrupt_png()
        with self.image.open("r+b") as stream:
            stream.seek(-1, 2)
            stream.write(b"x")
        with self.assertRaises(RecoveryError):
            resume_scan(self.output, backend=self.backend)
        self.assertFalse((self.output / "session.json").exists())

    def test_changed_backend_and_corrupt_cursor_reject(self):
        self.interrupt_png()
        state = read_json(self.output / "scan-progress.json")
        state["stages"]["png"]["partial"]["cursor"] = -1
        atomic_json(self.output / "scan-progress.json", state)
        with self.assertRaises(RecoveryError):
            resume_scan(self.output, backend=self.backend)

    def test_concurrent_resume_is_rejected(self):
        self.interrupt_png()
        with exclusive_job(self.output), self.assertRaises(RecoveryError):
            resume_scan(self.output, backend=self.backend)

    def test_jpeg_stage_resumes_without_duplicate_candidates(self):
        control = TaskControl()
        def changed(event):
            if event["phase"] == "jpeg" and event["completed"] >= 4608:
                control.cancelled.set()
        control.progress = changed
        with task_scope(control), patch.object(jpeg, "BLOCK", 512), self.assertRaises(OperationCancelled):
            scan(self.image, self.output, backend=self.backend, offset=3, deep_jpeg=True)
        state = read_json(self.output / "scan-progress.json")
        self.assertEqual(len(state["stages"]["jpeg"]["partial"]["candidates"]), 1)
        with patch.object(self.backend, "scan", side_effect=AssertionError("metadata repeated")):
            result = resume_scan(self.output, backend=self.backend)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["jpeg"]["signature_checks"], 1)

    def test_atomic_replace_failure_preserves_previous_checkpoint(self):
        path = self.root / "progress.json"
        atomic_json(path, {"schema_version": 1, "committed": 1})
        with patch("recovery_core.journal.os.replace", side_effect=OSError("injected full disk")):
            with self.assertRaises(OSError):
                atomic_json(path, {"committed": 2})
        self.assertEqual(read_json(path)["committed"], 1)
        self.assertEqual(list(self.root.glob(".progress-*.tmp")), [])

    def test_deleted_directory_traversal_reuses_completed_directory(self):
        from recovery_core.tsk import Tsk
        from test_core import body
        backend = object.__new__(Tsk)
        backend.versions = {"fls": "fixture", "icat": "fixture"}
        visited = []
        def listing(image, offset, sector, *, directories=False, inode=None, prefix="/"):
            from recovery_core.control import checkpoint
            checkpoint()  # Real child-process reads check cancellation before launch.
            if directories:
                return "roots" if inode is None else ""
            if inode is None:
                return ""
            visited.append(inode)
            return body(prefix + "note.txt", inode=inode + "-128-1", size=5)
        backend._listing = listing
        backend._directories = lambda value: [("71", "/first"), ("72", "/second")] if value == "roots" else []
        control = TaskControl()
        control.progress = lambda event: control.cancelled.set() if event["phase"] == "directories" and event["completed"] == 1 else None
        with task_scope(control), self.assertRaises(OperationCancelled):
            scan(self.image, self.output, backend=backend, offset=3)
        saved = read_json(self.output / "scan-progress.json")
        self.assertEqual(saved["stages"]["metadata"]["partial"]["visited"], ["71"])
        report = resume_scan(self.output, backend=backend)
        self.assertEqual(report["candidate_count"], 2)
        self.assertEqual(visited.count("71"), 1)
