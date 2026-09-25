from __future__ import annotations

from typing import Any

from domain.external.adapters.discord import DiscordResponseAdapter
from domain.external.adapters.line import LineResponseAdapter
from domain.external.adapters.slack import SlackResponseAdapter
from domain.integrations.http_client import post_json_public_url


def external_send_tool(
    arguments: dict[str, Any] | None, context: dict[str, Any] | None = None
) -> dict[str, Any]:
    args = arguments or {}
    provider = str(args.get("provider") or args.get("target_provider") or "").strip().lower()
    text = str(args.get("text") or args.get("message") or "").strip()
    dry_run = bool(args.get("dry_run"))
    if not provider:
        return _tool_error("provider is required")
    if not text:
        return _tool_error("text is required")

    safe_plan = {
        "provider": provider,
        "messages": [{"type": "text", "text": text}],
        "metadata": {
            "response_action_plan": {"type": "tool_external_send", "external_reply": True},
        },
        "safe_defaults": {"allowed_mentions": {"parse": []}} if provider == "discord" else {},
    }
    if dry_run:
        return _tool_ok(
            {
                "sent": False,
                "dry_run": True,
                "provider": provider,
                "plan": safe_plan,
                "target": _target_summary(provider, args),
            }
        )

    del context

    if provider == "line":
        adapter = LineResponseAdapter()
        reply_token = str(args.get("reply_token") or "").strip()
        target_id = str(
            args.get("target_id")
            or args.get("user_id")
            or args.get("group_id")
            or args.get("room_id")
            or ""
        ).strip()
        result = (
            adapter.send_text_reply(reply_token, text)
            if reply_token
            else adapter.send_text_push(target_id, text)
        )
        return _tool_ok({"provider": provider, **result})

    if provider == "discord":
        adapter = DiscordResponseAdapter()
        webhook_url = str(args.get("webhook_url") or "").strip()
        if webhook_url:
            result = adapter.send_webhook_message(webhook_url, text)
        else:
            result = adapter.send_channel_message(str(args.get("channel_id") or "").strip(), text)
        return _tool_ok({"provider": provider, **result})

    if provider == "slack":
        result = SlackResponseAdapter().send_channel_message(
            str(args.get("channel_id") or args.get("channel") or "").strip(),
            text,
            thread_ts=str(args.get("thread_ts") or "").strip(),
        )
        return _tool_ok({"provider": provider, **result})

    if provider in {"generic", "webhook", "web"}:
        callback_url = str(args.get("callback_url") or args.get("webhook_url") or "").strip()
        if not callback_url:
            return _tool_error("callback_url or webhook_url is required for generic/web output")
        result = post_json_public_url(
            callback_url,
            {},
            {
                "provider": provider,
                "text": text,
                "metadata": args.get("metadata") if isinstance(args.get("metadata"), dict) else {},
            },
        )
        return _tool_ok(
            {
                "provider": provider,
                "sent": bool(result.get("ok")),
                "provider_response": result,
            }
        )

    return _tool_error(f"unsupported provider: {provider}")


def _target_summary(provider: str, args: dict[str, Any]) -> dict[str, Any]:
    if provider == "discord":
        return {
            "channel_id": str(args.get("channel_id") or ""),
            "webhook_url_configured": bool(str(args.get("webhook_url") or "").strip()),
        }
    if provider == "line":
        return {
            "reply_token_configured": bool(str(args.get("reply_token") or "").strip()),
            "target_id": str(
                args.get("target_id")
                or args.get("user_id")
                or args.get("group_id")
                or args.get("room_id")
                or ""
            ),
        }
    if provider == "slack":
        return {
            "channel_id": str(args.get("channel_id") or args.get("channel") or ""),
            "thread_ts": str(args.get("thread_ts") or ""),
        }
    return {
        "callback_url_configured": bool(
            str(args.get("callback_url") or args.get("webhook_url") or "").strip()
        ),
    }


def _tool_ok(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "result": "external_send planned"
        if data.get("dry_run")
        else ("external_send sent" if data.get("sent") else "external_send not sent"),
        "is_error": False,
        "widget": {"type": "external_send", **data},
    }


def _tool_error(message: str) -> dict[str, Any]:
    return {
        "result": message,
        "is_error": True,
        "widget": {"type": "external_send", "error": message},
    }
