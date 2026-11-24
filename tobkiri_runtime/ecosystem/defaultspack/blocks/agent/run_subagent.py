

from tobkiri_protocol.settings_state import SettingsOwnerPort

from blocks._common import ok, error
from domain.agent.subagent_orchestrator import run_subagent_compat


def run(input_data, context, *, settings_owner: SettingsOwnerPort | None = None):
    data = input_data if isinstance(input_data, dict) else {}
    role_id = str(data.get("role_id") or data.get("role") or "").strip()
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
    runtime_context = _runtime_context_for_subagent(data, payload, context)
    if settings_owner is None:
        settings_owner = runtime_context.get("_settings_owner_port")
    if payload is not data and "timeout_seconds" in data and "timeout_seconds" not in payload:
        payload = dict(payload)
        payload["timeout_seconds"] = data.get("timeout_seconds")
    if not role_id and isinstance(payload, dict) and any(payload.get(key) for key in ("task", "prompt")):
        role_id = "delegate"
    if not role_id:
        return error("role_id is required", "MISSING_PARAM")
    try:
        return ok(
            run_subagent_compat(
                role_id,
                payload,
                model=str(data.get("model") or ""),
                settings=data.get("settings") if isinstance(data.get("settings"), dict) else {},
                call_handler=runtime_context.get("call_handler"),
                context=runtime_context,
                **({"settings_owner": settings_owner} if settings_owner is not None else {}),
            )
        )
    except ValueError as exc:
        return error(str(exc), "INVALID_SUBAGENT_ROLE")


def _runtime_context_for_subagent(data, payload, context):
    runtime_context = dict(context or {}) if isinstance(context, dict) else {}
    profile_id = str(runtime_context.get("profile_id") or "").strip()
    if profile_id:
        principal = str(runtime_context.get("authority_principal_id") or "").strip() or "profile:" + profile_id
        runtime_context.setdefault("authority_principal_id", principal)
        runtime_context.setdefault("principal_id", principal)
    return runtime_context
