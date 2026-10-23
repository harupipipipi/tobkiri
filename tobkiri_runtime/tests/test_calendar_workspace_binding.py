"""Calendar workspace lookup validates the current mount at its canonical owner."""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import pytest

PATH = (
    Path(__file__).resolve().parents[1]
    / "ecosystem/rumi_workspace_mount_pack/runtime/mounts.py"
)
SPEC = importlib.util.spec_from_file_location("calendar_workspace_binding_test", PATH)
mounts = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mounts
SPEC.loader.exec_module(mounts)


def captured(tmp_path):
    binding = NS(
        function=NS(
            function_id=mounts._RESOURCE_FUNCTION_ID,
            implementation_digest="sha256:" + "b" * 64,
        ),
        operation=NS(
            contract_id="tobkiri.resource.workspace.v1",
            contract_version="1.0.0",
            operation_id="rumi_workspace_mount_pack.workspace-resource",
        ),
        principal_ref=NS(value="principal"),
        artifact=NS(digest="sha256:" + "a" * 64),
    )
    context = NS(
        provider_bindings=[binding],
        user_data_root=tmp_path,
        profile_id="profile",
        plan_digest="sha256:" + "c" * 64,
        security_epoch=1,
        domain_ids={
            (
                binding.operation.contract_id,
                binding.operation.operation_id,
                "principal",
            ): "domain"
        },
    )
    invoke = (
        mounts.WorkspaceResourceHostFactoryV4().capture(context).contributions[0].invoke
    )
    invocation = NS(
        assert_current=lambda: None,
        envelope=NS(
            context=NS(
                profile_id="profile", plan_digest=context.plan_digest, security_epoch=1
            )
        ),
    )
    return lambda operation: invoke(
        binding.operation.operation_id,
        {"profile_id": "profile", "operation": operation, "workspace_id": "registered"},
        invocation,
    )


def test_binding_checks_current_directory_but_generic_get_remains_metadata(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    stat = root.stat()
    store = mounts.WorkspaceMountStore("profile", user_data_root=tmp_path)
    store.mount(
        "registered",
        str(root),
        metadata={"directory_identity": [stat.st_dev, stat.st_ino]},
        expected_revision=0,
    )
    read = captured(tmp_path)
    receipt = read("binding")
    assert receipt["mount"]["id"] == "registered"
    assert receipt["binding"] == {
        "workspace_id": "registered",
        "mount_revision": 1,
        "root_st_dev": stat.st_dev,
        "root_st_ino": stat.st_ino,
    }
    root.rename(tmp_path / "original")
    root.mkdir()
    assert read("get")["id"] == "registered"
    with pytest.raises(PermissionError, match="directory has changed"):
        read("binding")


def test_binding_rejects_symlink_replacement(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    store = mounts.WorkspaceMountStore("profile", user_data_root=tmp_path)
    store.mount("registered", str(root), expected_revision=0)
    root.rename(tmp_path / "original")
    root.symlink_to(tmp_path / "original", target_is_directory=True)
    with pytest.raises(PermissionError, match="symlink"):
        captured(tmp_path)("binding")
