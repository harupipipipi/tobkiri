"""Retired boolean-authorized clipboard entrypoint.

Use the captured clipboard resource/action v1 contracts through the Host
RequestBroker. A Viewer approval flag is not an InvocationLease.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def run_clipboard_host_action(
    action: str,
    payload: Mapping[str, Any] | None,
    *,
    viewer_host_approved: bool,
) -> dict[str, Any]:
    """Reject the retired direct OS entrypoint without invoking an executor."""
    del action, payload, viewer_host_approved
    return {
        "status": "denied",
        "success": False,
        "executed": False,
        "error_type": "legacy_clipboard_execution_retired",
        "recovery": "Invoke the canonical clipboard read/write contract.",
    }
