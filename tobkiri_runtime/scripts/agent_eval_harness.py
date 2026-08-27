#!/usr/bin/env python3
"""Run replayable, local-only Tobkiri agent evaluation fixtures.

This developer CLI deliberately supports only registered in-process solvers. It
does not import solver names, start subprocesses, call the network, or execute
host tools. Product-facing solvers must be added through the canonical V4
Broker and Authority boundary instead of extending this offline smoke runner.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping, Protocol


SUITE_SCHEMA = "io.tobkiri.agent-eval-suite.v1"
ARTIFACT_SCHEMA = "io.tobkiri.agent-eval-artifact.v1"
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class Solver(Protocol):
    """A finite, registered source of observations for one evaluation task."""

    def solve(self, task: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return an observation without performing undeclared host effects."""


class StubSolver:
    """Return checked-in observations for deterministic, keyless smoke evals."""

    def solve(self, task: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return the task's JSON observation or fail closed."""
        observation = task.get("stub_observation")
        if not isinstance(observation, Mapping):
            raise ValueError("stub solver requires a stub_observation object")
        return _json_clone(observation)


SOLVERS: dict[str, Solver] = {"stub": StubSolver()}


@dataclasses.dataclass(frozen=True)
class TaskResult:
    """Machine-readable result and evidence for one agent task."""

    task_id: str
    surface: str
    solver: str
    passed: bool
    mismatches: list[str]
    evidence: dict[str, Any]


def smoke_suite() -> dict[str, Any]:
    """Return three deterministic local smoke tasks with distinct scorers."""
    return {
        "schema": SUITE_SCHEMA,
        "suite_id": "local-agent-smoke",
        "tasks": [
            {
                "id": "chat-exact-match",
                "surface": "chat",
                "solver": "stub",
                "expected": {"text": "Tobkiri local smoke ready"},
                "stub_observation": {
                    "transcript": [
                        {"role": "user", "text": "Report local smoke state"},
                        {"role": "assistant", "text": "Tobkiri local smoke ready"},
                    ],
                    "text": "Tobkiri local smoke ready",
                    "provider_status": "offline_stub",
                },
            },
            {
                "id": "workspace-artifact-diff",
                "surface": "file",
                "solver": "stub",
                "expected": {
                    "artifacts": {
                        "notes.txt": {
                            "before": "draft\n",
                            "after": "reviewed\n",
                        }
                    }
                },
                "stub_observation": {
                    "artifacts": {
                        "notes.txt": {
                            "before": "draft\n",
                            "after": "reviewed\n",
                        }
                    },
                    "approvals": [],
                    "provider_status": "offline_stub",
                },
            },
            {
                "id": "tool-and-audit-trace",
                "surface": "tool",
                "solver": "stub",
                "expected": {
                    "tool_calls": [{"name": "calculator", "arguments": {"expression": "1 + 1"}}],
                    "audit_log": [{"event": "tool_observed", "effect": "read_only"}],
                },
                "stub_observation": {
                    "tool_calls": [{"name": "calculator", "arguments": {"expression": "1 + 1"}}],
                    "audit_log": [{"event": "tool_observed", "effect": "read_only"}],
                    "provider_status": "offline_stub",
                },
            },
        ],
    }


def load_suite(path: Path) -> dict[str, Any]:
    """Load one JSON suite without accepting non-object roots."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("eval suite must be a JSON object")
    return value


def run_suite(
    suite: Mapping[str, Any],
    output_root: Path,
    *,
    source_artifact: str | None = None,
) -> Path:
    """Run a validated suite and atomically write one replayable result artifact."""
    normalized = _validate_suite(suite)
    suite_id = str(normalized["suite_id"])
    output_root = _prepare_output_root(output_root)
    run_id = (
        dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    )
    run_directory = output_root / f"{suite_id}-{run_id}"
    run_directory.mkdir(mode=0o700)

    results = [_run_task(task) for task in normalized["tasks"]]
    passed = sum(result.passed for result in results)
    artifact = {
        "schema": ARTIFACT_SCHEMA,
        "suite_id": suite_id,
        "run_id": run_id,
        "status": "passed" if passed == len(results) else "failed",
        "summary": {
            "task_count": len(results),
            "passed": passed,
            "failed": len(results) - passed,
        },
        "tasks": [dataclasses.asdict(result) for result in results],
        "replay": {
            "suite": normalized,
            "suite_digest": _digest(normalized),
            "source_artifact": source_artifact,
            "mode": "failed_tasks_only",
        },
    }
    artifact_path = run_directory / "result.json"
    _atomic_write_json(artifact_path, artifact)
    return artifact_path


def replay_failed(artifact_path: Path, output_root: Path) -> Path:
    """Replay only failed tasks from a validated local artifact."""
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    if not isinstance(artifact, dict) or artifact.get("schema") != ARTIFACT_SCHEMA:
        raise ValueError("replay artifact schema is invalid")
    replay = artifact.get("replay")
    tasks = artifact.get("tasks")
    if not isinstance(replay, dict) or not isinstance(tasks, list):
        raise ValueError("replay artifact is incomplete")
    suite = replay.get("suite")
    if not isinstance(suite, dict) or replay.get("suite_digest") != _digest(suite):
        raise ValueError("replay suite digest mismatch")
    failed_ids = {
        str(item.get("task_id"))
        for item in tasks
        if isinstance(item, dict) and item.get("passed") is False
    }
    if not failed_ids:
        raise ValueError("artifact has no failed tasks to replay")
    replay_suite = _validate_suite(suite)
    selected = [task for task in replay_suite["tasks"] if task["id"] in failed_ids]
    if {task["id"] for task in selected} != failed_ids:
        raise ValueError("failed task is missing from replay suite")
    replay_suite = {
        "schema": SUITE_SCHEMA,
        "suite_id": _identifier(f"{replay_suite['suite_id']}-replay", "suite_id"),
        "tasks": selected,
    }
    return run_suite(
        replay_suite,
        output_root,
        source_artifact=str(artifact_path.resolve()),
    )


def _run_task(task: Mapping[str, Any]) -> TaskResult:
    solver_name = str(task["solver"])
    solver = SOLVERS.get(solver_name)
    if solver is None:
        raise ValueError(f"solver is not registered: {solver_name}")
    observation = _normalize_evidence(solver.solve(task))
    expected = task["expected"]
    mismatches = [key for key, value in expected.items() if observation.get(key) != value]
    return TaskResult(
        task_id=str(task["id"]),
        surface=str(task["surface"]),
        solver=solver_name,
        passed=not mismatches,
        mismatches=mismatches,
        evidence=observation,
    )


def _validate_suite(suite: Mapping[str, Any]) -> dict[str, Any]:
    if suite.get("schema") != SUITE_SCHEMA:
        raise ValueError("eval suite schema is invalid")
    suite_id = _identifier(suite.get("suite_id"), "suite_id")
    raw_tasks = suite.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("eval suite requires at least one task")
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_tasks:
        if not isinstance(raw, Mapping):
            raise ValueError("eval task must be an object")
        task_id = _identifier(raw.get("id"), "task id")
        if task_id in seen:
            raise ValueError(f"duplicate eval task id: {task_id}")
        seen.add(task_id)
        surface = _identifier(raw.get("surface"), "surface")
        solver = _identifier(raw.get("solver"), "solver")
        if solver not in SOLVERS:
            raise ValueError(f"solver is not registered: {solver}")
        expected = raw.get("expected")
        if not isinstance(expected, Mapping) or not expected:
            raise ValueError(f"eval task {task_id} requires expected evidence")
        tasks.append(
            {
                "id": task_id,
                "surface": surface,
                "solver": solver,
                "expected": _json_clone(expected),
                "stub_observation": _json_clone(raw.get("stub_observation")),
            }
        )
    return {"schema": SUITE_SCHEMA, "suite_id": suite_id, "tasks": tasks}


def _normalize_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    evidence = {
        "transcript": [],
        "tool_calls": [],
        "approvals": [],
        "screenshots": [],
        "audit_log": [],
        "artifacts": {},
        "ui_state": {},
        "cost": None,
        "latency_ms": 0,
        "provider_status": "not_applicable",
    }
    evidence.update(_json_clone(value))
    return evidence


def _identifier(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(text):
        raise ValueError(f"{label} is invalid")
    return text


def _json_clone(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise ValueError("eval value must be finite JSON") from error


def _digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _prepare_output_root(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    if absolute.is_symlink():
        raise ValueError("eval output root may not be a symlink")
    absolute.mkdir(parents=True, exist_ok=True)
    if not absolute.is_dir():
        raise ValueError("eval output root must be a directory")
    return absolute


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".result.",
            suffix=".json",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    """Run the local smoke suite, a JSON suite, or a failed artifact replay."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    smoke = subparsers.add_parser("smoke")
    smoke.add_argument("--output-dir", type=Path, required=True)
    run = subparsers.add_parser("run")
    run.add_argument("suite", type=Path)
    run.add_argument("--output-dir", type=Path, required=True)
    replay = subparsers.add_parser("replay")
    replay.add_argument("artifact", type=Path)
    replay.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "smoke":
            artifact_path = run_suite(smoke_suite(), args.output_dir)
        elif args.command == "run":
            artifact_path = run_suite(load_suite(args.suite), args.output_dir)
        else:
            artifact_path = replay_failed(args.artifact, args.output_dir)
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"agent eval rejected: {error}", file=sys.stderr)
        return 2
    print(str(artifact_path))
    return 0 if artifact["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
