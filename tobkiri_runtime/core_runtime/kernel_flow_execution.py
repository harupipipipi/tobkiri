"""
kernel_flow_execution.py - Flow実行系 Mixin (Mixin分割版)

Flow の実行（同期 pipeline / async Flow）に関するロジックを提供する。
Mixin方式で Kernel クラスに合成される。

含まれるメソッド:
- run_startup / run_pipeline (同期 pipeline 実行)
- execute_flow / execute_flow_sync (async/sync Flow 実行エントリ)
- _execute_flow_internal / _execute_steps_async (async 実行内部)
- _execute_handler_step_async / _execute_sub_flow_step (ステップ実行)
- _execute_function_step_async (Wave 27-C: function.call ステップ)
- _eval_condition (条件式評価)
- _execute_flow_step (同期 pipeline 用ステップ実行)
- _check_depends_on (Wave 10-C: depends_on 実行時チェック)

依存する self 属性 (KernelCore.__init__ で初期化済み前提):
    self.config             : KernelConfig
    self.diagnostics        : Diagnostics
    self.interface_registry : InterfaceRegistry
    self.event_bus          : EventBus
    self._flow              : Optional[Dict]
    self._executor          : ThreadPoolExecutor
    self._flow_converter    : FlowConverter
    self._variable_resolver : VariableResolver

依存する self メソッド (KernelCore / 他 Mixin で定義):
    self._now_ts()
    self._build_kernel_context()
    self._resolve_value(value, ctx, depth=0)
    self._resolve_handler(handler, args=None)
    self.load_flow(path=None)
    self.load_user_flows(path=None)
    self._load_single_flow(flow_path)
    self._vocab_normalize_output(unwrapped, step, ctx)

Wave 10-C: depends_on 実行時チェック追加
- ステップ実行ループに depends_on 依存チェック追加
- 実行済みステップID集合で追跡
- fail_soft: 未実行依存をスキップ+警告
- depends_on なし/空はゼロコスト

Wave 15-B: 基盤モジュール統合
- logging → get_structured_logger 移行
- Profiler でFlow/ステップ実行計測
- MetricsCollector でステップ成功/失敗/Flow完了カウント
- 計測エラーでFlow実行が失敗しないよう try-except で防護

Wave 27-D1: 同期 pipeline での function step 対応
- _execute_flow_step() に function step の分岐を追加
- step dict に function フィールドがある場合、function step として実行

Wave 27-D2: function step への vocab_normalize 追加
- _execute_function_step_async() の結果格納前に vocab_normalize を適用 (opt-in)
"""

from __future__ import annotations

import asyncio
import copy
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol, Set, Tuple

from .paths import BASE_DIR
from .flow_context_security import sanitize_user_flow_context

from .logging_utils import get_structured_logger
from .profiling import get_profiler
from .metrics import get_metrics_collector
from .kernel_facade import KernelFacade
from .diagnostics import Diagnostics, Status


class _InterfaceRegistryPort(Protocol):
    def get(self, key: str, strategy: str = "first") -> Any: ...

    def list(self) -> Dict[str, Any]: ...

    def register(
        self,
        key: str,
        value: Any,
        *,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None: ...

_logger = get_structured_logger("rumi.kernel.flow_execution")


# --- Flow chain / resolve depth limits (Fix #58, #70) ---
MAX_FLOW_CHAIN_DEPTH = 10

# --- Condition parser pattern (Fix #16, Wave 27-A: comparison operators) ---
_CONDITION_OP_RE = re.compile(r'\s+(==|!=|>=|<=|>|<)\s+')



# ── universal_call constants ────────────────────────────────
_UC_MAX_RESPONSE_SIZE = 1 * 1024 * 1024   # 1 MiB
_UC_MAX_TIMEOUT = 120.0                    # seconds
_UC_DEFAULT_TIMEOUT = 30.0                 # seconds
_UC_VALID_RUNTIMES = frozenset({"python", "binary", "command"})
_FLOW_AUTHORIZATION_SOURCE_UNSET = object()
_FLOW_AUTHORIZATION_SOURCE_METADATA = ContextVar(
    "rumi_flow_authorization_source_metadata",
    default=_FLOW_AUTHORIZATION_SOURCE_UNSET,
)


def _approval_check_allowed(value: object) -> bool:
    """Normalize the verified approval result, including legacy test doubles."""
    if isinstance(value, tuple):
        return bool(value) and value[0] is True
    return value is True

class KernelFlowExecutionMixin:
    """
    Flow実行系 Mixin

    __init__ を持たない。self の属性は KernelCore.__init__ で初期化済みの
    前提でアクセスする。
    """

    _PACK_FLOW_SOURCE_TYPES = frozenset({"pack", "local_pack"})
    _PACK_FLOW_ALLOWED_KERNEL_HANDLERS = frozenset({
        "kernel:noop",
        "kernel:ctx.set",
        "kernel:ctx.get",
        "kernel:ctx.copy",
        "kernel:python_file_call",
        "kernel:graph.compile",
    })
    _startup_ctx: Optional[Dict[str, Any]]
    _startup_steps: Optional[List[Dict[str, Any]]]
    _startup_next_index: int
    _startup_executed_ids: Set[str]
    _startup_fail_soft_default: bool
    diagnostics: Diagnostics
    interface_registry: _InterfaceRegistryPort
    _executor: ThreadPoolExecutor
    _flow: Optional[Dict[str, Any]]

    if TYPE_CHECKING:
        def load_user_flows(self, path: Optional[str] = None) -> None: ...

        def load_flow(self, path: Optional[str] = None) -> Dict[str, Any]: ...

        def _build_kernel_context(self) -> Dict[str, Any]: ...

        def _resolve_value(
            self,
            value: Any,
            ctx: Dict[str, Any],
            depth: int = 0,
        ) -> Any: ...

        def _resolve_handler(
            self,
            handler: str,
            args: Optional[Dict[str, Any]] = None,
        ) -> Any: ...

        def _load_single_flow(self, flow_path: Path) -> Dict[str, Any]: ...

        def _vocab_normalize_output(
            self,
            unwrapped: Any,
            step: Dict[str, Any],
            ctx: Dict[str, Any],
        ) -> Any: ...

    # ------------------------------------------------------------------
    # Wave 10-C: depends_on チェック
    # ------------------------------------------------------------------

    @staticmethod
    def _get_step_depends_on(step: Any) -> Optional[List[str]]:
        """ステップから depends_on を安全に取得する。

        dict の場合は .get()、オブジェクトの場合は getattr() で取得。
        depends_on 属性が存在しない旧形式にも対応する。
        """
        if isinstance(step, dict):
            return step.get("depends_on")
        return getattr(step, "depends_on", None)

    @staticmethod
    def _snapshot_flow_authorization_metadata(ctx: Dict[str, Any]) -> Dict[str, Any]:
        """Copy source metadata before flow steps can mutate kernel-owned ctx keys."""
        return {
            "source_type": ctx.get("_flow_source_type"),
            "source_file": ctx.get("_flow_source_file"),
            "source_pack_id": ctx.get("_flow_source_pack_id"),
        }

    def _get_flow_source_metadata_for_authorization(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        """Return the immutable flow source metadata recorded for this step scope."""
        metadata = _FLOW_AUTHORIZATION_SOURCE_METADATA.get()
        if metadata is _FLOW_AUTHORIZATION_SOURCE_UNSET:
            return self._snapshot_flow_authorization_metadata(ctx)
        if isinstance(metadata, dict):
            return dict(metadata)
        return {"source_type": metadata, "source_file": None, "source_pack_id": None}

    def _get_flow_source_type_for_authorization(self, ctx: Dict[str, Any]) -> str:
        """Return the immutable flow source type recorded when step execution started."""
        metadata = self._get_flow_source_metadata_for_authorization(ctx)
        return str(metadata.get("source_type") or "").strip()

    def _ensure_flow_authorization_context(self, ctx: Dict[str, Any]) -> Any:
        """Snapshot mutable source metadata before any flow step can alter ctx."""
        return _FLOW_AUTHORIZATION_SOURCE_METADATA.set(
            self._snapshot_flow_authorization_metadata(ctx)
        )

    def _clear_flow_authorization_context(self, token: Any) -> None:
        """Remove the immutable source snapshot for a completed flow context."""
        _FLOW_AUTHORIZATION_SOURCE_METADATA.reset(token)

    def _is_pack_flow_context(self, ctx: Dict[str, Any]) -> bool:
        """Return True when the current async flow originated from an approved pack."""
        return self._get_flow_source_type_for_authorization(ctx) in self._PACK_FLOW_SOURCE_TYPES

    def _is_kernel_handler_allowed_for_pack_flow(self, handler_key: str) -> bool:
        """Allow only inert/sandboxed kernel handlers needed by pack flow syntax."""
        return handler_key in self._PACK_FLOW_ALLOWED_KERNEL_HANDLERS

    def _should_block_kernel_handler_for_flow(self, handler_key: str, ctx: Dict[str, Any]) -> bool:
        """Prevent pack-provided flows from invoking privileged internal kernel handlers."""
        return (
            isinstance(handler_key, str)
            and handler_key.startswith("kernel:")
            and self._is_pack_flow_context(ctx)
            and not self._is_kernel_handler_allowed_for_pack_flow(handler_key)
        )

    def _record_blocked_kernel_handler_step(
        self,
        step_id: Any,
        handler_key: str,
        ctx: Dict[str, Any],
        *,
        phase: str = "flow",
    ) -> None:
        """Record an authorization skip for a blocked pack-flow kernel handler."""
        source_metadata = self._get_flow_source_metadata_for_authorization(ctx)
        self.diagnostics.record_step(
            phase=phase,
            step_id=f"{step_id or 'unknown'}.kernel_handler.blocked",
            handler=handler_key,
            status="skipped",
            meta={
                "reason": "pack_flow_kernel_handler_not_allowed",
                "flow_id": ctx.get("_flow_id"),
                "source_type": str(source_metadata.get("source_type") or "").strip(),
                "source_file": source_metadata.get("source_file"),
                "source_pack_id": source_metadata.get("source_pack_id"),
            },
        )

    def _check_depends_on(
        self, step: Any, executed_ids: Set[str]
    ) -> Tuple[bool, List[str]]:
        """ステップの depends_on をチェックする。

        Args:
            step: ステップ (dict or FlowStep or any object)
            executed_ids: 実行済みステップIDの集合

        Returns:
            (should_execute, missing_deps):
            - depends_on が None or 空 → (True, [])  ゼロコスト
            - 全ID が executed_ids に含まれる → (True, [])
            - 含まれないIDがある → (False, [missing_ids])
        """
        deps = self._get_step_depends_on(step)
        if not deps:
            # None or 空リスト → チェックスキップ（ゼロコスト）
            return True, []
        missing = [d for d in deps if d not in executed_ids]
        if missing:
            return False, missing
        return True, []

    # ------------------------------------------------------------------
    # Startup / Pipeline 実行 (同期)
    # ------------------------------------------------------------------

    def _prepare_startup_pipeline(self) -> None:
        if self._startup_steps is not None and self._startup_ctx is not None:
            return

        self.load_user_flows()
        flow = self._flow or self.load_flow()
        defaults = flow.get("defaults", {}) if isinstance(flow, dict) else {}
        fail_soft_default = bool(defaults.get("fail_soft", True))
        on_missing_handler = str(defaults.get("on_missing_handler", "skip")).strip().lower()
        pipelines = flow.get("pipelines", {})
        startup_steps = pipelines.get("startup", []) if isinstance(pipelines, dict) else []
        startup_steps = startup_steps if isinstance(startup_steps, list) else []

        ctx = self._build_kernel_context()
        ctx["_flow_defaults"] = {
            "fail_soft": fail_soft_default,
            "on_missing_handler": on_missing_handler,
        }

        self._startup_ctx = ctx
        self._startup_steps = startup_steps
        self._startup_next_index = 0
        self._startup_executed_ids = set()
        self._startup_fail_soft_default = fail_soft_default

    def _reset_startup_pipeline(self) -> None:
        self._startup_ctx = None
        self._startup_steps = None
        self._startup_next_index = 0
        self._startup_executed_ids = set()
        self._startup_fail_soft_default = True

    def _run_prepared_startup(self, stop_after_step_id: Optional[str] = None) -> Dict[str, Any]:
        self._prepare_startup_pipeline()
        steps = self._startup_steps or []
        ctx = self._startup_ctx or {}
        fail_soft_default = self._startup_fail_soft_default

        if self._startup_next_index == 0:
            self.diagnostics.record_step(
                phase="startup",
                step_id="startup.pipeline.start",
                handler="kernel:startup.run",
                status="success",
                meta={"step_count": len(steps)},
            )

        aborted = False
        for index in range(self._startup_next_index, len(steps)):
            step = steps[index]
            step_id_for_dep = step.get("id") if isinstance(step, dict) else getattr(step, "id", None)
            dep_ok, dep_missing = self._check_depends_on(step, self._startup_executed_ids)
            if not dep_ok:
                if fail_soft_default:
                    _logger.warning(
                        f"Step '{step_id_for_dep}' skipped: depends_on not satisfied (missing: {dep_missing})",
                    )
                    self.diagnostics.record_step(
                        phase="startup",
                        step_id=f"{step_id_for_dep or 'unknown'}.depends_on.skipped",
                        handler="kernel:depends_on_check",
                        status="skipped",
                        meta={"missing_deps": dep_missing},
                    )
                    self._startup_next_index = index + 1
                    if step_id_for_dep == stop_after_step_id:
                        break
                    continue

                self.diagnostics.record_step(
                    phase="startup",
                    step_id=f"{step_id_for_dep or 'unknown'}.depends_on.abort",
                    handler="kernel:depends_on_check",
                    status="failed",
                    meta={"missing_deps": dep_missing},
                )
                aborted = True
                break

            try:
                aborted = self._execute_flow_step(step, phase="startup", ctx=ctx)
                if not aborted and step_id_for_dep:
                    self._startup_executed_ids.add(step_id_for_dep)
            except Exception as e:
                self.diagnostics.record_step(
                    phase="startup",
                    step_id="startup.pipeline.internal_error",
                    handler="kernel:startup.run",
                    status="failed",
                    error=e,
                )
                if not fail_soft_default:
                    aborted = True
                    break

            self._startup_next_index = index + 1
            if aborted or step_id_for_dep == stop_after_step_id:
                break

        completed = self._startup_next_index >= len(steps)
        if completed or aborted:
            self.diagnostics.record_step(
                phase="startup",
                step_id="startup.pipeline.end",
                handler="kernel:startup.run",
                status="success" if not aborted else "failed",
                meta={"aborted": aborted},
            )
            self._reset_startup_pipeline()

        return self.diagnostics.as_dict()

    def run_startup(self) -> Dict[str, Any]:
        return self._run_prepared_startup()

    def run_startup_until(self, step_id: str) -> Dict[str, Any]:
        return self._run_prepared_startup(stop_after_step_id=step_id)

    def run_startup_remaining(self) -> Dict[str, Any]:
        return self._run_prepared_startup()

    def run_pipeline(
        self,
        pipeline_name: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        flow = self._flow or self.load_flow()
        defaults = flow.get("defaults", {}) if isinstance(flow, dict) else {}
        fail_soft_default = bool(defaults.get("fail_soft", True))
        pipelines = flow.get("pipelines", {})
        steps = pipelines.get(pipeline_name, []) if isinstance(pipelines, dict) else []
        steps = steps if isinstance(steps, list) else []

        ctx = self._build_kernel_context()
        ctx["_flow_defaults"] = {
            "fail_soft": fail_soft_default,
            "on_missing_handler": str(defaults.get("on_missing_handler", "skip")).lower()
        }
        if context:
            ctx.update(sanitize_user_flow_context(context))

        self.diagnostics.record_step(
            phase=pipeline_name,
            step_id=f"{pipeline_name}.pipeline.start",
            handler=f"kernel:{pipeline_name}.run",
            status="success",
            meta={"step_count": len(steps), "pipeline": pipeline_name}
        )

        aborted = False
        executed_ids: Set[str] = set()
        for step in steps:
            if aborted:
                break
            # --- Wave 10-C: depends_on check ---
            step_id_for_dep = step.get("id") if isinstance(step, dict) else getattr(step, "id", None)
            dep_ok, dep_missing = self._check_depends_on(step, executed_ids)
            if not dep_ok:
                if fail_soft_default:
                    _logger.warning(
                        f"Step '{step_id_for_dep}' skipped: depends_on not satisfied (missing: {dep_missing})",
                    )
                    self.diagnostics.record_step(
                        phase=pipeline_name,
                        step_id=f"{step_id_for_dep or 'unknown'}.depends_on.skipped",
                        handler="kernel:depends_on_check",
                        status="skipped",
                        meta={"missing_deps": dep_missing},
                    )
                    continue
                else:
                    self.diagnostics.record_step(
                        phase=pipeline_name,
                        step_id=f"{step_id_for_dep or 'unknown'}.depends_on.abort",
                        handler="kernel:depends_on_check",
                        status="failed",
                        meta={"missing_deps": dep_missing},
                    )
                    aborted = True
                    break
            # --- end depends_on check ---
            try:
                aborted = self._execute_flow_step(step, phase=pipeline_name, ctx=ctx)
                if not aborted and step_id_for_dep:
                    executed_ids.add(step_id_for_dep)
            except Exception as e:
                self.diagnostics.record_step(
                    phase=pipeline_name,
                    step_id=f"{pipeline_name}.pipeline.internal_error",
                    handler=f"kernel:{pipeline_name}.run",
                    status="failed",
                    error=e
                )
                if not fail_soft_default:
                    break

        self.diagnostics.record_step(
            phase=pipeline_name,
            step_id=f"{pipeline_name}.pipeline.end",
            handler=f"kernel:{pipeline_name}.run",
            status="success" if not aborted else "failed",
            meta={"aborted": aborted, "pipeline": pipeline_name}
        )

        return ctx

    # ------------------------------------------------------------------
    # Async Flow 実行
    # ------------------------------------------------------------------

    async def execute_flow(
        self,
        flow_id: str,
        context: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        trusted_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if timeout:
            try:
                if trusted_context is None:
                    return await asyncio.wait_for(
                        self._execute_flow_internal(flow_id, context),
                        timeout=timeout,
                    )
                return await asyncio.wait_for(
                    self._execute_flow_internal(flow_id, context, trusted_context=trusted_context),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                return {"_error": f"Flow '{flow_id}' timed out after {timeout}s", "_flow_timeout": True}
        if trusted_context is None:
            return await self._execute_flow_internal(flow_id, context)
        return await self._execute_flow_internal(flow_id, context, trusted_context=trusted_context)

    def execute_flow_sync(
        self,
        flow_id: str,
        context: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        trusted_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Flow を同期的に実行する。

        S-4: asyncio.get_running_loop() の RuntimeError 依存をやめ、
        Python 3.9+ 互換のパターンに変更。
        """
        effective_timeout = timeout or 300
        if trusted_context is None:
            coro = self.execute_flow(flow_id, context, timeout)
        else:
            coro = self.execute_flow(flow_id, context, timeout, trusted_context=trusted_context)

        # S-4: ループの状態を安全に判定
        try:
            loop = asyncio.get_running_loop()
            is_running = loop.is_running()
        except RuntimeError:
            is_running = False

        if is_running:
            # 既にイベントループが走っている → run_coroutine_threadsafe で安全にスケジュール
            from concurrent.futures import TimeoutError as FuturesTimeoutError
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            try:
                return future.result(timeout=effective_timeout)
            except FuturesTimeoutError:
                return {"_error": f"Flow '{flow_id}' timed out after {effective_timeout}s (sync)", "_flow_timeout": True}
        else:
            # イベントループなし → asyncio.run で実行
            return asyncio.run(coro)

    async def _execute_flow_internal(
        self,
        flow_id: str,
        context: Optional[Dict[str, Any]] = None,
        trusted_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        _prof_start = time.monotonic()
        ctx = self._build_kernel_context()
        ctx.update(sanitize_user_flow_context(context))
        if trusted_context:
            ctx.update(trusted_context)
        try:
            from .runtime_profile_resolver import resolve_runtime_profile_context

            ctx = resolve_runtime_profile_context(
                ctx,
                interface_registry=getattr(self, "interface_registry", None),
            )
        except Exception:
            pass
        execution_id = str(uuid.uuid4())
        ctx["_flow_id"] = flow_id
        ctx["_flow_execution_id"] = execution_id
        ctx["_flow_timeout"] = False
        call_stack = ctx.setdefault("_flow_call_stack", [])

        # Fix #58: chain depth limit
        if len(call_stack) >= MAX_FLOW_CHAIN_DEPTH:
            return {
                "_error": f"Flow chain depth limit exceeded ({MAX_FLOW_CHAIN_DEPTH}): {' -> '.join(call_stack)} -> {flow_id}",
                "_flow_call_stack": list(call_stack),
            }

        if flow_id in call_stack:
            return {"_error": f"Recursive flow detected: {' -> '.join(call_stack)} -> {flow_id}", "_flow_call_stack": list(call_stack)}
        call_stack.append(flow_id)
        try:
            flow_def = self.interface_registry.get(f"flow.{flow_id}", strategy="last")
            if flow_def is None:
                available = [k[5:] for k in (self.interface_registry.list() or {}).keys()
                            if k.startswith("flow.") and not k.startswith("flow.hooks") and not k.startswith("flow.construct")]
                return {"_error": f"Flow '{flow_id}' not found", "_available": available}
            # M-8: modifier 適用前のオリジナルを保存
            original_key = f"flow._original.{flow_id}"
            if self.interface_registry.get(original_key, strategy="last") is None:
                self.interface_registry.register(
                    original_key,
                    copy.deepcopy(flow_def),
                    meta={"_is_original": True, "_flow_id": flow_id},
                )

            ctx["_flow_source_type"] = flow_def.get("_source_type")
            ctx["_flow_source_file"] = flow_def.get("_source_file")
            ctx["_flow_source_pack_id"] = flow_def.get("_source_pack_id")

            steps = flow_def.get("steps", [])
            ctx["_total_steps"] = len(steps)
            self.diagnostics.record_step(phase="flow", step_id=f"flow.{flow_id}.start", handler="kernel:execute_flow",
                                          status="success", meta={"flow_id": flow_id, "execution_id": execution_id, "step_count": len(steps)})
            ctx = await self._execute_steps_async(steps, ctx)
            self.diagnostics.record_step(phase="flow", step_id=f"flow.{flow_id}.end", handler="kernel:execute_flow",
                                          status="success", meta={"flow_id": flow_id, "execution_id": execution_id})
            # --- Wave 15-B: metrics ---
            try:
                get_metrics_collector().increment("flow.execution.complete", labels={"flow_id": flow_id})
            except Exception:
                pass
            return ctx
        finally:
            call_stack.pop()
            # --- Wave 15-B: profiler ---
            try:
                get_profiler()._record(f"flow.{flow_id}", time.monotonic() - _prof_start)
            except Exception:
                pass

    async def _execute_steps_async(self, steps: List[Dict[str, Any]], ctx: Dict[str, Any]) -> Dict[str, Any]:
        authorization_scope_token = self._ensure_flow_authorization_context(ctx)
        executed_ids: Set[str] = set()
        try:
            for i, step in enumerate(steps):
                if not isinstance(step, dict) or ctx.get("_flow_timeout"):
                    continue
                ctx["_current_step_index"] = i
                step_id = step.get("id", f"step_{i}")
                step_type = step.get("type", "handler")
                if step.get("when") and not self._eval_condition(step["when"], ctx):
                    continue
                # --- Wave 10-C: depends_on check ---
                dep_ok, dep_missing = self._check_depends_on(step, executed_ids)
                if not dep_ok:
                    fail_soft = ctx.get("_flow_defaults", {}).get("fail_soft", True)
                    if fail_soft:
                        _logger.warning(
                            f"Step '{step_id}' skipped: depends_on not satisfied (missing: {dep_missing})",
                        )
                        self.diagnostics.record_step(
                            phase="flow",
                            step_id=f"{step_id}.depends_on.skipped",
                            handler="kernel:depends_on_check",
                            status="skipped",
                            meta={
                                "missing_deps": dep_missing,
                                "flow_id": ctx.get("_flow_id"),
                            },
                        )
                        continue
                    else:
                        self.diagnostics.record_step(
                            phase="flow",
                            step_id=f"{step_id}.depends_on.abort",
                            handler="kernel:depends_on_check",
                            status="failed",
                            meta={
                                "missing_deps": dep_missing,
                                "flow_id": ctx.get("_flow_id"),
                            },
                        )
                        return ctx
                # --- end depends_on check ---
                meta = {"flow_id": ctx.get("_flow_id"), "execution_id": ctx.get("_flow_execution_id"),
                        "step_index": i, "total_steps": ctx.get("_total_steps", len(steps)),
                        "parent_execution_id": ctx.get("_parent_flow_execution_id")}
                should_skip, should_abort = False, False
                for hook in self.interface_registry.get("flow.hooks.before_step", strategy="all"):
                    if callable(hook):
                        try:
                            result = hook(step, ctx, meta)
                            if isinstance(result, dict):
                                if result.get("_skip"):
                                    should_skip = True
                                    break
                                if result.get("_abort"):
                                    should_abort = True
                                    break
                        except Exception as e:
                            self.diagnostics.record_step(phase="flow", step_id=f"{step_id}.before_hook",
                                                          handler="flow.hooks.before_step", status="failed", error=e)
                if should_abort:
                    return ctx
                if should_skip:
                    continue
                step_result = None
                try:
                    if step_type == "handler":
                        ctx, step_result = await self._execute_handler_step_async(step, ctx)
                    elif step_type == "flow":
                        ctx, step_result = await self._execute_sub_flow_step(step, ctx)
                    elif step_type == "function":
                        ctx, step_result = await self._execute_function_step_async(step, ctx)
                    else:
                        construct = self.interface_registry.get(f"flow.construct.{step_type}")
                        if construct and callable(construct):
                            # Wave 17-A: Pack の construct に Kernel 直接参照を渡さず KernelFacade でラップ
                            _facade = KernelFacade(self)
                            ctx = await construct(_facade, step, ctx) if asyncio.iscoroutinefunction(construct) else construct(_facade, step, ctx)
                    # C5: check flow control abort after step execution
                    if ctx.get("_flow_control_abort"):
                        return ctx
                    for hook in self.interface_registry.get("flow.hooks.after_step", strategy="all"):
                        if callable(hook):
                            try:
                                hook(step, ctx, step_result, meta)
                            except Exception as e:
                                _logger.debug(f"after_step hook failed: {e}")
                                self.diagnostics.record_step(
                                    phase="flow",
                                    step_id=f"{step_id}.after_hook",
                                    handler="flow.hooks.after_step",
                                    status="failed",
                                    error=e,
                                )
                    # --- Wave 10-C: mark step as executed on success ---
                    executed_ids.add(step_id)
                except Exception as e:
                    error_handler = self.interface_registry.get("flow.error_handler")
                    if error_handler and callable(error_handler):
                        try:
                            action = error_handler(step, ctx, e)
                            if action == "abort":
                                self.diagnostics.record_step(phase="flow", step_id=f"{step_id}.error",
                                                              handler=step.get("handler", "unknown"), status="failed", error=e, meta={"action": "abort"})
                                return ctx
                            if action == "retry":
                                continue
                        except Exception:
                            pass
                    self.diagnostics.record_step(phase="flow", step_id=f"{step_id}.error",
                                                  handler=step.get("handler", "unknown"), status="failed", error=e, meta={"action": "continue"})
            return ctx
        finally:
            self._clear_flow_authorization_context(authorization_scope_token)

    async def _execute_handler_step_async(self, step: Dict[str, Any], ctx: Dict[str, Any]) -> Tuple[Dict[str, Any], Any]:
        handler_key = step.get("handler")
        if not handler_key:
            return ctx, None
        resolved_args = self._resolve_value(step.get("args", {}), ctx)

        if self._should_block_kernel_handler_for_flow(handler_key, ctx):
            self._record_blocked_kernel_handler_step(step.get("id"), handler_key, ctx)
            return ctx, None

        # handler 解決統一: kernel:* は _resolve_handler を優先し、
        # pipeline 実行と同じ経路で解決する（async/pipeline 非対称の解消）
        handler = self._resolve_handler(handler_key, resolved_args)

        # kernel:* で見つからなかった場合は IR にフォールバック
        if handler is None:
            handler = self.interface_registry.get(handler_key, strategy="last")

        if handler is None or not callable(handler):
            return ctx, None
        _step_prof_start = time.monotonic()
        try:
            if asyncio.iscoroutinefunction(handler):
                result = await handler(resolved_args, ctx)
            else:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(self._executor, lambda: handler(resolved_args, ctx))
            # C7: unwrap output — strip _kernel_step_status wrapper
            unwrapped = result["output"] if isinstance(result, dict) and "output" in result else result

            # C5: flow control protocol — check for abort signal
            if isinstance(unwrapped, dict) and unwrapped.get("__flow_control") == "abort":
                output_key = step.get("output")
                if output_key:
                    ctx[output_key] = unwrapped
                ctx["_flow_control_abort"] = True
                ctx["_flow_control_abort_reason"] = unwrapped.get("reason", "abort requested by step")
                self.diagnostics.record_step(
                    phase="flow",
                    step_id=f"{step.get('id', 'unknown')}.flow_control_abort",
                    handler=step.get("handler", "unknown"),
                    status="failed",
                    meta={
                        "reason": ctx["_flow_control_abort_reason"],
                        "__flow_control": "abort",
                        "terminal_status": "aborted",
                    },
                )
                return ctx, unwrapped

            # --- Wave 27-B: output storage ---
            output_key_explicit = step.get("output")
            output_key_auto = step.get("id") if not output_key_explicit else None

            if output_key_explicit:
                # 明示指定: None でも格納（後方互換維持）
                val = unwrapped
                if isinstance(val, dict) and step.get("vocab_normalize", True):
                    val = self._vocab_normalize_output(val, step, ctx)
                ctx[output_key_explicit] = val
            elif output_key_auto and unwrapped is not None:
                # 自動格納: namespace 付きで ctx 汚染を防止、None は格納しない
                val = unwrapped
                if isinstance(val, dict) and step.get("vocab_normalize", True):
                    val = self._vocab_normalize_output(val, step, ctx)
                ctx[f"_step_out.{output_key_auto}"] = val
            # --- Wave 15-B: metrics (success) ---
            try:
                get_metrics_collector().increment("flow.step.success", labels={"handler": handler_key})
            except Exception:
                pass
            return ctx, unwrapped
        except Exception:
            # --- Wave 15-B: metrics (error) ---
            try:
                get_metrics_collector().increment("flow.step.error", labels={"handler": handler_key})
            except Exception:
                pass
            raise
        finally:
            # --- Wave 15-B: profiler ---
            try:
                get_profiler()._record(f"step.{handler_key}", time.monotonic() - _step_prof_start)
            except Exception:
                pass

    async def _execute_sub_flow_step(self, step: Dict[str, Any], ctx: Dict[str, Any]) -> Tuple[Dict[str, Any], Any]:
        flow_name = step.get("flow")
        if not flow_name:
            return ctx, None

        call_stack = ctx.get("_flow_call_stack", [])

        # Fix #58: chain depth limit
        if len(call_stack) >= MAX_FLOW_CHAIN_DEPTH:
            error_msg = f"Flow chain depth limit exceeded ({MAX_FLOW_CHAIN_DEPTH}): {' -> '.join(call_stack)} -> {flow_name}"
            self.diagnostics.record_step(
                phase="flow",
                step_id=f"subflow.{flow_name}.depth_limit",
                handler="kernel:subflow",
                status="failed",
                error={"type": "FlowChainDepthError", "message": error_msg}
            )
            return ctx, {"_error": error_msg}

        if flow_name in call_stack:
            error_msg = f"Recursive flow detected: {' -> '.join(call_stack)} -> {flow_name}"
            self.diagnostics.record_step(
                phase="flow",
                step_id=f"subflow.{flow_name}.recursive",
                handler="kernel:subflow",
                status="failed",
                error={"type": "RecursiveFlowError", "message": error_msg}
            )
            return ctx, {"_error": error_msg}

        child_ctx = copy.deepcopy(ctx)
        child_ctx["_flow_call_stack"] = call_stack + [flow_name]
        child_ctx["_parent_flow_id"] = ctx.get("_flow_id")

        args = step.get("args", {})
        resolved_args = self._resolve_value(args, ctx)
        if isinstance(resolved_args, dict):
            child_ctx.update(sanitize_user_flow_context(resolved_args))

        try:
            flow_def = self.interface_registry.get(f"flow.{flow_name}", strategy="last")

            if flow_def is None:
                ecosystem_flow_path = BASE_DIR / "flow" / "ecosystem" / f"{flow_name}.flow.yaml"
                if ecosystem_flow_path.exists():
                    flow_def = self._load_single_flow(ecosystem_flow_path)
                    if "pipelines" in flow_def:
                        first_pipeline = list(flow_def["pipelines"].values())[0]
                        flow_def = {"steps": first_pipeline}

            if flow_def is None:
                self.diagnostics.record_step(
                    phase="flow",
                    step_id=f"subflow.{flow_name}.not_found",
                    handler="kernel:subflow",
                    status="failed",
                    error={"type": "FlowNotFoundError", "message": f"Flow '{flow_name}' not found"}
                )
                return ctx, {"_error": f"Flow '{flow_name}' not found"}

            steps = flow_def.get("steps", [])
            if not steps and "pipelines" in flow_def:
                first_pipeline = list(flow_def["pipelines"].values())[0]
                steps = first_pipeline if isinstance(first_pipeline, list) else []

            child_ctx["_flow_id"] = flow_name
            child_ctx["_flow_source_type"] = flow_def.get("_source_type")
            child_ctx["_flow_source_file"] = flow_def.get("_source_file")
            child_ctx["_flow_source_pack_id"] = flow_def.get("_source_pack_id")
            child_ctx = await self._execute_steps_async(steps, child_ctx)

            result = child_ctx.get("output") or child_ctx.get("result") or child_ctx

            output_key = step.get("output")
            if output_key:
                ctx[output_key] = result

            self.diagnostics.record_step(
                phase="flow",
                step_id=f"subflow.{flow_name}.complete",
                handler="kernel:subflow",
                status="success",
                meta={"flow_name": flow_name, "output_key": output_key}
            )

            return ctx, result

        except Exception as e:
            self.diagnostics.record_step(
                phase="flow",
                step_id=f"subflow.{flow_name}.error",
                handler="kernel:subflow",
                status="failed",
                error=e,
                meta={"flow_name": flow_name}
            )
            return ctx, {"_error": str(e)}

    # ------------------------------------------------------------------
    # Wave 27-C: function.call ステップ
    # ------------------------------------------------------------------

    async def _execute_function_step_async(
        self, step: Dict[str, Any], ctx: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Any]:
        """function.call ステップを実行する。

        capability_executor 経由で function.call を呼ぶ。
        principal_id は信頼済みの flow 実行コンテキストから取得し、フォールバックは使わない（フェイルクローズ）。

        Wave 27-D2: vocab_normalize (opt-in) を結果格納前に適用。
        """
        qualified_name = step.get("function")
        if not qualified_name:
            _logger.warning(
                "function step is missing its function field",
                step_id=step.get("id"),
            )
            return ctx, None

        # フェイルクローズ: trusted flow principal が ctx に無い場合は実行拒否
        principal_id = ctx.get("_flow_run_principal_id")
        if not principal_id:
            error_result = {"_error": "no trusted flow principal in ctx", "_step_id": step.get("id")}
            _logger.error(
                "function step '%s': no trusted flow principal in ctx, refusing execution",
                step.get("id"),
            )
            output_key = step.get("output") or step.get("id")
            if output_key:
                ctx[f"_step_out.{output_key}"] = error_result
            return ctx, error_result

        resolved_args = self._resolve_value(step.get("args", {}), ctx)

        # DI コンテナ経由で capability_executor を取得（循環インポート回避）
        try:
            from .di_container import get_container as _get_di
            executor = _get_di().get_or_none("capability_executor")
        except Exception:
            executor = None

        if executor is None:
            error_result = {"_error": "capability_executor not available"}
            _logger.error("function step '%s': capability_executor not available", step.get("id"))
            output_key = step.get("output") or step.get("id")
            if output_key:
                ctx[f"_step_out.{output_key}"] = error_result
            return ctx, error_result

        request = {
            "type": "function.call",
            "qualified_name": qualified_name,
            "args": resolved_args,
            "request_id": f"flow-{ctx.get('_flow_execution_id', '')}-{step.get('id', '')}",
        }

        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(
            self._executor,
            lambda: executor.execute(principal_id, request)
        )

        result = resp.output if resp.success else {"_error": resp.error}

        # --- Wave 27-D2: vocab_normalize (opt-in for function steps) ---
        if resp.success and step.get("vocab_normalize") and isinstance(result, dict):
            result = self._vocab_normalize_output(result, step, ctx)

        # 出力格納: 明示 output があればそちら、なければ _step_out.{id}
        output_key_explicit = step.get("output")
        output_key_auto = step.get("id") if not output_key_explicit else None

        if output_key_explicit:
            ctx[output_key_explicit] = result
        elif output_key_auto and result is not None:
            ctx[f"_step_out.{output_key_auto}"] = result

        if not resp.success:
            _logger.warning(
                "function.call step failed",
                step_id=step.get("id"),
                qualified_name=qualified_name,
                principal_id=principal_id,
                error=resp.error,
            )

        return ctx, result

    # ------------------------------------------------------------------
    # 条件評価
    # ------------------------------------------------------------------

    def _eval_condition(self, condition: str, ctx: Dict[str, Any]) -> bool:  # noqa: C901
        """条件式を評価する。Wave 27-A: 比較演算子拡張（>, <, >=, <=, None）。"""
        condition = condition.strip()
        m = _CONDITION_OP_RE.search(condition)
        if m:
            op = m.group(1)
            left = condition[:m.start()].strip()
            right = condition[m.end():].strip()

            left_val = self._resolve_value(left, ctx)
            right_val = right.strip('"\'')

            # 型変換
            target: object
            if right_val.lower() == "true":
                target = True
            elif right_val.lower() == "false":
                target = False
            elif right_val.lower() == "none":
                target = None
            else:
                try:
                    target = float(right_val) if '.' in right_val else int(right_val)
                except ValueError:
                    target = right_val

            # None 比較
            if target is None:
                if op == "==":
                    return left_val is None
                elif op == "!=":
                    return left_val is not None
                else:
                    return False  # None に対する > < >= <= は常に False

            # 数値変換ヘルパー
            def _to_num(v):
                if isinstance(v, (int, float)):
                    return v
                if not isinstance(v, str):
                    return None
                try:
                    return float(v) if '.' in v else int(v)
                except ValueError:
                    return None

            if op == "==":
                if isinstance(target, (bool, int, float)):
                    return left_val == target
                return str(left_val) == target
            elif op == "!=":
                if isinstance(target, (bool, int, float)):
                    return left_val != target
                return str(left_val) != target
            elif op in (">", "<", ">=", "<="):
                ln, rn = _to_num(left_val), _to_num(target)
                if ln is None or rn is None:
                    return False  # 数値比較不能 → false（安全側）
                return {">": ln > rn, "<": ln < rn, ">=": ln >= rn, "<=": ln <= rn}[op]

        return bool(self._resolve_value(condition, ctx))

    # ------------------------------------------------------------------
    # Flow Step 実行（同期・pipeline用）
    # ------------------------------------------------------------------

    def _execute_flow_step(self, step: Any, phase: str, ctx: Dict[str, Any]) -> bool:
        step_id, handler, args, optional, on_error_action = None, None, {}, False, None
        if isinstance(step, dict):
            step_id = step.get("id")
            run = step.get("run", {})
            if isinstance(run, dict):
                handler = run.get("handler")
                run_args = run.get("args", {})
                if isinstance(run_args, dict):
                    args = dict(run_args)
            optional = bool(step.get("optional", False))
            on_error = step.get("on_error", {})
            if isinstance(on_error, dict):
                on_error_action = on_error.get("action")

        # --- Wave 27-D1: function step support in sync pipeline ---
        function_name = step.get("function") if isinstance(step, dict) else None
        if function_name:
            step_id_str = str(step_id or "unknown.step")
            self.diagnostics.record_step(
                phase=phase, step_id=f"{step_id_str}.start",
                handler=f"function:{function_name}", status="success",
                meta={"function": function_name},
            )
            # フェイルクローズ: trusted flow principal が ctx に無い場合は実行拒否
            principal_id = ctx.get("_flow_run_principal_id")
            if not principal_id:
                _logger.error(
                    "sync function step '%s': no trusted flow principal in ctx, refusing execution",
                    step_id,
                )
                self.diagnostics.record_step(
                    phase=phase, step_id=f"{step_id_str}.failed",
                    handler=f"function:{function_name}", status="failed",
                    error={"type": "MissingPrincipalId", "message": "no trusted flow principal in ctx"},
                    meta={"optional": optional},
                )
                fail_soft = ctx.get("_flow_defaults", {}).get("fail_soft", True)
                return not fail_soft  # fail_soft=True → continue (False), else abort (True)

            resolved_args = self._resolve_value(step.get("args", {}), ctx)

            # DI コンテナ経由で capability_executor を取得
            try:
                from .di_container import get_container as _get_di
                executor = _get_di().get_or_none("capability_executor")
            except Exception:
                executor = None

            if executor is None:
                _logger.error(
                    "sync function step '%s': capability_executor not available", step_id,
                )
                self.diagnostics.record_step(
                    phase=phase, step_id=f"{step_id_str}.failed",
                    handler=f"function:{function_name}", status="failed",
                    error={"type": "MissingExecutor", "message": "capability_executor not available"},
                    meta={"optional": optional},
                )
                fail_soft = ctx.get("_flow_defaults", {}).get("fail_soft", True)
                return not fail_soft

            request = {
                "type": "function.call",
                "qualified_name": function_name,
                "args": resolved_args,
                "request_id": f"sync-pipeline-{step_id or 'unknown'}",
            }

            try:
                resp = executor.execute(principal_id, request)
                result = resp.output if resp.success else {"_error": resp.error}

                # vocab_normalize (opt-in, symmetric with async path)
                if resp.success and step.get("vocab_normalize") and isinstance(result, dict):
                    result = self._vocab_normalize_output(result, step, ctx)

                # 出力格納
                output_key = step.get("output")
                if output_key:
                    ctx[output_key] = result
                elif step_id and result is not None:
                    ctx[f"_step_out.{step_id}"] = result

                done_status: Status = "success" if resp.success else "failed"
                self.diagnostics.record_step(
                    phase=phase, step_id=f"{step_id_str}.done",
                    handler=f"function:{function_name}", status=done_status,
                    meta={"function": function_name},
                )
                return False  # continue
            except Exception as e:
                action = str(on_error_action or ("continue" if ctx.get("_flow_defaults", {}).get("fail_soft", True) else "abort")).lower()
                self.diagnostics.record_step(
                    phase=phase, step_id=f"{step_id_str}.failed",
                    handler=f"function:{function_name}", status="failed",
                    error=e, meta={"on_error.action": action, "optional": optional},
                )
                return action == "abort"
        # --- end Wave 27-D1 ---

        step_id_str = str(step_id or "unknown.step")
        handler_str = str(handler or "unknown.handler")
        if self._should_block_kernel_handler_for_flow(handler_str, ctx):
            self._record_blocked_kernel_handler_step(step_id_str, handler_str, ctx, phase=phase)
            return False
        fn = self._resolve_handler(handler_str, args)
        if fn is None:
            missing_policy = str(ctx.get("_flow_defaults", {}).get("on_missing_handler", "skip")).lower()
            if missing_policy == "error" and not optional:
                self.diagnostics.record_step(phase=phase, step_id=step_id_str, handler=handler_str, status="failed",
                                              error={"type": "MissingHandler", "message": f"handler not found: {handler_str}"},
                                              meta={"optional": optional, "on_missing_handler": missing_policy})
                return True
            self.diagnostics.record_step(phase=phase, step_id=step_id_str, handler=handler_str, status="skipped",
                                          meta={"reason": "missing_handler", "optional": optional, "on_missing_handler": missing_policy})
            return False
        self.diagnostics.record_step(phase=phase, step_id=f"{step_id_str}.start", handler=handler_str, status="success", meta={"args": args})
        try:
            ret = fn(args, ctx)
            done_status = "success"
            done_meta: Dict[str, Any] = {}
            if isinstance(ret, dict):
                maybe_status = ret.get("_kernel_step_status")
                if maybe_status in ("success", "skipped"):
                    done_status = maybe_status
                maybe_meta = ret.get("_kernel_step_meta")
                if isinstance(maybe_meta, dict):
                    done_meta = dict(maybe_meta)
            self.diagnostics.record_step(phase=phase, step_id=f"{step_id_str}.done", handler=handler_str, status=done_status, meta=done_meta)
            return False
        except Exception as e:
            action = str(on_error_action or ("continue" if ctx.get("_flow_defaults", {}).get("fail_soft", True) else "abort")).lower()
            status: Status = "disabled" if action == "disable_target" else "failed"
            self.diagnostics.record_step(phase=phase, step_id=f"{step_id_str}.failed", handler=handler_str, status=status, error=e,
                                          meta={"on_error.action": action, "optional": optional})
            return action == "abort"



    # ================================================================
    # universal_call – async handler + helpers
    # ================================================================
    async def _handle_universal_call_async(
        self,
        step: dict,
        ctx: dict,
    ) -> dict:
        """Execute a universal_call step (python / binary / command)."""
        import asyncio
        import json
        import time

        owner_pack = step.get("owner_pack", "")
        uc_file = step.get("file", "")
        runtime = step.get("runtime", "python")
        docker_image = step.get("docker_image")
        input_data = step.get("input", {}) if isinstance(step.get("input"), dict) else {}
        timeout = min(
            float(step.get("timeout_seconds", _UC_DEFAULT_TIMEOUT) or _UC_DEFAULT_TIMEOUT),
            _UC_MAX_TIMEOUT,
        )
        step_id = step.get("id", "universal_call")

        def _err(msg, etype="execution_error"):
            return {
                "_kernel_step_status": "failed",
                "_error": msg,
                "_error_type": etype,
                "_step_id": step_id,
            }

        # ── validation ──
        if not owner_pack or not uc_file:
            return _err("owner_pack and file are required", "validation_error")
        if runtime not in _UC_VALID_RUNTIMES:
            return _err(f"invalid runtime: {runtime!r}", "validation_error")

        # ── pack approval ──
        try:
            from core_runtime.approval_manager import get_approval_manager
            am = get_approval_manager()
            approval_check = am.is_pack_approved_and_verified(owner_pack)
            approved = _approval_check_allowed(approval_check)
            if not approved:
                return _err(
                    f"Pack '{owner_pack}' is not approved for universal_call",
                    "approval_error",
                )
        except Exception as exc:
            return _err(f"approval check failed: {exc}", "approval_error")

        # ── path resolution & traversal protection ──
        try:
            from core_runtime.paths import ECOSYSTEM_DIR, is_path_within
            pack_dir_path = Path(ECOSYSTEM_DIR) / owner_pack
            target_path = Path(
                os.path.realpath(os.path.join(str(pack_dir_path), uc_file))
            )
            if not is_path_within(target_path, pack_dir_path):
                return _err(f"path traversal blocked: {uc_file}", "security_error")
            if not os.path.isfile(str(target_path)):
                return _err(f"file not found: {uc_file}", "file_error")
            pack_dir = str(pack_dir_path)
            target = str(target_path)
        except ImportError:
            pack_dir = ""
            target = uc_file

        # ── execute by runtime ──
        t0 = time.monotonic()
        try:
            if runtime == "python":
                result = await self._uc_exec_python(
                    owner_pack, target, input_data, timeout, docker_image, ctx,
                )
            elif runtime == "binary":
                result = await self._uc_exec_binary(
                    target, input_data, timeout, docker_image, pack_dir,
                )
            elif runtime == "command":
                result = await self._uc_exec_command(
                    target, input_data, timeout, docker_image, pack_dir,
                )
            else:
                result = _err(f"unsupported runtime: {runtime}")
        except asyncio.TimeoutError:
            result = _err(f"timeout after {timeout}s", "timeout_error")
        except Exception as exc:
            result = _err(f"execution failed: {exc}")

        elapsed = time.monotonic() - t0

        # ── output size guard ──
        raw = json.dumps(result) if isinstance(result, dict) else str(result)
        if len(raw.encode("utf-8", errors="replace")) > _UC_MAX_RESPONSE_SIZE:
            result = _err(
                f"output exceeds {_UC_MAX_RESPONSE_SIZE} bytes",
                "output_size_error",
            )

        # ── wrap in kernel envelope ──
        if isinstance(result, dict) and "_kernel_step_status" in result:
            return result
        return {
            "_kernel_step_status": "success",
            "_kernel_step_meta": {"elapsed_s": round(elapsed, 4), "runtime": runtime},
            "result": result,
        }

    # ── python runtime ──────────────────────────────────────
    async def _uc_exec_python(self, owner_pack, target, input_data, timeout, docker_image, ctx):
        try:
            from core_runtime.python_file_executor import ExecutionContext, PythonFileExecutor
            executor = PythonFileExecutor()
            exec_ctx = ExecutionContext(
                flow_id=str(ctx.get("_flow_id") or "universal_call"),
                step_id=str(ctx.get("_step_id") or "universal_call"),
                phase="flow",
                ts=str(ctx.get("_ts") or ""),
                owner_pack=owner_pack,
                inputs=dict(input_data) if isinstance(input_data, dict) else {},
                principal_id=str(ctx.get("_flow_run_principal_id") or "") or None,
            )
            exec_result = await asyncio.to_thread(
                executor.execute,
                target,
                owner_pack,
                input_data,
                exec_ctx,
                timeout_seconds=timeout,
            )
            if hasattr(exec_result, "to_dict"):
                return exec_result.to_dict()
            return {
                "stdout": getattr(exec_result, "stdout", ""),
                "success": getattr(exec_result, "success", False),
            }
        except Exception as exc:
            return {"_kernel_step_status": "failed", "_error": str(exc), "_error_type": "python_exec_error"}

    # ── binary runtime ──────────────────────────────────────
    async def _uc_exec_binary(self, target, input_data, timeout, docker_image, pack_dir):
        import asyncio
        import json
        if docker_image:
            return await self._uc_exec_in_container(
                target, input_data, timeout, docker_image, pack_dir, runtime="binary",
            )
        input_json = json.dumps(input_data)
        proc = await asyncio.create_subprocess_exec(
            target,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=input_json.encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise
        stdout_str = stdout.decode("utf-8", errors="replace")[:_UC_MAX_RESPONSE_SIZE]
        try:
            return json.loads(stdout_str)
        except json.JSONDecodeError:
            return {"raw_stdout": stdout_str, "stderr": stderr.decode("utf-8", errors="replace")[:4096]}

    # ── command runtime ─────────────────────────────────────
    async def _uc_exec_command(self, target, input_data, timeout, docker_image, pack_dir):
        import asyncio
        import json
        import os
        if docker_image:
            return await self._uc_exec_in_container(
                target, input_data, timeout, docker_image, pack_dir, runtime="command",
            )
        input_json = json.dumps(input_data)
        proc = await asyncio.create_subprocess_shell(
            f"sh {target}",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.path.dirname(target) or None,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=input_json.encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise
        stdout_str = stdout.decode("utf-8", errors="replace")[:_UC_MAX_RESPONSE_SIZE]
        try:
            return json.loads(stdout_str)
        except json.JSONDecodeError:
            return {"raw_stdout": stdout_str, "stderr": stderr.decode("utf-8", errors="replace")[:4096]}

    # ── Docker container execution ──────────────────────────
    async def _uc_exec_in_container(self, target, input_data, timeout, docker_image, pack_dir, runtime="binary"):
        import asyncio
        import json
        import os
        try:
            from core_runtime.container_orchestrator import get_container_orchestrator
            orch = get_container_orchestrator()
            cmd_args = orch.build_universal_call_command(
                pack_id=os.path.basename(pack_dir) if pack_dir else "unknown",
                workspace_dir=pack_dir or os.path.dirname(target),
                input_file="",
                filename=os.path.basename(target),
                runtime=runtime,
                docker_image=docker_image,
            )
        except Exception:
            cmd_args = [
                "docker", "run", "--rm",
                "--network=none", "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true",
                "--read-only", "--memory=256m", "--cpus=0.5", "--pids-limit=50",
                "-v", f"{pack_dir}:/workspace:ro",
                "-w", "/workspace",
                docker_image or "alpine:latest",
            ]
            if runtime == "command":
                cmd_args += ["sh", os.path.basename(target)]
            else:
                cmd_args += [f"./{os.path.basename(target)}"]

        input_bytes = json.dumps(input_data).encode("utf-8")
        proc = await asyncio.create_subprocess_exec(
            *cmd_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=input_bytes),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise
        stdout_str = stdout.decode("utf-8", errors="replace")[:_UC_MAX_RESPONSE_SIZE]
        try:
            return json.loads(stdout_str)
        except json.JSONDecodeError:
            return {"raw_stdout": stdout_str, "stderr": stderr.decode("utf-8", errors="replace")[:4096]}


__all__ = ["KernelFlowExecutionMixin", "MAX_FLOW_CHAIN_DEPTH"]
