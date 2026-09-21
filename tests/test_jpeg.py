import copy
import hashlib
import io
from pathlib import Path
import random
import struct
import tempfile
import unittest
from unittest.mock import patch

from recovery_core import jpeg
from recovery_core.common import RecoveryError
from recovery_core.service import scan, recover, _validate_candidates
from recovery_core.preview import preview
from test_carving import make_image, BitmapBackend


def segment(marker, payload):
    return bytes((255, marker)) + struct.pack(">H", len(payload) + 2) + payload


def minimal_jpeg(*, restart=False):
    # Two independent blocks with one-bit DC=0 and AC=EOB Huffman codes.
    count = bytes([1]) + bytes(15)
    header = (b"\xff\xd8" + segment(0xdb, b"\0" + bytes([1]) * 64)
              + segment(0xc0, b"\x08\0\x08\0\x10\x01\x01\x11\0")
              + segment(0xc4, b"\0" + count + b"\0" + b"\x10" + count + b"\0"))
    if restart:
        header += segment(0xdd, b"\0\x01")
    return header + segment(0xda, b"\x01\x01\0\0\x3f\0") + (b"\x3f\xff\xd0\x3f" if restart else b"\x0f") + b"\xff\xd9"


def qt_jpeg(progressive=False):
    from PySide6.QtCore import QByteArray, QBuffer, QIODevice
    from PySide6.QtGui import QImage, QImageWriter
    rng = random.Random(619)
    image = QImage(96, 80, QImage.Format.Format_RGB32)
    for y in range(image.height()):
        for x in range(image.width()):
            image.setPixel(x, y, 0xff000000 | rng.randrange(1 << 24))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QImageWriter(buffer, b"jpeg")
    writer.setQuality(88)
    writer.setProgressiveScanWrite(progressive)
    if not writer.write(image):
        raise AssertionError(writer.errorString())
    return bytes(data)


class JpegStructureTests(unittest.TestCase):
    def test_independent_sequential_and_restart_samples(self):
        for restart in (False, True):
            data = minimal_jpeg(restart=restart)
            info = jpeg.inspect_jpeg(data + b"tail")
            self.assertEqual(info["size"], len(data))
            self.assertEqual(info["validation"], "baseline_entropy")
            self.assertEqual(info["sha256"], hashlib.sha256(data).hexdigest())

    def test_entropy_truncation_extra_bytes_and_wrong_restart_are_rejected(self):
        source = minimal_jpeg(restart=True)
        for data in (b"", source[:-1], source.replace(b"\xff\xd0", b"\xff\xd2"),
                     source[:-2] + b"extra\xff\xd9", source.replace(b"\x3f\xff\xd0", b"\x00\xff\xd0")):
            with self.subTest(data=data[-12:]), self.assertRaises(jpeg.InvalidJpeg):
                jpeg.inspect_jpeg(data)

    def test_image_limits_and_malformed_tables(self):
        source = minimal_jpeg()
        for data in (source.replace(b"\x08\0\x08\0\x10", b"\x08\xff\xff\xff\xff"),
                     source.replace(b"\xff\xdb\0C", b"\xff\xdb\0\x01"),
                     source.replace(b"\xff\xc4\0", b"\xff\xc3\0")):
            with self.assertRaises(jpeg.InvalidJpeg):
                jpeg.inspect_jpeg(data)

    def test_qt_encoded_baseline_and_progressive(self):
        try:
            import PySide6
        except ImportError:
            self.skipTest("Qt encoder is optional for core-only tests")
        for progressive in (False, True):
            data = qt_jpeg(progressive)
            info = jpeg.inspect_jpeg(data)
            self.assertEqual(info["size"], len(data))
            self.assertEqual((info["width"], info["height"]), (96, 80))
            if progressive:
                with self.assertRaises(jpeg.InvalidJpeg):
                    jpeg.inspect_jpeg(data, baseline_only=True)


class JpegPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_preview_export_and_modified_extent_refusal(self):
        data = minimal_jpeg()
        image = make_image(self.root / "image.img", [(1023, data)])
        backend = BitmapBackend()
        session = self.root / "session"
        with patch.object(jpeg, "BLOCK", 512):
            result = scan(image, session, backend=backend, offset=3, deep_jpeg=True)
        item, = result["candidates"]
        self.assertEqual(preview(session, item["id"], backend)["data"], data)
        recovered = recover(session, self.root / "out", backend=backend)
        self.assertEqual((self.root / "out" / recovered["results"][0]["saved_path"]).read_bytes(), data)
        malformed = copy.deepcopy(item)
        malformed["carving"]["extents"].append(malformed["carving"]["extents"][0])
        with self.assertRaises(RecoveryError):
            _validate_candidates([malformed])
        with image.open("r+b") as stream:
            stream.seek(item["carving"]["image_offset"])
            stream.write(b"xx")
        with self.assertRaises(RecoveryError):
            jpeg.extract_jpeg(image, 3, 512, item, io.BytesIO(), backend=backend)

    def test_two_and_multiple_fragment_reassembly_matches_original(self):
        try:
            data = qt_jpeg()
        except ImportError:
            self.skipTest("Qt fixture encoder unavailable")
        for cuts in ((1024,), (1024, 2560)):
            with self.subTest(cuts=cuts):
                placements, bitmap, last, physical = [], bytearray(b"\x01" + bytes(7)), 0, 512
                for end in (*cuts, len(data)):
                    placements.append((physical, data[last:end]))
                    physical += end - last
                    if end != len(data):
                        bitmap[physical // 512 // 8] |= 1 << ((physical // 512) % 8)
                        physical += 512
                    last = end
                image = make_image(self.root / (str(len(cuts)) + ".img"), placements)
                backend = BitmapBackend(bytes(bitmap))
                rows, _ = jpeg.scan_jpeg(image, 3, 512, backend=backend, max_candidates=5)
                self.assertEqual(rows, [])
                rows, report = jpeg.scan_jpeg(image, 3, 512, backend=backend, max_candidates=5, reassemble=True)
                self.assertEqual(report["reconstructed_count"], 1)
                self.assertEqual(rows[0]["carving"]["sha256"], hashlib.sha256(data).hexdigest())
                output = io.BytesIO()
                jpeg.extract_jpeg(image, 3, 512, rows[0], output, backend=backend)
                self.assertEqual(output.getvalue(), data)

    def test_allocated_and_truncated_images_do_not_become_candidates(self):
        data = minimal_jpeg()
        image = make_image(self.root / "image.img", [(512, data)])
        rows, _ = jpeg.scan_jpeg(image, 3, 512, backend=BitmapBackend(b"\x03" + bytes(7)), max_candidates=1)
        self.assertEqual(rows, [])
        image = make_image(image, [(512, data[:-2])])
        rows, _ = jpeg.scan_jpeg(image, 3, 512, backend=BitmapBackend(), max_candidates=1)
        self.assertEqual(rows, [])

    def test_two_different_valid_fragment_tails_are_ambiguous(self):
        # DC category 1 allows two different, equally plausible coefficient streams.
        original = minimal_jpeg().replace(bytes([0, 1]) + bytes(15) + b"\0",
                                          bytes([0, 1]) + bytes(15) + b"\x01", 1)
        prefix = original[:-3]
        prefix = prefix[:2] + segment(0xe1, bytes(512 - len(prefix) - 4)) + prefix[2:]
        self.assertEqual(len(prefix), 512)
        first, second = prefix + b"\x03\xff\xd9", prefix + b"\x4b\xff\xd9"
        self.assertNotEqual(jpeg.inspect_jpeg(first)["sha256"], jpeg.inspect_jpeg(second)["sha256"])
        image = make_image(self.root / "ambiguous.img", [(512, prefix), (1536, first[512:]), (2560, second[512:])])
        rows, result = jpeg.scan_jpeg(image, 3, 512, backend=BitmapBackend(b"\x15" + bytes(7)),
                                      max_candidates=5, reassemble=True)
        self.assertEqual(rows, [])
        self.assertEqual(result["ambiguous_headers"], 1)
