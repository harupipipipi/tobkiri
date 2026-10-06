"""Exact selection ownership, immutable mount plans and canonical persistence."""

from types import SimpleNamespace
import json
import pytest

from core_runtime.host_provider_backend_v4 import HostProviderCaptureContextV4
from core_runtime.interactive_effect_coordinator import (
    INTERACTIVE_EFFECT_SPECS,
    _execute_payload,
    _presentation_metadata,
    InteractiveEffectUnavailable,
)
from core_runtime.project_directory_port import ProjectDirectoryPort
from ecosystem.rumi_workspace_mount_pack.runtime.mounts import (
    HOST_PROVIDER_FACTORY,
    WorkspaceMountStore,
    WorkspaceConflict,
    capture_workspace_binding,
)
from tobkiri_host.directory_picker import CapturedDirectoryPicker
from tobkiri_host.directory_selections import DirectorySelections
from tobkiri_protocol.canonical import canonical_digest

CONTRACT = "tobkiri.service.workspace.project.v1"
PACK = "rumi_workspace_mount_pack"
PROFILE = "defaults"
PLAN = canonical_digest({"plan": "picker"})


class Invocation:
    def __init__(self, *, owner="owner", session="session", profile=PROFILE):
        self.envelope = SimpleNamespace(
            context=SimpleNamespace(
                profile_id=profile, plan_digest=PLAN, security_epoch=7
            )
        )
        self.presentation_owner_principal_id = owner
        self.presentation_owner_session_id = session
        self.current = True

    def assert_current(self):
        if not self.current:
            raise PermissionError("stale invocation")


def hook(tmp_path, port, function, operation):
    function = PACK + "." + function
    binding = SimpleNamespace(
        function=SimpleNamespace(
            function_id=function, implementation_digest="implementation"
        ),
        operation=SimpleNamespace(
            contract_id=CONTRACT, contract_version="1.0.0", operation_id=operation
        ),
        principal_ref=SimpleNamespace(value=function),
        artifact=SimpleNamespace(digest="artifact"),
    )
    context = HostProviderCaptureContextV4(
        profile_id=PROFILE,
        plan_digest=PLAN,
        security_epoch=7,
        activation={"activation_id": "activation"},
        state_root=tmp_path,
        user_data_root=tmp_path,
        provider_bindings=(binding,),
        catalog_bindings=(binding,),
        domain_ids={(CONTRACT, operation, function): "domain"},
        directory_selection_port=port,
    )
    return HOST_PROVIDER_FACTORY[function].capture(context).contributions[0].invoke


def workflow(tmp_path, *, picker=None):
    root = tmp_path / "selected"
    root.mkdir()
    selections = DirectorySelections()
    picker = picker or SimpleNamespace(pick_directory=lambda: root)
    port = ProjectDirectoryPort(CapturedDirectoryPicker(picker, selections), selections)
    acquire = hook(
        tmp_path, port, "project-directory.service", "workspace.directory.acquire"
    )
    prepare = hook(
        tmp_path, port, "project-workspace-prepare.service", "workspace.mount.prepare"
    )
    execute = hook(
        tmp_path, None, "project-workspace-execute.service", "workspace.mount.execute"
    )
    return root, port, acquire, prepare, execute


def test_selected_ticket_prepare_has_no_mount_and_execute_atomically_mounts_and_selects(
    tmp_path,
):
    root, port, acquire, prepare, execute = workflow(tmp_path)
    inv = Invocation()
    choice = acquire("workspace.directory.acquire", {}, inv)
    assert "path" not in choice and str(root) not in json.dumps(choice)
    request = {"selection_id": choice["selection_id"]}
    plan = prepare("workspace.mount.prepare", request, inv)
    store = WorkspaceMountStore(PROFILE, user_data_root=tmp_path)
    assert store.snapshot()["mounts"] == [] and not store.path.exists()
    payload = _execute_payload(
        INTERACTIVE_EFFECT_SPECS["workspace_mount"], request, plan
    )
    metadata = _presentation_metadata(
        INTERACTIVE_EFFECT_SPECS["workspace_mount"],
        SimpleNamespace(
            normalized_payload=payload, request_digest=canonical_digest(payload)
        ),
    )
    assert str(root) not in json.dumps(metadata)
    assert metadata["workspace_id"] == plan["workspace_id"]
    execute("workspace.mount.execute", payload, inv)
    snapshot = store.snapshot()
    assert (
        snapshot["revision"] == 1
        and snapshot["selected_workspace_id"] == plan["workspace_id"]
    )
    assert snapshot["mounts"][0]["metadata"]["trusted"] is False
    assert snapshot["mounts"][0]["root_path"] == str(root)
    with pytest.raises(PermissionError):
        prepare("workspace.mount.prepare", request, inv)
    with pytest.raises(WorkspaceConflict):
        execute("workspace.mount.execute", payload, inv)


@pytest.mark.parametrize(
    "payload",
    [
        {"path": "/tmp"},
        {"root_path": "/tmp"},
        {"approved": True},
        {"prompt": "injected"},
        {"profile_id": "other"},
    ],
)
def test_browser_cannot_supply_picker_authority_or_paths(tmp_path, payload):
    root, port, acquire, prepare, execute = workflow(tmp_path)
    with pytest.raises(ValueError):
        acquire("workspace.directory.acquire", payload, Invocation())
    assert not WorkspaceMountStore(PROFILE, user_data_root=tmp_path).path.exists()


@pytest.mark.parametrize(
    "field,value",
    [("owner", "foreign"), ("session", "foreign"), ("profile", "foreign")],
)
def test_ticket_rejects_foreign_owner_and_preserves_legitimate_redemption(
    tmp_path, field, value
):
    root, port, acquire, prepare, execute = workflow(tmp_path)
    choice = acquire("workspace.directory.acquire", {}, Invocation())
    request = {"selection_id": choice["selection_id"]}
    with pytest.raises(PermissionError):
        prepare("workspace.mount.prepare", request, Invocation(**{field: value}))
    assert prepare("workspace.mount.prepare", request, Invocation())[
        "root_path"
    ] == str(root)


def test_dialog_late_result_is_discarded_before_ticket_or_mount(tmp_path):
    inv = Invocation()
    root = tmp_path / "native"
    root.mkdir()

    def pick():
        inv.current = False
        return root

    _, port, acquire, prepare, execute = workflow(
        tmp_path, picker=SimpleNamespace(pick_directory=pick)
    )
    with pytest.raises(PermissionError):
        acquire("workspace.directory.acquire", {}, inv)
    assert not WorkspaceMountStore(PROFILE, user_data_root=tmp_path).path.exists()


def test_native_cancellation_never_creates_ticket_or_mount(tmp_path):
    _, port, acquire, prepare, execute = workflow(
        tmp_path, picker=SimpleNamespace(pick_directory=lambda: None)
    )
    assert acquire("workspace.directory.acquire", {}, Invocation()) == {
        "cancelled": True,
        "selection_id": None,
    }
    assert not WorkspaceMountStore(PROFILE, user_data_root=tmp_path).path.exists()


def test_replaced_root_after_prepare_fails_without_persistence(tmp_path):
    root, port, acquire, prepare, execute = workflow(tmp_path)
    inv = Invocation()
    request = {
        "selection_id": acquire("workspace.directory.acquire", {}, inv)["selection_id"]
    }
    plan = prepare("workspace.mount.prepare", request, inv)
    root.rename(tmp_path / "retired")
    root.mkdir()
    with pytest.raises(PermissionError):
        execute("workspace.mount.execute", {"request": request, "plan": plan}, inv)
    assert not WorkspaceMountStore(PROFILE, user_data_root=tmp_path).path.exists()


def test_mounted_project_replacement_cannot_be_recaptured_for_later_file_actions(
    tmp_path,
):
    root, port, acquire, prepare, execute = workflow(tmp_path)
    inv = Invocation()
    request = {
        "selection_id": acquire("workspace.directory.acquire", {}, inv)["selection_id"]
    }
    plan = prepare("workspace.mount.prepare", request, inv)
    execute("workspace.mount.execute", {"request": request, "plan": plan}, inv)
    capture_workspace_binding(PROFILE, plan["workspace_id"], user_data_root=tmp_path)
    root.rename(tmp_path / "retired")
    root.mkdir()
    with pytest.raises(PermissionError):
        capture_workspace_binding(
            PROFILE, plan["workspace_id"], user_data_root=tmp_path
        )


@pytest.mark.parametrize(
    "change",
    [
        {"root_path": "relative"},
        {"expected_revision": True},
        {"root_identity": [True, 2]},
        {"workspace_id": "client-path"},
        {"approved": True},
        {"profile_id": "foreign"},
    ],
)
def test_malformed_or_foreign_execute_plan_does_not_persist(tmp_path, change):
    root, port, acquire, prepare, execute = workflow(tmp_path)
    inv = Invocation()
    request = {
        "selection_id": acquire("workspace.directory.acquire", {}, inv)["selection_id"]
    }
    plan = prepare("workspace.mount.prepare", request, inv)
    plan.update(change)
    with pytest.raises((PermissionError, InteractiveEffectUnavailable)):
        payload = _execute_payload(
            INTERACTIVE_EFFECT_SPECS["workspace_mount"], request, plan
        )
        execute("workspace.mount.execute", payload, inv)
    assert not WorkspaceMountStore(PROFILE, user_data_root=tmp_path).path.exists()


def test_metadata_revision_conflict_preserves_other_mount_and_selection(tmp_path):
    root, port, acquire, prepare, execute = workflow(tmp_path)
    inv = Invocation()
    request = {
        "selection_id": acquire("workspace.directory.acquire", {}, inv)["selection_id"]
    }
    plan = prepare("workspace.mount.prepare", request, inv)
    store = WorkspaceMountStore(PROFILE, user_data_root=tmp_path)
    store.mount("existing", str(root), expected_revision=0)
    before = store.snapshot()
    with pytest.raises(WorkspaceConflict):
        execute("workspace.mount.execute", {"request": request, "plan": plan}, inv)
    assert store.snapshot() == before


def test_exact_backend_preserves_uncertainty_after_mount_or_capture_failure():
    from core_runtime.host_provider_backend_v4 import (
        ExactHostProviderBackendV4,
        HostProviderContributionV4,
    )
    from core_runtime.workspace_mount_effect import ProjectMountPersistenceUncertain
    from tobkiri_host.broker import RequestEnvelope
    from tobkiri_host.effects import EffectDisposition
    from tobkiri_host.models import OpaqueAuthorityRef
    from tobkiri_host.ports import OpaqueInvocationLease

    request = RequestEnvelope(
        context=SimpleNamespace(),
        target_principal=OpaqueAuthorityRef("mount-principal"),
        target_domain=OpaqueAuthorityRef("domain"),
        contract_id=CONTRACT,
        contract_version="1.0.0",
        operation_id="workspace.mount.execute",
        payload={},
        request_digest=PLAN,
        deadline_monotonic=10000000.0,
        lease=OpaqueInvocationLease(b"lease"),
        idempotency_key=None,
    )
    inv = Invocation()
    writes = []

    def completed_then_stale(operation, payload, invocation):
        writes.append("mounted")
        invocation.current = False
        return {"mounted": True}

    def uncertain_write(operation, payload, invocation):
        raise ProjectMountPersistenceUncertain("unknown")

    for invoke in [completed_then_stale, uncertain_write]:
        inv.current = True
        contribution = HostProviderContributionV4(
            contract_id=CONTRACT,
            contract_version="1.0.0",
            operation_id="workspace.mount.execute",
            principal_id="mount-principal",
            artifact_digest=PLAN,
            implementation_digest=PLAN,
            domain_id="domain",
            invoke=invoke,
        )
        backend = ExactHostProviderBackendV4(
            (contribution,),
            backend_id="project",
            profile_id=PROFILE,
            plan_digest=PLAN,
            security_epoch=7,
            invocation_context=lambda envelope: inv,
        )
        assert backend.invoke(request).disposition is EffectDisposition.UNKNOWN
        assert (
            CONTRACT,
            "workspace.mount.execute",
        ) in backend.cancellation_may_leave_effect_operations
    assert writes == ["mounted"]
