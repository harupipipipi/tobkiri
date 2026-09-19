from __future__ import annotations

import multiprocessing
import os
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _json_store_increment_worker(path: str, barrier, queue) -> None:
    from ecosystem.defaultspack.backend.continuity.store import JsonFileStore

    store = JsonFileStore(path)
    try:
        barrier.wait(timeout=10)

        def _increment(data):
            current = int(data.get("value") or 0)
            time.sleep(0.1)
            data["value"] = current + 1
            return data, data["value"]

        queue.put(("ok", store.update(_increment)))
    except BaseException as exc:  # pragma: no cover - surfaced through parent process assertion
        queue.put(("error", repr(exc)))


@pytest.fixture()
def continuity_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CONTINUITY_DIR", str(tmp_path / "continuity"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_SECRETS_DIR", str(tmp_path / "secrets"))
    for key in ("OPENAI_API_KEY", "OLLAMA_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    yield tmp_path


def _save_named_key(provider_id: str, api_id: str, value: str, **metadata):
    from domain.ai_client.api_key_store import set_provider_api_key

    result = set_provider_api_key(provider_id, value, api_id=api_id, **metadata)
    assert result["success"] is True
    return result


def test_provider_route_preserves_exact_named_api_route_and_blocks_local(continuity_env):
    from ecosystem.defaultspack.backend.continuity import ContinuityCoordinator

    _save_named_key(
        "openai",
        "primary",
        "test-openai-secret",
        default_model="gpt-4.1",
        allowed_models=["gpt-4.1"],
    )
    _save_named_key(
        "ollama",
        "local",
        "test-ollama-secret",
        base_url="http://127.0.0.1:11434/v1",
        default_model="llama3",
        allowed_models=["llama3"],
    )
    coordinator = ContinuityCoordinator()
    destination = coordinator.node_registry.register_destination(display_name="Workstation")

    routes = coordinator.list_provider_routes()["routes"]
    openai_route = next(route for route in routes if route["provider_id"] == "openai" and route["api_id"] == "primary")
    ollama_route = next(route for route in routes if route["provider_id"] == "ollama" and route["api_id"] == "local")

    assert openai_route["qualified_route"] == "openai/primary/gpt-4.1"
    assert openai_route["credential_ref"].startswith("RUMIAPI_OPENAI_PRIMARY")
    assert openai_route["portable"] is True
    assert ollama_route["portable"] is False
    assert ollama_route["blocked_reason"] == "LOCAL_MODEL_PROVIDER_NOT_PORTABLE"

    probe = coordinator.probe_provider_route(
        {
            "route_id": openai_route["route_id"],
            "destination_node_id": destination["node_id"],
        }
    )
    assert probe["ok"] is True
    assert {check["code"] for check in probe["checks"]} >= {"DESTINATION_ENDPOINT_REACHABLE", "CREDENTIAL_REFERENCE_CONFIGURED"}


def test_handoff_start_is_scope_gated_and_keeps_source_primary(continuity_env):
    from ecosystem.defaultspack.backend.continuity import ContinuityCoordinator

    secret = "test-provider-secret-value"
    _save_named_key(
        "openai",
        "primary",
        secret,
        default_model="gpt-4.1",
        allowed_models=["gpt-4.1"],
    )
    coordinator = ContinuityCoordinator()
    destination = coordinator.node_registry.register_destination(display_name="Workstation")
    source_node_id = coordinator.node_registry.local_node()["node_id"]
    coordinator.primary_leases.acquire("sandbox-123", source_node_id, generation=1)

    result = coordinator.start_handoff(
        {
            "sandbox_id": "sandbox-123",
            "destination_node_id": destination["node_id"],
            "provider_id": "openai",
            "api_id": "primary",
            "model_id": "gpt-4.1",
            "mode": "move",
        }
    )

    operation = result["operation"]
    assert operation["status"] == "PAUSED_USER_ACTION"
    assert operation["code"] == "CONTINUITY_REMOTE_HANDOFF_UNAVAILABLE"
    assert operation["destination_primary"] is False
    assert operation["source_primary"] is True
    assert coordinator.primary_leases.get("sandbox-123")["owner_node_id"] == source_node_id

    credential_path = Path(os.environ["RUMI_DEFAULTSPACK_CONTINUITY_DIR"]) / "credential_envelopes.json"
    if credential_path.exists():
        assert secret not in credential_path.read_text(encoding="utf-8")


def test_handoff_pauses_when_preflight_rejects_source_only_provider(continuity_env):
    from ecosystem.defaultspack.backend.continuity import ContinuityCoordinator

    _save_named_key(
        "ollama",
        "local",
        "test-ollama-secret",
        base_url="http://127.0.0.1:11434/v1",
        default_model="llama3",
        allowed_models=["llama3"],
    )
    coordinator = ContinuityCoordinator()
    destination = coordinator.node_registry.register_destination(display_name="Workstation")

    result = coordinator.start_handoff(
        {
            "sandbox_id": "sandbox-local",
            "destination_node_id": destination["node_id"],
            "provider_id": "ollama",
            "api_id": "local",
            "model_id": "llama3",
        }
    )

    operation = result["operation"]
    assert operation["status"] == "PAUSED_USER_ACTION"
    assert operation["plan"]["status"] == "blocked"
    errors = operation["plan"]["resource_preflight"]["errors"]
    assert errors[0]["code"] == "LOCAL_MODEL_PROVIDER_NOT_PORTABLE"


def test_checkpoint_manifest_rejects_secret_looking_state(continuity_env):
    from ecosystem.defaultspack.backend.continuity import ContinuityCoordinator
    from ecosystem.defaultspack.backend.continuity.errors import ContinuityError

    _save_named_key(
        "openai",
        "primary",
        "test-openai-secret",
        default_model="gpt-4.1",
        allowed_models=["gpt-4.1"],
    )
    coordinator = ContinuityCoordinator()
    with pytest.raises(ContinuityError) as exc:
        coordinator.checkpoint(
            {
                "sandbox_id": "sandbox-secret",
                "provider_id": "openai",
                "api_id": "primary",
                "model_id": "gpt-4.1",
                "state": {"api_key": "should-not-be-here"},
            }
        )
    assert exc.value.code == "CHECKPOINT_SECRET_LEAK"


def test_json_file_store_update_is_cross_process_atomic(continuity_env):
    from ecosystem.defaultspack.backend.continuity.store import JsonFileStore

    path = Path(os.environ["RUMI_DEFAULTSPACK_CONTINUITY_DIR"]) / "race.json"
    JsonFileStore(path).write({"value": 0})
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    processes = [
        ctx.Process(target=_json_store_increment_worker, args=(str(path), barrier, queue))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    results = [queue.get(timeout=5) for _ in processes]

    assert all(process.exitcode == 0 for process in processes)
    assert all(status == "ok" for status, _ in results)
    assert JsonFileStore(path).read()["value"] == 2


def test_continuity_requires_captured_handoff_operation():
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "GET",
        "/api/continuity/nodes",
        "tobkiri.continuity.v1",
        "defaultspack.continuity.list-nodes",
    )
