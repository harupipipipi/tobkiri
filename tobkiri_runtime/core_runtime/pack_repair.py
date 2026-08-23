"""Fail-closed conflict reports and explicit generated repair Pack lifecycle.

AI generation is a replaceable callback.  It receives only the bounded plan and
can never approve, install, activate, or mutate either source Pack.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from copy import deepcopy
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from .pack_sdk import PackSdkError, refresh_scaffold_artifacts, scaffold_pack
from tobkiri_protocol.validation import validate_document


CONFLICT_API_VERSION = "io.tobkiri.pack-conflict-report.v1"
PLAN_API_VERSION = "io.tobkiri.repair-pack-plan.v1"
REPAIR_API_VERSION = "io.tobkiri.generated-repair-pack.v1"
PACK_REPAIR_OPERATIONS = (
    "pack.conflicts.list",
    "pack.conflicts.get",
    "pack.repair.plan",
    "pack.repair.generate",
    "pack.repair.validate",
    "pack.repair.approve",
    "pack.repair.install",
    "pack.repair.activate",
    "pack.repair.remove",
    "pack.repair.status",
)

REPAIR_KINDS: dict[str, tuple[str, ...]] = {
    "ambiguous_one_provider": ("provider_selection",),
    "compatible_schema_version": ("schema_adapter",),
    "contract_alias_required": ("contract_alias",),
    "resource_id_collision": ("namespace_mapping",),
    "profile_route_ambiguity": ("profile_routing",),
    "compatible_version_constraint": ("constraint_refinement",),
    "policy_chain_supported": ("policy_chain",),
    "incompatible_semantic": (),
    "authority_conflict": (),
}

SENSITIVE_KEY = re.compile(
    r"(?:secret|token|password|credential|api[_-]?key|private[_-]?key|environment|hidden[_-]?prompt)",
    re.IGNORECASE,
)
SENSITIVE_VALUE = re.compile(
    r"(?:\bBearer\s+[A-Za-z0-9._~-]{16,}|\bsk-[A-Za-z0-9_-]{20,}|\bgh[oprsu]_[A-Za-z0-9]{20,})",
    re.IGNORECASE,
)
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")


class PackRepairError(ValueError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def build_pack_conflict_report(
    *,
    kind: str,
    profile_id: str,
    profile_fingerprint: str,
    involved_packs: Sequence[Mapping[str, Any]],
    affected_contracts: Sequence[str] = (),
    affected_resources: Sequence[str] = (),
    schemas: Sequence[Mapping[str, Any]] = (),
    constraints: Sequence[str] = (),
    diagnostics: Sequence[str] = (),
    validation_requirements: Sequence[str] = (),
) -> dict[str, Any]:
    """Create a deterministic, source-path-free conflict report."""

    kind = str(kind).strip()
    if kind not in REPAIR_KINDS:
        raise PackRepairError("CONFLICT_KIND_INVALID", "Unknown Pack conflict kind")
    if not ID.fullmatch(str(profile_id)) or not SHA256.fullmatch(str(profile_fingerprint)):
        raise PackRepairError("PROFILE_IDENTITY_INVALID", "Exact profile identity and fingerprint are required")
    packs = []
    for value in involved_packs:
        pack = dict(value)
        _reject_sensitive(pack)
        pack_id = str(pack.get("pack_id") or "")
        version = str(pack.get("version") or "")
        artifact_hash = str(pack.get("artifact_hash") or "")
        if not ID.fullmatch(pack_id) or not version or not SHA256.fullmatch(artifact_hash):
            raise PackRepairError("PACK_IDENTITY_INVALID", "Every conflict Pack requires ID, version, and artifact hash")
        row = {"pack_id": pack_id, "version": version, "artifact_hash": artifact_hash}
        if pack.get("provider_instance_id"):
            provider_id = str(pack["provider_instance_id"])
            if not ID.fullmatch(provider_id):
                raise PackRepairError("PROVIDER_ID_INVALID", "Provider instance identity is invalid")
            row["provider_instance_id"] = provider_id
        packs.append(row)
    packs.sort(key=lambda item: (item["pack_id"], item["artifact_hash"]))
    if len(packs) < 2:
        raise PackRepairError("CONFLICT_PARTICIPANTS_REQUIRED", "A conflict requires at least two exact Pack artifacts")
    identity = {
        "kind": kind,
        "profile_id": str(profile_id),
        "profile_fingerprint": str(profile_fingerprint),
        "involved_packs": packs,
        "affected_contracts": _strings(affected_contracts),
        "affected_resources": _strings(affected_resources),
        "schemas": [_safe_schema(item) for item in schemas],
        "constraints": _strings(constraints),
    }
    conflict_id = "pcf_" + hashlib.sha256(_json(identity).encode()).hexdigest()[:24]
    result = {
        "conflict_api_version": CONFLICT_API_VERSION,
        "conflict_id": conflict_id,
        **identity,
        "safe_repair_kinds": list(REPAIR_KINDS[kind]),
        "repairable": bool(REPAIR_KINDS[kind]),
        "diagnostics": _bounded_messages(diagnostics),
        "validation_requirements": _strings(
            validation_requirements
            or (
                "pack_v4_schema",
                "contract_schema_compatibility",
                "cross_pack_boundary",
                "capability_delta",
                "private_source_coupling",
                "artifact_integrity",
                "dry_run_resolution",
            )
        ),
    }
    _reject_sensitive(result)
    validate_document(result, "pack_conflict_report")
    return result


class PackRepairManager:
    """Persisted conflict -> plan -> generation -> review -> activation state."""

    def __init__(self, database: Path, generated_workspace: Path) -> None:
        self.database = Path(database)
        self.generated_workspace = Path(generated_workspace)
        self._lock = threading.RLock()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.generated_workspace.mkdir(parents=True, exist_ok=True)
        if self.generated_workspace.is_symlink():
            raise PackRepairError("WORKSPACE_UNSAFE", "Generated Pack workspace cannot be a symlink")
        self._initialize()

    def register_conflict(self, report: Mapping[str, Any]) -> dict[str, Any]:
        normalized = build_pack_conflict_report(
            kind=str(report.get("kind") or ""),
            profile_id=str(report.get("profile_id") or ""),
            profile_fingerprint=str(report.get("profile_fingerprint") or ""),
            involved_packs=_sequence(report.get("involved_packs"), "involved_packs"),
            affected_contracts=_sequence(report.get("affected_contracts") or [], "affected_contracts"),
            affected_resources=_sequence(report.get("affected_resources") or [], "affected_resources"),
            schemas=_sequence(report.get("schemas") or [], "schemas"),
            constraints=_sequence(report.get("constraints") or [], "constraints"),
            diagnostics=_sequence(report.get("diagnostics") or [], "diagnostics"),
            validation_requirements=_sequence(
                report.get("validation_requirements") or [], "validation_requirements"
            ),
        )
        with self._transaction() as connection:
            prior = connection.execute(
                "SELECT report_json FROM pack_conflicts WHERE conflict_id = ?", (normalized["conflict_id"],)
            ).fetchone()
            if prior and json.loads(prior[0]) != normalized:
                raise PackRepairError("CONFLICT_ID_COLLISION", "Conflict identity collision")
            connection.execute(
                "INSERT OR IGNORE INTO pack_conflicts(conflict_id, report_json, created_at) VALUES (?, ?, ?)",
                (normalized["conflict_id"], _json(normalized), time.time()),
            )
            self._audit(connection, normalized["conflict_id"], "conflict.detected", {
                "kind": normalized["kind"], "repairable": normalized["repairable"]
            })
        return deepcopy(normalized)

    def list_conflicts(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT report_json FROM pack_conflicts ORDER BY conflict_id").fetchall()
        return [json.loads(row[0]) for row in rows]

    def get_conflict(self, conflict_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT report_json FROM pack_conflicts WHERE conflict_id = ?", (conflict_id,)
            ).fetchone()
        if row is None:
            raise PackRepairError("CONFLICT_NOT_FOUND", "Pack conflict does not exist")
        return json.loads(row[0])

    def list_conflict_reviews(self) -> list[dict[str, Any]]:
        """Return Launcher-safe reports plus the latest inspectable repair state."""

        reports = self.list_conflicts()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT conflict_id, record_json FROM repair_packs ORDER BY updated_at, repair_id"
            ).fetchall()
        latest = {row[0]: json.loads(row[1]) for row in rows}
        result: list[dict[str, Any]] = []
        for report in reports:
            item = deepcopy(report)
            record = latest.get(report["conflict_id"])
            if record is not None:
                validation = record.get("validation") or {}
                dry_run = validation.get("dry_run") or {}
                approval = record.get("approval") or {}
                warnings: list[str] = []
                if validation and not validation.get("passed"):
                    warnings.append(str(dry_run.get("reason") or "Repair validation did not pass"))
                if record.get("stale"):
                    warnings.append("Source Pack binding changed; regeneration and review are required")
                item["repair"] = {
                    "repair_id": record["repair_id"],
                    "artifact_hash": record["artifact_hash"],
                    "state": record["state"],
                    "capability_delta": validation.get("capability_delta") or [],
                    "validation_passed": validation.get("passed") is True,
                    "dry_run_resolved": dry_run.get("resolved") is True,
                    "warnings": warnings,
                    "approval_actor_id": approval.get("actor_id"),
                }
            result.append(item)
        return result

    def plan(self, conflict_id: str, *, repair_kind: str, generation_run_id: str) -> dict[str, Any]:
        report = self.get_conflict(conflict_id)
        if repair_kind not in report["safe_repair_kinds"]:
            code = "MANUAL_RESOLUTION_REQUIRED" if not report["repairable"] else "REPAIR_KIND_FORBIDDEN"
            raise PackRepairError(code, "The conflict cannot be repaired by the requested bounded class")
        if not ID.fullmatch(str(generation_run_id)):
            raise PackRepairError("GENERATION_RUN_INVALID", "Generation requires a bounded run identity")
        plan_identity = {
            "conflict_id": conflict_id,
            "repair_kind": repair_kind,
            "generation_run_id": generation_run_id,
            "profile_fingerprint": report["profile_fingerprint"],
            "packs": report["involved_packs"],
        }
        plan = {
            "plan_api_version": PLAN_API_VERSION,
            "plan_id": "rpp_" + hashlib.sha256(_json(plan_identity).encode()).hexdigest()[:24],
            **plan_identity,
            "inputs": {
                "contracts": report["affected_contracts"],
                "resources": report["affected_resources"],
                "schemas": report["schemas"],
                "constraints": report["constraints"],
            },
            "expected_outputs": [repair_kind],
            "forbidden": {
                "source_pack_writes": True,
                "private_source_imports": True,
                "permission_expansion": True,
                "self_approval": True,
                "automatic_activation": True,
                "secrets": True,
            },
            "validation_requirements": report["validation_requirements"],
        }
        validate_document(plan, "repair_pack_plan", reject_authority_fields=False)
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO repair_plans(plan_id, conflict_id, plan_json, created_at) VALUES (?, ?, ?, ?)",
                (plan["plan_id"], conflict_id, _json(plan), time.time()),
            )
            self._audit(connection, conflict_id, "repair.planned", {
                "plan_id": plan["plan_id"], "repair_kind": repair_kind
            })
        return deepcopy(plan)

    def generation_input(self, plan_id: str) -> dict[str, Any]:
        """Return the only data that may be sent to an AI generator."""

        return self._get_plan(plan_id)

    def generate(
        self,
        plan_id: str,
        generator: Callable[[dict[str, Any]], Mapping[str, Any]],
    ) -> dict[str, Any]:
        plan = self._get_plan(plan_id)
        try:
            output = dict(generator(deepcopy(plan)))
            _reject_sensitive(output)
            repair = self._normalize_generator_output(plan, output)
        except Exception as error:
            with self._transaction() as connection:
                self._audit(connection, plan["conflict_id"], "repair.generation_failed", {
                    "plan_id": plan_id, "error_type": type(error).__name__
                })
            if isinstance(error, PackRepairError):
                raise
            raise PackRepairError("GENERATOR_UNAVAILABLE", "Repair generator failed or is unavailable") from error
        pack_id = repair["pack_id"]
        repair_id = "rpr_" + hashlib.sha256(
            _json({"plan_id": plan_id, "repair": repair}).encode()
        ).hexdigest()[:24]
        target = self.generated_workspace / f"{pack_id}--{repair_id[4:]}"
        if target.exists():
            raise PackRepairError("REPAIR_PACK_EXISTS", "Generated repair Pack already exists")
        try:
            with tempfile.TemporaryDirectory(prefix=".repair-stage-", dir=self.generated_workspace) as temp:
                stage_parent = Path(temp)
                manifest_path = scaffold_pack(
                    stage_parent / pack_id,
                    pack_id=pack_id,
                    display_name=repair["display_name"],
                    profile="minimal",
                    intent=f"Explicit generated repair for {plan['conflict_id']}",
                )
                root = manifest_path.parent
                source_path = root / "scaffold-source.v1.json"
                source = json.loads(source_path.read_text(encoding="utf-8"))
                source["generated"] = True
                source["generator"] = {"kind": "ai", "run_id": plan["generation_run_id"]}
                source["repairs_conflict_ids"] = [plan["conflict_id"]]
                source["generated_for"] = {
                    "profile_fingerprint": plan["profile_fingerprint"],
                    "packs": plan["packs"],
                }
                source_path.write_text(_pretty(source), encoding="utf-8")
                metadata = {
                    "repair_api_version": REPAIR_API_VERSION,
                    "repair_id": repair_id,
                    "plan_id": plan_id,
                    "conflict_id": plan["conflict_id"],
                    "repair_kind": plan["repair_kind"],
                    "generated": True,
                    "generator": {"kind": "ai", "run_id": plan["generation_run_id"]},
                    "generated_for": {
                        "profile_fingerprint": plan["profile_fingerprint"],
                        "packs": plan["packs"],
                    },
                    "repair": repair["repair"],
                    "permissions": [],
                }
                (root / "repair.v1.json").write_text(_pretty(metadata), encoding="utf-8")
                for relative, document in repair["resources"].items():
                    path = _scoped_resource(root, relative)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(_pretty(document), encoding="utf-8")
                refresh_scaffold_artifacts(root)
                self._static_validate(root, plan, metadata)
                artifact_hash = _directory_hash(root)
                os.replace(root, target)
        except Exception as error:
            with self._transaction() as connection:
                self._audit(connection, plan["conflict_id"], "repair.generation_failed", {
                    "plan_id": plan_id, "error_type": type(error).__name__
                })
            if isinstance(error, PackRepairError):
                raise
            if isinstance(error, (PackSdkError, OSError, ValueError)):
                raise PackRepairError(
                    "GENERATED_PACK_INVALID", "Generated output did not form a valid Pack"
                ) from error
            raise
        record = {
            "repair_id": metadata["repair_id"],
            "plan_id": plan_id,
            "conflict_id": plan["conflict_id"],
            "pack_id": pack_id,
            "pack_root": str(target),
            "artifact_hash": artifact_hash,
            "state": "generated",
            "validation": None,
            "approval": None,
            "installed": False,
            "active": False,
            "stale": False,
        }
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO repair_packs(repair_id, conflict_id, record_json, updated_at) VALUES (?, ?, ?, ?)",
                (record["repair_id"], record["conflict_id"], _json(record), time.time()),
            )
            self._audit(connection, record["conflict_id"], "repair.generated", {
                "repair_id": record["repair_id"], "artifact_hash": artifact_hash
            })
        return deepcopy(record)

    def validate(self, repair_id: str, current_packs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        with self._transaction() as connection:
            record = self._repair_for_update(connection, repair_id)
            plan = self._plan_for_connection(connection, record["plan_id"])
            current = _pack_hashes(current_packs)
            expected = {item["pack_id"]: item["artifact_hash"] for item in plan["packs"]}
            changed = sorted(
                pack_id for pack_id, artifact_hash in expected.items()
                if current.get(pack_id) != artifact_hash
            )
            if changed:
                record["stale"] = True
                record["state"] = "stale"
                record["active"] = False
                record["installed"] = False
                record["approval"] = None
                self._save_repair(connection, record)
                self._audit(connection, record["conflict_id"], "repair.stale", {
                    "repair_id": repair_id, "changed_pack_ids": changed
                })
                return deepcopy(record)
            root = Path(record["pack_root"])
            current_hash = _directory_hash(root)
            if current_hash != record["artifact_hash"]:
                record["state"] = "modified"
                record["active"] = False
                record["installed"] = False
                record["approval"] = None
                self._save_repair(connection, record)
                self._audit(connection, record["conflict_id"], "repair.approval_invalidated", {
                    "repair_id": repair_id, "reason": "artifact_modified"
                })
                return deepcopy(record)
            metadata = json.loads((root / "repair.v1.json").read_text(encoding="utf-8"))
            self._static_validate(root, plan, metadata)
            dry_run = self._dry_run(plan, metadata)
            record["validation"] = {
                "passed": dry_run["resolved"],
                "artifact_hash": current_hash,
                "requirements": plan["validation_requirements"],
                "dry_run": dry_run,
                "capability_delta": [],
                "executed_entrypoints": False,
            }
            record["state"] = "validated" if dry_run["resolved"] else "blocked"
            self._save_repair(connection, record)
            self._audit(connection, record["conflict_id"], "repair.validated", {
                "repair_id": repair_id, "passed": dry_run["resolved"]
            })
            return deepcopy(record)

    def approve(self, repair_id: str, *, actor_id: str, artifact_hash: str) -> dict[str, Any]:
        if not ID.fullmatch(str(actor_id)):
            raise PackRepairError("ACTOR_INVALID", "Approval requires an exact actor identity")
        with self._transaction() as connection:
            record = self._repair_for_update(connection, repair_id)
            plan = self._plan_for_connection(connection, record["plan_id"])
            if actor_id == plan["generation_run_id"] or actor_id.startswith("ai"):
                raise PackRepairError("SELF_APPROVAL_FORBIDDEN", "The generator cannot approve its own Pack")
            if record["state"] != "validated" or not (record.get("validation") or {}).get("passed"):
                raise PackRepairError("REPAIR_NOT_VALIDATED", "Validation must pass before approval")
            if artifact_hash != record["artifact_hash"] or _directory_hash(Path(record["pack_root"])) != artifact_hash:
                raise PackRepairError("APPROVAL_DIGEST_MISMATCH", "Approval must bind the exact generated artifact")
            record["approval"] = {"actor_id": actor_id, "artifact_hash": artifact_hash, "reviewed": True}
            record["state"] = "approved"
            self._save_repair(connection, record)
            self._audit(connection, record["conflict_id"], "repair.approved", {
                "repair_id": repair_id, "actor_id": actor_id, "artifact_hash": artifact_hash
            })
            return deepcopy(record)

    def install(self, repair_id: str, current_packs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        validated = self.validate(repair_id, current_packs)
        with self._transaction() as connection:
            record = self._repair_for_update(connection, repair_id)
            approval = record.get("approval")
            if not approval or approval.get("artifact_hash") != record["artifact_hash"]:
                raise PackRepairError("APPROVAL_REQUIRED", "Generation and validation never imply installation approval")
            if not (validated.get("validation") or {}).get("passed"):
                raise PackRepairError("REPAIR_NOT_VALIDATED", "Repair Pack cannot be installed")
            record["installed"] = True
            record["state"] = "installed"
            self._save_repair(connection, record)
            self._audit(connection, record["conflict_id"], "repair.installed", {"repair_id": repair_id})
            return deepcopy(record)

    def activate(self, repair_id: str) -> dict[str, Any]:
        modified = False
        with self._transaction() as connection:
            record = self._repair_for_update(connection, repair_id)
            if not record["installed"] or not record.get("approval") or record["stale"]:
                raise PackRepairError("ACTIVATION_FORBIDDEN", "Only an installed, approved, current repair may activate")
            if _directory_hash(Path(record["pack_root"])) != record["artifact_hash"]:
                record["state"] = "modified"
                record["active"] = False
                record["installed"] = False
                record["approval"] = None
                self._save_repair(connection, record)
                self._audit(connection, record["conflict_id"], "repair.approval_invalidated", {
                    "repair_id": repair_id, "reason": "artifact_modified"
                })
                modified = True
            else:
                record["active"] = True
                record["state"] = "active"
                self._save_repair(connection, record)
                self._audit(connection, record["conflict_id"], "repair.activated", {"repair_id": repair_id})
        if modified:
            raise PackRepairError("ARTIFACT_MODIFIED", "Repair changed after approval")
        return deepcopy(record)

    def remove(self, repair_id: str) -> dict[str, Any]:
        """Remove the profile override without deleting the inspectable artifact."""

        with self._transaction() as connection:
            record = self._repair_for_update(connection, repair_id)
            record["installed"] = False
            record["active"] = False
            record["state"] = "removed"
            self._save_repair(connection, record)
            self._audit(connection, record["conflict_id"], "repair.removed", {"repair_id": repair_id})
            return deepcopy(record)

    def resolution_status(self, conflict_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT record_json FROM repair_packs WHERE conflict_id = ? ORDER BY repair_id", (conflict_id,)
            ).fetchall()
        repairs = [json.loads(row[0]) for row in rows]
        active = [item for item in repairs if item["active"] and not item["stale"]]
        lock_override = None
        if len(active) == 1:
            selected = active[0]
            plan = self._get_plan(selected["plan_id"])
            lock_override = {
                "pack_id": selected["pack_id"],
                "artifact_hash": selected["artifact_hash"],
                "repair_id": selected["repair_id"],
                "repairs_conflict_id": conflict_id,
                "profile_fingerprint": plan["profile_fingerprint"],
                "source_packs": plan["packs"],
            }
        return {
            "conflict_id": conflict_id,
            "resolved": len(active) == 1,
            "active_repair_id": active[0]["repair_id"] if len(active) == 1 else None,
            "profile_lock_override": lock_override,
            "repair_states": [{"repair_id": item["repair_id"], "state": item["state"]} for item in repairs],
        }

    def audit_events(self, conflict_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT event_type, payload_json FROM repair_audit WHERE conflict_id = ? ORDER BY event_id",
                (conflict_id,),
            ).fetchall()
        return [{"event_type": row[0], "payload": json.loads(row[1])} for row in rows]

    def dispatch(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        generator: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    ) -> Any:
        """Dispatch one finite vendor-neutral operation without combining lifecycle states."""

        request = dict(payload)
        handlers: dict[str, tuple[set[str], Callable[[], Any]]] = {
            "pack.conflicts.list": (set(), self.list_conflict_reviews),
            "pack.conflicts.get": (
                {"conflict_id"},
                lambda: self.get_conflict(str(request["conflict_id"])),
            ),
            "pack.repair.plan": (
                {"conflict_id", "repair_kind", "generation_run_id"},
                lambda: self.plan(
                    str(request["conflict_id"]),
                    repair_kind=str(request["repair_kind"]),
                    generation_run_id=str(request["generation_run_id"]),
                ),
            ),
            "pack.repair.generate": (
                {"plan_id"},
                lambda: self.generate(
                    str(request["plan_id"]),
                    generator or _unavailable_generator,
                ),
            ),
            "pack.repair.validate": (
                {"repair_id", "current_packs"},
                lambda: self.validate(
                    str(request["repair_id"]),
                    _sequence(request["current_packs"], "current_packs"),
                ),
            ),
            "pack.repair.approve": (
                {"repair_id", "actor_id", "artifact_hash"},
                lambda: self.approve(
                    str(request["repair_id"]),
                    actor_id=str(request["actor_id"]),
                    artifact_hash=str(request["artifact_hash"]),
                ),
            ),
            "pack.repair.install": (
                {"repair_id", "current_packs"},
                lambda: self.install(
                    str(request["repair_id"]),
                    _sequence(request["current_packs"], "current_packs"),
                ),
            ),
            "pack.repair.activate": (
                {"repair_id"},
                lambda: self.activate(str(request["repair_id"])),
            ),
            "pack.repair.remove": (
                {"repair_id"},
                lambda: self.remove(str(request["repair_id"])),
            ),
            "pack.repair.status": (
                {"conflict_id"},
                lambda: self.resolution_status(str(request["conflict_id"])),
            ),
        }
        entry = handlers.get(operation)
        if entry is None:
            raise PackRepairError("OPERATION_UNKNOWN", "Unknown Pack repair operation")
        required, handler = entry
        if set(request) != required:
            raise PackRepairError(
                "OPERATION_PAYLOAD_INVALID",
                "Pack repair operation payload must contain exactly its declared fields",
                details={"required": sorted(required)},
            )
        _reject_sensitive(request)
        return handler()

    def _normalize_generator_output(self, plan: Mapping[str, Any], output: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"pack_id", "display_name", "repair", "resources", "requested_capabilities"}
        if set(output) - allowed:
            raise PackRepairError("GENERATOR_OUTPUT_INVALID", "Generated output contains undeclared fields")
        if _strings(output.get("requested_capabilities") or []):
            raise PackRepairError("CAPABILITY_EXPANSION_FORBIDDEN", "Repair generation cannot request new capabilities")
        pack_id = str(output.get("pack_id") or f"generated.repair.{plan['conflict_id'][4:]}")
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+", pack_id):
            raise PackRepairError("PACK_ID_INVALID", "Generated repair Pack ID is invalid")
        repair = _mapping(output.get("repair"), "repair")
        if repair.get("kind") != plan["repair_kind"]:
            raise PackRepairError("REPAIR_KIND_MISMATCH", "Generator changed the approved repair class")
        resources: dict[str, Mapping[str, Any]] = {}
        for relative, document in _mapping(output.get("resources") or {}, "resources").items():
            path = str(relative).replace("\\", "/")
            if not path.startswith("resources/") or not path.endswith(".json"):
                raise PackRepairError("GENERATED_FILE_FORBIDDEN", "Only declarative JSON resources are allowed")
            resources[path] = _mapping(document, path)
        return {
            "pack_id": pack_id,
            "display_name": str(output.get("display_name") or "Generated conflict repair")[:128],
            "repair": repair,
            "resources": resources,
        }

    def _static_validate(self, root: Path, plan: Mapping[str, Any], metadata: Mapping[str, Any]) -> None:
        validate_document((root / "pack.v4.json").read_bytes(), "pack")
        validate_document((root / "contracts.v4.json").read_bytes(), "pack_contract_catalog")
        validate_document((root / "artifact-index.v4.json").read_bytes(), "pack_artifact_index")
        validate_document((root / "executables.v4.json").read_bytes(), "executable_catalog")
        validate_document(metadata, "generated_repair_pack")
        manifest = json.loads((root / "pack.v4.json").read_text(encoding="utf-8"))
        if manifest["requirements"]["capabilities"] or manifest["requirements"]["secrets"]:
            raise PackRepairError("CAPABILITY_EXPANSION_FORBIDDEN", "Repair Pack authority envelope must be empty")
        if manifest["functions"] or manifest["requirements"]["execution_boundary"] != "declarative_only":
            raise PackRepairError("EXECUTABLE_REPAIR_FORBIDDEN", "Discovery and dry-run repair must be inert")
        content = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in root.rglob("*")
            if path.is_file()
        )
        for pack in plan["packs"]:
            private_markers = (
                f"ecosystem.{pack['pack_id']}",
                f"ecosystem/{pack['pack_id']}",
                f"ecosystem\\{pack['pack_id']}",
            )
            if any(marker in content for marker in private_markers):
                raise PackRepairError("PRIVATE_SOURCE_COUPLING", "Repair imports a source Pack private tree")
        _reject_sensitive(metadata)

    def _dry_run(self, plan: Mapping[str, Any], metadata: Mapping[str, Any]) -> dict[str, Any]:
        repair = _mapping(metadata.get("repair"), "repair")
        kind = plan["repair_kind"]
        resolved = False
        reason = "repair did not prove the declared bridge"
        if kind == "provider_selection":
            providers = {item.get("provider_instance_id") for item in plan["packs"]}
            selected = repair.get("selected_provider_instance_id")
            resolved = selected in providers and selected is not None
            reason = "explicit selected provider is one of the exact conflicting artifacts"
        elif kind == "schema_adapter":
            resolved = repair.get("schema_compatibility") == "fixture_proven"
            reason = "adapter declares fixture-proven bounded schema compatibility"
        elif kind in {"contract_alias", "namespace_mapping", "profile_routing", "constraint_refinement", "policy_chain"}:
            resolved = bool(repair.get("mapping")) and repair.get("validated") is True
            reason = "bounded declarative mapping validated"
        return {"resolved": resolved, "reason": reason, "executed_entrypoints": False}

    def _get_plan(self, plan_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            return self._plan_for_connection(connection, plan_id)

    @staticmethod
    def _plan_for_connection(connection: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT plan_json FROM repair_plans WHERE plan_id = ?", (plan_id,)).fetchone()
        if row is None:
            raise PackRepairError("PLAN_NOT_FOUND", "Repair plan does not exist")
        return json.loads(row[0])

    @staticmethod
    def _repair_for_update(connection: sqlite3.Connection, repair_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT record_json FROM repair_packs WHERE repair_id = ?", (repair_id,)).fetchone()
        if row is None:
            raise PackRepairError("REPAIR_NOT_FOUND", "Generated repair Pack does not exist")
        return json.loads(row[0])

    @staticmethod
    def _save_repair(connection: sqlite3.Connection, record: Mapping[str, Any]) -> None:
        connection.execute(
            "UPDATE repair_packs SET record_json = ?, updated_at = ? WHERE repair_id = ?",
            (_json(record), time.time(), record["repair_id"]),
        )

    def _audit(
        self, connection: sqlite3.Connection, conflict_id: str, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        _reject_sensitive(payload)
        connection.execute(
            "INSERT INTO repair_audit(conflict_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (conflict_id, event_type, _json(payload), time.time()),
        )
        connection.execute(
            """
            DELETE FROM repair_audit
            WHERE conflict_id = ? AND event_id NOT IN (
                SELECT event_id FROM repair_audit
                WHERE conflict_id = ? ORDER BY event_id DESC LIMIT 1024
            )
            """,
            (conflict_id, conflict_id),
        )

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS pack_conflicts(
                    conflict_id TEXT PRIMARY KEY, report_json TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS repair_plans(
                    plan_id TEXT PRIMARY KEY, conflict_id TEXT NOT NULL,
                    plan_json TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS repair_packs(
                    repair_id TEXT PRIMARY KEY, conflict_id TEXT NOT NULL,
                    record_json TEXT NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS repair_audit(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT, conflict_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at REAL NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _transaction(self):
        return _Transaction(self)


class _Transaction:
    def __init__(self, manager: PackRepairManager) -> None:
        self.manager = manager
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        self.manager._lock.acquire()
        self.connection = self.manager._connect()
        self.connection.execute("BEGIN IMMEDIATE")
        return self.connection

    def __exit__(self, exc_type, _exc, _tb) -> None:
        assert self.connection is not None
        try:
            self.connection.rollback() if exc_type else self.connection.commit()
        finally:
            self.connection.close()
            self.manager._lock.release()


def _directory_hash(root: Path) -> str:
    digest = hashlib.sha256()
    entries = sorted(root.rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise PackRepairError("ARTIFACT_LINK_FORBIDDEN", "Repair Pack artifacts cannot contain links")
    for path in (item for item in entries if item.is_file()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _pack_hashes(packs: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for pack in packs:
        pack_id = str(pack.get("pack_id") or "")
        artifact_hash = str(pack.get("artifact_hash") or "")
        if not ID.fullmatch(pack_id) or not SHA256.fullmatch(artifact_hash) or pack_id in result:
            raise PackRepairError("PACK_IDENTITY_INVALID", "Current source Pack identity is invalid")
        result[pack_id] = artifact_hash
    return result


def _scoped_resource(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or any(part.startswith(".") for part in pure.parts):
        raise PackRepairError("GENERATED_PATH_INVALID", "Generated resource escaped the Pack root")
    path = root.joinpath(*pure.parts)
    resolved_root = root.resolve()
    resolved = path.resolve()
    if resolved_root not in resolved.parents:
        raise PackRepairError("GENERATED_PATH_INVALID", "Generated resource escaped the Pack root")
    return path


def _safe_schema(value: Mapping[str, Any]) -> dict[str, Any]:
    schema = deepcopy(dict(value))
    _reject_sensitive(schema)
    if len(_json(schema)) > 64_000:
        raise PackRepairError("SCHEMA_TOO_LARGE", "Conflict schema input exceeds the bounded size")
    return schema


def _reject_sensitive(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if SENSITIVE_KEY.search(key_text):
                raise PackRepairError(
                    "SENSITIVE_DATA_FORBIDDEN", "Secrets and hidden context cannot enter repair state",
                    details={"field": ".".join((*path, key_text))},
                )
            _reject_sensitive(child, (*path, key_text))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            _reject_sensitive(child, (*path, str(index)))
    elif isinstance(value, str) and SENSITIVE_VALUE.search(value):
        raise PackRepairError("SENSITIVE_DATA_FORBIDDEN", "Credential-shaped value rejected")


def _strings(value: Any) -> list[str]:
    result = sorted({str(item).strip() for item in _sequence(value, "string list") if str(item).strip()})
    if any(len(item) > 512 for item in result):
        raise PackRepairError("VALUE_TOO_LONG", "Repair contract strings are bounded")
    return result


def _bounded_messages(value: Sequence[str]) -> list[str]:
    return sorted({str(item)[:512] for item in value[:64] if str(item).strip()})


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PackRepairError("MAPPING_REQUIRED", f"{label} must be an object")
    return dict(value)


def _sequence(value: Any, label: str) -> list[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise PackRepairError("LIST_REQUIRED", f"{label} must be a list")
    if len(value) > 1024:
        raise PackRepairError("LIST_TOO_LARGE", f"{label} exceeds the bounded list size")
    return list(value)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _unavailable_generator(_plan: dict[str, Any]) -> Mapping[str, Any]:
    raise RuntimeError("no AI generator contract is available")


__all__ = [
    "CONFLICT_API_VERSION",
    "PLAN_API_VERSION",
    "PACK_REPAIR_OPERATIONS",
    "REPAIR_API_VERSION",
    "PackRepairError",
    "PackRepairManager",
    "build_pack_conflict_report",
]
