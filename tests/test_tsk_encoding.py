from pathlib import Path
import unittest
from unittest.mock import patch

from recovery_core.common import RecoveryError
from recovery_core.tsk import Tsk, parse_body
from test_core import body


class TskEncodingTests(unittest.TestCase):
    def listing(self, raw, encoding):
        backend = Tsk.__new__(Tsk)
        backend.executables = {"fls": "unused-fls"}
        backend.timeout = 1

        def output_bytes(command, output, **kwargs):
            output.write(raw)

        with patch("recovery_core.tsk.run_bounded", side_effect=output_bytes), \
                patch("recovery_core.tsk.identify", return_value="ntfs"), \
                patch("recovery_core.tsk._output_encoding", return_value=encoding):
            return backend._listing(Path("fixture.img"), 0, 512)

    def test_windows_code_page_preserves_chinese_even_when_bytes_are_valid_utf8(self):
        for name in ("/中文.txt", "/漏.txt"):
            with self.subTest(name=name):
                text = self.listing(body(name).encode("cp936"), "cp936")
                candidates, _ = parse_body(text)
                self.assertEqual(candidates[0]["original_path"], name)

    def test_utf8_output_preserves_multilingual_names(self):
        name = "/中文-é-🚀.txt"
        text = self.listing(body(name).encode("utf-8"), "utf-8")
        self.assertEqual(parse_body(text)[0][0]["original_path"], name)

    def test_invalid_bytes_are_rejected_without_replacement_names(self):
        for encoding, raw in (("cp936", b"\x81"), ("utf-8", b"\xff")):
            with self.subTest(encoding=encoding), self.assertRaises(RecoveryError):
                self.listing(raw, encoding)


if __name__ == "__main__":
    unittest.main()
