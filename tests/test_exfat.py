from pathlib import Path
import struct
import tempfile
import unittest

from recovery_core.common import RecoveryError
from recovery_core.filesystems import exfat_geometry, exfat_bitmap, identify
from recovery_core.partitions import inspect_image
from recovery_core.service import scan, recover
from recovery_core import carving
from recovery_core.tsk import parse_body
from test_carving import png, BitmapBackend


def update_checksum(data, origin=0):
    checksum = 0
    for index, value in enumerate(data[origin:origin + 11 * 512]):
        if index not in (106, 107, 112):
            checksum = (((checksum >> 1) | ((checksum & 1) << 31)) + value) & 0xffffffff
    data[origin + 11 * 512:origin + 12 * 512] = struct.pack("<I", checksum) * 128


def make_exfat(path, *, offset=0):
    data = bytearray((offset + 128) * 512)
    at = offset * 512
    data[at:at + 11] = b"\xeb\x76\x90EXFAT   "
    struct.pack_into("<QQIIIII", data, at + 64, offset, 128, 24, 1, 32, 96, 2)
    data[at + 104:at + 111] = b"\0\x01\0\0\x09\0\x01"
    data[at + 510:at + 512] = b"\x55\xaa"
    for cluster in (0, 1, 2, 3):
        struct.pack_into("<I", data, at + 24 * 512 + 4 * cluster, 0xffffffff)
    data[at + 32 * 512] = 0x81
    struct.pack_into("<IQ", data, at + 32 * 512 + 20, 3, 12)
    data[at + 33 * 512] = 0x03
    payload = png()
    data[at + 34 * 512:at + 34 * 512 + len(payload)] = payload
    update_checksum(data, at)
    path.write_bytes(data)
    return path


class ExfatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.image = make_exfat(self.root / "exfat.img")

    def tearDown(self):
        self.tmp.cleanup()

    def test_layout_bitmap_and_content_scan_use_cluster_heap_origin(self):
        volume = exfat_geometry(self.image, 0, 512)
        self.assertEqual((volume.start, volume.cluster_count, volume.cluster_size), (32 * 512, 96, 512))
        self.assertEqual(exfat_bitmap(self.image, volume), b"\x03" + bytes(11))
        partitions = inspect_image(self.image)
        self.assertEqual(partitions[0]["filesystem"], "exfat")
        original = self.image.read_bytes()
        report = scan(self.image, self.root / "scan", backend=BitmapBackend(), deep_png=True)
        item, = report["candidates"]
        self.assertEqual(item["carving"]["image_offset"], 34 * 512)
        recovered = recover(self.root / "scan", self.root / "out", backend=BitmapBackend())
        self.assertEqual((self.root / "out" / recovered["results"][0]["saved_path"]).read_bytes(), png())
        self.assertEqual(self.image.read_bytes(), original)

    def test_boot_checksum_bounds_and_transactional_volume_refused(self):
        original = self.image.read_bytes()
        for case in ("checksum", "heap", "fat", "root", "texfat", "shift", "size"):
            data = bytearray(original)
            if case == "checksum":
                data[123] ^= 1
            else:
                if case == "heap":
                    struct.pack_into("<I", data, 88, 500)
                elif case == "fat":
                    struct.pack_into("<I", data, 80, 12)
                elif case == "root":
                    struct.pack_into("<I", data, 96, 99)
                elif case == "texfat":
                    data[110] = 2
                elif case == "shift":
                    data[109] = 31
                else:
                    struct.pack_into("<Q", data, 72, 500)
                update_checksum(data)
            self.image.write_bytes(data)
            with self.subTest(case=case), self.assertRaises(RecoveryError):
                exfat_geometry(self.image, 0, 512)

    def test_bitmap_corruption_and_missing_metadata_fail_closed(self):
        data = bytearray(self.image.read_bytes())
        struct.pack_into("<Q", data, 32 * 512 + 24, 11)
        self.image.write_bytes(data)
        with self.assertRaises(RecoveryError):
            exfat_bitmap(self.image, exfat_geometry(self.image, 0, 512))
        data[32 * 512] = 0
        self.image.write_bytes(data)
        with self.assertRaises(RecoveryError):
            exfat_bitmap(self.image, exfat_geometry(self.image, 0, 512))

    def test_offset_image_and_wrong_sector_size(self):
        make_exfat(self.image, offset=5)
        self.assertEqual(identify(self.image, 5, 512), "exfat")
        self.assertEqual(exfat_geometry(self.image, 5, 512).start, 37 * 512)
        with self.assertRaises(RecoveryError):
            identify(self.image, 0, 512)

    def test_exfat_numeric_records_do_not_relax_ntfs_attribute_checks(self):
        line = "0|/folder/file.txt (deleted)|123|r/rrwxrwxrwx|0|0|7|0|0|0|0"
        self.assertEqual(parse_body(line)[0], [])
        rows, _ = parse_body(line, "exfat")
        self.assertEqual(rows[0]["recovery_method"], "exfat_metadata")
        self.assertEqual(rows[0]["original_path"], "/folder/file.txt")

    def test_ntfs_log_option_refused_before_output_creation(self):
        with self.assertRaises(RecoveryError):
            scan(self.image, self.root / "scan", backend=BitmapBackend(), deep_log=True)
        self.assertFalse((self.root / "scan").exists())
