import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recovery_core.service import recover, scan
from test_core import FakeBackend


class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.image = self.root / "fixture.img"
        data = bytearray(1024)
        data[3:11] = b"NTFS    "
        data[11:13] = (512).to_bytes(2, "little")
        data[510:512] = b"\x55\xaa"
        self.image.write_bytes(data)
        self.backend = FakeBackend()
        self.session = self.root / "session"
        self.destination = self.root / "recovered"
        self.scanned = scan(self.image, self.session, backend=self.backend)

    def tearDown(self):
        self.temporary.cleanup()

    def assert_report(self, result):
        saved = json.loads((self.destination / "recovery.json").read_text())
        self.assertEqual(saved["status"], "cancelled")
        self.assertEqual(saved, result)
        self.assertEqual(saved["source_verification"], "not_completed_due_to_cancellation")
        self.assertEqual(saved["processed_count"], 1)

    def test_cancel_while_hashing_export_preserves_path_and_unfinished_hash(self):
        with patch("recovery_core.service.sha256_file", side_effect=KeyboardInterrupt):
            result = recover(self.session, self.destination, backend=self.backend)
        self.assert_report(result)
        output = result["results"][0]
        self.assertEqual(output["status"], "partial")
        self.assertIsNone(output["sha256"])
        self.assertEqual((self.destination / output["saved_path"]).read_bytes(), b"hello")

    def test_cancel_final_source_hash_preserves_completed_outputs(self):
        with patch("recovery_core.service.check_identity", side_effect=[self.scanned["source"], KeyboardInterrupt]):
            result = recover(self.session, self.destination, backend=self.backend)
        self.assert_report(result)
        self.assertEqual(result["results"][0]["status"], "exported_unverified")
        self.assertIsNotNone(result["results"][0]["sha256"])

    def test_cancel_extraction_does_not_start_large_post_cancel_hash(self):
        def interrupted(image, offset, sector_size, inode, output, limit):
            output.write(b"he")
            raise KeyboardInterrupt
        self.backend.extract = interrupted
        with patch("recovery_core.service.sha256_file") as hashing:
            result = recover(self.session, self.destination, backend=self.backend)
            hashing.assert_not_called()
        self.assert_report(result)
        self.assertEqual(result["results"][0]["actual_size"], 2)


if __name__ == "__main__":
    unittest.main()
