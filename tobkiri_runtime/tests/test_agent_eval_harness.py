from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.agent_eval_harness import (
    ARTIFACT_SCHEMA,
    SUITE_SCHEMA,
    main,
    replay_failed,
    run_suite,
    smoke_suite,
)


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_offline_smoke_writes_three_machine_readable_results(tmp_path: Path) -> None:
    artifact_path = run_suite(smoke_suite(), tmp_path)
    artifact = _read(artifact_path)

    assert artifact["schema"] == ARTIFACT_SCHEMA
    assert artifact["status"] == "passed"
    assert artifact["summary"] == {"task_count": 3, "passed": 3, "failed": 0}
    assert {item["surface"] for item in artifact["tasks"]} == {
        "chat",
        "file",
        "tool",
    }
    required_evidence = {
        "transcript",
        "tool_calls",
        "approvals",
        "screenshots",
        "audit_log",
        "artifacts",
        "ui_state",
        "cost",
        "latency_ms",
        "provider_status",
    }
    assert all(required_evidence <= set(item["evidence"]) for item in artifact["tasks"])


def test_failed_eval_replays_from_artifact_without_network(tmp_path: Path) -> None:
    suite = smoke_suite()
    suite["suite_id"] = "replay-smoke"
    suite["tasks"][0]["expected"]["text"] = "different"
    original_path = run_suite(suite, tmp_path / "first")

    replay_path = replay_failed(original_path, tmp_path / "replay")
    replay = _read(replay_path)

    assert replay["status"] == "failed"
    assert replay["summary"] == {"task_count": 1, "passed": 0, "failed": 1}
    assert replay["tasks"][0]["task_id"] == "chat-exact-match"
    assert replay["tasks"][0]["mismatches"] == ["text"]
    assert replay["replay"]["source_artifact"] == str(original_path.resolve())


def test_unknown_solver_and_traversal_identifier_fail_closed(tmp_path: Path) -> None:
    suite = smoke_suite()
    suite["tasks"][0]["solver"] = "external"
    with pytest.raises(ValueError, match="not registered"):
        run_suite(suite, tmp_path / "unknown")

    suite = smoke_suite()
    suite["suite_id"] = "../outside"
    with pytest.raises(ValueError, match="suite_id is invalid"):
        run_suite(suite, tmp_path / "traversal")

    assert list(tmp_path.iterdir()) == []


def test_replay_rejects_tampered_embedded_suite(tmp_path: Path) -> None:
    suite = smoke_suite()
    suite["tasks"][0]["expected"]["text"] = "different"
    artifact_path = run_suite(suite, tmp_path / "original")
    artifact = _read(artifact_path)
    artifact["replay"]["suite"]["tasks"][0]["expected"]["text"] = "tampered"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    with pytest.raises(ValueError, match="digest mismatch"):
        replay_failed(artifact_path, tmp_path / "replay")


def test_smoke_cli_needs_no_cloud_keys(tmp_path: Path, capsys) -> None:
    assert main(["smoke", "--output-dir", str(tmp_path)]) == 0
    artifact_path = Path(capsys.readouterr().out.strip())
    assert _read(artifact_path)["schema"] == ARTIFACT_SCHEMA


def test_suite_schema_constant_is_stable() -> None:
    assert smoke_suite()["schema"] == SUITE_SCHEMA
