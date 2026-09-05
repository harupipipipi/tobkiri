from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_runtime_router_keeps_computer_use_as_last_operation_layer() -> None:
    from ecosystem.defaultspack.domain.agent_runtime.supervisor_dashboard import (
        build_runtime_router_contract,
    )

    router = build_runtime_router_contract()

    assert router["structured_first"] is True
    assert router["computer_use_role"] == "last_operation_layer"
    assert router["preferred_order"][0] == "shell"
    assert "computer_use" not in router["preferred_order"]
    assert router["fallback_order"][-1] == "computer_use"


def test_supervisor_catalog_is_cloud_first_without_docker_desktop_requirement() -> None:
    from ecosystem.defaultspack.domain.agent_runtime.supervisor_dashboard import (
        build_supervisor_dashboard_snapshot,
    )

    snapshot = build_supervisor_dashboard_snapshot(run_store=None)
    providers = {provider["id"]: provider for provider in snapshot["sandbox_providers"]}

    assert snapshot["capabilities"] == {
        "snapshot": True,
        "live_screen": False,
        "takeover": False,
        "replay": False,
    }
    assert providers["cloud"]["default"] is True
    assert providers["cloud"]["install_required"] is False
    assert "browserbase" in providers["cloud"]["providers"]
    assert "docker_sbx" in providers["local_packaged"]["providers"]
    assert "docker_desktop" not in providers["local_packaged"]["providers"]
    assert "docker_desktop" in providers["byo_advanced"]["providers"]


def test_supervisor_snapshot_summarizes_agent_runtime_store(tmp_path) -> None:
    from ecosystem.defaultspack.domain.agent_runtime.supervisor_dashboard import (
        build_supervisor_dashboard_snapshot,
    )
    from domain.agent_runtime.models import AgentRun
    from domain.agent_runtime.run_store import AgentRunStore

    store = AgentRunStore(tmp_path / "agent_runtime.db")
    store.upsert_run(
        AgentRun(
            run_id="run_wait",
            session_key="agent:reviewer:main",
            task="Approve browser upload",
            status="waiting_approval",
            agent_id="reviewer",
            runtime_profile_json={"policy": {"risk": "high"}, "sandbox": {"provider": "browserbase"}},
            execution_json={
                "screen": {
                    "available": True,
                    "provider": "browserbase",
                    "url": "https://live.example/session",
                    "screenshot_url": "https://snapshot.example/session.png",
                },
                "replay": {"available": True, "url": "https://replay.example/session"},
                "artifacts": {"screenshots": ["one.png"], "logs": ["run.log"]},
            },
        )
    )
    store.upsert_run(
        AgentRun(
            run_id="run_stale",
            session_key="agent:coding_engineer:main",
            task="Old coding work",
            status="running",
            agent_id="coding_engineer",
            heartbeat_at="2000-01-01T00:00:00Z",
        )
    )
    store.add_event("run_wait", "approval_requested", {"tool": "browser_upload_file"})

    snapshot = build_supervisor_dashboard_snapshot(
        run_store=store,
        stale_after_seconds=1,
        event_limit=5,
    )

    assert snapshot["metrics"]["available"] is True
    assert snapshot["metrics"]["active_runs"] == 2
    assert snapshot["metrics"]["waiting_approvals"] == 1
    assert snapshot["metrics"]["stale_runs"] == 1
    assert snapshot["metrics"]["screen_sessions"] == 0
    assert snapshot["metrics"]["replay_ready"] == 0
    assert "recordings" not in snapshot["metrics"]["artifact_streams"]
    assert snapshot["selected_session"]["run_id"] == "run_wait"
    assert snapshot["selected_session"]["risk"] == "high"
    assert snapshot["selected_session"]["screen"]["available"] is False
    assert snapshot["selected_session"]["screen"]["url"] is None
    assert snapshot["selected_session"]["screen"]["screenshot_url"] == "https://snapshot.example/session.png"
    assert snapshot["selected_session"]["replay"] == {"available": False, "url": None}
    assert snapshot["recent_events"][0]["event_type"] == "approval_requested"


def test_supervisor_snapshot_redacts_poisoned_event_payload(tmp_path) -> None:
    from ecosystem.defaultspack.domain.agent_runtime.supervisor_dashboard import (
        build_supervisor_dashboard_snapshot,
    )
    from domain.agent_runtime.models import AgentRun
    from domain.agent_runtime.run_store import AgentRunStore

    store = AgentRunStore(tmp_path / "agent_runtime.db")
    store.upsert_run(
        AgentRun(
            run_id="run_poison",
            session_key="agent:reviewer:main",
            task="Review poisoned dashboard event",
            status="waiting_approval",
            agent_id="reviewer",
        )
    )
    store.add_event(
        "run_poison",
        "approval_requested",
        {
            "tool": "browser_upload_file",
            "status": "pending",
            "secret": "dashboard-secret-poison",
            "token": "dashboard-token-poison",
            "payload_json": {"raw": "dashboard-raw-payload-poison"},
            "nested": {"api_key": "dashboard-api-key-poison"},
            "reason": "x" * 500,
        },
    )

    snapshot = build_supervisor_dashboard_snapshot(run_store=store, event_limit=1)
    event = snapshot["recent_events"][0]
    serialized = repr(snapshot)

    assert event["payload"]["tool"] == "browser_upload_file"
    assert event["payload"]["status"] == "pending"
    assert len(event["payload"]["reason"]) <= 160
    assert event["payload"]["_omitted_fields"] >= 4
    assert "payload_json" not in event["payload"]
    assert "dashboard-secret-poison" not in serialized
    assert "dashboard-token-poison" not in serialized
    assert "dashboard-raw-payload-poison" not in serialized
    assert "dashboard-api-key-poison" not in serialized


def test_supervisor_snapshot_does_not_advertise_live_controls() -> None:
    from ecosystem.defaultspack.domain.agent_runtime.supervisor_dashboard import (
        build_supervisor_dashboard_snapshot,
    )

    snapshot = build_supervisor_dashboard_snapshot(run_store=None)
    live_actions = {"pause", "resume", "take_over", "open_live_screen", "open_replay"}

    assert snapshot["capabilities"]["snapshot"] is True
    assert snapshot["capabilities"]["live_screen"] is False
    assert snapshot["capabilities"]["takeover"] is False
    assert snapshot["capabilities"]["replay"] is False
    assert live_actions.isdisjoint(snapshot["action_buttons"])
    assert "replay_evidence_is_recorded" not in snapshot["security_guardrails"]
