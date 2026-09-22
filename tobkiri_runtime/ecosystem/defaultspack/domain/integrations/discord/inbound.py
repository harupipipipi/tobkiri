from __future__ import annotations

from typing import Any, Dict

from tobkiri_protocol.settings_state import SettingsOwnerPort

from blocks._common import ok, error
from blocks.integrations.common import allow_unsigned_webhook_dev, headers_from_request, raw_body_bytes, text_limit
from domain.external.adapters.discord import DiscordResponseAdapter
from domain.external.normalizer import normalize_discord_interaction, normalize_discord_message
from domain.external.pipeline import dispatch_external_event
from domain.external.response import RumiResponse
from domain.external.response_planner import ResponsePlanner
from domain.integrations.http_client import post_json
from domain.integrations.secrets import get_integration_secret, load_integration_secrets_into_env


DISCORD_PING = 1
DISCORD_APPLICATION_COMMAND = 2
DISCORD_MESSAGE_WITH_SOURCE = 4
DISCORD_DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE = 5


def run(input_data, context, *, settings_owner: SettingsOwnerPort | None = None):
    settings_owner = (
        settings_owner
        if settings_owner is not None
        else (context or {}).get("_settings_owner_port")
    )
    load_integration_secrets_into_env()
    headers = headers_from_request(input_data)
    raw_body = raw_body_bytes(input_data)
    verification = _verify_discord(headers, raw_body)
    if not verification["ok"]:
        return {**error(verification["reason"], "SIGNATURE_INVALID"), "_http_status": 401}

    payload_type = input_data.get("type")
    if payload_type == DISCORD_PING:
        return {"type": DISCORD_PING}

    if payload_type == DISCORD_APPLICATION_COMMAND:
        result = _handle_interaction(
            input_data,
            context,
            verified=bool(verification["verified"]),
            **({"settings_owner": settings_owner} if settings_owner is not None else {}),
        )
        if not _interaction_external_reply_enabled(result):
            return {
                "type": DISCORD_DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE,
                "rumi": {key: value for key, value in result.items() if key != "assistant_text"},
                "verified": verification["verified"],
            }
        return {
            "type": DISCORD_MESSAGE_WITH_SOURCE,
            "data": {
                "content": text_limit(_interaction_response_text(result), 2000),
                "allowed_mentions": {"parse": []},
            },
            "rumi": {key: value for key, value in result.items() if key != "assistant_text"},
            "verified": verification["verified"],
        }

    if str(input_data.get("t") or "").upper() == "MESSAGE_CREATE" or input_data.get("content"):
        result = _handle_message_create(
            input_data,
            context,
            verified=bool(verification["verified"]),
            **({"settings_owner": settings_owner} if settings_owner is not None else {}),
        )
        return ok({**result, "verified": verification["verified"]})

    return ok({"ignored": True, "reason": "unsupported discord payload", "verified": verification["verified"]})


def _handle_interaction(
    input_data: Dict[str, Any],
    context,
    *,
    verified: bool = False,
    settings_owner: SettingsOwnerPort | None = None,
) -> Dict[str, Any]:
    external_event = normalize_discord_interaction(input_data, verified=verified)
    return dispatch_external_event(
        external_event,
        input_profile_id="discord.default",
        audience_policy={"default": "allow"},
        context=context,
        send_response=True,
        **({"settings_owner": settings_owner} if settings_owner is not None else {}),
    )


def _handle_message_create(
    input_data: Dict[str, Any],
    context,
    *,
    verified: bool = False,
    settings_owner: SettingsOwnerPort | None = None,
) -> Dict[str, Any]:
    data = input_data.get("d") if isinstance(input_data.get("d"), dict) else input_data
    author = data.get("author") if isinstance(data.get("author"), dict) else {}
    if author.get("bot"):
        return {"status": "ignored", "reason": "bot message", "assistant_text": ""}
    channel_id = str(data.get("channel_id") or input_data.get("channel_id") or "")
    external_event = normalize_discord_message(input_data, verified=verified)
    result = dispatch_external_event(
        external_event,
        input_profile_id="discord.default",
        audience_policy={"default": "allow"},
        context=context,
        send_response=True,
        **({"settings_owner": settings_owner} if settings_owner is not None else {}),
    )
    plan = result.get("response_plan") if isinstance(result.get("response_plan"), dict) else ResponsePlanner("discord").plan(RumiResponse.from_result(result))
    reply = _send_response_plan(plan, external_event)
    return {**result, "reply": reply}


def _interaction_response_text(result: dict[str, Any]) -> str:
    plan = result.get("response_plan") if isinstance(result.get("response_plan"), dict) else {}
    messages = plan.get("messages") if isinstance(plan.get("messages"), list) else []
    for message in messages:
        if isinstance(message, dict) and str(message.get("text") or "").strip():
            return str(message.get("text") or "").strip()
    action_plan = (plan.get("metadata") or {}).get("response_action_plan") if isinstance(plan.get("metadata"), dict) else {}
    if isinstance(action_plan, dict) and not action_plan.get("external_reply", True):
        return "処理を受け付けました。"
    return str(result.get("assistant_text") or "応答を生成できませんでした。")


def _interaction_external_reply_enabled(result: dict[str, Any]) -> bool:
    plan = result.get("response_plan") if isinstance(result.get("response_plan"), dict) else {}
    action_plan = (plan.get("metadata") or {}).get("response_action_plan") if isinstance(plan.get("metadata"), dict) else {}
    return not isinstance(action_plan, dict) or bool(action_plan.get("external_reply", True))


def _send_response_plan(plan: dict[str, Any], external_event) -> Dict[str, Any]:
    action_plan = (plan.get("metadata") or {}).get("response_action_plan") if isinstance(plan.get("metadata"), dict) else {}
    if isinstance(action_plan, dict) and not action_plan.get("external_reply", True):
        return {"sent": False, "reason": "external reply suppressed by response prompt policy"}
    return DiscordResponseAdapter().send(plan, event=external_event)


def _interaction_text(data: Dict[str, Any]) -> str:
    options = data.get("options") if isinstance(data.get("options"), list) else []
    for option in options:
        if not isinstance(option, dict):
            continue
        value = option.get("value")
        if isinstance(value, str) and value.strip():
            return value.strip()
    name = str(data.get("name") or "").strip()
    return name or "hello"


def _interaction_user_id(input_data: Dict[str, Any]) -> str:
    member = input_data.get("member") if isinstance(input_data.get("member"), dict) else {}
    user = member.get("user") if isinstance(member.get("user"), dict) else input_data.get("user")
    if isinstance(user, dict):
        return str(user.get("id") or "")
    return ""


def _verify_discord(headers: Dict[str, str], raw_body: bytes) -> Dict[str, Any]:
    public_key = get_integration_secret("discord", "DISCORD_PUBLIC_KEY")
    if not public_key:
        if allow_unsigned_webhook_dev():
            return {"ok": True, "verified": False, "reason": "unsigned dev mode enabled"}
        return {"ok": False, "verified": False, "reason": "Discord public key not configured"}
    signature = headers.get("x-signature-ed25519", "")
    timestamp = headers.get("x-signature-timestamp", "")
    if not signature or not timestamp:
        return {"ok": False, "verified": False, "reason": "missing Discord signature headers"}
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key))
        key.verify(bytes.fromhex(signature), timestamp.encode("utf-8") + raw_body)
    except Exception:
        return {"ok": False, "verified": False, "reason": "Discord signature mismatch"}
    return {"ok": True, "verified": True, "reason": ""}


def _send_discord_channel_message(channel_id: str, text: str) -> Dict[str, Any]:
    return DiscordResponseAdapter().send_channel_message(channel_id, text_limit(text, 2000))
