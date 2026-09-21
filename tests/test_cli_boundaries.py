import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recovery_core.cli import main
from recovery_core.common import RecoveryError
from recovery_core.windows import DiskSource, VolumeSource
from test_core import FakeBackend


class CliBoundaryTests(unittest.TestCase):
    def invoke(self, command):
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
            code = main(command)
        return code, output.getvalue(), error.getvalue()

    def test_doctor_emits_engine_versions(self):
        backend = FakeBackend()
        backend.executables = {"fls": "test-fls", "icat": "test-icat"}
        with patch("recovery_core.cli.Tsk", return_value=backend):
            code, output, error = self.invoke(["doctor"])
        self.assertEqual(code, 0)
        self.assertFalse(error)
        self.assertIn("fls", output)

    def test_invalid_positive_values_and_exclusive_sources(self):
        commands = [["scan", "test.img", "--output", "unused", "--max-candidates", value]
                    for value in ("0", "-1", "NaN", "1.2")]
        commands += [["acquire", "--file", "test.img", "--disk", "1", "--output", "unused"],
                     ["acquire", "--output", "unused"], ["resume-scan"]]
        for command in commands:
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                main(command)
            self.assertEqual(raised.exception.code, 2)

    def test_volume_and_disk_acquisition_dispatch_without_tsk(self):
        for command, source in ((["--volume", "Z:"], VolumeSource("Z:")), (["--disk", "72"], DiskSource(72))):
            with patch("recovery_core.cli.Tsk", side_effect=AssertionError("unneeded")), \
                    patch("recovery_core.acquisition.acquire", return_value={"status": "completed"}) as acquire:
                self.assertEqual(self.invoke(["acquire", *command, "--output", "unused"])[0], 0)
                self.assertEqual(acquire.call_args.args[0], source)

    def test_resume_scan_dispatch_uses_selected_backend(self):
        backend = FakeBackend()
        report = {"status": "scanned", "candidate_count": 0, "candidates": []}
        with patch("recovery_core.cli.Tsk", return_value=backend), patch("recovery_core.cli.resume_scan", return_value=report) as resume:
            code, output, _ = self.invoke(["resume-scan", "existing"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["candidate_count"], 0)
        resume.assert_called_once_with(Path("existing"), backend=backend)

    def test_recover_failure_partial_skip_cancel_and_source_change_exit_codes(self):
        cases = [(dict(status="completed", **{key: 1}), 1) for key in ("failed_count", "partial_count", "skipped_count")]
        cases += [({"status": "cancelled"}, 130), ({"status": "source_changed"}, 2), ({"status": "completed"}, 0)]
        for report, expected in cases:
            with self.subTest(report=report), patch("recovery_core.cli.Tsk", return_value=FakeBackend()), \
                    patch("recovery_core.cli.recover", return_value=report):
                self.assertEqual(self.invoke(["recover", "scan", "--destination", "new"])[0], expected)

    def test_verify_actual_match_and_workflow_completion_both_required(self):
        for complete, matched, expected in ((True, 2, 0), (True, 1, 1), (False, 2, 1)):
            report = dict(recovery_completed=complete, exact_content_matches=matched, total_targets=2)
            with patch("recovery_core.verification.verify", return_value=report), patch("recovery_core.cli.Tsk", side_effect=AssertionError("unneeded")):
                self.assertEqual(self.invoke(["verify", "output", "--manifest", "original.json"])[0], expected)

    def test_existing_verification_report_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "report.json"
            output.write_bytes(b"prior evidence")
            with patch("recovery_core.verification.verify", return_value={}):
                code, _, error = self.invoke(["verify", "output", "--manifest", "original.json", "--output", str(output)])
            self.assertEqual(code, 2)
            self.assertTrue(error)
            self.assertEqual(output.read_bytes(), b"prior evidence")

    def test_expected_errors_and_keyboard_cancel_have_no_traceback(self):
        for exc, expected in ((RecoveryError("unavailable"), 2), (FileNotFoundError("missing"), 2),
                              (ValueError("corrupt"), 2), (KeyboardInterrupt(), 130)):
            with patch("recovery_core.cli.Tsk", side_effect=exc):
                code, output, error = self.invoke(["doctor"])
            self.assertEqual(code, expected)
            self.assertFalse(output)
            self.assertTrue(error)
            self.assertNotIn("Traceback", error)

    def test_null_backend_in_checkpoint_is_a_user_facing_error(self):
        from recovery_core import __version__
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = dict(schema_version=1, kind="scan_checkpoint", checkpoint_version=1,
                         prototype_version=__version__, source={}, stages={}, backend=None)
            (root / "scan-progress.json").write_text(json.dumps(state), encoding="utf-8")
            with patch("recovery_core.cli.Tsk", return_value=FakeBackend()):
                code, _, error = self.invoke(["resume-scan", str(root)])
            self.assertEqual(code, 2)
            self.assertTrue(error)
            self.assertFalse((root / "session.json").exists())
