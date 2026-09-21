import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recovery_core.acquisition import acquire, resume_acquisition, read_chunk, ReadFailure
from recovery_core.common import RecoveryError, read_json
from recovery_core.control import OperationCancelled
from recovery_core.journal import atomic_json, exclusive_job


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "original.img"
        self.data = bytes(range(256)) * 128 + b"tail"
        self.source.write_bytes(self.data)
        self.output = self.root / "acquisition"

    def tearDown(self):
        self.tmp.cleanup()

    def reader(self, path, offset, size, timeout):
        self.assertEqual(Path(path), self.source)
        return self.data[offset:offset + size]

    def test_successful_image_matches_independent_original_and_source_is_unchanged(self):
        result = acquire(self.source, self.output, block_bytes=4096, reader=self.reader)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["good_bytes"], len(self.data))
        self.assertEqual(result["image_sha256"], hashlib.sha256(self.data).hexdigest())
        self.assertEqual((self.output / "image.img").read_bytes(), self.data)
        self.assertEqual(self.source.read_bytes(), self.data)

    def test_bad_sector_is_localized_and_retry_repairs_only_missing_bytes(self):
        calls = []
        def failing(path, offset, size, timeout):
            calls.append((offset, size))
            if offset < 4608 and offset + size > 4096:
                raise ReadFailure("injected I/O failure")
            return self.reader(path, offset, size, timeout)
        result = acquire(self.source, self.output, block_bytes=16384, retries=1, reader=failing)
        self.assertEqual(result["status"], "completed_with_errors")
        self.assertEqual((result["bad_bytes"], result["pending_bytes"]), (512, 0))
        self.assertEqual(calls.count((4096, 512)), 2)
        self.assertEqual((self.output / "image.img").read_bytes(), self.data[:4096] + bytes(512) + self.data[4608:])
        retries = []
        def repaired(*args):
            retries.append(args[1:3])
            return self.reader(*args)
        result = resume_acquisition(self.output, reader=repaired)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(retries, [(4096, 512)])
        self.assertEqual((self.output / "image.img").read_bytes(), self.data)

    def test_cancel_and_resume_preserves_completed_ranges(self):
        def interrupted(path, offset, size, timeout):
            if offset >= 4096:
                raise OperationCancelled()
            return self.reader(path, offset, size, timeout)
        result = acquire(self.source, self.output, block_bytes=4096, reader=interrupted)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["good_bytes"], 4096)
        seen = []
        def resumed(*args):
            seen.append(args[1])
            return self.reader(*args)
        result = resume_acquisition(self.output, reader=resumed)
        self.assertEqual(result["status"], "completed")
        self.assertNotIn(0, seen)

    def test_source_replacement_and_tampered_destination_are_rejected(self):
        acquire(self.source, self.output, reader=self.reader)
        image = self.output / "image.img"
        with image.open("r+b") as stream:
            stream.write(b"wrong")
        with self.assertRaisesRegex(RecoveryError, "校验"):
            resume_acquisition(self.output, reader=self.reader)
        self.source.write_bytes(b"changed")
        with self.assertRaises(RecoveryError):
            resume_acquisition(self.output, reader=self.reader)

    def test_map_overlap_and_foreign_output_are_refused_without_writes(self):
        acquire(self.source, self.output, reader=self.reader)
        original = read_json(self.output / "acquisition.json")
        for case in ("overlap", "path", "identity", "negative"):
            state = copy.deepcopy(original)
            if case == "overlap":
                state["ranges"].append(state["ranges"][0])
            elif case == "path":
                state["image"] = str(self.source)
            elif case == "identity":
                state["image_identity"]["inode"] += 1
            else:
                state["ranges"][0]["size"] = -1
            atomic_json(self.output / "acquisition.json", state)
            with self.subTest(case=case), self.assertRaises(RecoveryError):
                resume_acquisition(self.output, reader=self.reader)
        self.assertEqual(self.source.read_bytes(), self.data)

    def test_permission_failure_stops_instead_of_marking_all_bytes_bad(self):
        with patch("recovery_core.acquisition.read_chunk", side_effect=PermissionError("access denied")):
            result = acquire(self.source, self.output)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["bad_bytes"], 0)
        self.assertEqual(result["pending_bytes"], len(self.data))

    def test_invalid_options_empty_file_and_existing_directory_are_refused(self):
        for options in ({"block_bytes": 513}, {"retries": -1}, {"timeout": float("nan")}, {"retries": True}):
            with self.subTest(options=options), self.assertRaises(RecoveryError):
                acquire(self.source, self.output, **options)
        self.assertFalse(self.output.exists())
        self.source.write_bytes(b"")
        with self.assertRaises(RecoveryError):
            acquire(self.source, self.output)

    def test_destination_exclusive_job_blocks_a_second_writer(self):
        acquire(self.source, self.output, reader=self.reader)
        with exclusive_job(self.output), self.assertRaises(RecoveryError):
            resume_acquisition(self.output, reader=self.reader)

    def test_child_reads_exact_bytes_and_rejects_short_reads(self):
        self.assertEqual(read_chunk(str(self.source), 512, 1024, 10), self.data[512:1536])
        with self.assertRaises(ReadFailure):
            read_chunk(str(self.source), len(self.data), 512, 10)
        with self.assertRaises(RecoveryError):
            read_chunk(str(self.root / "missing.img"), 0, 512, 10)

    def test_timeout_becomes_retryable_read_failure(self):
        with patch("recovery_core.acquisition.run_bounded", side_effect=RecoveryError("timed out")):
            with self.assertRaises(ReadFailure):
                read_chunk(str(self.source), 0, 512, 0.1)

    def test_coarse_pass_reads_healthy_regions_before_revisiting_bad_area(self):
        calls = []
        def failing(path, offset, size, timeout):
            calls.append((offset, size))
            if offset == 0:
                raise ReadFailure("injected")
            return self.reader(path, offset, size, timeout)
        acquire(self.source, self.output, block_bytes=16384, retries=0, reader=failing)
        self.assertLess(calls.index((16384, 16384)), calls.index((0, 4096)))

    def test_source_changes_during_capture_leave_failure_and_pending_bytes(self):
        def changing(path, offset, size, timeout):
            self.source.write_bytes(b"changed")
            raise ReadFailure("disconnect")
        report = acquire(self.source, self.output, reader=changing)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["good_bytes"], 0)
        self.assertEqual(report["pending_bytes"], len(self.data))
