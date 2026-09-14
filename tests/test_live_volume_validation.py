import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from recovery_core.common import RecoveryError, write_json
from tools.live_volume_validation import copy_stage_vhd, compare_results, expect_rejected, run_mounted


def footer(size, kind=2):
    raw = bytearray(512)
    raw[:8] = b'conectix'
    struct.pack_into('>I', raw, 12, 0x10000)
    struct.pack_into('>Q', raw, 16, (1 << 64) - 1)
    struct.pack_into('>QQ', raw, 40, size, size)
    struct.pack_into('>I', raw, 60, kind)
    struct.pack_into('>I', raw, 64, ~sum(raw) & 0xffffffff)
    return bytes(raw)


class LiveVolumeValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.image = self.root / 'stage.img'
        self.container = self.root / 'fixture.vhd'
        self.target = self.root / 'readonly-copy.vhd'
        self.raw = bytes(range(256)) * 16
        self.image.write_bytes(self.raw)
        self.container.write_bytes(bytes(len(self.raw)) + footer(len(self.raw)))
        self.digest = hashlib.sha256(self.raw).hexdigest()

    def copy_stage(self):
        copy_stage_vhd(self.image, self.container, self.target, self.digest, len(self.raw))

    def test_exact_frozen_stage_and_footer_are_copied_without_changing_originals(self):
        container = self.container.read_bytes()
        self.copy_stage()
        self.assertEqual(self.target.read_bytes(), self.raw + footer(len(self.raw)))
        self.assertEqual(self.image.read_bytes(), self.raw)
        self.assertEqual(self.container.read_bytes(), container)

    def test_existing_target_is_never_overwritten(self):
        self.target.write_bytes(b'previous evidence')
        with self.assertRaises(FileExistsError):
            self.copy_stage()
        self.assertEqual(self.target.read_bytes(), b'previous evidence')

    def test_bad_sizes_cookie_checksum_and_nonfixed_layout_create_no_copy(self):
        original = self.container.read_bytes()
        malformed = [original[:-1], original[:len(self.raw)] + footer(len(self.raw), kind=3),
                     original[:len(self.raw)] + footer(len(self.raw) * 2)]
        for offset in (len(self.raw), len(self.raw) + 64, len(self.raw) + 16):
            changed = bytearray(original)
            changed[offset] ^= 1
            malformed.append(bytes(changed))
        for data in malformed:
            with self.subTest(tail=data[-8:]):
                self.container.write_bytes(data)
                with self.assertRaises(RecoveryError):
                    self.copy_stage()
                self.assertFalse(self.target.exists())
        self.container.write_bytes(original)
        self.image.write_bytes(b'')
        with self.assertRaises(RecoveryError):
            self.copy_stage()
        self.assertFalse(self.target.exists())

    def test_changed_raw_bytes_do_not_receive_a_mountable_footer(self):
        self.digest = 'a' * 64
        with self.assertRaisesRegex(RecoveryError, 'changed'):
            self.copy_stage()
        self.assertEqual(self.target.read_bytes(), self.raw)

    def test_guard_refuses_a_writable_or_wrong_copy_before_accessing_the_volume(self):
        write_json(self.root / 'plan.json', {'schema_version': 1, 'copy': {'path': str(self.target)}})
        for guard in ({'schema_version': 1, 'copy': str(self.target), 'read_only': False},
                      {'schema_version': 1, 'copy': str(self.image), 'read_only': True}):
            (self.root / 'mount.json').write_text(json.dumps(guard), encoding='utf-8')
            with patch('tools.live_volume_validation.is_admin', return_value=True), \
                    patch('tools.live_volume_validation.check_volume') as access, \
                    self.assertRaisesRegex(RecoveryError, 'Missing read-only copied-VHD guard'):
                run_mounted(self.root, self.root)
            access.assert_not_called()

    def test_negative_check_requires_correct_error_and_no_output(self):
        def rejected():
            raise RecoveryError('same disk')
        self.assertEqual(expect_rejected(rejected, self.target, 'same disk'), 'same disk')
        with self.assertRaises(RecoveryError):
            expect_rejected(lambda: None, self.target)
        with self.assertRaises(RecoveryError):
            expect_rejected(rejected, self.target, 'disconnected')
        def wrote_before_rejecting():
            self.target.write_bytes(b'wrongly created')
            rejected()
        with self.assertRaises(RecoveryError):
            expect_rejected(wrote_before_rejecting, self.target)

    def test_comparison_requires_bytes_paths_status_and_distinct_source_check_semantics(self):
        row = dict(candidate_id='one', status='exported_unverified', actual_size=7, sha256='a' * 64,
                   original_path='/original.txt', observed_path='/deleted.txt', saved_path='files/original.txt')
        image = dict(status='completed', source_verification='unchanged', results=[row])
        volume = dict(status='completed', source_verification='live_volume_identity_only', results=[dict(row)])
        compare_results(image, volume)
        for field, value in [('sha256', 'b' * 64), ('actual_size', 6), ('status', 'partial'),
                             ('original_path', None), ('saved_path', 'files/wrong.txt')]:
            changed = copy.deepcopy(volume)
            changed['results'][0][field] = value
            with self.subTest(field=field), self.assertRaises(RecoveryError):
                compare_results(image, changed)
        with self.assertRaises(RecoveryError):
            compare_results(image, dict(volume, source_verification='unchanged'))
        with self.assertRaises(RecoveryError):
            compare_results(image, dict(volume, status='source_changed'))


if __name__ == '__main__':
    unittest.main()
