from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

DEFAULTSPACK_ROOT = Path(__file__).resolve().parent.parent / "ecosystem" / "defaultspack"
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.chat.attachment_security import (  # noqa: E402
    _attachment_fingerprint,
    attachment_requires_review,
    validate_attachment_security_reviews,
)


def _reviewed_attachment(name: str, content: str, *, status: str = "approved") -> dict:
    attachment = {
        "name": name,
        "content": content,
        "size": len(content),
        "type": "text/plain",
        "truncated": False,
    }
    attachment["securityReview"] = {
            "version": 1,
            "status": status,
            "fingerprint": _attachment_fingerprint(attachment),
            "scannedCharacters": len(content),
            "truncated": False,
            "findings": [],
    }
    return attachment


@pytest.mark.parametrize(
    ("name", "content"),
    [
        (".env", "ORDINARY=value"),
        ("settings.txt", "AWS_ACCESS_KEY_ID=AKIA1234567890ABCDEF"),
        ("notes.md", "Authorization: Bearer header-secret-value"),
        ("database.txt", "postgres://user:database-password@db.internal/app"),
        ("key.txt", "-----BEGIN PRIVATE KEY-----\nmaterial\n-----END PRIVATE KEY-----"),
        ("request.log", "Cookie: session=top-secret"),
    ],
)
def test_server_rescans_sensitive_names_and_content(name: str, content: str):
    assert attachment_requires_review({"name": name, "content": content}) is True
    with pytest.raises(ValueError, match="requires explicit review"):
        validate_attachment_security_reviews([{"name": name, "content": content}])


def test_server_accepts_explicit_review_bound_to_unchanged_content():
    attachment = _reviewed_attachment(".env", "API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456")
    validate_attachment_security_reviews([attachment])


def test_server_rejects_content_changed_after_review_without_echoing_secret():
    attachment = _reviewed_attachment(".env", "API_KEY=first-secret-value")
    attachment["content"] = "API_KEY=second-secret-value"
    with pytest.raises(ValueError, match="changed after security review") as error:
        validate_attachment_security_reviews([attachment])
    assert "first-secret-value" not in str(error.value)
    assert "second-secret-value" not in str(error.value)


@pytest.mark.parametrize("field", ["name", "size", "type", "source", "sourcePath"])
def test_server_review_fingerprint_binds_attachment_metadata(field: str):
    attachment = _reviewed_attachment(".env", "API_KEY=first-secret-value")
    attachment[field] = "changed"
    with pytest.raises(ValueError, match="changed after security review"):
        validate_attachment_security_reviews([attachment])


def test_server_review_fingerprint_binds_data_url_payload():
    attachment = {
        "name": "scan.png",
        "size": 4,
        "type": "image/png",
        "dataUrl": "data:image/png;base64,AAAA",
        "truncated": False,
    }
    attachment["securityReview"] = {
        "version": 1,
        "status": "approved",
        "fingerprint": _attachment_fingerprint(attachment),
        "scannedCharacters": 0,
        "truncated": False,
        "findings": [],
    }
    attachment["dataUrl"] = "data:image/png;base64,BBBB"
    with pytest.raises(ValueError, match="changed after security review"):
        validate_attachment_security_reviews([attachment])


def test_server_requires_explicit_review_for_unscanned_data_url_payload():
    attachment = {
        "name": "notes.txt",
        "size": 18,
        "type": "text/plain",
        "dataUrl": "data:text/plain;base64,c2VjcmV0LXZhbHVl",
        "truncated": False,
    }
    assert attachment_requires_review(attachment) is True
    with pytest.raises(ValueError, match="requires explicit review"):
        validate_attachment_security_reviews([attachment])

    attachment["securityReview"] = {
        "version": 1,
        "status": "approved",
        "fingerprint": _attachment_fingerprint(attachment),
        "scannedCharacters": 0,
        "truncated": False,
        "findings": [],
    }
    validate_attachment_security_reviews([attachment])


def test_server_requires_review_for_truncated_content_even_without_a_match():
    with pytest.raises(ValueError, match="requires explicit review"):
        validate_attachment_security_reviews(
            [{"name": "notes.txt", "content": "ordinary prefix", "truncated": True}]
        )


def test_server_allows_clear_ordinary_attachments_without_review_metadata():
    validate_attachment_security_reviews(
        [{"name": "notes.md", "content": "Meeting notes without credentials", "truncated": False}]
    )


def test_server_rejects_metadata_only_payload_smuggling():
    attachment = _reviewed_attachment(".env", "API_KEY=secret-value", status="metadata_only")
    with pytest.raises(ValueError, match="must not include file content"):
        validate_attachment_security_reviews([attachment])


def test_server_rejects_incomplete_or_truncated_redaction():
    attachment = _reviewed_attachment(".env", "API_KEY=secret-value", status="redacted")
    with pytest.raises(ValueError, match="still requires security review"):
        validate_attachment_security_reviews([attachment])

    truncated = _reviewed_attachment("notes.txt", "ordinary prefix", status="redacted")
    truncated["truncated"] = True
    truncated["securityReview"]["truncated"] = True
    truncated["securityReview"]["fingerprint"] = _attachment_fingerprint(truncated)
    with pytest.raises(ValueError, match="still requires security review"):
        validate_attachment_security_reviews([truncated])


def test_server_rejects_fabricated_scan_range_and_review_version():
    attachment = _reviewed_attachment(".env", "API_KEY=secret-value")
    attachment["securityReview"]["scannedCharacters"] = 0
    with pytest.raises(ValueError, match="range does not match"):
        validate_attachment_security_reviews([attachment])

    attachment = _reviewed_attachment(".env", "API_KEY=secret-value")
    attachment["securityReview"]["version"] = 2
    with pytest.raises(ValueError, match="version is unsupported"):
        validate_attachment_security_reviews([attachment])


def test_server_applies_custom_literal_policy_without_echoing_value():
    custom_value = "internal-customer-marker"
    attachment = {"name": "notes.txt", "content": f"Reference: {custom_value}"}
    with pytest.raises(ValueError, match="requires explicit review") as error:
        validate_attachment_security_reviews([attachment], [custom_value])
    assert custom_value not in str(error.value)


def test_server_loads_custom_literal_policy_from_settings(tmp_path, monkeypatch):
    custom_value = "workspace-sensitive-marker"
    settings = tmp_path / "frontend_settings.json"
    settings.write_text(
        '{"privacy_security":{"attachment_secret_patterns":"workspace-sensitive-marker"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH", str(settings))
    with pytest.raises(ValueError, match="requires explicit review"):
        validate_attachment_security_reviews(
            [{"name": "notes.txt", "content": f"Reference: {custom_value}"}]
        )


def test_server_fails_closed_when_security_policy_is_corrupt(tmp_path, monkeypatch):
    settings = tmp_path / "frontend_settings.json"
    settings.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH", str(settings))
    with pytest.raises(ValueError, match="policy could not be loaded") as error:
        validate_attachment_security_reviews([{"name": "notes.txt", "content": "ordinary"}])
    assert "not-json" not in str(error.value)


@pytest.mark.parametrize(
    "attachment",
    [
        {"name": ".npmrc", "content": "registry=https://registry.npmjs.org"},
        {"name": "archive.zip", "type": "text/plain", "content": "not really a zip"},
        {"name": "notes.txt", "type": "application/octet-stream", "content": "plain-looking"},
        {"name": "notes.txt", "type": "text/plain", "content": "prefix\u0000suffix"},
        {"name": "notes.txt", "content": "q7Vn2Lk9Pz4Xa8Mc1Re6Ty3Ui5Oo0WbH"},
    ],
)
def test_server_matches_hidden_mime_mismatch_and_entropy_policy(attachment: dict):
    assert attachment_requires_review(attachment) is True


def test_chat_request_revalidates_before_persisting_or_dispatching(monkeypatch):
    if sys.platform == "win32":
        monkeypatch.setitem(
            sys.modules,
            "fcntl",
            types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=8, flock=lambda *_args: None),
        )
    from domain.chat.run_request import _prepared_user_content

    class Store:
        persisted = False

        def persist_attachments(self, _conversation_id, _attachments):
            self.persisted = True
            return []

    store = Store()
    raw = {"name": ".env", "content": "API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456"}
    with pytest.raises(ValueError, match="requires explicit review"):
        _prepared_user_content(store, "conversation", {"content": "hello", "attachments": [raw]})
    assert store.persisted is False

    reviewed = _reviewed_attachment(raw["name"], raw["content"])
    _prepared_user_content(store, "conversation", {"content": "hello", "attachments": [reviewed]})
    assert store.persisted is True


def test_chat_request_rejects_non_object_attachment_before_persisting(monkeypatch):
    if sys.platform == "win32":
        monkeypatch.setitem(
            sys.modules,
            "fcntl",
            types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=8, flock=lambda *_args: None),
        )
    from domain.chat.run_request import _prepared_user_content

    class Store:
        persisted = False

        def persist_attachments(self, _conversation_id, _attachments):
            self.persisted = True
            return []

    store = Store()
    with pytest.raises(ValueError, match="payload must be an object"):
        _prepared_user_content(
            store,
            "conversation",
            {"content": "hello", "attachments": ["not-an-object"]},
        )
    assert store.persisted is False


def test_workspace_file_source_is_jailed_against_symlink_escape(tmp_path):
    from ecosystem.rumi_file_inspect_pack.runtime.inspect import FileInspectService

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("outside-secret", encoding="utf-8")
    link = workspace / "linked-secret.txt"
    try:
        link.symlink_to(outside / "secret.txt")
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable on this platform: {exc}")

    class Client:
        def invoke(self, contract, operation, payload):
            assert contract == "rumi.resource.workspace.v1"
            assert operation == "get"
            assert payload["workspace_id"] == "trusted-workspace"
            return {"root_path": str(workspace)}

    service = FileInspectService(Client())
    with pytest.raises(PermissionError, match="escapes the workspace"):
        service.invoke(
            "read",
            {"workspace_id": "trusted-workspace", "path": "linked-secret.txt"},
        )
