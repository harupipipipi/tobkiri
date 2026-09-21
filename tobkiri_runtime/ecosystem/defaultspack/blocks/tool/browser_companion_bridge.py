from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error, ok
from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import (
    GlobalContractInvocationError,
    GlobalContractUnavailable,
    invoke_global_contract,
)


_BROWSER_CONTRACT = "rumi.resource.browser.host.v1"
# An embedding/test harness may supply an isolated bridge store.  Production
# leaves this unset and therefore cannot reach a Pack implementation directly.
BrowserCompanionBridgeStore = None


def _injected_store():
    factory = BrowserCompanionBridgeStore
    return factory() if callable(factory) else None


def _authorized_store(input_data):
    store = _injected_store()
    if store is None:
        return None, _contract_unavailable("browser.companion.authorization")
    headers = input_data.get("_headers") if isinstance(input_data, dict) else {}
    headers = headers if isinstance(headers, dict) else {}
    authorization = str(headers.get("Authorization") or headers.get("authorization") or "")
    token = authorization.removeprefix("Bearer ").strip()
    token = token or str(input_data.get("pairing_token") or "")
    if not store.pairing_authorized(token):
        response = error(
            "invalid or missing browser companion pairing token",
            code="PAIRING_UNAUTHORIZED",
        )
        response["_http_status"] = 401
        return None, response
    return store, None


def _invoke_browser(operation, payload=None):
    session = get_container().get_or_none("v4_dispatch_session")
    if session is None:
        raise GlobalContractUnavailable("a captured v4 dispatch session is required")
    result = invoke_global_contract(session, _BROWSER_CONTRACT, operation, payload or {})
    if not isinstance(result, dict):
        raise GlobalContractInvocationError(
            "invalid_result", "browser host contract returned an invalid result"
        )
    return result


def _contract_unavailable(operation):
    response = error(
        "Browser companion is not an executable v4 contract operation.",
        code="BROWSER_COMPANION_CONTRACT_UNAVAILABLE",
        details={"operation": operation},
    )
    response["_http_status"] = 503
    return response


def run_session(input_data=None, context=None):
    injected = _injected_store()
    if injected is not None:
        context = context if isinstance(context, dict) else {}
        clients = injected.list_clients()
        active = next(
            (client for client in clients if client.get("is_active")), None
        )
        pairing = injected.ensure_pairing(rotate=False)
        return ok(
            {
                "action": "session",
                "pairing": {
                    **pairing,
                    "server_urls": [
                        str(context.get("base_url") or "").rstrip("/")
                    ],
                },
                "clients": clients,
                "active_client_id": (
                    active.get("client_id")
                    if isinstance(active, dict)
                    else injected.active_client_id()
                ),
                "setup_required": not bool(clients),
            }
        )
    del input_data, context
    try:
        return ok(_invoke_browser("browser.session.get"))
    except (GlobalContractInvocationError, GlobalContractUnavailable):
        return _contract_unavailable("browser.session.get")


def run_poll(input_data, context=None):
    store, failure = _authorized_store(input_data if isinstance(input_data, dict) else {})
    if store is not None:
        payload = dict(input_data.get("client") or input_data or {})
        client = store.upsert_client(payload)
        command = store.claim_next_command(str(client.get("client_id") or ""))
        return ok(
            {
                "accepted": True,
                "client_id": client.get("client_id"),
                "command": _public_command(command),
                "commands": [_public_command(command)] if isinstance(command, dict) else [],
            }
        )
    del context
    return failure


def run_result(input_data, context=None):
    store, failure = _authorized_store(input_data if isinstance(input_data, dict) else {})
    if store is not None:
        payload = input_data if isinstance(input_data, dict) else {}
        client_payload = dict(payload.get("client") or {})
        client_id = str(payload.get("client_id") or client_payload.get("client_id") or "")
        if client_id:
            client_payload["client_id"] = client_id
        client = store.upsert_client(client_payload)
        records = []
        raw_results = payload.get("results")
        items = raw_results if isinstance(raw_results, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            command_id = str(item.get("command_id") or "")
            if command_id:
                records.append(
                    store.complete_command(
                        str(client.get("client_id") or ""),
                        command_id,
                        _normalized_result(item),
                    )
                )
        return ok(
            {
                "accepted": True,
                "command_id": records[0].get("command_id") if records else None,
                "command_ids": [record.get("command_id") for record in records],
            }
        )
    del context
    return failure


def _public_command(record):
    if not isinstance(record, dict):
        return None
    return {
        "command_id": record.get("command_id"),
        "action": (record.get("request") or {}).get("action"),
        "payload": (record.get("request") or {}).get("payload") or {},
        "created_at": record.get("created_at"),
    }


def _normalized_result(payload):
    if not isinstance(payload, dict):
        return {}
    raw_result = payload.get("result")
    result = dict(raw_result) if isinstance(raw_result, dict) else {}
    if not result:
        result = {
            key: value
            for key, value in payload.items()
            if key not in {"command_id", "client_id", "type", "ok", "started_at", "finished_at", "error", "result"}
        }
    if payload.get("ok") is False:
        result["is_error"] = True
        if not result.get("reason"):
            result["reason"] = payload.get("error") or "Browser companion command failed."
    elif payload.get("ok") is True and "is_error" not in result:
        result["is_error"] = False
    return result
