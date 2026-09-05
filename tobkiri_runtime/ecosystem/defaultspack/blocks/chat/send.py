import sys
import os
import base64
import json
import re
import time
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from blocks._common import ok, error, gen_id, timestamp

from domain.ai_client.gateway import AIClient, LLMGateway
from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService
from domain.chat.store import ChatStore
from domain.chat.message_converter import convert_to_standard
from domain.chat.message_builder import build_assistant_message
from domain.dev.inspector import Inspector
from domain.frontend_settings import frontend_settings_path
from domain.prompt.manager import get_manager
from blocks.chat._context_helpers import extract_user_text, enrich_messages
from domain.tool.registry import ToolRegistry
from domain.tool.schema_adapter import (
    adapt_tool_definitions,
    build_tool_execution_context,
    connected_tool_names,
    filter_tool_definitions_for_runtime_profile,
    max_tool_calls,
    resolve_runtime_profile_context,
    tool_name_from_definition,
)
from domain.chat.loop_guard import (
    LoopGuard,
    build_loop_observation,
    emergency_budget_from_context,
    explicit_param_max_tool_calls,
    loop_guard_config_from_context,
)


MAX_ATTACHMENT_TEXT_CHARS = 240_000
MAX_ATTACHMENT_TEXT_CHARS_PER_FILE = 120_000
MAX_ATTACHMENT_IMAGE_BYTES = 8 * 1024 * 1024
_DATA_IMAGE_PREFIX = "data:image/"
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|bearer|credential|password|secret|token)",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(r"\b(AIza[0-9A-Za-z_-]{20,}|sk-[0-9A-Za-z_-]{20,}|gh[pousr]_[0-9A-Za-z_]{20,})\b")
_JWT_VALUE_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*\b"
)
_AUTH_SCHEME_VALUE_RE = re.compile(
    r"(?i)\b(?P<scheme>bearer|basic|token)(?P<separator>\s+)"
    r"(?P<value>[A-Za-z0-9._~+/=-]{8,})"
)
_AUTH_PROSE_STOPWORDS = {
    "authentication",
    "authorization",
    "capacity",
    "credential",
    "credentials",
    "information",
    "interoperability",
    "responsibilities",
    "scheme",
}
_SENSITIVE_ERROR_KEY_PATTERN = (
    r"(?:api[_-]?key|x-api[_-]?key|authorization|proxy-authorization|bearer|"
    r"credential|password|secret|access[_-]?token|refresh[_-]?token|token)"
)
_SENSITIVE_QUOTED_ASSIGNMENT_RE = re.compile(
    rf"(?i)(?P<prefix>[\"']?\b{_SENSITIVE_ERROR_KEY_PATTERN}\b[\"']?\s*[:=]\s*)"
    rf"(?P<quote>[\"'])(?P<value>(?:\\.|(?!(?P=quote)).)*)(?P=quote)"
)
_AUTHORIZATION_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<prefix>[\"']?\b(?:proxy-)?authorization\b[\"']?\s*[:=]\s*)"
    r"(?![\"'])(?P<value>(?:[A-Za-z][A-Za-z0-9._~-]*\s+)?[^\s,;}]+)"
)
_SENSITIVE_UNQUOTED_ASSIGNMENT_RE = re.compile(
    rf"(?i)(?P<prefix>[\"']?\b{_SENSITIVE_ERROR_KEY_PATTERN}\b[\"']?\s*[:=]\s*)"
    r"(?![\"'])(?P<value>[^\s,;}]+)"
)
_PROMPT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TRANSIENT_AI_ERROR_RE = re.compile(
    r"\b(429|500|502|503|504)\b|temporary|temporarily|timeout|timed out|try again|rate limit|internal error",
    re.IGNORECASE,
)
_DEFAULT_AI_RETRY_DELAYS = (1.5, 4.0, 9.0)
_NON_RETRYABLE_AI_ERROR_RE = re.compile(
    r"(api key|not configured|unauthorized|forbidden|authentication|invalid api|invalid request|bad request|\b400\b|\b401\b|\b403\b)",
    re.IGNORECASE,
)
_RETRYABLE_RATE_LIMIT_OVERRIDE_RE = re.compile(
    r"\b429\b|rate limit|rate_limit|router_queue_limitation|quota|resource_exhausted",
    re.IGNORECASE,
)
_COMPUTER_USE_REQUEST_RE = re.compile(
    r"compute[\s_-]*use|compu?ter[\s_-]*use|computer\s+ツール|コンピューター操作|pc操作|"
    r"(google\s*chrome|chrome|chatgpt|atlas|アトラス|vivaldi|vivladi|line|ブラウザ|browser).{0,80}(操作|送信|入力|クリック|開いて|開く)",
    re.IGNORECASE,
)
_COMPUTER_USE_CHROME_TARGET_RE = re.compile(r"google\s*chrome|chrome|グーグル\s*クローム|クローム", re.IGNORECASE)
_COMPUTER_USE_CHROME_NEGATED_RE = re.compile(
    r"(google\s*chrome|chrome|グーグル\s*クローム|クローム).{0,16}"
    r"(使わない|使わず|禁止|not\s+use|do\s+not\s+use|don't\s+use)",
    re.IGNORECASE,
)
_COMPUTER_USE_VIVALDI_TARGET_RE = re.compile(r"vivaldi|vivladi|ヴィヴァルディ|ビバルディ", re.IGNORECASE)
_COMPUTER_USE_ATLAS_TARGET_RE = re.compile(r"chat\s*gpt\s*atlas|chatgpt\s*atlas|atlas|ａｔｌａｓ|アトラス", re.IGNORECASE)
_COMPUTER_USE_LINE_TARGET_RE = re.compile(r"(?<![A-Za-z])line(?![A-Za-z])|ライン", re.IGNORECASE)
_COMPUTER_USE_CHATGPT_TARGET_RE = re.compile(r"chat\s*gpt|chatgpt", re.IGNORECASE)
_COMPUTER_USE_PHYSICAL_INPUT_RE = re.compile(
    r"mouse|keyboard|key\s*board|physical|real\s+(?:ui|mouse|keyboard|click)|foreground|"
    r"マウス|キーボード|キー入力|物理|実操作|実際に|クリック|入力",
    re.IGNORECASE,
)


def _conversation_system_prompt(conv, manager):
    from blocks.chat._prompt_helpers import resolve_conversation_system_prompt
    from domain.kanban.prompt_note import append_kanban_system_prompt_note

    return append_kanban_system_prompt_note(resolve_conversation_system_prompt(conv, manager), conv)


def _has_real_provider(gateway, model):
    """Return True only when a non-stub provider will handle the request."""
    return gateway.has_real_provider(model)


def _is_transient_ai_error(message):
    return bool(_TRANSIENT_AI_ERROR_RE.search(str(message or "")))


def _ai_retry_attempts(params):
    retry = params.get("retry") if isinstance(params, dict) else None
    if isinstance(retry, dict):
        if retry.get("enabled") is False:
            return 1
        raw = retry.get("max_attempts")
    else:
        raw = params.get("max_retry_attempts") if isinstance(params, dict) else None
    try:
        attempts = int(raw) if raw is not None else 3
    except Exception:
        attempts = 3
    return max(1, min(attempts, 5))


def _ai_retry_delay(params, retry_index):
    retry = params.get("retry") if isinstance(params, dict) else None
    if isinstance(retry, dict) and isinstance(retry.get("delays"), list) and retry["delays"]:
        try:
            value = float(retry["delays"][min(retry_index, len(retry["delays"]) - 1)])
            return max(0.0, min(value, 30.0))
        except Exception:
            pass
    return _DEFAULT_AI_RETRY_DELAYS[min(retry_index, len(_DEFAULT_AI_RETRY_DELAYS) - 1)]


def _is_retryable_ai_error(message):
    text = str(message or "")
    if not text:
        return True
    # Some providers wrap a transient inner 429 in an outer HTTP 400 envelope.
    # Prefer the inner rate-limit signal so we still back off and retry.
    if _RETRYABLE_RATE_LIMIT_OVERRIDE_RE.search(text):
        return True
    if _NON_RETRYABLE_AI_ERROR_RE.search(text):
        return False
    return _is_transient_ai_error(text)


def _ai_direct_complete(model, messages, tools=None, params=None):
    """AI gateway を通して complete を実行する。
    APIキー未設定等で実プロバイダーがない場合は明示的エラーを返す。

    Returns:
        (response_dict, None) on success
        (None, error_message) on failure
    """
    gateway = LLMGateway(client=AIClient())
    if not _has_real_provider(gateway, model):
        return None, "AI provider API key not configured"
    try:
        response = gateway.complete(
            {"model": model, "messages": messages, "tools": tools or [], "params": params or {}}
        )
        return response, None
    except RuntimeError as exc:
        return None, "AI request failed: " + str(exc)


def _call_ai_complete_with_retry(model, messages, tools, params, call_handler, events, context, *, allow_retry=True):
    attempts = _ai_retry_attempts(params) if allow_retry else 1
    last_error = "AI request failed"
    for attempt_index in range(attempts):
        try:
            if call_handler is not None:
                response = call_handler(
                    "defaults.ai.complete",
                    {
                        "model": model,
                        "messages": messages,
                        "tools": tools,
                        "params": params,
                    },
                )
                if isinstance(response, dict) and response.get("status") == "error":
                    err = response.get("error", {})
                    raise RuntimeError(str(err.get("message") or "AI request failed"))
                if isinstance(response, dict) and response.get("status") == "ok":
                    return response.get("data", {})
                return response
            response, ai_error = _ai_direct_complete(model, messages, tools, params)
            if ai_error is not None:
                raise RuntimeError(ai_error)
            return response
        except _ChatCancelled:
            raise
        except Exception as exc:
            last_error = str(exc)
            if attempt_index >= attempts - 1 or not _is_retryable_ai_error(last_error):
                break
            delay = _ai_retry_delay(params, attempt_index)
            _append_event(
                events,
                context,
                _event(
                    "ai_retry_scheduled",
                    "APIエラーのため少し待って再送信します",
                    phase="ai_retry_scheduled",
                    attempt=attempt_index + 1,
                    max_attempts=attempts,
                    delay_seconds=delay,
                    error=_clip_error_text(last_error, 1200),
                ),
            )
            if delay > 0:
                time.sleep(delay)
    raise RuntimeError(_clip_error_text(last_error, 1200))


def _redact_error_text(value):
    text = str(value or "AI request failed")
    text = _SECRET_VALUE_RE.sub("[redacted]", text)
    text = _JWT_VALUE_RE.sub("[redacted]", text)
    text = _SENSITIVE_QUOTED_ASSIGNMENT_RE.sub(
        r"\g<prefix>\g<quote>[redacted]\g<quote>",
        text,
    )
    text = _AUTHORIZATION_ASSIGNMENT_RE.sub(
        r"\g<prefix>[redacted]",
        text,
    )
    text = _AUTH_SCHEME_VALUE_RE.sub(_redact_auth_scheme_value, text)
    text = _SENSITIVE_UNQUOTED_ASSIGNMENT_RE.sub(
        r"\g<prefix>[redacted]",
        text,
    )
    return text


def _redact_auth_scheme_value(match):
    candidate = match.group("value")
    normalized = candidate.lower()
    if normalized in _AUTH_PROSE_STOPWORDS:
        return match.group(0)
    return "[redacted]"


def _clip_error_text(value, limit=900):
    text = _redact_error_text(value).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "... (truncated)"


def _extract_provider_error(message):
    raw = _redact_error_text(message)
    lower = raw.lower()
    provider = "Google" if "google api error" in lower else "AI provider"
    status_match = re.search(r"\b(?:google|openai|anthropic)?\s*api error\s+(\d{3})\b", raw, re.IGNORECASE)
    status_code = int(status_match.group(1)) if status_match else None
    payload = None
    json_start = raw.find("{")
    if json_start >= 0:
        candidate = raw[json_start:].strip()
        try:
            payload = json.loads(candidate)
        except Exception:
            json_end = candidate.rfind("}")
            if json_end >= 0:
                try:
                    payload = json.loads(candidate[: json_end + 1])
                except Exception:
                    payload = None
    error_payload = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else payload
    code = None
    status = None
    provider_message = None
    if isinstance(error_payload, dict):
        if status_code is None and isinstance(error_payload.get("code"), int):
            status_code = error_payload.get("code")
        code = error_payload.get("code")
        status = error_payload.get("status") or error_payload.get("type")
        provider_message = error_payload.get("message")
    return {
        "provider": provider,
        "status_code": status_code,
        "code": code,
        "status": status,
        "message": str(provider_message or raw),
        "raw": raw,
    }


def _compact_provider_error_message(message, limit=520):
    text = _redact_error_text(message).strip()
    lines = [line.strip().lstrip("*").strip() for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        unique = []
        for line in lines:
            if line not in unique:
                unique.append(line)
            if len(unique) == 3:
                break
        suffix = ""
        if len(lines) > len(unique):
            suffix = " (ほか {} 件)".format(len(lines) - len(unique))
        return _clip_error_text("; ".join(unique) + suffix, limit)
    return _clip_error_text(re.sub(r"\s+", " ", text), limit)


def _ai_error_hint(status_code, provider_message):
    lower = str(provider_message or "").lower()
    if status_code == 400 and "function_declarations" in lower and "missing field" in lower:
        return "Google/Gemma が tool 定義の schema を拒否しました。tool の配列 schema を補正したうえで再試行してください。"
    if status_code == 400:
        return "リクエスト形式、モデル設定、添付ファイル、tool 選択の組み合わせを確認してください。"
    if status_code in {401, 403}:
        return "APIキー、OAuth、モデル利用権限、またはローカル承認が拒否されています。設定と承認カードを確認してください。"
    if status_code == 404:
        return "指定モデルまたはエンドポイントが見つかりません。モデル名と provider 設定を確認してください。"
    if status_code == 409:
        return "同時実行や状態の衝突が起きています。少し待ってから再送信してください。"
    if status_code == 429:
        return "レート制限またはクォータ上限です。時間を置くか、別のAPIキーまたはモデルを選んでください。"
    if status_code and status_code >= 500:
        return "provider 側またはバックエンド側の一時的な障害です。少し待って再試行してください。"
    return "同じ入力で再発する場合は、モデル、APIキー、選択中の tool を順に切り分けてください。"


def _format_terminal_ai_error_message(model, message):
    info = _extract_provider_error(message)
    status_code = info["status_code"]
    provider = info["provider"]
    code = info["code"]
    provider_status = info["status"]
    compact_message = _compact_provider_error_message(info["message"])
    raw_message = _clip_error_text(info["raw"], 1200)
    status_label = "HTTP {}".format(status_code) if status_code else "provider error"
    code_bits = [str(item) for item in (provider_status, code) if item not in (None, "", status_code)]
    code_suffix = " ({})".format(", ".join(code_bits)) if code_bits else ""
    lines = [
        "APIエラーでこのタスクを終了しました。",
        "",
        "- モデル: {}".format(model or "unknown"),
        "- 原因: {} {}{}".format(provider, status_label, code_suffix),
        "- 内容: {}".format(compact_message),
        "- 次に試すこと: {}".format(_ai_error_hint(status_code, info["message"])),
    ]
    return "\n".join(lines), raw_message


def _ai_error_response(model, message, params, events=None):
    text, raw_message = _format_terminal_ai_error_message(model, message)
    return {
        "content": [{"type": "text", "text": text}],
        "finish_reason": "error",
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "events": list(events or []),
        "tool_logs": [],
        "metadata": {
            "model": model,
            "attached_tool_count": 0,
            "attached_tools": [],
            "thinking": {"state": "failed"},
            "thinking_level": params.get("thinking_level") if isinstance(params, dict) else None,
            "error": {
                "type": "AI_ERROR",
                "message": text,
                "raw_message": raw_message,
                "terminal": True,
            },
        },
    }


def _ai_error_after_tool_use_response(ai_error):
    message = str(ai_error or "AI request failed")
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    "tool 実行後に AI provider がエラーを返したため停止しました。"
                    "ここまでの tool ログとスクリーンショットは保存済みです。"
                    " reason: "
                    + message
                ),
            }
        ],
        "finish_reason": "ai_error_after_tool_use",
        "usage": {},
        "metadata": {
            "ai_error_after_tool_use": True,
            "ai_error": message,
            "transient_ai_error": _is_transient_ai_error(message),
        },
    }


def _stop_after_tool_ai_error(events, context, ai_error):
    _append_event(
        events,
        context,
        _event(
            "status",
            "tool 実行後の AI provider エラーで停止しました",
            phase="ai_error_after_tool_use",
            ai_error=str(ai_error or "AI request failed"),
            transient_ai_error=_is_transient_ai_error(ai_error),
        ),
    )
    return _ai_error_after_tool_use_response(ai_error)


def _event(event_type, message, **extra):
    payload = {
        "type": event_type,
        "message": message,
        "timestamp": timestamp(),
    }
    payload.update(_redact_sensitive_value(extra))
    return payload


class _ChatCancelled(Exception):
    pass


def _is_cancelled(context):
    checker = (context or {}).get("is_cancelled") if isinstance(context, dict) else None
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return False


def _raise_if_cancelled(context):
    if _is_cancelled(context):
        raise _ChatCancelled()


def _append_event(events, context, event):
    events.append(event)
    persist_callback = (context or {}).get("stream_event_persist_callback") if isinstance(context, dict) else None
    if callable(persist_callback):
        try:
            persist_callback(list(events), event)
        except Exception:
            pass
    callback = (context or {}).get("stream_event_callback") if isinstance(context, dict) else None
    if callable(callback):
        try:
            callback(event)
        except Exception:
            pass


def _is_stream_fallback_context(context):
    if not isinstance(context, dict):
        return False
    return callable(context.get("stream_event_callback")) and callable(context.get("is_cancelled"))


def _assistant_raw_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return " ".join(parts)


def _create_stream_assistant_draft(store, conversation_id, user_msg, model, params):
    seq = user_msg.get("sequence_number", 1) + 1
    return store.add_message(
        conversation_id,
        {
            "role": "assistant",
            "parent_id": user_msg["id"],
            "sequence_number": seq,
            "content": [],
            "raw_text": "",
            "finish_reason": "streaming",
            "usage": {},
            "widget": None,
            "metadata": {
                "model": model,
                "streaming": True,
                "draft": True,
                "thinking": {"state": "running"},
                "thinking_level": (params or {}).get("thinking_level"),
            },
            "events": [],
            "tool_logs": [],
            "model": model,
        },
    )


def _stream_draft_event_persister(store, conversation_id, draft_id, model, params):
    def persist(events, _event):
        draft = store.get_message(conversation_id, draft_id)
        metadata = dict((draft or {}).get("metadata") or {})
        metadata.update(
            {
                "model": model,
                "streaming": True,
                "draft": True,
                "thinking": {"state": "running"},
                "thinking_level": (params or {}).get("thinking_level"),
            }
        )
        store.update_message(
            conversation_id,
            draft_id,
            {
                "events": events,
                "metadata": metadata,
                "finish_reason": "streaming",
            },
        )

    return persist


def _final_assistant_updates(assistant_msg_dict):
    updates = dict(assistant_msg_dict)
    content = updates.get("content", [])
    updates["raw_text"] = _assistant_raw_text(content)
    metadata = dict(updates.get("metadata") or {})
    metadata.pop("streaming", None)
    metadata.pop("draft", None)
    updates["metadata"] = metadata
    return updates


def _redact_sensitive_value(value, *, parent_key=""):
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key)
            if _SECRET_KEY_RE.search(key_text):
                redacted[key] = "[redacted]"
            else:
                redacted[key] = _redact_sensitive_value(item, parent_key=key_text)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive_value(item, parent_key=parent_key) for item in value]
    if isinstance(value, str):
        if parent_key and _SECRET_KEY_RE.search(parent_key):
            return "[redacted]"
        if value.startswith("data:image/"):
            return "[image data saved as artifact]"
    return value


def _resolve_selected_tools(raw_tools):
    registry = ToolRegistry()
    if not isinstance(raw_tools, list):
        return registry.list_tools(), []

    resolved = []
    unknown = []
    for item in raw_tools:
        if isinstance(item, dict):
            resolved.append(item)
            continue
        if not isinstance(item, str):
            continue
        tool_id = item.strip()
        if not tool_id:
            continue
        tool_def = registry.get(tool_id)
        if tool_def is None:
            unknown.append(tool_id)
            continue
        resolved.append(tool_def)
    return resolved, unknown


def _infer_requested_tools_from_message(user_text):
    if not isinstance(user_text, str) or not _COMPUTER_USE_REQUEST_RE.search(user_text):
        return []
    return ["computer_use", "browser_computer"]


def _with_inferred_tools(input_data, inferred_tool_ids):
    if not inferred_tool_ids:
        return input_data
    if _has_explicit_selected_tools(input_data):
        return input_data
    raw_tools = input_data.get("tools")
    existing_tools = list(raw_tools) if isinstance(raw_tools, list) else []
    merged = []
    seen = set()
    for item in existing_tools + list(inferred_tool_ids):
        key = item if isinstance(item, str) else id(item)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    updated = dict(input_data)
    updated["tools"] = merged
    return updated


def _has_explicit_selected_tools(input_data):
    params = input_data.get("params") if isinstance(input_data.get("params"), dict) else {}
    tool_policy = params.get("tool_policy") if isinstance(params.get("tool_policy"), dict) else {}
    if "selected_tools" in tool_policy:
        return True
    message = input_data.get("message") if isinstance(input_data.get("message"), dict) else {}
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    return "selected_tools" in metadata


def _computer_use_preferences_from_text(user_text):
    text = user_text if isinstance(user_text, str) else ""
    preferences = {}
    atlas_target = _COMPUTER_USE_ATLAS_TARGET_RE.search(text)
    if atlas_target:
        preferences["computer_use_target_app"] = "ChatGPT Atlas"
        preferences["computer_use_foreground_preferred"] = True
    elif _COMPUTER_USE_VIVALDI_TARGET_RE.search(text):
        preferences["computer_use_target_app"] = "Vivaldi"
    elif _COMPUTER_USE_CHROME_TARGET_RE.search(text) and not _COMPUTER_USE_CHROME_NEGATED_RE.search(text):
        preferences["computer_use_target_app"] = "Google Chrome"
    if _COMPUTER_USE_LINE_TARGET_RE.search(text):
        preferences["computer_use_target_title"] = "LINE"
    elif not atlas_target and _COMPUTER_USE_CHATGPT_TARGET_RE.search(text):
        preferences["computer_use_target_title"] = "ChatGPT"
    if _COMPUTER_USE_PHYSICAL_INPUT_RE.search(text):
        preferences["computer_use_mouse_keyboard_requested"] = True
        preferences["computer_use_physical_clicks"] = True
    return preferences


def _apply_computer_use_context_preferences(context, user_text):
    updated = dict(context or {})
    preferences = _computer_use_preferences_from_text(user_text)
    for key, value in preferences.items():
        if value not in (None, "", False):
            updated[key] = value
    return updated


def _available_tools(context, input_data):
    raw_tools = input_data.get("tools")
    params = input_data.get("params") if isinstance(input_data.get("params"), dict) else {}
    tool_policy = params.get("tool_policy") if isinstance(params.get("tool_policy"), dict) else {}
    if raw_tools is None and "selected_tools" in tool_policy:
        raw_tools = tool_policy.get("selected_tools")
    if raw_tools is None:
        message = input_data.get("message") if isinstance(input_data.get("message"), dict) else {}
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        if "selected_tools" in metadata:
            raw_tools = metadata.get("selected_tools")
    try:
        tools, unknown_tools = _resolve_selected_tools(raw_tools)
    except Exception:
        tools, unknown_tools = [], []
    resolved_context = resolve_runtime_profile_context(context or {})
    if unknown_tools:
        resolved_context["unknown_selected_tools"] = unknown_tools
    runtime_profile = resolved_context.get("runtime_profile")
    agent_id = input_data.get("agent_id")
    filtered = filter_tool_definitions_for_runtime_profile(tools, runtime_profile, agent_id=agent_id)
    return filtered, adapt_tool_definitions(filtered), resolved_context


def _prefocus_computer_use_target_window(available_tools, base_context, *, call_handler=None):
    if not isinstance(base_context, dict) or not base_context.get("user_requested_computer_use"):
        return None
    if not _computer_use_prefocus_is_preapproved(base_context):
        return None
    target_app = str(base_context.get("computer_use_target_app") or "").strip()
    target_title = str(base_context.get("computer_use_target_title") or "").strip()
    if not (target_app or target_title):
        return None
    connected_names = connected_tool_names(
        available_tools,
        base_context.get("runtime_profile") if isinstance(base_context.get("runtime_profile"), dict) else None,
        agent_id=base_context.get("agent_id"),
    )
    tool_name = next(
        (candidate for candidate in ("browser_computer", "computer_use", "browser_use") if candidate in connected_names),
        "",
    )
    if not tool_name:
        return None

    payload = {}
    if target_app:
        payload["app"] = target_app
    if target_title:
        payload["title"] = target_title
    arguments = {"action": "computer.select_window", "payload": payload}
    invoke_context = build_tool_execution_context(base_context, tool_name, connected_names)
    if call_handler is not None:
        result = call_handler(
            "defaults.tool.invoke",
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "context": invoke_context,
            },
        )
        if isinstance(result, dict) and result.get("status") == "ok":
            return result.get("data", {})
        return result

    from domain.tool.executor import ToolExecutor

    return ToolExecutor().execute(tool_name, arguments, invoke_context)


def _computer_use_prefocus_is_preapproved(context):
    if not isinstance(context, dict):
        return False
    try:
        from domain.tool_policy.internal_context import (
            internal_tool_decision_allows,
            tool_server_approval_context_is_internal,
        )
    except Exception:
        return False
    return bool(tool_server_approval_context_is_internal(context) or internal_tool_decision_allows(context))


def _tool_use_blocks(response):
    blocks = response.get("content", []) if isinstance(response, dict) else []
    if not isinstance(blocks, list):
        return []
    return [
        block
        for block in blocks
        if (
            isinstance(block, dict)
            and block.get("type") in {"tool_use", "tool_call"}
            and _tool_arguments_or_none(block) is not None
        )
    ]


def _response_text(response):
    blocks = response.get("content", []) if isinstance(response, dict) else []
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    parts = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts)


def _params_without_thinking(params):
    retry_params = dict(params or {})
    for key in ("thinking", "thinking_level", "reasoning_effort"):
        retry_params.pop(key, None)
    return retry_params


def _empty_response_message(finish_reason):
    reason = str(finish_reason or "unknown").strip() or "unknown"
    return (
        "モデルから本文のない応答が返りました。"
        "もう一度送信するか、thinkingを「なし」にして試してください。"
        f" (finish_reason: {reason})"
    )


def _tool_limit_message(limit, tool_uses):
    names = []
    for block in tool_uses:
        name = str(block.get("name") or block.get("tool_name") or "").strip()
        if name:
            names.append(name)
    suffix = " pending_tools=" + ", ".join(names) if names else ""
    return (
        "tool call の上限に達したため停止しました。"
        "同じ依頼を続ける場合は、もう一度送信してください。"
        f" (max_tool_calls: {limit}{suffix})"
    )


def _unconnected_tool_call_response(tool_name, tool_call_id, connected_names):
    connected = sorted(str(name) for name in connected_names if name)
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"{tool_name} はこの会話に接続されていないため実行しませんでした。"
                    "接続済みの tool だけを使用してください。"
                ),
            }
        ],
        "finish_reason": "tool_call_rejected",
        "usage": {},
        "metadata": {
            "tool_call_rejected": True,
            "rejected_tool_name": tool_name,
            "rejected_tool_call_id": tool_call_id,
            "connected_tools": connected,
        },
    }


def _reject_unconnected_tool_use(tool_uses, connected_names):
    allowed = {str(name) for name in connected_names if name}
    for block in tool_uses:
        tool_name = str(block.get("name") or block.get("tool_name") or "").strip()
        if not tool_name or tool_name in allowed:
            continue
        tool_call_id = str(block.get("id") or block.get("tool_call_id") or "")
        return tool_name, tool_call_id
    return None


def _tool_result_data(result):
    if not isinstance(result, dict):
        return {}
    data = result.get("data", result)
    return data if isinstance(data, dict) else {}


def _tool_result_reason(result):
    if not isinstance(result, dict):
        return ""
    data = _tool_result_data(result)
    for source in (data, result):
        if not isinstance(source, dict):
            continue
        for key in ("reason", "message", "result", "summary"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        error_value = source.get("error")
        if isinstance(error_value, dict):
            message = error_value.get("message") or error_value.get("reason")
            if isinstance(message, str) and message.strip():
                return message.strip()
        elif isinstance(error_value, str) and error_value.strip():
            return error_value.strip()
    return ""


def _tool_result_is_error(result):
    if not isinstance(result, dict):
        return False
    if result.get("status") == "error":
        return True
    data = _tool_result_data(result)
    if data.get("status") == "error" or data.get("is_error") is True:
        return True
    widget = data.get("widget") if isinstance(data.get("widget"), dict) else {}
    return widget.get("is_error") is True


def _find_tool_recovery(value):
    if isinstance(value, dict):
        recovery = value.get("recovery")
        if isinstance(recovery, dict):
            return recovery
        widget = value.get("widget")
        if isinstance(widget, dict):
            recovery = widget.get("recovery")
            if isinstance(recovery, dict):
                return recovery
        data = value.get("data")
        if isinstance(data, dict):
            recovery = _find_tool_recovery(data)
            if recovery:
                return recovery
        error_value = value.get("error")
        if isinstance(error_value, dict):
            recovery = _find_tool_recovery(error_value)
            if recovery:
                return recovery
    return {}


def _tool_result_recovery_kind(result):
    recovery = _find_tool_recovery(result)
    kind = str(recovery.get("kind") or "").strip()
    if kind:
        return kind
    if not _tool_result_is_error(result):
        return ""
    reason = _tool_result_reason(result).lower()
    if "foregroundで作業しますか" in reason:
        return "foreground_confirmation_required"
    if "visible window" in reason or "background computer-use is disabled" in reason:
        return "visible_window_required"
    return ""


def _message_content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _tool_blocked_response(tool_name, result):
    recovery = _find_tool_recovery(result)
    kind = str(recovery.get("kind") or "").strip()
    if not kind:
        kind = _tool_result_recovery_kind(result)
    reason = _tool_result_reason(result)
    if kind == "foreground_confirmation_required":
        prompt = str(recovery.get("prompt") or "").strip()
        message = prompt or reason or f"{tool_name} は background で実行できません。foregroundで作業しますか？"
    elif kind in {"visible_window_required", "focus_required"}:
        message = (
            f"{tool_name} は現在表示されている画面だけを操作する設定のため停止しました。"
            + (f" reason: {reason}" if reason else "")
        )
    else:
        message = (
            f"{tool_name} が回復不能な tool ブロックを返したため停止しました。"
            + (f" reason: {reason}" if reason else "")
        )
    return {
        "content": [{"type": "text", "text": message}],
        "finish_reason": "tool_blocked",
        "usage": {},
        "metadata": {
            "tool_blocked": True,
            "tool_blocked_tool": tool_name,
            "tool_blocked_kind": kind,
            "tool_blocked_recovery": recovery,
        },
    }


def _tool_arguments_or_none(block):
    value = block.get("input", block.get("arguments", {}))
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _tool_arguments(block):
    return _tool_arguments_or_none(block) or {}


def _append_assistant_tool_use_message(messages, tool_uses, *, reasoning_content=""):
    tool_calls = []
    for block in tool_uses:
        tool_name = str(block.get("name") or block.get("tool_name") or "")
        if not tool_name:
            continue
        tool_call_id = str(block.get("id") or block.get("tool_call_id") or gen_id())
        arguments = _tool_arguments(block)
        tool_calls.append(
            {
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        )
    if not tool_calls:
        return
    entry = {
        "role": "assistant",
        "content": "",
        "tool_calls": tool_calls,
    }
    if isinstance(reasoning_content, str) and reasoning_content.strip():
        entry["reasoning_content"] = reasoning_content
    messages.append(entry)


def _model_supports_vision(model):
    try:
        matches = LLMGateway(client=AIClient()).runtime_model_matches(str(model or ""))
    except Exception:
        matches = []
    for match in matches or []:
        capabilities = match.get("capabilities", [])
        if isinstance(capabilities, dict):
            if capabilities.get("vision") or capabilities.get("image_input") or capabilities.get("multimodal"):
                return True
        elif any(str(item) in {"vision", "image_input", "multimodal"} for item in capabilities or []):
            return True
    return any(token in str(model or "").lower() for token in ("gemini", "gemma", "gpt-4o", "gpt-5"))


def _model_supports_attachments(model):
    try:
        matches = LLMGateway(client=AIClient()).runtime_model_matches(str(model or ""))
    except Exception:
        matches = []
    for match in matches or []:
        for source in (
            match,
            match.get("metadata", {}) if isinstance(match, dict) else {},
            match.get("availability", {}) if isinstance(match, dict) else {},
        ):
            if isinstance(source, dict) and source.get("supports_attachments") is False:
                return False
    return True


def _image_data_url_byte_length(data_url):
    if not isinstance(data_url, str) or not data_url.startswith(_DATA_IMAGE_PREFIX):
        return None
    header, separator, encoded = data_url.partition(",")
    if not separator or ";base64" not in header.lower():
        return None
    try:
        import base64

        return len(base64.b64decode(encoded, validate=True))
    except Exception:
        return None


def _browser_screenshot_data_url(result):
    if not isinstance(result, dict):
        return ""
    data = result.get("data", result)
    if not isinstance(data, dict):
        return ""
    widget = data.get("widget") if isinstance(data.get("widget"), dict) else {}
    candidates = [data, widget]
    for candidate in candidates:
        data_url = candidate.get("data_url") or candidate.get("dataUrl")
        byte_length = _image_data_url_byte_length(data_url)
        if byte_length is not None and byte_length <= MAX_ATTACHMENT_IMAGE_BYTES:
            return data_url
    path = data.get("path") or widget.get("path")
    mime = data.get("mime_type") or widget.get("mime_type") or "image/png"
    if isinstance(path, str) and path:
        try:
            import base64

            encoded = base64.b64encode(Path(path).read_bytes()).decode("ascii")
            return "data:{};base64,{}".format(mime, encoded)
        except Exception:
            return ""
    return ""


def _browser_screenshot_guidance(result, tool_call_id=""):
    del result
    call_id = str(tool_call_id or "").strip()
    linkage = " for tool_call_id={}".format(call_id) if call_id else ""
    return (
        "Browser/computer screenshot tool-output evidence{}; "
        "it belongs to the preceding tool result and is not a new user request."
    ).format(linkage)


def _tool_result_message_text(tool_name, result):
    if isinstance(result, dict):
        data = result.get("data", result)
        if isinstance(data, dict):
            if tool_name in {"browser_companion", "browser_computer", "browser_use", "computer_use"}:
                result_text = json.dumps(_compact_tool_log_value(data), ensure_ascii=False)
            else:
                result_text = str(data.get("result", data.get("summary", json.dumps(data, ensure_ascii=False))))
        else:
            result_text = str(data)
    else:
        result_text = str(result)
    max_chars = 12000
    if len(result_text) > max_chars:
        return result_text[:max_chars] + "\n[tool result truncated]"
    return result_text


def _append_tool_result_message(messages, tool_name, result, tool_call_id="", *, model=""):
    result_text = _tool_result_message_text(tool_name, result)
    messages.append(
        {
            "role": "tool",
            "name": tool_name,
            "tool_call_id": tool_call_id,
            "content": result_text,
        }
    )
    if (
        tool_name in {"browser_companion", "browser_computer", "browser_use", "computer_use"}
        and _model_supports_vision(model)
        and _model_supports_attachments(model)
    ):
        screenshot = _browser_screenshot_data_url(result)
        if screenshot:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": _browser_screenshot_guidance(result, tool_call_id),
                        },
                        {"type": "image_url", "image_url": {"url": screenshot}},
                    ],
                }
            )


_TOOL_LOG_LONG_TEXT_KEYS = {
    "content",
    "diff",
    "output",
    "result",
    "stderr",
    "stdout",
    "text",
}
_TOOL_LOG_STRING_LIMIT = 1800
_TOOL_LOG_LIST_LIMIT = 16


def _truncate_tool_log_text(value, limit=_TOOL_LOG_STRING_LIMIT):
    text = str(value or "")
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return "{}\n[tool log truncated: {} chars omitted]".format(
        text[: max(0, limit - 48)].rstrip(),
        omitted,
    )


def _compact_tool_log_value(value, key=""):
    value = _redact_sensitive_value(value)
    if isinstance(value, dict):
        compact = {}
        for key, item in value.items():
            if key in {"data_url", "dataUrl"} and isinstance(item, str) and item.startswith("data:image/"):
                compact[key] = "[image data saved as artifact]"
            else:
                compact[key] = _compact_tool_log_value(item, key=str(key))
        return compact
    if isinstance(value, list):
        compact_items = [_compact_tool_log_value(item, key=key) for item in value[:_TOOL_LOG_LIST_LIMIT]]
        if len(value) > _TOOL_LOG_LIST_LIMIT:
            compact_items.append({
                "truncated": True,
                "omitted_items": len(value) - _TOOL_LOG_LIST_LIMIT,
            })
        return compact_items
    if isinstance(value, str) and "data:image/" in value:
        import re

        value = re.sub(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+", "[image data saved as artifact]", value)
    if isinstance(value, str) and (key in _TOOL_LOG_LONG_TEXT_KEYS or len(value) > _TOOL_LOG_STRING_LIMIT * 2):
        return _truncate_tool_log_text(value)
    return value


def _truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "debug", "enabled"}
    return False


def _frontend_debug_settings_enabled():
    try:
        settings_path = frontend_settings_path()
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    debug = settings.get("debug") if isinstance(settings, dict) else {}
    if not isinstance(debug, dict):
        return False
    return _truthy(debug.get("ai_request_logging") or debug.get("enabled"))


def _ai_debug_enabled(input_data=None, params=None, context=None):
    if _truthy(os.environ.get("RUMI_DEFAULTSPACK_AI_DEBUG")):
        return True
    for source in (context, params, input_data):
        if not isinstance(source, dict):
            continue
        for key in ("ai_debug_enabled", "ai_debug", "debug_mode", "debug", "log_ai_requests"):
            if key in source and _truthy(source.get(key)):
                return True
    return _frontend_debug_settings_enabled()


def _ai_debug_log_dir(context):
    workspace = (context or {}).get("conversation_workspace_dir") if isinstance(context, dict) else None
    if isinstance(workspace, str) and workspace.strip():
        return Path(workspace) / "debug" / "ai_requests"
    return Path(__file__).resolve().parents[2] / "user_data" / "shared" / "chat" / "debug" / "ai_requests"


def _debug_image_suffix(mime_type):
    subtype = str(mime_type or "").split("/", 1)[-1].split(";", 1)[0].lower()
    if subtype in {"jpeg", "jpg"}:
        return ".jpg"
    if subtype in {"png", "gif", "webp"}:
        return "." + subtype
    return ".img"


def _save_debug_data_image(data_url, debug_dir, request_key, images):
    header, separator, encoded = str(data_url or "").partition(",")
    if not separator:
        return "[invalid image data_url]"
    mime_type = header[5:].split(";", 1)[0] if header.startswith("data:") else "image/png"
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception:
        return "[invalid image data_url]"
    index = len(images) + 1
    path = debug_dir / "{}-image-{}{}".format(request_key, index, _debug_image_suffix(mime_type))
    try:
        path.write_bytes(raw)
    except OSError:
        return "[failed to save image data_url]"
    record = {
        "path": str(path),
        "mime_type": mime_type,
        "bytes": len(raw),
    }
    images.append(record)
    return {
        "url": "[image data saved as artifact]",
        "debug_image_path": str(path),
        "mime_type": mime_type,
        "bytes": len(raw),
    }


def _debug_sanitize_ai_payload(value, debug_dir, request_key, images, *, parent_key=""):
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            key_text = str(key)
            if _SECRET_KEY_RE.search(key_text):
                sanitized[key] = "[redacted]"
            else:
                sanitized[key] = _debug_sanitize_ai_payload(
                    item,
                    debug_dir,
                    request_key,
                    images,
                    parent_key=key_text,
                )
        return sanitized
    if isinstance(value, list):
        return [
            _debug_sanitize_ai_payload(item, debug_dir, request_key, images, parent_key=parent_key)
            for item in value
        ]
    if isinstance(value, str):
        if parent_key and _SECRET_KEY_RE.search(parent_key):
            return "[redacted]"
        if value.startswith(_DATA_IMAGE_PREFIX):
            return _save_debug_data_image(value, debug_dir, request_key, images)
    return value


def _write_debug_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _log_ai_debug_request(context, *, model, messages, tools, params, step_index, reason=""):
    if not _ai_debug_enabled(params=params, context=context):
        return None
    debug_dir = _ai_debug_log_dir(context)
    debug_dir.mkdir(parents=True, exist_ok=True)
    step_label = str(step_index)
    request_key = "ai-{}-step-{}".format(int(time.time() * 1000), re.sub(r"[^A-Za-z0-9_.-]+", "_", step_label))
    images = []
    payload = {
        "schema_version": 1,
        "kind": "ai_request_debug_log",
        "created_at": timestamp(),
        "model": model,
        "step_index": step_index,
        "reason": reason,
        "messages": _debug_sanitize_ai_payload(messages, debug_dir, request_key, images),
        "tools": _debug_sanitize_ai_payload(tools, debug_dir, request_key, images),
        "params": _debug_sanitize_ai_payload(params, debug_dir, request_key, images),
        "images": images,
    }
    path = debug_dir / "{}.json".format(request_key)
    try:
        _write_debug_json(path, payload)
    except OSError:
        return None
    return str(path)


def _append_ai_debug_response(path, response):
    if not path:
        return
    try:
        debug_path = Path(path)
        payload = json.loads(debug_path.read_text(encoding="utf-8"))
        images = payload.get("response_images")
        if not isinstance(images, list):
            images = []
        payload["response_logged_at"] = timestamp()
        payload["response"] = _debug_sanitize_ai_payload(
            response,
            debug_path.parent,
            debug_path.stem + "-response",
            images,
        )
        if images:
            payload["response_images"] = images
        _write_debug_json(debug_path, payload)
    except Exception:
        pass


def _truncate_text(value, limit=480):
    text = str(value or "").strip()
    if len(text) > limit:
        return text[: max(0, limit - 3)] + "..."
    return text


def _tool_window_details(result, key):
    if not isinstance(result, dict):
        return ""
    data = _tool_result_data(result)
    widget = data.get("widget") if isinstance(data.get("widget"), dict) else {}
    candidates = []
    for container in (data, widget):
        if not isinstance(container, dict):
            continue
        window = container.get(key)
        if isinstance(window, dict):
            candidates.append(window)
    for window in candidates:
        app = _truncate_text(window.get("app"), limit=80)
        title = _truncate_text(window.get("title"), limit=140)
        if app and title:
            return "{} | {}".format(app, title)
        if title:
            return title
        if app:
            return app
    return ""


def _tool_result_summary(tool_name, result):
    reason = _tool_result_reason(result)
    if reason:
        return _truncate_text(reason)
    data = _tool_result_data(result)
    lowered = str(tool_name or "").lower()
    if "file" in lowered:
        widget = data.get("widget") if isinstance(data.get("widget"), dict) else {}
        file_data = {**data, **widget}
        path = file_data.get("path")
        basename = Path(str(path or "")).name if path else ""
        label = basename or str(path or "").strip() or "file"
        if file_data.get("written"):
            return _truncate_text("Edited {}".format(label))
        if file_data.get("patched"):
            return _truncate_text("Patched {}".format(label))
        if file_data.get("deleted"):
            return _truncate_text("Deleted {}".format(label))
        if isinstance(file_data.get("content"), str) or isinstance(data.get("result"), str):
            if file_data.get("truncated"):
                return _truncate_text("Read compact excerpt from {}".format(label))
            return _truncate_text("Read {}".format(label))
    if "terminal" in lowered or "shell" in lowered or "exec" in lowered:
        exit_code = data.get("exit_code")
        if exit_code is not None:
            return _truncate_text("Command finished with exit code {}".format(exit_code))
    if tool_name in {"browser_computer", "browser_use", "computer_use"}:
        active_window = _tool_window_details(result, "active_window")
        selected_window = (
            _tool_window_details(result, "selected_window")
            or _tool_window_details(result, "target_window")
        )
        if active_window and selected_window and active_window != selected_window:
            return _truncate_text(
                "{} completed. Foreground: {}. Selected target: {}.".format(
                    tool_name,
                    active_window,
                    selected_window,
                )
            )
        if active_window:
            return _truncate_text("{} completed on {}.".format(tool_name, active_window))
        if selected_window:
            return _truncate_text("{} completed with target {}.".format(tool_name, selected_window))
    for key in ("results", "items", "files", "screenshots"):
        value = data.get(key)
        if isinstance(value, list):
            return "{} returned {} {}".format(tool_name, len(value), key)
    if _tool_result_is_error(result):
        return "{} failed".format(tool_name)
    return "{} completed".format(tool_name)


def _artifact_kind_for_path(path):
    suffix = Path(str(path or "")).suffix.lower()
    return "image" if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"} else "file"


def _tool_result_artifacts(value, artifacts=None, seen=None):
    artifacts = artifacts if isinstance(artifacts, list) else []
    seen = seen if isinstance(seen, set) else set()
    if isinstance(value, dict):
        preferred_path = ""
        for key in ("model_image_path", "screenshot_path", "path"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                preferred_path = item.strip()
                break
        if preferred_path and preferred_path not in seen:
            seen.add(preferred_path)
            artifacts.append(
                {
                    "name": Path(preferred_path).name or "artifact",
                    "path": preferred_path,
                    "kind": _artifact_kind_for_path(preferred_path),
                }
            )
        for key, item in value.items():
            if key in {"path", "screenshot_path", "model_image_path", "data_url", "dataUrl"}:
                continue
            _tool_result_artifacts(item, artifacts, seen)
    elif isinstance(value, list):
        for item in value:
            _tool_result_artifacts(item, artifacts, seen)
    return artifacts


def _bounded_compact_tool_result(result, summary, artifacts, limit=6000):
    compact = _compact_tool_log_value(result)
    try:
        encoded = json.dumps(compact, ensure_ascii=False)
    except Exception:
        encoded = str(compact)
    if len(encoded) <= limit:
        return compact
    return {
        "summary": summary,
        "artifacts": artifacts,
        "truncated": True,
    }


def _tool_visibility_message(tools):
    names = []
    for tool in tools or []:
        name = tool_name_from_definition(tool)
        if not name:
            continue
        description = ""
        if isinstance(tool, dict):
            function_def = tool.get("function")
            if isinstance(function_def, dict):
                description = str(function_def.get("description") or "")
            description = description or str(tool.get("description") or tool.get("summary") or "")
        label = name if not description else "{}: {}".format(name, description)
        names.append(label)
    if not names:
        return None
    guidance = ""
    tool_names = {tool_name_from_definition(tool) for tool in tools or []}
    if tool_names.intersection({"browser_companion", "browser_computer", "browser_use", "computer_use"}):
        guidance = (
            " Browser tool rules: browser_companion is the DOM-aware extension path and can inspect paired browser tabs with the user's live session; "
            "browser_computer/computer_use are visible-window computer-use paths, so use apps/windows plus select_app/select_window to target Vivaldi, VS Code, Finder, LINE, Chrome, or any other visible app/window; "
            "when you need background-tab DOM access, element ids, or the user's signed-in browser session, prefer browser_companion; "
            "for visible-window actions, inspect app state with context before screenshots; "
            "for visual clicks, use a zoom ladder: first take a full or selected-window screenshot, then when the target is small or ambiguous call screenshot again with crop/zoom around the likely region; source=latest crops from that last full/selected-window screenshot, while source=current_crop is only for intentionally cropping the current crop again; click only using normalized_x/normalized_y relative to the attached image; "
            "after a zoomed/cropped inspection, take a fresh full or selected-window screenshot before unrelated actions so stale crop coordinates are not reused; "
            "prefer one type call for words like hello and key only for shortcuts/return; "
            "click/move without physical=true only moves the virtual AI cursor and does not move the user's mouse."
        )
    return {
        "role": "system",
        "content": (
            "Available tools are connected for this turn. "
            "Use them when they are relevant, and do not claim that no tools are available. "
            "Connected tools: " + "; ".join(names) + guidance
        ),
    }


# Compatibility helper retained for focused legacy tests and comparison only.
# `send.run()` now routes through `ChatRunEngine` instead of this tool loop.
def _complete_with_tools(model, messages, tools, context, call_handler, params):
    events = []
    _append_event(events, context, _event("status", "{} が考えています".format(model), phase="thinking", model=model))
    tool_logs = []
    debug_logs = []
    if tools:
        _append_event(
            events,
            context,
            _event(
                "status",
                "{} 個の tool を接続しました".format(len(tools)),
                phase="tools_attached",
                tool_count=len(tools),
            )
        )

    working_messages = list(messages)
    tool_context_message = _tool_visibility_message(tools)
    if tool_context_message is not None:
        insert_at = 1 if working_messages and working_messages[0].get("role") == "system" else 0
        working_messages.insert(insert_at, tool_context_message)
    response = None
    connected_names = connected_tool_names(tools, context.get("runtime_profile") if isinstance(context, dict) else None)
    limit = max_tool_calls(context or {})
    if limit is None:
        limit = explicit_param_max_tool_calls(params)
    if limit is None and str(os.environ.get("RUMI_FORCE_LEGACY_TOOL_LIMIT") or "").strip().lower() in {"1", "true", "yes", "on"}:
        limit = 12 if connected_names.intersection({"browser_companion", "browser_computer", "browser_use", "computer_use"}) else 4
    emergency_budget = emergency_budget_from_context(context or {})
    loop_guard = LoopGuard(
        run_id=str((context or {}).get("run_id") or ""),
        conversation_id=str((context or {}).get("conversation_id") or ""),
        task_lineage_id=str((context or {}).get("task_lineage_id") or (context or {}).get("conversation_id") or ""),
        config=loop_guard_config_from_context(context or {}),
    )

    blocked_response = None
    for step_index in range(max(1, emergency_budget.max_model_turns)):
        _raise_if_cancelled(context)
        ai_params = {
            "model": model,
            "messages": working_messages,
            "tools": tools,
            "params": params,
        }
        debug_request_path = _log_ai_debug_request(
            context,
            model=model,
            messages=working_messages,
            tools=tools,
            params=params,
            step_index=step_index + 1,
        )
        if debug_request_path:
            debug_logs.append(debug_request_path)
            _append_event(
                events,
                context,
                _event(
                    "status",
                    "AI debug log を保存しました",
                    phase="ai_debug",
                    debug_log_path=debug_request_path,
                    step_index=step_index + 1,
                ),
            )
        try:
            response = _call_ai_complete_with_retry(
                model,
                working_messages,
                tools,
                params,
                call_handler,
                events,
                context,
                allow_retry=True,
            )
        except RuntimeError as exc:
            ai_error = str(exc)
            _append_ai_debug_response(
                debug_request_path,
                {
                    "status": "error",
                    "error": {
                        "message": ai_error,
                    },
                },
            )
            if tool_logs:
                response = _stop_after_tool_ai_error(events, context, ai_error)
                break
            raise
        _append_ai_debug_response(debug_request_path, response)
        _raise_if_cancelled(context)

        if not isinstance(response, dict):
            response = _ai_error_response(
                model,
                "AI provider returned an invalid response",
                params,
                events,
            )
        tool_uses = _tool_use_blocks(response)
        if not tool_uses and not _response_text(response).strip():
            retry_params = _params_without_thinking(params)
            if retry_params != params:
                retry_response = None
                retry_debug_path = _log_ai_debug_request(
                    context,
                    model=model,
                    messages=working_messages,
                    tools=tools,
                    params=retry_params,
                    step_index="{}-retry-no-thinking".format(step_index + 1),
                    reason="empty_response_retry_without_thinking",
                )
                if retry_debug_path:
                    debug_logs.append(retry_debug_path)
                    _append_event(
                        events,
                        context,
                        _event(
                            "status",
                            "AI debug log を保存しました",
                            phase="ai_debug",
                            debug_log_path=retry_debug_path,
                            step_index="{}-retry-no-thinking".format(step_index + 1),
                        ),
                    )
                if call_handler is not None:
                    retry_payload = {
                        "model": model,
                        "messages": working_messages,
                        "tools": tools,
                        "params": retry_params,
                    }
                    retry_response = call_handler("defaults.ai.complete", retry_payload)
                    if isinstance(retry_response, dict) and retry_response.get("status") == "ok":
                        retry_response = retry_response.get("data", {})
                else:
                    retry_response, ai_error = _ai_direct_complete(
                        model,
                        working_messages,
                        tools,
                        retry_params,
                    )
                    if ai_error is not None:
                        _append_ai_debug_response(retry_debug_path, {"status": "error", "error": ai_error})
                        retry_response = None
                _append_ai_debug_response(retry_debug_path, retry_response)
                if isinstance(retry_response, dict) and (
                    _response_text(retry_response).strip() or _tool_use_blocks(retry_response)
                ):
                    retry_metadata = dict(retry_response.get("metadata") or {})
                    retry_metadata["recovered_from_empty_response"] = True
                    retry_response["metadata"] = retry_metadata
                    response = retry_response
                    tool_uses = _tool_use_blocks(response)
        planned_tool_executions = len(tool_logs) + len(tool_uses or [])
        if tool_uses and limit is not None and planned_tool_executions > limit:
            response = {
                "content": [{"type": "text", "text": _tool_limit_message(limit, tool_uses)}],
                "finish_reason": "tool_call_limit",
                "usage": response.get("usage", {}) if isinstance(response, dict) else {},
                "metadata": {
                    "max_tool_calls_reached": True,
                    "tool_executions": len(tool_logs),
                    "pending_tool_uses": [
                        {
                            "name": str(block.get("name") or block.get("tool_name") or ""),
                            "id": str(block.get("id") or block.get("tool_call_id") or ""),
                        }
                        for block in tool_uses
                    ],
                },
            }
            _append_event(
                events,
                context,
                _event(
                    "status",
                    "tool call の上限に達したため停止しました",
                    phase="tool_call_limit",
                    tool_count=len(tool_logs),
                    max_tool_calls=limit,
                )
            )
            break
        if tool_uses and step_index + 1 >= emergency_budget.max_model_turns:
            response = {
                "content": [{"type": "text", "text": "内部の安全予算に達したため、状態を保存して一時停止しました。"}],
                "finish_reason": "paused_emergency_budget",
                "usage": {},
                "metadata": {
                    "emergency_budget": {
                        "paused": True,
                        "reason": "max_model_turns",
                        "model_turns": step_index + 1,
                    }
                },
            }
            _append_event(
                events,
                context,
                _event(
                    "run_paused_emergency",
                    "内部安全予算に達したため一時停止しました",
                    phase="run_paused_emergency",
                    model_turns=step_index + 1,
                    max_model_turns=emergency_budget.max_model_turns,
                ),
            )
            break
        if tool_uses and planned_tool_executions > emergency_budget.max_tool_executions:
            response = {
                "content": [{"type": "text", "text": "内部の安全予算に達したため、状態を保存して一時停止しました。"}],
                "finish_reason": "paused_emergency_budget",
                "usage": {},
                "metadata": {
                    "emergency_budget": {
                        "paused": True,
                        "reason": "max_tool_executions",
                        "tool_executions": len(tool_logs),
                    }
                },
            }
            _append_event(
                events,
                context,
                _event(
                    "run_paused_emergency",
                    "内部安全予算に達したため一時停止しました",
                    phase="run_paused_emergency",
                    tool_executions=len(tool_logs),
                    max_tool_executions=emergency_budget.max_tool_executions,
                ),
            )
            break
        if not tool_uses:
            break

        rejected_tool_call = _reject_unconnected_tool_use(tool_uses, connected_names)
        if rejected_tool_call is not None:
            rejected_tool_name, rejected_tool_call_id = rejected_tool_call
            response = _unconnected_tool_call_response(rejected_tool_name, rejected_tool_call_id, connected_names)
            _append_event(
                events,
                context,
                _event(
                    "tool_call_rejected",
                    "接続されていない tool call を拒否しました",
                    phase="tool_call_rejected",
                    tool_name=rejected_tool_name,
                    tool_call_id=rejected_tool_call_id,
                    connected_tools=sorted(str(name) for name in connected_names if name),
                )
            )
            break
        proposal_decision = loop_guard.inspect_proposal(tool_uses)
        if proposal_decision.kind == "duplicate_side_effect":
            response = {
                "content": [{"type": "text", "text": "同じ副作用を持つ操作が再提案されたため、実行前に停止しました。"}],
                "finish_reason": "duplicate_side_effect_guard",
                "usage": {},
                "metadata": {"loop_guard": proposal_decision.event_data()},
            }
            _append_event(
                events,
                context,
                _event(
                    "run_paused_loop",
                    "同じ副作用操作の再実行を防ぐため停止しました",
                    phase="run_paused_loop",
                    **proposal_decision.event_data(),
                ),
            )
            break

        _append_assistant_tool_use_message(working_messages, tool_uses)
        logs_before_cycle = len(tool_logs)
        for block in tool_uses:
            _raise_if_cancelled(context)
            tool_name = str(block.get("name") or block.get("tool_name") or "")
            if not tool_name:
                continue
            tool_call_id = str(block.get("id") or block.get("tool_call_id") or gen_id())
            arguments = _tool_arguments(block)
            _append_event(
                events,
                context,
                _event(
                    "tool_call_started",
                    "{} を使用中".format(tool_name),
                    phase="tool_call_started",
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    arguments=arguments,
                )
            )
            invoke_context = build_tool_execution_context(context or {}, tool_name, connected_names)
            if call_handler is not None:
                result = call_handler(
                    "defaults.tool.invoke",
                    {
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "context": invoke_context,
                    },
                )
            else:
                from domain.tool.executor import ToolExecutor

                executed = ToolExecutor().execute(tool_name, arguments, invoke_context)
                result = {"status": "ok", "data": executed}
            _raise_if_cancelled(context)
            log = {
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "arguments": _redact_sensitive_value(arguments),
                "result": _compact_tool_log_value(result),
                "timestamp": timestamp(),
            }
            tool_logs.append(log)
            result_summary = _tool_result_summary(tool_name, result)
            artifacts = _tool_result_artifacts(result)
            _append_event(
                events,
                context,
                _event(
                    "tool_call_completed",
                    result_summary or "{} の結果を受け取りました".format(tool_name),
                    phase="tool_call_completed",
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    is_error=_tool_result_is_error(result),
                    recovery_kind=_tool_result_recovery_kind(result),
                    result_summary=result_summary,
                    summary=result_summary,
                    result=_bounded_compact_tool_result(result, result_summary, artifacts),
                    artifacts=artifacts,
                    artifact_paths=[artifact.get("path") for artifact in artifacts if artifact.get("path")],
                )
            )
            _append_tool_result_message(
                working_messages,
                tool_name,
                result,
                tool_call_id,
                model=model,
            )
            recovery_kind = _tool_result_recovery_kind(result)
            if recovery_kind in {"visible_window_required", "focus_required", "foreground_confirmation_required"}:
                blocked_response = _tool_blocked_response(tool_name, result)
                _append_event(
                    events,
                    context,
                    _event(
                        "status",
                        (
                            "foreground 実行の確認待ちです"
                            if recovery_kind == "foreground_confirmation_required"
                            else "可視画面外の tool 実行要求のため停止しました"
                        ),
                        phase="tool_blocked",
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        recovery_kind=recovery_kind,
                    )
                )
                break
        if blocked_response is not None:
            response = blocked_response
            break
        new_tool_logs = tool_logs[logs_before_cycle:]
        if new_tool_logs:
            observation = build_loop_observation(tool_uses=tool_uses, tool_logs=new_tool_logs, response=response)
            loop_decision = loop_guard.observe_cycle(observation)
            if loop_decision.kind == "recover":
                working_messages.append(
                    {
                        "role": "system",
                        "content": (
                            "[RUNTIME LOOP RECOVERY DIRECTIVE - protected]\n"
                            "A no-progress tool loop was detected. Continue with a different strategy. "
                            "This directive grants no new capabilities or approvals.\n"
                            f"reason: {loop_decision.reason}\n"
                            f"forbidden_action_signature: {loop_decision.directive.get('forbidden_action_signature')}"
                        ),
                    }
                )
                _append_event(
                    events,
                    context,
                    _event(
                        "loop_recovery_completed",
                        "作業内容を保持したまま、別方針で再開します",
                        phase="loop_recovery_completed",
                        **loop_decision.event_data(),
                    ),
                )
                continue
            if loop_decision.kind == "pause":
                response = {
                    "content": [
                        {
                            "type": "text",
                            "text": "同じパターンの自己回復が繰り返されたため、状態を保存して一時停止しました。",
                        }
                    ],
                    "finish_reason": "paused_loop",
                    "usage": {},
                    "metadata": {"loop_guard": loop_decision.event_data()},
                }
                _append_event(
                    events,
                    context,
                    _event(
                        "run_paused_loop",
                        "loop guard により一時停止しました",
                        phase="run_paused_loop",
                        **loop_decision.event_data(),
                    ),
                )
                break

    response = response or _ai_error_response(
        model,
        "AI provider did not return a response",
        params,
        events,
    )
    if not _tool_use_blocks(response) and not _response_text(response).strip():
        content = response.get("content")
        if not isinstance(content, list):
            content = []
        response["content"] = [{"type": "text", "text": _empty_response_message(response.get("finish_reason"))}]
        metadata = dict(response.get("metadata") or {})
        metadata["empty_ai_response"] = True
        response["metadata"] = metadata
    existing_events = response.get("events", [])
    response["events"] = events + (existing_events if isinstance(existing_events, list) else [])
    response["tool_logs"] = tool_logs
    metadata = dict(response.get("metadata", {}))
    metadata.update(
        {
            "model": model,
            "attached_tool_count": len(tools),
            "attached_tools": [tool_name_from_definition(tool) for tool in tools if tool_name_from_definition(tool)],
            "thinking": {"state": "completed"},
            "thinking_level": params.get("thinking_level"),
            "deepthink_enabled": bool(params.get("deepthink_enabled")),
        }
    )
    if debug_logs:
        metadata["ai_debug"] = {
            "enabled": True,
            "request_logs": debug_logs,
        }
    response["metadata"] = metadata
    return response


def _attachment_text_blocks(attachments):
    if not isinstance(attachments, list):
        return []

    blocks = []
    remaining = MAX_ATTACHMENT_TEXT_CHARS
    for attachment in attachments:
        if remaining <= 0:
            break
        if not isinstance(attachment, dict):
            continue
        text = attachment.get("content")
        if not isinstance(text, str) or not text:
            continue

        limit = min(MAX_ATTACHMENT_TEXT_CHARS_PER_FILE, remaining)
        clipped = text[:limit]
        was_truncated = len(text) > limit or attachment.get("truncated") is True
        remaining -= len(clipped)

        name = attachment.get("name")
        if not isinstance(name, str) or not name.strip():
            name = "unnamed"
        name = name.strip()[:200]

        suffix = "\n..." if was_truncated else ""
        blocks.append(
            {
                "type": "text",
                "text": "\n\n添付ファイル: {}\n```\n{}{}\n```".format(name, clipped, suffix),
            }
        )
    return blocks


def _attachment_image_blocks(attachments):
    if not isinstance(attachments, list):
        return []

    blocks = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        mime = str(attachment.get("type") or "").lower()
        data_url = attachment.get("dataUrl") or attachment.get("data_url")
        byte_length = _image_data_url_byte_length(data_url)
        if not mime.startswith("image/") or byte_length is None:
            continue
        size = attachment.get("size")
        if isinstance(size, int) and size > MAX_ATTACHMENT_IMAGE_BYTES:
            continue
        if byte_length > MAX_ATTACHMENT_IMAGE_BYTES:
            continue
        blocks.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": data_url,
                },
            }
        )
    return blocks


def _sanitize_attachment_metadata(attachments):
    if not isinstance(attachments, list):
        return attachments
    sanitized = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        sanitized.append(
            {
                key: attachment.get(key)
                for key in ("id", "name", "size", "type", "truncated", "source", "sourcePath")
                if key in attachment
            }
        )
    return sanitized


def run(input_data, context):
    from domain.chat.run_request import validate_chat_run_input
    from domain.chat.idempotency import (
        IdempotencyConflictError,
        reserve_chat_operation,
    )
    from domain.chat.stream_engine import ChatRunEngine
    from domain.stream.events import to_legacy_chat_stream_event

    validation_error = validate_chat_run_input(input_data if isinstance(input_data, dict) else {})
    if validation_error:
        return error(validation_error, "INVALID_INPUT")

    try:
        engine_context = reserve_chat_operation(input_data, context)
    except IdempotencyConflictError as exc:
        response = error(str(exc), "IDEMPOTENCY_CONFLICT")
        response["_http_status"] = 409
        return response
    except ValueError as exc:
        response = error(str(exc), "INVALID_INPUT")
        response["_http_status"] = 400
        return response
    reservation = engine_context.get("_chat_idempotency_reservation")
    claim = reservation.get("claim") if isinstance(reservation, dict) else None
    if (
        getattr(claim, "status", "") == "in_progress"
        and getattr(claim, "state", "") == "replay"
    ):
        response = error(
            "This chat operation is already in progress",
            "IDEMPOTENCY_IN_PROGRESS",
        )
        response["_http_status"] = 409
        return response

    final_message = None
    try:
        streaming_callback = (
            context.get("stream_event_callback")
            if isinstance(context, dict)
            else None
        )
        use_stream_adapter = callable(streaming_callback) and callable((context or {}).get("is_cancelled"))
        callback_passthrough_types = {
            "status",
            "tool_call_started",
            "tool_call_delta",
            "tool_call_completed",
            "browser_state_invalidated",
            "browser_state_snapshot",
            "browser_dom_snapshot",
            "browser_screenshot",
            "approval_requested",
            "ai_retry_scheduled",
            "task_failed",
        }
        engine_context.setdefault("run_source", "blocks.chat.send")
        for event in ChatRunEngine().stream(input_data, engine_context, stream_mode=use_stream_adapter):
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "").strip()
            if use_stream_adapter:
                legacy = to_legacy_chat_stream_event(event)
                if legacy is not None and legacy.get("type") in callback_passthrough_types:
                    try:
                        streaming_callback(legacy)
                    except Exception:
                        pass
            if event_type in {"assistant_message_completed", "done"}:
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                message = data.get("message")
                if isinstance(message, dict):
                    final_message = message
            elif event_type == "error":
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                err = data.get("error") if isinstance(data.get("error"), dict) else {}
                if isinstance(err, dict):
                    return error(str(err.get("message") or "AI request failed"), str(err.get("code") or "INTERNAL_ERROR"))
                return error("AI request failed", "INTERNAL_ERROR")
    except ValueError as exc:
        message = str(exc)
        code = "NOT_FOUND" if "not found" in message.lower() else "INVALID_INPUT"
        return error(message, code)
    except Exception as exc:
        return error("AI request failed: " + str(exc), "INTERNAL_ERROR")

    if final_message is not None:
        return ok(final_message)
    return error("Chat run ended without final message", "INTERNAL_ERROR")
