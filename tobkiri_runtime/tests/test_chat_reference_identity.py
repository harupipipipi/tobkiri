"""Read-only reference ownership, bounds and durable existing-ID regression."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
from ecosystem.tobkiri_ui_settings_pack.runtime.projects import ProjectStateStore

RUNTIME = (
    Path(__file__).resolve().parents[1]
    / "ecosystem/rumi_conversation_store_pack/runtime"
)


def load(path: Path, name: str, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(__import__("sys").modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def modules(monkeypatch):
    helper = load(
        RUNTIME / "chat_reference.py",
        "ecosystem.rumi_conversation_store_pack.runtime.chat_reference",
        monkeypatch,
    )
    host = load(RUNTIME / "chat_reference_host.py", "reference_host_test", monkeypatch)
    return helper, host


def project(identifier="group-one", title="One"):
    return {
        "id": identifier,
        "title": title,
        "workspace_id": None,
        "workspace_label": None,
        "workspace_root": "/private/path",
        "rumi_data_path": "/private/secret",
    }


@pytest.fixture
def owners(tmp_path):
    conversations = ConversationStore("defaults", user_data_root=tmp_path)
    projects = ProjectStateStore(tmp_path, "defaults", "shell.one")
    projects.replace(
        projects=[project(), project("group-two", "Two")],
        expected_revision=0,
        mutation_id="create-projects",
        migration_digest=None,
    )
    conversations.create(
        {
            "id": "a1111111-1111-4111-8111-111111111111",
            "title": "Chat One",
            "group_id": "group-one",
        },
        expected_revision=0,
    )
    return conversations, projects


def request(refs=None):
    return (
        {"profile_id": "defaults", "operation": "list"}
        if refs is None
        else {
            "profile_id": "defaults",
            "operation": "resolve",
            "references": refs,
        }
    )


def resolve(helper, owners, refs=None):
    c, p = owners
    return helper.project_references(
        request(refs), c.snapshot(), p.read(), profile_id="defaults"
    )


def test_reload_rename_move_preserve_existing_identity_and_membership_digest(
    modules, owners, tmp_path
):
    helper, _ = modules
    refs = [{"kind": "group", "id": "group-one"}]
    before = resolve(helper, owners, refs)["references"][0]
    p = owners[1]
    p.replace(
        projects=[project(title="Renamed"), project("group-two", "Two")],
        expected_revision=1,
        mutation_id="rename",
        migration_digest=None,
    )
    reloaded = (
        ConversationStore("defaults", user_data_root=tmp_path),
        ProjectStateStore(tmp_path, "defaults", "shell.one"),
    )
    renamed = resolve(helper, reloaded, refs)["references"][0]
    assert renamed["id"] == before["id"] == "group-one"
    assert renamed["snapshot_digest"] == before["snapshot_digest"]
    assert renamed["label"] == "Renamed"
    cid = before["conversation_ids"][0]
    c = reloaded[0]
    revision = c.get(cid)["conversation_revision"]
    c.update(cid, {"title": "Another title"}, expected_conversation_revision=revision)
    assert (
        resolve(helper, reloaded, refs)["references"][0]["snapshot_digest"]
        == before["snapshot_digest"]
    )
    c.append_message(
        cid,
        {"id": "message-one", "role": "user", "content": "new message"},
        expected_conversation_revision=revision + 1,
    )
    assert (
        resolve(helper, reloaded, refs)["references"][0]["snapshot_digest"]
        == before["snapshot_digest"]
    )
    c.update(
        cid, {"group_id": "group-two"}, expected_conversation_revision=revision + 2
    )
    moved = resolve(helper, reloaded, refs)["references"][0]
    assert moved["id"] == "group-one" and moved["conversation_ids"] == []
    assert moved["snapshot_digest"] != before["snapshot_digest"]
    assert c.get(cid)["id"] == cid
    encoded = str(resolve(helper, reloaded))
    assert "/private/" not in encoded and "workspace_root" not in encoded


def test_persisted_compat_membership_only_and_conflicts_fail_closed(modules, owners):
    helper, _ = modules
    c, _ = owners
    cid = c.snapshot()["conversations"][0]["id"]
    c.update(
        cid,
        {"group_id": None, "metadata": {"groupId": "group-one"}},
        expected_conversation_revision=1,
    )
    assert resolve(helper, owners, [{"kind": "group", "id": "group-one"}])[
        "references"
    ][0]["conversation_ids"] == [cid]
    c.update(cid, {"group_id": "group-two"}, expected_conversation_revision=2)
    with pytest.raises(ValueError, match="conflicts"):
        resolve(helper, owners)


@pytest.mark.parametrize(
    "refs",
    [
        [{"kind": "group", "id": "Older"}],
        [{"kind": "group", "id": "custom-group-one"}],
        [{"kind": "chat", "id": "deleted-chat"}],
        [{"kind": "group", "id": "group-one", "label": "forged"}],
        [{"kind": "other", "id": "group-one"}],
        [{"kind": [], "id": "group-one"}],
        [{"kind": "group", "id": "../path"}],
        [{"kind": "chat", "id": "missing"}] * 17,
    ],
)
def test_unknown_virtual_malformed_and_overbound_refs_rejected(modules, owners, refs):
    with pytest.raises((KeyError, ValueError)):
        resolve(modules[0], owners, refs)


def test_profile_snapshot_and_request_reject_crossprofile(modules, owners):
    helper, _ = modules
    c, p = owners
    with pytest.raises(PermissionError):
        helper.project_references(
            {"profile_id": "other", "operation": "list"},
            c.snapshot(),
            p.read(),
            profile_id="defaults",
        )
    snapshot = c.snapshot()
    snapshot["profile_id"] = "other"
    with pytest.raises(PermissionError):
        helper.project_references(request(), snapshot, p.read(), profile_id="defaults")


def test_group_and_combined_read_fanout_bound256(modules):
    helper, _ = modules
    snapshot = {
        "profile_id": "defaults",
        "revision": 1,
        "conversations": [
            {
                "id": f"chat-{i}",
                "title": "Chat",
                "group_id": "group-one",
                "metadata": {},
                "updated_at": 1,
            }
            for i in range(257)
        ],
    }
    projects = {
        "namespace": "defaultspack.projects.v1",
        "revision": 1,
        "projects": [project()],
    }
    with pytest.raises(ValueError, match="fanout"):
        helper.project_references(
            request([{"kind": "group", "id": "group-one"}]),
            snapshot,
            projects,
            profile_id="defaults",
        )
    snapshot["conversations"][-1]["group_id"] = None
    refs = [{"kind": "group", "id": "group-one"}, {"kind": "chat", "id": "chat-256"}]
    with pytest.raises(ValueError, match="combined"):
        helper.project_references(
            request(refs), snapshot, projects, profile_id="defaults"
        )


def capture(host):
    operation = SimpleNamespace(
        contract_id=host.CONTRACT_ID,
        operation_id=host.OPERATION_ID,
        contract_version="1.0.0",
    )
    binding = SimpleNamespace(
        operation=operation,
        function=SimpleNamespace(
            function_id=host.FUNCTION_ID, implementation_digest="sha256:" + "1" * 64
        ),
        principal_ref=SimpleNamespace(value="provider"),
        artifact=SimpleNamespace(digest="sha256:" + "2" * 64),
    )
    context = SimpleNamespace(
        profile_id="defaults",
        provider_bindings=[binding],
        domain_ids={(host.CONTRACT_ID, host.OPERATION_ID, "provider"): "domain"},
    )
    return host.ChatReferenceHostFactoryV4().capture(context).contributions[0].invoke


class Invocation:
    presentation_owner_principal_id = "shell.one"
    presentation_owner_session_id = "session.one"

    def __init__(self, owners, host):
        self.owners, self.host, self.calls = owners, host, []
        self.assertions = 0

    def assert_current(self):
        self.assertions += 1

    def contract_client(self, **kwargs):
        assert kwargs == {
            "allowed_contract_ids": frozenset(
                {self.host.CONVERSATION_CONTRACT, self.host.PROJECT_CONTRACT}
            ),
            "consumer_pack_id": "rumi_conversation_store_pack",
            "include_credentials": False,
        }
        return self

    def invoke(self, contract, operation, payload):
        self.calls.append((contract, operation, payload))
        assert payload["profile_id"] == "defaults"
        if contract == self.host.CONVERSATION_CONTRACT:
            assert (
                operation == self.host.CONVERSATION_OPERATION
                and payload["operation"] == "list"
            )
            return self.owners[0].snapshot()
        assert operation == self.host.PROJECT_OPERATION
        assert self.presentation_owner_principal_id == self.owners[1].owner_principal_id
        return self.owners[1].read()


def test_host_bounded_contract_client_preserves_caller_and_checks_current(
    modules, owners
):
    _, host = modules
    invocation = Invocation(owners, host)
    result = capture(host)(host.OPERATION_ID, request(), invocation)
    assert len(invocation.calls) == 2 and invocation.assertions == 4
    assert result["references"] and "/private/" not in str(result)


@pytest.mark.parametrize(
    "field", ["presentation_owner_principal_id", "presentation_owner_session_id"]
)
def test_host_requires_authenticated_owner(modules, owners, field):
    _, host = modules
    invocation = Invocation(owners, host)
    setattr(invocation, field, "")
    with pytest.raises(PermissionError, match="caller"):
        capture(host)(host.OPERATION_ID, request(), invocation)
    assert invocation.calls == []


def test_stale_or_client_selected_profile_rejected_before_reads(modules, owners):
    _, host = modules
    invocation = Invocation(owners, host)
    with pytest.raises(PermissionError):
        capture(host)(
            host.OPERATION_ID, {"profile_id": "other", "operation": "list"}, invocation
        )
    assert invocation.calls == []

    def stale():
        raise PermissionError("stale capture")

    invocation.assert_current = stale
    with pytest.raises(PermissionError, match="stale"):
        capture(host)(host.OPERATION_ID, request(), invocation)


def test_deleted_group_and_other_caller_namespace_cannot_resolve(
    modules, owners, tmp_path
):
    helper, _ = modules
    c, p = owners
    p.replace(
        projects=[],
        expected_revision=1,
        mutation_id="delete-projects",
        migration_digest=None,
    )
    with pytest.raises(KeyError):
        resolve(helper, owners, [{"kind": "group", "id": "group-one"}])
    other = ProjectStateStore(tmp_path, "defaults", "shell.two")
    with pytest.raises(KeyError):
        helper.project_references(
            request([{"kind": "group", "id": "group-one"}]),
            c.snapshot(),
            other.read(),
            profile_id="defaults",
        )


def test_builtin_age_buckets_use_owner_timestamps_and_stable_clock_digest(modules):
    helper, _ = modules
    now = 2_000_000_000_000
    day = 86_400_000
    snapshot = {
        "profile_id": "defaults",
        "revision": 1,
        "conversations": [
            {"id": key, "title": key, "updated_at": now - age, "metadata": {}}
            for key, age in [("today", 0), ("recent", 2 * day), ("older", 8 * day)]
        ],
    }
    projects = {"namespace": "defaultspack.projects.v1", "revision": 0, "projects": []}
    payload = request([{"kind": "group", "id": "group-older"}])
    first = helper.project_references(
        payload, snapshot, projects, profile_id="defaults", now_ms=now
    )
    second = helper.project_references(
        payload, snapshot, projects, profile_id="defaults", now_ms=now + 1000
    )
    assert first["kind"] == "tobkiri.chat.reference.snapshot.v1"
    assert first["references"][0]["conversation_ids"] == ["older"]
    assert (
        first["references"][0]["snapshot_digest"]
        == second["references"][0]["snapshot_digest"]
    )
    assert first["expires_at"] - first["snapshot_time"] == 600_000
    assert first["snapshot_time"] != second["snapshot_time"]


def test_builtin_metadata_buckets_primary_tag_union_and_project_precedence(modules):
    helper, _ = modules
    records = [
        {"id": "pinned", "title": "P", "is_pinned": True, "tags": ["coding"]},
        {"id": "company", "title": "C", "metadata": {"company_id": "co"}},
        {"id": "coding", "title": "Code", "metadata": {"mode": "coding"}},
        {"id": "tagged", "title": "T", "tags": ["Research Stuff", "secondary"]},
        {"id": "custom", "title": "Custom", "group_id": "group-one", "is_pinned": True},
        {"id": "recent", "title": "R"},
    ]
    snapshot = {"profile_id": "defaults", "revision": 1, "conversations": records}
    projects = {
        "namespace": "defaultspack.projects.v1",
        "revision": 1,
        "projects": [project()],
    }
    result = helper.project_references(
        request(), snapshot, projects, profile_id="defaults", now_ms=1
    )
    groups = {
        ref["id"]: ref["conversation_ids"]
        for ref in result["references"]
        if ref["kind"] == "group"
    }
    assert groups["group-pinned"] == ["pinned"]
    assert groups["group-company"] == ["company"]
    assert groups["group-coding"] == ["coding"]
    assert (
        groups[helper.tag_group_id("research-stuff")]
        == groups["group-tags"]
        == ["tagged"]
    )
    assert groups["group-one"] == ["custom"] and groups["group-recent"] == ["recent"]
    with pytest.raises(KeyError):
        helper.project_references(
            request([{"kind": "group", "id": "group-tag-secondary"}]),
            snapshot,
            projects,
            profile_id="defaults",
            now_ms=1,
        )


def test_group18_discoverable_and_read_resolve_is_authoritative(modules):
    helper, _ = modules
    snapshot = {
        "profile_id": "defaults",
        "revision": 1,
        "conversations": [
            {
                "id": f"member-{index}",
                "title": "Member",
                "group_id": "group-one",
                "updated_at": 1,
            }
            for index in range(18)
        ],
    }
    projects = {
        "namespace": "defaultspack.projects.v1",
        "revision": 1,
        "projects": [project()],
    }
    listed = helper.project_references(
        request(), snapshot, projects, profile_id="defaults", now_ms=1
    )
    group = next(ref for ref in listed["references"] if ref["id"] == "group-one")
    resolved = helper.project_references(
        request([{"kind": "group", "id": "group-one"}]),
        snapshot,
        projects,
        profile_id="defaults",
        now_ms=1,
    )
    assert group["member_count"] == 18 and group["membership_complete"] is True
    assert group["conversation_ids"] == resolved["references"][0]["conversation_ids"]
    assert (
        len(group["conversation_ids"]) == 18
    )  # send admission owns its separate 16 limit


def test_large_group_discoverable_without_silent_membership_truncation(modules):
    helper, _ = modules
    snapshot = {
        "profile_id": "defaults",
        "revision": 1,
        "conversations": [
            {
                "id": f"member-{index}",
                "title": "Member",
                "group_id": "group-one",
                "updated_at": 1,
            }
            for index in range(257)
        ],
    }
    projects = {
        "namespace": "defaultspack.projects.v1",
        "revision": 1,
        "projects": [project()],
    }
    listed = helper.project_references(
        request(), snapshot, projects, profile_id="defaults", now_ms=1
    )
    group = next(ref for ref in listed["references"] if ref["id"] == "group-one")
    assert group["member_count"] == 257 and group["membership_complete"] is False
    assert group["conversation_ids"] == []
    assert listed["truncated"] is True and len(listed["references"]) == 100
    with pytest.raises(ValueError, match="read fanout"):
        helper.project_references(
            request([{"kind": "group", "id": "group-one"}]),
            snapshot,
            projects,
            profile_id="defaults",
            now_ms=1,
        )


def test_list_cursor_pages_have_no_duplicates_and_reject_stale_snapshot(modules):
    helper, _ = modules
    snapshot = {
        "profile_id": "defaults",
        "revision": 1,
        "conversations": [
            {"id": f"member-{index:03}", "title": "Member", "updated_at": 1}
            for index in range(18)
        ],
    }
    projects = {"namespace": "defaultspack.projects.v1", "revision": 0, "projects": []}
    payload = {**request(), "limit": 5}
    keys = []
    first_cursor = None
    while True:
        page = helper.project_references(
            payload, snapshot, projects, profile_id="defaults", now_ms=1
        )
        keys.extend((ref["kind"], ref["id"]) for ref in page["references"])
        if not page["next_cursor"]:
            break
        first_cursor = first_cursor or page["next_cursor"]
        payload["cursor"] = page["next_cursor"]
    assert len(keys) == len(set(keys)) == 21
    snapshot["conversations"][0]["title"] = "Renamed"
    with pytest.raises(ValueError, match="stale"):
        helper.project_references(
            {**request(), "limit": 5, "cursor": first_cursor},
            snapshot,
            projects,
            profile_id="defaults",
            now_ms=1,
        )
    with pytest.raises(ValueError, match="limit"):
        helper.project_references(
            {**request(), "limit": 101},
            snapshot,
            projects,
            profile_id="defaults",
            now_ms=1,
        )


def test_builtin_visible_roots_filter_but_explicit_project_members_include_all(modules):
    helper, _ = modules
    records = [
        {"id": "root", "title": "Root", "updated_at": 1},
        {
            "id": "hidden",
            "title": "Hidden",
            "updated_at": 1,
            "metadata": {"hidden": True},
        },
        {"id": "archived", "title": "Archived", "updated_at": 1, "is_archived": True},
        {
            "id": "child",
            "title": "Child",
            "updated_at": 1,
            "parent_conversation_id": "root",
        },
        {
            "id": "project-hidden",
            "title": "Project Hidden",
            "updated_at": 1,
            "group_id": "group-one",
            "metadata": {"is_hidden": True},
        },
    ]
    snapshot = {"profile_id": "defaults", "revision": 1, "conversations": records}
    projects = {
        "namespace": "defaultspack.projects.v1",
        "revision": 1,
        "projects": [project()],
    }
    result = helper.project_references(
        request(
            [
                {"kind": "group", "id": "group-today"},
                {"kind": "group", "id": "group-one"},
            ]
        ),
        snapshot,
        projects,
        profile_id="defaults",
        now_ms=1,
    )
    assert result["references"][0]["conversation_ids"] == [
        "archived",
        "child",
        "hidden",
        "root",
    ]
    assert result["references"][1]["conversation_ids"] == ["project-hidden"]
    assert helper.project_references(
        request([{"kind": "chat", "id": "child"}]),
        snapshot,
        projects,
        profile_id="defaults",
        now_ms=1,
    )["references"][0]["conversation_ids"] == ["child"]


def test_nested_explicit_inverse_children_union_visible_and_unique(modules):
    helper, _ = modules
    records = [
        {
            "id": "root",
            "title": "Root",
            "updated_at": 1,
            "group_id": "group-one",
            "child_conversation_ids": ["child", "child", "hidden"],
        },
        {
            "id": "child",
            "title": "Child",
            "updated_at": 1,
            "parent_conversation_id": "root",
            "child_conversation_ids": ["grandchild"],
        },
        {
            "id": "grandchild",
            "title": "Grandchild",
            "updated_at": 1,
            "is_archived": True,
        },
        {
            "id": "hidden",
            "title": "Hidden",
            "updated_at": 1,
            "metadata": {"is_hidden": True},
        },
    ]
    snapshot = {"profile_id": "defaults", "revision": 1, "conversations": records}
    projects = {
        "namespace": "defaultspack.projects.v1",
        "revision": 1,
        "projects": [project()],
    }
    result = helper.project_references(
        request([{"kind": "group", "id": "group-one"}]),
        snapshot,
        projects,
        profile_id="defaults",
        now_ms=1,
    )
    assert result["references"][0]["conversation_ids"] == [
        "child",
        "grandchild",
        "root",
    ]
    records[0]["group_id"] = None
    result = helper.project_references(
        request([{"kind": "group", "id": "group-today"}]),
        snapshot,
        projects,
        profile_id="defaults",
        now_ms=1,
    )
    assert result["references"][0]["conversation_ids"] == [
        "child",
        "grandchild",
        "root",
    ]


def test_unicode_tag_codec_and_long_owner_title_display_bounds(modules):
    import base64

    helper, _ = modules
    tag = "  調査 分析 😀  "
    expected = "group-tag-" + base64.urlsafe_b64encode(
        "調査-分析-😀".encode()
    ).decode().rstrip("=")
    assert helper.tag_group_id(tag) == expected
    assert helper.tag_group_id("é" * 50) == helper.tag_group_id("é" * 40)
    records = [{"id": "unicode", "title": "😀" * 500, "updated_at": 1, "tags": [tag]}]
    result = helper.project_references(
        request([{"kind": "group", "id": expected}, {"kind": "chat", "id": "unicode"}]),
        {"profile_id": "defaults", "revision": 1, "conversations": records},
        {"namespace": "defaultspack.projects.v1", "revision": 0, "projects": []},
        profile_id="defaults",
        now_ms=1,
    )
    assert result["references"][0]["label"] == "#調査-分析-😀"
    assert result["references"][0]["conversation_ids"] == ["unicode"]
    assert len(result["references"][1]["label"]) == 256
    assert len(result["references"][1]["label"].encode()) == 1024
