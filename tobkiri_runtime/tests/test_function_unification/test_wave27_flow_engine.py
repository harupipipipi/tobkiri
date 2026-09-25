"""
Wave 27-D3: Flow エンジン コアロジック ユニットテスト

テスト対象:
  - KernelFlowExecutionMixin._eval_condition (10 cases)
  - KernelFlowExecutionMixin._execute_function_step_async (7 cases)
  - KernelFlowExecutionMixin._execute_flow_step (2 cases, sync function step)
  - KernelFlowExecutionMixin._check_depends_on (2 cases)

合計: 21 テストケース
"""
from __future__ import annotations

import sys
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# ダミーモジュール登録 — kernel_flow_execution.py の依存を満たす
# ---------------------------------------------------------------------------
_CORE_RT = "tobkiri_runtime.core_runtime"

# パッケージ自体（未登録の場合のみ）
for _pkg in ["tobkiri_runtime", _CORE_RT]:
    if _pkg not in sys.modules:
        _mod = types.ModuleType(_pkg)
        _mod.__path__ = []  # type: ignore[attr-defined]
        sys.modules[_pkg] = _mod

# paths
if f"{_CORE_RT}.paths" not in sys.modules:
    _m_paths = types.ModuleType(f"{_CORE_RT}.paths")
    _m_paths.BASE_DIR = Path("/tmp/rumi_dummy_base")  # type: ignore[attr-defined]
    sys.modules[f"{_CORE_RT}.paths"] = _m_paths

# logging_utils
if f"{_CORE_RT}.logging_utils" not in sys.modules:
    _m_log = types.ModuleType(f"{_CORE_RT}.logging_utils")
    _m_log.get_structured_logger = MagicMock(return_value=MagicMock())  # type: ignore[attr-defined]
    sys.modules[f"{_CORE_RT}.logging_utils"] = _m_log

# profiling
if f"{_CORE_RT}.profiling" not in sys.modules:
    _m_prof = types.ModuleType(f"{_CORE_RT}.profiling")
    _m_prof.get_profiler = MagicMock(return_value=MagicMock())  # type: ignore[attr-defined]
    sys.modules[f"{_CORE_RT}.profiling"] = _m_prof

# metrics
if f"{_CORE_RT}.metrics" not in sys.modules:
    _m_met = types.ModuleType(f"{_CORE_RT}.metrics")
    _m_met.get_metrics_collector = MagicMock(return_value=MagicMock())  # type: ignore[attr-defined]
    sys.modules[f"{_CORE_RT}.metrics"] = _m_met

# kernel_facade
if f"{_CORE_RT}.kernel_facade" not in sys.modules:
    _m_facade = types.ModuleType(f"{_CORE_RT}.kernel_facade")
    _m_facade.KernelFacade = MagicMock  # type: ignore[attr-defined]
    sys.modules[f"{_CORE_RT}.kernel_facade"] = _m_facade

# di_container (function step 内で動的インポートされる)
if f"{_CORE_RT}.di_container" not in sys.modules:
    _m_di = types.ModuleType(f"{_CORE_RT}.di_container")
    _m_di.get_container = MagicMock(return_value=MagicMock())  # type: ignore[attr-defined]
    sys.modules[f"{_CORE_RT}.di_container"] = _m_di

# ---------------------------------------------------------------------------
# テスト対象インポート
# ---------------------------------------------------------------------------
from tobkiri_runtime.core_runtime.kernel_flow_execution import (  # noqa: E402
    KernelFlowExecutionMixin,
)


# ======================================================================
# テスト用スタブ
# ======================================================================

class StubKernel(KernelFlowExecutionMixin):
    """テスト用 Kernel スタブ — Mixin が依存する属性・メソッドを提供する。"""

    def __init__(self):
        self.diagnostics = MagicMock()
        self.interface_registry = MagicMock()
        self.config = MagicMock()
        self.event_bus = MagicMock()
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._flow = None
        self._flow_converter = MagicMock()
        self._variable_resolver = MagicMock()

    def _resolve_value(self, value, ctx, depth=0):
        """テスト用: ctx にキーがあればその値を返す、dict なら再帰、それ以外はそのまま。"""
        if isinstance(value, str) and value in ctx:
            return ctx[value]
        if isinstance(value, dict):
            return {k: self._resolve_value(v, ctx) for k, v in value.items()}
        return value

    def _resolve_handler(self, handler, args=None):
        return None

    def _vocab_normalize_output(self, unwrapped, step, ctx):
        """テスト用: デフォルトではそのまま返す。テストでモック化して検証する。"""
        return unwrapped

    def _build_kernel_context(self):
        return {}

    def _now_ts(self):
        return "2026-01-01T00:00:00Z"


# ======================================================================
# Fixture
# ======================================================================

@pytest.fixture
def kernel():
    """StubKernel インスタンスを返す fixture。"""
    return StubKernel()


@pytest.fixture
def di_mock():
    """DI コンテナのモックを設定し、テスト後にリセットする。"""
    mock_container = MagicMock()
    di_mod = sys.modules[f"{_CORE_RT}.di_container"]
    original_get_container = getattr(di_mod, "get_container", None)
    di_mod.get_container = MagicMock(return_value=mock_container)  # type: ignore[attr-defined]
    yield mock_container
    # リセット
    if original_get_container is not None:
        di_mod.get_container = original_get_container  # type: ignore[attr-defined]


def _make_executor_resp(success=True, output=None, error=None):
    """capability_executor.execute の戻り値をスタブ化する。"""
    resp = MagicMock()
    resp.success = success
    resp.output = output
    resp.error = error
    return resp


# ======================================================================
# TestEvalCondition (1-10)
# ======================================================================

class TestEvalCondition:
    """_eval_condition のユニットテスト。"""

    def test_eval_condition_simple_truthy(self, kernel):
        """1. 真値の単純条件が True を返す。"""
        ctx = {"flag": True}
        assert kernel._eval_condition("flag", ctx) is True

    def test_eval_condition_simple_falsy(self, kernel):
        """2. 偽値の単純条件が False を返す。"""
        ctx = {"flag": False}
        assert kernel._eval_condition("flag", ctx) is False

    def test_eval_condition_eq_operator(self, kernel):
        """3. == 演算子が正しく評価される。"""
        ctx = {"status": "active"}
        assert kernel._eval_condition("status == active", ctx) is True
        assert kernel._eval_condition("status == inactive", ctx) is False

    def test_eval_condition_neq_operator(self, kernel):
        """4. != 演算子が正しく評価される。"""
        ctx = {"status": "active"}
        assert kernel._eval_condition("status != inactive", ctx) is True
        assert kernel._eval_condition("status != active", ctx) is False

    def test_eval_condition_gt_operator(self, kernel):
        """5. > 演算子が正しく評価される。"""
        ctx = {"count": 10}
        assert kernel._eval_condition("count > 5", ctx) is True
        assert kernel._eval_condition("count > 15", ctx) is False

    def test_eval_condition_lt_operator(self, kernel):
        """6. < 演算子が正しく評価される。"""
        ctx = {"count": 3}
        assert kernel._eval_condition("count < 5", ctx) is True
        assert kernel._eval_condition("count < 1", ctx) is False

    def test_eval_condition_gte_operator(self, kernel):
        """7. >= 演算子が正しく評価される。"""
        ctx = {"count": 5}
        assert kernel._eval_condition("count >= 5", ctx) is True
        assert kernel._eval_condition("count >= 6", ctx) is False

    def test_eval_condition_lte_operator(self, kernel):
        """8. <= 演算子が正しく評価される。"""
        ctx = {"count": 5}
        assert kernel._eval_condition("count <= 5", ctx) is True
        assert kernel._eval_condition("count <= 4", ctx) is False

    def test_eval_condition_none_comparison(self, kernel):
        """9. None との比較が正しく動作する。"""
        ctx = {"val": None}
        assert kernel._eval_condition("val == None", ctx) is True
        ctx_not_none = {"val": "something"}
        assert kernel._eval_condition("val == None", ctx_not_none) is False
        assert kernel._eval_condition("val != None", ctx_not_none) is True

    def test_eval_condition_variable_resolve(self, kernel):
        """10. 条件内の変数が ctx から解決される。"""
        ctx = {"my_var": 42}
        assert kernel._eval_condition("my_var == 42", ctx) is True
        assert kernel._eval_condition("my_var == 99", ctx) is False


# ======================================================================
# TestExecuteFunctionStepAsync (11-17)
# ======================================================================

class TestExecuteFunctionStepAsync:
    """_execute_function_step_async のユニットテスト。"""

    @pytest.mark.asyncio
    async def test_function_step_executor_retired_fails_closed(self, kernel, di_mock):
        """11. function step は退役済み executor tombstone でフェイルクローズ。"""
        step = {"id": "s1", "type": "function", "function": "math.add", "args": {"a": 1, "b": 2}}
        ctx = {"_flow_run_principal_id": "user1", "_flow_execution_id": "exec1"}

        # The retired capability_executor can no longer be bound through DI;
        # any injected lookup is dead code and the step must fail closed.
        mock_executor = MagicMock()
        mock_executor.execute = MagicMock(
            return_value=_make_executor_resp(success=True, output={"sum": 3})
        )
        di_mock.get_or_none = MagicMock(return_value=mock_executor)

        ctx, result = await kernel._execute_function_step_async(step, ctx)

        assert result == {"_error": "capability_executor not available"}
        assert ctx.get("_step_out.s1") == result
        mock_executor.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_function_step_no_principal_id(self, kernel, di_mock):
        """12. trusted flow principal がない場合にフェイルクローズで拒否される。"""
        step = {"id": "s1", "type": "function", "function": "math.add"}
        ctx = {}  # no trusted flow principal

        ctx, result = await kernel._execute_function_step_async(step, ctx)

        assert result is not None
        assert "_error" in result
        assert "trusted flow principal" in result["_error"]

    @pytest.mark.asyncio
    async def test_function_step_executor_not_found(self, kernel, di_mock):
        """13. capability_executor が DI にない場合のエラー処理。"""
        step = {"id": "s1", "type": "function", "function": "math.add"}
        ctx = {"_flow_run_principal_id": "user1", "_flow_execution_id": "exec1"}

        di_mock.get_or_none = MagicMock(return_value=None)

        ctx, result = await kernel._execute_function_step_async(step, ctx)

        assert result is not None
        assert "_error" in result
        assert "capability_executor" in result["_error"]

    @pytest.mark.asyncio
    async def test_function_step_retired_executor_error(self, kernel, di_mock):
        """14. executor 解決は tombstone で失敗し注入済み executor は呼ばれない。"""
        step = {"id": "s1", "type": "function", "function": "math.add"}
        ctx = {"_flow_run_principal_id": "user1", "_flow_execution_id": "exec1"}

        mock_executor = MagicMock()
        mock_executor.execute = MagicMock(
            return_value=_make_executor_resp(success=False, error="division by zero")
        )
        di_mock.get_or_none = MagicMock(return_value=mock_executor)

        ctx, result = await kernel._execute_function_step_async(step, ctx)

        assert result == {"_error": "capability_executor not available"}
        mock_executor.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_function_step_output_storage_explicit(self, kernel, di_mock):
        """15. 明示 output キーにはフェイルクローズエラーが格納される。"""
        step = {"id": "s1", "type": "function", "function": "math.add", "output": "my_result"}
        ctx = {"_flow_run_principal_id": "user1", "_flow_execution_id": "exec1"}

        ctx, result = await kernel._execute_function_step_async(step, ctx)

        assert result == {"_error": "capability_executor not available"}
        assert ctx.get("_step_out.my_result") == result
        assert "_step_out.s1" not in ctx  # auto key は使われない

    @pytest.mark.asyncio
    async def test_function_step_output_storage_auto(self, kernel, di_mock):
        """16. 自動 _step_out.{id} にもフェイルクローズエラーが格納される。"""
        step = {"id": "calc_step", "type": "function", "function": "math.add"}
        ctx = {"_flow_run_principal_id": "user1", "_flow_execution_id": "exec1"}

        ctx, result = await kernel._execute_function_step_async(step, ctx)

        assert result == {"_error": "capability_executor not available"}
        assert ctx.get("_step_out.calc_step") == result

    @pytest.mark.asyncio
    async def test_function_step_vocab_normalize(self, kernel, di_mock):
        """17. executor 退役により vocab_normalize は適用されない。"""
        step = {
            "id": "s1", "type": "function", "function": "math.add",
            "vocab_normalize": True,
        }
        ctx = {"_flow_run_principal_id": "user1", "_flow_execution_id": "exec1"}

        # _vocab_normalize_output must never run: the step fails closed first.
        kernel._vocab_normalize_output = MagicMock(
            return_value={"normalized_key": "value"}
        )

        ctx, result = await kernel._execute_function_step_async(step, ctx)

        kernel._vocab_normalize_output.assert_not_called()
        assert result == {"_error": "capability_executor not available"}
        assert ctx.get("_step_out.s1") == result


# ======================================================================
# TestSyncFunctionStep (18-19)
# ======================================================================

class TestSyncFunctionStep:
    """_execute_flow_step (同期版) の function step テスト。"""

    def test_sync_function_step_retired_executor_fails_closed(
        self, kernel, di_mock
    ):
        """18. 同期 pipeline の function step も tombstone でフェイルクローズ。"""
        step = {
            "id": "sync_s1",
            "function": "math.multiply",
            "args": {"x": 3, "y": 4},
        }
        ctx = {
            "_flow_run_principal_id": "user1",
            "_flow_defaults": {"fail_soft": True, "on_missing_handler": "skip"},
        }

        mock_executor = MagicMock()
        mock_executor.execute = MagicMock(
            return_value=_make_executor_resp(success=True, output={"product": 12})
        )
        di_mock.get_or_none = MagicMock(return_value=mock_executor)

        aborted = kernel._execute_flow_step(step, phase="startup", ctx=ctx)

        # fail_soft=True → continue (False), but nothing is stored and the
        # retired executor is never invoked.
        assert aborted is False
        assert ctx.get("_step_out.sync_s1") is None
        mock_executor.execute.assert_not_called()
        kernel.diagnostics.record_step.assert_called()

    def test_sync_function_step_no_principal_id(self, kernel, di_mock):
        """19. 同期版でも trusted flow principal がない場合に拒否される。"""
        step = {
            "id": "sync_s2",
            "function": "math.multiply",
        }
        ctx = {
            "_flow_defaults": {"fail_soft": True, "on_missing_handler": "skip"},
        }
        # no trusted flow principal in ctx

        aborted = kernel._execute_flow_step(step, phase="startup", ctx=ctx)

        # fail_soft=True → continue (False)
        assert aborted is False
        # diagnostics にエラーが記録されている
        kernel.diagnostics.record_step.assert_called()


# ======================================================================
# TestDependsOn (20-21)
# ======================================================================

class TestDependsOn:
    """_check_depends_on のユニットテスト。"""

    def test_depends_on_satisfied(self, kernel):
        """20. depends_on の条件が満たされている場合にステップが実行される。"""
        step = {"id": "step_b", "depends_on": ["step_a"]}
        executed_ids = {"step_a"}

        should_execute, missing = kernel._check_depends_on(step, executed_ids)

        assert should_execute is True
        assert missing == []

    def test_depends_on_not_satisfied(self, kernel):
        """21. depends_on の条件が満たされていない場合にステップがスキップされる。"""
        step = {"id": "step_c", "depends_on": ["step_a", "step_b"]}
        executed_ids = {"step_a"}  # step_b is missing

        should_execute, missing = kernel._check_depends_on(step, executed_ids)

        assert should_execute is False
        assert "step_b" in missing
