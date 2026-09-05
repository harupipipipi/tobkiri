from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict

from blocks._common import ok, error
from blocks.integrations.common import allow_unsigned_webhook_dev, headers_from_request, raw_body_bytes, text_limit
from domain.external.adapters.line import LineResponseAdapter
from domain.external.audience_policy import AudiencePolicy
from domain.external.audience_policy_registry import AudiencePolicyRegistry
from domain.external.normalizer import normalize_line_event
from domain.external.pipeline import dispatch_external_event
from domain.external.response import RumiResponse
from domain.external.response_planner import ResponsePlanner
from domain.external.source_store import ExternalSourceStore
from domain.external.targeting import origin_from_external_event
from domain.frontend_settings import frontend_settings_path
from domain.integrations.http_client import post_json
from domain.integrations.secrets import get_integration_secret, load_integration_secrets_into_env
from domain.integrations.line.addressing import decide_line_addressing
from domain.webhook.endpoint import WebhookEndpoint
from domain.webhook.endpoint_resolver import ProviderEndpointResolver


_LOGGER = logging.getLogger(__name__)
_LINE_WEBHOOK_ACK_TEXT = "\u5c4a\u3044\u305f\u3088\uff01"
_LINE_REPLY_DEADLINE_SECONDS = 60
_LINE_REPLY_DEADLINE_PROMPT = (
    "LINE reply tokens expire about 1 minute after the webhook event. "
    "Keep the answer concise, finish within that deadline, and use the LINE reply response path when available."
)


def run(input_data, context):
    load_integration_secrets_into_env()
    raw_body = raw_body_bytes(input_data)
    endpoint_input = {} if _has_raw_body(input_data) else input_data
    endpoint = ProviderEndpointResolver().resolve("line", endpoint_input)
    if endpoint is None:
        return {**error("LINE webhook endpoint not found", "WEBHOOK_ENDPOINT_NOT_FOUND"), "_http_status": 404}
    if not endpoint.enabled:
        return {**error("LINE webhook endpoint disabled", "WEBHOOK_ENDPOINT_DISABLED"), "_http_status": 403}

    headers = headers_from_request(input_data)
    security = endpoint.security if isinstance(endpoint.security, dict) else {}
    verification = {"ok": True, "verified": False, "reason": "provider signature disabled"}
    if str(security.get("mode") or "provider_signature") != "none":
        verification = _verify_line(headers, raw_body)
    if not verification["ok"]:
        return {**error(verification["reason"], "SIGNATURE_INVALID"), "_http_status": 401}
    request_payload, parse_error = _payload_from_raw_body(input_data, raw_body)
    if parse_error:
        return {**error(parse_error, "INVALID_LINE_BODY"), "_http_status": 400}

    events = request_payload.get("events") if isinstance(request_payload.get("events"), list) else []
    results = []
    destination = str(request_payload.get("destination") or "")
    model = str(request_payload.get("model") or endpoint.conversation.get("model") or "") or None
    for event in events:
        if not isinstance(event, dict):
            continue
        result = _handle_event(
            event,
            context,
            model=model,
            verified=bool(verification["verified"]),
            destination=destination,
            endpoint=endpoint,
        )
        results.append(result)
    return ok({"verified": verification["verified"], "endpoint": endpoint.as_dict(), "events": results})


def _handle_event(
    event: Dict[str, Any],
    context,
    *,
    model: str | None = None,
    verified: bool = False,
    destination: str = "",
    endpoint: WebhookEndpoint,
) -> Dict[str, Any]:
    if event.get("type") != "message":
        return {"ignored": True, "reason": "unsupported LINE event", "event_type": event.get("type")}
    external_event = normalize_line_event(event, verified=verified, destination=destination)
    if model:
        external_event.metadata["model"] = model
    mentioned = _line_message_mentions_bot(event, destination=destination)
    require_group_mention = _require_line_group_mention(endpoint, external_event)
    addressing = decide_line_addressing(
        event,
        external_event,
        endpoint=endpoint,
        mentioned=mentioned,
    )
    addressed = bool(addressing.get("addressed"))
    external_event.metadata["line_mention"] = {
        "mentioned": mentioned,
        "require_group_mention": require_group_mention,
        "addressed": addressed,
    }
    external_event.metadata["line_addressing"] = addressing
    origin = origin_from_external_event(external_event)
    source_record = ExternalSourceStore().record_origin(origin, verified=verified)
    external_event.metadata["origin"] = origin.as_dict()
    external_event.metadata["source_record"] = source_record
    runtime_context = dict(context or {})
    _apply_external_output_context(runtime_context)
    runtime_context.setdefault("webhook_endpoint", endpoint.as_dict())
    runtime_context.setdefault("output_profile_id", endpoint.response_profile_id)
    runtime_context.setdefault("response_profile_id", endpoint.response_profile_id)
    runtime_context.setdefault("conversation", dict(endpoint.conversation))
    runtime_context.setdefault("source_record", source_record)
    runtime_context = _apply_endpoint_response_context(runtime_context, endpoint)
    runtime_context = _apply_line_reply_deadline_context(runtime_context, origin)
    policy = AudiencePolicyRegistry().resolve(endpoint.audience_policy_id, event=external_event)
    if require_group_mention and not addressed:
        return _line_addressing_ignored_result(external_event, addressing)
    if require_group_mention:
        policy = _require_audience_mention(policy)
        source = source_record.get("source") if isinstance(source_record, dict) else None
        if isinstance(source, dict) and source.get("enabled"):
            policy = _allow_current_scope(policy, external_event)
    decision = AudiencePolicy(policy).evaluate(external_event, mentioned=addressed if require_group_mention else mentioned)
    if not decision.allowed:
        return _policy_denied_result(external_event, decision)
    acknowledgement = _send_line_webhook_acknowledgement(event, endpoint=endpoint)
    runtime_context.setdefault("line_webhook_acknowledgement", acknowledgement)
    if _should_process_line_event_in_background(endpoint):
        return _with_line_acknowledgement(_dispatch_line_event_in_background(
            external_event,
            input_profile_id=endpoint.input_profile_id,
            audience_policy=policy,
            audience_decision=decision,
            context=runtime_context,
            mentioned=addressed if require_group_mention else mentioned,
        ), acknowledgement)
    return _with_line_acknowledgement(_dispatch_line_event(
        external_event,
        input_profile_id=endpoint.input_profile_id,
        audience_policy=policy,
        audience_decision=decision,
        context=runtime_context,
        mentioned=addressed if require_group_mention else mentioned,
    ), acknowledgement)


def _dispatch_line_event(
    external_event,
    *,
    input_profile_id: str,
    audience_policy: dict[str, Any],
    audience_decision,
    context: dict[str, Any],
    mentioned: bool = False,
) -> Dict[str, Any]:
    result = dispatch_external_event(
        external_event,
        input_profile_id=input_profile_id,
        audience_policy=audience_policy,
        audience_decision=audience_decision,
        context=context,
        send_response=True,
        mentioned=mentioned,
    )
    plan = result.get("response_plan") if isinstance(result.get("response_plan"), dict) else ResponsePlanner("line").plan(RumiResponse.from_result(result))
    reply = _send_response_plan(plan, external_event, context=context)
    return {**result, "reply": reply}


def _dispatch_line_event_in_background(
    external_event,
    *,
    input_profile_id: str,
    audience_policy: dict[str, Any],
    audience_decision,
    context: dict[str, Any],
    mentioned: bool = False,
) -> Dict[str, Any]:
    event_id = str((external_event.event or {}).get("id") or "").strip()
    background_context = dict(context or {})
    background_context["line_background_processing"] = True

    def worker() -> None:
        try:
            _dispatch_line_event(
                external_event,
                input_profile_id=input_profile_id,
                audience_policy=audience_policy,
                audience_decision=audience_decision,
                context=background_context,
                mentioned=mentioned,
            )
        except Exception:
            _LOGGER.exception("LINE background event processing failed event_id=%s", event_id or "<missing>")

    name_suffix = event_id or str(os.getpid())
    thread = threading.Thread(target=worker, name=f"line-webhook-{name_suffix}", daemon=True)
    thread.start()
    return {
        "status": "accepted",
        "assistant_text": "",
        "background_processing": True,
        "event_id": event_id,
        "event": external_event.as_dict(),
        "policy": audience_decision.as_dict() if hasattr(audience_decision, "as_dict") else audience_decision,
        "input_profile_id": input_profile_id,
        "reply": {"sent": False, "reason": "LINE event accepted for background processing"},
    }


def _send_response_plan(plan: dict[str, Any], external_event, *, context: dict[str, Any] | None = None) -> Dict[str, Any]:
    acknowledgement = context.get("line_webhook_acknowledgement") if isinstance(context, dict) else {}
    if isinstance(acknowledgement, dict) and acknowledgement.get("sent") is True:
        return {"sent": False, "reason": "LINE reply token already used for webhook acknowledgement"}
    action_plan = (plan.get("metadata") or {}).get("response_action_plan") if isinstance(plan.get("metadata"), dict) else {}
    if isinstance(action_plan, dict) and not action_plan.get("external_reply", True):
        return {"sent": False, "reason": "external reply suppressed by response prompt policy"}
    return LineResponseAdapter().send(plan, event=external_event, context=context)


def _payload_from_raw_body(input_data, raw_body: bytes) -> tuple[dict[str, Any], str]:
    if not _has_raw_body(input_data):
        return (input_data if isinstance(input_data, dict) else {}), ""
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}, "invalid LINE JSON body"
    if not isinstance(payload, dict):
        return {}, "LINE JSON body must be an object"
    return payload, ""


def _has_raw_body(input_data) -> bool:
    return isinstance(input_data, dict) and ("_raw_body_base64" in input_data or "_raw_body" in input_data)


def _apply_external_output_context(runtime_context: dict[str, Any]) -> None:
    output = _frontend_external_output_settings()
    send_mode = str(output.get("output_send_mode") or output.get("send_mode") or "").strip()
    if send_mode:
        runtime_context.setdefault("send_mode", send_mode)
        runtime_context.setdefault("line_send_mode", send_mode)
    output_profile_id = str(output.get("output_profile_id") or "").strip()
    if output_profile_id:
        runtime_context.setdefault("output_profile_id", output_profile_id)
        runtime_context.setdefault("response_profile_id", output_profile_id)
    target_id = str(output.get("output_target_id") or "").strip()
    if target_id:
        runtime_context.setdefault("target_id", target_id)
        runtime_context.setdefault("line_target_id", target_id)


def _frontend_external_output_settings() -> dict[str, Any]:
    path = _frontend_settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    output = data.get("external_output") if isinstance(data.get("external_output"), dict) else {}
    return dict(output)


def _frontend_settings_path() -> Path:
    return frontend_settings_path()


def _policy_denied_result(external_event, decision) -> Dict[str, Any]:
    return {
        "status": "denied",
        "assistant_text": "",
        "policy": decision.as_dict(),
        "event": external_event.as_dict(),
        "reply": {"sent": False, "reason": "audience policy denied"},
    }


def _line_addressing_ignored_result(external_event, addressing: dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "ignored",
        "assistant_text": "",
        "reason": "LINE group/room message was not addressed to Rumi",
        "event": external_event.as_dict(),
        "line_addressing": dict(addressing or {}),
        "reply": {"sent": False, "reason": "message not addressed to Rumi"},
    }


def _verify_line(headers: Dict[str, str], raw_body: bytes) -> Dict[str, Any]:
    secret = get_integration_secret("line", "LINE_CHANNEL_SECRET")
    if not secret:
        if allow_unsigned_webhook_dev():
            return {"ok": True, "verified": False, "reason": "unsigned dev mode enabled"}
        return {"ok": False, "verified": False, "reason": "LINE channel secret not configured"}
    signature = headers.get("x-line-signature", "")
    if not signature:
        return {"ok": False, "verified": False, "reason": "missing LINE signature header"}
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    if not hmac.compare_digest(expected, signature):
        return {"ok": False, "verified": False, "reason": "LINE signature mismatch"}
    return {"ok": True, "verified": True, "reason": ""}


def _send_line_reply(reply_token: str, text: str) -> Dict[str, Any]:
    return LineResponseAdapter().send_text_reply(reply_token, text_limit(text, 5000))


def _send_line_webhook_acknowledgement(event: dict[str, Any], *, endpoint: WebhookEndpoint) -> Dict[str, Any]:
    if not _line_webhook_ack_enabled(endpoint):
        return {"sent": False, "reason": "LINE webhook acknowledgement disabled"}
    reply_token = str(event.get("replyToken") or "").strip()
    if not reply_token:
        return {"sent": False, "reason": "missing reply token"}
    result = _send_line_reply(reply_token, _LINE_WEBHOOK_ACK_TEXT)
    return {
        **result,
        "text": _LINE_WEBHOOK_ACK_TEXT,
    }


def _with_line_acknowledgement(result: Dict[str, Any], acknowledgement: dict[str, Any]) -> Dict[str, Any]:
    return {**result, "acknowledgement": acknowledgement}


def _line_webhook_ack_enabled(endpoint: WebhookEndpoint) -> bool:
    response = endpoint.response if isinstance(endpoint.response, dict) else {}
    mode = str(response.get("mode") or "").strip().lower()
    if mode != "computer_use_line_biz":
        return False
    configured = None
    for key in ("reply_on_receive", "acknowledge_on_receive", "send_webhook_acknowledgement"):
        if key in response:
            configured = response.get(key)
            break
    return True if configured is None else _truthy(configured)


def _apply_endpoint_response_context(runtime_context: dict[str, Any], endpoint: WebhookEndpoint) -> dict[str, Any]:
    updated = dict(runtime_context or {})
    response = endpoint.response if isinstance(endpoint.response, dict) else {}
    if not response:
        return updated

    mode = str(response.get("mode") or "").strip().lower()
    history_mode = str(
        response.get("chat_history_mode")
        or response.get("external_chat_history_mode")
        or ""
    ).strip().lower()
    if history_mode:
        updated.setdefault("external_chat_history_mode", history_mode)
    elif mode == "computer_use_line_biz":
        updated.setdefault("external_chat_history_mode", "current_turn")

    prompt_prefix = str(
        response.get("prompt_prefix")
        or response.get("instruction_prefix")
        or response.get("computer_use_prompt")
        or _line_biz_prompt_prefix(response, mode=mode)
        or ""
    ).strip()
    if prompt_prefix:
        updated.setdefault("external_prompt_prefix", prompt_prefix)

    prompt_suffix = str(
        response.get("prompt_suffix")
        or response.get("instruction_suffix")
        or ""
    ).strip()
    if prompt_suffix:
        updated.setdefault("external_prompt_suffix", prompt_suffix)

    target_app = str(
        response.get("target_app")
        or response.get("computer_use_target_app")
        or ("Google Chrome" if mode == "computer_use_line_biz" else "")
        or ""
    ).strip()
    if target_app:
        updated.setdefault("computer_use_target_app", target_app)

    target_title = str(
        response.get("target_title")
        or response.get("computer_use_target_title")
        or ("LINE Chat" if mode == "computer_use_line_biz" else "")
        or ""
    ).strip()
    if target_title:
        updated.setdefault("computer_use_target_title", target_title)
    if mode == "computer_use_line_biz":
        updated.setdefault("computer_use_physical_clicks", True)
        updated.setdefault("computer_use_reply_surface", "line_biz")

    tool_policy = dict(updated.get("profile_policy") if isinstance(updated.get("profile_policy"), dict) else {})
    response_tool_policy = response.get("tool_policy") if isinstance(response.get("tool_policy"), dict) else {}
    if response_tool_policy:
        tool_policy.update(response_tool_policy)
    # LINE webhook events are external, remote-origin input.  Endpoint response
    # configuration must not translate "auto approve" aliases into yolo_mode
    # for computer/browser tools, because yolo_mode bypasses the local approval
    # token checks for screenshots and desktop input.  Operators can still set
    # other explicit tool_policy fields above, but inbound LINE response presets
    # cannot auto-approve computer use on behalf of a remote sender.
    if tool_policy:
        updated["profile_policy"] = tool_policy

    if _truthy(response.get("user_requested_computer_use")) or target_app or target_title or prompt_prefix:
        updated.setdefault("user_requested_computer_use", True)

    if _suppress_provider_reply(response):
        updated.setdefault(
            "response_prompt_decision",
            {
                "action": "store_only",
                "reason": "provider reply suppressed by LINE endpoint response settings",
                "sensitivity": "local_only",
                "metadata": {
                    "source": "line_endpoint_response",
                    "mode": str(response.get("mode") or ""),
                },
            },
        )

    return updated


def _apply_line_reply_deadline_context(runtime_context: dict[str, Any], origin) -> dict[str, Any]:
    updated = dict(runtime_context or {})
    if not getattr(origin, "can_reply", False):
        return updated
    updated.setdefault("line_reply_deadline_seconds", _LINE_REPLY_DEADLINE_SECONDS)
    expires_at = getattr(origin, "reply_expires_at_ms", None)
    if isinstance(expires_at, int):
        updated.setdefault("line_reply_expires_at_ms", expires_at)
    existing_suffix = str(updated.get("external_prompt_suffix") or "").strip()
    if _LINE_REPLY_DEADLINE_PROMPT not in existing_suffix:
        updated["external_prompt_suffix"] = (
            existing_suffix + "\n" + _LINE_REPLY_DEADLINE_PROMPT
            if existing_suffix
            else _LINE_REPLY_DEADLINE_PROMPT
        )
    return updated


def _should_process_line_event_in_background(endpoint: WebhookEndpoint) -> bool:
    response = endpoint.response if isinstance(endpoint.response, dict) else {}
    if not response:
        return False
    mode = str(response.get("mode") or "").strip().lower()
    if mode != "computer_use_line_biz":
        return False
    return any(
        _truthy(response.get(key))
        for key in ("background_processing", "async_processing", "run_in_background")
    )


def _line_biz_prompt_prefix(response: dict[str, Any], *, mode: str = "") -> str:
    resolved_mode = (mode or str(response.get("mode") or "")).strip().lower()
    if resolved_mode != "computer_use_line_biz":
        return ""
    chat_url = str(
        response.get("line_biz_chat_url")
        or response.get("chat_url")
        or response.get("computer_use_target_url")
        or ""
    ).strip()
    if not chat_url:
        return ""
    reply_language = str(
        response.get("line_biz_reply_language")
        or response.get("reply_language")
        or "Japanese"
    ).strip()
    return (
        "Use computer_use in Google Chrome to open "
        f"{chat_url} and reply in {reply_language} inside LINE Official Account Manager. "
        "Before using any tools, decide the exact reply text from the external source message in this prompt. "
        "If the source message says to reply exactly with some text, send exactly that text and nothing else. "
        "Treat the visible LINE Biz chat history only as the destination UI; it can be stale or unrelated to this webhook event. "
        "Do not inspect, reread, or scroll visible chat bubbles to understand the customer request. "
        "Start by checking computer.windows, and if a visible Google Chrome LINE window exists, "
        "target it with computer.select_window before screenshots or clicks. "
        "This Windows workflow only works against a visible desktop Chrome window, so if Chrome is "
        "not visible return a short local note asking for the LINE Biz window to be opened on screen. "
        "The external source message below is already the customer message you should answer. "
        "Before typing, pressing Enter, or sending, call computer.context or inspect active_window in the latest "
        "screenshot result to confirm the foreground window is the Chrome LINE chat; if Codex or another app is frontmost, "
        "refocus the LINE window with computer.select_window before continuing. "
        "After the target chat is visible, use screenshots only to locate the reply composer or send control near the bottom of the chat pane. "
        "If the reply composer is hidden, scroll toward the bottom once and click the large red circular reply button near the lower edge to open it. "
        "Any click that must affect LINE Biz must be a physical foreground click: call computer.click with physical=true. "
        "A normal computer.click is only a virtual cursor marker and will not open the composer or press Send. "
        "Do not use Ctrl+A or select existing chat text. "
        "If the exact reply text is already visible in the composer, do not type it again. "
        "To send, click the left green Send button labeled 送信, not the small dropdown arrow on its right. "
        "Do not keep scrolling through the transcript repeatedly; after one bottom scroll, use a physical click to focus the composer/reply button. "
        "Then answer the external source message clearly, "
        "send the message in LINE Biz, and only after the send succeeds return a short local confirmation."
    )


def _suppress_provider_reply(response: dict[str, Any]) -> bool:
    if _truthy(response.get("suppress_provider_reply")):
        return True
    mode = str(response.get("mode") or "").strip().lower()
    return mode in {
        "store_only",
        "local_only",
        "web_local",
        "tool_only",
        "computer_use_line_biz",
        "computer_use_only",
    }


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _line_message_mentions_bot(event: dict[str, Any], *, destination: str = "") -> bool:
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    mention = message.get("mention") if isinstance(message.get("mention"), dict) else {}
    mentionees = mention.get("mentionees") if isinstance(mention.get("mentionees"), list) else []
    destination_id = str(destination or "").strip()
    for item in mentionees:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").strip() != "user":
            continue
        if bool(item.get("isSelf")):
            return True
        if destination_id and str(item.get("userId") or "").strip() == destination_id:
            return True
    return False


def _require_line_group_mention(endpoint: WebhookEndpoint, external_event) -> bool:
    if getattr(external_event, "scope", None) is None or external_event.scope.type not in {"group", "room"}:
        return False
    response = endpoint.response if isinstance(endpoint.response, dict) else {}
    conversation = endpoint.conversation if isinstance(endpoint.conversation, dict) else {}
    metadata = endpoint.metadata if isinstance(endpoint.metadata, dict) else {}
    configured = None
    for container in (metadata, response, conversation):
        for key in ("require_group_mention", "group_mention_only", "mention_only_in_groups", "group_room_mention_required"):
            if key in container:
                configured = container.get(key)
                break
        if configured is not None:
            break
    if configured is None:
        configured = _line_mention_policy_default()
    if configured is None:
        configured = True
    return _truthy(configured)


def _line_mention_policy_default() -> Any:
    try:
        path = _frontend_settings_path()
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return True
    if not isinstance(data, dict):
        return True
    line_settings = data.get("line") if isinstance(data.get("line"), dict) else {}
    policy = line_settings.get("mention_policy")
    if isinstance(policy, dict):
        for key in ("group_room_mention_required", "require_group_mention", "groups_require_mention"):
            if key in policy:
                return policy.get(key)
    if policy is not None:
        text = str(policy).strip().lower()
        if text in {"mention_required", "groups_only", "group_room", "true", "on", "1"}:
            return True
        if text in {"always", "all", "false", "off", "0"}:
            return False
    return True


def _require_audience_mention(policy: dict[str, Any]) -> dict[str, Any]:
    updated = dict(policy or {})
    require = dict(updated.get("require") if isinstance(updated.get("require"), dict) else {})
    require["mention"] = True
    updated["require"] = require
    return updated


def _allow_current_scope(policy: dict[str, Any], external_event) -> dict[str, Any]:
    scope = getattr(external_event, "scope", None)
    if scope is None or scope.type not in {"group", "room"} or not scope.id:
        return dict(policy or {})
    updated = dict(policy or {})
    allow = list(updated.get("allow")) if isinstance(updated.get("allow"), list) else []
    scope_rule = {
        "id": f"mentioned-scope:{scope.type}:{scope.id}",
        "provider": external_event.provider,
        "scope": {"type": scope.type, "id": scope.id},
    }
    if not any(
        isinstance(rule, dict)
        and rule.get("provider") == scope_rule["provider"]
        and isinstance(rule.get("scope"), dict)
        and str(rule["scope"].get("type") or "") == scope.type
        and str(rule["scope"].get("id") or "") == scope.id
        for rule in allow
    ):
        allow.append(scope_rule)
    updated["allow"] = allow
    return updated
