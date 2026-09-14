import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recovery_core.common import RecoveryError, sha256_file
from recovery_core.verification import verify


def digest(data):
    return hashlib.sha256(data).hexdigest()


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "recovery"
        self.root.mkdir()
        self.manifest_path = self.base / "manifest.json"
        self.results, self.targets = [], []

    def add_target(self, path, data):
        self.targets.append({"original_path": path, "size": len(data), "sha256": digest(data)})

    def add_output(self, candidate_id, path, data, status="exported_unverified"):
        saved = f"files/{candidate_id}/data.bin"
        destination = self.root / saved
        destination.parent.mkdir(parents=True)
        destination.write_bytes(data)
        item = {"candidate_id": candidate_id, "original_path": path, "observed_path": path,
                "expected_size": len(data), "actual_size": len(data), "sha256": digest(data),
                "saved_path": saved, "status": status}
        self.results.append(item)
        return item

    def run_verify(self, status="completed"):
        (self.root / "recovery.json").write_text(json.dumps({"schema_version": 1, "status": status, "results": self.results}), encoding="utf-8")
        self.manifest_path.write_text(json.dumps({"schema_version": 1, "files": self.targets}), encoding="utf-8")
        return verify(self.root, self.manifest_path)

    def test_exact_bytes_and_drive_normalized_path(self):
        self.add_target("/Docs/报告.txt", b"original")
        self.add_output("a", "X:\\Docs\\报告.txt", b"original")
        report = self.run_verify()
        self.assertEqual((report["exact_content_matches"], report["correct_paths"]), (1, 1))
        self.assertTrue(report["all_targets_verified"])

    def test_same_content_different_paths_reserves_exact_match_first(self):
        self.add_target("/other/b.txt", b"identical")
        self.add_target("/Docs/a.txt", b"identical")
        self.add_output("a", "/Docs/a.txt", b"identical")
        report = self.run_verify()
        self.assertEqual(report["exact_content_matches"], 1)
        self.assertEqual(report["correct_paths"], 1)
        self.assertIsNone(report["targets"][0]["matched_candidate_id"])
        self.assertEqual(report["targets"][1]["matched_candidate_id"], "a")

    def test_distinct_identical_outputs_match_once_each(self):
        self.add_target("/Docs/a.txt", b"identical")
        self.add_target("/Docs/b.txt", b"identical")
        self.add_output("a", "/Docs/a.txt", b"identical")
        self.add_output("b", "/Docs/b.txt", b"identical")
        self.assertEqual(self.run_verify()["exact_content_matches"], 2)

    def test_content_and_filename_directory_stats_are_separate(self):
        self.add_target("/Docs/a.txt", b"a")
        self.add_target("/Docs/b.txt", b"b")
        self.add_output("a", "/Wrong/a.txt", b"a")
        self.add_output("b", "/Docs/renamed.txt", b"b")
        report = self.run_verify()
        self.assertEqual(report["exact_content_matches"], 2)
        self.assertEqual(report["correct_paths"], 0)
        self.assertEqual(report["correct_filenames"], 1)
        self.assertEqual(report["correct_directories"], 1)

    def test_actual_file_hash_and_size_override_report_claims(self):
        self.add_target("/Docs/a.txt", b"good")
        item = self.add_output("a", "/Docs/a.txt", b"good")
        (self.root / item["saved_path"]).write_bytes(b"bad and longer")
        report = self.run_verify()
        self.assertEqual(report["exact_content_matches"], 0)
        self.assertEqual(report["correct_paths"], 0)
        self.assertFalse(report["outputs"][0]["report_hash_matches"])
        self.assertFalse(report["outputs"][0]["report_size_matches"])

    def test_incorrect_report_claims_do_not_hide_matching_actual_bytes(self):
        self.add_target("/a.txt", b"actual")
        item = self.add_output("a", "/a.txt", b"actual")
        item.update(actual_size=1, sha256="0" * 64)
        report = self.run_verify()
        self.assertEqual(report["exact_content_matches"], 1)
        self.assertFalse(report["outputs"][0]["report_hash_matches"])

    def test_file_change_during_hashing_cannot_count_as_verified(self):
        self.add_target("/a.txt", b"actual")
        self.add_output("a", "/a.txt", b"actual")

        def mutate_after_hash(path):
            result = sha256_file(path)
            path.write_bytes(b"changed after hashing")
            return result

        with patch("recovery_core.verification.sha256_file", side_effect=mutate_after_hash):
            report = self.run_verify()
        self.assertEqual(report["exact_content_matches"], 0)
        self.assertEqual(report["outputs"][0]["status"], "changed_during_verification")

    def test_unknown_original_path_does_not_borrow_observed_path(self):
        self.add_target("/Docs/a.txt", b"a")
        item = self.add_output("a", None, b"a")
        item["observed_path"] = "/Docs/a.txt"
        report = self.run_verify()
        self.assertEqual(report["exact_content_matches"], 1)
        self.assertEqual(report["correct_paths"], 0)

    def test_partial_bytes_can_match_but_cancelled_source_changed_are_not_completed(self):
        self.add_target("/Docs/a.txt", b"a")
        self.add_output("a", "/Docs/a.txt", b"a", status="partial")
        for status in ("cancelled", "source_changed"):
            with self.subTest(status=status):
                report = self.run_verify(status)
                self.assertEqual(report["exact_content_matches"], 1)
                self.assertFalse(report["recovery_completed"])
                self.assertFalse(report["all_targets_verified"])

    def test_zero_targets_are_not_one_hundred_percent(self):
        report = self.run_verify()
        self.assertIsNone(report["content_match_rate"])
        self.assertIsNone(report["path_match_rate"])
        self.assertFalse(report["all_targets_verified"])

    def test_missing_file_is_a_miss(self):
        self.add_target("/a.txt", b"a")
        item = self.add_output("a", "/a.txt", b"a")
        (self.root / item["saved_path"]).unlink()
        report = self.run_verify()
        self.assertEqual(report["exact_content_matches"], 0)
        self.assertEqual(report["outputs"][0]["status"], "missing")

    def test_duplicate_saved_paths_are_rejected(self):
        item = self.add_output("a", "/a.txt", b"a")
        self.results.append({**item, "candidate_id": "b"})
        with self.assertRaisesRegex(RecoveryError, "Duplicate saved_path"):
            self.run_verify()

    def test_duplicate_manifest_paths_are_rejected(self):
        self.add_target("/Docs/a.txt", b"a")
        self.add_target("C:\\docs\\A.TXT", b"a")
        with self.assertRaisesRegex(RecoveryError, "duplicate original paths"):
            self.run_verify()

    def test_saved_path_escape_and_absolute_paths_are_rejected(self):
        item = self.add_output("a", "/a.txt", b"a")
        for path in ("../outside.txt", "/etc/passwd", "C:\\outside.txt", "C:outside.txt", "files/../outside.txt", "files//a", "files/a:stream", "\\\\server\\share\\a"):
            with self.subTest(path=path):
                item["saved_path"] = path
                with self.assertRaises(RecoveryError):
                    self.run_verify()

    def test_file_and_directory_symlinks_are_rejected(self):
        item = self.add_output("a", "/a.txt", b"a")
        file_path = self.root / item["saved_path"]
        file_path.unlink()
        outside = self.base / "outside.txt"
        outside.write_bytes(b"a")
        try:
            file_path.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic links unavailable")
        with self.assertRaisesRegex(RecoveryError, "Symbolic links"):
            self.run_verify()
        file_path.unlink()
        file_path.parent.rmdir()
        file_path.parent.symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(RecoveryError, "Symbolic links"):
            self.run_verify()

    def test_hardlinks_cannot_multiply_output_files(self):
        item = self.add_output("a", "/a.txt", b"a")
        other = self.root / "files" / "b.bin"
        try:
            os.link(self.root / item["saved_path"], other)
        except (OSError, NotImplementedError):
            self.skipTest("Hard links unavailable")
        self.results.append({**item, "candidate_id": "b", "saved_path": "files/b.bin"})
        with self.assertRaisesRegex(RecoveryError, "same output file"):
            self.run_verify()

    def test_manifest_values_are_checked(self):
        valid = {"original_path": "/a.txt", "size": 1, "sha256": digest(b"a")}
        for field, value in (("size", True), ("size", -1), ("size", "1"), ("sha256", "x" * 64), ("sha256", "a" * 63), ("original_path", "../a.txt"), ("original_path", "")):
            with self.subTest(field=field, value=value):
                self.targets = [{**valid, field: value}]
                with self.assertRaises(RecoveryError):
                    self.run_verify()

    def test_invalid_report_status_is_rejected(self):
        with self.assertRaises(RecoveryError):
            self.run_verify("running")

    def test_failed_and_skipped_records_are_not_counted(self):
        self.add_target("/a.txt", b"a")
        self.add_output("a", "/a.txt", b"a", status="failed")
        self.results.append({"candidate_id": "b", "original_path": "/b.txt", "observed_path": "/b.txt", "expected_size": 1, "actual_size": None, "sha256": None, "saved_path": None, "status": "skipped"})
        self.assertEqual(self.run_verify()["exact_content_matches"], 0)


if __name__ == "__main__":
    unittest.main()
