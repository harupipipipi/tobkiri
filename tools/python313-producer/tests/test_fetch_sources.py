"""Download framing tests use memory streams; no network or upstream execution."""
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_sources


class SourceDownloadTests(unittest.TestCase):
    """The source archive identity and original size bound are mandatory."""

    def test_complete_bytes_match_recorded_digest(self) -> None:
        content = b"source archive" * 10000
        output = io.BytesIO()
        size = fetch_sources.capture(io.BytesIO(content), output,
                                     hashlib.sha256(content).hexdigest())
        self.assertEqual(size, len(content))
        self.assertEqual(output.getvalue(), content)

    def test_changed_truncated_and_empty_downloads_fail(self) -> None:
        content = b"source archive"
        expected = hashlib.sha256(content).hexdigest()
        for invalid in (b"", content[:-1], content + b"extra", b"changed input"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                fetch_sources.capture(io.BytesIO(invalid), io.BytesIO(), expected)

    def test_exact_size_limit_accepts_and_one_extra_byte_fails(self) -> None:
        with patch.object(fetch_sources, "MAX_ARCHIVE", 8):
            for content in (b"12345678", b"123456789"):
                expected = hashlib.sha256(content).hexdigest()
                if len(content) == 8:
                    self.assertEqual(fetch_sources.capture(
                        io.BytesIO(content), io.BytesIO(), expected), 8)
                else:
                    with self.assertRaisesRegex(ValueError, "size bound"):
                        fetch_sources.capture(io.BytesIO(content), io.BytesIO(), expected)

    def test_existing_destination_prevents_any_network_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            marker = root / "keep"
            marker.write_bytes(b"unchanged")
            with patch.object(fetch_sources, "urlopen") as opened:
                with self.assertRaises(FileExistsError):
                    fetch_sources.fetch(root)
            opened.assert_not_called()
            self.assertEqual(marker.read_bytes(), b"unchanged")


if __name__ == "__main__":
    unittest.main()
