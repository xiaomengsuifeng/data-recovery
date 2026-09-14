import os
from pathlib import Path
import sys
import tempfile
import unittest

from tools.validate_windows import run_step, test_result_summary


class ValidationStepTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.environment = dict(os.environ, PYTHONUTF8="1")

    def invoke(self, program, **kwargs):
        return run_step("check", [sys.executable, "-c", program], self.output,
                        self.environment, **kwargs)

    def test_success_records_stdout_stderr_and_unicode(self):
        result = self.invoke("import sys; print('中文路径'); print('diagnostic', file=sys.stderr)")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["returncode"], 0)
        log = (self.output / result["log"]).read_text(encoding="utf-8")
        self.assertIn("中文路径", log)
        self.assertIn("diagnostic", log)

    def test_nonzero_exit_is_not_reported_as_a_pass(self):
        result = self.invoke("raise SystemExit(7)")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["returncode"], 7)

    def test_timeout_stops_the_check_and_preserves_its_log(self):
        result = self.invoke("import time; print('started', flush=True); time.sleep(30)", timeout=.5)
        self.assertEqual(result["status"], "timed_out")
        self.assertIsNone(result["returncode"])
        self.assertLess(result["elapsed_seconds"], 5)
        log = (self.output / result["log"]).read_text(encoding="utf-8")
        self.assertIn("Exceeded", log)

    def test_missing_executable_is_a_failed_check(self):
        result = run_step("missing", [str(self.output / "not-installed.exe")],
                          self.output, self.environment)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["error"])

    @unittest.skipUnless(os.name == "nt", "Windows process-tree cleanup")
    def test_timeout_releases_files_held_by_descendants(self):
        held = self.output / "held.txt"
        program = "import sys,time; f=open(sys.argv[1], 'w'); f.write('child'); f.flush(); time.sleep(30)"
        parent = ("import subprocess,sys,time; "
                  f"subprocess.Popen([sys.executable, '-c', {program!r}, {str(held)!r}]); time.sleep(30)")
        result = self.invoke(parent, timeout=1)
        self.assertEqual(result["status"], "timed_out")
        self.assertNotIn("cleanup_error", result)
        self.assertEqual(held.read_text(), "child")
        # Windows refuses this unlink if the descendant still has it open.
        held.unlink()

    def test_existing_log_cannot_be_overwritten(self):
        log = self.output / "check.log"
        log.write_bytes(b"previous evidence")
        with self.assertRaises(FileExistsError):
            self.invoke("pass")
        self.assertEqual(log.read_bytes(), b"previous evidence")


class ValidationSummaryTests(unittest.TestCase):
    def test_empty_suite_does_not_pass(self):
        self.assertFalse(test_result_summary(unittest.TestResult())["passed"])

    def test_skips_failures_and_errors_remain_distinct(self):
        class Cases(unittest.TestCase):
            def test_ok(self):
                pass

            def test_skip(self):
                self.skipTest("privilege unavailable")

            def test_failure(self):
                self.fail("bad output")

            def test_error(self):
                raise ValueError("bad input")

        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(Cases).run(result)
        summary = test_result_summary(result)
        self.assertFalse(summary["passed"])
        self.assertEqual(summary["tests_run"], 4)
        self.assertEqual(len(summary["failures"]), 1)
        self.assertEqual(len(summary["errors"]), 1)
        self.assertEqual(summary["skipped"][0]["reason"], "privilege unavailable")

    def test_success_with_skip_keeps_the_skip_visible(self):
        result = unittest.TestResult()
        test = unittest.FunctionTestCase(lambda: None)
        test.run(result)
        result.startTest(test)
        result.addSkip(test, "symlink privilege unavailable")
        result.stopTest(test)
        summary = test_result_summary(result)
        self.assertTrue(summary["passed"])
        self.assertEqual(summary["tests_run"], 2)
        self.assertEqual(len(summary["skipped"]), 1)


if __name__ == "__main__":
    unittest.main()
