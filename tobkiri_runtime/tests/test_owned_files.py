"""Selected owner routing and real descriptor-jailed reads."""

import json
from types import SimpleNamespace
import pytest
from ecosystem.rumi_default_tools_pack.runtime import files
from ecosystem.rumi_file_inspect_pack.runtime.inspect import FileInspectService
from ecosystem.rumi_workspace_mount_pack.runtime.mounts import WorkspaceMountStore


@pytest.fixture
def owner(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "hello.txt").write_text("one\ntwo\n", encoding="utf-8")
    store = WorkspaceMountStore("test", user_data_root=tmp_path)
    store.mount("first", str(root), expected_revision=0)
    store.select("first", expected_revision=1)
    calls = []

    class Client:
        switch = False

        def invoke(self, contract, operation, payload):
            calls.append((contract, operation, dict(payload)))
            if contract == files.WORKSPACE:
                return (
                    store.snapshot()
                    if payload["operation"] == "list"
                    else store.get(payload["workspace_id"])
                )
            assert (contract, operation) == (files.FILE_INSPECT, files.FILE_OPERATION)
            result = FileInspectService(self).invoke(payload["name"], payload)
            if self.switch:
                store.select("second", expected_revision=3)
            return result

    client = Client()

    def contract_client(**kwargs):
        assert kwargs == dict(
            allowed_contract_ids=files.ALLOWED_CONTRACTS,
            consumer_pack_id=files.PACK_ID,
            include_credentials=False,
        )
        return client

    invocation = SimpleNamespace(assert_current=lambda: None, contract_client=contract_client)
    invoke = files._bind(SimpleNamespace(profile_id="test", user_data_root=tmp_path))
    return invoke, invocation, client, calls, root, store


def payload(tool="coding_file_read", **arguments):
    return dict(tool_id=tool, tool_call_id="call-1", arguments=arguments)


def test_real_owner_read_list_search(owner):
    invoke, invocation, _, calls, _, _ = owner
    assert (
        json.loads(invoke(payload(path="hello.txt", start_line=2), invocation)["result"])["content"]
        == "two\n"
    )
    assert (
        json.loads(invoke(payload("coding_file_list"), invocation)["result"])["items"][0]["path"]
        == "hello.txt"
    )
    assert json.loads(invoke(payload("coding_file_search", pattern="*.txt"), invocation)["result"])[
        "matches"
    ] == ["hello.txt"]
    assert all(call[2]["profile_id"] == "test" for call in calls)


@pytest.mark.parametrize(
    "extra",
    [
        dict(approved=True),
        dict(profile_id="other"),
        dict(_workspace_binding={}),
        dict(authority_receipt={}),
        dict(start_line=True),
        dict(start_line=0),
    ],
)
def test_client_claims_never_reach_owner(owner, extra):
    invoke, invocation, _, calls, _, _ = owner
    with pytest.raises(ValueError):
        invoke(payload(path="hello.txt", **extra), invocation)
    assert calls == []


@pytest.mark.parametrize("path", ["../hello.txt", "/etc/passwd", ".env", "secrets/password"])
def test_real_owner_denies_traversal_and_secrets(owner, path):
    invoke, invocation, _, _, _, _ = owner
    with pytest.raises(PermissionError):
        invoke(payload(path=path), invocation)


def test_real_owner_denies_symlink(owner, tmp_path):
    invoke, invocation, _, _, root, _ = owner
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)
    with pytest.raises((PermissionError, OSError)):
        invoke(payload(path="link.txt"), invocation)


def test_selection_switch_discards_result(owner, tmp_path):
    invoke, invocation, client, _, _, store = owner
    other = tmp_path / "other"
    other.mkdir()
    store.mount("second", str(other), expected_revision=2)
    client.switch = True
    with pytest.raises(PermissionError, match="binding changed"):
        invoke(payload(path="hello.txt"), invocation)


@pytest.mark.parametrize("tool", ["sandbox_file_read", "coding_file_write", "file_read"])
def test_unowned_tools_never_reach_owner(owner, tool):
    invoke, invocation, _, calls, _, _ = owner
    with pytest.raises(ValueError):
        invoke(payload(tool, path="hello.txt"), invocation)
    assert calls == []


def test_owned_resource_mount_uses_id_and_inspect_uses_workspace_id(owner):
    """The resource mount identity differs from the file result identity."""
    invoke, invocation, _, _, _, store = owner
    mount = store.get("first")
    assert mount["id"] == "first"
    assert "workspace_id" not in mount
    result = json.loads(invoke(payload(path="hello.txt"), invocation)["result"])
    assert result["workspace_id"] == "first"
