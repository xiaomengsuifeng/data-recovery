import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib

from recovery_core.common import RecoveryError, sha256_file
from recovery_core.control import TaskControl, task_scope, OperationCancelled
from recovery_core.partitions import inspect_image
from recovery_core.preview import describe, preview
from recovery_core.service import scan, recover, safe_relative_path
from recovery_core.tsk import Tsk
from test_core import FakeBackend, body


def boot(sectors=64, bps=512):
    data = bytearray(512)
    data[3:11] = b"NTFS    "
    struct.pack_into("<H", data, 11, bps)
    struct.pack_into("<Q", data, 40, sectors)
    data[510:512] = b"\x55\xaa"
    return data


class DesktopCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.image = self.root / "volume.img"
        self.image.write_bytes(boot() + bytes(63 * 512))

    def tearDown(self):
        self.tmp.cleanup()

    def test_partition_image_detected(self):
        result = inspect_image(self.image)
        self.assertEqual(result[0]["offset"], 0)
        self.assertEqual(result[0]["size"], 32768)

    def test_mbr_image_offset_detected(self):
        data = bytearray(512 * 66)
        data[510:512] = b"\x55\xaa"
        data[450] = 7
        struct.pack_into("<II", data, 454, 2, 64)
        data[1024:1536] = boot()
        self.image.write_bytes(data)
        self.assertEqual(inspect_image(self.image)[0]["offset"], 2)

    def test_gpt_and_crc(self):
        data = bytearray(512 * 160)
        data[510:512] = b"\x55\xaa"
        data[450] = 238
        struct.pack_into("<II", data, 454, 1, 159)
        array = bytearray(128)
        array[:16] = bytes([1]) * 16
        struct.pack_into("<QQ", array, 32, 40, 103)
        header = bytearray(512)
        header[:8] = b"EFI PART"
        struct.pack_into("<I", header, 12, 92)
        struct.pack_into("<QIII", header, 72, 2, 1, 128, zlib.crc32(array))
        struct.pack_into("<I", header, 16, zlib.crc32(header[:92]))
        data[512:1024] = header
        data[1024:1152] = array
        data[40*512:41*512] = boot()
        self.image.write_bytes(data)
        self.assertEqual(inspect_image(self.image)[0]["offset"], 40)
        data[1040] ^= 1
        self.image.write_bytes(data)
        with self.assertRaises(RecoveryError): inspect_image(self.image)

    def test_bounds_and_unsupported(self):
        self.image.write_bytes(boot(999999) + bytes(512))
        with self.assertRaises(RecoveryError): inspect_image(self.image)
        self.image.write_bytes(bytes(1024))
        with self.assertRaises(RecoveryError): inspect_image(self.image)

    def test_cooperative_cancel_hash(self):
        control = TaskControl()
        control.cancelled.set()
        with task_scope(control), self.assertRaises(OperationCancelled): sha256_file(self.image)

    def test_plain_text_html_is_not_evaluated(self):
        result = describe(b'<script>alert(1)</script>', 'test.html')
        self.assertEqual(result["kind"], "text")
        self.assertIn('<script>', result["text"])

    def test_docx_preview_does_not_execute_or_expand_entities(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", '<doc><t>季度报告</t></doc>')
        self.assertIn("季度报告", describe(buf.getvalue(), 'report.docx')["text"])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", '<!DOCTYPE foo [<!ENTITY a "xx">]><doc>&a;</doc>')
        with self.assertRaises(RecoveryError): describe(buf.getvalue(), 'report.docx')

    def test_preview_real_pipeline_and_source_change(self):
        backend = FakeBackend()
        report = scan(self.image, self.root / "scan", backend=backend)
        result = preview(self.root / 'scan', report['candidates'][0]['id'], backend)
        self.assertEqual(result['text'], 'hello')
        self.image.write_bytes(self.image.read_bytes() + b'change')
        with self.assertRaises(RecoveryError): preview(self.root / 'scan', report['candidates'][0]['id'], backend)

    def test_unicode_export_path_and_shared_directory(self):
        self.assertEqual(safe_relative_path(r'C:\文档\报告.txt').as_posix(), '文档/报告.txt')
        backend = FakeBackend()
        scan(self.image, self.root / "scan", backend=backend)
        result = recover(self.root / 'scan', self.root / 'export', backend=backend)
        self.assertEqual(result['results'][0]['saved_path'], 'files/Documents/report.txt')

    def test_empty_explicit_selection_does_not_recover_everything(self):
        backend = FakeBackend()
        scan(self.image, self.root / "scan", backend=backend)
        with self.assertRaises(RecoveryError):
            recover(self.root / 'scan', self.root / 'export', backend=backend, candidate_ids=[])
        self.assertFalse((self.root / 'export').exists())

    def test_collision_suffix_cannot_alias_another_export(self):
        from recovery_core.service import _candidate_id
        # A real filename can already contain the suffix we generate for a duplicate.
        duplicate_id = _candidate_id('67-128-1', '/a.txt')
        names = [f'/a__{duplicate_id}.txt', '/a.txt', '/a.txt']
        listing = ''.join(body(name, f'{65+i}-128-1') for i, name in enumerate(names))
        payloads = {f'{65+i}-128-1': str(i).encode() * 5 for i in range(3)}
        backend = FakeBackend(listing, payloads)
        scan(self.image, self.root / 'scan', backend=backend)
        result = recover(self.root / 'scan', self.root / 'export', backend=backend)
        self.assertEqual(result['exported_unverified_count'], 3)
        paths = [r['saved_path'] for r in result['results']]
        self.assertEqual(len(set(paths)), 3)
        self.assertEqual([(self.root / 'export' / p).read_bytes() for p in paths], [b'00000', b'11111', b'22222'])

    def test_deleted_directories_traversed_once_even_with_cycle(self):
        backend = object.__new__(Tsk)
        def listing(image, offset, sector_size, directories=False, inode=None, prefix='/'):
            if directories:
                return '0|/Documents (deleted)|50-144-1|-/drwxrwxrwx|0|0|0|1|1|1|1\n'
            if inode:
                return '0|/Documents/a.txt (deleted)|55-128-1|-/rrwxrwxrwx|0|0|5|1|1|1|1\n'
            return ''
        backend._listing = listing
        rows, warnings = backend.scan(self.image, 0, 512)
        self.assertEqual([r['observed_path'] for r in rows], ['/Documents/a.txt'])


if __name__ == '__main__': unittest.main()
