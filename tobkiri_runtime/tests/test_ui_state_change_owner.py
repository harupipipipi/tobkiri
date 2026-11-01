"""Finite model and recovery-diagnostic owner operations."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


from ecosystem.tobkiri_ui_settings_pack.runtime.state_changes import (
    DIAGNOSTIC_ACTION,
    DIAGNOSTIC_CONTRACT,
    DIAGNOSTIC_READ_FUNCTION,
    DIAGNOSTIC_READ_OPERATION,
    DIAGNOSTIC_WRITE_FUNCTION,
    DIAGNOSTIC_WRITE_OPERATION,
    MODEL_ACTION,
    MODEL_CONTRACT,
    MODEL_READ_FUNCTION,
    MODEL_READ_OPERATION,
    MODEL_WRITE_FUNCTION,
    MODEL_WRITE_OPERATION,
    STRATEGY_CATALOG_CONTRACT,
    STRATEGY_CATALOG_OPERATION,
    StateChangesHostFactoryV4,
)


class _Invocation:
    envelope = SimpleNamespace()
    presentation_owner_principal_id = "shell.tauri.default"
    presentation_owner_session_id = "session-one"

    def assert_current(self) -> None:
        """Represent a current Host-authenticated request."""


class _StrategyCatalogClient:
    """A captured read-only strategy catalog used by model state writes."""

    def __init__(self, strategies: list[dict[str, object]]) -> None:
        self.strategies = strategies
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        self.calls.append((contract_id, operation_id, payload))
        assert (contract_id, operation_id) == (
            STRATEGY_CATALOG_CONTRACT,
            STRATEGY_CATALOG_OPERATION,
        )
        return {"strategies": self.strategies}


class _StrategyInvocation(_Invocation):
    """Expose only the invocation-bound catalog client to the state owner."""

    def __init__(self, strategies: list[dict[str, object]]) -> None:
        self.client = _StrategyCatalogClient(strategies)

    def contract_client(self, **kwargs: object) -> _StrategyCatalogClient:
        assert kwargs == {
            "allowed_contract_ids": frozenset({STRATEGY_CATALOG_CONTRACT}),
            "consumer_pack_id": "tobkiri_ui_settings_pack",
            "include_credentials": False,
        }
        return self.client


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
    reader = StateChangesHostFactoryV4(MODEL_READ_FUNCTION).capture(
        _capture(tmp_path, MODEL_READ_FUNCTION, MODEL_CONTRACT, MODEL_READ_OPERATION)
    ).contributions[0]
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
    current = reader.invoke(MODEL_READ_OPERATION, {"profile_id": "defaults"}, _Invocation())
    assert current["revision"] == 1
    assert current["values"]["preferred_model"] == "local/main"
    second = writer.invoke(
        MODEL_WRITE_OPERATION,
        {**payload, "value": "local/next", "expected_revision": current["revision"],
         "mutation_id": "model-mutation-2"},
        _Invocation(),
    )
    assert second["revision"] == 2
    assert reader.invoke(MODEL_READ_OPERATION, {"profile_id": "defaults"}, _Invocation())[
        "revision"
    ] == 2
    for forbidden in ({"path": "/tmp/state"}, {"approved": True}, {"callback": "x"}):
        with pytest.raises(PermissionError):
            writer.invoke(MODEL_WRITE_OPERATION, {**payload, **forbidden}, _Invocation())


def test_strategy_state_write_requires_the_captured_plan_catalog(tmp_path: Path) -> None:
    """A persisted strategy reference must be present exactly once in the Host catalog."""

    writer = StateChangesHostFactoryV4(MODEL_WRITE_FUNCTION).capture(
        _capture(tmp_path, MODEL_WRITE_FUNCTION, MODEL_ACTION, MODEL_WRITE_OPERATION)
    ).contributions[0]
    reference = "third_party_strategy_pack.review.execute"
    payload = {
        "profile_id": "defaults",
        "kind": "strategy_reference",
        "value": reference,
        "expected_revision": 0,
        "mutation_id": "strategy-mutation-1",
    }
    invocation = _StrategyInvocation([{"strategy_reference": reference}])

    result = writer.invoke(MODEL_WRITE_OPERATION, payload, invocation)

    assert result["value"] == reference
    assert invocation.client.calls == [
        (STRATEGY_CATALOG_CONTRACT, STRATEGY_CATALOG_OPERATION, {})
    ]
    with pytest.raises(PermissionError, match="selected strategy is unavailable"):
        writer.invoke(
            MODEL_WRITE_OPERATION,
            {
                **payload,
                "value": "missing_strategy_pack.review.execute",
                "expected_revision": 1,
                "mutation_id": "strategy-mutation-2",
            },
            _StrategyInvocation([]),
        )


def test_legacy_model_settings_cannot_bypass_strategy_catalog(tmp_path: Path) -> None:
    """Legacy settings writes may clear, but cannot select, a strategy."""

    import sys

    defaultspack_root = Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack"
    sys.path.insert(0, str(defaultspack_root))
    from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService

    settings = ModelRuntimeSettingsService(pack_root=tmp_path)
    with pytest.raises(PermissionError, match="captured model-state action"):
        settings.set_strategy_reference("third_party_strategy_pack.review.execute")
    with pytest.raises(PermissionError, match="captured model-state action"):
        settings.sanitize_models_patch(
            {"strategy_reference": "third_party_strategy_pack.review.execute"}
        )


def test_diagnostic_is_redacted_bounded_and_never_returns_content(tmp_path: Path) -> None:
    reader = StateChangesHostFactoryV4(DIAGNOSTIC_READ_FUNCTION).capture(
        _capture(
            tmp_path, DIAGNOSTIC_READ_FUNCTION, DIAGNOSTIC_CONTRACT,
            DIAGNOSTIC_READ_OPERATION,
        )
    ).contributions[0]
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
    current = reader.invoke(
        DIAGNOSTIC_READ_OPERATION, {"profile_id": "defaults"}, _Invocation()
    )
    assert current["revision"] == current["record_count"] == 1
    second = writer.invoke(
        DIAGNOSTIC_WRITE_OPERATION,
        {**payload, "expected_revision": current["revision"],
         "mutation_id": "diagnostic-mutation-2",
         "diagnostic": {**payload["diagnostic"], "event_id": "event-two"}},
        _Invocation(),
    )
    assert second["revision"] == 2
    current = reader.invoke(
        DIAGNOSTIC_READ_OPERATION, {"profile_id": "defaults"}, _Invocation()
    )
    assert current["revision"] == current["record_count"] == 2
    saved = (tmp_path / "defaultspack" / "shared" / "frontend_settings.json").read_text()
    assert "token=secret" not in saved
    assert "/Users/person" not in saved
    assert "example.test" not in saved
