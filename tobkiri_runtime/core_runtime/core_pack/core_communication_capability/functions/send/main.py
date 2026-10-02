"""
pack.inbox.send - Built-in Capability Handler

Pack A -> Pack B の特定 component 宛てに JSON Patch / 置換を送る。
受信側 addon_policy を検証し、正規化イベントとして inbox に保存する。

安全要件:
- addon_policy なし -> 全拒否 (fail-closed)
- deny_all=true -> 全拒否
- JSON Patch の move / copy 禁止
- file path は editable_files.path_glob マッチ必須
- file_replace_json はデフォルト拒否

検証順序 (使用回数を無駄に消費しない):
  入力バリデーション -> Grant config -> ポリシー検証
  -> Patch検証 -> 使用回数消費 -> 保存
"""

from __future__ import annotations

import fnmatch
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Protocol, runtime_checkable

_SAFE_ID_RE = re.compile("^[a-zA-Z0-9_.-]+$")


class _ReceiverComponent(Protocol):
    manifest: Dict[str, Any]


@runtime_checkable
class _ReceiverRegistry(Protocol):
    def get_pack(self, pack_id: str) -> object | None: ...

    def get_component(
        self, pack_id: str, component_type: str, component_id: str
    ) -> _ReceiverComponent | None: ...


def execute(context: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    principal_id = context.get("principal_id", "")
    grant_config = context.get("grant_config", {})
    request_id_ctx = context.get("request_id", "")

    to_pack_id = args.get("to_pack_id", "")
    target_component = args.get("target_component", {})
    payload = args.get("payload", {})
    priority = args.get("priority", 100)
    request_id = args.get("request_id", request_id_ctx or uuid.uuid4().hex[:12])
    notes = args.get("notes", "")

    if not to_pack_id or not isinstance(to_pack_id, str):
        return _error("Missing or invalid to_pack_id", "validation_error")

    comp_type = target_component.get("type", "")
    comp_id = target_component.get("id", "")
    if not comp_type or not comp_id:
        return _error("Missing target_component type or id", "validation_error")

    kind = payload.get("kind", "")
    valid_kinds = {"manifest_json_patch", "file_json_patch", "file_replace_json"}
    if kind not in valid_kinds:
        return _error(
            "Invalid payload.kind: must be one of " + str(valid_kinds),
            "validation_error",
        )

    if kind in ("file_json_patch", "file_replace_json") and not payload.get("file"):
        return _error("payload.file is required for kind=" + kind, "validation_error")
    if kind in ("manifest_json_patch", "file_json_patch") and not payload.get("patch"):
        return _error("payload.patch is required for kind=" + kind, "validation_error")
    if kind == "file_replace_json" and payload.get("json") is None:
        return _error(
            "payload.json is required for kind=file_replace_json", "validation_error"
        )
    if not _safe_pack_id(to_pack_id):
        return _error("Invalid to_pack_id (path traversal)", "validation_error")

    allowed_targets = grant_config.get("allowed_target_packs", [])
    if allowed_targets and to_pack_id not in allowed_targets:
        return _error("Target pack not in allowed_target_packs", "grant_denied")

    default_kinds = ["manifest_json_patch", "file_json_patch"]
    allowed_kinds = grant_config.get("allowed_kinds", default_kinds)
    if kind not in allowed_kinds:
        return _error("Kind '" + kind + "' not in allowed_kinds", "grant_denied")

    max_payload_bytes = grant_config.get("max_payload_bytes", 1048576)
    payload_size = len(
        json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    )
    if payload_size > max_payload_bytes:
        return _error(
            "Payload too large: " + str(payload_size) + " > " + str(max_payload_bytes),
            "payload_too_large",
        )

    policy_result = _check_receiver_policy(to_pack_id, comp_type, comp_id, payload)
    if not policy_result["allowed"]:
        return _error(policy_result["reason"], "policy_denied")
    rejected_ops = policy_result.get("rejected_ops", 0)

    if kind in ("manifest_json_patch", "file_json_patch"):
        for op in payload.get("patch", []):
            if op.get("op") in ("move", "copy"):
                return _error(
                    "Forbidden patch operation: " + str(op.get("op")),
                    "forbidden_operation",
                )

    scope_level = grant_config.get("send_scope_level", 1)
    scope_key = _build_scope_key(
        to_pack_id, comp_type, comp_id, payload.get("file"), scope_level
    )
    max_sends = grant_config.get("max_sends_per_scope", 0)
    max_daily = grant_config.get("max_daily_sends_per_scope", 0)
    expires_at = grant_config.get("expires_at_epoch", 0)

    consumption = None
    if max_sends > 0 or max_daily > 0 or expires_at > 0:
        try:
            from core_runtime.capability_usage_store import get_capability_usage_store

            store = get_capability_usage_store()
            result = store.check_and_consume(
                principal_id=principal_id,
                permission_id="pack.inbox.send",
                scope_key=scope_key,
                max_count=max_sends,
                max_daily_count=max_daily,
                expires_at_epoch=expires_at,
            )
            if not result.allowed:
                return _error(
                    "Usage limit: " + str(result.reason),
                    "usage_limit_exceeded",
                    consumed={
                        "scope_key": scope_key,
                        "used": result.used_count,
                        "max": result.max_count,
                    },
                )
            consumption = {
                "scope_key": scope_key,
                "used": result.used_count,
                "max": result.max_count,
            }
        except Exception as e:
            return _error("Usage store error: " + str(e), "internal_error")

    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    ts_safe = ts.replace(":", "-").replace(".", "-")
    comp_full_id = to_pack_id + ":" + comp_type + ":" + comp_id

    inbox_event = {
        "schema": "inbox_request.v1",
        "ts": ts,
        "from_pack_id": principal_id,
        "to_pack_id": to_pack_id,
        "target_component": {"type": comp_type, "id": comp_id},
        "payload": payload,
        "priority": priority,
        "request_id": request_id,
        "notes": notes,
    }

    safe_to = _sanitize_path_segment(to_pack_id)
    safe_comp = _sanitize_path_segment(comp_full_id)
    safe_from = _sanitize_path_segment(principal_id)

    inbox_dir = (
        Path("user_data") / "packs" / safe_to / "inbox" / "v1"
        / "components" / safe_comp / "from" / safe_from
    )
    inbox_dir.mkdir(parents=True, exist_ok=True)

    filename = ts_safe + "__" + (request_id or uuid.uuid4().hex[:8]) + ".json"
    file_path = inbox_dir / filename

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(inbox_event, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return _error("Failed to write inbox event: " + str(e), "write_error")

    _audit_inbox_send(
        principal_id=principal_id,
        to_pack_id=to_pack_id,
        comp_full_id=comp_full_id,
        kind=kind,
        file=payload.get("file"),
        patch_op_count=len(payload.get("patch", [])),
        payload_bytes=payload_size,
        consumption=consumption,
        success=True,
        stored_path=str(file_path),
    )

    return {
        "success": True,
        "stored_path": str(file_path),
        "consumed": consumption,
        "rejected_ops": rejected_ops,
    }


def _error(message: str, error_type: str, **extra) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "success": False,
        "error": message,
        "error_type": error_type,
    }
    result.update(extra)
    return result


def _safe_pack_id(pack_id: str) -> bool:
    if ".." in pack_id:
        return False
    if "/" in pack_id:
        return False
    if pack_id.startswith("."):
        return False
    return bool(_SAFE_ID_RE.match(pack_id))


def _sanitize_path_segment(value: str) -> str:
    return re.sub("[^a-zA-Z0-9_.:-]", "_", value).strip("_")


def _build_scope_key(
    to_pack_id: str,
    comp_type: str,
    comp_id: str,
    file_path: Optional[str],
    scope_level: int,
) -> str:
    if scope_level <= 1:
        return to_pack_id
    if scope_level == 2:
        return to_pack_id + ":" + comp_type + ":" + comp_id
    if scope_level >= 3 and file_path:
        return to_pack_id + ":" + comp_type + ":" + comp_id + ":" + file_path
    return to_pack_id + ":" + comp_type + ":" + comp_id


def _check_receiver_policy(
    to_pack_id: str,
    comp_type: str,
    comp_id: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    kind = payload.get("kind", "")
    try:
        from backend_core.ecosystem.registry import get_registry
        registry = get_registry()
    except Exception:
        return {"allowed": False, "reason": "Cannot access component registry"}

    if not isinstance(registry, _ReceiverRegistry):
        return {"allowed": False, "reason": "Component registry is incompatible"}

    pack = registry.get_pack(to_pack_id)
    if pack is None:
        return {"allowed": False, "reason": "Pack not found: " + to_pack_id}

    component = registry.get_component(to_pack_id, comp_type, comp_id)
    if component is None:
        return {
            "allowed": False,
            "reason": "Component not found: " + to_pack_id + ":" + comp_type + ":" + comp_id,
        }

    addon_policy = component.manifest.get("addon_policy")
    if addon_policy is None:
        return {"allowed": False, "reason": "No addon_policy defined (deny by default)"}
    if addon_policy.get("deny_all", False):
        return {"allowed": False, "reason": "addon_policy.deny_all is true"}

    if kind == "manifest_json_patch":
        return _check_manifest_patch_policy(addon_policy, payload)
    if kind == "file_json_patch":
        return _check_file_patch_policy(addon_policy, payload)
    if kind == "file_replace_json":
        return _check_file_replace_policy(addon_policy, payload)
    return {"allowed": False, "reason": "Unknown kind: " + kind}


def _check_manifest_patch_policy(policy, payload):
    allowed_paths = policy.get("allowed_manifest_paths", [])
    patch_ops = payload.get("patch", [])
    if not allowed_paths:
        return {"allowed": False, "reason": "No allowed_manifest_paths defined"}
    rejected = sum(
        1 for op in patch_ops
        if not any(op.get("path", "").startswith(ap) for ap in allowed_paths)
    )
    if rejected == len(patch_ops):
        return {
            "allowed": False,
            "reason": "All patch ops rejected by allowed_manifest_paths",
            "rejected_ops": rejected,
        }
    return {"allowed": True, "reason": "ok", "rejected_ops": rejected}


def _check_file_patch_policy(policy, payload):
    file_path = payload.get("file", "")
    editable_files = policy.get("editable_files", [])
    patch_ops = payload.get("patch", [])
    if not editable_files:
        return {"allowed": False, "reason": "No editable_files defined"}

    allowed_prefixes = None
    for rule in editable_files:
        if fnmatch.fnmatch(file_path, rule.get("path_glob", "")):
            allowed_prefixes = rule.get("allowed_json_pointer_prefixes", [])
            break

    if allowed_prefixes is None:
        return {
            "allowed": False,
            "reason": "File '" + file_path + "' not in editable_files",
        }
    if allowed_prefixes:
        rejected = sum(
            1 for op in patch_ops
            if not any(op.get("path", "").startswith(ap) for ap in allowed_prefixes)
        )
        if rejected == len(patch_ops):
            return {
                "allowed": False,
                "reason": "All ops rejected by allowed_json_pointer_prefixes",
                "rejected_ops": rejected,
            }
        return {"allowed": True, "reason": "ok", "rejected_ops": rejected}
    return {"allowed": True, "reason": "ok", "rejected_ops": 0}


def _check_file_replace_policy(policy, payload):
    file_path = payload.get("file", "")
    editable_files = policy.get("editable_files", [])
    if not editable_files:
        return {"allowed": False, "reason": "No editable_files defined"}
    for rule in editable_files:
        if fnmatch.fnmatch(file_path, rule.get("path_glob", "")):
            prefixes = rule.get("allowed_json_pointer_prefixes", [])
            if not prefixes:
                return {"allowed": True, "reason": "ok", "rejected_ops": 0}
            return {
                "allowed": False,
                "reason": "file_replace_json requires empty allowed_json_pointer_prefixes",
            }
    return {
        "allowed": False,
        "reason": "File '" + file_path + "' not in editable_files",
    }


def _audit_inbox_send(**kwargs: Any) -> None:
    try:
        from core_runtime.audit_logger import get_audit_logger
        audit = get_audit_logger()
        audit.log_permission_event(
            pack_id=kwargs.get("principal_id", ""),
            permission_type="capability",
            action="inbox_send",
            success=kwargs.get("success", False),
            details={
                "to_pack_id": kwargs.get("to_pack_id"),
                "target_component": kwargs.get("comp_full_id"),
                "kind": kwargs.get("kind"),
                "file": kwargs.get("file"),
                "patch_op_count": kwargs.get("patch_op_count", 0),
                "payload_bytes": kwargs.get("payload_bytes", 0),
                "consumption": kwargs.get("consumption"),
                "stored_path": kwargs.get("stored_path"),
            },
        )
    except Exception:
        pass
