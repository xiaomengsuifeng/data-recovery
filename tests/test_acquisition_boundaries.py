import copy
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from recovery_core import acquisition
from recovery_core.common import RecoveryError, read_json
from recovery_core.control import OperationCancelled, TaskControl, task_scope
from recovery_core.journal import atomic_json


class AcquisitionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "源文件.bin"
        self.data = bytes(range(256)) * 4 + b"tail"
        self.source.write_bytes(self.data)
        self.output = self.root / "采集结果"

    def reader(self, path, offset, size, timeout):
        return self.data[offset:offset + size]

    def test_option_type_range_and_alignment_matrix_create_no_output(self):
        invalid = {
            "block_bytes": [None, True, "512", 0, 511, 513, 64 * 1024 * 1024 + 512],
            "retries": [None, True, 1.5, -1, 11],
            "timeout": [None, True, "1", 0, -1, 301, float("nan"), float("inf")],
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(RecoveryError):
                    acquisition.acquire(self.source, self.output, **{field: value})
                self.assertFalse(self.output.exists())

    def test_minimum_block_one_byte_and_unaligned_final_sector(self):
        for size in (1, 511, 512, 513, len(self.data)):
            data = self.data[:size]
            self.source.write_bytes(data)
            result = acquisition.acquire(self.source, self.root / f"out-{size}", block_bytes=512,
                                         reader=lambda path, offset, amount, timeout: data[offset:offset + amount])
            self.assertEqual(result["good_bytes"], size)
            self.assertEqual(result["image_sha256"], hashlib.sha256(data).hexdigest())

    def test_disk_space_failure_does_not_create_or_read_anything(self):
        with patch.object(acquisition.shutil, "disk_usage", return_value=SimpleNamespace(free=1)), \
                patch.object(acquisition, "read_chunk") as read:
            with self.assertRaises(RecoveryError):
                acquisition.acquire(self.source, self.output)
            read.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_existing_destination_and_missing_parent_preserve_source(self):
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_bytes(b"existing")
        for destination in (self.output, self.root / "missing" / "child"):
            with self.assertRaises((RecoveryError, OSError)):
                acquisition.acquire(self.source, destination, reader=self.reader)
        self.assertEqual(marker.read_bytes(), b"existing")
        self.assertEqual(self.source.read_bytes(), self.data)

    def test_short_source_reads_never_publish_good_bytes(self):
        result = acquisition.acquire(self.source, self.output, block_bytes=512, retries=0,
                                     reader=lambda *args: b"short")
        self.assertEqual(result["status"], "completed_with_errors")
        self.assertEqual(result["bad_bytes"], len(self.data))
        self.assertEqual(result["good_bytes"], 0)
        self.assertEqual((self.output / "image.img").read_bytes(), bytes(len(self.data)))

    def test_resume_without_retry_preserves_bad_map_and_does_not_read_source(self):
        def unreadable(*args):
            raise acquisition.ReadFailure("bad sector")
        acquisition.acquire(self.source, self.output, retries=0, reader=unreadable)
        with patch.object(acquisition, "read_chunk", side_effect=AssertionError("unexpected reread")):
            report = acquisition.resume_acquisition(self.output, retry_bad=False)
        self.assertEqual(report["status"], "completed_with_errors")
        self.assertEqual(report["bad_bytes"], len(self.data))

    def test_completed_resume_is_idempotent_without_source_reads(self):
        original = acquisition.acquire(self.source, self.output, reader=self.reader)
        with patch.object(acquisition, "read_chunk", side_effect=AssertionError("unexpected reread")):
            result = acquisition.resume_acquisition(self.output)
        self.assertEqual(result, original)

    def test_map_shape_and_retry_values_fail_before_destination_is_modified(self):
        def interrupt(*args):
            raise OperationCancelled()
        original = acquisition.acquire(self.source, self.output, reader=interrupt)
        image = self.output / "image.img"
        before = image.read_bytes()
        mutations = [
            ("kind", None), ("acquisition_version", 999), ("source", []), ("options", {}),
            ("image_identity", None), ("ranges", []), ("ranges", None),
        ]
        states = [dict(copy.deepcopy(original), **{key: value}) for key, value in mutations]
        for changes in ({"offset": True}, {"status": "unknown"}, {"attempts": 12},
                        {"attempts": -1}, {"block_bytes": 513}, {"block_bytes": True},
                        {"size": len(self.data) - 1}, {"status": "good", "sha256": "wrong"}):
            state = copy.deepcopy(original)
            state["ranges"][0].update(changes)
            states.append(state)
        for index, state in enumerate(states):
            atomic_json(self.output / "acquisition.json", state)
            with self.subTest(case=index), self.assertRaises(RecoveryError):
                acquisition.resume_acquisition(self.output, reader=self.reader)
            self.assertEqual(image.read_bytes(), before)

    def test_cancel_during_completed_range_verification_keeps_checkpoint(self):
        acquisition.acquire(self.source, self.output, reader=self.reader)
        path = self.output / "acquisition.json"
        before = path.read_bytes()
        control = TaskControl()
        control.cancelled.set()
        with task_scope(control), self.assertRaises(OperationCancelled):
            acquisition.resume_acquisition(self.output, reader=self.reader)
        self.assertEqual(path.read_bytes(), before)

    def test_final_source_change_cannot_report_success(self):
        def changing(path, offset, size, timeout):
            data = self.reader(path, offset, size, timeout)
            self.source.write_bytes(b"replaced")
            return data
        result = acquisition.acquire(self.source, self.output, reader=changing)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("image_sha256", result)

    def test_range_budget_retains_a_resumable_checkpoint(self):
        with patch.object(acquisition, "MAX_RANGES", 3):
            result = acquisition.acquire(self.source, self.output, block_bytes=512, reader=self.reader)
        self.assertEqual(result["status"], "failed")
        self.assertGreater(result["pending_bytes"], 0)
        result = acquisition.resume_acquisition(self.output, reader=self.reader)
        self.assertEqual(result["status"], "completed")
        self.assertEqual((self.output / "image.img").read_bytes(), self.data)

    def test_short_destination_writes_are_retried_before_recording_success(self):
        actual_open = Path.open
        class ShortOutput:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def __getattr__(self, name):
                return getattr(self.stream, name)
            def write(self, data):
                return self.stream.write(data[:3])
        def opened(path, mode="r", *args, **kwargs):
            stream = actual_open(path, mode, *args, **kwargs)
            return ShortOutput(stream) if path.name == "image.img" and mode == "r+b" else stream
        with patch.object(Path, "open", opened):
            result = acquisition.acquire(self.source, self.output, reader=self.reader)
        self.assertEqual(result["status"], "completed")
        self.assertEqual((self.output / "image.img").read_bytes(), self.data)

    def test_destination_failure_keeps_pending_bytes_for_retry(self):
        def no_write(*args):
            raise OSError("target full")
        real_open = Path.open
        def opened(path, mode="r", *args, **kwargs):
            if path.name == "image.img" and mode == "r+b":
                no_write()
            return real_open(path, mode, *args, **kwargs)
        with patch.object(Path, "open", opened):
            report = acquisition.acquire(self.source, self.output, reader=self.reader)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["pending_bytes"], len(self.data))
        self.assertEqual(acquisition.resume_acquisition(self.output, reader=self.reader)["status"], "completed")

    def test_console_sibling_and_child_short_read_are_checked(self):
        def child(command, output, **limits):
            self.assertEqual(Path(command[0]).name, "python.exe")
            output.write(b"a")
        with patch.object(acquisition.sys, "executable", str(self.root / "pythonw.exe")), \
                patch.object(acquisition, "run_bounded", side_effect=child), self.assertRaises(acquisition.ReadFailure):
            acquisition.read_chunk(str(self.source), 0, 512, 1)

    def test_bad_range_zero_fill_handles_short_writes_after_interrupted_capture(self):
        def interrupted(*args):
            raise OperationCancelled()
        acquisition.acquire(self.source, self.output, block_bytes=512, reader=interrupted)
        image = self.output / "image.img"
        # Pending bytes may contain an interrupted, unpublished write. They are
        # deliberately not trusted on resume and must all become zero on failure.
        with image.open("r+b") as stream:
            stream.write(b"stale" * (len(self.data) // 5) + b"x" * (len(self.data) % 5))
        original_open = Path.open
        class ShortOutput:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def __getattr__(self, name):
                return getattr(self.stream, name)
            def write(self, data):
                return self.stream.write(data[:3])
        def opened(path, mode="r", *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            return ShortOutput(stream) if path.name == "image.img" and mode == "r+b" else stream
        def unreadable(*args):
            raise acquisition.ReadFailure("bad source")
        with patch.object(Path, "open", opened):
            report = acquisition.resume_acquisition(self.output, reader=unreadable)
        self.assertEqual(report["status"], "completed_with_errors")
        self.assertEqual(image.read_bytes(), bytes(len(self.data)))
