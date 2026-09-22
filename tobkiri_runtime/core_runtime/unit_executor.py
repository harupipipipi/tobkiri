"""
unit_executor.py - ユニット実行ゲート / モード選択

ユニット実行は必ずホスト側のゲートを通す。

実行モード:
- host_capability: subprocess で python/binary を実行（v1 で実装）
- pack_container: v1 では枠のみ (mode_not_implemented)
- sandbox: v1 では枠のみ (mode_not_implemented)

実行前チェック（必須）:
1. Pack承認チェック (ApprovalManager)
2. ストアとユニットを解決
3. mode検証 (exec_modes_allowed)
4. permission_id の階層grantチェック
4.5. kindホワイトリスト (ALLOWED_KINDS)
5. Trustチェック (kind=python/binary)
5.5. TOCTOU緩和
6. 実行＋監査ログ
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat as stat_module
import subprocess
import sys
import tempfile
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .compat import safe_chmod
from .paths import is_path_within


DEFAULT_TIMEOUT = 60.0
MAX_TIMEOUT = 300.0
MAX_RESPONSE_SIZE = 1 * 1024 * 1024

# --- A-11: Rate limiting constants ---
MAX_EXECUTIONS_PER_MINUTE = 30
RATE_WINDOW_SEC = 60.0

# --- Security: input format validation (A-4) ---
_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.\-]{1,128}$")

# --- Security: kind whitelist (I-02) ---
ALLOWED_KINDS = frozenset({"data", "python", "binary"})

# --- Security: subprocess environment whitelist (A-7) ---
SUBPROCESS_ENV_WHITELIST = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "PYTHONPATH", "PYTHONHASHSEED", "PYTHONDONTWRITEBYTECODE",
    "TMPDIR", "TMP", "TEMP",
})

# --- A-14: Fields to strip from external-facing result dicts ---
_RESULT_STRIP_KEYS = frozenset({"_stderr_head", "stderr", "stderr_head"})


@dataclass
class UnitExecutionResult:
    success: bool
    output: Any = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    execution_mode: str = "unknown"
    latency_ms: float = 0.0
    _stderr_head: Optional[str] = field(default=None, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        # A-14: Explicit guard — never expose stderr in external response
        d = {
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "error_type": self.error_type,
            "execution_mode": self.execution_mode,
            "latency_ms": self.latency_ms,
        }
        # Belt-and-suspenders: strip any stderr-related keys
        for k in _RESULT_STRIP_KEYS:
            d.pop(k, None)
        return d


class UnitExecutor:
    def __init__(self):
        self._lock = threading.Lock()
        # A-11: per-pack_id sliding window rate limiter
        self._rate_limiter: Dict[str, List[float]] = {}

    @staticmethod
    def _now_ts() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _build_subprocess_env() -> Dict[str, str]:
        """Build a filtered environment dict for subprocess execution."""
        return {
            k: v for k, v in os.environ.items()
            if k in SUBPROCESS_ENV_WHITELIST
        }

    def _get_rate_limit(self) -> int:
        """Return effective rate limit, allowing env-var override."""
        env_val = os.environ.get("RUMI_UNIT_RATE_LIMIT")
        if env_val is not None:
            try:
                v = int(env_val)
                if v > 0:
                    return v
            except (ValueError, TypeError):
                pass
        return MAX_EXECUTIONS_PER_MINUTE

    def _check_rate_limit(self, pack_id: str) -> Optional[UnitExecutionResult]:
        """
        A-11: Sliding-window rate limit check.

        Records the current timestamp and checks whether the pack has
        exceeded the maximum number of executions within RATE_WINDOW_SEC.

        Returns None if allowed, or a UnitExecutionResult if rate-limited.
        """
        now = time.monotonic()
        limit = self._get_rate_limit()
        with self._lock:
            timestamps = self._rate_limiter.get(pack_id)
            if timestamps is None:
                timestamps = []
                self._rate_limiter[pack_id] = timestamps

            # Prune timestamps outside the window
            cutoff = now - RATE_WINDOW_SEC
            while timestamps and timestamps[0] < cutoff:
                timestamps.pop(0)

            if len(timestamps) >= limit:
                return UnitExecutionResult(
                    success=False,
                    error=f"Rate limit exceeded: {limit} executions per {RATE_WINDOW_SEC}s",
                    error_type="rate_limit_exceeded",
                    execution_mode="unknown",
                )

            # Record this attempt
            timestamps.append(now)
        return None

    def execute(
        self,
        principal_id: str,
        unit_ref: Dict[str, str],
        mode: str,
        args: Dict[str, Any],
        timeout_seconds: float = DEFAULT_TIMEOUT,
    ) -> UnitExecutionResult:
        # A-13: Use monotonic clock for execution time measurement
        mono_start = time.monotonic()
        start_time = time.time()
        store_id = unit_ref.get("store_id", "")
        unit_id = unit_ref.get("unit_id", "")
        version = unit_ref.get("version", "")
        timeout_seconds = min(float(timeout_seconds), MAX_TIMEOUT)

        # A-13: Audit enrichment context (populated as we progress)
        audit_ctx: Dict[str, Any] = {
            "unit_kind": None,
            "exec_mode": mode,
            "entrypoint": None,
            "trust_verified": False,
        }

        if not principal_id:
            return self._denied(
                "Missing principal_id", "invalid_request",
                start_time, mode, principal_id, unit_ref,
                mono_start=mono_start, audit_extra=audit_ctx,
            )
        if not store_id or not unit_id or not version:
            return self._denied(
                "Missing store_id, unit_id, or version", "invalid_request",
                start_time, mode, principal_id, unit_ref,
                mono_start=mono_start, audit_extra=audit_ctx,
            )

        # A-4: 入力形式検証
        for _name, _val in (
            ("principal_id", principal_id),
            ("store_id", store_id),
            ("unit_id", unit_id),
            ("version", version),
        ):
            if not _ID_PATTERN.match(_val):
                return self._denied(
                    "Invalid input format", "invalid_request",
                    start_time, mode, principal_id, unit_ref,
                    internal_detail=f"Field '{_name}' failed format validation",
                    mono_start=mono_start, audit_extra=audit_ctx,
                )

        # A-11: Rate limit check (pack_id = principal_id)
        rate_result = self._check_rate_limit(principal_id)
        if rate_result is not None:
            rate_result.execution_mode = mode
            rate_result.latency_ms = (time.time() - start_time) * 1000
            # A-11+A-13: Audit the rate limit event
            self._audit_execution(
                principal_id, unit_ref, mode, rate_result,
                audit_extra={
                    **audit_ctx,
                    "execution_time_ms": (time.monotonic() - mono_start) * 1000,
                    "denial_reason": "rate_limit_exceeded",
                },
            )
            return rate_result

        # 1. Pack 承認チェック
        try:
            from .approval_manager import get_approval_manager
            am = get_approval_manager()
            is_valid, reason = am.is_pack_approved_and_verified(principal_id)
            if not is_valid:
                return self._denied(
                    "Pack not approved", "approval_denied",
                    start_time, mode, principal_id, unit_ref,
                    internal_detail=f"Pack not approved: {reason}",
                    mono_start=mono_start, audit_extra=audit_ctx,
                )
        except Exception as e:
            return self._denied(
                "Approval check failed", "approval_error",
                start_time, mode, principal_id, unit_ref,
                internal_detail=f"Approval check failed: {e}",
                mono_start=mono_start, audit_extra=audit_ctx,
            )

        # 2. ストアとユニットを解決
        try:
            from .store_registry import get_store_registry
            from .unit_registry import get_unit_registry, UnitRef as UReg

            store_reg = get_store_registry()
            store_def = store_reg.get_store(store_id)
            if store_def is None:
                return self._denied(
                    "Store not found", "store_not_found",
                    start_time, mode, principal_id, unit_ref,
                    internal_detail=f"Store not found: {store_id}",
                    mono_start=mono_start, audit_extra=audit_ctx,
                )

            unit_reg = get_unit_registry()
            unit_ref_obj = UReg(
                store_id=store_id, unit_id=unit_id, version=version,
            )
            store_root = Path(store_def.root_path)
            unit_meta = unit_reg.get_unit_by_ref(store_root, unit_ref_obj)
            if unit_meta is None:
                return self._denied(
                    "Unit not found", "unit_not_found",
                    start_time, mode, principal_id, unit_ref,
                    internal_detail=f"Unit not found: {unit_id} v{version}",
                    mono_start=mono_start, audit_extra=audit_ctx,
                )
        except Exception as e:
            return self._denied(
                "Unit resolution failed", "resolution_error",
                start_time, mode, principal_id, unit_ref,
                internal_detail=f"Unit resolution failed: {e}",
                mono_start=mono_start, audit_extra=audit_ctx,
            )

        # A-13: Populate audit context now that we have unit_meta
        audit_ctx["unit_kind"] = unit_meta.kind
        audit_ctx["entrypoint"] = (
            Path(unit_meta.entrypoint).name
            if unit_meta.entrypoint else None
        )

        # 3. mode 検証
        if mode not in unit_meta.exec_modes_allowed:
            return self._denied(
                "Mode not allowed",
                "mode_not_allowed", start_time, mode, principal_id, unit_ref,
                internal_detail=f"Mode '{mode}' not in exec_modes_allowed: {unit_meta.exec_modes_allowed}",
                mono_start=mono_start, audit_extra=audit_ctx,
            )

        # 4. permission_id の階層 grant チェック
        if unit_meta.permission_id:
            try:
                from .capability_grant_manager import get_capability_grant_manager
                gm = get_capability_grant_manager()
                grant_result = gm.check(principal_id, unit_meta.permission_id)
                if not grant_result.allowed:
                    return self._denied(
                        "Permission denied",
                        "grant_denied",
                        start_time, mode, principal_id, unit_ref,
                        internal_detail=f"Permission denied: {grant_result.reason}",
                        mono_start=mono_start, audit_extra=audit_ctx,
                    )
            except Exception as e:
                return self._denied(
                    "Grant check failed", "grant_error",
                    start_time, mode, principal_id, unit_ref,
                    internal_detail=f"Grant check failed: {e}",
                    mono_start=mono_start, audit_extra=audit_ctx,
                )

        # 4.5. kind ホワイトリスト (I-02)
        if unit_meta.kind not in ALLOWED_KINDS:
            return self._denied(
                f"Unknown kind: {unit_meta.kind}",
                "unknown_kind",
                start_time, mode, principal_id, unit_ref,
                internal_detail=f"Unknown kind: {unit_meta.kind}",
                mono_start=mono_start, audit_extra=audit_ctx,
            )

        # 5. Trust チェック（kind=python/binary のみ）
        verified_content: Optional[bytes] = None
        trust_sha256: Optional[str] = None
        verified_entrypoint_path: Optional[Path] = None
        if unit_meta.kind in ("python", "binary"):
            unit_dir = unit_meta.unit_dir
            entrypoint = unit_meta.entrypoint
            if unit_dir is None or not entrypoint:
                return self._denied(
                    "No entrypoint for executable unit",
                    "missing_entrypoint",
                    start_time, mode, principal_id, unit_ref,
                    mono_start=mono_start, audit_extra=audit_ctx,
                )
            verified_entrypoint_path = unit_dir / entrypoint
            try:
                from .unit_registry import get_unit_registry as _gur
                ur = _gur()
                actual_sha256 = ur.compute_entrypoint_sha256(
                    unit_dir, entrypoint,
                )
                if actual_sha256 is None:
                    return self._denied(
                        "Failed to compute entrypoint sha256",
                        "trust_error",
                        start_time, mode, principal_id, unit_ref,
                        mono_start=mono_start, audit_extra=audit_ctx,
                    )
                trust_sha256 = actual_sha256

                from .unit_trust_store import get_unit_trust_store
                trust = get_unit_trust_store()
                if not trust.is_loaded():
                    trust.load()
                trust_result = trust.is_trusted(unit_id, version, actual_sha256)
                if not trust_result.trusted:
                    return self._denied(
                        "Trust check denied",
                        "trust_denied",
                        start_time, mode, principal_id, unit_ref,
                        internal_detail=f"Unit trust denied: {trust_result.reason}",
                        mono_start=mono_start, audit_extra=audit_ctx,
                    )
            except Exception as e:
                return self._denied(
                    "Trust check failed", "trust_error",
                    start_time, mode, principal_id, unit_ref,
                    internal_detail=f"Trust check failed: {e}",
                    mono_start=mono_start, audit_extra=audit_ctx,
                )

        # A-13: Mark trust_verified in audit context
        audit_ctx["trust_verified"] = trust_sha256 is not None

        # 5.5. TOCTOU 緩和 (I-03): Trust チェック後にファイル内容を読み込み二重検証
        if trust_sha256 is not None and verified_entrypoint_path is not None:
            ep_path = verified_entrypoint_path
            try:
                content = ep_path.read_bytes()
            except Exception as e:
                return self._denied(
                    "TOCTOU verification failed",
                    "toctou_read_error",
                    start_time, mode, principal_id, unit_ref,
                    internal_detail=f"Failed to read entrypoint for TOCTOU verification: {e}",
                    mono_start=mono_start, audit_extra=audit_ctx,
                )
            content_sha256 = hashlib.sha256(content).hexdigest()
            if content_sha256 != trust_sha256:
                return self._denied(
                    "Entrypoint content changed after trust check (TOCTOU detected)",
                    "toctou_mismatch",
                    start_time, mode, principal_id, unit_ref,
                    mono_start=mono_start, audit_extra=audit_ctx,
                )
            verified_content = content

        # 6. 実行
        if mode == "host_capability":
            result = self._execute_host_capability(
                unit_meta, args, timeout_seconds, start_time, verified_content,
            )
        elif mode == "pack_container":
            result = UnitExecutionResult(
                success=False,
                error="Mode 'pack_container' is not yet implemented",
                error_type="mode_not_implemented",
                execution_mode=mode,
                latency_ms=(time.time() - start_time) * 1000,
            )
        elif mode == "sandbox":
            result = UnitExecutionResult(
                success=False,
                error="Mode 'sandbox' is not yet implemented",
                error_type="mode_not_implemented",
                execution_mode=mode,
                latency_ms=(time.time() - start_time) * 1000,
            )
        else:
            result = UnitExecutionResult(
                success=False,
                error=f"Unknown mode: {mode}",
                error_type="invalid_mode",
                execution_mode=mode,
                latency_ms=(time.time() - start_time) * 1000,
            )

        # A-13: Compute execution_time_ms from monotonic clock
        audit_ctx["execution_time_ms"] = (time.monotonic() - mono_start) * 1000

        self._audit_execution(
            principal_id, unit_ref, mode, result, audit_extra=audit_ctx,
        )
        return result

    def _execute_host_capability(
        self,
        unit_meta,
        args: Dict[str, Any],
        timeout_seconds: float,
        start_time: float,
        verified_content: Optional[bytes] = None,
    ) -> UnitExecutionResult:
        if unit_meta.kind == "python":
            return self._execute_python_host(
                unit_meta, args, timeout_seconds, start_time, verified_content,
            )
        elif unit_meta.kind == "binary":
            return self._execute_binary_host(
                unit_meta, args, timeout_seconds, start_time, verified_content,
            )
        else:
            return UnitExecutionResult(
                success=False,
                error=f"host_capability does not support kind={unit_meta.kind}",
                error_type="unsupported_kind",
                execution_mode="host_capability",
                latency_ms=(time.time() - start_time) * 1000,
            )

    def _execute_python_host(
        self,
        unit_meta,
        args: Dict[str, Any],
        timeout_seconds: float,
        start_time: float,
        verified_content: Optional[bytes] = None,
    ) -> UnitExecutionResult:
        ep_path = unit_meta.unit_dir / unit_meta.entrypoint
        try:
            if not ep_path.exists():
                return UnitExecutionResult(
                    success=False,
                    error="Entrypoint not found",
                    error_type="entrypoint_not_found",
                    execution_mode="host_capability",
                    latency_ms=(time.time() - start_time) * 1000,
                )
            if ep_path.is_symlink():
                return UnitExecutionResult(
                    success=False,
                    error="Symlink entrypoint not allowed",
                    error_type="symlink_denied",
                    execution_mode="host_capability",
                    latency_ms=(time.time() - start_time) * 1000,
                )
        except OSError:
            return UnitExecutionResult(
                success=False,
                error="Failed to stat entrypoint for security check",
                error_type="internal_error",
                execution_mode="host_capability",
                latency_ms=(time.time() - start_time) * 1000,
            )
        if not is_path_within(ep_path, unit_meta.unit_dir):
            return UnitExecutionResult(
                success=False,
                error="Path traversal in entrypoint",
                error_type="path_traversal",
                execution_mode="host_capability",
                latency_ms=(time.time() - start_time) * 1000,
            )

        # TOCTOU 緩和: verified_content がある場合は一時ファイル経由で実行
        verified_ep_file: Optional[str] = None
        runner_file = None
        target_ep_path = str(ep_path)
        if verified_content is not None:
            try:
                fd, verified_ep_file = tempfile.mkstemp(
                    suffix=".py", prefix="rumi_verified_ep_",
                )
                try:
                    written = os.write(fd, verified_content)
                    if written != len(verified_content):
                        raise OSError(f"Partial write: {written}/{len(verified_content)}")
                finally:
                    os.close(fd)
                safe_chmod(verified_ep_file, 0o500)
                target_ep_path = verified_ep_file
            except Exception:
                if verified_ep_file:
                    try:
                        os.unlink(verified_ep_file)
                    except Exception:
                        pass
                return UnitExecutionResult(
                    success=False,
                    error="Failed to create verified entrypoint temp file",
                    error_type="internal_error",
                    execution_mode="host_capability",
                    latency_ms=(time.time() - start_time) * 1000,
                )

        runner = self._generate_python_runner(target_ep_path)
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, encoding="utf-8",
            ) as f:
                f.write(runner)
                runner_file = f.name

            input_json = json.dumps(
                {"args": args}, ensure_ascii=False, default=str,
            )
            proc = subprocess.run(
                [sys.executable, runner_file],
                input=input_json,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=str(unit_meta.unit_dir),
                env=self._build_subprocess_env(),
            )
            latency = (time.time() - start_time) * 1000

            # A-14: Log stderr but never include in result
            stderr_head = (proc.stderr or "")[:500] or None

            if proc.returncode != 0:
                return UnitExecutionResult(
                    success=False,
                    error="Unit execution failed",
                    error_type="execution_error",
                    execution_mode="host_capability",
                    latency_ms=latency,
                    _stderr_head=stderr_head,
                )

            stdout = proc.stdout or ""
            # A-14: Enforce MAX_RESPONSE_SIZE on stdout
            if len(stdout.encode("utf-8")) > MAX_RESPONSE_SIZE:
                return UnitExecutionResult(
                    success=False,
                    error="Response too large",
                    error_type="response_too_large",
                    execution_mode="host_capability",
                    latency_ms=latency,
                )

            output = None
            if stdout.strip():
                try:
                    output = json.loads(stdout.strip())
                except json.JSONDecodeError:
                    output = stdout.strip()

            return UnitExecutionResult(
                success=True,
                output=output,
                execution_mode="host_capability",
                latency_ms=latency,
            )
        except subprocess.TimeoutExpired:
            return UnitExecutionResult(
                success=False,
                error=f"Timed out after {timeout_seconds}s",
                error_type="timeout",
                execution_mode="host_capability",
                latency_ms=(time.time() - start_time) * 1000,
            )
        except Exception:
            return UnitExecutionResult(
                success=False,
                error="Internal execution error",
                error_type="internal_error",
                execution_mode="host_capability",
                latency_ms=(time.time() - start_time) * 1000,
            )
        finally:
            if runner_file:
                try:
                    os.unlink(runner_file)
                except Exception:
                    pass
            if verified_ep_file:
                try:
                    os.unlink(verified_ep_file)
                except Exception:
                    pass

    def _execute_binary_host(
        self,
        unit_meta,
        args: Dict[str, Any],
        timeout_seconds: float,
        start_time: float,
        verified_content: Optional[bytes] = None,
    ) -> UnitExecutionResult:
        ep_path = unit_meta.unit_dir / unit_meta.entrypoint
        try:
            if not ep_path.exists():
                return UnitExecutionResult(
                    success=False,
                    error="Entrypoint not found",
                    error_type="entrypoint_not_found",
                    execution_mode="host_capability",
                    latency_ms=(time.time() - start_time) * 1000,
                )
            if ep_path.is_symlink():
                return UnitExecutionResult(
                    success=False,
                    error="Symlink entrypoint not allowed",
                    error_type="symlink_denied",
                    execution_mode="host_capability",
                    latency_ms=(time.time() - start_time) * 1000,
                )
        except OSError:
            return UnitExecutionResult(
                success=False,
                error="Failed to stat entrypoint for security check",
                error_type="internal_error",
                execution_mode="host_capability",
                latency_ms=(time.time() - start_time) * 1000,
            )
        if not is_path_within(ep_path, unit_meta.unit_dir):
            return UnitExecutionResult(
                success=False,
                error="Path traversal in entrypoint",
                error_type="path_traversal",
                execution_mode="host_capability",
                latency_ms=(time.time() - start_time) * 1000,
            )

        # I-06: setuid/setgid チェック（Windows 以外のみ）
        if platform.system() != "Windows":
            try:
                ep_stat = os.stat(str(ep_path))
                if ep_stat.st_mode & (stat_module.S_ISUID | stat_module.S_ISGID):
                    return UnitExecutionResult(
                        success=False,
                        error="Entrypoint has setuid/setgid bits set",
                        error_type="security_violation",
                        execution_mode="host_capability",
                        latency_ms=(time.time() - start_time) * 1000,
                    )
            except OSError:
                return UnitExecutionResult(
                    success=False,
                    error="Failed to stat entrypoint for security check",
                    error_type="internal_error",
                    execution_mode="host_capability",
                    latency_ms=(time.time() - start_time) * 1000,
                )

        # TOCTOU 緩和: verified_content がある場合は一時ファイル経由で実行
        verified_bin_file: Optional[str] = None
        target_bin_path = str(ep_path)
        if verified_content is not None:
            try:
                fd, verified_bin_file = tempfile.mkstemp(
                    prefix="rumi_verified_bin_",
                )
                try:
                    written = os.write(fd, verified_content)
                    if written != len(verified_content):
                        raise OSError(f"Partial write: {written}/{len(verified_content)}")
                finally:
                    os.close(fd)
                safe_chmod(verified_bin_file, 0o500)
                target_bin_path = verified_bin_file
            except Exception:
                if verified_bin_file:
                    try:
                        os.unlink(verified_bin_file)
                    except Exception:
                        pass
                return UnitExecutionResult(
                    success=False,
                    error="Failed to create verified binary temp file",
                    error_type="internal_error",
                    execution_mode="host_capability",
                    latency_ms=(time.time() - start_time) * 1000,
                )

        try:
            input_json = json.dumps(
                {"args": args}, ensure_ascii=False, default=str,
            )
            proc = subprocess.run(
                [target_bin_path],
                input=input_json,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=str(unit_meta.unit_dir),
                env=self._build_subprocess_env(),
            )
            latency = (time.time() - start_time) * 1000

            # A-14: Log stderr but never include in result
            stderr_head = (proc.stderr or "")[:500] or None

            if proc.returncode != 0:
                return UnitExecutionResult(
                    success=False,
                    error="Unit execution failed",
                    error_type="execution_error",
                    execution_mode="host_capability",
                    latency_ms=latency,
                    _stderr_head=stderr_head,
                )

            stdout = proc.stdout or ""

            # A-14: Enforce MAX_RESPONSE_SIZE on stdout (parity with python host)
            if len(stdout.encode("utf-8")) > MAX_RESPONSE_SIZE:
                return UnitExecutionResult(
                    success=False,
                    error="Response too large",
                    error_type="response_too_large",
                    execution_mode="host_capability",
                    latency_ms=latency,
                )

            output = None
            if stdout.strip():
                try:
                    output = json.loads(stdout.strip())
                except json.JSONDecodeError:
                    output = stdout.strip()

            return UnitExecutionResult(
                success=True,
                output=output,
                execution_mode="host_capability",
                latency_ms=latency,
            )
        except subprocess.TimeoutExpired:
            return UnitExecutionResult(
                success=False,
                error=f"Timed out after {timeout_seconds}s",
                error_type="timeout",
                execution_mode="host_capability",
                latency_ms=(time.time() - start_time) * 1000,
            )
        except Exception:
            return UnitExecutionResult(
                success=False,
                error="Internal execution error",
                error_type="internal_error",
                execution_mode="host_capability",
                latency_ms=(time.time() - start_time) * 1000,
            )
        finally:
            if verified_bin_file:
                try:
                    os.unlink(verified_bin_file)
                except Exception:
                    pass

    def _generate_python_runner(self, handler_py_path: str) -> str:
        safe_path = json.dumps(handler_py_path)
        return f"""
import sys, json, importlib.util

def main():
    input_data = json.loads(sys.stdin.read())
    args = input_data.get("args", {{}})

    spec = importlib.util.spec_from_file_location("unit_module", {safe_path})
    if spec is None or spec.loader is None:
        print(json.dumps({{"error": "Cannot load module"}}))
        sys.exit(1)

    module = importlib.util.module_from_spec(spec)
    sys.modules["unit_module"] = module
    spec.loader.exec_module(module)

    fn = getattr(module, "execute", None) or getattr(module, "run", None) or getattr(module, "main", None)
    if fn is None:
        print(json.dumps({{"error": "No execute/run/main function"}}))
        sys.exit(1)

    try:
        result = fn(args)
    except Exception as e:
        print(json.dumps({{"error": str(e)}}))
        sys.exit(1)

    if result is not None:
        try:
            print(json.dumps(result, ensure_ascii=False, default=str))
        except Exception:
            print(json.dumps({{"error": "Result not serializable"}}))
            sys.exit(1)

if __name__ == "__main__":
    main()
"""

    def _denied(
        self,
        error: str,
        error_type: str,
        start_time: float,
        mode: str,
        principal_id: str,
        unit_ref: Dict[str, str],
        internal_detail: str = "",
        *,
        mono_start: Optional[float] = None,
        audit_extra: Optional[Dict[str, Any]] = None,
    ) -> UnitExecutionResult:
        result = UnitExecutionResult(
            success=False,
            error=error,
            error_type=error_type,
            execution_mode=mode,
            latency_ms=(time.time() - start_time) * 1000,
        )
        # A-13: Build enriched audit_extra with denial_reason
        extra = dict(audit_extra) if audit_extra else {}
        if mono_start is not None:
            elapsed_ms = (time.monotonic() - mono_start) * 1000
            if elapsed_ms <= 0:
                try:
                    elapsed_ms = max(elapsed_ms, (time.perf_counter() - mono_start) * 1000)
                except Exception:
                    pass
            extra["execution_time_ms"] = max(elapsed_ms, 0.0)
        extra["denial_reason"] = error
        self._audit_execution(
            principal_id, unit_ref, mode, result, internal_detail,
            audit_extra=extra,
        )
        return result

    @staticmethod
    def _audit_execution(
        principal_id: str,
        unit_ref: Dict[str, str],
        mode: str,
        result: UnitExecutionResult,
        internal_detail: str = "",
        *,
        audit_extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            details: Dict[str, Any] = {
                "unit_ref": unit_ref,
                "mode": mode,
                "latency_ms": result.latency_ms,
                "error_type": result.error_type,
            }
            if getattr(result, '_stderr_head', None):
                details["stderr_head"] = result._stderr_head
            if internal_detail:
                details["internal_detail"] = internal_detail
            # A-13: Merge enrichment fields
            if audit_extra:
                details.update(audit_extra)
            audit.log_permission_event(
                pack_id=principal_id,
                permission_type="unit_execution",
                action="execute",
                success=result.success,
                details=details,
                rejection_reason=result.error if not result.success and result.error else "",
            )
        except Exception:
            pass


_global_unit_executor: Optional[UnitExecutor] = None
_executor_lock = threading.Lock()


def get_unit_executor() -> UnitExecutor:
    """
    グローバルなUnitExecutorを取得する。

    DI コンテナ経由で遅延初期化・キャッシュされる。
    """
    from .di_container import get_container
    return get_container().get("unit_executor")


def reset_unit_executor() -> UnitExecutor:
    """UnitExecutorをリセット（テスト用）"""
    global _global_unit_executor
    from .di_container import get_container
    container = get_container()
    new = UnitExecutor()
    with _executor_lock:
        _global_unit_executor = new
    container.set_instance("unit_executor", new)
    return new
