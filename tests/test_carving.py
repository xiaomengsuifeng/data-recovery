import contextlib
import copy
import hashlib
import io
from pathlib import Path
import random
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from recovery_core import carving
from recovery_core.cli import main
from recovery_core.common import RecoveryError, read_json, write_json
from recovery_core.control import OperationCancelled, TaskControl, task_scope
from recovery_core.preview import preview
from recovery_core.service import _validate_candidates, recover, scan
from recovery_core.verification import verify
from recovery_core.windows import VolumeSource


def chunk(kind, data=b""):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(*, width=4, height=3, depth=8, colour=2, payload=None):
    header = struct.pack(">IIBBBBB", width, height, depth, colour, 0, 0, 0)
    raw = b"\0\x20\x80\xc0\xff\x00\x10\x30\x90\xee\x01\x02\x03" * height
    return carving.SIGNATURE + chunk(b"IHDR", header) + chunk(b"IDAT", payload or zlib.compress(raw)) + chunk(b"IEND")


class BitmapBackend:
    versions = {"fls": "carving-test", "icat": "carving-test"}

    def __init__(self, bitmap=b"\x01" + bytes(7)):
        self.bitmap = bitmap
        self.bitmap_reads = 0

    def scan(self, *args):
        return [], []

    def extract_bitmap(self, image, offset, sector_size, output, limit):
        self.bitmap_reads += 1
        output.write(self.bitmap)

    def extract(self, *args):
        raise AssertionError("Carved bytes must come from the image, not a reused MFT record")


def make_image(path, placements=(), *, offset=3, sectors=64, cluster_sectors=1):
    data = bytearray((offset + sectors + 1) * 512)
    start = offset * 512
    data[start + 3:start + 11] = b"NTFS    "
    data[start + 11:start + 13] = struct.pack("<H", 512)
    data[start + 13] = cluster_sectors
    data[start + 40:start + 48] = struct.pack("<Q", sectors)
    data[start + 510:start + 512] = b"\x55\xaa"
    for relative, payload in placements:
        data[start + relative:start + relative + len(payload)] = payload
    path.write_bytes(data)
    return path


class PngStructureTests(unittest.TestCase):
    def inspect(self, data):
        return carving.inspect_png(io.BytesIO(data), 0, len(data))

    def test_exact_end_crc_and_digest_ignore_trailing_bytes(self):
        data = png()
        self.assertEqual(self.inspect(data + b"unrelated trailing bytes"),
                         {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()})

    def test_palette_multiple_idat_and_ancillary_chunks(self):
        data = (carving.SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 1, 3, 0, 0, 1))
                + chunk(b"PLTE", b"\0\0\0\xff\xff\xff") + chunk(b"IDAT")
                + chunk(b"IDAT", zlib.compress(b"\0\0")) + chunk(b"tEXt", b"key\0value") + chunk(b"IEND"))
        self.assertEqual(self.inspect(data)["size"], len(data))

    def test_empty_bad_signature_truncation_and_crc_fail(self):
        data = png()
        for bad in (b"", data[1:], data[:-1], data[:-12], data[:45] + bytes([data[45] ^ 1]) + data[46:]):
            with self.subTest(data=bad[:8]), self.assertRaises(carving.InvalidPng):
                self.inspect(bad)

    def test_invalid_ihdr_fields_and_lengths_fail(self):
        for data in (png(width=0), png(height=0), png(width=0x80000000), png(depth=3), png(colour=1),
                     carving.SIGNATURE + chunk(b"IHDR", bytes(12)) + chunk(b"IEND")):
            with self.subTest(data=data[8:33]), self.assertRaises(carving.InvalidPng):
                self.inspect(data)

    def test_invalid_order_critical_chunks_palette_and_animation_fail(self):
        header = png()[:33]
        idat = chunk(b"IDAT", zlib.compress(b"\0\0"))
        for middle in (chunk(b"IHDR", bytes(13)), chunk(b"ABCD"), chunk(b"abct"), chunk(b"acTL", bytes(8)),
                       chunk(b"PLTE", bytes(4)), idat + chunk(b"PLTE", bytes(3)),
                       idat + chunk(b"tEXt", b"a\0b") + idat, b"", chunk(b"IDAT")):
            with self.subTest(middle=middle[:12]), self.assertRaises(carving.InvalidPng):
                self.inspect(header + middle + chunk(b"IEND"))
        with self.assertRaises(carving.InvalidPng):
            self.inspect(png(colour=3))  # Indexed image has no palette.

    def test_chunk_length_cannot_read_beyond_source(self):
        for size in (0x7fffffff, 0xffffffff):
            with self.assertRaises(carving.InvalidPng):
                self.inspect(png()[:33] + struct.pack(">I4s", size, b"IDAT") + bytes(12))

    def test_byte_chunk_and_global_validation_limits(self):
        data = png()
        with patch.object(carving, "MAX_PNG_BYTES", len(data)):
            self.assertEqual(self.inspect(data)["size"], len(data))
        with patch.object(carving, "MAX_PNG_BYTES", len(data) - 1), self.assertRaises(carving.InvalidPng):
            self.inspect(data)
        with patch.object(carving, "MAX_CHUNKS", 2), self.assertRaises(carving.InvalidPng):
            self.inspect(data)
        with self.assertRaisesRegex(RecoveryError, "budget"):
            carving.inspect_png(io.BytesIO(data), 0, len(data), budget=[20])


class CarvingPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.payload = png()
        self.image = make_image(self.root / "test.img", [(1021, self.payload)])
        self.backend = BitmapBackend()
        self.session = self.root / "session"

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, **kwargs):
        return scan(self.image, self.session, backend=self.backend, offset=3, deep_png=True, **kwargs)

    def test_scan_preview_export_reopen_and_independent_content_only_verification(self):
        original_image = self.image.read_bytes()
        with patch.object(carving, "SCAN_BLOCK", 512):
            report = self.start()
        item, = report["candidates"]
        self.assertEqual(item["carving"]["image_offset"], 3 * 512 + 1021)
        self.assertIsNone(item["original_path"])
        self.assertEqual(report["carving"]["candidate_count"], 1)
        self.assertEqual(report["carving"]["scanned_bytes"], 63 * 512)
        self.assertEqual(preview(self.session, item["id"], self.backend)["data"], self.payload)
        _validate_candidates(read_json(self.session / "session.json")["candidates"])
        destination = self.root / "recovered"
        result = recover(self.session, destination, backend=self.backend)
        saved, = result["results"]
        self.assertEqual(saved["status"], "exported_unverified")
        self.assertEqual((destination / saved["saved_path"]).read_bytes(), self.payload)
        self.assertEqual(result["source_verification"], "unchanged")
        self.assertEqual(self.image.read_bytes(), original_image)
        reference = self.root / "reference.json"
        write_json(reference, {"schema_version": 1, "files": [{"original_path": "/lost/real-name.png",
                   "size": len(self.payload), "sha256": hashlib.sha256(self.payload).hexdigest()}]})
        verified = verify(destination, reference)
        self.assertEqual(verified["exact_content_matches"], 1)
        self.assertEqual(verified["correct_paths"], 0)
        self.assertEqual(verified["correct_filenames"], 0)

    def test_opt_in_only_and_live_volume_refused_before_access(self):
        report = scan(self.image, self.session, backend=self.backend, offset=3)
        self.assertEqual(report["candidates"], [])
        self.assertEqual(self.backend.bitmap_reads, 0)
        with patch("recovery_core.service.volume_identity") as identity, self.assertRaises(RecoveryError):
            scan(VolumeSource("Z:"), self.root / "live", backend=self.backend, deep_png=True)
        identity.assert_not_called()
        self.assertFalse((self.root / "live").exists())

    def test_empty_and_allocated_images_do_not_yield_candidates(self):
        self.backend.bitmap = bytes([255]) * 8
        self.assertEqual(self.start()["candidates"], [])
        self.session = self.root / "empty"
        make_image(self.image)
        self.backend.bitmap = b"\x01" + bytes(7)
        self.assertEqual(self.start()["candidates"], [])

    def test_allocated_cluster_cannot_be_joined_or_read_as_a_png(self):
        large = png(payload=zlib.compress(random.Random(17).randbytes(900)))
        make_image(self.image, [(2 * 512, large)])
        self.backend.bitmap = bytes([1 | (1 << 3)]) + bytes(7)
        self.assertEqual(self.start()["candidates"], [])

    def test_scan_stays_inside_partition_and_discards_partial_tail_cluster(self):
        make_image(self.image, [(64 * 512, self.payload)])
        self.assertEqual(self.start()["candidates"], [])
        self.session = self.root / "tail"
        make_image(self.image, [(62 * 512, self.payload)], sectors=63, cluster_sectors=4)
        self.backend.bitmap = b"\x01\x00" + bytes(6)
        self.assertEqual(self.start()["candidates"], [])

    def test_invalid_and_oversized_geometry_and_bitmap_fail_closed(self):
        for sectors, clusters in ((0, 1), (64, 3), (64, 0)):
            make_image(self.image, sectors=sectors, cluster_sectors=clusters)
            with self.subTest(sectors=sectors, clusters=clusters), self.assertRaises(RecoveryError):
                carving.geometry(self.image, 3, 512)
        make_image(self.image)
        self.image.write_bytes(self.image.read_bytes()[:-1024])
        with self.assertRaises(RecoveryError):
            carving.geometry(self.image, 3, 512)
        make_image(self.image)
        volume = carving.geometry(self.image, 3, 512)
        for bitmap in (bytes(7), bytes(9)):
            self.backend.bitmap = bitmap
            with self.assertRaises(RecoveryError):
                carving.allocation_bitmap(self.image, 3, 512, volume, self.backend)
        with patch.object(carving, "MAX_BITMAP_BYTES", 1), self.assertRaises(RecoveryError):
            carving.allocation_bitmap(self.image, 3, 512, volume, self.backend)

    def test_free_runs_match_cluster_bits_including_last_partial_byte(self):
        rng = random.Random(77)
        for count in (1, 7, 8, 9, 15, 16, 33, 64):
            volume = carving.Geometry(4096, 4096, count)
            for _ in range(10):
                bitmap = rng.randbytes((count + 7) // 8)
                runs = list(carving.free_runs(bitmap, volume))
                expected = {i for i in range(count) if not bitmap[i // 8] & (1 << (i % 8))}
                actual = set()
                for start, end in runs:
                    self.assertTrue(volume.start <= start < end <= volume.end)
                    actual.update(range((start - volume.start) // 4096, (end - volume.start) // 4096))
                self.assertEqual(actual, expected)

    def test_four_kib_sector_geometry_preserves_absolute_byte_offset(self):
        data = bytearray(4096 * 12)
        data[4096 + 3:4096 + 11] = b"NTFS    "
        data[4096 + 11:4096 + 13] = struct.pack("<H", 4096)
        data[4096 + 13] = 2
        data[4096 + 40:4096 + 48] = struct.pack("<Q", 8)
        data[4096 + 510:4096 + 512] = b"\x55\xaa"
        start = 4096 + 8192 + 19
        data[start:start + len(self.payload)] = self.payload
        self.image.write_bytes(data)
        report = scan(self.image, self.session, backend=self.backend, offset=1, sector_size=4096, deep_png=True)
        self.assertEqual(report["candidates"][0]["carving"]["image_offset"], start)
        self.assertEqual(preview(self.session, report["candidates"][0]["id"], self.backend)["data"], self.payload)

    def test_false_signature_does_not_hide_later_valid_png(self):
        make_image(self.image, [(512, carving.SIGNATURE + bytes(30)), (1021, self.payload)])
        report = self.start()
        self.assertEqual(report["carving"]["signature_checks"], 2)
        self.assertEqual(len(report["candidates"]), 1)

    def test_distinct_offsets_with_same_content_remain_distinct_unknown_names(self):
        make_image(self.image, [(1021, self.payload), (4096, self.payload)])
        report = self.start()
        self.assertEqual(len({item["id"] for item in report["candidates"]}), 2)
        self.assertTrue(all(item["original_path"] is None for item in report["candidates"]))

    def test_candidate_and_signature_limits_leave_no_completed_session(self):
        make_image(self.image, [(1021, self.payload), (4096, self.payload)])
        with self.assertRaisesRegex(RecoveryError, "max-candidates"):
            self.start(max_candidates=1)
        self.assertFalse((self.session / "session.json").exists())
        self.session = self.root / "attempts"
        with patch.object(carving, "MAX_SIGNATURE_CHECKS", 1), self.assertRaisesRegex(RecoveryError, "signature count"):
            self.start()
        self.assertFalse((self.session / "session.json").exists())

    def test_cancel_during_content_scan_does_not_publish_session(self):
        control = TaskControl()
        control.progress = lambda event: control.cancelled.set() if event["phase"] == "carving" else None
        with task_scope(control), self.assertRaises(OperationCancelled):
            self.start()
        self.assertFalse((self.session / "session.json").exists())

    def test_source_change_blocks_preview_and_export(self):
        report = self.start()
        with self.image.open("ab") as output:
            output.write(b"changed")
        with self.assertRaises(RecoveryError):
            preview(self.session, report["candidates"][0]["id"], self.backend)
        with self.assertRaises(RecoveryError):
            recover(self.session, self.root / "export", backend=self.backend)
        self.assertFalse((self.root / "export").exists())

    def test_changed_allocation_or_content_is_rejected_when_reopening(self):
        item, = self.start()["candidates"]
        self.backend.bitmap = bytes([255]) * 8
        with self.assertRaisesRegex(RecoveryError, "unallocated"):
            preview(self.session, item["id"], self.backend)
        self.backend.bitmap = b"\x01" + bytes(7)
        item["carving"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(RecoveryError, "differs"), io.BytesIO() as output:
            carving.extract_png(self.image, 3, 512, item, output, backend=self.backend)
        item["carving"]["image_offset"] = 1000000
        item["observed_path"] = carving.observed_path(1000000)
        with self.assertRaisesRegex(RecoveryError, "unallocated"), io.BytesIO() as output:
            carving.extract_png(self.image, 3, 512, item, output, backend=self.backend)

    def test_forged_names_methods_sizes_and_offsets_are_rejected(self):
        item, = self.start()["candidates"]
        for change in ({"original_path": "/claimed/real.png"}, {"observed_path": "/../../foo.png"},
                       {"recovery_method": "anything"}, {"recovery_method": "ntfs_metadata", "inode": "42-128-1"},
                       {"size": True}, {"size": 1 << 63}, {"kind": "recycle_metadata"}, {"inode": "42-128-1"}):
            with self.subTest(change=change), self.assertRaises(RecoveryError):
                _validate_candidates([dict(item, **change)])
        for value in (True, -1, "5", 1 << 63):
            altered = copy.deepcopy(item)
            altered["carving"]["image_offset"] = value
            with self.subTest(value=value), self.assertRaises(RecoveryError):
                _validate_candidates([altered])

    def test_cancel_during_carved_export_preserves_cancelled_report(self):
        self.start()
        control = TaskControl()
        control.progress = lambda event: control.cancelled.set() if event["phase"] == "export" else None
        with task_scope(control):
            report = recover(self.session, self.root / "export", backend=self.backend)
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual(report["exported_unverified_count"], 0)
        self.assertEqual(read_json(self.root / "export/recovery.json")["status"], "cancelled")

    def test_copy_detects_bytes_changed_after_structure_check(self):
        # Mutate a later copy block only after parsing completed; the returned
        # bytes must not silently inherit the scan's validity claim.
        make_image(self.image, [(1024, png(payload=zlib.compress(random.Random(19).randbytes(18000))))])
        item, = self.start()["candidates"]
        image = self.image
        start = item["carving"]["image_offset"]

        class MutatingOutput(io.BytesIO):
            def write(self, data):
                result = super().write(data)
                if self.tell() == 8192:
                    with image.open("r+b") as source:
                        source.seek(start + 16400)
                        value = source.read(1)[0]
                        source.seek(start + 16400)
                        source.write(bytes([value ^ 1]))
                return result

        with patch.object(carving, "READ_BLOCK", 8192), MutatingOutput() as output:
            with self.assertRaisesRegex(RecoveryError, "changed during extraction"):
                carving.extract_png(self.image, 3, 512, item, output, backend=self.backend)

    def test_cli_deep_png_reaches_scanner(self):
        with patch("recovery_core.cli.Tsk", return_value=self.backend), contextlib.redirect_stdout(io.StringIO()):
            status = main(["scan", str(self.image), "--output", str(self.session), "--offset", "3", "--deep-png"])
        self.assertEqual(status, 0)
        self.assertTrue(read_json(self.session / "session.json")["scan_options"]["deep_png"])


if __name__ == "__main__":
    unittest.main()
