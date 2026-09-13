"""Generic pack function invocation helper."""

from __future__ import annotations

import importlib.util
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from .paths import (
    BASE_DIR,
    CORE_PACK_DIR,
    CORE_PACK_ID_PREFIX,
    ECOSYSTEM_DIR,
    is_path_within,
)
from .approval_manager import get_approval_manager
from .pack_function_policy import (
    permission_id_for_entry,
)
from .validation import validate_entrypoint


def _is_pack_approved_and_verified(pack_id: str) -> tuple[bool, Optional[str]]:
    try:
        from .approval_manager import get_approval_manager

        result = get_approval_manager().is_pack_approved_and_verified(pack_id)
    except Exception as exc:
        return False, f"approval_check_error:{exc}"
    if isinstance(result, tuple):
        return bool(result[0]), result[1] if len(result) > 1 else None
    return bool(result), None


def _pack_function_policy_module():
    """Resolve policy helpers at call time so test monkeypatches survive import aliases."""
    return (
        sys.modules.get("core_runtime.pack_function_policy")
        or sys.modules.get("tobkiri_runtime.core_runtime.pack_function_policy")
    )


def _find_pack_root(path_hint: Any) -> Optional[Path]:
    try:
        candidate = Path(path_hint).resolve()
    except (OSError, TypeError):
        return None
    if candidate.is_file():
        candidate = candidate.parent
    for current in (candidate, *candidate.parents):
        if (current / "ecosystem.json").is_file():
            return current
    return candidate


def _is_pack_root_under(pack_root: Path, allowed_root: Path, pack_id: str) -> bool:
    try:
        resolved_pack = pack_root.resolve()
    except OSError:
        resolved_pack = pack_root
    try:
        resolved_allowed = allowed_root.resolve()
    except OSError:
        resolved_allowed = allowed_root
    try:
        relative = resolved_pack.relative_to(resolved_allowed)
    except ValueError:
        return False
    return bool(relative.parts) and relative.parts[0] == pack_id


def is_pack_function_in_process_allowed(
    pack_id: str,
    pack_root_hint: Any = None,
) -> bool:
    """Return True only for first-party pack functions allowed in this process."""
    normalized_pack_id = str(pack_id or "").strip()
    if not normalized_pack_id:
        return False

    pack_root = _find_pack_root(pack_root_hint) if pack_root_hint is not None else None

    if normalized_pack_id.startswith(CORE_PACK_ID_PREFIX):
        if pack_root is None:
            pack_root = Path(CORE_PACK_DIR) / normalized_pack_id
            if not pack_root.is_dir():
                return False
        return _is_pack_root_under(
            pack_root,
            Path(CORE_PACK_DIR),
            normalized_pack_id,
        )

    if pack_root is None:
        pack_root = Path(ECOSYSTEM_DIR) / normalized_pack_id
        if not pack_root.is_dir():
            return False
    try:
        return bool(
            get_approval_manager().is_pack_in_process_allowed(
                normalized_pack_id,
                pack_root,
            )
        )
    except Exception:
        return False


def invoke_pack_function(
    pack_id: str,
    function_id: str,
    args: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Any:
    entry = resolve_function_entry(pack_id, function_id)
    assert_pack_function_executable(entry, context)
    return execute_function_entry(entry, args=args, context=context)


def resolve_function_entry(pack_id: str, function_id: str) -> Any:
    from .di_container import get_container

    qualified_name = (
        function_id if ":" in function_id else f"{pack_id}:{function_id}"
    )
    entry = get_container().get("function_registry").get(qualified_name)
    if entry is None:
        raise KeyError(f"Function not found: {qualified_name}")
    pack_root_hint = getattr(entry, "function_dir", None) or getattr(entry, "main_py_path", None)
    if not is_pack_function_in_process_allowed(entry.pack_id, pack_root_hint):
        raise PermissionError(
            f"In-process pack function execution is not allowed: {qualified_name}"
        )
    approved, reason = _is_pack_approved_and_verified(entry.pack_id)
    if not approved:
        detail = f": {reason}" if reason else ""
        raise PermissionError(f"Pack not approved: {entry.pack_id}{detail}")
    return entry


def _resolve_entrypoint_parts(entry: Any) -> tuple[Path | None, str]:
    entrypoint = entry.entrypoint or "main.py:run"
    entrypoint_for_validation = entrypoint if ":" in entrypoint else f"{entrypoint}:run"
    function_dir = Path(entry.function_dir)
    valid, error, module_path = validate_entrypoint(entrypoint_for_validation, function_dir)
    if not valid or module_path is None:
        raise PermissionError(error or f"Invalid entrypoint: {entrypoint}")
    _, callable_name = entrypoint_for_validation.rsplit(":", 1)
    if not module_path.is_file():
        raise FileNotFoundError(f"Entrypoint file not found: {module_path}")
    if not is_path_within(module_path, BASE_DIR):
        raise PermissionError(f"Entrypoint escapes project boundary: {module_path}")
    trusted_path = getattr(entry, "main_py_path", None)
    if trusted_path is not None and Path(trusted_path).resolve() != module_path.resolve():
        raise PermissionError(
            f"Entrypoint differs from trusted registry path: {module_path}"
        )
    return module_path, callable_name


def _resolve_executable_boundary_path(entry: Any) -> Path | None:
    calling_convention = str(getattr(entry, "calling_convention", "") or "").strip()
    if calling_convention in {"subprocess", "block", "python_host", "python_docker", ""}:
        module_path, _callable_name = _resolve_entrypoint_parts(entry)
        return module_path
    if calling_convention == "binary":
        binary_path = getattr(entry, "main_binary_path", None)
        return Path(binary_path) if binary_path else None
    if calling_convention == "command":
        command = getattr(entry, "command", None) or []
        if command and isinstance(command[0], str) and command[0]:
            candidate = Path(command[0])
            if candidate.is_absolute():
                return candidate
        return None
    return None


def assert_pack_function_executable(
    entry: Any,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    del context

    function_dir = Path(entry.function_dir)
    if not function_dir.is_dir():
        raise FileNotFoundError(f"Function directory not found: {function_dir}")
    if not is_path_within(function_dir, BASE_DIR):
        raise PermissionError(f"Function directory escapes project boundary: {function_dir}")

    calling_convention = str(getattr(entry, "calling_convention", "") or "").strip()
    boundary_path = _resolve_executable_boundary_path(entry)
    if calling_convention == "command" and boundary_path is None:
        command = getattr(entry, "command", None) or []
        if command and isinstance(command[0], str) and command[0]:
            raise PermissionError(
                "Command entrypoints must use an absolute executable path."
            )
    if boundary_path is not None:
        if not boundary_path.exists():
            raise FileNotFoundError(f"Entrypoint file not found: {boundary_path}")
        if not is_path_within(boundary_path, function_dir):
            raise PermissionError(
                f"Entrypoint escapes function boundary: {boundary_path}"
            )
        if not is_path_within(boundary_path, BASE_DIR):
            raise PermissionError(
                f"Entrypoint escapes project boundary: {boundary_path}"
            )

    policy_module = _pack_function_policy_module()
    validate = getattr(policy_module, "validate_function_execution")
    calling_convention, grant_config = validate(entry, boundary_path)
    if calling_convention == "python_host":
        allow_host = str(os.environ.get("RUMI_ALLOW_HOST_EXECUTION", "")).lower()
        if allow_host not in {"1", "true"}:
            raise PermissionError(
                "Host execution is disabled. Set RUMI_ALLOW_HOST_EXECUTION=1 to enable."
            )
    setattr(entry, "_pack_function_grant_config", grant_config)


def _call_context_for_entry(entry: Any, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    call_context = dict(context or {})
    call_context.setdefault("pack_id", entry.pack_id)
    call_context.setdefault("qualified_name", entry.qualified_name)
    call_context.setdefault("function_id", entry.function_id)
    call_context.setdefault("permission_id", permission_id_for_entry(entry))
    call_context.setdefault(
        "request_id",
        str(call_context.get("request_id") or uuid.uuid4()),
    )
    grant_config = getattr(entry, "_pack_function_grant_config", None)
    if grant_config:
        call_context.setdefault("grant_config", dict(grant_config))
    return call_context


def _raise_for_capability_response(response: Any) -> Any:
    if getattr(response, "success", False):
        return getattr(response, "output", None)
    error = getattr(response, "error", None) or "Function execution failed"
    raise RuntimeError(str(error))


def _execute_direct_python_entry(
    entry: Any,
    args: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Any:
    module_path, callable_name = _resolve_entrypoint_parts(entry)
    assert module_path is not None
    spec = importlib.util.spec_from_file_location(
        f"pack_function_{entry.qualified_name.replace(':', '_')}",
        str(module_path),
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module: {module_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn = getattr(module, callable_name, None)
    if fn is None:
        raise AttributeError(f"Callable '{callable_name}' not found in {module_path}")

    return fn(_call_context_for_entry(entry, context), dict(args or {}))


def execute_function_entry(
    entry: Any,
    args: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Any:
    from .capability_executor import (
        DEFAULT_FUNCTION_TIMEOUT,
        _HandlerDefAdapter,
        get_capability_executor,
    )

    calling_convention = str(getattr(entry, "calling_convention", "") or "").strip()
    if calling_convention in {"", "block"}:
        return _execute_direct_python_entry(entry, args=args, context=context)
    if calling_convention == "kernel":
        raise RuntimeError("kernel calling_convention functions must be executed via kernel dispatch")

    executor = get_capability_executor()
    request_id = str((context or {}).get("request_id") or uuid.uuid4())
    start_time = time.time()
    grant_config = dict(getattr(entry, "_pack_function_grant_config", {}) or {})
    request_context = _call_context_for_entry(entry, context)
    timeout = float((context or {}).get("timeout_seconds") or DEFAULT_FUNCTION_TIMEOUT)

    if calling_convention == "subprocess":
        entrypoint = entry.entrypoint or "main.py:run"
        function_dir = Path(entry.function_dir)
        ep_file = entrypoint.rsplit(":", 1)[0] if ":" in entrypoint else entrypoint
        adapter = _HandlerDefAdapter(
            handler_id=entry.qualified_name,
            permission_id=permission_id_for_entry(entry),
            entrypoint=entrypoint,
            handler_dir=function_dir,
            handler_py_path=function_dir / ep_file,
            is_builtin=bool(getattr(entry, "is_builtin", False)),
            pack_id=str(getattr(entry, "pack_id", "") or ""),
        )
        response = executor._execute_handler_subprocess(
            handler_def=adapter,
            principal_id=entry.pack_id,
            permission_id=permission_id_for_entry(entry),
            grant_config=grant_config,
            args=dict(args or {}),
            timeout_seconds=timeout,
            request_id=request_id,
            start_time=start_time,
            request_context=request_context,
        )
        return _raise_for_capability_response(response)

    if calling_convention == "python_docker":
        response = executor._execute_user_function(
            principal_id=entry.pack_id,
            entry=entry,
            args=dict(args or {}),
            request_id=request_id,
            start_time=start_time,
            grant_config=grant_config,
            request_context=request_context,
            force_docker=True,
        )
        return _raise_for_capability_response(response)

    response = executor._dispatch_by_calling_convention(
        calling_convention=calling_convention,
        entry=entry,
        principal_id=entry.pack_id,
        effective_permission_id=permission_id_for_entry(entry),
        grant_config=grant_config,
        args=dict(args or {}),
        timeout_seconds=timeout,
        request_id=request_id,
        start_time=start_time,
        request_context=request_context,
    )
    return _raise_for_capability_response(response)
