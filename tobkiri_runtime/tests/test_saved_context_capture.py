"""Selected owner code, detached DATA and operation-local fixed borrows.

Accepted global module captures intentionally remain legacy debt. These tests
prove port-reference release, never claim module/Source destruction or funding.
"""

from copy import deepcopy
from contextlib import contextmanager, nullcontext
import ast
import json
from dataclasses import asdict, replace
import shutil
import sys
from types import ModuleType, SimpleNamespace
import weakref

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.host_provider_hooks_v4 import load_host_provider_factory
from core_runtime.bootstrap.saved_bridge import SavedBridgeCallbacks
from tests.saved_context_capture_support import RUNTIME, owners, borrow, operation, binding, context, CONVERSATION, PROJECT
from tests.test_host_extension_update_v4 import _refresh
from tests.test_saved_project_context import project
from tobkiri_host.models import OpaqueAuthorityRef
from core_runtime.global_contract_dispatch import GlobalContractClient
from tobkiri_protocol.saved_conversation import (
    ResolvedSavedConversation, SavedOwnedContextResolution, SavedWorkspaceResolution,
)


def conversation():
    return {"id": "chat-one", "conversation_revision": 1, "conversation_kind": "chat",
            "group_id": "group-one", "metadata": {"group_id": "group-one"}, "messages": []}


def read_project(_payload):
    assert _payload == {"profile_id": "defaults"}
    return {"namespace": "defaultspack.projects.v1", "revision": 1, "projects": [project()]}


def plain(value):
    """Inspect the complete returned value graph without following code globals."""
    if type(value) is ResolvedSavedConversation:
        plain(dict(value))
        plain(value.owned_context_owner)
        assert type(value.owned_context_resolution) is SavedOwnedContextResolution
        plain(asdict(value.owned_context_resolution))
        if value.workspace_resolution is not None:
            assert type(value.workspace_resolution) is SavedWorkspaceResolution
            plain(asdict(value.workspace_resolution))
        return
    if value is None or type(value) in (str, bool, int, float):
        return
    assert type(value) in (dict, tuple, list), type(value)
    if type(value) is dict:
        assert all(type(key) is str for key in value)
        values = value.values()
    else:
        values = value
    for item in values:
        plain(item)


def copied_owners(tmp_path):
    store = tmp_path / "store" / "rumi_conversation_store_pack"
    project_root = tmp_path / "project" / "tobkiri_ui_settings_pack"
    shutil.copytree(RUNTIME / "ecosystem/rumi_conversation_store_pack", store)
    shutil.copytree(RUNTIME / "ecosystem/tobkiri_ui_settings_pack", project_root)
    return store, project_root


def test_real_loader_closes_helper_globals_against_canonical_poison_and_disk_changes(tmp_path, monkeypatch):
    store, project_root = copied_owners(tmp_path)
    selected = owners(conversation_root=store, project_root=project_root)
    for name in (
        "ecosystem.rumi_conversation_store_pack.runtime.saved_project_context",
        "ecosystem.rumi_conversation_store_pack.runtime.chat_reference",
        "ecosystem.tobkiri_ui_settings_pack.runtime.projects",
    ):
        poison = ModuleType(name)
        def unavailable(*args, **kwargs):
            raise AssertionError("ambient helper was consulted")
        poison._membership = poison.identifier = poison._projects = poison.BorrowedSavedContext = unavailable
        monkeypatch.setitem(sys.modules, name, poison)
    for root, relative in ((store, "runtime/saved_project_context.py"),
                           (store, "runtime/chat_reference.py"), (project_root, "runtime/projects.py")):
        (root / relative).write_text("raise AssertionError('changed owner source')\n")
    owner = operation(selected)
    with borrow(selected, operation=owner, read_project=read_project) as port:
        policy = sys.modules[type(port).__module__]
        store_namespace = type(selected[3][0][2]).__module__.split(".")[0]
        project_namespace = type(selected[3][1][2]).__module__.split(".")[0]
        assert policy._membership.__module__ == store_namespace + ".runtime.chat_reference"
        assert policy.identifier.__globals__ is policy._membership.__globals__
        validator = selected[3][1][2].saved_snapshot_validator()
        assert validator.__module__ == project_namespace + ".runtime.projects"
        assert validator.__globals__["_workspace_bindings"].__globals__ is validator.__globals__
        data = port.resolve(owner, conversation())
    assert data.owned_context_owner["project_title"] == "Group"
    plain(data)


def test_actual_selected_owner_revision_a_to_b_never_mixes_helper_globals(tmp_path):
    store, project_root = copied_owners(tmp_path)
    first = owners(conversation_root=store, project_root=project_root)
    source = store / "runtime/saved_project_context.py"
    original = source.read_text()
    changed = original.replace('"project_title": project["title"],', '"project_title": "B:" + project["title"],')
    assert changed != original
    source.write_text(changed)
    _refresh(store)
    second_binding = binding("rumi_conversation_store_pack", first[0].function.function_id,
                             CONVERSATION, root=store)
    second_factory = load_host_provider_factory(store, second_binding)
    second = (second_binding, first[1], first[2], [
        (second_binding.function.function_id, (second_binding,), second_factory, "test-adapter"), first[3][1],
    ])
    assert first[0].artifact.digest != second[0].artifact.digest
    assert type(first[3][0][2]).__module__ != type(second_factory).__module__
    owner_a, owner_b = operation(first), operation(second)
    with borrow(first, operation=owner_a, read_project=read_project) as port:
        data_a = port.resolve(owner_a, conversation())
    with borrow(second, operation=owner_b, read_project=read_project) as port:
        data_b = port.resolve(owner_b, conversation())
    assert data_a.owned_context_owner["project_title"] == "Group"
    assert data_b.owned_context_owner["project_title"] == "B:Group"
    current = True
    def guard():
        if not current:
            raise AuthorityDenied("captured Profile activation is stale")
    with borrow(first, operation=owner_a, read_project=read_project, guard=guard) as port:
        current = False
        with pytest.raises(AuthorityDenied, match="stale"):
            port.resolve(owner_a, conversation())


@pytest.mark.parametrize("consumer", ["turn", "bridge"])
def test_no_project_and_clear_need_no_project_code_binding_or_presentation_identity(consumer):
    selected = owners(project_capture=False)
    if consumer == "bridge":
        bridge = binding("tobkiri_conversation_orchestration_pack", "tobkiri_conversation_orchestration_pack.saved",
                         ("conversation.saved-turn.v1", "saved_complete"))
        selected = (selected[0], selected[1], bridge, selected[3])
    owner = operation(selected)
    def unused(*args):
        raise AssertionError("NoProject attempted Project code/read/identity")
    for metadata in ({}, {"workspace_id": "workspace-one", "workspace_bindings": [{
        "workspace_id": "workspace-one", "workspace_label": "One", "workspace_root": "/inert",
    }]}):
        value = {**conversation(), "group_id": None, "metadata": metadata}
        with borrow(selected, operation=owner, read_project=unused, owner=unused) as port:
            data = port.resolve(owner, value)
        assert dict(data) == value
    with borrow(selected, operation=owner, read_project=unused, owner=unused) as port:
        with pytest.raises(AuthorityDenied, match="selected owner"):
            port.resolve(owner, conversation())


@pytest.mark.parametrize("change", ["principal", "profile", "consumer"])
def test_fixed_borrow_rejects_other_principal_profile_or_consumer(change):
    selected = owners()
    owner = operation(selected)
    if change == "principal":
        owner.target_principal = OpaqueAuthorityRef("authority:other")
    elif change == "profile":
        owner.context.profile_id = "other"
    else:
        consumer = replace(selected[2], function=replace(selected[2].function, function_id="other.saved"))
        selected = (selected[0], selected[1], consumer, selected[3])
    with pytest.raises(AuthorityDenied, match="owner changed|consumer"):
        with borrow(selected, operation=owner, read_project=read_project):
            raise AssertionError("unrelated operation received a port")


def test_data_is_detached_wire_copy_has_no_proof_and_port_reference_ends_with_operation(monkeypatch):
    selected = owners()
    owner = operation(selected)
    import core_runtime.host_provider_hooks_v4 as hooks
    def extra_capture(*args, **kwargs):
        raise AssertionError("a borrow started another loader capture")
    monkeypatch.setattr(hooks, "load_host_provider_factory", extra_capture)
    monkeypatch.setattr(hooks.CapturedProviderModules, "for_capture", extra_capture)
    with borrow(selected, operation=owner, read_project=read_project) as port:
        reference = weakref.ref(port)
        data = port.resolve(owner, conversation())
        plain(data)
        with pytest.raises(PermissionError, match="operation changed"):
            port.resolve(operation(selected), conversation())
        with pytest.raises(ValueError, match="effect conversation"):
            port.retain_effect(owner, conversation(), dict(data))
    with pytest.raises(PermissionError, match="closed"):
        port.resolve(owner, conversation())
    del port
    assert reference() is None
    # DATA still works after this port ends. Existing accepted global captures
    # remain intentionally: this is not a fabricated Source-destructor proof.
    plain(data)
    assert type(selected[3][0][2]).__module__.split(".")[0] in sys.modules


def test_code_bearing_nested_values_are_rejected_before_project_read():
    selected = owners()
    owner = operation(selected)
    value = deepcopy(conversation())
    value["metadata"]["hidden_callback"] = lambda: None
    def unused(*args):
        raise AssertionError("invalid DATA reached Project read")
    with borrow(selected, operation=owner, read_project=unused) as port:
        with pytest.raises(ValueError, match="non-builtin DATA"):
            port.resolve(owner, value)
    with borrow(selected, operation=owner, read_project=read_project) as port:
        accepted = port.resolve(owner, conversation())
        accepted.owned_context_owner["hidden_callback"] = lambda: None
        with pytest.raises(ValueError, match="non-builtin DATA"):
            port.retain_effect(owner, conversation(), accepted)


def test_real_turn_factory_consumes_the_fixed_context_borrow_and_releases_it(tmp_path):
    from tests.test_saved_turn_coordinator import _Session
    from ecosystem.rumi_turn_runtime_pack.runtime.saved import SAVED_CONTRACTS
    from ecosystem.tobkiri_ui_settings_pack.runtime.projects import ProjectStateStore

    selected = owners()
    session = _Session(tmp_path)
    request = session.initial["request"]
    updated = session.conversations.update(request["conversation_id"], {
        "group_id": "group-one", "metadata": {"group_id": "group-one"},
    }, expected_conversation_revision=1)["conversation"]
    request["conversation_revision"] = updated["conversation_revision"]
    ProjectStateStore(tmp_path, "defaults", "shell.tauri.default").replace(
        projects=[project()], expected_revision=0, mutation_id="create", migration_digest=None)
    reader = selected[3][1][2].capture(context(selected[1], tmp_path)).contributions[0]
    original = session.invoke
    reads = []
    def invoke(contract, op, payload, **kwargs):
        if (contract, op) == PROJECT:
            reads.append(payload)
            return reader.invoke(op, payload, invocation)
        return original(contract, op, payload, **kwargs)
    session.invoke = invoke
    client = GlobalContractClient(session=session, allowed_contract_ids=SAVED_CONTRACTS,
                                  consumer_pack_id="rumi_turn_runtime_pack")
    invocation = SimpleNamespace(envelope=operation(selected), assert_current=lambda: None,
        presentation_owner_principal_id="shell.tauri.default", presentation_owner_session_id="session-one",
        contract_client=lambda **kwargs: client, cancellation=SimpleNamespace(track=lambda _: nullcontext()))
    references = []
    @contextmanager
    def fixed_borrow(current):
        with borrow(selected, operation=current,
                    read_project=lambda payload: invoke(*PROJECT, payload)) as port:
            references.append(weakref.ref(port))
            yield port
    turn_factory = load_host_provider_factory(RUNTIME / "ecosystem/rumi_turn_runtime_pack", selected[2])
    captured = turn_factory.capture(replace(context(selected[2], tmp_path), borrow_saved_context=fixed_borrow))
    try:
        result = captured.contributions[0].invoke(selected[2].operation.operation_id, session.initial, invocation)
    finally:
        captured.close()
    assert result["status"] == "completed"
    actual = session.conversations.get(request["conversation_id"])
    assert actual["group_id"] == "group-one"
    assert [item["content"] for item in actual["messages"]] == ["Hello", "Hi"]
    assert reads and references and all(reference() is None for reference in references)


def legacy_exchange(tmp_path, patch):
    """Select a real indexed old owner with neither hook nor resolver sidecar."""
    from tests.test_saved_host_exchange import _Exchange
    from tests.test_saved_bridge_callbacks import saved
    from tobkiri_host.saved_workspace_context import WORKSPACE_TARGET
    from ecosystem.rumi_workspace_mount_pack.runtime.mounts import WorkspaceMountStore, capture_workspace_binding

    root = tmp_path / "legacy" / "rumi_conversation_store_pack"
    shutil.copytree(RUNTIME / "ecosystem/rumi_conversation_store_pack", root)
    source = root / "runtime/host.py"
    lines = source.read_text().splitlines(keepends=True)
    tree = ast.parse("".join(lines))
    owner_class = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                       and node.name == "ConversationReadHostFactoryV4")
    hook = next(node for node in owner_class.body if isinstance(node, ast.FunctionDef)
                and node.name == "borrow_saved_context")
    del lines[hook.lineno - 1:hook.end_lineno]
    source.write_text("".join(lines))
    relative = "runtime/saved_project_context.py"
    for name in ("pack.v4.json", "artifact-index.v4.json"):
        path = root / name
        document = json.loads(path.read_text())
        document["artifacts"] = [item for item in document["artifacts"] if item["path"] != relative]
        if name == "pack.v4.json":
            document["pack"]["version"] = "1.0.0"
        path.write_text(json.dumps(document, sort_keys=True) + "\n")
    (root / relative).unlink()
    _refresh(root)
    selected = owners(conversation_root=root, project_capture=False)
    assert not hasattr(selected[3][0][2], "borrow_saved_context")
    exchange = _Exchange(tmp_path)
    original = exchange.callback
    updated = exchange.store.update("conversation-1", patch, expected_conversation_revision=1)["conversation"]
    exchange.outer.payload["request"]["conversation_revision"] = updated["conversation_revision"]
    exchange.intent = saved.start(exchange.outer.payload["request"])
    exchange.outer.target_principal = selected[2].principal_ref
    exchange.outer.contract_id = selected[2].operation.contract_id
    exchange.outer.operation_id = selected[2].operation.operation_id
    reader = selected[3][0][2].capture(context(selected[0], tmp_path))
    mount_root = tmp_path / "workspace"
    mount_root.mkdir()
    mounts = WorkspaceMountStore("defaults", user_data_root=tmp_path)
    mounts.mount("workspace-one", str(mount_root), expected_revision=0,
                 metadata={"directory_identity": [mount_root.stat().st_dev, mount_root.stat().st_ino]})
    workspace_reads = []
    def unused(*args):
        raise AssertionError("legacy NoProject/unsupported context attempted Project lookup")
    def dispatch(outer, target, payload):
        if target == saved.TARGETS[0]:
            return {"status": "ok", "value": reader.contributions[0].invoke(
                target[1], {"profile_id": "defaults", **payload}, None)}
        if target == WORKSPACE_TARGET:
            workspace_reads.append(payload["workspace_id"])
            fact = capture_workspace_binding("defaults", payload["workspace_id"], user_data_root=tmp_path)
            return {"status": "ok", "value": {"mount": mounts.get(payload["workspace_id"]),
                "binding": {name: getattr(fact, name) for name in (
                    "workspace_id", "mount_revision", "root_st_dev", "root_st_ino")}}}
        if target == saved.TARGETS[1]:
            return {"status": "ok", "value": exchange.store.append_message(payload["conversation_id"],
                payload["message"], expected_conversation_revision=payload["expected_conversation_revision"],
                saved_input=outer.payload)}
        return original._dispatch(outer, target, payload)
    callbacks = SavedBridgeCallbacks(dispatch, lambda outer, targets: None,
        borrow_saved_context=lambda outer: borrow(selected, operation=outer, read_project=unused, owner=unused))
    exchange.callback = lambda outer, wire: callbacks(outer, exchange.host.callback_frame(exchange.host._pending))
    return exchange, updated, workspace_reads, reader.close


@pytest.mark.parametrize("coding", [False, True])
def test_real_legacy_owner_without_hook_preserves_plain_chat_and_canonical_coding(tmp_path, coding):
    patch = {"group_id": None, "conversation_kind": "coding" if coding else "chat",
             "metadata": {"mode": "coding", "workspace_id": "workspace-one"} if coding else {}}
    exchange, before, workspace_reads, close = legacy_exchange(tmp_path, patch)
    try:
        for _ in range(4):
            exchange.step()
        exchange.host.finish(exchange.intent)
        actual = exchange.store.get("conversation-1")
    finally:
        close()
    assert actual["id"] == before["id"] and actual["group_id"] is None
    assert actual["metadata"] == before["metadata"]
    assert [item["content"] for item in actual["messages"]] == ["Hello", "Hi"]
    assert set(workspace_reads) == ({"workspace-one"} if coding else set())


@pytest.mark.parametrize("owned", ["group", "alias", "bindings", "root"])
def test_real_legacy_owner_without_hook_rejects_required_owned_context_without_data_loss(tmp_path, owned):
    patch = {"group_id": None, "metadata": {}}
    if owned == "group":
        patch.update(group_id="group-one", metadata={"group_id": "group-one"})
    elif owned == "alias":
        patch["metadata"] = {"groupId": "group-one"}
    elif owned == "bindings":
        patch.update(conversation_kind="coding", metadata={"mode": "coding", "workspace_id": "workspace-one",
            "workspace_bindings": [{"workspace_id": "workspace-one", "workspace_label": "One", "workspace_root": "/inert"}]})
    else:
        patch["metadata"] = {"workspace_root": "/inert"}
    exchange, before, _workspace_reads, close = legacy_exchange(tmp_path, patch)
    try:
        with pytest.raises(AuthorityDenied, match="owned context resolution"):
            exchange.step()
    finally:
        close()
    actual = exchange.store.get("conversation-1")
    assert actual == before and actual["messages"] == []
