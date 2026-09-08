"""Captured turn contracts share persistence without ambient Profile authority."""

from pathlib import Path
import copy
import json
from types import SimpleNamespace
from typing import Any

import pytest

from ecosystem.rumi_turn_runtime_pack.runtime.host import TurnHostFactoryV4
from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnConflict


def _context(root: Path, factory: TurnHostFactoryV4) -> Any:
    return SimpleNamespace(
        profile_id="defaults",
        user_data_root=root,
        provider_bindings=(
            SimpleNamespace(
                function=SimpleNamespace(
                    function_id=factory.function_id, implementation_digest="impl"
                ),
                operation=SimpleNamespace(
                    contract_id=factory.contract_id,
                    operation_id=factory.operation_id,
                    contract_version="1.0.0",
                ),
                principal_ref=SimpleNamespace(value="principal"),
                artifact=SimpleNamespace(digest="artifact"),
            ),
        ),
        domain_ids={(factory.contract_id, factory.operation_id, "principal"): "domain"},
    )


def _invoke(root: Path, kind: str, **values: Any) -> Any:
    factory = TurnHostFactoryV4(kind)
    captured = factory.capture(_context(root, factory))
    try:
        return captured.contributions[0].invoke(
            factory.operation_id, {"profile_id": "defaults", **values}, None
        )
    finally:
        captured.close()


BEGIN = {
    "operation": "begin",
    "turn_id": "turn",
    "request_id": "request",
    "conversation_id": "conversation",
    "conversation_revision": 1,
}


def test_recaptured_actions_resources_events_share_real_store(tmp_path: Path) -> None:
    before = _invoke(tmp_path, "lifecycle", **BEGIN)
    running = _invoke(
        tmp_path,
        "lifecycle",
        operation="transition",
        turn_id="turn",
        expected_revision=1,
        status="running",
    )
    assert running["revision"] == 2
    assert _invoke(tmp_path, "lifecycle", **BEGIN) == running
    for kind in ("resource", "events"):
        assert _invoke(tmp_path, kind, operation="get", turn_id="turn") == running
        assert _invoke(tmp_path, kind, operation="list", conversation_id="conversation") == {
            "turns": [running]
        }
        assert _invoke(tmp_path, kind, operation="list", conversation_id="other") == {"turns": []}
        with pytest.raises(PermissionError):
            _invoke(tmp_path, kind, **BEGIN)
    with pytest.raises(TurnConflict):
        _invoke(
            tmp_path,
            "lifecycle",
            operation="transition",
            turn_id="turn",
            expected_revision=before["revision"],
            status="cancelled",
        )


@pytest.mark.parametrize(
    "patch",
    [
        {"profile_id": "other"},
        {"approved": True},
        {"user_data_root": "/tmp/other"},
        {"operation": "execute"},
        {"conversation_revision": True},
        {"turn_id": None},
    ],
)
def test_invalid_begin_never_creates_owner_files(tmp_path: Path, patch: dict) -> None:
    with pytest.raises((PermissionError, ValueError)):
        _invoke(tmp_path, "lifecycle", **{**BEGIN, **patch})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "field", ["profile_id", "user_data_root", "domain_ids", "provider_bindings"]
)
def test_incomplete_capture_does_not_create_files(tmp_path: Path, field: str) -> None:
    factory = TurnHostFactoryV4("lifecycle")
    context = _context(tmp_path, factory)
    setattr(
        context,
        field,
        {} if field == "domain_ids" else () if field == "provider_bindings" else None,
    )
    with pytest.raises(PermissionError):
        factory.capture(context)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field", ["contract_id", "operation_id", "contract_version"])
def test_wrong_binding_is_rejected(tmp_path: Path, field: str) -> None:
    factory = TurnHostFactoryV4("resource")
    context = _context(tmp_path, factory)
    setattr(context.provider_bindings[0].operation, field, "other")
    with pytest.raises(PermissionError):
        factory.capture(context)


@pytest.mark.parametrize(
    "patch",
    [
        {"expected_revision": True},
        {"details": []},
        {"approved": True},
        {"status": None},
    ],
)
def test_invalid_mutation_leaves_state_unchanged(tmp_path: Path, patch: dict) -> None:
    before = _invoke(tmp_path, "lifecycle", **BEGIN)
    with pytest.raises((PermissionError, ValueError)):
        _invoke(
            tmp_path,
            "lifecycle",
            **{
                "operation": "transition",
                "turn_id": "turn",
                "expected_revision": 1,
                "status": "running",
                **patch,
            },
        )
    assert _invoke(tmp_path, "resource", operation="get", turn_id="turn") == before


def test_filter_is_applied_before_limit(tmp_path: Path) -> None:
    _invoke(tmp_path, "lifecycle", **{**BEGIN, "turn_id": "z", "request_id": "z"})
    _invoke(tmp_path, "lifecycle", **{**BEGIN, "conversation_id": "other"})
    values = _invoke(
        tmp_path, "resource", operation="list", conversation_id="conversation", limit=1
    )
    assert [turn["id"] for turn in values["turns"]] == ["z"]


def test_offline_legacy_pin_requires_verified_replacement_identity() -> None:
    from scripts.migrate_manifest_authority import _load_v4, _matches_legacy_adapter

    root = Path(__file__).resolve().parents[1] / "ecosystem" / "rumi_turn_runtime_pack"
    manifest = json.loads((root / "rumi.pack.v3.json").read_text())
    entrypoint = manifest["entrypoints"][0]
    v4 = _load_v4(root)
    digest = entrypoint["artifact_hash"]
    assert _matches_legacy_adapter(root, entrypoint, v4, digest)
    assert not _matches_legacy_adapter(root, {**entrypoint, "symbol": "other"}, v4, digest)
    assert not _matches_legacy_adapter(root, entrypoint, v4, "sha256:" + "0" * 64)
    altered = copy.deepcopy(v4)
    altered["functions"] = {key: "sha256:" + "0" * 64 for key in v4["functions"]}
    assert not _matches_legacy_adapter(root, entrypoint, altered, digest)
