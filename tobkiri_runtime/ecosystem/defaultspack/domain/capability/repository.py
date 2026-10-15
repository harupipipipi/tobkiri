"""Durable capability settings and process-safe Capability Plan storage."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
from typing import Any

from domain.capability.settings import normalize_capability_settings


_SECRET_KEY = re.compile(
    r"(?:authorization|cookie|password|passwd|secret|token|api[_-]?key|"
    r"private[_-]?key|credential|session)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:Bearer\s+\S+|Basic\s+\S+|"
    r"(?:sk|rk|ghp|github_pat|xox[baprs]|ya29)[-_][A-Za-z0-9._-]{8,}|"
    r"(?:authorization|cookie|password|passwd|secret|token|api[_-]?key|"
    r"private[_-]?key|credential|session(?:[_-]?id)?)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)
_OWNER_FIELDS = ("principal_id", "workspace_id", "conversation_id", "profile_id")


class CapabilityRepository:
    """Persist Plans with an inter-process atomic claim boundary."""

    _lock = threading.RLock()

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or (
            Path(__file__).resolve().parents[2] / "user_data" / "shared" / "capabilities"
        )
        self._settings_path = self._root / "settings.json"
        self._database_path = self._root / "capabilities.sqlite3"
        self._root.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    def settings(self) -> dict[str, Any]:
        with self._lock:
            return normalize_capability_settings(self._read(self._settings_path, {}))

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ValueError("settings patch must be an object")
        with self._lock:
            current = self.settings()
            incoming = (
                patch.get("capabilities")
                if isinstance(patch.get("capabilities"), dict)
                else patch
            )
            _merge_known(current["capabilities"], incoming)
            normalized = normalize_capability_settings(current)
            self._write(self._settings_path, normalized)
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO capability_metadata(key, value)
                    VALUES ('policy_generation', '1')
                    ON CONFLICT(key) DO UPDATE SET
                        value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
                    """
                )
                connection.commit()
            return deepcopy(normalized)

    def policy_generation(self) -> int:
        """Return a monotonic generation that does not revive on A→B→A."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM capability_metadata WHERE key = 'policy_generation'"
            ).fetchone()
        return int(row["value"]) if row is not None else 0

    def put_plan(
        self,
        plan: dict[str, Any],
        *,
        owner: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        safe_plan = _redact_for_storage(plan)
        plan_id = str(safe_plan.get("plan_id") or "").strip()
        trace_id = str(safe_plan.get("trace_id") or "").strip()
        if not plan_id or not trace_id:
            raise ValueError("plan_id and trace_id are required")
        scope = _normalize_owner(owner)
        plan_digest = _digest(safe_plan)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO capability_plans (
                    plan_id, trace_id, principal_id, workspace_id,
                    conversation_id, profile_id, plan_json, plan_digest,
                    state, generation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'resolved', 1)
                ON CONFLICT(plan_id) DO UPDATE SET
                    trace_id=excluded.trace_id,
                    principal_id=excluded.principal_id,
                    workspace_id=excluded.workspace_id,
                    conversation_id=excluded.conversation_id,
                    profile_id=excluded.profile_id,
                    plan_json=excluded.plan_json,
                    plan_digest=excluded.plan_digest,
                    approval_json=NULL,
                    execution_json=NULL,
                    state='resolved',
                    generation=capability_plans.generation + 1
                """,
                (
                    plan_id,
                    trace_id,
                    *[scope[field] for field in _OWNER_FIELDS],
                    _dump(safe_plan),
                    plan_digest,
                ),
            )
            connection.commit()
        return self.get_plan(plan_id, owner=scope, require_owner=False) or {}

    def get_plan(
        self,
        plan_id: str,
        *,
        owner: dict[str, Any] | None = None,
        require_owner: bool = False,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM capability_plans WHERE plan_id = ?",
                (str(plan_id),),
            ).fetchone()
        return self._record(row, owner=owner, require_owner=require_owner)

    def get_trace(
        self,
        trace_id: str,
        *,
        owner: dict[str, Any] | None = None,
        require_owner: bool = False,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM capability_plans WHERE trace_id = ?",
                (str(trace_id),),
            ).fetchone()
        return self._record(row, owner=owner, require_owner=require_owner)

    def approve_plan(
        self,
        plan_id: str,
        *,
        registry_revision: str,
        policy_revision: str,
        approved_effects: list[dict[str, Any]],
        principal_id: str,
        owner: dict[str, Any] | None = None,
        invocation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scope = _normalize_owner(owner, principal_id=principal_id)
        safe_invocation = _canonical_invocation(invocation)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM capability_plans WHERE plan_id = ?",
                (str(plan_id),),
            ).fetchone()
            record = self._record(row, owner=scope, require_owner=owner is not None)
            if record is None:
                connection.rollback()
                raise KeyError(plan_id)
            plan = record["plan"]
            if str(plan.get("registry_revision") or "") != registry_revision:
                connection.rollback()
                raise StaleCapabilityPlan("registry revision changed")
            if str(plan.get("policy_revision") or "") != policy_revision:
                connection.rollback()
                raise StaleCapabilityPlan("policy revision changed")
            approval = {
                "registry_revision": registry_revision,
                "policy_revision": policy_revision,
                "plan_digest": record["plan_digest"],
                "approved_effects": _redact_for_storage(approved_effects),
                "principal_id": scope["principal_id"],
                "owner": scope,
                "invocation": safe_invocation,
                "invocation_digest": _digest(safe_invocation),
                "generation": int(record["generation"]),
            }
            changed = connection.execute(
                """
                UPDATE capability_plans
                SET approval_json = ?, execution_json = NULL, state = 'approved'
                WHERE plan_id = ? AND generation = ? AND state IN ('resolved', 'approved')
                """,
                (_dump(approval), str(plan_id), record["generation"]),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise StaleCapabilityPlan("Capability Plan changed during approval")
            connection.commit()
        return self.get_plan(plan_id, owner=scope, require_owner=owner is not None) or {}

    def claim_execution(
        self,
        plan_id: str,
        execution: dict[str, Any],
        *,
        owner: dict[str, Any] | None = None,
        invocation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Consume an approved Plan before any external effect begins."""

        scope = _normalize_owner(owner)
        safe_invocation = _canonical_invocation(invocation)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM capability_plans WHERE plan_id = ?",
                (str(plan_id),),
            ).fetchone()
            record = self._record(row, owner=scope, require_owner=owner is not None)
            if record is None:
                connection.rollback()
                raise KeyError(plan_id)
            approval = record.get("approval")
            if not isinstance(approval, dict):
                connection.rollback()
                raise PermissionError("Capability Plan is not approved")
            if approval.get("plan_digest") != record.get("plan_digest"):
                connection.rollback()
                raise StaleCapabilityPlan("Capability Plan digest changed")
            if approval.get("generation") != record.get("generation"):
                connection.rollback()
                raise StaleCapabilityPlan("Capability Plan generation changed")
            if approval.get("invocation_digest") != _digest(safe_invocation):
                connection.rollback()
                raise StaleCapabilityPlan("approved invocation changed")
            claimed = {
                **_redact_for_storage(execution),
                "status": "started",
                "invocation_digest": approval["invocation_digest"],
                "effect_journal": [
                    {
                        "effect_id": _digest(effect),
                        "tool_id": str(effect.get("tool_id") or ""),
                        "class": str(effect.get("class") or ""),
                        "status": "started",
                    }
                    for effect in approval.get("approved_effects", [])
                    if isinstance(effect, dict)
                ],
            }
            changed = connection.execute(
                """
                UPDATE capability_plans
                SET execution_json = ?, state = 'executing'
                WHERE plan_id = ? AND generation = ? AND state = 'approved'
                """,
                (_dump(claimed), str(plan_id), record["generation"]),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise CapabilityPlanAlreadyExecuted(plan_id)
            connection.commit()
        return self.get_plan(plan_id, owner=scope, require_owner=owner is not None) or {}

    def complete_execution(
        self,
        plan_id: str,
        execution: dict[str, Any],
        *,
        owner: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Finalize a claimed execution without reopening replay."""

        scope = _normalize_owner(owner)
        safe_execution = _redact_for_storage(execution)
        status = str(safe_execution.get("status") or "outcome_unknown")
        if status not in {
            "failed_pre_effect",
            "outcome_unknown",
            "partially_succeeded",
            "succeeded",
        }:
            raise ValueError("invalid terminal capability execution status")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM capability_plans WHERE plan_id = ?",
                (str(plan_id),),
            ).fetchone()
            record = self._record(row, owner=scope, require_owner=owner is not None)
            if record is None:
                connection.rollback()
                raise KeyError(plan_id)
            if record.get("state") != "executing":
                connection.rollback()
                raise RuntimeError("Capability Plan execution was not claimed")
            previous_execution = record.get("execution")
            journal = (
                previous_execution.get("effect_journal", [])
                if isinstance(previous_execution, dict)
                and isinstance(previous_execution.get("effect_journal"), list)
                else []
            )
            terminal_effect_status = (
                "succeeded"
                if status == "succeeded"
                else (
                    "failed_pre_effect"
                    if status == "failed_pre_effect"
                    else "outcome_unknown"
                )
            )
            safe_execution["effect_journal"] = [
                {
                    **item,
                    "status": terminal_effect_status,
                }
                for item in journal
                if isinstance(item, dict)
            ]
            connection.execute(
                "UPDATE capability_plans SET execution_json = ?, state = ? WHERE plan_id = ?",
                (_dump(safe_execution), status, str(plan_id)),
            )
            connection.commit()
        return self.get_plan(plan_id, owner=scope, require_owner=owner is not None) or {}

    def mark_executed(self, plan_id: str, execution: dict[str, Any]) -> dict[str, Any]:
        """Backward-compatible alias for the atomic execution claim."""

        return self.claim_execution(plan_id, execution)

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS capability_plans (
                    plan_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    principal_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    plan_digest TEXT NOT NULL,
                    approval_json TEXT,
                    execution_json TEXT,
                    state TEXT NOT NULL,
                    generation INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS capability_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @staticmethod
    def _record(
        row: sqlite3.Row | None,
        *,
        owner: dict[str, Any] | None,
        require_owner: bool,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        stored_owner = {field: str(row[field] or "") for field in _OWNER_FIELDS}
        if require_owner and stored_owner != _normalize_owner(owner):
            raise CapabilityOwnerMismatch("Capability Plan belongs to another scope")
        return {
            "plan": json.loads(row["plan_json"]),
            "approval": (
                json.loads(row["approval_json"]) if row["approval_json"] else None
            ),
            "execution": (
                json.loads(row["execution_json"]) if row["execution_json"] else None
            ),
            "state": str(row["state"]),
            "generation": int(row["generation"]),
            "plan_digest": str(row["plan_digest"]),
            "owner": stored_owner,
        }

    @staticmethod
    def _read(path: Path, default: Any) -> Any:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return deepcopy(default)
        return value

    @staticmethod
    def _write(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


class StaleCapabilityPlan(RuntimeError):
    """Raised when approval revisions no longer bind to a compiled plan."""


class CapabilityPlanAlreadyExecuted(RuntimeError):
    """Raised when an execution grant would be reused."""


class CapabilityOwnerMismatch(PermissionError):
    """Raised when a Plan or Trace is accessed from another authority scope."""


def _merge_known(target: dict[str, Any], patch: Any) -> None:
    if not isinstance(patch, dict):
        raise ValueError("capabilities patch must be an object")
    unknown = sorted(set(patch) - set(target))
    if unknown:
        raise ValueError(f"unknown capability setting(s): {', '.join(unknown)}")
    for key, value in patch.items():
        if isinstance(target[key], dict):
            if not isinstance(value, dict):
                raise ValueError(f"{key} must be an object")
            _merge_known(target[key], value)
        else:
            target[key] = deepcopy(value)


def _normalize_owner(
    value: dict[str, Any] | None,
    *,
    principal_id: str = "",
) -> dict[str, str]:
    source = value if isinstance(value, dict) else {}
    return {
        "principal_id": str(
            source.get("principal_id")
            or source.get("user_id")
            or principal_id
            or "local-user"
        ),
        "workspace_id": str(source.get("workspace_id") or "local-workspace"),
        "conversation_id": str(
            source.get("conversation_id") or "local-conversation"
        ),
        "profile_id": str(source.get("profile_id") or "default"),
    }


def _canonical_invocation(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return json.loads(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    )


def _digest(value: Any) -> str:
    return sha256(_dump(value).encode("utf-8")).hexdigest()


def _dump(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _redact_for_storage(value: Any, *, key: str = "") -> Any:
    if _SECRET_KEY.search(str(key)):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _redact_for_storage(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_for_storage(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [_redact_for_storage(item, key=key) for item in value]
    if isinstance(value, str):
        redacted = _SECRET_VALUE.sub("[REDACTED]", value)
        if len(redacted) > 4_000:
            return {
                "sha256": sha256(redacted.encode("utf-8")).hexdigest(),
                "length": len(redacted),
                "redacted": True,
            }
        return redacted
    return value
