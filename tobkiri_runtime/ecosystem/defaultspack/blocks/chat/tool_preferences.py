

from blocks._common import ok, error
from domain.chat.store import ChatStore
from domain.chat.tool_selection_schema import (
    TOOL_SELECTION_MODES,
    TOOL_SELECTION_SCOPES,
    TOOL_SELECTION_STRATEGIES,
    normalize_tool_target,
    normalize_tool_targets,
)


PREFERENCE_KEY = "tool_preferences"
PREFERENCE_OWNER_KEY = "tool_preferences_owner_profile_id"
_ALLOWED_KEYS = {"mode", "include", "exclude", "scope", "strategy", "must_use", "review", "preview_id"}
_MAX_TARGETS = 64
_MAX_TARGET_ID_LENGTH = 160


def run(input_data, context):
    method = str((input_data or {}).get("_method") or "GET").upper()
    return run_put(input_data, context) if method == "PUT" else run_get(input_data, context)


def run_get(input_data, context):
    conversation_id = str((input_data or {}).get("conversation_id") or "").strip()
    if not conversation_id:
        return error("conversation_id is required", "INVALID_INPUT")
    conversation = ChatStore().get_conversation(conversation_id)
    if conversation is None:
        return error("Conversation not found", "NOT_FOUND")
    access_error = _conversation_access_error(conversation, context)
    if access_error:
        return access_error
    metadata = conversation.get("metadata") if isinstance(conversation.get("metadata"), dict) else {}
    preferences = metadata.get(PREFERENCE_KEY) if isinstance(metadata.get(PREFERENCE_KEY), dict) else {}
    return ok({"conversation_id": conversation_id, "preferences": preferences})


def run_put(input_data, context):
    conversation_id = str((input_data or {}).get("conversation_id") or "").strip()
    if not conversation_id:
        return error("conversation_id is required", "INVALID_INPUT")
    preferences = input_data.get("preferences")
    if not isinstance(preferences, dict):
        preferences = {
            "mode": input_data.get("mode"),
            "include": input_data.get("include", []),
            "exclude": input_data.get("exclude", []),
        }
    store = ChatStore()
    conversation = store.get_conversation(conversation_id)
    if conversation is None:
        return error("Conversation not found", "NOT_FOUND")
    access_error = _conversation_access_error(conversation, context)
    if access_error:
        return access_error
    metadata = conversation.get("metadata") if isinstance(conversation.get("metadata"), dict) else {}
    try:
        sanitized = _sanitize_preferences(preferences)
    except ValueError as exc:
        return error(str(exc), "INVALID_INPUT")
    actor_profile_id = _context_profile_id(context)
    updated = {**metadata, PREFERENCE_KEY: sanitized}
    if actor_profile_id and not str(updated.get(PREFERENCE_OWNER_KEY) or "").strip():
        updated[PREFERENCE_OWNER_KEY] = actor_profile_id
    saved = store.update_conversation(conversation_id, {"metadata": updated})
    if saved is None:
        return error("Conversation not found", "NOT_FOUND")
    return ok({"conversation_id": conversation_id, "preferences": updated[PREFERENCE_KEY]})


def _sanitize_preferences(value):
    if not isinstance(value, dict):
        raise ValueError("preferences must be an object")
    unknown_keys = set(value) - _ALLOWED_KEYS
    if unknown_keys:
        raise ValueError("preferences contains unsupported keys")
    mode = str(value.get("mode") or "auto").strip().lower()
    if mode not in TOOL_SELECTION_MODES:
        raise ValueError("preferences.mode must be one of auto, review, manual, none")
    scope = str(value.get("scope") or "conversation").strip().lower()
    if scope not in TOOL_SELECTION_SCOPES:
        raise ValueError("preferences.scope must be one of turn, conversation")
    strategy = str(value.get("strategy") or "").strip().lower()
    if strategy and strategy not in TOOL_SELECTION_STRATEGIES:
        raise ValueError("preferences.strategy is not supported")
    include = _sanitize_targets(value.get("include"))
    exclude = _sanitize_targets(value.get("exclude"))
    preview_id = str(value.get("preview_id") or "").strip()
    if len(preview_id) > 128:
        raise ValueError("preferences.preview_id is too long")
    return {
        "mode": mode,
        "include": include,
        "exclude": exclude,
        "scope": scope,
        "strategy": strategy or None,
        "must_use": _coerce_bool(value.get("must_use"), default=False),
        "review": _coerce_bool(value.get("review"), default=mode == "review"),
        "preview_id": preview_id or None,
    }


def _sanitize_targets(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("preferences include/exclude must be arrays")
    if len(value) > _MAX_TARGETS:
        raise ValueError("preferences include/exclude is too large")
    for item in value:
        if isinstance(item, str) and item.strip():
            continue
        if isinstance(item, dict):
            target = normalize_tool_target(item)
            allowed_keys = {"kind", "id", "tool_id", "service_id"}
            if target is not None and set(item).issubset(allowed_keys):
                continue
        raise ValueError("preferences include/exclude must contain only string IDs or {kind, id} targets")
    targets = normalize_tool_targets(value)
    result = []
    for target in targets[:_MAX_TARGETS]:
        if not target.id or len(target.id) > _MAX_TARGET_ID_LENGTH:
            raise ValueError("preferences target id is invalid")
        result.append(target.to_dict())
    return result


def _conversation_access_error(conversation, context):
    owner_profile_id = _conversation_owner_profile_id(conversation)
    actor_profile_id = _context_profile_id(context)
    if owner_profile_id and (not actor_profile_id or owner_profile_id != actor_profile_id):
        return error("Conversation preferences are not available to this profile", "FORBIDDEN")
    return None


def _conversation_owner_profile_id(conversation):
    if not isinstance(conversation, dict):
        return ""
    metadata = conversation.get("metadata") if isinstance(conversation.get("metadata"), dict) else {}
    for source in (conversation, metadata):
        for key in ("owner_profile_id", "profile_id", PREFERENCE_OWNER_KEY):
            candidate = str(source.get(key) or "").strip()
            if candidate:
                return candidate
    return ""


def _context_profile_id(context):
    if not isinstance(context, dict):
        return ""
    principal = context.get("_authenticated_principal")
    if isinstance(principal, dict):
        candidate = str(principal.get("profile_id") or "").strip()
        if candidate:
            return candidate
    subject = context.get("_authority_subject")
    if isinstance(subject, dict):
        candidate = str(subject.get("profile_id") or "").strip()
        if candidate:
            return candidate
    for key in ("profile_id", "input_profile_id", "active_profile_id"):
        candidate = str(context.get(key) or "").strip()
        if candidate:
            return candidate
    return ""


def _coerce_bool(value, *, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if value is None:
        return default
    return bool(value)
