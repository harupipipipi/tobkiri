from __future__ import annotations

import html
import json


from blocks._common import error, ok
from domain.ai_client.oauth_store import (
    clear_provider_oauth_client_config,
    disconnect_provider_oauth,
    finish_provider_oauth,
    provider_oauth_status,
    provider_oauth_statuses,
    save_provider_oauth_client_config,
    start_provider_oauth,
)
from domain.connections.store import import_connection_bundle


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _callback_page(provider_id: str, *, success: bool, title: str, message: str, payload: dict[str, object]) -> dict[str, object]:
    safe_title = html.escape(title, quote=True)
    safe_message = html.escape(message, quote=True)
    status_color = "#86efac" if success else "#fca5a5"
    event_payload = json.dumps(
        {
            "type": "rumi_provider_oauth",
            "provider_id": provider_id,
            "success": success,
            **payload,
        },
        ensure_ascii=False,
    )
    body = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{safe_title}</title>
    <style>
      :root {{ color-scheme: dark; }}
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        background: #09090b;
        color: #e4e4e7;
        font-family: "Segoe UI", system-ui, sans-serif;
      }}
      main {{
        width: min(32rem, calc(100vw - 2rem));
        padding: 2rem;
        border-radius: 1rem;
        border: 1px solid rgba(63, 63, 70, 0.9);
        background: rgba(9, 9, 11, 0.96);
        box-shadow: 0 24px 64px rgba(0, 0, 0, 0.45);
      }}
      h1 {{
        margin: 0 0 0.8rem;
        color: {status_color};
        font-size: 1.35rem;
      }}
      p {{
        margin: 0 0 1rem;
        color: #d4d4d8;
        line-height: 1.6;
      }}
      small {{
        display: block;
        color: #a1a1aa;
      }}
      button {{
        appearance: none;
        border: 0;
        border-radius: 999px;
        padding: 0.85rem 1rem;
        background: #f4f4f5;
        color: #09090b;
        font: inherit;
        font-weight: 600;
        cursor: pointer;
      }}
    </style>
  </head>
  <body>
    <main>
      <h1>{safe_title}</h1>
      <p>{safe_message}</p>
      <p>Return to Rumi. This tab will close automatically if possible.</p>
      <button type="button" onclick="window.close()">Close this tab</button>
      <small>If the app does not refresh immediately, open Settings again and reconnect.</small>
    </main>
    <script>
      (() => {{
        const payload = {event_payload};
        try {{
          if (window.opener && !window.opener.closed) {{
            window.opener.postMessage(payload, window.location.origin);
          }}
        }} catch (_error) {{
          // Ignore cross-window errors and keep the visible success state.
        }}
        window.setTimeout(() => {{
          try {{
            window.close();
          }} catch (_error) {{
            // Ignore close failures.
          }}
        }}, 300);
      }})();
    </script>
  </body>
</html>
"""
    return {
        "_static": True,
        "content_type": "text/html; charset=utf-8",
        "body": body,
    }


def run(input_data, context):
    del context
    method = str((input_data or {}).get("_method", "GET")).upper()
    provider_id = str((input_data or {}).get("provider_id", "")).strip()
    headers = (input_data or {}).get("_headers")
    request_headers = headers if isinstance(headers, dict) else {}

    if method == "GET" and provider_id and (
        str((input_data or {}).get("code") or "").strip()
        or str((input_data or {}).get("state") or "").strip()
        or str((input_data or {}).get("error") or "").strip()
    ):
        result = finish_provider_oauth(provider_id, dict(input_data or {}))
        if result.get("success"):
            label = str(result.get("display_name") or result.get("email") or provider_id).strip()
            return _callback_page(
                provider_id,
                success=True,
                title="Browser login connected",
                message=f"{label} is now connected for {provider_id}.",
                payload={key: value for key, value in result.items() if key not in {"success"}},
            )
        return _callback_page(
            provider_id,
            success=False,
            title="Browser login failed",
            message=str(result.get("error") or "OAuth callback failed"),
            payload={"error": str(result.get("error") or "oauth_failed")},
        )

    if method == "GET":
        active_diagnostics = _truthy((input_data or {}).get("active_diagnostics") or (input_data or {}).get("diagnostics"))
        if provider_id:
            return ok({"provider": provider_oauth_status(provider_id, active_diagnostics=active_diagnostics)})
        return ok({"providers": provider_oauth_statuses(active_diagnostics=active_diagnostics)})

    if method == "POST":
        action = str((input_data or {}).get("action", "status")).strip().lower()
        if action == "import":
            raw_bundle = (input_data or {}).get("connection") or (input_data or {}).get("credential_bundle") or (input_data or {}).get("bundle")
            if isinstance(raw_bundle, str):
                result = import_connection_bundle(raw_bundle, provider_id=provider_id)
            elif isinstance(raw_bundle, dict):
                result = import_connection_bundle(raw_bundle, provider_id=provider_id)
            else:
                result = {"success": False, "provider_id": provider_id, "error": "credential bundle JSON is required"}
        elif action == "save_client":
            raw_value = str((input_data or {}).get("client_config") or (input_data or {}).get("value") or "")
            result = save_provider_oauth_client_config(provider_id, raw_value)
        elif action == "clear_client":
            result = clear_provider_oauth_client_config(provider_id)
        elif action == "disconnect":
            result = disconnect_provider_oauth(provider_id)
        elif action == "start":
            requested_services = (input_data or {}).get("services")
            result = start_provider_oauth(
                provider_id,
                request_headers=request_headers,
                scope_mode=str((input_data or {}).get("scope_mode") or "").strip() or None,
                services=requested_services if isinstance(requested_services, list) else None,
            )
        elif action in {"diagnostics", "active_diagnostics", "cloudflare_diagnostics"}:
            result = {
                "success": True,
                "provider_id": provider_id or "cloudflare",
                "provider": provider_oauth_status(provider_id or "cloudflare", active_diagnostics=True),
            }
        else:
            result = {
                "success": True,
                "provider_id": provider_id,
                "provider": provider_oauth_status(provider_id),
            }
        if not result.get("success"):
            return error(str(result.get("error") or "oauth action failed"), "OAUTH_FAILED")
        return ok(result)

    return error("unsupported method", "METHOD_NOT_ALLOWED")
