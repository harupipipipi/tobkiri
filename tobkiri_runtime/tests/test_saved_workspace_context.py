from dataclasses import replace

import pytest

from tobkiri_protocol.saved_conversation import (
    validate_saved_conversation_context,
)
from tobkiri_host.saved_workspace_context import (
    SavedWorkspacePins,
    resolve_saved_workspace,
    workspace_resolution,
    recheck_saved_workspace,
)


def conversation(revision=1):
    return {
        "id": "chat-1",
        "conversation_revision": revision,
        "metadata": {"workspace_id": "workspace-1"},
        "tags": [],
    }


def binding(payload):
    assert payload == {
        "profile_id": "defaults",
        "operation": "binding",
        "workspace_id": "workspace-1",
    }
    return {
        "mount": {"id": "workspace-1", "mount_revision": 2},
        "binding": {
            "workspace_id": "workspace-1",
            "mount_revision": 2,
            "root_st_dev": 3,
            "root_st_ino": 4,
        },
    }


def resolve(value, read=binding):
    return resolve_saved_workspace(
        value, profile_id="defaults", read_binding=read, guard=lambda: None
    )


def test_private_projection_preserves_metadata_and_rejects_wire_copy():
    source = conversation()
    result = resolve(source)
    assert result == source
    validate_saved_conversation_context(
        result,
        workspace_resolution=workspace_resolution(result),
    )
    with pytest.raises(ValueError):
        validate_saved_conversation_context(dict(result))
    with pytest.raises(ValueError):
        validate_saved_conversation_context(source, workspace_resolution=True)


@pytest.mark.parametrize(
    "field",
    [
        "workspaceId",
        "workspaceRoot",
        "rootPath",
        "workspace_root",
        "rumi_data_path",
        "groupId",
    ],
)
def test_private_resolution_does_not_admit_legacy_or_other_context(field):
    value = conversation()
    value["metadata"][field] = "legacy"
    with pytest.raises(ValueError):
        resolve(value)


@pytest.mark.parametrize("field", ["workspace_id", "mount_revision", "root_st_dev"])
def test_fresh_identity_change_fenced_before_write(field):
    old = resolve(conversation())

    def changed(payload):
        value = binding(payload)
        if field == "workspace_id":
            value["binding"][field] = "other"
        elif field == "mount_revision":
            value["binding"][field] = 5
            value["mount"][field] = 5
        else:
            value["binding"][field] = 5
        return value

    with pytest.raises(ValueError):
        fresh = resolve(conversation(), changed)
        recheck_saved_workspace(old, fresh)


def test_exchange_pin_retains_mount_across_actual_owner_revision_advances():
    pin = SavedWorkspacePins()
    old = resolve(conversation())
    pin.accept(workspace_resolution(old))
    fresh = resolve(conversation(2))
    pin.accept(workspace_resolution(fresh))
    with pytest.raises(ValueError):
        recheck_saved_workspace(old, fresh)
    pin.accept(workspace_resolution(resolve(conversation(3))))
    with pytest.raises(ValueError):
        pin.accept(replace(workspace_resolution(fresh), root_st_ino=999))


def test_guard_rechecked_after_owner_read():
    events = []

    def read(payload):
        events.append("read")
        return binding(payload)

    resolve_saved_workspace(
        conversation(),
        profile_id="defaults",
        read_binding=read,
        guard=lambda: events.append("guard"),
    )
    assert events == ["guard", "read", "guard"]


def _actual_owner_setup(tmp_path):
    from tests.test_saved_bridge_callbacks import _setup
    from core_runtime.bootstrap.saved_bridge import SavedBridgeCallbacks
    from ecosystem.rumi_workspace_mount_pack.runtime.mounts import (
        WorkspaceMountStore,
        capture_workspace_binding,
    )
    from tobkiri_host.saved_workspace_context import WORKSPACE_TARGET

    store, outer, calls, original = _setup(tmp_path)
    root = tmp_path / "mounted"
    root.mkdir()
    mounts = WorkspaceMountStore("defaults", user_data_root=tmp_path)
    mounts.mount(
        "workspace-1",
        str(root),
        expected_revision=0,
        metadata={"directory_identity": [root.stat().st_dev, root.stat().st_ino]},
    )
    store.update(
        "conversation-1",
        {"metadata": {"workspace_id": "workspace-1"}},
        expected_conversation_revision=1,
    )
    outer.payload["request"]["conversation_revision"] = 2

    def dispatch(envelope, target, payload):
        if target != WORKSPACE_TARGET:
            return original._dispatch(envelope, target, payload)
        fact = capture_workspace_binding(
            "defaults", "workspace-1", user_data_root=tmp_path
        )
        return {
            "status": "ok",
            "value": {
                "mount": mounts.get("workspace-1"),
                "binding": {
                    field: getattr(fact, field)
                    for field in (
                        "workspace_id",
                        "mount_revision",
                        "root_st_dev",
                        "root_st_ino",
                    )
                },
            },
        }

    callbacks = SavedBridgeCallbacks(dispatch, lambda envelope, targets: None)
    return store, outer, callbacks, mounts, root


def test_actual_owner_preflight_and_saved_append_keep_workspace_metadata(tmp_path):
    from tests.test_saved_bridge_callbacks import _frame, saved
    from tobkiri_host.saved_turn_plan import SavedToolFrame

    store, outer, callbacks, mounts, root = _actual_owner_setup(tmp_path)
    callbacks.preflight(outer)
    assert store.get("conversation-1")["messages"] == []
    pin = SavedWorkspacePins()
    intent = saved.start(outer.payload["request"])
    for stage in ["read", "user", "ai", "assistant"]:
        frame = SavedToolFrame(
            _frame(intent), None, b"[]", stage, None, None, workspace_context_owner=pin
        )
        intent = saved.resume(intent["state"], callbacks(outer, frame))
    assert intent["status"] == "ok"
    result = store.get("conversation-1")
    assert result["metadata"] == {"workspace_id": "workspace-1"}
    assert [m["content"] for m in result["messages"]] == ["Hello", "Hi"]


def test_actual_owner_remount_between_read_and_user_prevents_append(tmp_path):
    from tests.test_saved_bridge_callbacks import _frame, saved
    from tobkiri_host.saved_turn_plan import SavedToolFrame
    from core_runtime.authority.v4 import AuthorityDenied

    store, outer, callbacks, mounts, root = _actual_owner_setup(tmp_path)
    pin = SavedWorkspacePins()
    intent = saved.start(outer.payload["request"])
    frame = SavedToolFrame(
        _frame(intent), None, b"[]", "read", None, None, workspace_context_owner=pin
    )
    intent = saved.resume(intent["state"], callbacks(outer, frame))
    mounts.mount("workspace-1", str(root), expected_revision=1)
    with pytest.raises(AuthorityDenied, match="mount changed"):
        callbacks(
            outer,
            SavedToolFrame(
                _frame(intent),
                None,
                b"[]",
                "user",
                None,
                None,
                workspace_context_owner=pin,
            ),
        )
    assert store.get("conversation-1")["messages"] == []


def test_actual_owner_replaced_root_rejected_before_any_saved_append(tmp_path):
    store, outer, callbacks, mounts, root = _actual_owner_setup(tmp_path)
    root.rename(root.with_name("old"))
    root.mkdir()
    with pytest.raises(PermissionError, match="changed"):
        callbacks.preflight(outer)
    assert store.get("conversation-1")["messages"] == []


def test_retained_exchange_populates_one_private_pin_owner_for_all_frames():
    from types import SimpleNamespace
    from tobkiri_host.continuation_chain import ChainIdentity, ContinuationChains
    from tobkiri_host.saved_host_exchange import SavedHostExchange
    from tobkiri_protocol.canonical import canonical_json

    exchange = SavedHostExchange(
        ChainIdentity("domain", "request", "sha256:" + "a" * 64, 60),
        request_digest="sha256:" + "b" * 64,
        artifact_identity="artifact",
        deadline_text="60",
        chains=ContinuationChains(),
    )
    first = exchange.callback_frame(SimpleNamespace(frame=canonical_json({"hop": 0})))
    second = exchange.callback_frame(SimpleNamespace(frame=canonical_json({"hop": 1})))
    assert type(first.workspace_context_owner) is SavedWorkspacePins
    assert first.workspace_context_owner is second.workspace_context_owner
    assert "workspace_context_owner" not in first.frame


def test_wire_frame_without_private_exchange_owner_cannot_append(tmp_path):
    from tests.test_saved_bridge_callbacks import _frame, saved
    from core_runtime.authority.v4 import AuthorityDenied

    store, outer, callbacks, mounts, root = _actual_owner_setup(tmp_path)
    intent = saved.start(outer.payload["request"])
    with pytest.raises(AuthorityDenied, match="retained Host exchange"):
        callbacks(outer, _frame(intent))
    assert store.get("conversation-1")["messages"] == []


def test_exchange_pin_cannot_add_or_remove_workspace_association():
    pin = SavedWorkspacePins()
    pin.accept(workspace_resolution(resolve(conversation())))
    with pytest.raises(ValueError):
        pin.accept(None)
    ordinary = SavedWorkspacePins()
    ordinary.accept(None)
    ordinary.accept(None)
    with pytest.raises(ValueError):
        ordinary.accept(workspace_resolution(resolve(conversation())))


@pytest.mark.parametrize("mode", ["ask", "agent", "full"])
def test_composed_mode_is_preserved_by_real_owner_user_and_assistant_ack(
    tmp_path, mode
):
    from tests.test_saved_bridge_callbacks import _frame, saved
    from tobkiri_host.saved_turn_plan import SavedToolFrame

    store, outer, callbacks, mounts, root = _actual_owner_setup(tmp_path)
    outer.payload["request"]["action_approval_mode"] = mode
    pin = SavedWorkspacePins()
    intent = saved.start(outer.payload["request"])
    for stage in ["read", "user", "ai", "assistant"]:
        frame = SavedToolFrame(
            _frame(intent), None, b"[]", stage, None, None, workspace_context_owner=pin
        )
        intent = saved.resume(intent["state"], callbacks(outer, frame))
    assert intent["status"] == "ok"
    for message in store.get("conversation-1")["messages"]:
        assert message["metadata"]["action_approval_mode"] == mode


def test_calendar_reserved_workspace_owner_failure_does_not_claim_or_append(tmp_path):
    from tests.test_saved_turn_guidance_owner import _OwnerSession, _client
    from ecosystem.rumi_turn_runtime_pack.runtime.saved import execute_saved_turn
    from tobkiri_host.saved_workspace_context import WORKSPACE_TARGET

    session = _OwnerSession(tmp_path)
    session.conversations.update(
        "conversation-1",
        {"metadata": {"workspace_id": "workspace-1"}},
        expected_conversation_revision=1,
    )
    task = {
        "profile_id": "defaults",
        "conversation_id": "conversation-1",
        "message": "Scheduled",
        "workspace_id": "workspace-1",
    }
    session.turns.reserve_calendar_preparation(
        occurrence_key="occurrence",
        source=task,
        turn_id="calendar-test",
        conversation_id="conversation-1",
        conversation_revision=2,
    )
    payload = {
        "request": {
            "turn_id": "calendar-test",
            "conversation_id": "conversation-1",
            "conversation_revision": 2,
            "content": "Scheduled",
        }
    }
    session.turns.bind_calendar_preparation(
        occurrence_key="occurrence",
        source=task,
        payload=payload,
    )
    original = session.invoke

    def unavailable(contract, operation, request, **options):
        if (contract, operation) == WORKSPACE_TARGET:
            raise PermissionError("workspace owner is unavailable")
        return original(contract, operation, request, **options)

    session.invoke = unavailable
    with pytest.raises(PermissionError):
        execute_saved_turn(
            session.turns, payload, client=_client(session), guard=lambda: None
        )
    assert session.turns.get("calendar-test")["status"] == "queued"
    assert session.model_inputs == []
    assert session.conversations.get("conversation-1")["messages"] == []
    released = session.turns.release_calendar_preparation(
        occurrence_key="occurrence",
        source=task,
        turn_id="calendar-test",
    )
    assert released["status"] == "released"
