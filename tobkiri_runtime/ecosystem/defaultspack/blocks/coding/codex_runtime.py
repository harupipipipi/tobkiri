"""HTTP entry points for the production Codex App Server runtime."""

from __future__ import annotations

from typing import Any, Callable

from blocks._common import error, ok
from domain.coding.codex_app_server_runtime import (
    CodexRuntimeError,
    get_codex_app_server_runtime,
)
from domain.coding.workspace_policy import WorkspaceTrustRequired
from domain.coding.workspace_resolver import WorkspaceResolutionError


def _workspace_id(input_data: dict[str, Any]) -> str:
    return str(input_data.get("workspace_id") or "").strip()


def _handle(action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return ok(action())
    except WorkspaceTrustRequired as exc:
        result = error(str(exc), code=exc.code)
        result["_http_status"] = 403
        return result
    except WorkspaceResolutionError as exc:
        return error(str(exc), code=exc.code)
    except CodexRuntimeError as exc:
        return error(str(exc), code="CODEX_APP_SERVER_UNAVAILABLE")
    except Exception as exc:
        return error(str(exc), code="CODEX_APP_SERVER_ERROR")


def status(input_data: dict[str, Any], context: dict[str, Any] | None = None):
    """Return models, thread state, and streamed events for one workspace."""

    del context
    return _handle(
        lambda: get_codex_app_server_runtime().status(_workspace_id(input_data))
    )


def start(input_data: dict[str, Any], context: dict[str, Any] | None = None):
    """Start a turn in the workspace's durable App Server thread."""

    del context
    return _handle(
        lambda: get_codex_app_server_runtime().start_turn(
            _workspace_id(input_data),
            str(input_data.get("message") or input_data.get("text") or ""),
            model=str(input_data.get("model") or ""),
            effort=str(input_data.get("effort") or ""),
        )
    )


def interrupt(input_data: dict[str, Any], context: dict[str, Any] | None = None):
    """Interrupt the active turn in the workspace's App Server thread."""

    del context
    return _handle(
        lambda: get_codex_app_server_runtime().interrupt(_workspace_id(input_data))
    )
