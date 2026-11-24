"""Generated artifact digests pin exact UTF-8 bytes on every platform."""
import hashlib

from ecosystem.tobkiri_workflow_pack.generate_v4 import (
    _matches_generated_bytes, _write_generated_bytes,
)


def test_workflow_generated_bytes_do_not_depend_on_text_newline_translation(tmp_path, monkeypatch):
    path = tmp_path / "backend-integrity.v4.json"
    content = '{\n  "label": "日本語"\n}\n'
    # Model the platform behavior which caused the real Windows failure.
    original = type(path).write_text
    def windows_text_write(self, value, *args, **kwargs):
        return original(self, value.replace("\n", "\r\n"), *args, **kwargs)
    monkeypatch.setattr(type(path), "write_text", windows_text_write)
    _write_generated_bytes(path, content)
    assert path.read_bytes() == content.encode("utf-8")
    assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(content.encode()).digest()
    assert _matches_generated_bytes(path, content)


def test_normalized_text_equality_does_not_hide_crlf_digest_drift(tmp_path):
    path = tmp_path / "frontend_contract_map.v4.json"
    content = '{\n  "version": 1\n}\n'
    path.write_bytes(content.replace("\n", "\r\n").encode())
    assert path.read_text(encoding="utf-8") == content
    assert not _matches_generated_bytes(path, content)
    _write_generated_bytes(path, content)
    assert _matches_generated_bytes(path, content)
