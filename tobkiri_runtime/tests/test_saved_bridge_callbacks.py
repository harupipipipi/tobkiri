"""Real-owner callback policy tests; transport/Broker adapters are explicit."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.bootstrap.saved_bridge import READINESS, REQUIRED_TARGETS, SavedBridgeCallbacks
from ecosystem.defaultspack.runtime import saved_conversation as saved
from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
from tobkiri_host.continuation_chain import ChainIdentity
from tobkiri_host.continuation_envelope import seal_continuation_intent
from tobkiri_protocol.canonical import canonical_json, strict_loads


def _setup(tmp_path: Path):
    store = ConversationStore("defaults", user_data_root=tmp_path)
    store.create(
        {"id": "conversation-1", "model_reference": "model-profile-1"}, expected_revision=0
    )
    outer = SimpleNamespace(
        context=SimpleNamespace(request_id="host-request"),
        payload={
            "request": {
                "turn_id": "turn-1",
                "conversation_id": "conversation-1",
                "conversation_revision": 1,
                "content": "Hello",
            }
        },
    )
    calls = []

    def require_targets(request, targets):
        assert request is outer
        assert targets == REQUIRED_TARGETS

    def dispatch(request, target, payload):
        assert request is outer
        calls.append((target, deepcopy(payload)))
        if target == saved.TARGETS[0]:
            value = {"conversation": store.get(payload["conversation_id"])}
        elif target == saved.TARGETS[1]:
            value = store.append_message(
                payload["conversation_id"],
                payload["message"],
                expected_conversation_revision=payload["expected_conversation_revision"],
            )
        elif target == READINESS:
            value = {"ready": True, "model_profile_id": "model-profile-1"}
        else:
            assert target == saved.TARGETS[2]
            value = {"status": "ok", "output": "Hi"}
        return {"status": "ok", "value": value}

    return store, outer, calls, SavedBridgeCallbacks(dispatch, require_targets)


def _frame(intent, request_id="host-request"):
    hop = intent["hop"]
    checked = seal_continuation_intent(
        canonical_json(intent),
        identity=ChainIdentity("domain", request_id, "sha256:" + "a" * 64, 60),
        hop=hop,
        previous_digest=None if hop == 0 else "sha256:" + "b" * 64,
        target=saved.TARGETS[hop],
        nonce=str(hop) * 48,
    )
    return strict_loads(checked.frame)


def test_preflight_is_read_only_and_four_stages_use_real_owner(tmp_path: Path) -> None:
    store, outer, calls, callbacks = _setup(tmp_path)
    before = store.path.read_bytes()
    callbacks.preflight(outer)
    assert store.path.read_bytes() == before
    assert [target for target, _ in calls] == [saved.TARGETS[0], READINESS]
    assert calls[-1][1] == {
        "model_profile_id": "model-profile-1",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    intent = saved.start(outer.payload["request"])
    for _ in range(4):
        outcome = callbacks(outer, _frame(intent))
        intent = saved.resume(intent["state"], outcome)
    assert intent["status"] == "ok"
    assert [message["content"] for message in store.get("conversation-1")["messages"]] == [
        "Hello",
        "Hi",
    ]
    # The Host independently re-reads owned history/model immediately before AI.
    assert [target for target, _ in calls[2:]] == [
        saved.TARGETS[0],
        saved.TARGETS[0],
        saved.TARGETS[1],
        saved.TARGETS[0],
        saved.TARGETS[2],
        saved.TARGETS[3],
    ]


@pytest.mark.parametrize("patch", [
    {"conversation_kind": "coding"},
    {"group_id": "group-1"},
    {"metadata": {"workspace_id": "workspace-1"}},
    {"metadata": {"groupId": "group-1"}},
    {"metadata": {"workspaceId": "workspace-1"}},
    {"metadata": {"workspaceRoot": "/workspace"}},
    {"metadata": {"rootPath": "/workspace"}},
    {"metadata": {"rumiDataPath": "/data"}},
    {"metadata": {"rumi_dp_path": "/data"}},
    {"metadata": {"workspace_root": "/workspace"}},
    {"metadata": {"rumi_data_path": "/data"}},
    {"metadata": {"mode": "coding"}},
    {"metadata": {"shared_read_only": True}},
    {"metadata": {"profile_id": "defaultspack.operations_company"}},
    {"tags": ["mimo-coding-company"]},
])
@pytest.mark.parametrize("stage", ["preflight", "append"])
def test_owned_special_context_is_rejected_before_user_append(
    tmp_path: Path, patch: dict, stage: str,
) -> None:
    store, outer, calls, callbacks = _setup(tmp_path)
    store.update("conversation-1", patch, expected_conversation_revision=1)
    outer.payload["request"]["conversation_revision"] = 2
    before = store.path.read_bytes()
    with pytest.raises(AuthorityDenied, match="context resolution"):
        if stage == "preflight":
            callbacks.preflight(outer)
        else:
            # A guest omitting the context check still cannot obtain a write.
            intent = saved.start(outer.payload["request"])
            intent = saved.resume(intent["state"], {
                "status": "ok", "value": {"conversation": store.get("conversation-1")},
            })
            callbacks(outer, _frame(intent))
    assert store.path.read_bytes() == before
    assert calls and all(target == saved.TARGETS[0] for target, _ in calls)


def test_display_only_metadata_does_not_block_saved_preflight(tmp_path: Path) -> None:
    store, outer, calls, callbacks = _setup(tmp_path)
    store.update("conversation-1", {
        "title": "My chat", "tags": ["research"],
        "metadata": {"icon": "chat", "mode": "chat", "shared_read_only": False},
    }, expected_conversation_revision=1)
    outer.payload["request"]["conversation_revision"] = 2
    before = store.path.read_bytes()
    callbacks.preflight(outer)
    assert store.path.read_bytes() == before
    assert calls[-1][0] == READINESS


def test_saved_text_blocks_use_plain_readiness_without_changing_history(
    tmp_path: Path,
) -> None:
    """The readiness contract gets text while the owner retains exact blocks."""
    store, outer, calls, callbacks = _setup(tmp_path)
    blocks = [{"type": "text", "text": "First "}, {"type": "text", "text": "reply"}]
    store.append_message("conversation-1", {
        "id": "prior-assistant", "role": "assistant", "content": blocks,
    }, expected_conversation_revision=1)
    outer.payload["request"]["conversation_revision"] = 2
    before = store.path.read_bytes()
    callbacks.preflight(outer)
    assert store.path.read_bytes() == before
    assert calls[-1] == (READINESS, {
        "model_profile_id": "model-profile-1",
        "messages": [
            {"role": "assistant", "content": "First reply"},
            {"role": "user", "content": "Hello"},
        ],
    })


@pytest.mark.parametrize("content", [
    [{"type": "image", "url": "https://example.invalid/private.png"}],
    [{"type": "text", "text": "visible"}, {"type": "tool_result", "content": "hidden"}],
    [{"type": "text", "text": "visible", "attachment_id": "unresolved"}],
    [{"type": "text", "text": {"url": "https://example.invalid"}}],
    ["untyped text"],
])
def test_saved_text_blocks_do_not_admit_unresolved_content(
    tmp_path: Path, content: list,
) -> None:
    """Reject mixed/unknown blocks before a readiness probe or user write."""
    store, outer, calls, callbacks = _setup(tmp_path)
    store.append_message("conversation-1", {
        "id": "prior-assistant", "role": "assistant", "content": content,
    }, expected_conversation_revision=1)
    outer.payload["request"]["conversation_revision"] = 2
    before = store.path.read_bytes()
    with pytest.raises(AuthorityDenied, match="additional content resolution"):
        callbacks.preflight(outer)
    assert store.path.read_bytes() == before
    assert calls and all(target == saved.TARGETS[0] for target, _ in calls)


def test_missing_target_rejects_before_first_owner_read(tmp_path: Path) -> None:
    _, outer, calls, callbacks = _setup(tmp_path)

    def denied(request, targets):
        raise AuthorityDenied("missing captured edge")

    callbacks._require_targets = denied
    with pytest.raises(AuthorityDenied, match="missing captured edge"):
        callbacks.preflight(outer)
    assert not calls


@pytest.mark.parametrize("change", ["revision", "prompt", "model", "route"])
def test_preflight_failure_cannot_persist_user_input(tmp_path: Path, change: str) -> None:
    store, outer, calls, callbacks = _setup(tmp_path)
    before = store.path.read_bytes()
    original = callbacks._dispatch

    def dispatch(request, target, payload):
        outcome = original(request, target, payload)
        if target == saved.TARGETS[0]:
            conversation = outcome["value"]["conversation"]
            if change == "revision":
                conversation["conversation_revision"] += 1
            elif change == "prompt":
                conversation["system_prompt_id"] = "unresolved-prompt"
            elif change == "model":
                conversation["model_reference"] = None
        elif target == READINESS and change == "route":
            outcome["value"]["ready"] = False
        return outcome

    callbacks._dispatch = dispatch
    with pytest.raises(AuthorityDenied):
        callbacks.preflight(outer)
    assert store.path.read_bytes() == before
    assert all(target in (saved.TARGETS[0], READINESS) for target, _ in calls)


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "other-request"),
        ("version", 1),
        ("hop", True),
        ("target", {"contract_id": "other", "operation_id": "get"}),
        ("payload", {"operation": "get", "conversation_id": "another-conversation"}),
    ],
)
def test_forged_stage_is_rejected_without_dispatch(
    tmp_path: Path, field: str, value: object
) -> None:
    _, outer, calls, callbacks = _setup(tmp_path)
    frame = _frame(saved.start(outer.payload["request"]))
    frame[field] = value
    with pytest.raises(AuthorityDenied):
        callbacks(outer, frame)
    assert not calls


def test_user_append_cannot_change_the_original_input(tmp_path: Path) -> None:
    store, outer, calls, callbacks = _setup(tmp_path)
    intent = saved.start(outer.payload["request"])
    intent = saved.resume(intent["state"], callbacks(outer, _frame(intent)))
    frame = _frame(intent)
    frame["payload"]["message"]["content"] = "changed by guest"
    before = store.path.read_bytes()
    with pytest.raises(AuthorityDenied, match="append is out of scope"):
        callbacks(outer, frame)
    assert store.path.read_bytes() == before
    assert len(calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_reference", "other-model"),
        ("messages", [{"role": "user", "content": "injected history"}]),
        ("requirements", {"request_surface": "other-surface"}),
        ("requirements", {"request_surface": "defaultspack.conversation"}),
    ],
)
def test_ai_input_is_checked_against_current_owner(
    tmp_path: Path, field: str, value: object
) -> None:
    _, outer, calls, callbacks = _setup(tmp_path)
    intent = saved.start(outer.payload["request"])
    for _ in range(2):
        intent = saved.resume(intent["state"], callbacks(outer, _frame(intent)))
    frame = _frame(intent)
    frame["payload"][field] = value
    with pytest.raises(AuthorityDenied, match="AI input differs"):
        callbacks(outer, frame)
    assert all(target != saved.TARGETS[2] for target, _ in calls)


@pytest.mark.parametrize("missing_readiness", [False, True])
def test_production_capture_binds_saved_edges_and_real_owner_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_readiness: bool,
) -> None:
    """Use isolated signed Profile capture; AI/readiness and VM are adapters."""
    import json
    import threading
    import time

    from core_runtime.authority.v4 import AuthorityStore, FunctionPrincipal
    from core_runtime.bootstrap.profile_capture import (
        capture_profile,
        host_profile_catalog,
        prepare_profile_confirmation,
    )
    from core_runtime.bootstrap.production_v4 import capture_production_dispatch
    from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
    from ecosystem.defaultspack.defaultspack.runtime_composition import (
        defaultspack_activation_snapshot_loader,
    )
    from ecosystem.defaultspack.domain.runtime_surface_v4 import create_runtime_surface_services
    from tests.test_live_production_v4_dispatch import _CapturedBackend, _bundle_root, _digest
    from tobkiri_host.backends import BackendRegistry
    from tobkiri_host.models import OpaqueAuthorityRef
    from tobkiri_host.runtime import V4DispatchSession

    user_data = tmp_path / "isolated-host"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    host_profile_catalog()
    definitions = ProfileDefinitionStore(user_data)
    profile = deepcopy(definitions.get_profile("defaults").profile)
    runtime = Path(__file__).resolve().parents[1]
    saved_function = "defaultspack.conversation.saved"
    # This isolated negative fixture controls its own saved edges. Ordinary
    # Defaults now has coordinator edges, verified separately below.
    profile["requested_edges"] = [
        item for item in profile["requested_edges"]
        if item["caller_function_id"] not in {
            saved_function,
            "rumi_tool_broker_pack.tool-broker.invoke",
            "rumi_tool_local_executor_pack.tool-executor.local",
        }
        and not (missing_readiness and item["caller_function_id"] == READINESS[1])
    ]

    def edge(caller, target, provider):
        pack = provider.split(".")[0]
        variants = json.loads((runtime / "ecosystem" / pack / "executables.v4.json").read_text())[
            "variants"
        ]
        operation = next(
            op
            for variant in variants
            if variant["function_id"] == provider
            for op in variant["operations"]
            if (op["contract_id"], op["operation_id"]) == target
        )
        return {
            "caller_function_id": caller,
            "contract_id": target[0],
            "operation_id": target[1],
            "target_provider_id": provider,
            "requested_scope_template": {
                "capability": "operation.invoke",
                "semantics_digest": operation["revision_digest"],
                "dimensions": {"contract": [target[0]], "operation": [target[1]]},
                "quotas": {},
                "exact_request_digest": None,
                "opaque": False,
            },
        }

    profile["requested_edges"].extend(
        [
            edge(
                "shell.tauri.default",
                ("conversation.saved-turn.v1", "saved_complete"),
                saved_function,
            ),
            edge(
                saved_function,
                saved.TARGETS[0],
                "rumi_conversation_store_pack.conversation-store.resource",
            ),
            edge(
                saved_function,
                saved.TARGETS[1],
                "rumi_conversation_store_pack.conversation-store.message-manage",
            ),
            edge(saved_function, saved.TARGETS[2], "rumi_ai_gateway_pack.ai-gateway.generate"),
        ]
    )
    if not missing_readiness:
        profile["requested_edges"].append(edge(saved_function, READINESS, READINESS[1]))
    definitions.create_profile(
        profile, profile_id="profile-saved-test", display_name="Saved callback test"
    )
    active = capture_profile(
        "profile-saved-test", confirmation=prepare_profile_confirmation("profile-saved-test")
    )
    binding = next(
        item
        for item in active.resolved.plan["bindings"]
        if item["contract_id"] == "conversation.saved-turn.v1"
    )
    principal = FunctionPrincipal.from_dict(binding["function_principal"])
    backend = _CapturedBackend(_digest("saved-capture"))
    session = capture_production_dispatch(
        active,
        bundle_root=_bundle_root(),
        ecosystem_root=runtime / "ecosystem",
        authority_store=AuthorityStore(user_data / "authority/v4.sqlite3"),
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
        backends=BackendRegistry((backend,)),
        target_backend_digests={principal.principal_id: backend.status.backend_digest},
    )
    try:
        assert backend.saved_callbacks is not None
        callback, preflight = backend.saved_callbacks
        context = session.context_for(
            "conversation.saved-turn.v1", "saved_complete", "session.panel.saved"
        )
        outer = SimpleNamespace(
            context=context,
            contract_id="conversation.saved-turn.v1",
            contract_version="1.0.0",
            operation_id="saved_complete",
            target_principal=OpaqueAuthorityRef(principal.principal_id),
            target_domain=OpaqueAuthorityRef(context.target_domain_id),
            deadline_monotonic=time.monotonic() + 60,
            cancellation_requested=threading.Event(),
            payload={
                "request": {
                    "turn_id": "turn-1",
                    "conversation_id": "conversation-1",
                    "conversation_revision": 1,
                    "content": "Hello",
                }
            },
        )
        store = ConversationStore("profile-saved-test", user_data_root=user_data)
        store.create(
            {"id": "conversation-1", "model_reference": "model-profile-1"}, expected_revision=0
        )
        original = V4DispatchSession.invoke
        calls = []

        def invoke(self, contract_id, operation_id, payload, **kwargs):
            calls.append((contract_id, operation_id))
            if (contract_id, operation_id) == READINESS:
                return {"ready": True, "model_profile_id": "model-profile-1"}
            if (contract_id, operation_id) == saved.TARGETS[2]:
                return {"status": "ok", "output": "Hi"}
            return original(self, contract_id, operation_id, payload, **kwargs)

        monkeypatch.setattr(V4DispatchSession, "invoke", invoke)
        before = store.path.read_bytes()
        if missing_readiness:
            with pytest.raises(AuthorityDenied, match="target is missing"):
                preflight(outer)
            assert not calls
            assert store.path.read_bytes() == before
            return
        preflight(outer)
        assert store.path.read_bytes() == before
        intent = saved.start(outer.payload["request"])
        for _ in range(4):
            intent = saved.resume(
                intent["state"], callback(outer, _frame(intent, context.request_id))
            )
        assert intent["status"] == "ok", intent
        assert [message["content"] for message in store.get("conversation-1")["messages"]] == [
            "Hello",
            "Hi",
        ]
    finally:
        session.close()


@pytest.mark.parametrize("lost_reply", [None, "guest", "owner"])
def test_normal_defaults_saved_coordinator_dispatches_owner_stages_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lost_reply: str | None,
) -> None:
    """Normal Defaults/Broker/owners; VM, readiness and AI are explicit adapters."""
    from core_runtime.authority.v4 import AuthorityStore, FunctionPrincipal
    from core_runtime.bootstrap.profile_capture import (
        capture_default_profile, prepare_default_profile_confirmation,
        profile_capture_scope,
    )
    from core_runtime.bootstrap.production_v4 import capture_production_dispatch
    from ecosystem.defaultspack.defaultspack.runtime_composition import (
        defaultspack_activation_snapshot_loader,
    )
    from ecosystem.defaultspack.domain.runtime_surface_v4 import create_runtime_surface_services
    from tests.test_live_production_v4_dispatch import _CapturedBackend, _bundle_root, _digest
    from tobkiri_host.backends import BackendRegistry
    from tobkiri_host.effects import ProviderOutcome
    from tobkiri_host.runtime import V4DispatchSession

    user_data = tmp_path / "normal-defaults"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    active = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    binding = next(item for item in active.resolved.plan["bindings"]
                   if item["contract_id"] == "conversation.saved-turn.v1")
    principal = FunctionPrincipal.from_dict(binding["function_principal"])
    backend = _CapturedBackend(_digest("normal-saved-coordinator"))
    backend.target_executable_digest = principal.function_implementation_digest
    guest_requests = []

    def guest(envelope):
        guest_requests.append(envelope)
        assert envelope.contract_id == "conversation.saved-turn.v1"
        callback, preflight = backend.saved_callbacks
        preflight(envelope)
        intent = saved.start(envelope.payload["request"])
        for _ in range(4):
            intent = saved.resume(intent["state"], callback(
                envelope, _frame(intent, envelope.context.request_id),
            ))
        if lost_reply == "guest":
            raise RuntimeError("guest reply lost after final owner commit")
        return ProviderOutcome(intent)

    monkeypatch.setattr(backend, "invoke", guest)
    original = V4DispatchSession.invoke
    ai_calls = []

    def invoke(self, contract_id, operation_id, payload, **kwargs):
        if (contract_id, operation_id) == READINESS:
            return {"ready": True, "model_profile_id": "model-profile-1"}
        if (contract_id, operation_id) == saved.TARGETS[2]:
            ai_calls.append(payload)
            return {"status": "ok", "output": "Hi"}
        result = original(self, contract_id, operation_id, payload, **kwargs)
        if (
            lost_reply == "owner" and (contract_id, operation_id) == saved.TARGETS[3]
            and payload.get("operation") == "append_saved"
            and payload["message"]["role"] == "assistant"
        ):
            raise RuntimeError("owner reply lost after final commit")
        return result

    monkeypatch.setattr(V4DispatchSession, "invoke", invoke)
    session = capture_production_dispatch(
        active, bundle_root=_bundle_root(),
        ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
        authority_store=AuthorityStore(user_data / "authority/v4.sqlite3"),
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
        backends=BackendRegistry((backend,)),
        target_backend_digests={principal.principal_id: backend.status.backend_digest},
    )
    store = ConversationStore("defaults", user_data_root=user_data)
    store.create({"id": "conversation-1", "model_reference": "model-profile-1"},
                 expected_revision=0)
    initial = {"_session_id": "session.panel.saved-normal", "request": {
        "turn_id": "turn-1", "conversation_id": "conversation-1",
        "conversation_revision": 1, "content": "Hello",
    }}
    try:
        # Match the production HTTP boundary: each request has its own capture
        # scope, propagated by Broker to nested owner calls. Never share one
        # between the initial submission and its duplicate or extend deadlines.
        with profile_capture_scope():
            result = session.invoke("tobkiri.action.turn.saved.v1",
                                    "rumi_turn_runtime_pack.turn-saved", initial)
        assert result["status"] == (
            "reconciliation_required" if lost_reply else "completed"
        ), result
        with profile_capture_scope():
            repeated = session.invoke("tobkiri.action.turn.saved.v1",
                                      "rumi_turn_runtime_pack.turn-saved", initial)
        if lost_reply:
            assert repeated["status"] == "completed", repeated
            assert repeated["turn"]["result_reference"]["conversation_revision"] == 3
        else:
            assert repeated == {"status": "existing", "turn": result["turn"]}
        assert len(guest_requests) == len(ai_calls) == 1
        assert [item["content"] for item in store.get("conversation-1")["messages"]] == [
            "Hello", "Hi",
        ]
    finally:
        session.close()


def test_user_append_cannot_reparent_the_selected_branch(tmp_path: Path) -> None:
    store, outer, calls, callbacks = _setup(tmp_path)
    for index in (1, 2):
        store.append_message(
            "conversation-1",
            {"id": f"old-{index}", "role": "user", "content": "Earlier", "status": "complete"},
            expected_conversation_revision=index,
        )
    outer.payload["request"]["conversation_revision"] = 3
    intent = saved.start(outer.payload["request"])
    intent = saved.resume(intent["state"], callbacks(outer, _frame(intent)))
    frame = _frame(intent)
    frame["payload"]["message"]["parent_id"] = "old-1"
    before = store.path.read_bytes()
    with pytest.raises(AuthorityDenied, match="selected branch changed"):
        callbacks(outer, frame)
    assert store.path.read_bytes() == before
    assert all(target == saved.TARGETS[0] for target, _ in calls)
