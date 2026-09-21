import copy
import hashlib
import io
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from recovery_core import jpeg
from recovery_core.common import RecoveryError
from recovery_core.control import OperationCancelled, TaskControl, task_scope
from test_carving import BitmapBackend, make_image
from test_jpeg import minimal_jpeg, segment


def replace_segment(data, marker, payload):
    start = data.index(bytes((255, marker)))
    end = start + 2 + int.from_bytes(data[start + 2:start + 4], "big")
    return data[:start] + (segment(marker, payload) if payload is not None else b"") + data[end:]


class JpegBoundaryTests(unittest.TestCase):
    def test_every_truncation_of_known_good_jpeg_fails_cleanly(self):
        data = minimal_jpeg(restart=True)
        for size in range(len(data)):
            with self.subTest(size=size), self.assertRaises(jpeg.InvalidJpeg):
                jpeg.inspect_jpeg(data[:size])

    def test_standalone_markers_prefixes_and_segment_lengths(self):
        for content in (b"x", b"\xff", b"\xff\xd9", b"\xff\xe1", b"\xff\xe1\0", b"\xff\xe1\0\1",
                        *(bytes((255, n)) for n in (0, 1, 0xd0, 0xd7, 0xd8))):
            with self.subTest(content=content), self.assertRaises(jpeg.InvalidJpeg):
                jpeg.inspect_jpeg(b"\xff\xd8" + content)

    def test_application_and_comment_data_do_not_set_file_boundary(self):
        original = minimal_jpeg()
        data = original[:2] + segment(0xe1, b"fake\xff\xd9\xff\xd8") + segment(0xfe, b"comment") + original[2:]
        result = jpeg.inspect_jpeg(data + b"unrelated trailer")
        self.assertEqual(result["size"], len(data))
        self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())

    def test_frame_precision_sampling_component_and_dimension_matrix(self):
        data = minimal_jpeg()
        good = b"\x08\0\x08\0\x10\x01\x01\x11\0"
        cases = [b"", bytes((12,)) + good[1:], good[:1] + bytes(4) + good[5:],
                 good[:5] + b"\x02" + good[6:], good[:7] + b"\x01\0", good[:7] + b"\x51\0",
                 good[:-1] + b"\x04", good[:5] + b"\x03" + b"\x01\x11\0" * 3,
                 good[:5] + b"\x03" + b"\x01\x44\0\x02\x11\0\x03\x11\0"]
        for index, payload in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(jpeg.InvalidJpeg):
                jpeg.inspect_jpeg(replace_segment(data, 0xc0, payload))
        with self.assertRaises(jpeg.InvalidJpeg):
            jpeg.inspect_jpeg(data[:2] + segment(0xc0, good) + data[2:])

    def test_quantizer_missing_zero_truncated_and_unknown_ids(self):
        for payload in (None, b"\0" + bytes(64), b"\0" + b"\1" * 63,
                        b"\x20" + b"\1" * 192, b"\x04" + b"\1" * 64):
            with self.subTest(payload=payload and payload[:1]), self.assertRaises(jpeg.InvalidJpeg):
                jpeg.inspect_jpeg(replace_segment(minimal_jpeg(), 0xdb, payload))

    def test_huffman_empty_oversubscribed_unknown_and_truncated_tables(self):
        for payload in (None, b"\0", bytes(17), b"\0\2" + bytes(15) + b"\0\1",
                        b"\x20\1" + bytes(15) + b"\0", b"\0\xff\xff" + bytes(14)):
            with self.subTest(payload=payload and payload[:2]), self.assertRaises(jpeg.InvalidJpeg):
                jpeg.inspect_jpeg(replace_segment(minimal_jpeg(), 0xc4, payload))

    def test_invalid_scan_selectors_spectral_range_and_missing_frame(self):
        for payload in (b"", b"\0\0\x3f\0", b"\1\2\0\0\x3f\0",
                        b"\1\1\x40\0\x3f\0", b"\1\1\0\1\x3f\0", b"\1\1\0\0\x3f\1"):
            with self.subTest(payload=payload), self.assertRaises(jpeg.InvalidJpeg):
                jpeg.inspect_jpeg(replace_segment(minimal_jpeg(), 0xda, payload))
        with self.assertRaises(jpeg.InvalidJpeg):
            jpeg.inspect_jpeg(replace_segment(minimal_jpeg(), 0xc0, None))

    def test_empty_entropy_invalid_restart_and_parser_cancellation(self):
        for payload in (b"", b"\0", b"\0\0\0"):
            with self.subTest(payload=payload), self.assertRaises(jpeg.InvalidJpeg):
                jpeg.inspect_jpeg(replace_segment(minimal_jpeg(restart=True), 0xdd, payload))
        with self.assertRaises(jpeg.InvalidJpeg):
            jpeg.inspect_jpeg(minimal_jpeg()[:-3] + b"\xff\xd9")
        control = TaskControl()
        control.cancelled.set()
        with task_scope(control), self.assertRaises(OperationCancelled):
            jpeg.inspect_jpeg(minimal_jpeg())

    def test_seeded_mutations_never_crash_or_invent_bytes(self):
        # Fixed seed makes failures reproducible. Valid mutations are allowed:
        # a successful descriptor must still describe exactly the input prefix.
        random_source = random.Random(20260921)
        original = minimal_jpeg(restart=True)
        for index in range(512):
            data = bytearray(original)
            for _ in range(random_source.randrange(1, 5)):
                data[random_source.randrange(len(data))] = random_source.randrange(256)
            with self.subTest(mutation=index):
                try:
                    info = jpeg.inspect_jpeg(bytes(data))
                except jpeg.InvalidJpeg:
                    continue
                self.assertLessEqual(info["size"], len(data))
                self.assertEqual(info["sha256"], hashlib.sha256(data[:info["size"]]).hexdigest())


class JpegCandidateBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.payload = minimal_jpeg()
        self.image = make_image(self.root / "test.img", [(512, self.payload)])
        self.backend = BitmapBackend()
        rows, _ = jpeg.scan_jpeg(self.image, 3, 512, backend=self.backend, max_candidates=4)
        self.item, = rows

    def test_descriptor_types_names_hash_and_offsets_cannot_be_forged(self):
        changes = {"image_offset": -1, "extents": None, "format": "png", "allocation": "allocated",
                   "validation": "trusted", "sha256": "A" * 64, "reconstructed": 1}
        for key, value in changes.items():
            row = copy.deepcopy(self.item)
            row["carving"][key] = value
            with self.subTest(key=key), self.assertRaises(RecoveryError):
                jpeg.validate_candidate(row)
        for key, value in (("carving", None), ("inode", "1"), ("original_path", "/original.jpg"),
                           ("observed_path", "/forged.jpg"), ("size", True)):
            with self.subTest(key=key), self.assertRaises(RecoveryError):
                jpeg.validate_candidate(dict(self.item, **{key: value}))

    def test_extent_types_lengths_overflow_and_overlap_are_rejected(self):
        start = self.item["carving"]["image_offset"]
        size = self.item["size"]
        cases = [[], [None], [[start]], [[start, 0]], [[True, size]], [[start, size - 1]],
                 [[1 << 63, size]], [[start, size], [start, size]], [[start, size]] * 33]
        for index, extents in enumerate(cases):
            row = copy.deepcopy(self.item)
            row["carving"]["extents"] = extents
            with self.subTest(case=index), self.assertRaises(RecoveryError):
                jpeg.validate_candidate(row)

    def test_changed_allocation_and_digest_write_no_export_bytes(self):
        for backend, row in ((BitmapBackend(b"\x03" + bytes(7)), self.item),
                             (self.backend, copy.deepcopy(self.item))):
            if backend is self.backend:
                row["carving"]["sha256"] = "0" * 64
            destination = io.BytesIO()
            with self.assertRaises(RecoveryError):
                jpeg.extract_jpeg(self.image, 3, 512, row, destination, backend=backend)
            self.assertEqual(destination.getvalue(), b"")

    def test_candidate_signature_and_byte_budgets_fail_explicitly(self):
        with self.assertRaises(RecoveryError):
            jpeg.scan_jpeg(self.image, 3, 512, backend=self.backend, max_candidates=0)
        with patch.object(jpeg, "MAX_CHECKS", 0), self.assertRaises(RecoveryError):
            jpeg.scan_jpeg(self.image, 3, 512, backend=self.backend, max_candidates=4)
        saved = dict(candidates=[], cursor=3 * 512, checks=0, ambiguous=0, limited=0, budget=0)
        with patch.object(jpeg, "partial", return_value=saved), self.assertRaises(RecoveryError):
            jpeg.scan_jpeg(self.image, 3, 512, backend=self.backend, max_candidates=4)
