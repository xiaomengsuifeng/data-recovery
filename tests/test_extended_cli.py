import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recovery_core.cli import main
from recovery_core.common import RecoveryError
from recovery_core.windows import check_disk, disk_identity


class ExtendedCliTests(unittest.TestCase):
    def test_acquire_and_resume_do_not_require_tsk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "original.bin"
            source.write_bytes(bytes(range(256)) * 4)
            with patch("recovery_core.cli.Tsk", side_effect=AssertionError("TSK must not be needed")):
                for command in (["acquire", "--file", str(source), "--output", str(root / "capture")],
                                ["resume-acquire", str(root / "capture")]):
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(main(command), 0)
                    self.assertEqual(json.loads(output.getvalue())["status"], "completed")
            self.assertEqual((root / "capture/image.img").read_bytes(), source.read_bytes())

    def test_acquisition_exit_codes_preserve_missing_data_status(self):
        for status, expected in (("completed_with_errors", 1), ("failed", 2), ("cancelled", 130)):
            with patch("recovery_core.acquisition.acquire", return_value={"status": status}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["acquire", "--file", "test.img", "--output", "capture"]), expected)

    def test_physical_device_identity_rejects_replacement_and_path_tampering(self):
        row = dict(number=7, size=1048576, sector_size=512, disk_ids=["fixture-only"],
                   disk_numbers=[7], label="isolated fixture", system=False)
        with patch("recovery_core.windows.list_disks", return_value=[row]), patch("recovery_core.windows.os.name", "nt"):
            identity = disk_identity(7)
            self.assertEqual(check_disk(identity), identity)
            for change in ({"path": r"\\.\PhysicalDrive0"}, {"disk_ids": ["replaced"]}, {"size": 512}):
                with self.assertRaises(RecoveryError):
                    check_disk(dict(identity, **change))
