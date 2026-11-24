from __future__ import annotations

import copy
import os
import time
from pathlib import Path
from typing import Any

from domain.coding.workspace_jail import (
    WorkspaceJail,
    WorkspacePathViolation,
    WorkspaceRestrictedPath,
)
from domain.coding.workspace_policy import (
    WorkspaceTrustRequired,
    require_registered_trusted_workspace,
)
from domain.coding.workspace_resolver import (
    WorkspacePathError,
    WorkspaceResolutionError,
    WorkspaceResolver,
)


SECRET_KEY_PARTS = (
    "authorization",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
    "api_key",
    "apikey",
)

NON_SECRET_POLICY_KEYS = {
    "context_token",
    "context_tokens",
    "max_context_tokens",
    "reserved_tokens",
    "used_tokens",
    "redact_secret",
    "redact_secrets",
    "secret_access",
    "secret_use",
    "secrets_access",
}


class AdaptiveError(Exception):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def now_seconds() -> int:
    return int(time.time())


def clean_profile_id(value: Any) -> str:
    candidate = str(value or "").strip() or "default"
    try:
        from core_runtime.profile_workspace import validate_profile_id

        return validate_profile_id(candidate)
    except ValueError:
        raise
    except Exception:
        if "/" in candidate or "\\" in candidate or ".." in candidate:
            raise ValueError("profile_id must not contain path traversal segments")
        return candidate


def profile_id_from(args: dict[str, Any] | None, ctx: dict[str, Any] | None) -> str:
    args = args if isinstance(args, dict) else {}
    ctx = ctx if isinstance(ctx, dict) else {}
    for value in (
        args.get("profile_id"),
        ctx.get("profile_id"),
        ctx.get("active_startup_profile_id"),
        (ctx.get("active_startup_profile") or {}).get("profile_id")
        if isinstance(ctx.get("active_startup_profile"), dict)
        else None,
    ):
        if str(value or "").strip():
            return clean_profile_id(value)
    try:
        from core_runtime.profile_paths import active_profile_id

        active = active_profile_id()
        if active:
            return clean_profile_id(active)
    except Exception:
        pass
    return "default"


def adaptive_store_root(profile_id: str) -> Path:
    configured = os.environ.get("RUMI_ADAPTIVE_STORE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve() / clean_profile_id(profile_id)
    try:
        from core_runtime.profile_workspace import ProfileWorkspaceManager

        return (
            ProfileWorkspaceManager()
            .profile_user_data_dir(clean_profile_id(profile_id))
            .joinpath("adaptive")
        )
    except Exception:
        base = Path(os.environ.get("RUMI_USER_DATA") or Path.cwd() / "user_data")
        return base / "workspaces" / clean_profile_id(profile_id) / "state" / "adaptive"


def workspace_root_from(args: dict[str, Any] | None, ctx: dict[str, Any] | None) -> Path:
    args = args if isinstance(args, dict) else {}
    ctx = ctx if isinstance(ctx, dict) else {}
    resolver = WorkspaceResolver()
    request_selects_workspace = any(
        args.get(key) not in (None, "") for key in ("workspace_id", "workspace_root", "root")
    )
    context_selects_workspace = any(
        ctx.get(key) not in (None, "") for key in ("workspace_id", "workspace_root", "root")
    )
    try:
        if request_selects_workspace or context_selects_workspace:
            resolution = resolver.resolve(args, ctx, allow_cwd_fallback=False)
            trusted = require_registered_trusted_workspace(
                resolution,
                operation="adaptive.context",
                store=resolver.store,
            )
            return Path(trusted.root_path).resolve()
    except WorkspaceTrustRequired as exc:
        raise AdaptiveError(exc.code, str(exc)) from exc
    except WorkspacePathError as exc:
        raise AdaptiveError(exc.code, str(exc)) from exc
    except WorkspaceResolutionError as exc:
        raise AdaptiveError(exc.code, str(exc)) from exc
    except ValueError as exc:
        raise AdaptiveError("WORKSPACE_INVALID", str(exc)) from exc

    raw = os.environ.get("RUMI_WORKSPACE_ROOT")
    root = Path(raw).expanduser() if raw else Path.cwd()
    root = root.resolve()
    if not root.exists() or not root.is_dir():
        raise AdaptiveError("WORKSPACE_NOT_FOUND", f"workspace root not found: {root}")
    return root


def resolve_under(root: Path, value: Any) -> Path:
    jail = WorkspaceJail(root)
    try:
        candidate = jail.resolve(value if str(value or "").strip() else ".")
        jail.ensure_allowed(jail.relative(candidate), operation="adaptive context access")
    except WorkspacePathViolation as exc:
        raise AdaptiveError("PATH_OUTSIDE_WORKSPACE", str(exc)) from exc
    except WorkspaceRestrictedPath as exc:
        raise AdaptiveError("PATH_RESTRICTED", str(exc)) from exc
    return candidate


def path_is_restricted(root: Path, path: Path) -> bool:
    jail = WorkspaceJail(root)
    try:
        return bool(jail.restriction_reason(jail.relative(path)))
    except Exception:
        return True


def coerce_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            lowered = text_key.lower()
            if lowered in NON_SECRET_POLICY_KEYS:
                output[text_key] = redact(item)
            elif any(part in lowered for part in SECRET_KEY_PARTS):
                output[text_key] = "[REDACTED]"
            else:
                output[text_key] = redact(item)
        return output
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return copy.deepcopy(value)


def compact_text(value: Any, *, limit: int = 240) -> str:
    text = str(value or "")
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."
