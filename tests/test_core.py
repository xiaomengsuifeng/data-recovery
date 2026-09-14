import hashlib
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

from recovery_core.common import RecoveryError, read_json
from recovery_core.service import recover, safe_relative_path, scan
from recovery_core.tsk import parse_body, run_bounded


def body(name="/Documents/report.txt", inode="65-128-1", size=5, suffix=" (deleted)"):
    return f"0|{name}{suffix}|{inode}|r/rrwxrwxrwx|0|0|{size}|1|2|3|4\n"


class BodyParserTests(unittest.TestCase):
    def test_deleted_data_and_pipe_in_filename(self):
        items, _ = parse_body(body("/a|b.txt"))
        self.assertEqual(items[0]["observed_path"], "/a|b.txt")
        self.assertEqual(items[0]["size"], 5)

    def test_reallocated_live_and_named_streams_are_not_export_candidates(self):
        text = body(suffix=" (deleted-realloc)") + body(suffix="") + body("/x:stream")
        items, warnings = parse_body(text)
        self.assertEqual(items, [])
        self.assertEqual(len(warnings), 3)

    def test_ntfs_filename_timestamp_record_is_not_a_second_file(self):
        items, warnings = parse_body(body() + body(inode="65-48-2"))
        self.assertEqual(len(items), 1)
        self.assertEqual(len(warnings), 1)

    def test_malformed_listing_fails_instead_of_returning_partial_success(self):
        with self.assertRaises(RecoveryError):
            parse_body(body() + "damaged output")
        with self.assertRaises(RecoveryError):
            parse_body(body(size=-1))


class ProcessTests(unittest.TestCase):
    def test_process_timeout_is_bounded(self):
        with tempfile.TemporaryFile() as output, self.assertRaises(RecoveryError):
            run_bounded([sys.executable, "-c", "import time; time.sleep(10)"], output,
                        limit=1024, timeout=0.05)

    def test_output_limit_and_nonzero_exit_fail(self):
        for program in ("import sys; sys.stdout.write('x'*8192)", "raise SystemExit(4)"):
            with self.subTest(program=program), tempfile.TemporaryFile() as output:
                with self.assertRaises(RecoveryError):
                    run_bounded([sys.executable, "-c", program], output, limit=32, timeout=3)

    def test_success_with_diagnostics_is_not_silent_success(self):
        with tempfile.TemporaryFile() as output, self.assertRaises(RecoveryError):
            run_bounded([sys.executable, "-c", "import sys; sys.stderr.write('partial scan')"],
                        output, limit=32, timeout=3)


class FakeBackend:
    versions = {"fls": "fixture-backend", "icat": "fixture-backend"}

    def __init__(self, listing=None, payloads=None, error=False):
        self.listing = listing or body()
        self.payloads = payloads or {"65-128-1": b"hello"}
        self.error = error
        self.mutate_source = False

    def scan(self, *args):
        return parse_body(self.listing)

    def extract(self, image, offset, sector_size, inode, output, limit):
        output.write(self.payloads[inode])
        if self.mutate_source:
            with image.open("ab") as source:
                source.write(b"changed")
        if self.error:
            raise RecoveryError("damaged extent")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.image = self.root / "fixture.raw"
        boot = bytearray(1024)
        boot[3:11] = b"NTFS    "
        boot[11:13] = (512).to_bytes(2, "little")
        boot[510:512] = b"\x55\xaa"
        self.image.write_bytes(boot)
        self.backend = FakeBackend()
        self.session = self.root / "session"
        self.destination = self.root / "export"

    def tearDown(self):
        self.temporary.cleanup()

    def start(self):
        return scan(self.image, self.session, backend=self.backend)

    def test_pipeline_exports_exact_bytes_but_does_not_claim_integrity(self):
        initial = self.image.read_bytes()
        self.start()
        result = recover(self.session, self.destination, backend=self.backend)
        file = result["results"][0]
        self.assertEqual(file["status"], "exported_unverified")
        self.assertEqual((self.destination / file["saved_path"]).read_bytes(), b"hello")
        self.assertEqual(file["sha256"], hashlib.sha256(b"hello").hexdigest())
        self.assertEqual(self.image.read_bytes(), initial)

    def test_source_changes_are_detected_before_creating_export(self):
        self.start()
        self.image.write_bytes(self.image.read_bytes() + b"changed")
        with self.assertRaises(RecoveryError):
            recover(self.session, self.destination, backend=self.backend)
        self.assertFalse(self.destination.exists())

    def test_change_during_extraction_is_recorded(self):
        self.start()
        self.backend.mutate_source = True
        result = recover(self.session, self.destination, backend=self.backend)
        self.assertEqual(result["status"], "source_changed")
        self.assertEqual(read_json(self.destination / "recovery.json")["status"], "source_changed")

    def test_existing_destination_and_symlink_are_not_reused(self):
        self.start()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep").write_bytes(b"original")
        self.destination.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RecoveryError):
            recover(self.session, self.destination, backend=self.backend)
        self.assertEqual((outside / "keep").read_bytes(), b"original")

    def test_existing_scan_directory_is_not_reused(self):
        self.start()
        with self.assertRaises(RecoveryError):
            self.start()

    def test_bad_geometry_is_rejected_without_output(self):
        with self.assertRaises(RecoveryError):
            scan(self.image, self.session, backend=self.backend, offset=1)
        self.assertFalse(self.session.exists())

    def test_invalid_selected_id_does_not_create_output(self):
        self.start()
        with self.assertRaises(RecoveryError):
            recover(self.session, self.destination, backend=self.backend, candidate_ids=["missing"])
        self.assertFalse(self.destination.exists())

    def test_failed_extraction_is_not_marked_complete_even_with_matching_length(self):
        self.start()
        self.backend.error = True
        report = recover(self.session, self.destination, backend=self.backend)
        self.assertEqual(report["results"][0]["status"], "partial")

    def test_length_mismatch_and_size_limit(self):
        self.start()
        self.backend.payloads["65-128-1"] = b"he"
        result = recover(self.session, self.destination, backend=self.backend)
        self.assertEqual(result["partial_count"], 1)
        result = recover(self.session, self.root / "skipped", backend=self.backend, max_file_bytes=1)
        self.assertEqual(result["skipped_count"], 1)

    def test_hostile_path_cannot_escape_destination(self):
        self.backend.listing = body("/../../Windows/CON.txt")
        self.start()
        result = recover(self.session, self.destination, backend=self.backend)
        path = (self.destination / result["results"][0]["saved_path"]).resolve()
        self.assertTrue(path.is_relative_to(self.destination.resolve()))
        self.assertIn("%2E%2E", path.parts)
        self.assertEqual(path.name, "_CON.txt")

    def test_recycle_pair_restores_path_as_unverified_evidence(self):
        original = "X:\\Documents\\报告.txt"
        encoded = (original + "\0").encode("utf-16le")
        metadata = struct.pack("<QQQI", 2, 5, 133000000000000000, len(encoded) // 2) + encoded
        parent = "/$Recycle.Bin/S-1-5-21-123-456-789-1001/"
        self.backend.listing = body(parent + "$IABC123.txt", "66-128-1", len(metadata)) + body(parent + "$RABC123.txt")
        self.backend.payloads["66-128-1"] = metadata
        result = self.start()
        payload = next(c for c in result["candidates"] if c["kind"] == "file")
        self.assertEqual(payload["original_path"], original)
        self.assertEqual(payload["path_evidence"], "recycle_metadata_association_unverified")
        recovered = recover(self.session, self.destination, backend=self.backend)
        self.assertEqual(recovered["processed_count"], 1)

    def test_recycle_size_mismatch_keeps_original_path_unknown(self):
        original = "X:\\a.txt"
        encoded = (original + "\0").encode("utf-16le")
        metadata = struct.pack("<QQQI", 2, 999, 133000000000000000, len(encoded) // 2) + encoded
        parent = "/$Recycle.Bin/S-1-5-21-123-456-789-1001/"
        self.backend.listing = body(parent + "$IABC123.txt", "66-128-1", len(metadata)) + body(parent + "$RABC123.txt")
        self.backend.payloads["66-128-1"] = metadata
        result = self.start()
        payload = next(c for c in result["candidates"] if c["kind"] == "file")
        self.assertIsNone(payload["original_path"])

    def test_tsk_orphan_directory_is_not_claimed_as_original_path(self):
        self.backend.listing = body("/$OrphanFiles/recovered.txt")
        result = self.start()
        self.assertIsNone(result["candidates"][0]["original_path"])

    def test_nested_recycle_payload_needs_directory_association_before_original_path_claim(self):
        self.backend.listing = body("/$Recycle.Bin/S-1-5-21-123-456-789-1001/$RABC123/report.txt")
        result = self.start()
        self.assertIsNone(result["candidates"][0]["original_path"])


if __name__ == "__main__":
    unittest.main()
