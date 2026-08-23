"""Evidence-bound contracts for native and harness-backed worktree tasks.

The module deliberately does not create worktrees or launch agents.  It is the
durable, provider-neutral admission and evidence boundary used immediately
before those owner runtimes are invoked.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import sqlite3
import threading
import time
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence


CONTRACT_VERSION = "io.tobkiri.worktree-team-task.v1"
HANDOFF_VERSION = "io.tobkiri.worktree-team-handoff.v1"

ROLES = {"implementation", "review", "integration_pm", "package", "gui"}
HARNESS_KINDS = {"native", "external"}
GATE_RESULTS = {"PASS", "FAIL", "UNVERIFIED"}
BLOCKER_CLASSES = {
    "product_source",
    "workflow",
    "harness_environment",
    "external_state",
    "policy",
}
PROMOTION_STATES = ("candidate", "reviewed", "stable", "final")
ONE_SHOT_OPERATIONS = ("commit", "build", "package", "gui", "push")
WORKTREE_TASK_PRESETS: dict[str, dict[str, Any]] = {
    "one_commit_implementation": {
        "role": "implementation",
        "required_gates": ["source", "tests", "review"],
        "attempt_budgets": {"commit": 1, "build": 1, "package": 0, "gui": 0, "push": 0},
    },
    "read_only_adversarial_review": {
        "role": "review",
        "required_gates": ["provenance", "scope", "security", "tests"],
        "attempt_budgets": {operation: 0 for operation in ONE_SHOT_OPERATIONS},
    },
    "one_shot_package_gui": {
        "role": "package",
        "required_gates": ["source", "package", "gui", "artifact"],
        "attempt_budgets": {"commit": 0, "build": 1, "package": 1, "gui": 1, "push": 0},
    },
    "integration_reconciliation": {
        "role": "integration_pm",
        "required_gates": ["parents", "conflicts", "tests", "review"],
        "attempt_budgets": {"commit": 1, "build": 1, "package": 0, "gui": 0, "push": 0},
    },
    "external_state_recovery": {
        "role": "review",
        "required_gates": ["external_state", "attempt_budget", "recovery"],
        "attempt_budgets": {operation: 0 for operation in ONE_SHOT_OPERATIONS},
    },
    "final_provenance_audit": {
        "role": "review",
        "required_gates": ["provenance", "evidence", "security", "final"],
        "attempt_budgets": {operation: 0 for operation in ONE_SHOT_OPERATIONS},
    },
}
SENSITIVE_KEYS = re.compile(
    r"(?:api[_-]?key|access[_-]?token|bearer|credential|password|private[_-]?key|secret|environment|env)",
    re.IGNORECASE,
)
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class WorktreeContractError(ValueError):
    """Typed fail-closed contract error."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def normalize_task_request(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact manifest exposed before task/worktree creation."""

    raw = deepcopy(dict(value))
    _reject_sensitive_values(raw)
    task_id = _required_identifier(raw, "task_id")
    parent_id = _optional_identifier(raw.get("parent_id"), "parent_id")
    pm_id = _required_identifier(raw, "pm_id")
    role = str(raw.get("role") or "").strip().lower()
    if role not in ROLES:
        raise WorktreeContractError("ROLE_INVALID", f"Unsupported worktree task role: {role or '<empty>'}")

    starting = _normalize_provenance(raw.get("starting"), require_clean=True)
    ownership = _normalize_ownership(raw.get("ownership"))
    dependencies = _identifiers(raw.get("dependencies"), "dependencies")
    predecessor_pass = _identifiers(
        raw.get("required_predecessor_pass"), "required_predecessor_pass"
    )
    if not set(predecessor_pass).issubset(set(dependencies)):
        raise WorktreeContractError(
            "PREDECESSOR_NOT_DEPENDENCY",
            "Every required predecessor PASS row must also be a dependency",
        )

    model_policy = _mapping(raw.get("model_policy"), "model_policy")
    if any(key in model_policy for key in ("vendor", "provider")):
        raise WorktreeContractError(
            "MODEL_VENDOR_PINNED",
            "The worktree contract accepts role/capability policy, not a hard-coded model vendor",
        )
    harness = _mapping(raw.get("harness") or {"kind": "native"}, "harness")
    harness_kind = str(harness.get("kind") or "native").strip().lower()
    if harness_kind not in HARNESS_KINDS:
        raise WorktreeContractError("HARNESS_INVALID", "Harness kind must be native or external")
    harness_id = _optional_identifier(harness.get("adapter_id"), "harness.adapter_id")
    if harness_kind == "external" and not harness_id:
        raise WorktreeContractError("HARNESS_ADAPTER_REQUIRED", "External harnesses require adapter_id")

    estimates = _mapping(raw.get("estimates") or {}, "estimates")
    attempt_budgets = _mapping(raw.get("attempt_budgets") or {}, "attempt_budgets")
    budgets: dict[str, int] = {}
    for operation in ONE_SHOT_OPERATIONS:
        budget = attempt_budgets.get(operation, 1)
        if isinstance(budget, bool) or not isinstance(budget, int) or not 0 <= budget <= 16:
            raise WorktreeContractError(
                "ATTEMPT_BUDGET_INVALID", f"{operation} attempt budget must be an integer from 0 to 16"
            )
        budgets[operation] = budget

    required_gates = _ordered_unique_strings(raw.get("required_gates") or ["tests", "review"])
    if not required_gates:
        raise WorktreeContractError("GATES_REQUIRED", "At least one evidence gate is required")
    required_evidence = _unique_strings(raw.get("required_evidence") or [])
    handoff_fields = _unique_strings(
        raw.get("required_handoff_fields")
        or [
            "output",
            "changed_files",
            "changed_fields",
            "commands",
            "evidence",
            "gate_matrix",
        ]
    )

    manifest = {
        "contract_version": CONTRACT_VERSION,
        "task_id": task_id,
        "parent_id": parent_id,
        "pm_id": pm_id,
        "role": role,
        "model_policy": dict(sorted(model_policy.items())),
        "starting": starting,
        "ownership": ownership,
        "dependencies": dependencies,
        "required_predecessor_pass": predecessor_pass,
        "estimates": {
            "checkout_bytes": _bounded_integer(estimates.get("checkout_bytes", 0), "checkout_bytes"),
            "output_bytes": _bounded_integer(estimates.get("output_bytes", 0), "output_bytes"),
        },
        "attempt_budgets": budgets,
        "forbidden_capabilities": _unique_strings(raw.get("forbidden_capabilities") or []),
        "forbidden_paths": _paths(raw.get("forbidden_paths") or [], allow_glob=True),
        "required_gates": required_gates,
        "required_evidence": required_evidence,
        "required_handoff_fields": handoff_fields,
        "harness": {"kind": harness_kind, "adapter_id": harness_id},
        "stop": {
            "first_material_blocker": True,
            "blind_retry": False,
        },
    }
    manifest["contract_digest"] = _digest(manifest)
    return manifest


def worktree_task_preset(name: str) -> dict[str, Any]:
    """Return a mutable copy of one vendor-neutral task/handoff preset."""

    try:
        return deepcopy(WORKTREE_TASK_PRESETS[str(name).strip()])
    except KeyError as error:
        raise WorktreeContractError("PRESET_NOT_FOUND", "Unknown worktree task preset") from error


class WorktreeTeamLedger:
    """SQLite owner for admission, attempts, gates, handoffs, and provenance."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._initialize()

    def preview(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return normalize_task_request(request)

    def admit(self, request: Mapping[str, Any]) -> dict[str, Any]:
        manifest = normalize_task_request(request)
        now = time.time()
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM worktree_tasks WHERE task_id = ?", (manifest["task_id"],)
            ).fetchone():
                raise WorktreeContractError("TASK_EXISTS", "Task ID already exists")
            self._require_predecessors(connection, manifest)
            conflicts = self._ownership_conflicts(connection, manifest["ownership"])
            status = "hold" if conflicts else "admitted"
            record = {
                "manifest": manifest,
                "status": status,
                "promotion_state": "candidate",
                "first_blocker": None,
                "gates": {},
                "handoff": None,
                "ownership_conflicts": conflicts,
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            }
            connection.execute(
                "INSERT INTO worktree_tasks(task_id, status, record_json, revision, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (manifest["task_id"], status, _json(record), now, now),
            )
            if not conflicts:
                self._insert_claims(connection, manifest)
            self._event(connection, manifest["task_id"], "task.admitted" if not conflicts else "task.hold", {
                "conflicts": conflicts,
                "contract_digest": manifest["contract_digest"],
            })
        return deepcopy(record)

    def get(self, task_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM worktree_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise WorktreeContractError("TASK_NOT_FOUND", "Worktree task does not exist")
        return json.loads(row[0])

    def consume_operation(
        self,
        task_id: str,
        operation: str,
        operation_identity: str,
        *,
        result: str = "started",
    ) -> dict[str, Any]:
        operation = str(operation).strip().lower()
        identity = str(operation_identity).strip()
        if operation not in ONE_SHOT_OPERATIONS or not identity:
            raise WorktreeContractError("OPERATION_INVALID", "A known operation and exact identity are required")
        if result not in {"started", "completed", "failed", "indeterminate"}:
            raise WorktreeContractError("OPERATION_RESULT_INVALID", "Unsupported operation result")
        with self._transaction() as connection:
            record = self._record_for_update(connection, task_id)
            _require_active(record)
            prior = connection.execute(
                "SELECT receipt_json FROM worktree_attempts WHERE task_id = ? AND operation_identity = ?",
                (task_id, identity),
            ).fetchone()
            if prior is not None:
                receipt = json.loads(prior[0])
                receipt["replayed"] = True
                return receipt
            used = connection.execute(
                "SELECT COUNT(*) FROM worktree_attempts WHERE task_id = ? AND operation = ?",
                (task_id, operation),
            ).fetchone()[0]
            budget = int(record["manifest"]["attempt_budgets"][operation])
            if used >= budget:
                raise WorktreeContractError(
                    "ATTEMPT_BUDGET_EXHAUSTED",
                    f"The {operation} attempt budget is exhausted; blind retry is forbidden",
                )
            receipt = {
                "task_id": task_id,
                "operation": operation,
                "operation_identity": identity,
                "attempt": used + 1,
                "budget": budget,
                "result": result,
                "consumed": True,
                "replayed": False,
            }
            connection.execute(
                "INSERT INTO worktree_attempts(task_id, operation, operation_identity, receipt_json) "
                "VALUES (?, ?, ?, ?)",
                (task_id, operation, identity, _json(receipt)),
            )
            self._event(connection, task_id, "operation.consumed", receipt)
            return receipt

    def record_gate(
        self,
        task_id: str,
        gate: str,
        outcome: str,
        *,
        blocker_class: str | None = None,
        evidence_refs: Sequence[str] = (),
    ) -> dict[str, Any]:
        gate = str(gate).strip()
        outcome = str(outcome).strip().upper()
        if outcome not in GATE_RESULTS:
            raise WorktreeContractError("GATE_RESULT_INVALID", "Gate result must be PASS, FAIL, or UNVERIFIED")
        with self._transaction() as connection:
            record = self._record_for_update(connection, task_id)
            _require_active(record)
            required = record["manifest"]["required_gates"]
            if gate not in required:
                raise WorktreeContractError("GATE_UNKNOWN", "Gate is not declared in the task manifest")
            first_blocker = record.get("first_blocker")
            gate_index = required.index(gate)
            if first_blocker and gate_index > required.index(first_blocker["gate"]):
                outcome = "UNVERIFIED"
                blocker_class = None
            elif outcome == "FAIL":
                if blocker_class not in BLOCKER_CLASSES:
                    raise WorktreeContractError("BLOCKER_CLASS_REQUIRED", "A typed material blocker is required")
                if first_blocker is None:
                    first_blocker = {"gate": gate, "class": blocker_class}
                    record["first_blocker"] = first_blocker
                    for later in required[gate_index + 1 :]:
                        record["gates"][later] = {
                            "outcome": "UNVERIFIED",
                            "evidence_refs": [],
                            "reason": "first_material_blocker",
                        }
            row = {
                "outcome": outcome,
                "evidence_refs": _unique_strings(evidence_refs),
                "blocker_class": blocker_class if outcome == "FAIL" else None,
            }
            record["gates"][gate] = row
            self._save_record(connection, task_id, record)
            self._event(connection, task_id, "gate.recorded", {"gate": gate, **row})
            return deepcopy(record)

    def complete(self, task_id: str, handoff: Mapping[str, Any]) -> dict[str, Any]:
        _reject_sensitive_values(handoff)
        with self._transaction() as connection:
            record = self._record_for_update(connection, task_id)
            _require_active(record)
            manifest = record["manifest"]
            packet = self._normalize_handoff(manifest, record, handoff)
            record["handoff"] = packet
            record["status"] = packet["overall"].lower()
            record["promotion_state"] = "candidate"
            self._save_record(connection, task_id, record)
            self._event(connection, task_id, "task.completed", {
                "overall": packet["overall"],
                "output": packet["output"],
                "handoff_digest": packet["handoff_digest"],
            })
            return deepcopy(record)

    def cancel(self, task_id: str, *, reason: str) -> dict[str, Any]:
        if not str(reason).strip():
            raise WorktreeContractError("CANCELLATION_REASON_REQUIRED", "Cancellation requires a reason")
        with self._transaction() as connection:
            record = self._record_for_update(connection, task_id)
            record["status"] = "cancelled"
            record["first_blocker"] = {"gate": "cancellation", "class": "workflow"}
            for gate in record["manifest"]["required_gates"]:
                record["gates"].setdefault(gate, {"outcome": "UNVERIFIED", "evidence_refs": []})
            self._save_record(connection, task_id, record)
            self._event(connection, task_id, "task.cancelled", {"reason": str(reason)[:512]})
            return deepcopy(record)

    def promote(self, task_id: str, state: str, *, exact_output_digest: str) -> dict[str, Any]:
        state = str(state).strip().lower()
        if state not in PROMOTION_STATES:
            raise WorktreeContractError("PROMOTION_INVALID", "Unknown evidence promotion state")
        with self._transaction() as connection:
            record = self._record_for_update(connection, task_id)
            packet = record.get("handoff")
            if not packet or packet["overall"] != "PASS":
                raise WorktreeContractError("PROMOTION_NOT_READY", "Only a complete PASS handoff can be promoted")
            if exact_output_digest != packet["output_digest"]:
                raise WorktreeContractError("OUTPUT_CHANGED", "Promotion is not bound to the exact reviewed output")
            current = PROMOTION_STATES.index(record["promotion_state"])
            requested = PROMOTION_STATES.index(state)
            if requested > current + 1:
                raise WorktreeContractError("PROMOTION_ORDER_INVALID", "Evidence states cannot be skipped")
            record["promotion_state"] = state
            self._save_record(connection, task_id, record)
            self._event(connection, task_id, "task.promoted", {"state": state, "output_digest": exact_output_digest})
            return deepcopy(record)

    def invalidate_review(self, task_id: str, output: Mapping[str, Any]) -> dict[str, Any]:
        normalized = _normalize_provenance(output, require_clean=False)
        with self._transaction() as connection:
            record = self._record_for_update(connection, task_id)
            packet = record.get("handoff")
            if packet and normalized != packet["output"]:
                record["promotion_state"] = "candidate"
                record["status"] = "unverified"
                packet["overall"] = "UNVERIFIED"
                packet["output"] = normalized
                packet["output_digest"] = _digest(normalized)
                packet["handoff_digest"] = _digest({key: value for key, value in packet.items() if key != "handoff_digest"})
                self._save_record(connection, task_id, record)
                self._event(connection, task_id, "review.invalidated", {"output": normalized})
            return deepcopy(record)

    def release(self, task_id: str, *, clean_boundary: bool) -> dict[str, Any]:
        if not clean_boundary:
            raise WorktreeContractError("DIRTY_HANDOFF", "Ownership cannot be released at a dirty boundary")
        with self._transaction() as connection:
            record = self._record_for_update(connection, task_id)
            connection.execute("DELETE FROM worktree_claims WHERE task_id = ?", (task_id,))
            record["ownership_released"] = True
            self._save_record(connection, task_id, record)
            self._event(connection, task_id, "ownership.released", {"clean_boundary": True})
            return deepcopy(record)

    def archive(self, task_id: str) -> dict[str, Any]:
        with self._transaction() as connection:
            record = self._record_for_update(connection, task_id)
            if record["status"] not in {"pass", "fail", "cancelled", "unverified"}:
                raise WorktreeContractError("TASK_NOT_TERMINAL", "Only a terminal task can be archived")
            connection.execute("DELETE FROM worktree_claims WHERE task_id = ?", (task_id,))
            record["status"] = "archived"
            record["ownership_released"] = True
            self._save_record(connection, task_id, record)
            self._event(connection, task_id, "task.archived", {})
            return deepcopy(record)

    def wake(self, task_id: str) -> dict[str, Any]:
        record = self.get(task_id)
        return {
            "task_id": task_id,
            "message": "Periodic wake; re-check ledger state",
            "status": record["status"],
            "revision": record["revision"],
            "resume_ref": f"worktree-ledger:{task_id}:{record['revision']}",
        }

    def events(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_type, payload_json, created_at FROM worktree_events "
                "WHERE task_id = ? ORDER BY event_id", (task_id,)
            ).fetchall()
        return [
            {"event_type": row[0], "payload": json.loads(row[1]), "created_at": row[2]}
            for row in rows
        ]

    def _normalize_handoff(
        self,
        manifest: Mapping[str, Any],
        record: Mapping[str, Any],
        handoff: Mapping[str, Any],
    ) -> dict[str, Any]:
        output = _normalize_provenance(handoff.get("output"), require_clean=False)
        changed_files = _paths(handoff.get("changed_files") or [])
        changed_fields = _unique_strings(handoff.get("changed_fields") or [])
        if not _changes_within_ownership(changed_files, manifest["ownership"]):
            raise WorktreeContractError("OWNERSHIP_EXPANDED", "Changed files exceed the admitted ownership envelope")
        if not set(changed_fields).issubset(set(manifest["ownership"]["semantic_fields"])):
            raise WorktreeContractError("OWNERSHIP_EXPANDED", "Changed semantic fields exceed admitted ownership")
        commands = []
        for item in _sequence(handoff.get("commands") or [], "commands"):
            command = _mapping(item, "command")
            argv = _ordered_strings(command.get("argv") or [], "command argv")
            exit_code = command.get("exit_code")
            if not argv or isinstance(exit_code, bool) or not isinstance(exit_code, int):
                raise WorktreeContractError("COMMAND_EVIDENCE_INVALID", "Commands require argv and integer exit_code")
            commands.append({"argv": argv, "exit_code": exit_code})
        evidence = []
        for item in _sequence(handoff.get("evidence") or [], "evidence"):
            row = _mapping(item, "evidence row")
            digest = str(row.get("sha256") or "").lower()
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise WorktreeContractError("EVIDENCE_DIGEST_INVALID", "Evidence requires an exact SHA-256")
            kind = _optional_identifier(row.get("kind"), "evidence.kind")
            if not kind:
                raise WorktreeContractError("EVIDENCE_KIND_REQUIRED", "Evidence requires a bounded kind")
            evidence.append({
                "kind": kind,
                "sha256": digest,
                "location": _safe_reference(row.get("location")),
            })

        gates = deepcopy(record.get("gates") or {})
        for gate in manifest["required_gates"]:
            gates.setdefault(gate, {"outcome": "UNVERIFIED", "evidence_refs": []})
        first_blocker = record.get("first_blocker")
        requested = str(handoff.get("overall") or "UNVERIFIED").upper()
        all_pass = all(gates[gate]["outcome"] == "PASS" for gate in manifest["required_gates"])
        complete_evidence = set(manifest["required_evidence"]).issubset({item["kind"] for item in evidence})
        if requested == "FAIL" and not first_blocker:
            raise WorktreeContractError("BLOCKER_CLASS_REQUIRED", "FAIL handoffs require the first typed blocker")
        overall = "PASS" if requested == "PASS" and all_pass and output["clean"] and not first_blocker and complete_evidence else requested
        if overall == "PASS" and (not all_pass or not output["clean"] or first_blocker or not complete_evidence):
            overall = "UNVERIFIED"
        if overall not in GATE_RESULTS:
            raise WorktreeContractError("HANDOFF_RESULT_INVALID", "Handoff result must be PASS, FAIL, or UNVERIFIED")
        if not output["clean"] and overall == "PASS":
            overall = "UNVERIFIED"
        packet = {
            "handoff_version": HANDOFF_VERSION,
            "task_id": manifest["task_id"],
            "input": manifest["starting"],
            "output": output,
            "output_digest": _digest(output),
            "changed_files": changed_files,
            "changed_fields": changed_fields,
            "commands": commands,
            "evidence": evidence,
            "first_blocker": first_blocker,
            "attempts": self._attempt_summary(manifest["task_id"], manifest["attempt_budgets"]),
            "gate_matrix": {gate: gates[gate] for gate in manifest["required_gates"]},
            "overall": overall,
            "promotion_state": "candidate",
        }
        packet["handoff_digest"] = _digest(packet)
        return packet

    def _attempt_summary(self, task_id: str, budgets: Mapping[str, int]) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT operation, COUNT(*) FROM worktree_attempts WHERE task_id = ? GROUP BY operation",
                (task_id,),
            ).fetchall()
        consumed = {row[0]: int(row[1]) for row in rows}
        return {
            operation: {
                "consumed": consumed.get(operation, 0),
                "remaining": int(budget) - consumed.get(operation, 0),
            }
            for operation, budget in budgets.items()
        }

    def _require_predecessors(self, connection: sqlite3.Connection, manifest: Mapping[str, Any]) -> None:
        for predecessor in manifest["required_predecessor_pass"]:
            row = connection.execute(
                "SELECT record_json FROM worktree_tasks WHERE task_id = ?", (predecessor,)
            ).fetchone()
            if row is None or (json.loads(row[0]).get("handoff") or {}).get("overall") != "PASS":
                raise WorktreeContractError(
                    "PREDECESSOR_NOT_PASSED", f"Required predecessor {predecessor} has no exact PASS handoff"
                )

    def _ownership_conflicts(
        self, connection: sqlite3.Connection, ownership: Mapping[str, Any]
    ) -> list[dict[str, str]]:
        conflicts: list[dict[str, str]] = []
        rows = connection.execute("SELECT task_id, claim_kind, claim_value FROM worktree_claims").fetchall()
        incoming = [("file", value) for value in ownership["files"]]
        incoming += [("field", value) for value in ownership["semantic_fields"]]
        incoming += [("glob", value) for value in ownership["collision_globs"]]
        for owner_task, existing_kind, existing_value in rows:
            for incoming_kind, incoming_value in incoming:
                if _claims_overlap(existing_kind, existing_value, incoming_kind, incoming_value):
                    conflicts.append({
                        "owner_task_id": owner_task,
                        "existing": f"{existing_kind}:{existing_value}",
                        "incoming": f"{incoming_kind}:{incoming_value}",
                    })
        return sorted(conflicts, key=lambda item: tuple(item.values()))

    def _insert_claims(self, connection: sqlite3.Connection, manifest: Mapping[str, Any]) -> None:
        task_id = manifest["task_id"]
        ownership = manifest["ownership"]
        rows = [(task_id, "file", value) for value in ownership["files"]]
        rows += [(task_id, "field", value) for value in ownership["semantic_fields"]]
        rows += [(task_id, "glob", value) for value in ownership["collision_globs"]]
        connection.executemany(
            "INSERT INTO worktree_claims(task_id, claim_kind, claim_value) VALUES (?, ?, ?)", rows
        )

    def _record_for_update(self, connection: sqlite3.Connection, task_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT record_json FROM worktree_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise WorktreeContractError("TASK_NOT_FOUND", "Worktree task does not exist")
        return json.loads(row[0])

    def _save_record(self, connection: sqlite3.Connection, task_id: str, record: dict[str, Any]) -> None:
        record["revision"] = int(record.get("revision", 0)) + 1
        record["updated_at"] = time.time()
        connection.execute(
            "UPDATE worktree_tasks SET status = ?, record_json = ?, revision = ?, updated_at = ? WHERE task_id = ?",
            (record["status"], _json(record), record["revision"], record["updated_at"], task_id),
        )

    def _event(
        self, connection: sqlite3.Connection, task_id: str, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        connection.execute(
            "INSERT INTO worktree_events(task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (task_id, event_type, _json(payload), time.time()),
        )

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS worktree_tasks(
                    task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS worktree_claims(
                    task_id TEXT NOT NULL REFERENCES worktree_tasks(task_id) ON DELETE CASCADE,
                    claim_kind TEXT NOT NULL,
                    claim_value TEXT NOT NULL,
                    PRIMARY KEY(task_id, claim_kind, claim_value)
                );
                CREATE INDEX IF NOT EXISTS worktree_claim_lookup
                    ON worktree_claims(claim_kind, claim_value);
                CREATE TABLE IF NOT EXISTS worktree_attempts(
                    task_id TEXT NOT NULL REFERENCES worktree_tasks(task_id) ON DELETE CASCADE,
                    operation TEXT NOT NULL,
                    operation_identity TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    PRIMARY KEY(task_id, operation_identity)
                );
                CREATE TABLE IF NOT EXISTS worktree_events(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES worktree_tasks(task_id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _transaction(self):
        return _LedgerTransaction(self)


class _LedgerTransaction:
    def __init__(self, ledger: WorktreeTeamLedger) -> None:
        self.ledger = ledger
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        self.ledger._lock.acquire()
        self.connection = self.ledger._connect()
        self.connection.execute("BEGIN IMMEDIATE")
        return self.connection

    def __exit__(self, exc_type, _exc, _tb) -> None:
        assert self.connection is not None
        try:
            self.connection.rollback() if exc_type else self.connection.commit()
        finally:
            self.connection.close()
            self.ledger._lock.release()


def _normalize_provenance(value: Any, *, require_clean: bool) -> dict[str, Any]:
    data = _mapping(value, "provenance")
    commit_sha = str(data.get("commit_sha") or "").lower()
    tree_sha = str(data.get("tree_sha") or "").lower()
    parents = [str(item).lower() for item in _sequence(data.get("ordered_parents") or [], "ordered_parents")]
    if not GIT_OBJECT_ID.fullmatch(commit_sha) or not GIT_OBJECT_ID.fullmatch(tree_sha):
        raise WorktreeContractError("PROVENANCE_INVALID", "Exact commit and tree object IDs are required")
    if any(not GIT_OBJECT_ID.fullmatch(item) for item in parents):
        raise WorktreeContractError("PROVENANCE_INVALID", "Every ordered parent must be an exact object ID")
    clean = data.get("clean")
    if not isinstance(clean, bool):
        raise WorktreeContractError("CLEAN_STATE_REQUIRED", "Provenance requires an explicit clean boolean")
    if require_clean and not clean:
        raise WorktreeContractError("DIRTY_START", "A worktree task must start from a clean state")
    return {"commit_sha": commit_sha, "tree_sha": tree_sha, "ordered_parents": parents, "clean": clean}


def _require_active(record: Mapping[str, Any]) -> None:
    if record.get("status") not in {"admitted", "running"}:
        raise WorktreeContractError(
            "TASK_NOT_ACTIVE",
            "Held, terminal, or archived tasks cannot consume attempts or submit evidence",
        )


def _normalize_ownership(value: Any) -> dict[str, list[str]]:
    data = _mapping(value, "ownership")
    ownership = {
        "files": _paths(data.get("files") or []),
        "semantic_fields": _unique_strings(data.get("semantic_fields") or []),
        "collision_globs": _paths(data.get("collision_globs") or [], allow_glob=True),
    }
    if not any(ownership.values()):
        raise WorktreeContractError("OWNERSHIP_REQUIRED", "At least one exclusive ownership claim is required")
    return ownership


def _changes_within_ownership(changed: Sequence[str], ownership: Mapping[str, Any]) -> bool:
    exact = set(ownership["files"])
    globs = ownership["collision_globs"]
    return all(path in exact or any(fnmatch.fnmatchcase(path, pattern) for pattern in globs) for path in changed)


def _claims_overlap(kind_a: str, value_a: str, kind_b: str, value_b: str) -> bool:
    if kind_a == kind_b == "field":
        return value_a == value_b
    if "field" in {kind_a, kind_b}:
        return False
    if kind_a == kind_b == "file":
        return value_a == value_b
    if kind_a == "glob" and kind_b == "file":
        return fnmatch.fnmatchcase(value_b, value_a)
    if kind_a == "file" and kind_b == "glob":
        return fnmatch.fnmatchcase(value_a, value_b)
    if value_a == value_b:
        return True
    prefix_a = re.split(r"[*?[]", value_a, maxsplit=1)[0]
    prefix_b = re.split(r"[*?[]", value_b, maxsplit=1)[0]
    return bool(prefix_a and prefix_b and (prefix_a.startswith(prefix_b) or prefix_b.startswith(prefix_a)))


def _paths(value: Any, *, allow_glob: bool = False) -> list[str]:
    paths = []
    for raw in _sequence(value, "paths"):
        path = str(raw).replace("\\", "/").strip()
        pure = PurePosixPath(path)
        if (
            not path
            or pure.is_absolute()
            or ".." in pure.parts
            or path.startswith("~")
            or (not allow_glob and any(marker in path for marker in "*?[]"))
        ):
            raise WorktreeContractError("PATH_INVALID", f"Path must be a bounded workspace-relative path: {path}")
        paths.append(path)
    return sorted(set(paths))


def _required_identifier(value: Mapping[str, Any], key: str) -> str:
    result = _optional_identifier(value.get(key), key)
    if not result:
        raise WorktreeContractError("IDENTIFIER_REQUIRED", f"{key} is required")
    return result


def _optional_identifier(value: Any, label: str) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    result = str(value).strip()
    if not IDENTIFIER.fullmatch(result):
        raise WorktreeContractError("IDENTIFIER_INVALID", f"{label} is invalid")
    return result


def _identifiers(value: Any, label: str) -> list[str]:
    result: set[str] = set()
    for item in _sequence(value or [], label):
        normalized = _optional_identifier(item, label)
        if normalized:
            result.add(normalized)
    return sorted(result)


def _unique_strings(value: Any) -> list[str]:
    result = {str(item).strip() for item in _sequence(value, "string list") if str(item).strip()}
    if any(len(item) > 512 for item in result):
        raise WorktreeContractError("VALUE_TOO_LONG", "Contract strings are bounded to 512 characters")
    return sorted(result)


def _ordered_strings(value: Any, label: str) -> list[str]:
    result = [str(item) for item in _sequence(value, label)]
    if any(not item or len(item) > 4096 for item in result):
        raise WorktreeContractError("VALUE_INVALID", f"{label} contains an empty or oversized value")
    return result


def _ordered_unique_strings(value: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in _ordered_strings(value, "ordered string list"):
        normalized = item.strip()
        if normalized and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result


def _bounded_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 2**63 - 1:
        raise WorktreeContractError("ESTIMATE_INVALID", f"{label} must be a bounded non-negative integer")
    return value


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorktreeContractError("MAPPING_REQUIRED", f"{label} must be an object")
    return dict(value)


def _sequence(value: Any, label: str) -> list[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise WorktreeContractError("LIST_REQUIRED", f"{label} must be a list")
    if len(value) > 1024:
        raise WorktreeContractError("LIST_TOO_LARGE", f"{label} exceeds the bounded list size")
    return list(value)


def _safe_reference(value: Any) -> str:
    reference = str(value or "").strip()
    if not reference or len(reference) > 1024 or SENSITIVE_KEYS.search(reference):
        raise WorktreeContractError("EVIDENCE_REFERENCE_INVALID", "Evidence location is missing or sensitive")
    return reference


def _reject_sensitive_values(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if SENSITIVE_KEYS.search(key_text):
                raise WorktreeContractError(
                    "SENSITIVE_INPUT_FORBIDDEN",
                    "Credentials, environment payloads, and secrets cannot enter a worktree contract",
                    details={"field": ".".join((*path, key_text))},
                )
            _reject_sensitive_values(child, (*path, key_text))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            _reject_sensitive_values(child, (*path, str(index)))


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "BLOCKER_CLASSES",
    "CONTRACT_VERSION",
    "GATE_RESULTS",
    "HANDOFF_VERSION",
    "PROMOTION_STATES",
    "WORKTREE_TASK_PRESETS",
    "WorktreeContractError",
    "WorktreeTeamLedger",
    "normalize_task_request",
    "worktree_task_preset",
]
