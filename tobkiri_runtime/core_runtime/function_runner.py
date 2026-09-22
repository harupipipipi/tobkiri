"""
function_runner.py - JSON 入力で Python callable を実行する runner
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import importlib.util
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any, Callable, Dict

# Avoid shadowing stdlib modules like `types` when this file is executed directly
# from inside the core_runtime package directory.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if sys.path and os.path.abspath(sys.path[0]) == _SCRIPT_DIR:
    sys.path.pop(0)

_CHILD_PROCESS_POLICY_ENV = "RUMI_SANDBOX_DENY_CHILD_PROCESS"
_BLOCKED_PROCESS_AUDIT_EVENTS = frozenset(
    {
        "os.exec",
        "os.fork",
        "os.forkpty",
        "os.posix_spawn",
        "os.posix_spawnp",
        "os.system",
        "pty.spawn",
        "subprocess.Popen",
    }
)
_REQUIRED_BLOCKED_PROCESS_SYSCALLS = (
    b"clone",
    b"clone3",
    b"execve",
    b"execveat",
)
# ``fork`` and ``vfork`` are separate system calls on most Linux ABIs, but are
# intentionally absent from the arm64 Linux syscall table.
_FORK_VFORK_SYSCALLS = (
    b"fork",
    b"vfork",
)
_FORK_VFORK_ABSENT_LINUX_ABIS = frozenset({"aarch64", "arm64"})
_SCMP_ACT_ALLOW = 0x7FFF0000
_SCMP_ACT_ERRNO = 0x00050000


class SandboxProcessDenied(PermissionError):
    """Raised when untrusted Pack code attempts to create a child process."""


def _resolved_blocked_process_syscalls(
    resolve_name: Callable[[bytes], int],
    *,
    machine: str | None = None,
) -> tuple[int, ...]:
    """Resolve every native child-process syscall without weakening the filter.

    The clone and exec variants are required on every supported Linux guest.
    ``fork`` and ``vfork`` are also required except for the arm64 Linux ABI,
    which does not expose those legacy aliases. A missing required syscall is
    therefore a fail-closed configuration error; no resolver mismatch can
    silently weaken x86_64 or another ABI's filter.
    """
    machine_name = machine if machine is not None else platform.machine()
    architecture = machine_name.strip().casefold()
    required_syscalls: tuple[bytes, ...] = _REQUIRED_BLOCKED_PROCESS_SYSCALLS
    if architecture not in _FORK_VFORK_ABSENT_LINUX_ABIS:
        required_syscalls += _FORK_VFORK_SYSCALLS

    resolved: list[int] = []
    missing_required: list[str] = []
    for syscall_name in required_syscalls:
        syscall = resolve_name(syscall_name)
        if syscall < 0:
            missing_required.append(syscall_name.decode("ascii"))
            continue
        resolved.append(syscall)
    if missing_required:
        missing = ", ".join(missing_required)
        raise RuntimeError(f"Sandbox child process policy is incomplete: {missing}")

    return tuple(resolved)


def _install_seccomp_child_process_filter() -> None:
    """Deny process creation after the Host-controlled Python runner starts."""
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Sandbox child process policy requires Linux seccomp")
    try:
        seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    except OSError as exc:
        raise RuntimeError("Sandbox child process policy is unavailable") from exc

    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    seccomp.seccomp_rule_add.restype = ctypes.c_int
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_load.restype = ctypes.c_int
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.restype = None

    context = seccomp.seccomp_init(_SCMP_ACT_ALLOW)
    if not context:
        raise RuntimeError("Sandbox child process policy initialization failed")
    deny_action = _SCMP_ACT_ERRNO | errno.EPERM
    try:
        for syscall in _resolved_blocked_process_syscalls(
            seccomp.seccomp_syscall_resolve_name
        ):
            if seccomp.seccomp_rule_add(context, deny_action, syscall, 0) != 0:
                raise RuntimeError("Sandbox child process policy rule failed")
        if seccomp.seccomp_load(context) != 0:
            raise RuntimeError("Sandbox child process policy could not be loaded")
    finally:
        seccomp.seccomp_release(context)


def _install_child_process_policy() -> Callable[[], bool]:
    """Install fail-closed syscall and audit guards for one Pack invocation."""
    if os.environ.get(_CHILD_PROCESS_POLICY_ENV) != "1":
        return lambda: False

    _install_seccomp_child_process_filter()
    violations: list[str] = []

    def deny_process_event(event: str, _args: tuple[Any, ...]) -> None:
        if event in _BLOCKED_PROCESS_AUDIT_EVENTS:
            violations.append(event)
            raise SandboxProcessDenied(
                "Sandbox Pack functions cannot create child processes"
            )

    sys.addaudithook(deny_process_event)
    return lambda: bool(violations)


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


_configure_utf8_stdio()


def _emit_error(message: str, error_type: str) -> None:
    print(json.dumps({"error": message, "error_type": error_type}))


def _load_input(args: argparse.Namespace) -> Dict[str, Any]:
    if args.input_file:
        raw = Path(args.input_file).read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Runner payload must be a JSON object")
    return payload


def _load_callable(module_path: str, callable_name: str):
    target = Path(module_path)
    if not target.is_file():
        raise FileNotFoundError(f"Module file not found: {module_path}")

    cwd = os.getcwd()
    module_dir = str(target.parent.resolve())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)

    spec = importlib.util.spec_from_file_location("rumi_runtime_target", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules["rumi_runtime_target"] = module
    spec.loader.exec_module(module)

    fn = getattr(module, callable_name, None)
    if fn is None:
        raise AttributeError(f"Callable '{callable_name}' not found")
    return fn


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a Python callable from JSON input.")
    parser.add_argument("--input-file", help="Path to JSON input file")
    parsed = parser.parse_args()

    try:
        payload = _load_input(parsed)
        module_path = payload.get("module_path", "")
        callable_name = payload.get("callable_name", "")
        context = payload.get("context", {})
        args = payload.get("args", {})

        if not module_path:
            _emit_error("No module_path specified", "config_error")
            return 1
        if not callable_name:
            _emit_error("No callable_name specified", "config_error")
            return 1

        process_policy_violated = _install_child_process_policy()
        fn = _load_callable(module_path, callable_name)
        result = fn(context, args)
        if process_policy_violated():
            raise SandboxProcessDenied(
                "Sandbox Pack functions cannot create child processes"
            )
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except json.JSONDecodeError as exc:
        _emit_error(f"Invalid input JSON: {exc}", "json_error")
        return 1
    except FileNotFoundError as exc:
        _emit_error(str(exc), "file_not_found")
        return 1
    except AttributeError as exc:
        _emit_error(str(exc), "func_not_found")
        return 1
    except RuntimeError as exc:
        _emit_error(str(exc), "load_error")
        return 1
    except SandboxProcessDenied as exc:
        _emit_error(str(exc), "sandbox_policy_denied")
        return 1
    except Exception as exc:
        _emit_error(str(exc), type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
