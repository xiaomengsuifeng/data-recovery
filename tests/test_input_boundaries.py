"""Malformed on-disk layouts and passive preview boundaries, without raw disks."""
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib

from recovery_core.common import RecoveryError
from recovery_core.filesystems import boot_format, exfat_geometry, exfat_bitmap, identify
from recovery_core.partitions import inspect_image
from recovery_core.preview import describe, preview, MAX_TEXT, MAX_PREVIEW_BYTES
from recovery_core.service import scan
from test_core import FakeBackend
from test_desktop_core import boot
from test_exfat import make_exfat


class PartitionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.image = Path(self.temporary.name) / "layout.img"

    def gpt(self, sector=512, *, count=2, entry_size=128, first=40, last=103):
        data = bytearray(sector * 160)
        data[510:512] = b"\x55\xaa"
        data[450] = 238
        struct.pack_into("<II", data, 454, 1, 159)
        entries = bytearray(256)
        entries[:16] = bytes([1]) * 16
        struct.pack_into("<QQ", entries, 32, first, last)
        header = bytearray(sector)
        header[:8] = b"EFI PART"
        struct.pack_into("<I", header, 12, 92)
        struct.pack_into("<QIII", header, 72, 2, count, entry_size, zlib.crc32(entries))
        struct.pack_into("<I", header, 16, zlib.crc32(header[:92]))
        data[sector:2 * sector] = header
        data[2 * sector:2 * sector + 256] = entries
        data[40 * sector:40 * sector + 512] = boot(bps=sector)
        return data

    def test_gpt_512_and_4096_with_unused_entries(self):
        for sector in (512, 4096):
            with self.subTest(sector=sector):
                self.image.write_bytes(self.gpt(sector))
                result, = inspect_image(self.image)
                self.assertEqual((result["offset"], result["sector_size"], result["size"]),
                                 (40, sector, 64 * sector))

    def test_gpt_header_corruption_cannot_fall_back_to_guessing(self):
        data = self.gpt()
        data[512 + 24] ^= 1
        self.image.write_bytes(data)
        with self.assertRaisesRegex(RecoveryError, "GPT"):
            inspect_image(self.image)

    def test_gpt_invalid_entry_budgets(self):
        for count, size in ((0, 128), (4097, 128), (1, 127), (1, 4097)):
            with self.subTest(count=count, size=size):
                self.image.write_bytes(self.gpt(count=count, entry_size=size))
                with self.assertRaises(RecoveryError):
                    inspect_image(self.image)

    def test_gpt_reversed_and_undersized_partitions_are_not_probed(self):
        for first, last in ((50, 40), (40, 42)):
            self.image.write_bytes(self.gpt(first=first, last=last))
            with self.subTest(first=first, last=last), self.assertRaises(RecoveryError):
                inspect_image(self.image)

    def extended(self, *, loop=False, outside=False, broken=False):
        data = bytearray(512 * 200)
        data[510:512] = b"\x55\xaa"
        data[450] = 15
        struct.pack_into("<II", data, 454, 2, 190)
        for at, next_offset in ((2, 80), (82, 80 if loop else 0)):
            origin = at * 512
            data[origin + 510:origin + 512] = b"\x55\xaa"
            data[origin + 450] = 7
            struct.pack_into("<II", data, origin + 454, 1, 64)
            if next_offset:
                data[origin + 466] = 15
                struct.pack_into("<II", data, origin + 470, next_offset, 100)
            data[(at + 1) * 512:(at + 2) * 512] = boot()
        if outside:
            struct.pack_into("<I", data, 2 * 512 + 458, 1000)
        if broken:
            data[82 * 512 + 510] = 0
        return data

    def test_logical_partition_chain_and_cycle_are_bounded(self):
        for loop in (False, True):
            self.image.write_bytes(self.extended(loop=loop))
            before = self.image.read_bytes()
            self.assertEqual([r["offset"] for r in inspect_image(self.image)], [3, 83])
            self.assertEqual(self.image.read_bytes(), before)

    def test_ebr_outside_container_and_missing_signature_are_skipped(self):
        for kwargs, expected in (({"outside": True}, [83]), ({"broken": True}, [3])):
            self.image.write_bytes(self.extended(**kwargs))
            self.assertEqual([r["offset"] for r in inspect_image(self.image)], expected)

    def test_boot_truncation_unknown_filesystem_and_sector_dimensions(self):
        original = boot()
        for data in (b"", original[:511], bytes(512), boot(bps=256), boot(bps=8192)):
            with self.subTest(length=len(data)), self.assertRaises(RecoveryError):
                boot_format(data)
        self.image.write_bytes(original)
        for offset, sector in ((True, 512), (-1, 512), (0, True), (0, 1024)):
            with self.subTest(offset=offset, sector=sector), self.assertRaises(RecoveryError):
                identify(self.image, offset, sector)

    def test_exfat_requires_full_boot_and_actual_exfat(self):
        self.image.write_bytes(boot())
        with self.assertRaises(RecoveryError):
            exfat_geometry(self.image, 0, 512)
        make_exfat(self.image)
        self.image.write_bytes(self.image.read_bytes()[:512])
        with self.assertRaises(RecoveryError):
            exfat_geometry(self.image, 0, 512)

    def test_exfat_root_chain_cycle_and_invalid_cluster_are_rejected(self):
        make_exfat(self.image)
        original = self.image.read_bytes()
        for next_cluster in (2, 0, 98):
            data = bytearray(original)
            # No end marker in the directory: force the next FAT link to be checked.
            data[32 * 512:33 * 512] = (b"\x85" + bytes(31)) * 16
            struct.pack_into("<I", data, 24 * 512 + 8, next_cluster)
            self.image.write_bytes(data)
            with self.subTest(cluster=next_cluster), self.assertRaises(RecoveryError):
                exfat_bitmap(self.image, exfat_geometry(self.image, 0, 512))

    def test_exfat_truncated_bitmap_and_duplicate_bitmap_are_rejected(self):
        make_exfat(self.image)
        volume = exfat_geometry(self.image, 0, 512)
        original = self.image.read_bytes()
        data = bytearray(original)
        data[32 * 512 + 32:32 * 512 + 64] = data[32 * 512:32 * 512 + 32]
        for payload in (original[:33 * 512 + 1], data):
            self.image.write_bytes(payload)
            with self.assertRaises(RecoveryError):
                exfat_bitmap(self.image, volume)


def document(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


class PassivePreviewBoundaryTests(unittest.TestCase):
    def test_all_supported_document_containers_extract_only_text(self):
        for suffix, name in (("docx", "word/document.xml"), ("xlsx", "xl/sharedStrings.xml"),
                             ("pptx", "ppt/slides/slide1.xml"), ("odt", "content.xml")):
            payload = document([(name, "<document><p>中文 &amp; text</p></document>"),
                                ("../escape.txt", "must not be extracted")])
            with self.subTest(suffix=suffix):
                result = describe(payload, "example." + suffix)
                self.assertEqual(result["kind"], "text")
                self.assertEqual(result["text"], "中文 & text")

    def test_office_part_count_and_display_length_are_bounded(self):
        for suffix, prefix in (("xlsx", "xl/worksheets/sheet"), ("pptx", "ppt/slides/slide")):
            entries = [(prefix + f"{n:02}.xml", f"<p>part-{n:02}</p>") for n in range(20)]
            text = describe(document(entries), "test." + suffix)["text"]
            self.assertEqual(text.count("part-"), 12)
            self.assertNotIn("part-12", text)
        payload = document([("word/document.xml", "<p>" + "字" * (MAX_TEXT + 1) + "</p>")])
        self.assertEqual(len(describe(payload, "test.docx")["text"]), MAX_TEXT)

    def test_zip_bomb_entry_and_xml_declarations_are_not_expanded(self):
        for xml in (b"x" * (2 * 1024 * 1024 + 1), b"<!entity x 'x'><p/>",
                    b"<!doctype p><p/>", "<p/>".encode("utf-16")):
            with self.subTest(length=len(xml)), self.assertRaises(RecoveryError):
                describe(document([("word/document.xml", xml)]), "test.docx")

    def test_broken_missing_and_invalid_xml_return_readable_failure(self):
        for payload in (b"broken zip", document([]), document([("word/document.xml", "<p>")])):
            result = describe(payload, "test.docx")
            self.assertIn("未能解析", result["text"])
            self.assertTrue(result["note"])

    def test_text_encodings_case_insensitivity_and_empty_input(self):
        for encoding in ("utf-8-sig", "utf-16", "gb18030"):
            self.assertEqual(describe("中文测试".encode(encoding), "REPORT.TXT")["text"], "中文测试")
        self.assertEqual(describe(b"", "empty.txt")["text"], "")
        self.assertEqual(len(describe(b"x" * (MAX_TEXT + 1), "large.txt")["text"]), MAX_TEXT)

    def test_binary_and_null_text_show_bounded_hex(self):
        for name, data in (("binary.bin", bytes(range(256)) * 20), ("null.txt", b"a\0b"),
                           ("invalid.txt", b"\xff")):
            result = describe(data, name)
            self.assertEqual(result["format"], "hex")
            self.assertLessEqual(len(result["text"].splitlines()), 128)
        self.assertIn("000007f0", describe(bytes(4096), "data.bin")["text"])

    def test_preview_rejects_missing_id_oversize_and_engine_change_before_extract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "test.img"
            image.write_bytes(boot() + bytes(63 * 512))
            backend = FakeBackend()
            report = scan(image, root / "scan", backend=backend)
            identifier = report["candidates"][0]["id"]
            with patch.object(backend, "extract") as extract:
                with self.assertRaises(RecoveryError):
                    preview(root / "scan", "unknown", backend)
                with patch("recovery_core.preview.MAX_PREVIEW_BYTES", 4), self.assertRaises(RecoveryError):
                    preview(root / "scan", identifier, backend)
                with patch.object(backend, "versions", {}), self.assertRaises(RecoveryError):
                    preview(root / "scan", identifier, backend)
                extract.assert_not_called()

    def test_short_preview_is_explicitly_marked_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "test.img"
            image.write_bytes(boot() + bytes(63 * 512))
            backend = FakeBackend()
            report = scan(image, root / "scan", backend=backend)
            with patch.object(backend, "extract", side_effect=lambda *args: args[4].write(b"hi")):
                result = preview(root / "scan", report["candidates"][0]["id"], backend)
            self.assertEqual(result["text"], "hi")
            self.assertIn("不完整", result["note"])
