"""Settings read identity checks run before any state or dependency access."""

from pathlib import Path
import json
from types import SimpleNamespace
from typing import Any

import pytest

from ecosystem.tobkiri_ui_settings_pack.runtime.settings import (
    CONTRACT_ID,
    FUNCTION_ID,
    OPERATION_ID,
    SettingsReadHostFactoryV4,
    PreferencesWriteHostFactoryV4,
    WRITE_CONTRACT_ID,
    WRITE_FUNCTION_ID,
    WRITE_OPERATION_ID,
)


def _context(root: Path) -> Any:
    binding = SimpleNamespace(
        function=SimpleNamespace(function_id=FUNCTION_ID, implementation_digest="impl"),
        operation=SimpleNamespace(
            contract_id=CONTRACT_ID, operation_id=OPERATION_ID, contract_version="1.0.0"
        ),
        principal_ref=SimpleNamespace(value="reader"),
        artifact=SimpleNamespace(digest="artifact"),
    )
    return SimpleNamespace(
        profile_id="defaults",
        user_data_root=root,
        provider_bindings=(binding,),
        domain_ids={(CONTRACT_ID, OPERATION_ID, "reader"): "domain"},
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"profile_id": "other"},
        {"approved": True},
        {"root": "/tmp/other"},
        {"operation": "write"},
        {"full": True},
    ],
)
def test_settings_rejects_request_widening_before_dependency_access(
    tmp_path: Path,
    patch: dict[str, Any],
) -> None:
    reader = SettingsReadHostFactoryV4().capture(_context(tmp_path))
    with pytest.raises(PermissionError, match="request is invalid"):
        reader.contributions[0].invoke(OPERATION_ID, {"profile_id": "defaults", **patch}, None)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field", ["domain_ids", "profile_id", "user_data_root"])
def test_settings_rejects_incomplete_capture(tmp_path: Path, field: str) -> None:
    context = _context(tmp_path)
    setattr(context, field, {} if field == "domain_ids" else None)
    with pytest.raises(PermissionError):
        SettingsReadHostFactoryV4().capture(context)
    assert list(tmp_path.iterdir()) == []


def test_settings_rejects_other_operation_before_dependency_access(tmp_path: Path) -> None:
    reader = SettingsReadHostFactoryV4().capture(_context(tmp_path))
    with pytest.raises(PermissionError, match="request is invalid"):
        reader.contributions[0].invoke("write", {"profile_id": "defaults"}, None)
    assert list(tmp_path.iterdir()) == []


def _write_context(root: Path) -> Any:
    context = _context(root)
    binding = context.provider_bindings[0]
    binding.function.function_id = WRITE_FUNCTION_ID
    binding.operation.contract_id = WRITE_CONTRACT_ID
    binding.operation.operation_id = WRITE_OPERATION_ID
    context.domain_ids = {(WRITE_CONTRACT_ID, WRITE_OPERATION_ID, "reader"): "write-domain"}
    return context


def test_preferences_write_cannot_capture_the_read_function(tmp_path):
    with pytest.raises(PermissionError, match="binding is invalid"):
        PreferencesWriteHostFactoryV4().capture(_context(tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("patch", [
    {"profile_id": "other"},
    {"approved": True},
    {"root": "/tmp/other"},
    {"allowed_fields": {"models": ["preferred_model"]}},
    {"changes": {"models": {"preferred_model": "model"}}},
    {"changes": {"apis": {"api_keys": ["secret"]}}},
    {"changes": {"general": {"not_a_control": True}}},
    {"changes": {"general": {"language": {"token": "secret"}}}},
    {"changes": {"general": {"language": "x" * 2049}}},
    {"changes": {"general": {"show_activity_in_messages": 1}}},
    {"expected_revision": True},
])
def test_preferences_write_denies_widening_without_storage(tmp_path, patch):
    writer = PreferencesWriteHostFactoryV4().capture(_write_context(tmp_path))
    with pytest.raises((PermissionError, ValueError)):
        writer.contributions[0].invoke(WRITE_OPERATION_ID, {
            "profile_id": "defaults", "expected_revision": 0,
            "changes": {"general": {"language": "ja"}}, **patch,
        }, None)
    assert list(tmp_path.iterdir()) == []


def test_preferences_write_returns_only_changed_values_and_preserves_private_state(tmp_path):
    path = tmp_path / "defaultspack" / "shared" / "frontend_settings.json"
    path.parent.mkdir(parents=True)
    saved = {"general": {"language": "en", "private": "secret"}, "unknown": ["kept"]}
    path.write_text(json.dumps(saved), encoding="utf-8")
    writer = PreferencesWriteHostFactoryV4().capture(_write_context(tmp_path))
    result = writer.contributions[0].invoke(WRITE_OPERATION_ID, {
        "profile_id": "defaults", "expected_revision": 0,
        "changes": {"general": {"language": "ja"}},
    }, None)
    assert result == {"values": {"general": {"language": "ja"}}, "document_revision": 1}
    assert json.loads(path.read_text()) == {
        **saved, "general": {"language": "ja", "private": "secret"},
        "_settings_revision": 1,
    }


def test_preferences_contract_has_independent_write_capability_and_closed_schema():
    from jsonschema import Draft202012Validator

    root = Path(__file__).resolve().parents[1]
    catalog = json.loads((root / "schemas" / "pack_v4_catalog.v1.json").read_text())
    pack = next(p for p in catalog["packs"] if p["pack_id"] == "tobkiri_ui_settings_pack")
    write = next(c for c in pack["provided_contracts"] if c["contract_id"] == WRITE_CONTRACT_ID)
    assert write["required_capabilities"] == ["ui.preferences.write"]
    validator = Draft202012Validator(write["schemas"]["input"])
    request = {"profile_id": "defaults", "expected_revision": 0,
               "changes": {"general": {"language": "ja"}}}
    validator.validate(request)
    assert list(validator.iter_errors({**request, "approved": True}))
    assert list(validator.iter_errors({**request, "changes": {"models": {"preferred_model": "m"}}}))
