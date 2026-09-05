from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import unicodedata
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from domain.frontend.invocation_events import InvocationEventError, InvocationEventStore
from domain.frontend.offline_queue import OfflineOperationQueue
from domain.frontend.command_operations import CommandOperationRegistry
from domain.frontend.command_registry import SlashCommandRegistry
from domain.frontend_settings_store import FrontendSettingsStore, defaultspack_frontend_settings_path

API_VERSION = "tobkiri.commands/v1"
PACK_ID = "defaultspack"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "command-protocol-v1.schema.json"
LEGACY_HOST_STATE_REFS = {
    "toggle_yolo": "host:approval.full_access",
    "toggle_ultra_yolo": "host:approval.full_access",
    "set_fast_mode": "host:model.fast_mode",
}

# This is a legacy compatibility registry, not the future host-operation
# registry. Keeping it explicit prevents a manifest typo from becoming a
# silent frontend no-op while the v1 broker is introduced.
LEGACY_FRONTEND_HANDLERS = {
    "clear_composer_state",
    "new_conversation",
    "open_approvals",
    "open_branch_picker",
    "open_command_help",
    "open_context_viewer",
    "open_debug",
    "open_diff_preview",
    "open_file_search",
    "open_history",
    "open_hooks",
    "open_keymap_settings",
    "open_logs",
    "open_mcp",
    "open_memory_inspector",
    "open_permissions",
    "open_plugins",
    "open_settings",
    "open_skills",
    "open_theme_settings",
    "open_tool_picker",
    "prepare_lint_run",
    "prepare_test_run",
    "request_commit_approval",
    "request_patch_approval",
    "request_push_approval",
    "request_restore_approval",
    "request_terminal_approval",
    "resume_conversation",
    "rename_conversation",
    "run_doctor",
    "set_fast_mode",
    "set_home_title",
    "set_mode_agent",
    "set_mode_chat",
    "set_mode_coding",
    "set_price_mode",
    "show_status",
    "show_raw",
    "show_usage",
    "start_review",
    "export_conversation",
    "fork_conversation",
    "toggle_ultra_yolo",
    "toggle_yolo",
}
OPERATION_AUTHORITY = {
    "host:request_commit_approval": {
        "permissions": ["host.process.exec_guarded"],
        "approval_policy": "required",
        "executor_policy_ref": "tobkiri.command.human_approved",
    },
    "host:request_push_approval": {
        "permissions": ["host.process.exec_guarded"],
        "approval_policy": "required",
        "executor_policy_ref": "tobkiri.command.human_approved",
    },
    "host:request_terminal_approval": {
        "permissions": ["host.process.exec_guarded"],
        "approval_policy": "required",
        "executor_policy_ref": "tobkiri.command.human_approved",
    },
    "host:request_patch_approval": {
        "permissions": ["host.process.exec_guarded"],
        "approval_policy": "required",
        "executor_policy_ref": "tobkiri.command.human_approved",
    },
    "host:request_restore_approval": {
        "permissions": ["host.process.exec_guarded"],
        "approval_policy": "required",
        "executor_policy_ref": "tobkiri.command.human_approved",
    },
}
EXECUTOR_POLICIES = {
    "tobkiri.command.standard": {"high_risk": False},
    "tobkiri.command.human_approved": {"high_risk": True},
}


class CommandProtocolSchemaError(ValueError):
    pass


def validate_protocol_document(document: dict[str, Any]) -> None:
    """Validate a v1 manifest/catalog with strict Draft 2020-12 semantics."""

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    if not errors:
        return
    first = errors[0]
    path = ".".join(str(part) for part in first.absolute_path) or "$"
    raise CommandProtocolSchemaError(f"{path}: {first.message}")


class CommandProtocolRegistry:
    """Resolved Command Protocol v1 view over the legacy command registry.

    Pack manifests remain authoritative and separate. This class only derives
    a validated, read-only catalog and a dual-stack invocation envelope.
    """

    _datasource_cache_lock = threading.Lock()
    _datasource_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
    _datasource_cache_ttl_seconds = 15.0

    def __init__(
        self,
        pack_root: Path | None = None,
        *,
        event_store: InvocationEventStore | None = None,
        offline_queue: OfflineOperationQueue | None = None,
    ) -> None:
        self.pack_root = pack_root or Path(__file__).resolve().parents[2]
        self._settings_owner = pack_root if pack_root is not None else None
        self.legacy = SlashCommandRegistry(self.pack_root)
        self.operations = CommandOperationRegistry(self.legacy, self.pack_root)
        settings_path = defaultspack_frontend_settings_path(self._settings_owner)
        self._event_store = event_store
        self._offline_queue = offline_queue
        self._event_store_path = settings_path.with_name(
            "command_invocation_events.sqlite3"
        )
        self._offline_queue_path = settings_path.with_name(
            "command_offline_queue.sqlite3"
        )

    @property
    def events(self) -> InvocationEventStore:
        if self._event_store is None:
            self._event_store = InvocationEventStore(self._event_store_path)
        return self._event_store

    @property
    def offline(self) -> OfflineOperationQueue:
        if self._offline_queue is None:
            self._offline_queue = OfflineOperationQueue(self._offline_queue_path)
        return self._offline_queue

    def catalog(self) -> dict[str, Any]:
        pack_generation = self._pack_generation()
        legacy_commands = self._source_commands()
        diagnostics = [deepcopy(item) for item in self.legacy.manifest_errors()]
        collisions = self._identity_collisions(legacy_commands)
        diagnostics.extend(collisions)
        commands = [
            self._resolve_command(command, diagnostics, pack_generation)
            for command in legacy_commands
        ]
        serialized = json.dumps(commands, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        revision = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
        catalog = {
            "api_version": API_VERSION,
            "kind": "ResolvedCommandCatalog",
            "catalog_revision": revision,
            "pack_generations": {PACK_ID: pack_generation},
            "rollout": {
                "feature_flag": "command_protocol_v1",
                "phase": "enforced",
                "legacy_execution_enabled": False,
            },
            "commands": commands,
            "states": [
                {
                    "state_ref": "defaultspack:models.deepthink_enabled",
                    "schema_version": "1.0.0",
                    "value_type": "boolean",
                    "authority": "backend_runtime",
                }
            ],
            "datasources": [
                {
                    "datasource_ref": "tobkiri:model_catalog",
                    "schema_version": "1.0.0",
                    "item_contract": "OptionItem",
                    "capabilities": ["search", "cursor_paging", "selected_item_retention"],
                },
                {
                    "datasource_ref": "tobkiri:provider_catalog",
                    "schema_version": "1.0.0",
                    "item_contract": "OptionItem",
                    "capabilities": ["search", "cursor_paging", "selected_item_retention"],
                },
            ],
            "state_snapshots": self.query_states(
                ["defaultspack:models.deepthink_enabled"]
            )["states"],
            "diagnostics": diagnostics,
        }
        validate_protocol_document(catalog)
        return catalog

    def legacy_read_projection(self) -> list[dict[str, Any]]:
        """Deprecated read-only projection; never used by the v1 executor."""

        catalog = self.catalog()
        source_commands = self._source_commands(public=True)
        source_by_id = {
            str(item.get("id") or item.get("name") or ""): deepcopy(item)
            for item in source_commands
        }
        return [
            {
                **source_by_id.get(str(command["identity"]["id"]), {}),
                "canonical_id": command["canonical_id"],
                "protocol_presentation": command["presentation"],
                "protocol_execution": command["execution"],
                "availability": command["availability"],
            }
            for command in catalog["commands"]
        ]

    def conformance_matrix(self) -> list[dict[str, Any]]:
        source_by_id = {
            str(item.get("id") or item.get("name") or ""): item
            for item in self._source_commands()
        }
        return [
            {
                "command_id": command["canonical_id"],
                "modes": command["constraints"]["modes"],
                "authority": command["authorization"],
                **self.operations.binding_contract(
                    source_by_id[str(command["identity"]["id"])],
                    command,
                ),
            }
            for command in self.catalog()["commands"]
        ]

    def invoke(self, payload: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            owner_key = self._owner_key(payload, context)
        except ValueError as exc:
            return {
                "api_version": API_VERSION,
                "status": "failed",
                "command_ref": payload.get("command_ref") or payload.get("command"),
                "state_changes": [],
                "error": {
                    "code": "UNTRUSTED_OWNER_SCOPE",
                    "message": str(exc),
                },
            }
        operation_id = str(
            payload.get("invocation_id")
            or payload.get("operation_id")
            or uuid.uuid4()
        )
        request_fingerprint = self._request_fingerprint(payload)
        self.events.recover_stale(operation_id, owner_key=owner_key)
        existing = self.events.stored(operation_id, owner_key=owner_key)
        continuing_approval = False
        if existing:
            original_fingerprint = str(existing.get("request_fingerprint") or "")
            if original_fingerprint and original_fingerprint != request_fingerprint:
                return {
                    "api_version": API_VERSION,
                    "operation_id": operation_id,
                    "status": "failed",
                    "command_ref": payload.get("command_ref")
                    or payload.get("command"),
                    "state_changes": [],
                    "error": {
                        "code": "INVOCATION_CONFLICT",
                        "message": (
                            "invocation_id was reused for a different request"
                        ),
                    },
                    "progress": self.events.snapshot(
                        operation_id,
                        owner_key=owner_key,
                    ),
                }
            stored_result = existing.get("result")
            may_resume = bool(
                payload.get("approval_token")
                or payload.get("authority_approval_token")
            ) and existing.get("state") == "approval_required"
            if isinstance(stored_result, dict) and not may_resume:
                result = deepcopy(stored_result)
                result["progress"] = self.events.snapshot(
                    operation_id,
                    owner_key=owner_key,
                )
                return result
            continuing_approval = may_resume
            if not continuing_approval:
                return {
                    "api_version": API_VERSION,
                    "operation_id": operation_id,
                    "status": "failed",
                    "command_ref": payload.get("command_ref")
                    or payload.get("command"),
                    "state_changes": [],
                    "error": {
                        "code": "INVOCATION_IN_PROGRESS",
                        "message": "invocation is already in progress",
                    },
                    "progress": self.events.snapshot(
                        operation_id,
                        owner_key=owner_key,
                    ),
                }
        invocation_payload = {
            **payload,
            "operation_id": operation_id,
        }
        resume_lease_id = uuid.uuid4().hex if continuing_approval else None
        claimed = (
            self.events.claim_resume(
                operation_id,
                owner_key=owner_key,
                request_fingerprint=request_fingerprint,
                lease_id=str(resume_lease_id),
            )
            if continuing_approval
            else self.events.claim(
                operation_id,
                {
                    "command_ref": payload.get("command_ref")
                    or payload.get("command"),
                    "request_fingerprint": request_fingerprint,
                },
                owner_key=owner_key,
                request_fingerprint=request_fingerprint,
            )
        )
        if not claimed:
            return {
                "api_version": API_VERSION,
                "operation_id": operation_id,
                "status": "failed",
                "command_ref": payload.get("command_ref")
                or payload.get("command"),
                "state_changes": [],
                "error": {
                    "code": "INVOCATION_IN_PROGRESS",
                    "message": "invocation is already in progress",
                },
                "progress": self.events.snapshot(
                    operation_id,
                    owner_key=owner_key,
                ),
            }
        self.events.append(
            operation_id,
            "validating",
            {"command_ref": payload.get("command_ref") or payload.get("command")},
            owner_key=owner_key,
        )
        expected_before_execution = (
            "resuming" if continuing_approval else "accepted"
        )
        if not self.events.mark_executing(
            operation_id,
            owner_key=owner_key,
            expected_state=expected_before_execution,
            lease_id=resume_lease_id,
        ):
            return {
                "api_version": API_VERSION,
                "operation_id": operation_id,
                "status": "failed",
                "command_ref": payload.get("command_ref")
                or payload.get("command"),
                "state_changes": [],
                "error": {
                    "code": "INVOCATION_STATE_CONFLICT",
                    "message": "invocation was cancelled before execution",
                },
                "progress": self.events.snapshot(
                    operation_id,
                    owner_key=owner_key,
                ),
            }
        try:
            result = self._invoke_resolved(
                invocation_payload,
                {**(context or {}), "_trusted_owner_key": owner_key},
            )
        except Exception as exc:
            failure = {
                "api_version": API_VERSION,
                "operation_id": operation_id,
                "status": "failed",
                "command_ref": payload.get("command_ref")
                or payload.get("command"),
                "state_changes": [],
                "error": {
                    "code": type(exc).__name__,
                    "message": "command execution failed",
                },
            }
            try:
                self.events.settle_terminal(
                    operation_id,
                    "failed",
                    owner_key=owner_key,
                    result=failure,
                    event_type="failed",
                    event_payload={"error": failure["error"]},
                    expected_states={"executing"},
                    lease_id=resume_lease_id,
                )
            except InvocationEventError:
                return self._state_conflict_result(
                    operation_id,
                    owner_key,
                    payload,
                )
            failure["progress"] = self.events.snapshot(
                operation_id,
                owner_key=owner_key,
            )
            return failure
        event_type = {
            "approval_required": "approval_required",
            "succeeded": "completed",
            "failed": "failed",
        }.get(str(result.get("status") or ""), "failed")
        approval_request_id = str(
            (result.get("approval") or {}).get("request_id") or ""
        ) or None
        audit_projection = {
            "command_ref": result.get("command_ref"),
            "state_change_refs": [
                str(item.get("state_ref") or "")
                for item in result.get("state_changes") or []
                if isinstance(item, dict)
            ],
            "error_code": str((result.get("error") or {}).get("code") or "")
            or None,
            "approval_request_id": approval_request_id,
        }
        try:
            if event_type in {"completed", "failed"}:
                self.events.settle_terminal(
                    operation_id,
                    str(result.get("status") or "failed"),
                    owner_key=owner_key,
                    result=result,
                    event_type=event_type,
                    event_payload=audit_projection,
                    approval_request_id=approval_request_id,
                    expected_states={"executing"},
                    lease_id=resume_lease_id,
                )
            else:
                self.events.set_state(
                    operation_id,
                    str(result.get("status") or "failed"),
                    owner_key=owner_key,
                    result=result,
                    approval_request_id=approval_request_id,
                    expected_states={"executing"},
                    lease_id=resume_lease_id,
                )
                self.events.append(
                    operation_id,
                    event_type,
                    audit_projection,
                    owner_key=owner_key,
                )
        except InvocationEventError:
            return self._state_conflict_result(
                operation_id,
                owner_key,
                payload,
            )
        result["progress"] = self.events.snapshot(
            operation_id,
            owner_key=owner_key,
        )
        return result

    def _state_conflict_result(
        self,
        operation_id: str,
        owner_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        stored = self.events.stored(operation_id, owner_key=owner_key)
        durable = stored.get("result") if stored else None
        result = (
            deepcopy(durable)
            if isinstance(durable, dict)
            else {
                "api_version": API_VERSION,
                "operation_id": operation_id,
                "status": "failed",
                "command_ref": payload.get("command_ref")
                or payload.get("command"),
                "state_changes": [],
                "error": {
                    "code": "INVOCATION_STATE_CONFLICT",
                    "message": "invocation state changed concurrently",
                },
            }
        )
        result["progress"] = self.events.snapshot(
            operation_id,
            owner_key=owner_key,
        )
        return result

    @staticmethod
    def _request_fingerprint(payload: dict[str, Any]) -> str:
        request = {
            "command_ref": payload.get("command_ref") or payload.get("command"),
            "args": payload.get("args")
            if isinstance(payload.get("args"), dict)
            else {},
            "conversation_id": payload.get("conversation_id"),
            "mode": payload.get("mode") or "chat",
            "expected_revision": payload.get("expected_revision"),
            "catalog_revision": payload.get("catalog_revision"),
            "profile_id": payload.get("profile_id"),
        }
        encoded = json.dumps(
            request,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _owner_key(
        payload: dict[str, Any],
        context: dict[str, Any] | None,
    ) -> str:
        if "_owner_key" in payload or "principal_id" in payload:
            raise ValueError(
                "_owner_key and principal_id are reserved transport fields"
            )
        source = context if isinstance(context, dict) else {}
        trusted_override = str(source.get("_trusted_owner_key") or "").strip()
        if trusted_override:
            return trusted_override[:512]
        principal = str(
            source.get("authenticated_principal_id")
            or source.get("principal_id")
            or source.get("principal")
            or "local"
        ).strip()
        profile = str(
            source.get("authorized_profile_id")
            or source.get("profile_id")
            or "default"
        ).strip()
        requested_profile = str(payload.get("profile_id") or "").strip()
        if requested_profile and requested_profile != profile:
            raise ValueError("profile_id is not authorized by request context")
        return f"{principal}:{profile}"[:512]

    @classmethod
    def owner_key(
        cls,
        payload: dict[str, Any],
        context: dict[str, Any] | None,
    ) -> str:
        return cls._owner_key(payload, context)

    def _invoke_resolved(
        self,
        payload: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        command_ref = str(payload.get("command_ref") or payload.get("command") or "").strip()
        short_name = command_ref.split(":", 1)[-1] if ":" in command_ref else command_ref
        catalog = self.catalog()
        requested_catalog_revision = str(
            payload.get("catalog_revision") or ""
        ).strip()
        if (
            requested_catalog_revision
            and requested_catalog_revision != catalog["catalog_revision"]
        ):
            return {
                "api_version": API_VERSION,
                "operation_id": str(
                    payload.get("invocation_id")
                    or payload.get("operation_id")
                    or ""
                ),
                "status": "failed",
                "command_ref": command_ref,
                "error": {
                    "code": "CATALOG_REVISION_CONFLICT",
                    "message": "command catalog changed; refresh before invoking",
                },
                "state_changes": [],
            }
        catalog_commands = catalog["commands"]
        matches = [
            item
            for item in catalog_commands
            if command_ref == item["canonical_id"]
            or short_name
            in {
                str(item["identity"].get("id") or ""),
                str(item["identity"].get("name") or ""),
                *(str(alias) for alias in item["identity"].get("aliases") or []),
            }
        ]
        operation_id = str(
            payload.get("invocation_id")
            or payload.get("operation_id")
            or uuid.uuid4()
        )
        if len(matches) != 1:
            return {
                "api_version": API_VERSION,
                "operation_id": operation_id,
                "status": "failed",
                "command_ref": command_ref,
                "error": {
                    "code": (
                        "COMMAND_IDENTITY_COLLISION"
                        if len(matches) > 1
                        else "COMMAND_NOT_FOUND"
                    ),
                    "message": (
                        "command identity is ambiguous"
                        if len(matches) > 1
                        else "command not found"
                    ),
                },
                "state_changes": [],
            }

        resolved = matches[0]
        source_commands = self._source_commands()
        command = next(
            (
                item
                for item in source_commands
                if str(item.get("id") or item.get("name") or "")
                == str(resolved["identity"]["id"])
            ),
            None,
        )
        if command is None:
            return {
                "api_version": API_VERSION,
                "operation_id": operation_id,
                "status": "failed",
                "command_ref": command_ref,
                "error": {
                    "code": "OPERATION_BINDING_MISSING",
                    "message": "resolved command has no registered source binding",
                },
                "state_changes": [],
            }
        availability = resolved.get("availability", {})
        if availability.get("status") == "unavailable":
            return {
                "api_version": API_VERSION,
                "operation_id": operation_id,
                "status": "failed",
                "command_ref": resolved["canonical_id"],
                "error": {
                    "code": "COMMAND_UNAVAILABLE",
                    "message": availability.get("reason") or "command is unavailable",
                },
                "state_changes": [],
            }

        legacy_payload = {
            "command": command.get("name") or command.get("id"),
            "args": payload.get("args") if isinstance(payload.get("args"), dict) else {},
            "conversation_id": payload.get("conversation_id"),
            "mode": payload.get("mode") or "chat",
            "invocation_id": operation_id,
            "idempotency_key": payload.get("idempotency_key"),
            "client_sequence": payload.get("client_sequence"),
            "expected_revision": payload.get("expected_revision"),
        }
        if resolved.get("authorization", {}).get("approval_required"):
            approval_result = self._authorize_high_risk_command(
                command,
                resolved,
                legacy_payload,
                payload,
                context or {},
            )
            if approval_result is not None:
                return approval_result
        legacy_result = self.operations.invoke(
            command,
            resolved,
            legacy_payload,
            context or {},
        )
        if legacy_result.get("status") == "error":
            return {
                "api_version": API_VERSION,
                "operation_id": operation_id,
                "status": "failed",
                "command_ref": resolved["canonical_id"],
                "error": deepcopy(legacy_result.get("error") or {}),
                "state_changes": [],
            }
        data = legacy_result.get("data") if isinstance(legacy_result.get("data"), dict) else {}
        requires_approval = bool(data.get("requires_approval"))
        return {
            "api_version": API_VERSION,
            "operation_id": str(data.get("operation_id") or operation_id),
            "status": "approval_required" if requires_approval else "succeeded",
            "command_ref": resolved["canonical_id"],
            "client_sequence": data.get("client_sequence", payload.get("client_sequence")),
            "state_changes": deepcopy(data.get("state_changes") or []),
            "approval": (
                {
                    "required": True,
                    "permission_ids": resolved.get("authorization", {}).get("permissions", []),
                }
                if requires_approval
                else None
            ),
            "message": data.get("message"),
            "legacy_result": deepcopy(data),
        }

    def _authorize_high_risk_command(
        self,
        command: dict[str, Any],
        resolved: dict[str, Any],
        legacy_payload: dict[str, Any],
        payload: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any] | None:
        execution = (
            command.get("execution")
            if isinstance(command.get("execution"), dict)
            else {}
        )
        action = str(execution.get("action") or "").strip()
        raw_args = (
            legacy_payload.get("args")
            if isinstance(legacy_payload.get("args"), dict)
            else {}
        )
        if execution.get("type") != "frontend" or action not in LEGACY_FRONTEND_HANDLERS:
            return self._high_risk_failure(
                resolved,
                legacy_payload,
                "HOST_OPERATION_NOT_REGISTERED",
                "approved host operation is not registered",
            )
        authorization = (
            resolved.get("authorization")
            if isinstance(resolved.get("authorization"), dict)
            else {}
        )
        policy_ref = str(authorization.get("executor_policy_ref") or "")
        policy = EXECUTOR_POLICIES.get(policy_ref)
        if policy is None or policy.get("high_risk") is not True:
            return self._high_risk_failure(
                resolved,
                legacy_payload,
                "EXECUTOR_POLICY_DENIED",
                "high-risk command executor policy is not registered",
            )
        if str(legacy_payload.get("mode") or "chat") not in command.get("modes", []):
            return self._high_risk_failure(
                resolved,
                legacy_payload,
                "COMMAND_UNAVAILABLE",
                "command is not available in this mode",
            )
        args = self.operations.source_registry.coerce_operation_args(
            command,
            raw_args,
        )
        if isinstance(args, dict) and args.get("status") == "error":
            return self._high_risk_failure(
                resolved,
                legacy_payload,
                str((args.get("error") or {}).get("code") or "INVALID_COMMAND"),
                str((args.get("error") or {}).get("message") or "command arguments are invalid"),
            )
        try:
            self.operations.binding_contract(command, resolved)
        except ValueError as exc:
            return self._high_risk_failure(
                resolved,
                legacy_payload,
                "HOST_OPERATION_NOT_REGISTERED",
                str(exc),
            )
        try:
            operation_plan = self.operations.prepare_high_risk_plan(
                action,
                args,
                context,
            )
        except ValueError as exc:
            return self._high_risk_failure(
                resolved,
                legacy_payload,
                "OPERATION_PLAN_INVALID",
                str(exc),
            )
        return self._enforce_runtime_authority(
            resolved,
            legacy_payload,
            payload,
            context,
            operation_plan,
        )

    @staticmethod
    def _high_risk_failure(
        resolved: dict[str, Any],
        legacy_payload: dict[str, Any],
        code: str,
        message: str,
    ) -> dict[str, Any]:
        return {
            "api_version": API_VERSION,
            "operation_id": str(legacy_payload.get("invocation_id") or ""),
            "status": "failed",
            "command_ref": resolved["canonical_id"],
            "state_changes": [],
            "error": {"code": code, "message": message},
        }

    def _enforce_runtime_authority(
        self,
        resolved: dict[str, Any],
        legacy_payload: dict[str, Any],
        payload: dict[str, Any],
        context: dict[str, Any],
        operation_plan: dict[str, Any],
    ) -> dict[str, Any] | None:
        authorization = (
            resolved.get("authorization")
            if isinstance(resolved.get("authorization"), dict)
            else {}
        )
        policy_ref = str(authorization.get("executor_policy_ref") or "")
        policy = EXECUTOR_POLICIES.get(policy_ref)
        if policy is None or policy.get("high_risk") is not True:
            return {
                "api_version": API_VERSION,
                "operation_id": str(legacy_payload.get("invocation_id") or ""),
                "status": "failed",
                "command_ref": resolved["canonical_id"],
                "state_changes": [],
                "error": {
                    "code": "EXECUTOR_POLICY_DENIED",
                    "message": "high-risk command executor policy is not registered",
                },
            }
        return {
            "api_version": API_VERSION,
            "operation_id": str(legacy_payload.get("invocation_id") or ""),
            "status": "failed",
            "command_ref": resolved["canonical_id"],
            "state_changes": [],
            "error": {
                "code": "HIGH_RISK_COMMAND_ADAPTER_REQUIRED",
                "message": (
                    "high-risk commands must use the captured Host interactive "
                    "approval adapter"
                ),
            },
        }

    def query_states(self, state_refs: list[str] | None = None) -> dict[str, Any]:
        requested = {str(item or "").strip() for item in state_refs or [] if str(item or "").strip()}
        states: list[dict[str, Any]] = []
        deepthink_ref = "defaultspack:models.deepthink_enabled"
        if not requested or deepthink_ref in requested:
            from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService

            value = ModelRuntimeSettingsService(self.pack_root).get_deepthink_enabled()
            states.append(
                {
                    "state_ref": deepthink_ref,
                    "value": bool(value.get("enabled")),
                    "revision": int(value.get("revision") or 0),
                    "freshness": "authoritative",
                }
            )
        return {"api_version": API_VERSION, "states": states}

    def enqueue_offline(
        self,
        payload: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Queue one explicit desired-state command for caller-triggered replay."""

        command_ref = str(payload.get("command_ref") or "").strip()
        command = next(
            (
                item
                for item in self.catalog()["commands"]
                if item["canonical_id"] == command_ref
            ),
            None,
        )
        if command is None:
            return {
                "api_version": API_VERSION,
                "status": "failed",
                "error": {
                    "code": "COMMAND_NOT_FOUND",
                    "message": "command not found",
                },
            }
        try:
            expected_revision = int(payload.get("expected_revision"))
        except (TypeError, ValueError):
            return {
                "api_version": API_VERSION,
                "status": "failed",
                "error": {
                    "code": "INVALID_INPUT",
                    "message": "expected_revision must be an integer",
                },
            }
        record = self.offline.enqueue(
            command=command,
            args=payload.get("args") if isinstance(payload.get("args"), dict) else {},
            idempotency_key=str(payload.get("idempotency_key") or ""),
            expected_revision=expected_revision,
            owner_key=self._owner_key(payload, context),
            pack_generation=int(command.get("pack_generation") or 0),
        )
        return {
            "api_version": API_VERSION,
            "status": "queued",
            "queue": record,
        }

    def replay_offline(
        self,
        *,
        limit: int = 100,
        owner_key: str = "local:default",
    ) -> dict[str, Any]:
        """Replay queued state commands, preserving explicit conflict results."""

        results = self.offline.reconcile_expired_effect_commits(
            owner_key=owner_key,
            limit=limit,
        )
        worker_id = f"replay:{uuid.uuid4().hex}"
        for record in self.offline.claim_pending(
            limit=limit,
            owner_key=owner_key,
            worker_id=worker_id,
        ):
            request = record["request"]
            lease_id = str(record["lease_id"])
            if int(request.get("pack_generation") or -1) != self._pack_generation():
                result = {
                    "api_version": API_VERSION,
                    "status": "failed",
                    "error": {
                        "code": "PACK_GENERATION_CONFLICT",
                        "message": "queued operation generation is stale",
                    },
                }
            else:
                barrier = self.offline.begin_effect_commit(
                    record["queue_id"],
                    owner_key=owner_key,
                    lease_id=lease_id,
                )
                if barrier["status"] == "cancelled":
                    results.append(barrier["queue"])
                    continue
                result = self.invoke(
                    {
                        "command_ref": request["command_ref"],
                        "args": request["args"],
                        "expected_revision": request["expected_revision"],
                        "idempotency_key": request["idempotency_key"],
                        "invocation_id": f"offline:{record['queue_id']}",
                    },
                    {
                        "principal_id": owner_key.split(":", 1)[0],
                        "profile_id": owner_key.split(":")[1]
                        if ":" in owner_key
                        else "default",
                    },
                )
            error_code = str((result.get("error") or {}).get("code") or "")
            if result.get("status") == "cancelled":
                terminal_state = "cancelled"
            elif result.get("status") == "succeeded":
                desired_values = request.get("args") or {}
                snapshots = [
                    item
                    for item in result.get("state_changes") or []
                    if isinstance(item, dict)
                ]
                desired = next(iter(desired_values.values()), None)
                if not snapshots or snapshots[-1].get("value") != desired:
                    terminal_state = "failed"
                    result = {
                        **result,
                        "status": "failed",
                        "error": {
                            "code": "AUTHORITATIVE_STATE_MISMATCH",
                            "message": (
                                "backend state did not confirm the queued desired value"
                            ),
                        },
                    }
                else:
                    terminal_state = "completed"
            elif "CONFLICT" in error_code.upper():
                terminal_state = "conflicted"
            else:
                terminal_state = "failed"
            queue_result = self.offline.record_result(
                record["queue_id"],
                state=terminal_state,
                result=result,
                owner_key=owner_key,
                lease_id=lease_id,
            )
            results.append(queue_result)
        return {
            "api_version": API_VERSION,
            "status": "succeeded",
            "results": results,
        }

    def cancel_invocation(
        self,
        payload: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        invocation_id = str(payload.get("invocation_id") or "").strip()
        owner_key = self._owner_key(payload, context)
        stored = self.events.stored(invocation_id, owner_key=owner_key)
        if stored is None:
            return {
                "api_version": API_VERSION,
                "status": "failed",
                "error": {
                    "code": "INVOCATION_NOT_FOUND",
                    "message": "invocation was not found",
                },
            }
        if stored["state"] not in {"approval_required", "accepted", "validating"}:
            return {
                "api_version": API_VERSION,
                "status": "failed",
                "error": {
                    "code": "INVOCATION_TERMINAL",
                    "message": "invocation is no longer cancellable",
                },
            }
        result = {
            "api_version": API_VERSION,
            "operation_id": invocation_id,
            "status": "cancelled",
            "command_ref": payload.get("command_ref"),
            "state_changes": [],
        }
        try:
            self.events.settle_terminal(
                invocation_id,
                "cancelled",
                owner_key=owner_key,
                result=result,
                event_type="cancelled",
                event_payload={"reason": str(payload.get("reason") or "denied")},
                expected_states={str(stored["state"])},
            )
        except InvocationEventError:
            return self._state_conflict_result(
                invocation_id,
                owner_key,
                payload,
            )
        result["progress"] = self.events.snapshot(
            invocation_id,
            owner_key=owner_key,
        )
        return result

    def reconcile_approval(
        self,
        payload: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> None:
        invocation_id = str(payload.get("invocation_id") or "").strip()
        owner_key = self._owner_key(payload, context)
        stored = self.events.stored(invocation_id, owner_key=owner_key)
        if not stored or stored["state"] != "approval_required":
            return
        request_id = str(stored.get("approval_request_id") or "")
        if not request_id:
            return
        from domain.safety.approval import get_approval_request

        request = get_approval_request(request_id)
        status = str((request or {}).get("status") or "")
        if status not in {"denied", "expired"}:
            return
        terminal = "expired" if status == "expired" else "cancelled"
        result = {
            "api_version": API_VERSION,
            "operation_id": invocation_id,
            "status": terminal,
            "command_ref": payload.get("command_ref"),
            "state_changes": [],
        }
        try:
            self.events.settle_terminal(
                invocation_id,
                terminal,
                owner_key=owner_key,
                result=result,
                event_type=terminal,
                event_payload={"approval_request_id": request_id},
                expected_states={"approval_required"},
            )
        except InvocationEventError:
            return

    def query_datasource(self, payload: dict[str, Any]) -> dict[str, Any]:
        datasource_ref = str(payload.get("datasource_ref") or "").strip()
        datasource_aliases = {
            "tobkiri:model_catalog": "tobkiri:model_catalog",
            "tobkiri:models.resolved": "tobkiri:model_catalog",
            "tobkiri:provider_catalog": "tobkiri:provider_catalog",
            "tobkiri:providers.resolved": "tobkiri:provider_catalog",
        }
        canonical_ref = datasource_aliases.get(datasource_ref)
        if canonical_ref is None:
            return {
                "api_version": API_VERSION,
                "status": "failed",
                "error": {"code": "DATASOURCE_NOT_FOUND", "message": "datasource not found"},
                "items": [],
            }
        from ecosystem.defaultspack.backend.ai_client.provider_catalog import list_profile_catalog

        query = self._search_text(payload.get("query"))
        try:
            offset = max(0, int(str(payload.get("cursor") or "0")))
        except ValueError:
            offset = 0
        try:
            limit = max(1, min(100, int(payload.get("limit") or 25)))
        except (TypeError, ValueError):
            limit = 25
        profiles = self._cached_profile_catalog(list_profile_catalog)
        if canonical_ref == "tobkiri:provider_catalog":
            items = self._provider_options(profiles)
        else:
            items = [self._model_option(profile) for profile in profiles if isinstance(profile, dict)]
        if query:
            items = [
                item
                for item in items
                if query in self._search_text(
                    " ".join(
                        [
                            str(item.get("value") or ""),
                            str(item.get("label", {}).get("fallback") or ""),
                            str(item.get("description", {}).get("fallback") or ""),
                        ]
                    )
                )
            ]
        selected_values = {
            str(value or "").strip()
            for value in payload.get("selected_values") or []
            if str(value or "").strip()
        }
        retained = [item for item in items if str(item.get("value") or "") in selected_values]
        page = items[offset : offset + limit]
        if offset == 0 and retained:
            page_ids = {str(item.get("value") or "") for item in page}
            page = [*retained, *(item for item in page if str(item.get("value") or "") not in page_ids)]
            page = page[:limit]
        next_offset = offset + len(page)
        return {
            "api_version": API_VERSION,
            "status": "succeeded",
            "datasource_ref": canonical_ref,
            "request_id": str(payload.get("request_id") or uuid.uuid4()),
            "items": page,
            "page": {
                "has_more": next_offset < len(items),
                "next_cursor": str(next_offset) if next_offset < len(items) else None,
            },
        }

    def _resolve_command(
        self,
        command: dict[str, Any],
        diagnostics: list[dict[str, Any]],
        pack_generation: int,
    ) -> dict[str, Any]:
        command_id = str(command.get("id") or command.get("name") or "").strip()
        canonical_id = f"{PACK_ID}:{command_id}"
        execution = command.get("execution") if isinstance(command.get("execution"), dict) else {}
        execution_type = str(execution.get("type") or "frontend")
        availability: dict[str, Any] = {"status": "available"}
        if execution_type == "frontend":
            action = str(execution.get("action") or "").strip()
            if action not in LEGACY_FRONTEND_HANDLERS:
                availability = {
                    "status": "unavailable",
                    "reason_code": "handler_missing",
                    "reason": f"Frontend handler is not registered for {action or command_id}",
                }
                diagnostics.append(
                    {
                        "level": "error",
                        "code": "handler_missing",
                        "command_ref": canonical_id,
                        "message": availability["reason"],
                    }
                )
        elif execution_type not in {
            "model_command",
            "settings_patch",
            "rumi_function",
            "chat_action",
            "pack_block",
        }:
            availability = {
                "status": "unavailable",
                "reason_code": "binding_missing",
                "reason": f"Unsupported legacy execution type: {execution_type}",
            }

        resolved_execution = self._execution(command)
        authority = OPERATION_AUTHORITY.get(
            str(resolved_execution.get("operation_ref") or ""),
            {
                "permissions": [],
                "approval_policy": "never",
                "executor_policy_ref": "tobkiri.command.standard",
            },
        )
        # The five high-risk command presentations stay available.  The
        # browser routes them to the signed Host Adapter at
        # ``/api/command-protocol/v1/high-risk`` before it can request an
        # approval or execute.  Marking them unavailable here would disable
        # that sole approved path in the Composer, even though the Host
        # adapter and its one-shot interactive approval port are captured.
        return {
            "canonical_id": canonical_id,
            "pack_id": PACK_ID,
            "pack_generation": pack_generation,
            "command_version": "1.0.0",
            "identity": {
                "id": command_id,
                "name": self._slash_token(command.get("name") or command_id),
                "aliases": list(
                    dict.fromkeys(
                        token
                        for token in (
                            self._slash_token(item)
                            for item in command.get("aliases") or []
                        )
                        if token
                    )
                )[:16],
                "version": "1.0.0",
            },
            "presentation": self._presentation(command),
            "execution": resolved_execution,
            "authorization": {
                "risk": command.get("risk") or "low",
                "permissions": deepcopy(authority["permissions"]),
                "approval_required": authority["approval_policy"] == "required",
                "approval_policy": authority["approval_policy"],
                "executor_policy_ref": authority["executor_policy_ref"],
            },
            "constraints": {"modes": deepcopy(command.get("modes") or [])},
            "availability": availability,
        }

    def _pack_generation(self) -> int:
        """Return a deterministic generation for the installed Pack contents."""

        digest = hashlib.sha256()
        for relative in (
            Path("pack.v4.json"),
            Path("commands/default_commands.json"),
            Path("schemas/command-protocol-v1.schema.json"),
        ):
            path = self.pack_root / relative
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
        return max(1, int.from_bytes(digest.digest()[:4], "big"))

    def _source_commands(self, *, public: bool = False) -> list[dict[str, Any]]:
        manifest_commands = (
            self.legacy.list_commands()
            if public
            else self.legacy.registered_commands()
        )
        return [*manifest_commands, *self._registered_settings_commands()]

    def _registered_settings_commands(self) -> list[dict[str, Any]]:
        settings = FrontendSettingsStore(
            defaultspack_frontend_settings_path(self._settings_owner)
        ).read()
        commands_section = settings.get("commands") if isinstance(settings.get("commands"), dict) else {}
        records = commands_section.get("registered_slash_commands") if isinstance(commands_section, dict) else []
        if not isinstance(records, list):
            return []
        commands: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in records:
            record = raw if isinstance(raw, dict) else {"name": raw, "action": "toggle_yolo"}
            if record.get("enabled") is False:
                continue
            name = self._slash_token(record.get("name") or record.get("command") or record.get("id"))
            action = str(record.get("action") or record.get("frontend_action") or "").strip()
            if not name or name in seen or action not in LEGACY_FRONTEND_HANDLERS:
                continue
            seen.add(name)
            args = self._registered_action_args(action)
            raw_aliases = record.get("aliases")
            aliases = raw_aliases.split(",") if isinstance(raw_aliases, str) else raw_aliases
            if not isinstance(aliases, list):
                aliases = []
            commands.append(
                {
                    "id": f"user_{name}",
                    "name": name,
                    "aliases": [
                        token
                        for token in dict.fromkeys(
                            self._slash_token(item)
                            for item in aliases
                        )
                        if token and token != name
                    ][:8],
                    "label": str(record.get("label") or name),
                    "description": str(record.get("description") or f"Run {action}."),
                    "category": self._registered_action_category(action),
                    "visibility": "default",
                    "risk": "medium" if action in {"toggle_yolo", "toggle_ultra_yolo"} else "low",
                    "modes": ["chat", "coding", "agent"],
                    "args": args,
                    "execution": {"type": "frontend", "action": action},
                    "source": "settings.registered_slash_commands",
                }
            )
        return commands

    @staticmethod
    def _registered_action_args(action: str) -> list[dict[str, Any]]:
        if action in {"toggle_yolo", "toggle_ultra_yolo", "set_fast_mode"}:
            return [{"name": "enabled", "type": "boolean", "required": False}]
        if action in {"open_model_picker", "open_tool_picker"}:
            return [{"name": "query", "type": "string", "required": False}]
        if action == "open_settings":
            return [{"name": "section", "type": "string", "required": False}]
        if action == "set_price_mode":
            return [{"name": "tier", "type": "enum", "required": False, "values": ["low", "high"]}]
        return []

    @staticmethod
    def _registered_action_category(action: str) -> str:
        if "model" in action or action in {"set_fast_mode", "set_price_mode"}:
            return "model"
        if "tool" in action:
            return "tools"
        if "settings" in action or action in {"open_permissions", "open_theme_settings", "open_keymap_settings"}:
            return "settings"
        if "mode" in action or "yolo" in action:
            return "mode"
        return "chat"

    def _presentation(self, command: dict[str, Any]) -> dict[str, Any]:
        args = command.get("args") if isinstance(command.get("args"), list) else []
        execution = command.get("execution") if isinstance(command.get("execution"), dict) else {}
        execution_type = str(execution.get("type") or "frontend")
        command_id = str(command.get("id") or "")
        frontend_action = str(execution.get("action") or "")
        qualified_name = str(execution.get("qualified_name") or "")
        if execution_type == "model_command":
            input_contract: dict[str, Any] = {
                "kind": "search_select",
                "argument": "query",
                "selection": "single",
                "datasource_ref": "tobkiri:model_catalog",
                "search": {"enabled": True, "min_chars": 0, "debounce_ms": 150},
                "keyboard": {"commit_keys": ["Enter", "Tab"]},
            }
        elif qualified_name == "defaultspack:ai.provider_command":
            input_contract = {
                "kind": "search_select",
                "argument": "target",
                "selection": "single",
                "datasource_ref": "tobkiri:provider_catalog",
                "search": {"enabled": True, "min_chars": 0, "debounce_ms": 150},
                "keyboard": {"commit_keys": ["Enter", "Tab"]},
            }
        elif frontend_action in LEGACY_HOST_STATE_REFS:
            input_contract = {
                "kind": "toggle",
                "argument": "enabled",
                "state_ref": LEGACY_HOST_STATE_REFS[frontend_action],
                "bare_behavior": "toggle",
                "show_current_state": True,
            }
        elif command_id == "deepthink" or execution_type == "settings_patch":
            section = str(execution.get("section") or "models")
            field = str(execution.get("field") or "deepthink_enabled")
            input_contract = {
                "kind": "toggle",
                "argument": "enabled",
                "state_ref": f"defaultspack:{section}.{field}",
                "bare_behavior": "toggle",
                "show_current_state": True,
            }
        elif len(args) == 1 and args[0].get("type") == "enum":
            input_contract = {
                "kind": "select",
                "argument": args[0].get("name"),
                "selection": "single",
                "options": [
                    {"value": value, "label": {"fallback": str(value)}}
                    for value in args[0].get("values", [])
                ],
            }
        elif args:
            input_contract = {
                "kind": "form",
                "fields": [
                    self._form_field(item)
                    for item in args
                    if isinstance(item, dict)
                ],
            }
        else:
            input_contract = {"kind": "action", "run_on_bare": True}

        mounts = [
            {
                "slot_ref": "tobkiri:command_palette.commands",
                "display": "command",
                "order": 100,
            }
        ]
        if command_id == "deepthink":
            mounts.insert(
                0,
                {
                    "slot_ref": "tobkiri:composer.toolbar.leading",
                    "display": "persistent",
                    "order": 20,
                },
            )
        return {
            "label": {"fallback": str(command.get("label") or command_id)},
            "description": {"fallback": str(command.get("description") or "")},
            "category": command.get("category") or "other",
            "visibility": command.get("visibility") or "default",
            "icon": self._icon_token(command, input_contract),
            "input": input_contract,
            "mounts": mounts,
        }

    @staticmethod
    def _form_field(item: dict[str, Any]) -> dict[str, Any]:
        field = {
            "argument": item.get("name"),
            "control": "checkbox" if item.get("type") == "boolean" else "text",
            "required": bool(item.get("required")),
        }
        label = str(item.get("label") or "").strip()
        placeholder = str(item.get("placeholder") or "").strip()
        if label:
            field["label"] = {"fallback": label}
        if placeholder:
            field["placeholder"] = {"fallback": placeholder}
        return field

    @staticmethod
    def _execution(command: dict[str, Any]) -> dict[str, Any]:
        execution = command.get("execution") if isinstance(command.get("execution"), dict) else {}
        execution_type = str(execution.get("type") or "frontend")
        if execution_type == "frontend":
            action = str(execution.get("action") or command.get("id") or "")
            state_ref = LEGACY_HOST_STATE_REFS.get(action)
            if state_ref:
                return {
                    "kind": "state_mutation",
                    "state_ref": state_ref,
                    "mutation": {"argument": "enabled", "when_present": "set"},
                }
            return {
                "kind": "host_operation",
                "operation_ref": f"host:{action}",
            }
        if execution_type == "model_command":
            return {
                "kind": "state_mutation",
                "state_ref": "tobkiri:active_model",
                "mutation": {"argument": "query", "when_present": "set"},
            }
        if execution_type == "settings_patch":
            return {
                "kind": "state_mutation",
                "state_ref": (
                    f"defaultspack:{execution.get('section')}.{execution.get('field')}"
                ),
                "mutation": {"argument": "enabled", "when_present": "set"},
                "offline": {
                    "queueable": True,
                    "semantics": "set",
                    "backend_authoritative": True,
                },
            }
        qualified = str(
            execution.get("qualified_name")
            or execution.get("action")
            or command.get("id")
            or ""
        )
        if command.get("id") == "deepthink":
            return {
                "kind": "state_mutation",
                "state_ref": "defaultspack:models.deepthink_enabled",
                "mutation": {"argument": "enabled", "when_present": "set"},
                "offline": {
                    "queueable": True,
                    "semantics": "set",
                    "backend_authoritative": True,
                },
            }
        return {
            "kind": "pack_operation",
            "operation_ref": qualified,
        }

    @staticmethod
    def _identity_collisions(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
        claims: dict[str, list[str]] = {}
        for command in commands:
            canonical = f"{PACK_ID}:{command.get('id')}"
            for claim in [command.get("name"), *(command.get("aliases") or [])]:
                token = CommandProtocolRegistry._slash_token(claim)
                if token:
                    claims.setdefault(token, []).append(canonical)
        return [
            {
                "level": "error",
                "code": "identity_collision",
                "claim": claim,
                "commands": refs,
                "message": f"Short command claim '{claim}' is ambiguous; use canonical invocation",
            }
            for claim, refs in claims.items()
            if len(set(refs)) > 1
        ]

    @staticmethod
    def _search_text(value: Any) -> str:
        return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()

    @staticmethod
    def _slash_token(value: Any) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
        normalized = re.sub(r"[\s-]+", "_", normalized)
        normalized = re.sub(r"[^a-z0-9._-]", "", normalized)
        normalized = re.sub(r"_+", "_", normalized).strip("_.-")
        return normalized[:128]

    @staticmethod
    def _model_option(profile: dict[str, Any]) -> dict[str, Any]:
        profile_id = str(
            profile.get("profile_id")
            or profile.get("qualified_model_id")
            or profile.get("id")
            or ""
        )
        provider_id = str(profile.get("provider_id") or profile.get("provider") or "")
        label = str(profile.get("display_name") or profile.get("name") or profile_id)
        availability = profile.get("availability") if isinstance(profile.get("availability"), dict) else {}
        configured = bool(availability.get("configured") or availability.get("active"))
        available = availability.get("available") is not False
        disabled_reason = None
        if not configured:
            disabled_reason = {"fallback": "Provider credential is not configured"}
        elif not available:
            disabled_reason = {"fallback": "Model is currently unavailable"}
        return {
            "id": profile_id,
            "value": profile_id,
            "label": {"fallback": label},
            "description": {"fallback": f"{provider_id} · {profile.get('model_id') or profile_id}"},
            "icon": "model",
            "badges": [
                {"label": "Configured", "tone": "success"}
            ] if configured else [],
            "disabled": bool(disabled_reason),
            "disabled_reason": disabled_reason,
            "metadata": {
                "provider_id": provider_id,
                "configured": configured,
                "available": available,
                "capability_tags": deepcopy(profile.get("capability_tags") or []),
            },
        }

    @classmethod
    def _cached_profile_catalog(cls, loader: Any) -> list[dict[str, Any]]:
        now = time.monotonic()
        with cls._datasource_cache_lock:
            cached = cls._datasource_cache.get("profiles")
            if cached and now - cached[0] < cls._datasource_cache_ttl_seconds:
                return deepcopy(cached[1])
        profiles = [item for item in loader() if isinstance(item, dict)]
        with cls._datasource_cache_lock:
            cls._datasource_cache["profiles"] = (now, deepcopy(profiles))
        return profiles

    @classmethod
    def invalidate_datasource_cache(cls) -> None:
        with cls._datasource_cache_lock:
            cls._datasource_cache.clear()

    @classmethod
    def _provider_options(cls, profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        providers: dict[str, dict[str, Any]] = {}
        for profile in profiles:
            provider_id = str(profile.get("provider_id") or profile.get("provider") or "").strip()
            if not provider_id:
                continue
            availability = profile.get("availability") if isinstance(profile.get("availability"), dict) else {}
            current = providers.setdefault(
                provider_id,
                {
                    "id": provider_id,
                    "value": provider_id,
                    "label": {"fallback": str(profile.get("provider_display_name") or provider_id)},
                    "description": {"fallback": "0 models"},
                    "icon": "provider",
                    "badges": [],
                    "disabled": True,
                    "disabled_reason": {"fallback": "No selectable models are currently available"},
                    "metadata": {
                        "provider_id": provider_id,
                        "configured": False,
                        "available": False,
                        "model_count": 0,
                    },
                },
            )
            metadata = current["metadata"]
            metadata["model_count"] = int(metadata["model_count"]) + 1
            metadata["configured"] = bool(metadata["configured"] or availability.get("configured") or availability.get("active"))
            metadata["available"] = bool(metadata["available"] or availability.get("available") is not False)
        for item in providers.values():
            metadata = item["metadata"]
            item["description"] = {"fallback": f"{metadata['model_count']} models"}
            item["disabled"] = not bool(metadata["available"])
            item["disabled_reason"] = (
                {"fallback": "No selectable models are currently available"}
                if item["disabled"]
                else None
            )
            if metadata["configured"]:
                item["badges"] = [{"label": "Configured", "tone": "success"}]
        return sorted(providers.values(), key=lambda item: str(item["label"]["fallback"]).casefold())

    @staticmethod
    def _icon_token(command: dict[str, Any], input_contract: dict[str, Any]) -> str:
        command_token = CommandProtocolRegistry._slash_token(command.get("id") or command.get("name"))
        if command_token:
            return command_token
        kind = str(input_contract.get("kind") or "action")
        category = str(command.get("category") or "other")
        if kind == "toggle":
            return "toggle"
        if kind in {"select", "search_select"}:
            return "search" if kind == "search_select" else "list"
        return {
            "chat": "message-square",
            "model": "cpu",
            "mode": "sliders-horizontal",
            "coding": "code-2",
            "tools": "wrench",
            "settings": "settings",
            "debug": "bug",
        }.get(category, "sparkles")
