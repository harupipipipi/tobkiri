"""Project membership through real owners and the saved Host/guest exchange.

Broker transport and AI remain explicit adapters, matching the existing saved
exchange fixtures. No model or filesystem tool is started by these tests.
"""
from copy import deepcopy
import hashlib
from types import SimpleNamespace

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.bootstrap.saved_bridge import SavedBridgeCallbacks
from core_runtime.global_contract_dispatch import GlobalContractClient, GlobalContractUnavailable
from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationConflict
from ecosystem.rumi_workspace_mount_pack.runtime.mounts import (
    WorkspaceMountStore, capture_workspace_binding,
)
from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
from ecosystem.rumi_turn_runtime_pack.runtime.saved import SAVED_CONTRACTS, validate_saved_turn_start
from ecosystem.tobkiri_ui_settings_pack.runtime.projects import (
    ProjectStateStore, READ_OPERATION_ID, _projects,
)
from ecosystem.rumi_conversation_store_pack.runtime.saved_project_context import (
    PROJECT_TARGET, project_context, resolve_saved_project,
    retain_saved_project_effect_snapshot,
)
from tests.test_project_state_owner import _Invocation
from tests.saved_context_capture_support import owners, borrow, context
from tests.test_saved_host_exchange import _Exchange
from tests.test_saved_bridge_callbacks import saved
from tobkiri_host.saved_workspace_context import (
    WORKSPACE_TARGET, SavedWorkspaceExecutionBinding, SavedWorkspaceExecutionRegistry,
    resolve_saved_workspace, workspace_resolution,
)
from tobkiri_protocol.canonical import canonical_json
from tobkiri_protocol.conversation_context import (
    context_binding, resolve_linked_conversation, resolve_request_context,
)
from tobkiri_protocol.saved_conversation import validate_saved_conversation_context


def project(primary=None, bindings=None, title="Group"):
    chosen = next((item for item in bindings or [] if item["workspace_id"] == primary), {})
    value = {
        "id": "group-one", "title": title, "workspace_id": primary,
        "workspace_label": chosen.get("workspace_label"),
        "workspace_root": chosen.get("workspace_root"), "rumi_data_path": None,
    }
    if bindings is not None:
        value["workspace_bindings"] = deepcopy(bindings)
    return value


def setup(tmp_path, *, execution_workspace=None, project_primary=None):
    exchange = _Exchange(tmp_path)
    original = exchange.callback
    roots = []
    mounts = WorkspaceMountStore("defaults", user_data_root=tmp_path)
    for index, identifier in enumerate(("workspace-one", "workspace-two")):
        root = tmp_path / identifier
        root.mkdir()
        mounts.mount(identifier, str(root), expected_revision=index,
                     metadata={"directory_identity": [root.stat().st_dev, root.stat().st_ino]})
        roots.append({"workspace_id": identifier, "workspace_label": identifier,
                      "workspace_root": str(root)})
    projects = ProjectStateStore(tmp_path, "defaults", "shell.tauri.default")
    projects.replace(projects=[project(project_primary, roots if project_primary else None)],
                     expected_revision=0, mutation_id="create", migration_digest=None)
    metadata = {"group_id": "group-one"}
    if execution_workspace:
        metadata.update(mode="coding", workspace_id=execution_workspace,
                        workspace_bindings=deepcopy(roots))
    updated = exchange.store.update(
        "conversation-1", {"group_id": "group-one", "metadata": metadata,
                           "conversation_kind": "coding" if execution_workspace else "chat"},
        expected_conversation_revision=1,
    )["conversation"]
    exchange.outer.payload["request"]["conversation_revision"] = updated["conversation_revision"]
    exchange.intent = saved.start(exchange.outer.payload["request"])
    selected = owners()
    exchange.outer.target_principal = selected[2].principal_ref
    exchange.outer.contract_id = selected[2].operation.contract_id
    exchange.outer.operation_id = selected[2].operation.operation_id
    reader = selected[3][1][2].capture(context(selected[1], tmp_path)).contributions[0]
    invocation = _Invocation()
    observed = {"workspaces": [], "projects": 0}
    hooks = {}

    def read_project(payload):
        observed["projects"] += 1
        before = hooks.get("before_project_read")
        if before:
            before()
        return reader.invoke(READ_OPERATION_ID, payload, invocation)

    def dispatch(outer, target, payload):
        if target == PROJECT_TARGET:
            return {"status": "ok", "value": read_project({"profile_id": "defaults", **payload})}
        if target == WORKSPACE_TARGET:
            identifier = payload["workspace_id"]
            observed["workspaces"].append(identifier)
            fact = capture_workspace_binding("defaults", identifier, user_data_root=tmp_path)
            return {"status": "ok", "value": {
                "mount": mounts.get(identifier), "binding": {
                    field: getattr(fact, field) for field in (
                        "workspace_id", "mount_revision", "root_st_dev", "root_st_ino",
                    )
                },
            }}
        if target == saved.TARGETS[1] and hooks.get("before_append"):
            hooks["before_append"]()
        if target == saved.TARGETS[1]:
            return {"status": "ok", "value": exchange.store.append_message(
                payload["conversation_id"], payload["message"],
                expected_conversation_revision=payload["expected_conversation_revision"],
                saved_input=outer.payload,
            )}
        return original._dispatch(outer, target, payload)

    callbacks = SavedBridgeCallbacks(
        dispatch, lambda outer, targets: None,
        borrow_saved_context=lambda outer: borrow(selected, operation=outer, read_project=read_project,
            owner=lambda: (invocation.presentation_owner_principal_id, invocation.presentation_owner_session_id)),
    )

    def callback(outer, _wire_frame):
        # _Exchange has already authenticated this exact pending frame. Use
        # its real nonserialized branch and workspace-pin owner, as production.
        return callbacks(outer, exchange.host.callback_frame(exchange.host._pending))

    exchange.callback = callback
    return SimpleNamespace(exchange=exchange, callbacks=callbacks, projects=projects,
                           mounts=mounts, roots=roots, invocation=invocation,
                           read_project=read_project, observed=observed, hooks=hooks, selected=selected)


def finish(fixture):
    for _ in range(4):
        fixture.exchange.step()
    fixture.exchange.host.finish(fixture.exchange.intent)
    return fixture.exchange.store.get("conversation-1")


def resolve(fixture, value=None):
    conversation = value or fixture.exchange.store.get("conversation-1")
    with borrow(fixture.selected, operation=fixture.exchange.outer, read_project=fixture.read_project,
                owner=lambda: (fixture.invocation.presentation_owner_principal_id,
                               fixture.invocation.presentation_owner_session_id)) as resolver:
        return resolver.resolve(fixture.exchange.outer, conversation)


def test_group_chat_saves_in_the_same_conversation_and_membership(tmp_path):
    fixture = setup(tmp_path)
    result = finish(fixture)
    assert result["id"] == "conversation-1" and result["group_id"] == "group-one"
    assert [message["content"] for message in result["messages"]] == ["Hello", "Hi"]
    assert fixture.observed["projects"] > 0 and fixture.observed["workspaces"] == []


@pytest.mark.parametrize("context", ["group-bound", "group-unbound", "none-unbound"])
def test_turn_owner_resolves_group_before_claim_without_requiring_project_for_no_group(tmp_path, context):
    fixture = setup(tmp_path)
    if context == "none-unbound":
        fixture.exchange.store.update("conversation-1", {"group_id": None, "metadata": {}},
                                      expected_conversation_revision=2)
        fixture.exchange.outer.payload["request"]["conversation_revision"] = 3

    def invoke(contract, operation, payload):
        if (contract, operation) == PROJECT_TARGET and context != "group-bound":
            raise GlobalContractUnavailable("Project read is unbound")
        outcome = fixture.callbacks._dispatch(fixture.exchange.outer, (contract, operation), payload)
        assert outcome["status"] == "ok"
        return outcome["value"]

    session = SimpleNamespace(
        profile_id="defaults", plan_digest="captured-test-adapter",
        presentation_owner_principal_id="shell.tauri.default",
        presentation_owner_session_id="session-one", invoke=invoke,
        provider_metadata=lambda _: (),
    )
    client = GlobalContractClient(session=session, allowed_contract_ids=SAVED_CONTRACTS,
                                  consumer_pack_id="rumi_turn_runtime_pack")
    turns = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    if context == "group-unbound":
        with pytest.raises(GlobalContractUnavailable, match="unbound"):
            with borrow(fixture.selected, operation=fixture.exchange.outer, read_project=lambda payload: invoke(*PROJECT_TARGET, payload)) as resolver:
                validate_saved_turn_start(turns, fixture.exchange.outer.payload, client=client,
                                          guard=lambda: None, context_resolver=resolver, context_owner=fixture.exchange.outer)
    else:
        with borrow(fixture.selected, operation=fixture.exchange.outer, read_project=lambda payload: invoke(*PROJECT_TARGET, payload)) as resolver:
            assert validate_saved_turn_start(turns, fixture.exchange.outer.payload, client=client,
                guard=lambda: None, context_resolver=resolver, context_owner=fixture.exchange.outer) == fixture.exchange.outer.payload
    assert not turns.path.exists()
    assert fixture.exchange.store.get("conversation-1")["messages"] == []
    assert fixture.observed["projects"] == (1 if context == "group-bound" else 0)


@pytest.mark.parametrize("execution", ["workspace-one", "workspace-two"])
def test_project_preserves_all_ordered_bindings_and_the_conversation_execution_primary(tmp_path, execution):
    fixture = setup(tmp_path, execution_workspace=execution, project_primary="workspace-one")
    before = fixture.exchange.store.get("conversation-1")["metadata"]
    facts = project_context(resolve(fixture))
    assert facts["project_primary_workspace_id"] == "workspace-one"
    assert facts["conversation_workspace_id"] == execution
    assert [item[0] for item in facts["project_bindings"]] == ["workspace-one", "workspace-two"]
    assert [item[0] for item in facts["conversation_bindings"]] == ["workspace-one", "workspace-two"]
    result = finish(fixture)
    assert result["metadata"] == before
    assert set(fixture.observed["workspaces"]) == {execution}


def test_folderless_group_keeps_the_conversations_existing_workspace_proof(tmp_path):
    fixture = setup(tmp_path, execution_workspace="workspace-one")
    before = fixture.exchange.store.get("conversation-1")
    result = finish(fixture)
    assert result["id"] == before["id"]
    assert result["metadata"] == before["metadata"]
    assert set(fixture.observed["workspaces"]) == {"workspace-one"}


def test_history_move_to_another_project_keeps_execution_workspace_and_binding_order(tmp_path):
    fixture = setup(tmp_path, execution_workspace="workspace-one", project_primary="workspace-one")
    second = project("workspace-two", list(reversed(fixture.roots)), "Second Project")
    second["id"] = "group-two"
    fixture.projects.replace(projects=[project("workspace-one", fixture.roots), second],
                             expected_revision=1, mutation_id="second", migration_digest=None)
    current = fixture.exchange.store.get("conversation-1")
    metadata = {**current["metadata"], "group_id": "group-two"}
    moved = fixture.exchange.store.update("conversation-1", {
        "group_id": "group-two", "metadata": metadata,
    }, expected_conversation_revision=current["conversation_revision"])["conversation"]
    fixture.exchange.outer.payload["request"]["conversation_revision"] = moved["conversation_revision"]
    fixture.exchange.intent = saved.start(fixture.exchange.outer.payload["request"])
    facts = project_context(resolve(fixture))
    assert facts["project_primary_workspace_id"] == "workspace-two"
    assert facts["conversation_workspace_id"] == "workspace-one"
    assert [item[0] for item in facts["project_bindings"]] == ["workspace-two", "workspace-one"]
    result = finish(fixture)
    assert result["id"] == current["id"] and result["group_id"] == "group-two"
    assert result["metadata"] == metadata
    assert set(fixture.observed["workspaces"]) == {"workspace-one"}


@pytest.mark.parametrize("change", ["other-principal", "other-profile", "orphan", "alias", "malformed"])
def test_owner_namespace_or_membership_mismatch_never_becomes_a_project_grant(tmp_path, change):
    fixture = setup(tmp_path)
    value = fixture.exchange.store.get("conversation-1")
    if change == "other-principal":
        fixture.invocation.presentation_owner_principal_id = "shell.other"
    elif change == "other-profile":
        with pytest.raises(PermissionError):
            fixture.read_project({"profile_id": "other"})
        return
    elif change == "orphan":
        value["group_id"] = value["metadata"]["group_id"] = "missing-group"
    elif change == "alias":
        value["metadata"]["groupId"] = "different-group"
    else:
        value["group_id"] = value["metadata"]["group_id"] = "../group"
    with pytest.raises(ValueError):
        resolve(fixture, value)
    assert fixture.exchange.store.get("conversation-1")["messages"] == []


def test_wire_copy_and_reference_snapshot_are_not_owner_resolution(tmp_path):
    fixture = setup(tmp_path)
    checked = resolve(fixture)
    validate_saved_conversation_context(checked)
    with pytest.raises(ValueError, match="resolution"):
        validate_saved_conversation_context(dict(checked))
    with pytest.raises(ValueError, match="snapshot"):
        resolve_saved_project(
            fixture.exchange.store.get("conversation-1"), profile_id="defaults",
            presentation_owner=("shell.tauri.default", "session-one"),
            read_owner=lambda _: {"kind": "tobkiri.chat.reference.snapshot.v1", "references": []},
            guard=lambda: None,
        )


def test_project_read_failure_is_not_a_folderless_group_fallback(tmp_path):
    fixture = setup(tmp_path, execution_workspace="workspace-one")
    def unavailable():
        raise PermissionError("owner unavailable")
    fixture.hooks["before_project_read"] = unavailable
    with pytest.raises(AuthorityDenied, match="owner unavailable"):
        fixture.exchange.step()
    assert fixture.exchange.store.get("conversation-1")["messages"] == []


def test_rename_before_the_next_read_adopts_new_owner_facts_without_changing_execution_context(tmp_path):
    fixture = setup(tmp_path, execution_workspace="workspace-two", project_primary="workspace-one")
    before = resolve(fixture)
    fixture.projects.replace(projects=[project("workspace-one", fixture.roots, "Renamed")],
                             expected_revision=1, mutation_id="rename", migration_digest=None)
    after = resolve(fixture)
    assert project_context(before)["group_id"] == project_context(after)["group_id"]
    assert project_context(after)["project_title"] == "Renamed"
    assert project_context(after)["project_revision"] == 2
    assert after["metadata"] == before["metadata"]


def test_project_change_after_the_final_read_does_not_revoke_that_append_but_next_effect_reads_fresh(tmp_path):
    fixture = setup(tmp_path)
    fixture.exchange.step()
    def remove_project():
        fixture.projects.replace(projects=[], expected_revision=1, mutation_id="remove", migration_digest=None)
        fixture.hooks.pop("before_append")
    fixture.hooks["before_append"] = remove_project
    fixture.exchange.step()
    assert len(fixture.exchange.store.get("conversation-1")["messages"]) == 1
    with pytest.raises(AuthorityDenied, match="membership"):
        fixture.exchange.step()
    assert len(fixture.exchange.store.get("conversation-1")["messages"]) == 1


def test_project_removed_before_the_final_read_prevents_the_user_append(tmp_path):
    fixture = setup(tmp_path)
    fixture.exchange.step()
    fixture.projects.replace(projects=[], expected_revision=1, mutation_id="remove", migration_digest=None)
    with pytest.raises(AuthorityDenied, match="membership"):
        fixture.exchange.step()
    assert fixture.exchange.store.get("conversation-1")["messages"] == []


@pytest.mark.parametrize("move_first", [False, True])
def test_membership_move_and_append_are_ordered_by_the_existing_conversation_cas(tmp_path, move_first):
    fixture = setup(tmp_path)
    fixture.exchange.step()
    revision = fixture.exchange.store.get("conversation-1")["conversation_revision"]
    def move():
        fixture.exchange.store.update("conversation-1", {"group_id": None, "metadata": {}},
                                      expected_conversation_revision=revision)
    if move_first:
        fixture.hooks["before_append"] = move
        with pytest.raises(ConversationConflict):
            fixture.exchange.step()
        assert fixture.exchange.store.get("conversation-1")["messages"] == []
    else:
        fixture.exchange.step()
        with pytest.raises(ConversationConflict):
            move()
        assert len(fixture.exchange.store.get("conversation-1")["messages"]) == 1


def test_linked_child_inherits_complete_context_but_resolves_its_own_scope(tmp_path):
    from ecosystem.tobkiri_side_chat_pack.runtime.side_chat import SideChat
    from tests.test_side_chat_pack import PublicClient
    client = PublicClient(tmp_path)
    parent = client.store.update("conversation-1", {
        "group_id": "group-one", "conversation_kind": "coding", "metadata": {
            "mode": "coding", "workspace_id": "workspace-two", "workspace_bindings": [
                {"workspace_id": "workspace-one", "workspace_label": "One", "workspace_root": "/one"},
                {"workspace_id": "workspace-two", "workspace_label": "Two", "workspace_root": "/two"},
            ],
        },
    }, expected_conversation_revision=1)["conversation"]
    result = SideChat(client, "defaults").ensure("conversation-1", parent["conversation_revision"])
    child = client.store.get(result["conversation_id"])
    assert child["group_id"] is None and "workspace_bindings" not in child["metadata"]
    parent = client.store.get("conversation-1")
    effective = resolve_request_context(child, {"context_binding": context_binding(parent, "defaults")},
                                        "defaults", lambda _: parent)
    assert effective["id"] == child["id"]
    assert effective["metadata"]["workspace_bindings"] == parent["metadata"]["workspace_bindings"]
    owner = ProjectStateStore(tmp_path, "defaults", "shell.tauri.default")
    owner.replace(projects=[project()], expected_revision=0, mutation_id="create", migration_digest=None)
    checked = resolve_saved_project(effective, profile_id="defaults",
        presentation_owner=("shell.tauri.default", "session-one"), read_owner=lambda _: owner.read(), guard=lambda: None, validate_projects=_projects)
    checked = resolve_saved_workspace(checked, profile_id="defaults", guard=lambda: None,
        read_binding=lambda _: {"mount": {"id": "workspace-two", "mount_revision": 1},
                               "binding": {"workspace_id": "workspace-two", "mount_revision": 1,
                                           "root_st_dev": 1, "root_st_ino": 2}})
    assert workspace_resolution(checked).conversation_id == child["id"]
    assert project_context(checked)["conversation_id"] == child["id"]
    original_binding = context_binding(parent, "defaults")
    reordered = deepcopy(parent)
    reordered["metadata"]["workspace_bindings"].reverse()
    with pytest.raises(ValueError, match="stale"):
        resolve_linked_conversation(child, reordered, original_binding, "defaults")


def test_actual_workspace_replacement_still_rejects_group_coding(tmp_path):
    fixture = setup(tmp_path, execution_workspace="workspace-one")
    root = tmp_path / "workspace-one"
    root.rename(tmp_path / "old-workspace-one")
    root.mkdir()
    with pytest.raises(PermissionError, match="changed"):
        fixture.exchange.step()
    assert fixture.exchange.store.get("conversation-1")["messages"] == []


def test_inert_root_hints_never_select_or_resolve_an_additional_workspace(tmp_path):
    fixture = setup(tmp_path, execution_workspace="workspace-one", project_primary="workspace-two")
    current = fixture.exchange.store.get("conversation-1")
    metadata = deepcopy(current["metadata"])
    metadata["workspace_root"] = "/not-an-authorized-root"
    metadata["workspace_bindings"][1]["workspace_root"] = "/another-inert-root"
    changed = fixture.exchange.store.update("conversation-1", {"metadata": metadata},
        expected_conversation_revision=current["conversation_revision"])["conversation"]
    fixture.exchange.outer.payload["request"]["conversation_revision"] = changed["conversation_revision"]
    fixture.exchange.intent = saved.start(fixture.exchange.outer.payload["request"])
    result = finish(fixture)
    assert result["metadata"] == metadata
    assert set(fixture.observed["workspaces"]) == {"workspace-one"}
    assert fixture.mounts.snapshot()["revision"] == 2


def test_tool_registry_uses_the_conversation_second_primary_and_rejects_project_primary_substitution(tmp_path):
    fixture = setup(tmp_path, execution_workspace="workspace-two", project_primary="workspace-one")

    def fresh():
        return resolve_saved_workspace(resolve(fixture), profile_id="defaults", guard=lambda: None,
            read_binding=lambda payload: fixture.callbacks._dispatch(
                fixture.exchange.outer, WORKSPACE_TARGET, payload)["value"])

    owner = fresh()
    resolution = workspace_resolution(owner)
    selected_id = "workspace-two"

    def selected():
        fact = capture_workspace_binding("defaults", selected_id, user_data_root=tmp_path)
        value = {
            "workspace_id": selected_id, "access": "read_only", "canonical_root": str(tmp_path / selected_id),
            "mount_revision": fact.mount_revision, "root_st_dev": fact.root_st_dev, "root_st_ino": fact.root_st_ino,
        }
        return {**value, "root_identity": hashlib.sha256(canonical_json(value)).hexdigest()}

    binding = SavedWorkspaceExecutionBinding(
        saved_request_id="saved-request", saved_request_digest="sha256:" + "a" * 64,
        resolution=resolution, canonical_root=str(tmp_path / "workspace-two"),
        selected_workspace=selected, read_owner=fresh, assert_live=lambda: None,
        owner_context=canonical_json({"conversation_kind": owner["conversation_kind"],
                                      "metadata_mode": owner["metadata"]["mode"]}),
    )
    registry = SavedWorkspaceExecutionRegistry()
    with registry.register(binding):
        assert binding.resolution.workspace_id == "workspace-two"
        selected_id = "workspace-one"
        with pytest.raises(PermissionError, match="selected binding changed"):
            binding.assert_current()
    with pytest.raises(PermissionError, match="lease expired"):
        binding.assert_current()


def test_tool_effect_keeps_its_final_project_snapshot_but_next_effect_must_read_again(tmp_path):
    fixture = setup(tmp_path, execution_workspace="workspace-two", project_primary="workspace-one")
    accepted = resolve(fixture)
    fixture.projects.replace(projects=[], expected_revision=1, mutation_id="remove", migration_digest=None)
    before_reads = fixture.observed["projects"]
    retained = retain_saved_project_effect_snapshot(
        fixture.exchange.store.get("conversation-1"), accepted, profile_id="defaults")
    checked = resolve_saved_workspace(retained, profile_id="defaults", guard=lambda: None,
        read_binding=lambda payload: fixture.callbacks._dispatch(fixture.exchange.outer, WORKSPACE_TARGET, payload)["value"])
    assert workspace_resolution(checked).workspace_id == "workspace-two"
    assert project_context(checked) == project_context(accepted)
    assert fixture.observed["projects"] == before_reads
    with pytest.raises(ValueError, match="membership"):
        resolve(fixture)
    for bad_snapshot, profile in ((dict(accepted), "defaults"), (accepted, "other")):
        with pytest.raises(ValueError, match="effect conversation"):
            retain_saved_project_effect_snapshot(
                fixture.exchange.store.get("conversation-1"), bad_snapshot, profile_id=profile)
    changed = fixture.exchange.store.update("conversation-1", {"group_id": None, "metadata": {}},
        expected_conversation_revision=2)["conversation"]
    with pytest.raises(ValueError, match="effect conversation"):
        retain_saved_project_effect_snapshot(changed, accepted, profile_id="defaults")


def test_no_project_uses_no_project_owner_read_or_presentation_identity(tmp_path):
    fixture = setup(tmp_path)
    fixture.exchange.store.update("conversation-1", {"group_id": None, "metadata": {}},
                                  expected_conversation_revision=2)
    fixture.exchange.outer.payload["request"]["conversation_revision"] = 3
    fixture.exchange.intent = saved.start(fixture.exchange.outer.payload["request"])
    fixture.invocation.presentation_owner_principal_id = ""
    fixture.invocation.presentation_owner_session_id = ""
    assert finish(fixture)["group_id"] is None
    assert fixture.observed["projects"] == 0


def test_clearing_group_membership_preserves_execution_bindings_without_project_access(tmp_path):
    fixture = setup(tmp_path, execution_workspace="workspace-two", project_primary="workspace-one")
    current = fixture.exchange.store.get("conversation-1")
    metadata = dict(current["metadata"])
    metadata.pop("group_id")
    fixture.exchange.store.update("conversation-1", {"group_id": None, "metadata": metadata},
                                  expected_conversation_revision=current["conversation_revision"])
    fixture.exchange.outer.payload["request"]["conversation_revision"] = 3
    fixture.exchange.intent = saved.start(fixture.exchange.outer.payload["request"])
    fixture.invocation.presentation_owner_principal_id = ""
    fixture.invocation.presentation_owner_session_id = ""
    result = finish(fixture)
    assert result["id"] == "conversation-1" and result["group_id"] is None
    assert result["metadata"] == metadata
    assert fixture.observed["projects"] == 0
    assert set(fixture.observed["workspaces"]) == {"workspace-two"}


def test_no_group_can_keep_complete_inert_bindings_without_inventing_an_execution_primary(tmp_path):
    fixture = setup(tmp_path)
    metadata = {"workspace_bindings": deepcopy(fixture.roots)}
    fixture.exchange.store.update("conversation-1", {"group_id": None, "metadata": metadata},
                                  expected_conversation_revision=2)
    fixture.exchange.outer.payload["request"]["conversation_revision"] = 3
    fixture.exchange.intent = saved.start(fixture.exchange.outer.payload["request"])
    fixture.invocation.presentation_owner_principal_id = ""
    fixture.invocation.presentation_owner_session_id = ""
    result = finish(fixture)
    assert result["id"] == "conversation-1" and result["group_id"] is None
    assert result["metadata"] == metadata
    assert fixture.observed == {"workspaces": [], "projects": 0}
