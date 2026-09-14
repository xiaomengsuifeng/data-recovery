import unittest
from unittest.mock import patch
from recovery_core.common import RecoveryError, check_identity
from recovery_core.windows import validate_volume_rows, check_volume, ensure_safe_locations, _DISCOVER


class WindowsContractTests(unittest.TestCase):
    def row(self):
        return dict(kind='windows_volume', mount='E:\\', path='\\\\.\\E:', guid='volume-id', size=102400,
                    disk_numbers=[2], disk_ids=['disk-2'], sector_size=512)

    def test_windows_identity_is_not_content_hash_verification(self):
        record=self.row()
        with patch('recovery_core.windows.volume_identity',return_value=record):
            self.assertEqual(check_identity(record),record)

    def test_tampered_device_path_and_replaced_disk_are_rejected(self):
        record=self.row()
        with patch('recovery_core.windows.volume_identity',return_value=record):
            for key,value in (('path','\\\\.\\PhysicalDrive0'),('guid','another'),('size',100),('disk_ids',['other']),('disk_numbers',[0])):
                with self.subTest(key=key),self.assertRaises(RecoveryError):check_volume(dict(record,**{key:value}))

    def test_bad_discovery_does_not_grant_device_access(self):
        valid=self.row()
        self.assertEqual(validate_volume_rows([valid]),[valid])
        for key,value in (('mount','C:\\Windows'),('size',-1),('disk_numbers','0'),('disk_ids',[None]),('sector_size',12)):
            with self.subTest(key=key),self.assertRaises(RecoveryError):validate_volume_rows([dict(valid,**{key:value})])

    def test_discovery_script_is_read_only(self):
        for operation in ('Format-Volume','Clear-Disk','Initialize-Disk','Remove-Partition','Set-Disk','Mount-DiskImage'):
            self.assertNotIn(operation,_DISCOVER)

    def test_reopened_live_session_rechecks_runtime_and_destinations(self):
        from pathlib import Path
        with patch('recovery_core.windows.ensure_other_disk') as check:
            ensure_safe_locations(self.row(), Path('session'), Path('destination'))
            self.assertEqual(check.call_count, 4)
            self.assertEqual(check.call_args_list[-1].args[1], Path('destination'))
        with patch('recovery_core.windows.ensure_other_disk', side_effect=RecoveryError('same disk')):
            with self.assertRaises(RecoveryError):ensure_safe_locations(self.row(), Path('destination'))


if __name__=='__main__':unittest.main()
