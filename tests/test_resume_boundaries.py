import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recovery_core import carving, jpeg
from recovery_core.common import RecoveryError, read_json
from recovery_core.control import OperationCancelled
from recovery_core.journal import atomic_json, run_stage, partial, save_partial
from recovery_core.service import scan, resume_scan
from test_carving import BitmapBackend, make_image, png


class ResumeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.image = make_image(self.root / "source.img", [(1021, png())])
        self.backend = BitmapBackend()
        self.output = self.root / "scan"
        with patch.object(carving, "scan_png", side_effect=OperationCancelled()), self.assertRaises(OperationCancelled):
            scan(self.image, self.output, backend=self.backend, offset=3, deep_png=True)
        self.path = self.output / "scan-progress.json"
        self.original = read_json(self.path)

    def test_version_backend_options_and_foreign_stages_are_rejected(self):
        states = [dict(copy.deepcopy(self.original), **changes) for changes in (
            {"checkpoint_version": 2}, {"kind": "unknown"}, {"prototype_version": "future"},
            {"backend": {"versions": {}}}, {"stages": {"foreign": {}}}, {"stages": []},
            {"options": {}}, {"options": dict(self.original["options"], reassemble_jpeg=True)},
            {"source": dict(self.original["source"], kind="windows_volume")},
            {"filesystem": "exfat"}, {"offset": -1})]
        for index, state in enumerate(states):
            atomic_json(self.path, state)
            with self.subTest(case=index), self.assertRaises(RecoveryError):
                resume_scan(self.output, backend=self.backend)
            self.assertFalse((self.output / "session.json").exists())

    def test_invalid_stage_shapes_do_not_reach_the_backend(self):
        for value in (None, [], {}, {"partial": {}, "result": []}, {"unknown": 1}):
            state = copy.deepcopy(self.original)
            state["stages"]["png"] = value
            atomic_json(self.path, state)
            with patch.object(self.backend, "scan", side_effect=AssertionError("must validate first")), \
                    self.subTest(value=value), self.assertRaises(RecoveryError):
                resume_scan(self.output, backend=self.backend)

    def test_corrupt_json_empty_and_wrong_schema_keep_source_and_prior_results(self):
        before = self.image.read_bytes()
        for content in ("{", "[]", "null", '{"schema_version":2}'):
            self.path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises((RecoveryError, ValueError)):
                resume_scan(self.output, backend=self.backend)
            self.assertFalse((self.output / "session.json").exists())
            self.assertEqual(self.image.read_bytes(), before)

    def test_scan_limits_and_flag_types_are_checked_before_output_creation(self):
        for options in ({"max_candidates": 0}, {"max_candidates": True}, {"max_candidates": 100001},
                        {"deep_png": 1}, {"deep_jpeg": "yes"}, {"reassemble_jpeg": True}):
            output = self.root / "new"
            with self.subTest(options=options), self.assertRaises(RecoveryError):
                scan(self.image, output, backend=self.backend, offset=3, **options)
            self.assertFalse(output.exists())

    def test_foreign_completed_session_is_not_replaced(self):
        atomic_json(self.output / "session.json", {"schema_version": 1, "candidates": [],
                                                  "source": {}, "status": "scanned"})
        before = (self.output / "session.json").read_bytes()
        with self.assertRaises(RecoveryError):
            resume_scan(self.output, backend=self.backend)
        self.assertEqual((self.output / "session.json").read_bytes(), before)

    def test_failed_scan_keeps_source_hash_and_can_be_retried(self):
        with patch.object(carving, "scan_png", side_effect=RecoveryError("temporary read error")), self.assertRaises(RecoveryError):
            resume_scan(self.output, backend=self.backend)
        failed = read_json(self.path)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["source"], self.original["source"])
        self.assertEqual(resume_scan(self.output, backend=self.backend)["candidate_count"], 1)

    def test_png_checkpoint_counter_and_budget_limits(self):
        saved = dict(candidates=[], cursor=3 * 512, accepted_end=3 * 512, scanned_bytes=0, attempts=0, budget=1024)
        for field, value in (("cursor", -1), ("accepted_end", 1 << 40), ("scanned_bytes", -1),
                             ("attempts", 100001), ("budget", -1), ("budget", 1 << 40), ("cursor", True)):
            with self.subTest(field=field, value=value), patch.object(carving, "partial", return_value=dict(saved, **{field: value})), \
                    self.assertRaises(RecoveryError):
                carving.scan_png(self.image, 3, 512, backend=self.backend, max_candidates=4)

    def test_jpeg_checkpoint_counter_and_budget_limits(self):
        saved = dict(candidates=[], cursor=3 * 512, checks=0, ambiguous=0, limited=0, budget=1024)
        for field, value in (("cursor", -1), ("cursor", True), ("checks", 100001), ("ambiguous", -1),
                             ("limited", None), ("budget", -1), ("budget", 1 << 40), ("candidates", None)):
            with self.subTest(field=field, value=value), patch.object(jpeg, "partial", return_value=dict(saved, **{field: value})), \
                    self.assertRaises(RecoveryError):
                jpeg.scan_jpeg(self.image, 3, 512, backend=self.backend, max_candidates=4)


class JournalBoundaryTests(unittest.TestCase):
    def test_failed_fsync_preserves_last_durable_progress_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "progress.json"
            atomic_json(path, {"schema_version": 1, "value": "old"})
            with patch("recovery_core.journal.os.fsync", side_effect=OSError("disk full")), self.assertRaises(OSError):
                atomic_json(path, {"schema_version": 1, "value": "new"})
            self.assertEqual(read_json(path)["value"], "old")
            self.assertEqual(list(path.parent.glob(".progress-*.tmp")), [])

    def test_stage_result_is_copied_and_failed_context_is_cleared(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "progress.json"
            state = {"schema_version": 1, "stages": {}}
            def interrupted():
                save_partial({"cursor": 12})
                observed = partial()
                observed["cursor"] = 99
                self.assertEqual(partial()["cursor"], 12)
                raise OperationCancelled()
            with self.assertRaises(OperationCancelled):
                run_stage(state, "metadata", path, interrupted)
            self.assertIsNone(partial())
            self.assertEqual(read_json(path)["stages"]["metadata"]["partial"]["cursor"], 12)
            result = run_stage(state, "metadata", path, lambda: ["complete"])
            result.append("mutated caller copy")
            self.assertEqual(run_stage(state, "metadata", path, lambda: self.fail("stage repeated")), ["complete"])

    def test_oversized_progress_is_rejected_before_creating_a_temp_file(self):
        class Oversized:
            def __add__(self, value):
                return self
            def encode(self, encoding):
                return self
            def __len__(self):
                return 64 * 1024 * 1024 + 1
        with patch("recovery_core.journal.json.dumps", return_value=Oversized()), \
                patch("recovery_core.journal.tempfile.mkstemp") as temporary, self.assertRaises(RecoveryError):
            atomic_json(Path("unused.json"), {})
        temporary.assert_not_called()
