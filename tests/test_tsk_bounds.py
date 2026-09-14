import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from recovery_core.common import RecoveryError
from recovery_core.tsk import STDERR_LIMIT, parse_body, run_bounded


class TskPipeBoundsTests(unittest.TestCase):
    def test_invalid_budget_or_nonfinite_timeout_never_starts_child(self):
        for limit, timeout in ((-1, 1), (True, 1), (1, 0), (1, float("inf")), (1, float("nan"))):
            with self.subTest(limit=limit, timeout=timeout):
                with patch("recovery_core.tsk.subprocess.Popen") as popen:
                    with self.assertRaises(RecoveryError):
                        run_bounded([sys.executable, "-c", "pass"], io.BytesIO(), limit=limit, timeout=timeout)
                    popen.assert_not_called()

    def assert_no_readers(self):
        self.assertEqual([], [t.name for t in threading.enumerate()
                              if t.name.startswith("recovery-tsk-")])

    def tearDown(self):
        self.assert_no_readers()

    def invoke(self, program, output, *, limit, timeout=3):
        children = []
        original_popen = subprocess.Popen

        def record_child(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            children.append(child)
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(kwargs["stderr"], subprocess.PIPE)
            return child

        try:
            with patch("recovery_core.tsk.subprocess.Popen", side_effect=record_child):
                run_bounded([sys.executable, "-c", program], output, limit=limit, timeout=timeout)
        finally:
            for child in children:
                self.assertIsNotNone(child.poll(), "child was not terminated/reaped")
                self.assertTrue(child.stdout.closed)
                self.assertTrue(child.stderr.closed)

    def test_zero_budget_and_exact_budget_succeed(self):
        for size in (0, 1, 64 * 1024, 64 * 1024 + 3):
            with self.subTest(size=size), tempfile.TemporaryFile() as output:
                self.invoke(f"import sys; sys.stdout.buffer.write(b'x'*{size})", output, limit=size)
                output.seek(0)
                self.assertEqual(output.read(), b"x" * size)

    def test_fast_stdout_overflow_never_reaches_destination(self):
        for budget in (0, 1, 17, 64 * 1024 + 1):
            with self.subTest(budget=budget), tempfile.TemporaryFile() as output:
                with self.assertRaisesRegex(RecoveryError, "output exceeded"):
                    self.invoke("import sys; sys.stdout.buffer.write(b'x'*(4*1024*1024))",
                                output, limit=budget)
                self.assertEqual(os.fstat(output.fileno()).st_size, budget)
                output.seek(0)
                self.assertEqual(output.read(), b"x" * budget)

    def test_exact_stderr_budget_is_drained_without_a_temp_file(self):
        # Whitespace diagnostics preserve the existing success behavior. The
        # payload exceeds an OS pipe buffer, so stdout-only draining deadlocks.
        output = io.BytesIO()
        with patch("recovery_core.tsk.tempfile.TemporaryFile", side_effect=AssertionError("no stderr tempfile")):
            self.invoke(f"import sys; sys.stderr.buffer.write(b' '*{STDERR_LIMIT}); "
                        "sys.stderr.buffer.flush(); sys.stdout.buffer.write(b'ok')",
                        output, limit=2)
        self.assertEqual(output.getvalue(), b"ok")

    def test_stderr_overflow_is_rejected_even_on_fast_exit(self):
        output = io.BytesIO()
        with self.assertRaisesRegex(RecoveryError, "diagnostic output exceeded"):
            self.invoke(f"import sys; sys.stderr.buffer.write(b' '*{STDERR_LIMIT + 1})",
                        output, limit=0)
        self.assertEqual(output.getvalue(), b"")

    def test_both_pipes_are_drained_without_deadlock(self):
        program = (
            "import sys,threading\n"
            "def errors():\n"
            " sys.stderr.buffer.write(b' '*400000); sys.stderr.buffer.flush()\n"
            "t=threading.Thread(target=errors); t.start()\n"
            "sys.stdout.buffer.write(b'x'*400000); sys.stdout.buffer.flush(); t.join()\n"
        )
        with tempfile.TemporaryFile() as output:
            self.invoke(program, output, limit=400000)
            self.assertEqual(os.fstat(output.fileno()).st_size, 400000)

    def test_timeout_kills_writer_and_joins_pipe_readers(self):
        start = time.monotonic()
        with tempfile.TemporaryFile() as output:
            with self.assertRaisesRegex(RecoveryError, "timed out"):
                self.invoke("import time; time.sleep(30)", output, limit=128, timeout=0.1)
        self.assertLess(time.monotonic() - start, 3)

    def test_keyboard_interrupt_reaps_child_and_readers(self):
        original_wait = threading.Event.wait
        raised = False

        def interrupt_main_wait(event, timeout=None):
            nonlocal raised
            # Thread.start also calls Event.wait without a timeout; leave that
            # alone and interrupt only the main run_bounded monitoring wait.
            if (threading.current_thread() is threading.main_thread()
                    and timeout is not None and not raised):
                raised = True
                raise KeyboardInterrupt
            return original_wait(event, timeout)

        with tempfile.TemporaryFile() as output:
            with patch("recovery_core.tsk.threading.Event.wait", new=interrupt_main_wait):
                with self.assertRaises(KeyboardInterrupt):
                    self.invoke("import time; time.sleep(30)", output, limit=128)
        self.assertTrue(raised)

    def test_destination_failure_terminates_a_busy_child(self):
        class BrokenOutput(io.BytesIO):
            def write(self, data):
                raise OSError("simulated disk full")

        with self.assertRaisesRegex(OSError, "disk full"):
            self.invoke("import sys\nwhile True: sys.stdout.buffer.write(b'x'*65536)",
                        BrokenOutput(), limit=1000000)

    def test_short_destination_writes_do_not_drop_bytes(self):
        class ShortWriter(io.BytesIO):
            def write(self, data):
                return super().write(data[:3])

        output = ShortWriter()
        self.invoke("import sys; sys.stdout.buffer.write(b'abcdefghij')", output, limit=10)
        self.assertEqual(output.getvalue(), b"abcdefghij")

    def test_failure_to_start_second_reader_still_reaps_child(self):
        original_start = threading.Thread.start
        calls = 0

        def failing_start(thread):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated thread startup failure")
            return original_start(thread)

        with tempfile.TemporaryFile() as output:
            with patch("recovery_core.tsk.threading.Thread.start", new=failing_start):
                with self.assertRaisesRegex(RuntimeError, "startup failure"):
                    self.invoke("import time; time.sleep(30)", output, limit=1)

    def test_nonzero_exit_and_diagnostics_are_preserved(self):
        for program, expected in (("raise SystemExit(7)", "exited with 7"),
                                  ("import sys; sys.stderr.write('partial NTFS listing')", "partial NTFS listing")):
            with self.subTest(program=program), tempfile.TemporaryFile() as output:
                with self.assertRaisesRegex(RecoveryError, expected):
                    self.invoke(program, output, limit=0)


class RealFlsModeTests(unittest.TestCase):
    def test_reallocated_marker_inside_filename_is_not_a_status(self):
        row = "0|/notes (deleted-realloc).txt (deleted)|65-128-2|-/rrwxrwxrwx|48|0|5|1|2|3|4\n"
        candidates, warnings = parse_body(row)
        self.assertEqual(warnings, [])
        self.assertEqual(candidates[0]["observed_path"], "/notes (deleted-realloc).txt")

    def test_nist_bunda_unknown_name_type_is_a_regular_data_candidate(self):
        # Observed from the NIST NTFS fixture with the real fls binary. NTFS
        # directory-name type is unknown, but metadata type is regular ('r').
        row = "0|/Bunda.txt (deleted)|65-128-2|-/rrwxrwxrwx|48|0|4296|1|2|3|4\n"
        candidates, warnings = parse_body(row)
        self.assertEqual(warnings, [])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["inode"], "65-128-2")
        self.assertEqual(candidates[0]["observed_path"], "/Bunda.txt")
        self.assertEqual(candidates[0]["size"], 4296)

    def test_directory_special_or_malformed_types_remain_unsupported(self):
        for mode in ("r/drwxrwxrwx", "-/drwxrwxrwx", "d/rrwxrwxrwx", "l/rrwxrwxrwx", "r/", "-/r"):
            row = f"0|/target (deleted)|65-128-2|{mode}|0|0|5|1|2|3|4\n"
            with self.subTest(mode=mode):
                candidates, warnings = parse_body(row)
                self.assertEqual(candidates, [])
                self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()
