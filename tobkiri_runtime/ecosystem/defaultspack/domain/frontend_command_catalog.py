"""Pure Command Protocol presentation shared by canonical and legacy readers."""

from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from typing import Any

PACK_ID = "defaultspack"

LEGACY_HOST_STATE_REFS = {
    "toggle_yolo": "host:approval.full_access",
    "toggle_ultra_yolo": "host:approval.full_access",
    "set_fast_mode": "host:model.fast_mode",
}

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


class CommandCatalogProjection:
    """Project command metadata without loading execution or user-state services."""

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
                            self._slash_token(item) for item in command.get("aliases") or []
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
                "fields": [self._form_field(item) for item in args if isinstance(item, dict)],
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
                "state_ref": (f"defaultspack:{execution.get('section')}.{execution.get('field')}"),
                "mutation": {"argument": "enabled", "when_present": "set"},
                "offline": {
                    "queueable": True,
                    "semantics": "set",
                    "backend_authoritative": True,
                },
            }
        qualified = str(
            execution.get("qualified_name") or execution.get("action") or command.get("id") or ""
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
                token = CommandCatalogProjection._slash_token(claim)
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
    def _slash_token(value: Any) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
        normalized = re.sub(r"[\s-]+", "_", normalized)
        normalized = re.sub(r"[^a-z0-9._-]", "", normalized)
        normalized = re.sub(r"_+", "_", normalized).strip("_.-")
        return normalized[:128]

    @staticmethod
    def _icon_token(command: dict[str, Any], input_contract: dict[str, Any]) -> str:
        command_token = CommandCatalogProjection._slash_token(
            command.get("id") or command.get("name")
        )
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
