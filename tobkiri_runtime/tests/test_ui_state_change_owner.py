"""Finite model and recovery-diagnostic owner operations."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ecosystem.tobkiri_ui_settings_pack.runtime.state_changes import (
    DIAGNOSTIC_ACTION,
    DIAGNOSTIC_WRITE_FUNCTION,
    DIAGNOSTIC_WRITE_OPERATION,
    MODEL_ACTION,
    MODEL_WRITE_FUNCTION,
    MODEL_WRITE_OPERATION,
    StateChangesHostFactoryV4,
)


class _Invocation:
    envelope = SimpleNamespace()
    presentation_owner_principal_id = "shell.tauri.default"
    presentation_owner_session_id = "session-one"

    def assert_current(self) -> None:
        """Represent a current Host-authenticated request."""


def _capture(root: Path, function_id: str, contract: str, operation: str):
    binding = SimpleNamespace(
        function=SimpleNamespace(function_id=function_id, implementation_digest="impl"),
        operation=SimpleNamespace(
            contract_id=contract, operation_id=operation, contract_version="1.0.0"
        ),
        principal_ref=SimpleNamespace(value="owner"),
        artifact=SimpleNamespace(digest="artifact"),
    )
    return SimpleNamespace(
        profile_id="defaults", user_data_root=root, provider_bindings=(binding,),
        domain_ids={(contract, operation, "owner"): "domain"},
    )


def test_model_state_is_finite_revisioned_and_receipted(tmp_path: Path) -> None:
    writer = StateChangesHostFactoryV4(MODEL_WRITE_FUNCTION).capture(
        _capture(tmp_path, MODEL_WRITE_FUNCTION, MODEL_ACTION, MODEL_WRITE_OPERATION)
    ).contributions[0]
    payload = {
        "profile_id": "defaults", "kind": "preferred_model", "value": "local/main",
        "expected_revision": 0, "mutation_id": "model-mutation-1",
    }
    result = writer.invoke(MODEL_WRITE_OPERATION, payload, _Invocation())
    replay = writer.invoke(MODEL_WRITE_OPERATION, payload, _Invocation())
    assert result["revision"] == replay["revision"] == 1
    assert result["receipt"].startswith("sha256:")
    assert replay["idempotent_replay"] is True
    for forbidden in ({"path": "/tmp/state"}, {"approved": True}, {"callback": "x"}):
        with pytest.raises(PermissionError):
            writer.invoke(MODEL_WRITE_OPERATION, {**payload, **forbidden}, _Invocation())


def test_diagnostic_is_redacted_bounded_and_never_returns_content(tmp_path: Path) -> None:
    writer = StateChangesHostFactoryV4(DIAGNOSTIC_WRITE_FUNCTION).capture(
        _capture(
            tmp_path, DIAGNOSTIC_WRITE_FUNCTION, DIAGNOSTIC_ACTION,
            DIAGNOSTIC_WRITE_OPERATION,
        )
    ).contributions[0]
    payload = {
        "profile_id": "defaults", "expected_revision": 0,
        "mutation_id": "diagnostic-mutation-1",
        "diagnostic": {
            "schema_version": "rumi.client_diagnostic.v2",
            "event_id": "event", "session_id": "session", "fingerprint": "fingerprint",
            "privacy_mode": "standard", "source": "react", "category": "crash",
            "level": "error", "message": "token=secret /Users/person/private.txt",
            "detail": {"route": "https://example.test/private", "line": 4},
        },
    }
    result = writer.invoke(DIAGNOSTIC_WRITE_OPERATION, payload, _Invocation())
    assert result["recorded"] is True
    assert result["diagnostic_id"].startswith("diag_")
    assert "diagnostic" not in result
    saved = (tmp_path / "defaultspack" / "shared" / "frontend_settings.json").read_text()
    assert "token=secret" not in saved
    assert "/Users/person" not in saved
    assert "example.test" not in saved
