"""Candidate-only tests for captured Kanban owner and native presentation."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace, ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


owner = _load(
    "candidate_kanban_owner", "ecosystem/rumi_kanban_state_store_pack/runtime/store.py"
)
presentation = _load(
    "candidate_kanban_presentation",
    "ecosystem/defaultspack/defaultspack/kanban_presentation.py",
)


def _provider(root: Path, write: bool) -> Any:
    function = owner._WRITE_FUNCTION if write else owner._READ_FUNCTION
    operation = owner._WRITE_OPERATION if write else owner._READ_OPERATION
    contract = "tobkiri.action.kanban.v1" if write else "tobkiri.resource.kanban.v1"
    binding = SimpleNamespace(
        function=SimpleNamespace(function_id=function, implementation_digest="impl"),
        operation=SimpleNamespace(
            contract_id=contract, operation_id=operation, contract_version="1.0.0"
        ),
        principal_ref=SimpleNamespace(value="owner"),
        artifact=SimpleNamespace(digest="artifact"),
    )
    capture = SimpleNamespace(
        profile_id="defaults",
        user_data_root=root,
        provider_bindings=(binding,),
        domain_ids={(contract, operation, "owner"): "domain"},
    )
    return owner.KanbanStateHostFactoryV4(function).capture(capture).contributions[0]


def _invocation(provider: Any) -> SimpleNamespace:
    from tobkiri_host.broker import RequestEnvelope
    from tobkiri_host.ports import OpaqueInvocationLease

    envelope = RequestEnvelope(
        context=SimpleNamespace(profile_id="defaults"),
        target_principal=None,
        target_domain=None,
        contract_id=provider.contract_id,
        contract_version="1.0.0",
        operation_id=provider.operation_id,
        payload={},
        request_digest="sha256:" + "a" * 64,
        deadline_monotonic=999999999999,
        lease=OpaqueInvocationLease(b"lease:test"),
        idempotency_key=None,
    )
    return SimpleNamespace(
        envelope=envelope,
        assert_current=lambda: None,
        presentation_owner_principal_id="captured-shell",
        presentation_owner_session_id="captured-session",
    )


def test_captured_owner_bootstrap_cards_cas_and_durable_receipts(
    tmp_path: Path,
) -> None:
    writer = _provider(tmp_path, True)
    invoke = lambda payload: writer.invoke(
        writer.operation_id, payload, _invocation(writer)
    )
    board = invoke(
        {
            "profile_id": "defaults",
            "operation": "board.create",
            "expected_revision": 0,
            "board_id": "board-one",
            "title": "Work",
            "scope": {"type": "global", "id": "default"},
        }
    )
    assert board["revision"] == 1
    column = next(iter(board["board"]["columns"]))
    created = invoke(
        {
            "profile_id": "defaults",
            "operation": "card.upsert",
            "expected_revision": 1,
            "board_id": "board-one",
            "record_id": "card-one",
            "record": {"title": "Task", "column_id": column},
        }
    )
    assert created["revision"] == 2
    conflict = invoke(
        {
            "profile_id": "defaults",
            "operation": "card.delete",
            "expected_revision": 1,
            "board_id": "board-one",
            "record_id": "card-one",
        }
    )
    assert conflict["state"] == "error"
    assert conflict["code"] == "STALE_REVISION"
    invoke(
        {
            "profile_id": "defaults",
            "operation": "card.move",
            "expected_revision": 2,
            "board_id": "board-one",
            "record_id": "card-one",
            "record": {"column_id": column, "position": 2},
        }
    )
    invoke(
        {
            "profile_id": "defaults",
            "operation": "card.delete",
            "expected_revision": 3,
            "board_id": "board-one",
            "record_id": "card-one",
        }
    )
    state = json.loads(
        owner.KanbanStateStore("defaults", root=tmp_path).path.read_text()
    )
    assert state["revision"] == 4
    assert len(state["host_mutation_receipts"]) == 4
    reader = _provider(tmp_path, False)
    result = reader.invoke(
        reader.operation_id,
        {"profile_id": "defaults", "operation": "get", "board_id": "board-one"},
        _invocation(reader),
    )
    projected = presentation.present_kanban_result(
        presentation.KANBAN_GET_TARGET, result
    )
    assert projected["board"]["board_id"] == "board-one"
    assert projected["cards"] == []


def test_owner_rejects_client_authority_foreign_profile_and_stale_host(
    tmp_path: Path,
) -> None:
    writer = _provider(tmp_path, True)
    base = {
        "profile_id": "defaults",
        "operation": "board.create",
        "expected_revision": 0,
        "board_id": "board-one",
        "scope": {"type": "global", "id": "default"},
    }
    for extra in (
        {"approved": True},
        {"authority_receipt": "fake"},
        {"profile_id": "other"},
    ):
        with pytest.raises(PermissionError):
            writer.invoke(writer.operation_id, {**base, **extra}, _invocation(writer))
    invocation = _invocation(writer)

    def stale() -> None:
        raise PermissionError("expired invocation")

    invocation.assert_current = stale
    with pytest.raises(PermissionError, match="expired"):
        writer.invoke(writer.operation_id, base, invocation)
    assert not owner.KanbanStateStore("defaults", root=tmp_path).path.exists()


def test_native_normalizer_requires_explicit_revision_and_rejects_read_bootstrap() -> (
    None
):
    for payload in ({"scope_type": "global", "bootstrap": "true"}, {"approved": True}):
        with pytest.raises(ValueError):
            presentation.normalize_kanban_request(
                presentation.KANBAN_LIST_TARGET, payload, profile_id="defaults"
            )
    with pytest.raises(ValueError, match="expected_revision"):
        presentation.normalize_kanban_request(
            presentation.KANBAN_CREATE_TARGET, {}, profile_id="defaults"
        )
    normalized = presentation.normalize_kanban_request(
        presentation.KANBAN_CREATE_TARGET,
        {"scope_type": "global", "scope_id": "default", "expected_revision": 0},
        profile_id="defaults",
    )
    assert normalized["operation"] == "board.create"
    assert normalized["profile_id"] == "defaults"


def test_import_reads_real_conversation_owner_and_updates_one_card(
    tmp_path: Path,
) -> None:
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore

    conversations = ConversationStore("defaults", user_data_root=tmp_path)
    conversations.create(
        {"id": "conversation-one", "title": "Plan the release"}, expected_revision=0
    )
    writer = _provider(tmp_path, True)
    invocation = _invocation(writer)

    def contract_client(**kwargs: Any) -> SimpleNamespace:
        assert kwargs["allowed_contract_ids"] == frozenset(
            {"tobkiri.resource.conversation.v1"}
        )
        assert kwargs["include_credentials"] is False

        def invoke(
            contract: str,
            operation: str,
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            assert contract == "tobkiri.resource.conversation.v1"
            assert operation == "rumi_conversation_store_pack.conversation-resource"
            assert payload["profile_id"] == "defaults"
            return {"conversation": conversations.get(payload["conversation_id"])}

        return SimpleNamespace(invoke=invoke)

    invocation.contract_client = contract_client
    writer.invoke(
        writer.operation_id,
        {
            "profile_id": "defaults",
            "operation": "board.create",
            "expected_revision": 0,
            "board_id": "board-one",
            "scope": {"type": "global", "id": "default"},
        },
        invocation,
    )
    normalized = presentation.normalize_kanban_request(
        presentation.KANBAN_IMPORT_TARGET,
        {
            "expected_revision": 1,
            "board_id": "board-one",
            "conversation_id": "conversation-one",
            "use_ai": False,
        },
        profile_id="defaults",
    )
    assert normalized["operation"] == "conversation.import"
    assert normalized["expected_revision"] == 1
    imported = writer.invoke(writer.operation_id, normalized, invocation)
    projected = presentation.present_kanban_result(
        presentation.KANBAN_IMPORT_TARGET,
        imported,
    )
    assert projected["card_id"] == imported["card"]["id"]
    assert projected["imported"]["conversation_id"] == "conversation-one"
    assert imported["card"]["title"] == "Plan the release"
    assert imported["card"]["conversation_id"] == "conversation-one"
    assert imported["imported"]["conversation_id"] == "conversation-one"
    writer.invoke(
        writer.operation_id,
        {
            "profile_id": "defaults",
            "operation": "conversation.import",
            "expected_revision": 2,
            "board_id": "board-one",
            "conversation_id": "conversation-one",
        },
        invocation,
    )
    board = owner.KanbanStateStore("defaults", root=tmp_path).get("board-one")
    assert len(board["cards"]) == 1


def test_candidate_catalog_has_exact_host_owner_and_read_effect() -> None:
    """Source declarations select the captured Host owner with a read-only resource."""
    import hashlib

    primary = Path(__file__).resolve().parents[5] / "tobkiri_runtime"
    spec = importlib.util.spec_from_file_location(
        "candidate_catalog_validator", primary / "scripts/migrate_pack_artifacts_v4.py"
    )
    compiler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(compiler)
    payload = json.loads((ROOT / "schemas/pack_v4_catalog.v1.json").read_text())
    records = compiler._validate_catalog_payload(payload)
    source = next(
        record for record in records if record["pack_id"] == owner.SERVICE_PACK_ID
    )
    assert source["kind"] == "host_extension"
    assert source["execution_boundary"] == "host_brokered"
    contracts = {item["contract_id"]: item for item in source["provided_contracts"]}
    assert contracts["tobkiri.resource.kanban.v1"]["operations"][0][
        "effect_ceiling"
    ] == ["capability:kanban.state.read"]
    assert (
        "host:brokered-execution"
        in contracts["tobkiri.action.kanban.v1"]["operations"][0]["effect_ceiling"]
    )
    digest = (
        "sha256:"
        + hashlib.sha256(
            (
                ROOT / "ecosystem/rumi_kanban_state_store_pack/runtime/store.py"
            ).read_bytes()
        ).hexdigest()
    )
    assert source["runtime_artifacts"][0]["digest"] == digest
