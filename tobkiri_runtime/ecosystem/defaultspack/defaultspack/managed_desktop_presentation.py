"""Finite HTTP bindings for the captured managed-desktop read provider."""

from __future__ import annotations
from typing import Mapping

CONTRACT_ID = "tobkiri.resource.managed-desktops.v1"
FUNCTION_ID = "tobkiri.managed-desktops.read"
READ_ROUTES = {
    ("GET", "/api/desktops"): "rumi_sandbox_runtime_pack.desktops-list",
    (
        "GET",
        "/api/runtime/providers",
    ): "rumi_sandbox_runtime_pack.runtime-providers-read",
    ("POST", "/api/runtime/doctor"): "rumi_sandbox_runtime_pack.runtime-doctor-read",
    (
        "GET",
        "/api/sandbox/templates",
    ): "rumi_sandbox_runtime_pack.sandbox-templates-read",
}
READ_TARGETS = frozenset(
    (
        f"defaults.managed-desktops.{operation.rsplit('.', 1)[-1]}",
        CONTRACT_ID,
        operation,
        FUNCTION_ID,
        FUNCTION_ID,
    )
    for operation in READ_ROUTES.values()
)


def normalize_managed_desktop_read(
    target: tuple[str, str, str, str, str],
    payload: Mapping[str, object],
    *,
    profile_id: str,
) -> dict[str, object]:
    """Supply captured identity and reject all authority/path caller fields."""
    allowed = {"request_id"} if target[2].endswith("runtime-doctor-read") else set()
    if target not in READ_TARGETS or not profile_id or set(payload) - allowed:
        raise ValueError("managed desktop read payload is invalid")
    if "request_id" in payload and (
        not isinstance(payload["request_id"], str)
        or not 1 <= len(payload["request_id"]) <= 128
    ):
        raise ValueError("managed desktop diagnostic request identity is invalid")
    return {"profile_id": profile_id}


def present_managed_desktop_read(result: Mapping[str, object]) -> dict[str, object]:
    """Return data for the Host APIResponse envelope without nesting it."""
    return dict(result)
