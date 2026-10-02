"""Policy decisions over declared authority operations, never tool IDs."""

from __future__ import annotations

from typing import Any, Callable, Mapping

_AUTHORITIES = frozenset(
    {
        "file.read",
        "file.write",
        "shell.inspect",
        "shell.execute",
        "git.read",
        "git.write",
        "git.publish",
        "browser.observe",
        "browser.control",
        "desktop.observe",
        "desktop.control",
        "clipboard.read",
        "clipboard.write",
        "service.invoke",
        "service.mutate",
        "remote.invoke",
        "mcp.invoke",
        "service.mutate",
    }
)
_APPROVAL_REQUIRED = frozenset(
    {
        "file.write",
        "shell.execute",
        "git.write",
        "git.publish",
        "browser.control",
        "desktop.control",
        "clipboard.read",
        "clipboard.write",
        "service.mutate",
        "remote.invoke",
        "mcp.invoke",
    }
)


def create_policy_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create a conservative policy evaluator for one declared operation."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"evaluate", "decide"}:
            raise ValueError(f"unknown tool policy operation: {name}")
        authority = str(payload.get("authority") or "").strip()
        grants = {
            str(item).strip()
            for item in payload.get("granted_authorities") or []
            if str(item).strip()
        }
        known = authority in _AUTHORITIES
        granted = known and authority in grants
        denied_by_rule = authority in {
            str(item).strip()
            for item in payload.get("denied_authorities") or []
            if str(item).strip()
        }
        allowed = known and granted and not denied_by_rule
        return {
            "allowed": allowed,
            "authority": authority,
            "known_authority": known,
            "profile_granted": granted,
            "denied_by_rule": denied_by_rule,
            "approval_required": authority in _APPROVAL_REQUIRED,
            "risk": _risk(authority),
            "reason": (
                "allowed"
                if allowed
                else "unknown_authority"
                if not known
                else "explicitly_denied"
                if denied_by_rule
                else "profile_permission_missing"
            ),
        }

    return operation


def _risk(authority: str) -> str:
    if authority in {"git.publish", "shell.execute", "desktop.control"}:
        return "critical"
    if authority in _APPROVAL_REQUIRED:
        return "high"
    if authority.endswith(".observe") or authority.endswith(".inspect"):
        return "medium"
    return "low"

