"""Finite container task plans, cancellation, COW output and captured scope."""

from __future__ import annotations

from copy import deepcopy
import json
import base64
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from core_runtime.bounded_process_runner import (
    BoundedProcessResult,
    HostBoundedProcessRunner,
    HostProcessAttestation,
    ProcessExecutionCancelled,
)
from ecosystem.rumi_coding_sandbox_service_pack.runtime import task_container
from ecosystem.rumi_coding_sandbox_service_pack.runtime.host_v4 import (
    ContainerTaskHostFactoryV4,
    reviewed_recipe,
)
from ecosystem.rumi_coding_sandbox_service_pack.runtime.task_container import (
    CLOUD,
    ContainerTasks,
    read_work_tree,
)
from ecosystem.rumi_coding_sandbox_service_pack.runtime.task_state import TaskState
from ecosystem.tobkiri_cloud_workspace_pack.runtime.service import CloudWorkspace
from ecosystem.tobkiri_cloud_workspace_pack.runtime.store import (
    Conflict,
    WorkspaceStore,
)
from ecosystem.tobkiri_cloud_workspace_pack.runtime.task import EFFECT, TASK_RESOURCE
from tobkiri_host.artifact_materialization import MaterializedArtifactFile
from tobkiri_protocol.workspace_capsule_v1 import (
    canonical,
    digest,
    import_archive,
    make_capsule,
    export_archive,
)
from tobkiri_protocol.workspace_task_v1 import (
    execute_payload,
    task_identity,
)
from tobkiri_protocol.validation import validate_document
from tobkiri_protocol.provenance import sha256_file

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "ecosystem/rumi_coding_sandbox_service_pack"
CLOUD_PACK = ROOT / "ecosystem/tobkiri_cloud_workspace_pack"
PLAN = "sha256:" + "a" * 64
OWNER = digest(canonical(["actor", "session"]))


def process(exit_code: int = 0, **values: Any) -> BoundedProcessResult:
    """Build an external Docker fixture result, never execution evidence."""
    return BoundedProcessResult(
        exit_code=exit_code,
        stdout=values.get("stdout", ""),
        stderr=values.get("stderr", ""),
        timed_out=values.get("timed_out", False),
        stdout_truncated=False,
        stderr_truncated=False,
        attestation=HostProcessAttestation("unit-fixture", "fixture", False, "fixture"),
    )


class DockerFixture:
    """Model only external transport; no Docker execution evidence is produced."""

    def __init__(self, recipe: dict[str, Any]) -> None:
        self.recipe = recipe
        self.calls: list[dict[str, Any]] = []
        self.cleanup, self.cancel, self.bad_output = True, False, False
        self.started, self.removed, self.name_conflict = False, False, False
        self.changed_token, self.changed_id = False, False
        self.work_exit, self.container_exit = 0, 0
        self.token, self.cid = "", "b" * 64
        self.archive = b""

    def run_local(self, **request: Any) -> BoundedProcessResult:
        self.calls.append(request)
        argv = request["argv"]
        if argv[1] == "info":
            return process(stdout=json.dumps("unit-daemon"))
        if argv[1:3] == ["image", "inspect"]:
            return process(stdout=json.dumps([self.recipe["image_reference"]]))
        if argv[1] == "create":
            self.token = argv[argv.index("--label") + 1].split("=", 1)[1]
            mount = argv[argv.index("--mount") + 1]
            path = Path(mount.removeprefix("type=bind,src=").split(",dst=", 1)[0])
            self.archive = path.read_bytes()
            return (
                process(125, stderr="name conflict")
                if self.name_conflict
                else process(stdout=self.cid + "\n")
            )
        if argv[1:3] == ["container", "inspect"]:
            if self.removed and self.cleanup:
                return process(1, stderr="No such container")
            return process(
                stdout=json.dumps(
                    {
                        "id": "c" * 64
                        if self.started and self.changed_id
                        else self.cid,
                        "ownership": "foreign"
                        if self.name_conflict or (self.started and self.changed_token)
                        else self.token,
                        "state": {
                            "StartedAt": "2026-10-03T00:00:00Z"
                            if self.started
                            else "0001-01-01T00:00:00Z",
                            "Running": False,
                            "ExitCode": 137 if self.cancel else self.container_exit,
                        },
                    }
                )
            )
        if argv[1] == "rm":
            assert argv[-1] == self.cid
            self.removed = True
            return process()
        if argv[1] == "start":
            self.started = True
            if self.cancel:
                deadline = time.monotonic() + 2
                while (
                    not request["cancel_event"].is_set() and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                raise ProcessExecutionCancelled(process(137))
            if self.bad_output:
                return process(stdout="malformed guest envelope")
            manifest, blobs = import_archive(self.archive)
            files = {
                entry["path"]: blobs[entry["digest"]] for entry in manifest["files"]
            }
            files["result.txt"] = b"actual COW fixture output\n"
            result, content = make_capsule(
                workspace_id=manifest["workspace_id"],
                profile_id=manifest["source"]["profile_id"],
                plan_digest=manifest["source"]["plan_digest"],
                revision=manifest["revision"] + 1,
                parent_digest=manifest["manifest_digest"],
                recipe_digest=manifest["recipe_digest"],
                files=files,
            )
            archive = export_archive(result, content)
            receipt = {
                "exit_code": self.work_exit,
                "stdout": "completed fixture\n",
                "stderr": "",
                "timed_out": False,
                "stdout_truncated": False,
                "stderr_truncated": False,
            }
            envelope = {
                "version": "tobkiri.workspace-task-output.v1",
                "receipt": receipt,
                "archive_base64": base64.b64encode(archive).decode(),
                "archive_digest": digest(archive),
            }
            return process(self.container_exit, stdout=canonical(envelope).decode())
        raise AssertionError(argv)


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Capture isolated Cloud state and replace only the external Docker process."""
    recipe = json.loads((CLOUD_PACK / "container/task-recipe.v1.json").read_text())
    cloud = CloudWorkspace(
        WorkspaceStore(tmp_path, "defaults"),
        None,
        plan_digest=PLAN,
        actor="actor",
        request_id="initialize",
        guard=lambda: None,
        private_owner=OWNER,
    )
    head = cloud.invoke(
        "initialize",
        {
            "conversation_id": "chat-1",
            "expected_revision": 0,
            "expected_writer_epoch": 0,
        },
    )

    class CloudSource:
        def invoke(self, contract: str, operation: str, values: dict[str, Any]) -> Any:
            assert contract == CLOUD
            assert values["operation"] == "task_source"
            return cloud.task_source(
                values["workspace_id"],
                values["expected_revision"],
                values["expected_writer_epoch"],
            )

    tasks = ContainerTasks(
        TaskState(tmp_path, "defaults"),
        profile_id="defaults",
        plan_digest=PLAN,
        security_epoch=4,
        recipe=recipe,
        guest_files={
            entry["path"]: (CLOUD_PACK / entry["path"]).read_bytes()
            for entry in recipe["files"]
        },
    )
    docker = DockerFixture(recipe)
    tasks.runner = docker
    identity = {
        "path": "/fake/docker",
        "digest": PLAN,
        "inode": 1,
        "device": 1,
        "size": 42,
        "mtime_ns": 1,
        "endpoint": "unix:///fake/docker.sock",
        "socket_device": 1,
        "socket_inode": 1,
    }
    monkeypatch.setattr(task_container, "docker_identity", lambda: deepcopy(identity))
    monkeypatch.setattr(task_container, "socket_identity", lambda endpoint: (1, 1))
    request = {
        "task_request_id": "request-one",
        "profile_id": "defaults",
        "workspace_id": head["id"],
        "expected_revision": 1,
        "expected_writer_epoch": 1,
        "argv": ["python3", "main.py"],
        "timeout_seconds": 60,
    }
    return SimpleNamespace(
        tasks=tasks,
        docker=docker,
        cloud=cloud,
        client=CloudSource(),
        request=request,
        identity=identity,
        head=head,
    )


def prepared(env: Any) -> dict[str, Any]:
    """Prepare with server owner context and seal the generic coordinator payload."""
    result = env.tasks.prepare(
        env.request,
        env.client,
        owner=OWNER,
        request_id="broker-prepare",
        guard=lambda: None,
    )
    return execute_payload(env.request, result)


def execute(env: Any, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Invoke the service below Broker in isolated external-process unit tests."""
    return env.tasks.execute(
        payload,
        env.client,
        owner=OWNER,
        guard=kwargs.get("guard", lambda: None),
        cancel_event=kwargs.get("cancel_event", threading.Event()),
    )


def test_task_cow_output_is_sealed_and_replay_never_starts_a_second_container(
    environment: Any,
) -> None:
    env = environment
    payload = prepared(env)
    assert all(call["argv"][1] != "start" for call in env.docker.calls)
    receipt = execute(env, payload)
    assert receipt["status"] == "completed" and receipt["container_cleanup_verified"]
    assert receipt["diff"] == ["result.txt"]
    run = next(call for call in env.docker.calls if call["argv"][1] == "create")
    argv = run["argv"]
    assert "--pull=never" in argv and argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--log-driver") + 1] == "none"
    assert argv[argv.index("--user") + 1].split(":")[0] != "0"
    assert argv[argv.index("--entrypoint") + 1] == "python3"
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in argv and "--read-only" in argv
    assert any("size=8m,nr_inodes=512" in arg for arg in argv)
    assert all(
        arg.endswith(",readonly")
        for index, arg in enumerate(argv)
        if index > 0 and argv[index - 1] == "--mount"
    )
    assert run["policy"].allowed_argv == (tuple(argv),)
    assert run["environment"]["PATH"] == os.defpath
    assert run["environment"]["DOCKER_HOST"] == "unix:///fake/docker.sock"
    assert Path(run["environment"]["DOCKER_CONFIG"]).parent == env.tasks.state.root
    assert set(run["environment"]) == {"PATH", "DOCKER_HOST", "DOCKER_CONFIG"}
    assert not run["policy"].allow_path_search
    task_id = payload["task_plan"]["task_id"]
    result = env.tasks.resource(task_id, OWNER, export=True)
    import base64

    manifest, blobs = import_archive(base64.b64decode(result["archive_base64"]))
    assert manifest["parent_digest"] == env.head["checkpoint_digest"]
    assert manifest["revision"] == 2
    assert env.cloud.store.get(env.head["id"])["revision"] == 1
    assert b"actual COW fixture output\n" in blobs.values()
    before = len(env.docker.calls)
    assert execute(env, payload) == receipt
    assert len(env.docker.calls) == before
    assert not (env.tasks.state.root / task_id).exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("approved", True),
        ("authority_receipt", "x"),
        ("profile_id", "other"),
        ("argv", ["-bad"]),
        ("timeout_seconds", True),
        ("timeout_seconds", 121),
    ],
)
def test_prepare_rejects_client_authority_foreign_profile_and_invalid_bounds(
    environment: Any, field: str, value: Any
) -> None:
    env = environment
    request = env.request | {field: value}
    with pytest.raises((ValueError, PermissionError)):
        env.tasks.prepare(
            request, env.client, owner=OWNER, request_id="x", guard=lambda: None
        )
    assert not env.docker.calls


def test_same_nonce_changed_request_is_rejected_and_retry_preserves_sealed_expiry(
    environment: Any,
) -> None:
    env = environment
    original = prepared(env)
    assert prepared(env) == original
    changed = env.request | {"argv": ["python3", "other.py"]}
    assert task_identity(changed, OWNER) == task_identity(env.request, OWNER)
    with pytest.raises(PermissionError, match="rebound"):
        env.tasks.prepare(
            changed, env.client, owner=OWNER, request_id="another", guard=lambda: None
        )
    assert (
        env.tasks.resource(original["task_plan"]["task_id"], OWNER, export=False)[
            "status"
        ]
        == "prepared"
    )


def test_profile_nonce_cannot_be_rebound_by_a_new_originating_session(
    environment: Any,
) -> None:
    env = environment
    prepared(env)
    for request in (env.request, env.request | {"argv": ["python3", "changed.py"]}):
        with pytest.raises(PermissionError, match="nonce.*rebound"):
            env.tasks.prepare(
                request,
                env.client,
                owner="another-session-owner",
                request_id="new-outer",
                guard=lambda: None,
            )
    assert not any(call["argv"][1] == "start" for call in env.docker.calls)


@pytest.mark.parametrize("fence", ["release", "expiry", "expiry-renew"])
def test_cloud_apply_rejects_completed_result_after_writer_release_or_expiry(
    environment: Any, fence: str
) -> None:
    env = environment
    payload = prepared(env)
    assert execute(env, payload)["status"] == "completed"
    env.cloud.store.record_task(
        env.head["id"],
        "actor",
        {
            "task_id": payload["task_plan"]["task_id"],
            "effect_id": "effect-one",
            "request": env.request,
            "checkpoint_digest": env.head["checkpoint_digest"],
        },
        lambda: None,
    )

    class OutputPort:
        def invoke(self, contract: str, operation: str, request: dict[str, Any]) -> Any:
            assert contract == TASK_RESOURCE and request["operation"] == "export"
            return env.tasks.resource(request["task_id"], OWNER, export=True)

    env.cloud.client = OutputPort()
    if fence == "release":
        env.cloud.store.release_writer(
            env.head["id"],
            expected_revision=1,
            expected_writer_epoch=1,
            actor="actor",
            guard=lambda: None,
        )
    else:
        with env.cloud.store.connection(write=True) as db:
            db.execute("UPDATE heads SET expiry=0")
        if fence == "expiry-renew":
            assert (
                env.cloud.store.renew_task_writer(
                    env.head["id"], 1, 1, "actor", lambda: None
                )
                == 2
            )
    head = env.cloud.store.get(env.head["id"])
    env.cloud.request_id = "apply-stale-result"
    with pytest.raises((PermissionError, Conflict)):
        env.cloud.invoke(
            "task_apply",
            {
                "conversation_id": "chat-1",
                "expected_revision": 1,
                "expected_writer_epoch": head["writer_epoch"],
            },
        )
    assert env.cloud.store.get(env.head["id"])["revision"] == 1


@pytest.mark.parametrize(
    "change", ["expiry", "plan", "epoch", "recipe", "checkpoint", "writer", "docker"]
)
def test_execute_rechecks_current_scope_source_writer_recipe_and_executable(
    environment: Any, change: str
) -> None:
    env = environment
    payload = prepared(env)
    if change == "expiry":
        env.tasks.clock = lambda: payload["task_plan"]["expires_at_ms"] / 1000 + 1
    elif change == "plan":
        env.tasks.plan_digest = "sha256:" + "b" * 64
    elif change == "epoch":
        env.tasks.security_epoch = 5
    elif change == "recipe":
        env.tasks.recipe_digest = "sha256:" + "b" * 64
    elif change == "docker":
        env.identity["inode"] = 2
    elif change == "writer":
        with env.cloud.store.connection(write=True) as db:
            db.execute("UPDATE heads SET expiry=0")
    else:
        env.cloud.store.release_writer(
            env.head["id"],
            expected_revision=1,
            expected_writer_epoch=1,
            actor="actor",
            guard=lambda: None,
        )
    with pytest.raises((PermissionError, Conflict)):
        execute(env, payload)
    assert not any(call["argv"][1] == "start" for call in env.docker.calls)


def test_cancellation_reaps_named_container_before_returning_and_never_seals_output(
    environment: Any,
) -> None:
    env = environment
    payload = prepared(env)
    env.docker.cancel = True
    signal = threading.Event()
    timer = threading.Timer(0.05, signal.set)
    timer.start()
    receipt = execute(env, payload, cancel_event=signal)
    timer.join()
    assert receipt["status"] == "cancelled" and receipt["container_cleanup_verified"]
    assert "output_checkpoint_digest" not in receipt
    assert [call["argv"][1] for call in env.docker.calls][-2:] == ["rm", "container"]
    with pytest.raises(LookupError):
        env.tasks.resource(payload["task_plan"]["task_id"], OWNER, export=True)


def test_failed_daemon_cleanup_retains_ambiguous_record_and_does_not_publish_output(
    environment: Any,
) -> None:
    env = environment
    payload = prepared(env)
    env.docker.cleanup = False
    receipt = execute(env, payload)
    assert (
        receipt["status"] == "ambiguous" and not receipt["container_cleanup_verified"]
    )
    assert (env.tasks.state.root / payload["task_plan"]["task_id"]).exists()
    assert execute(env, payload) == receipt


@pytest.mark.parametrize("kind", ["workload", "container"])
def test_nonzero_guest_or_container_exit_never_produces_completed_output(
    environment: Any, kind: str
) -> None:
    env = environment
    payload = prepared(env)
    if kind == "workload":
        env.docker.work_exit = 7
    else:
        env.docker.container_exit = 7
    result = execute(env, payload)
    assert result["status"] == "failed" and result["container_cleanup_verified"]
    with pytest.raises(LookupError):
        env.tasks.resource(payload["task_plan"]["task_id"], OWNER, export=True)


def test_create_name_conflict_never_removes_a_foreign_container(
    environment: Any,
) -> None:
    env = environment
    payload = prepared(env)
    env.docker.name_conflict = True
    result = execute(env, payload)
    assert result["status"] == "failed" and not result["executed"]
    assert not any(call["argv"][1] in {"rm", "start"} for call in env.docker.calls)


def test_daemon_or_local_socket_change_fails_before_task_execution(
    environment: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment
    payload = prepared(env)
    env.identity["socket_inode"] = 2
    with pytest.raises(PermissionError):
        execute(env, payload)
    assert not any(call["argv"][1] == "create" for call in env.docker.calls)


@pytest.mark.parametrize("field", ["changed_token", "changed_id"])
def test_confirmed_container_identity_change_never_claims_verified_cleanup(
    environment: Any, field: str
) -> None:
    env = environment
    payload = prepared(env)
    setattr(env.docker, field, True)
    result = execute(env, payload)
    assert result["status"] == "ambiguous"
    assert not result["container_cleanup_verified"]
    assert not any(call["argv"][1] == "rm" for call in env.docker.calls)
    assert (env.tasks.state.root / payload["task_plan"]["task_id"]).exists()
    with pytest.raises(LookupError):
        env.tasks.resource(payload["task_plan"]["task_id"], OWNER, export=True)


def test_private_object_label_survives_actual_host_transport_redaction(
    environment: Any,
) -> None:
    env = environment
    original = env.docker.run_local

    def redacted(**request: Any) -> BoundedProcessResult:
        result = original(**request)
        return process(
            result.exit_code,
            stdout=HostBoundedProcessRunner._redact(result.stdout, request["policy"]),
            stderr=HostBoundedProcessRunner._redact(result.stderr, request["policy"]),
        )

    env.tasks.runner = SimpleNamespace(run_local=redacted)
    receipt = execute(env, prepared(env))
    assert receipt["status"] == "completed"
    assert receipt["container_cleanup_verified"]


def test_guard_failure_before_claim_starts_nothing_and_invalid_guest_output_is_rejected(
    environment: Any,
) -> None:
    env = environment
    payload = prepared(env)

    def denied() -> None:
        raise PermissionError("captured activation expired")

    with pytest.raises(PermissionError):
        execute(env, payload, guard=denied)
    env.docker.bad_output = True
    result = execute(env, payload)
    assert result["status"] == "failed" and result["container_cleanup_verified"]
    with pytest.raises(LookupError):
        env.tasks.resource(payload["task_plan"]["task_id"], OWNER, export=True)


def test_private_owner_and_profile_cannot_read_another_task(
    environment: Any, tmp_path: Path
) -> None:
    env = environment
    payload = prepared(env)
    with pytest.raises(LookupError):
        env.tasks.resource(payload["task_plan"]["task_id"], "other-owner", export=False)
    other = TaskState(tmp_path, "another-profile")
    with pytest.raises(LookupError):
        other.read(payload["task_plan"]["task_id"], OWNER)
    assert (
        "owner" not in canonical(payload).decode()
        and str(tmp_path) not in canonical(payload).decode()
    )


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "secret", "reserved", "file_limit", "entries"]
)
def test_output_inspection_is_descriptor_safe_and_bounded(
    tmp_path: Path, kind: str
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    if kind == "symlink":
        (work / "x").symlink_to("/tmp")
    elif kind == "hardlink":
        (work / "x").write_bytes(b"x")
        os.link(work / "x", work / "y")
    elif kind == "secret":
        (work / ".env").write_text("excluded")
    elif kind == "reserved":
        (work / "capsule-manifest.json").mkdir()
    elif kind == "entries":
        for i in range(513):
            (work / f"d{i}").mkdir()
    else:
        (work / "x").write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises((ValueError, PermissionError)):
        read_work_tree(work)


def test_current_source_artifacts_schemas_and_guest_protocol_copy_are_exact() -> None:
    assert (CLOUD_PACK / "container/workspace_capsule_v1.py").read_bytes() == (
        ROOT / "tobkiri_protocol/workspace_capsule_v1.py"
    ).read_bytes()
    for filename, schema in [
        ("pack.v4.json", "pack_manifest_v4.schema.json"),
        ("contracts.v4.json", "pack_contract_catalog_v4.schema.json"),
        ("executables.v4.json", "executable_catalog_v4.schema.json"),
    ]:
        validate_document(json.loads((PACK / filename).read_text()), schema)
    manifest = json.loads((PACK / "pack.v4.json").read_text())
    for artifact in manifest["artifacts"]:
        assert artifact["digest"] == sha256_file(PACK / artifact["path"])
    assert len([f for f in manifest["functions"] if ".task-" in f["id"]]) == 3
    compatibility = json.loads((PACK / "compatibility-source.v1.json").read_text())
    assert (
        sha256_file(PACK / "runtime/sandbox.py")
        == compatibility["pack"]["functions"][0]["implementation_digest"]
    )


def test_captured_factory_rejects_foreign_scope_and_unpinned_recipe(
    environment: Any, tmp_path: Path
) -> None:
    env = environment
    factory = ContainerTaskHostFactoryV4("prepare")
    binding = SimpleNamespace(
        function=SimpleNamespace(
            function_id=factory.function_id, implementation_digest=PLAN
        ),
        operation=SimpleNamespace(
            contract_id=factory.contract_id,
            operation_id=factory.operation_id,
            contract_version="1.0.0",
        ),
        principal_ref=SimpleNamespace(value="provider"),
        artifact=SimpleNamespace(digest=PLAN),
    )
    content = canonical(env.tasks.recipe)
    data = SimpleNamespace(
        pack_id="tobkiri_cloud_workspace_pack",
        path_prefix="container/",
        files=(
            MaterializedArtifactFile(
                "container/task-recipe.v1.json", digest(content), False, content
            ),
            *tuple(
                MaterializedArtifactFile(path, digest(data), False, data)
                for path, data in env.tasks.guest_files.items()
            ),
        ),
    )
    context = SimpleNamespace(
        user_data_root=tmp_path,
        provider_bindings=(binding,),
        profile_id="defaults",
        plan_digest=PLAN,
        security_epoch=4,
        declared_pack_data=(data,),
        domain_ids={(factory.contract_id, factory.operation_id, "provider"): "domain"},
    )
    contribution = factory.capture(context).contributions[0]
    captured = SimpleNamespace(
        profile_id="defaults",
        plan_digest=PLAN,
        security_epoch=4,
        request_id="r",
        target_domain_id="domain",
    )
    invocation = SimpleNamespace(
        assert_current=lambda: None,
        envelope=SimpleNamespace(
            context=captured, target_principal=SimpleNamespace(value="provider")
        ),
        presentation_owner_principal_id="actor",
        presentation_owner_session_id="session",
        contract_client=lambda **kwargs: env.client,
    )
    for field, value in [
        ("profile_id", "other"),
        ("plan_digest", "sha256:" + "b" * 64),
        ("security_epoch", 5),
        ("target_domain_id", "other"),
    ]:
        previous = getattr(captured, field)
        setattr(captured, field, value)
        with pytest.raises(PermissionError, match="scope"):
            contribution.invoke(factory.operation_id, env.request, invocation)
        setattr(captured, field, previous)
    mutable = env.tasks.recipe | {"image_reference": "python:latest"}
    raw = canonical(mutable)
    data.files = (
        MaterializedArtifactFile(
            "container/task-recipe.v1.json", digest(raw), False, raw
        ),
    )
    with pytest.raises(ValueError, match="pinned"):
        reviewed_recipe(context)


def test_cloud_public_orchestration_requires_approval_then_applies_verified_output_with_cas(
    environment: Any,
) -> None:
    env = environment

    class PublicPorts:
        approved = False
        pending: dict[str, Any] = {}

        def invoke(self, contract: str, operation: str, values: dict[str, Any]) -> Any:
            if contract == CLOUD:
                return env.client.invoke(contract, operation, values)
            if contract == TASK_RESOURCE:
                if values["operation"] == "availability":
                    return {"status": "ready"}
                return env.tasks.resource(
                    values["task_id"], OWNER, export=values["operation"] == "export"
                )
            assert contract == EFFECT and operation == "interactive_effect.manage"
            if values["phase"] == "prepare":
                result = env.tasks.prepare(
                    values["request"],
                    self,
                    owner=OWNER,
                    request_id="nested",
                    guard=lambda: None,
                )
                self.pending = execute_payload(values["request"], result)
                return {"effect_id": "effect-one", "state": "awaiting_approval"}
            if values["phase"] == "resume" and self.approved:
                env.tasks.execute(
                    self.pending,
                    self,
                    owner=OWNER,
                    guard=lambda: None,
                    cancel_event=threading.Event(),
                )
                return {"state": "completed"}
            return {"state": "awaiting_approval"}

    ports = PublicPorts()
    env.cloud.client = ports
    values = {
        "conversation_id": "chat-1",
        "expected_revision": 1,
        "expected_writer_epoch": 1,
    }
    env.cloud.request_id = "task-start"
    result = env.cloud.invoke(
        "task_prepare", values | {"argv_json": '["python3", "main.py"]'}
    )
    assert result["task"]["status"] == "awaiting_approval"
    env.cloud.request_id = "task-resume"
    env.cloud.invoke("task_resume", values)
    assert not any(call["argv"][1] == "start" for call in env.docker.calls)
    ports.approved = True
    env.cloud.invoke("task_resume", values)
    env.cloud.request_id = "task-apply"
    head = env.cloud.invoke("task_apply", values)
    assert head["revision"] == 2
    assert env.cloud.invoke("task_apply", values) == head
    assert env.cloud.snapshot("chat-1")["task"]["receipt"]["status"] == "completed"
