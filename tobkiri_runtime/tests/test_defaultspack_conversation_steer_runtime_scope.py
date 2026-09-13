from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_steer_recovers_persisted_profile_for_request_worker(monkeypatch):
    from domain.chat import steer

    registry = object()
    plan = SimpleNamespace(profile_id="default-profile")
    calls: list[tuple[object, str, str, dict]] = []

    monkeypatch.setattr(
        steer,
        "get_container",
        lambda: SimpleNamespace(get_or_none=lambda key: registry),
    )
    monkeypatch.setattr(
        steer,
        "captured_profile_id",
        lambda current_registry: plan.profile_id,
    )
    monkeypatch.setattr(
        steer,
        "invoke_global_contract",
        lambda current_registry, contract_id, operation, payload: calls.append(
            (current_registry, contract_id, operation, payload)
        )
        or {"ok": True},
    )

    result = steer._invoke("rumi.resource.turn.v1", "list", {"conversation_id": "c1"})

    assert result == {"ok": True}
    assert calls == [
        (
            registry,
            "rumi.resource.turn.v1",
            "list",
            {"profile_id": "default-profile", "conversation_id": "c1"},
        )
    ]
