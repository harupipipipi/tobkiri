from __future__ import annotations

import hmac
import hashlib
import time
from typing import Any, Dict

from tobkiri_protocol.settings_state import SettingsOwnerPort

from blocks._common import ok, error
from blocks.integrations.common import allow_unsigned_webhook_dev, headers_from_request, raw_body_bytes
from domain.external.adapters.slack import SlackResponseAdapter
from domain.external.normalizer import normalize_slack_event
from domain.external.pipeline import dispatch_external_event
from domain.external.response import RumiResponse
from domain.external.response_planner import ResponsePlanner
from domain.external.token_store import read_external_token
from domain.integrations.secrets import load_integration_secrets_into_env


def run(input_data, context, *, settings_owner: SettingsOwnerPort | None = None):
    settings_owner = (
        settings_owner
        if settings_owner is not None
        else (context or {}).get("_settings_owner_port")
    )
    load_integration_secrets_into_env()
    headers = headers_from_request(input_data)
    raw_body = raw_body_bytes(input_data)
    verification = _verify_slack(headers, raw_body)
    if not verification["ok"]:
        return {**error(verification["reason"], "SIGNATURE_INVALID"), "_http_status": 401}

    if input_data.get("type") == "url_verification":
        return {"challenge": input_data.get("challenge", "")}

    if input_data.get("type") != "event_callback":
        return ok({"ignored": True, "reason": "unsupported slack payload"})

    event = input_data.get("event") if isinstance(input_data.get("event"), dict) else {}
    if event.get("subtype") == "bot_message" or event.get("bot_id"):
        return ok({"ignored": True, "reason": "bot message"})
    if event.get("type") not in {"message", "app_mention"}:
        return ok({"ignored": True, "reason": "unsupported slack event"})

    text = str(event.get("text") or "").strip()
    if not text:
        return ok({"ignored": True, "reason": "empty slack message"})

    external_event = normalize_slack_event(input_data, verified=bool(verification["verified"]))
    model = str(input_data.get("model") or "").strip()
    if model:
        external_event.metadata["model"] = model
    result = dispatch_external_event(
        external_event,
        input_profile_id="slack.default",
        audience_policy={"default": "allow"},
        context=context,
        send_response=True,
        **({"settings_owner": settings_owner} if settings_owner is not None else {}),
    )
    plan = result.get("response_plan") if isinstance(result.get("response_plan"), dict) else ResponsePlanner("slack").plan(RumiResponse.from_result(result))
    send_result = _send_response_plan(plan, external_event)
    return ok({**result, "verified": verification["verified"], "reply": send_result})


def _verify_slack(headers: Dict[str, str], raw_body: bytes) -> Dict[str, Any]:
    secret = read_external_token("slack", kind="signing_secret", legacy_key="SLACK_SIGNING_SECRET")
    if not secret:
        if allow_unsigned_webhook_dev():
            return {"ok": True, "verified": False, "reason": "unsigned dev mode enabled"}
        return {"ok": False, "verified": False, "reason": "Slack signing secret not configured"}
    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")
    if not timestamp or not signature:
        return {"ok": False, "verified": False, "reason": "missing Slack signature headers"}
    try:
        if abs(time.time() - int(timestamp)) > 60 * 5:
            return {"ok": False, "verified": False, "reason": "stale Slack signature timestamp"}
    except ValueError:
        return {"ok": False, "verified": False, "reason": "invalid Slack signature timestamp"}
    base = b"v0:" + timestamp.encode("utf-8") + b":" + raw_body
    expected = "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return {"ok": False, "verified": False, "reason": "Slack signature mismatch"}
    return {"ok": True, "verified": True, "reason": ""}


def _send_response_plan(plan: dict[str, Any], external_event) -> Dict[str, Any]:
    action_plan = (plan.get("metadata") or {}).get("response_action_plan") if isinstance(plan.get("metadata"), dict) else {}
    if isinstance(action_plan, dict) and not action_plan.get("external_reply", True):
        return {"sent": False, "reason": "external reply suppressed by response prompt policy"}
    return SlackResponseAdapter().send(plan, event=external_event)
