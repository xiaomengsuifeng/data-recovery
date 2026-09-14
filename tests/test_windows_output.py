import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from recovery_core.common import RecoveryError, new_directory


@unittest.skipUnless(os.name == "nt", "Windows output ACLs")
class WindowsOutputTests(unittest.TestCase):
    def test_private_acl_explicitly_grants_current_user_and_children_inherit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = new_directory(Path(temporary) / "中文 输出")
            child = root / "nested"
            child.mkdir()
            saved = child / "file.txt"
            saved.write_bytes(b"synthetic output")
            quoted = "'" + str(root).replace("'", "''") + "'"
            program = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$root = ROOT
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$acl = [IO.Directory]::GetAccessControl($root)
$child = [IO.File]::GetAccessControl((Join-Path $root 'nested\file.txt'))
$sections = [Security.AccessControl.AccessControlSections]::All
@{ user_sid = $sid; root_sddl = $acl.GetSecurityDescriptorSddlForm($sections)
   file_sddl = $child.GetSecurityDescriptorSddlForm($sections); protected = $acl.AreAccessRulesProtected } | ConvertTo-Json
""".replace("ROOT", quoted)
            encoded = base64.b64encode(program.encode("utf-16le")).decode("ascii")
            ps = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            result = subprocess.run([str(ps), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                    capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout.decode("utf-8-sig"))
            self.assertTrue(value["protected"])
            self.assertIn("(A;OICI;FA;;;" + value["user_sid"] + ")", value["root_sddl"])
            self.assertIn(";;;" + value["user_sid"] + ")", value["file_sddl"])
            self.assertEqual(value["root_sddl"].count("(A;"), 3)
            self.assertEqual(saved.read_bytes(), b"synthetic output")

    def test_existing_file_and_directory_are_never_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = new_directory(root / "existing-directory")
            file = root / "existing-file"
            file.write_bytes(b"keep")
            for target in (directory, file):
                with self.subTest(target=target.name), self.assertRaises(RecoveryError):
                    new_directory(target)
            self.assertEqual(file.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
