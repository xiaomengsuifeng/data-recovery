import dataclasses
import struct
import unittest

from recovery_core.recycle_bin import InvalidRecycleMetadata, RecycleMetadata, pair_key, parse_dollar_i


SID = "S-1-5-21-111-222-333-1001"
PATH = "C:\\Users\\测试\\Documents\\预算.xlsx"
FILETIME = 11644473600 * 10_000_000 + 1_234_567  # Unix epoch + 0.1234567 s.


def record(version=2, path=PATH, size=1234, ticks=FILETIME):
    encoded = (path + "\0").encode("utf-16-le")
    header = struct.pack("<QQQ", version, size, ticks)
    if version == 1:
        if len(encoded) > 520:
            raise ValueError("Test path exceeds version 1 capacity")
        return header + encoded.ljust(520, b"\0")
    return header + struct.pack("<I", len(encoded) // 2) + encoded


class ParseDollarITests(unittest.TestCase):
    def test_both_versions_parse_chinese_and_preserve_timestamp_precision(self):
        for version in (1, 2):
            with self.subTest(version=version):
                self.assertEqual(
                    parse_dollar_i(record(version)),
                    RecycleMetadata(PATH, 1234, "1970-01-01T00:00:00.1234567Z", version),
                )

    def test_unsigned_size_is_preserved_without_allocation(self):
        for size in (0, 1 << 63, (1 << 64) - 1):
            self.assertEqual(parse_dollar_i(record(size=size)).original_size, size)

    def test_non_bmp_path_counts_utf16_units(self):
        path = "D:\\照片\\旅行😀.jpg"
        self.assertEqual(parse_dollar_i(record(path=path)).original_path, path)

    def test_supported_path_length_boundaries(self):
        for version, length in ((1, 259), (2, 32767)):
            path = "C:\\" + "a" * (length - 3)
            self.assertEqual(parse_dollar_i(record(version, path)).original_path, path)
        with self.assertRaises(InvalidRecycleMetadata):
            parse_dollar_i(record(path="C:\\" + "a" * 32765))

    def test_unc_item_path_is_metadata_only(self):
        path = "\\\\server\\share\\用户\\file.txt"
        self.assertEqual(parse_dollar_i(record(path=path)).original_path, path)

    def test_metadata_is_immutable(self):
        item = parse_dollar_i(record())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            item.original_path = "D:\\changed.txt"

    def test_all_truncated_prefixes_are_rejected(self):
        for version in (1, 2):
            data = record(version)
            for length in range(len(data)):
                with self.subTest(version=version, length=length):
                    with self.assertRaises(InvalidRecycleMetadata):
                        parse_dollar_i(data[:length])

    def test_unknown_version_is_rejected(self):
        for version in (0, 3, (1 << 64) - 1):
            with self.assertRaises(InvalidRecycleMetadata):
                parse_dollar_i(struct.pack("<Q", version) + record()[8:])

    def test_invalid_declared_lengths_and_trailing_bytes(self):
        data = record()
        for length in (0, 1, 32769, 0xFFFFFFFF, (len(data) - 28) // 2 - 1):
            with self.subTest(length=length):
                with self.assertRaises(InvalidRecycleMetadata):
                    parse_dollar_i(data[:24] + struct.pack("<I", length) + data[28:])
        for version in (1, 2):
            with self.assertRaises(InvalidRecycleMetadata):
                parse_dollar_i(record(version) + b"\0\0")

    def test_missing_terminator_and_nonzero_padding(self):
        for version in (1, 2):
            data = record(version)
            with self.subTest(version=version):
                with self.assertRaises(InvalidRecycleMetadata):
                    parse_dollar_i(data[:-2] + b"A\0")
        with self.assertRaises(InvalidRecycleMetadata):
            parse_dollar_i(record(path="C:\\ok.txt\0hidden"))
        with self.assertRaises(InvalidRecycleMetadata):
            parse_dollar_i(record(path="C:\\ok.txt\0"))

    def test_invalid_utf16_surrogate_is_rejected(self):
        data = record()
        with self.assertRaises(InvalidRecycleMetadata):
            parse_dollar_i(data[:28] + b"\0\xd8" + data[30:])

    def test_unrepresentable_filetime_is_rejected(self):
        with self.assertRaises(InvalidRecycleMetadata):
            parse_dollar_i(record(ticks=(1 << 64) - 1))
        self.assertEqual(parse_dollar_i(record(ticks=0)).deleted_at, "1601-01-01T00:00:00.0000000Z")

    def test_ambiguous_original_paths_are_rejected(self):
        for path in ("", "notes.txt", "C:notes.txt", "C:\\", "C:\\a\\..\\b", "C:\\a\n.txt", "C:\\a.txt:stream", "\\\\?\\C:\\a.txt"):
            with self.subTest(path=path):
                with self.assertRaises(InvalidRecycleMetadata):
                    parse_dollar_i(record(path=path))

    def test_requires_bytes(self):
        for value in (None, "text", bytearray(record())):
            with self.assertRaises(TypeError):
                parse_dollar_i(value)


class PairKeyTests(unittest.TestCase):
    def test_root_relative_tsk_paths_pair_with_mixed_ascii_case(self):
        first = pair_key(f"/$Recycle.Bin/{SID}/$IABC123.TXT")
        second = pair_key(f"$recycle.bin/{SID.lower()}/$rabc123.txt")
        self.assertEqual(first, (f"/$recycle.bin/{SID.lower()}", "abc123.txt"))
        self.assertEqual(first, second)

    def test_windows_separators_and_volume_identity(self):
        first = pair_key(f"C:\\$Recycle.Bin\\{SID}\\$IABC123.txt")
        self.assertEqual(first, pair_key(f"c:/$recycle.bin/{SID}/$Rabc123.TXT"))
        self.assertNotEqual(first, pair_key(f"D:/$Recycle.Bin/{SID}/$RABC123.txt"))
        self.assertNotEqual(first, pair_key(f"/$Recycle.Bin/{SID}/$RABC123.txt"))

    def test_different_sid_suffix_and_extension_do_not_pair(self):
        first = pair_key(f"/$Recycle.Bin/{SID}/$IABC123.txt")
        for other in (f"/{SID}/$RABC124.txt", f"/{SID}/$RABC123.pdf", "/S-1-5-21-111-222-333-1002/$RABC123.txt"):
            with self.subTest(other=other):
                self.assertNotEqual(first, pair_key("/$Recycle.Bin" + other))

    def test_directory_and_chinese_extension_keys(self):
        for suffix in ("ABC123", "ABC123.文档"):
            first = pair_key(f"/$Recycle.Bin/{SID}/$I{suffix}")
            self.assertIsNotNone(first)
            self.assertEqual(first, pair_key(f"/$Recycle.Bin/{SID}/$R{suffix}"))

    def test_unicode_casefold_does_not_create_false_equivalence(self):
        self.assertNotEqual(pair_key(f"/$Recycle.Bin/{SID}/$IABC123.ß"), pair_key(f"/$Recycle.Bin/{SID}/$RABC123.ss"))

    def test_malformed_and_disguised_paths_are_not_keys(self):
        base = f"$Recycle.Bin/{SID}"
        paths = (
            f"/Users/name/{base}/$IABC123.txt",
            f"/{base}/folder/$IABC123.txt",
            f"/{base}/$RABC123/$IABC123.txt",
            f"/{base}/../{SID}/$IABC123.txt",
            f"//server/share/{base}/$IABC123.txt",
            f"\\\\?\\C:\\{base}\\$IABC123.txt",
            f"C:{base}/$IABC123.txt",
            f"/{base}//$IABC123.txt",
            f"/{base}/$IABC123.txt:stream",
            f"/{base}/$IABC123.txt/",
            f"/{base}/$IABC123.txt.",
            f"/{base}/$IABC123.txt ",
            f"/{base}/$IABC123.\x00txt",
            f"/{base}/$QABC123.txt",
            f"/{base}/$Ishort.txt",
            f"/{base}/$I1234567.txt",
            "/$Recycle.Bin/not-a-sid/$IABC123.txt",
            "/$Recycle.Bin/S-2-5-18/$IABC123.txt",
            "/$Recycle.Bin/S-1-281474976710656-18/$IABC123.txt",
            "/$Recycle.Bin/S-1-5-4294967296/$IABC123.txt",
            "/$Recycle.Bin/S-1-5-" + "9" * 5000 + "/$IABC123.txt",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertIsNone(pair_key(path))
        for path in (None, b"path", ""):
            self.assertIsNone(pair_key(path))


if __name__ == "__main__":
    unittest.main()
