"""
tests/test_wave27_flow_engine.py — Wave 27 Flow エンジン強化テスト

Wave 27-A: _eval_condition 比較演算子拡張
Wave 27-B: ステップ出力の自動 ctx 格納
Wave 27-C: function.call ステップタイプ
"""

from __future__ import annotations

import asyncio
import types
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
import sys as _sys

# ---------------------------------------------------------------------------
# テスト用ヘルパー: KernelFlowExecutionMixin を最小限の依存で動かすスタブ
# ---------------------------------------------------------------------------

_stub_logging = types.ModuleType("core_runtime.logging_utils")
_stub_logging.get_structured_logger = lambda name: MagicMock()
_stub_profiling = types.ModuleType("core_runtime.profiling")
_stub_profiling.get_profiler = MagicMock
_stub_metrics = types.ModuleType("core_runtime.metrics")
_stub_metrics.get_metrics_collector = MagicMock
_stub_paths = types.ModuleType("core_runtime.paths")
_stub_paths.BASE_DIR = None
_stub_facade = types.ModuleType("core_runtime.kernel_facade")
_stub_facade.KernelFacade = MagicMock

_sys.modules.setdefault("core_runtime", types.ModuleType("core_runtime"))
_sys.modules.setdefault("core_runtime.logging_utils", _stub_logging)
_sys.modules.setdefault("core_runtime.profiling", _stub_profiling)
_sys.modules.setdefault("core_runtime.metrics", _stub_metrics)
_sys.modules.setdefault("core_runtime.paths", _stub_paths)
_sys.modules.setdefault("core_runtime.kernel_facade", _stub_facade)

from core_runtime.kernel_flow_execution import KernelFlowExecutionMixin


class _FakeRegistry:
    """interface_registry のスタブ。"""

    def __init__(self):
        self._store: Dict[str, Any] = {}

    def get(self, key, strategy="last"):
        if strategy == "all":
            v = self._store.get(key)
            return v if isinstance(v, list) else ([] if v is None else [v])
        return self._store.get(key)

    def list(self):
        return self._store

    def register(self, key, value, meta=None):
        self._store[key] = value


class _FakeDiagnostics:
    """diagnostics のスタブ。"""

    def __init__(self):
        self.records = []

    def record_step(self, **kwargs):
        self.records.append(kwargs)

    def as_dict(self):
        return {"steps": self.records}


class _StubEngine(KernelFlowExecutionMixin):
    """テスト用の最小カーネルスタブ。"""

    def __init__(self):
        self.interface_registry = _FakeRegistry()
        self.diagnostics = _FakeDiagnostics()
        self._executor = None
        self._flow = None
        self.config = MagicMock()

    def _resolve_value(self, value, ctx, depth=0):
        """$flow.xxx 形式の簡易解決。テスト用途。"""
        if isinstance(value, str) and value.startswith("$flow."):
            parts = value[len("$flow."):].split(".")
            current = ctx
            for p in parts:
                if isinstance(current, dict):
                    current = current.get(p)
                else:
                    return None
            return current
        if isinstance(value, dict):
            return {k: self._resolve_value(v, ctx) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve_value(v, ctx) for v in value]
        return value

    def _vocab_normalize_output(self, unwrapped, step, ctx):
        return unwrapped

    def _resolve_handler(self, handler_key, args=None):
        return None

    def _build_kernel_context(self):
        return {}

    def _now_ts(self):
        return "2026-03-11T00:00:00Z"


@pytest.fixture
def engine():
    return _StubEngine()


# =====================================================================
# Wave 27-A: _eval_condition 比較演算子拡張
# =====================================================================


class TestEvalConditionComparison:
    """27-A: 比較演算子テスト。"""

    # 1. $flow.score > 80 — 数値比較 True
    def test_gt_true(self, engine):
        ctx = {"score": 90}
        assert engine._eval_condition("$flow.score > 80", ctx) is True

    # 2. $flow.score <= 80 — 数値比較 False
    def test_lte_false(self, engine):
        ctx = {"score": 90}
        assert engine._eval_condition("$flow.score <= 80", ctx) is False

    # 3. $flow.count >= 0 — ゼロ境界
    def test_gte_zero(self, engine):
        ctx = {"count": 0}
        assert engine._eval_condition("$flow.count >= 0", ctx) is True

    # 4. $flow.name == "hello" — 文字列比較（既存互換）
    def test_eq_string(self, engine):
        ctx = {"name": "hello"}
        assert engine._eval_condition('$flow.name == "hello"', ctx) is True

    # 5. $flow.flag != false — ブール比較（既存互換）
    def test_ne_bool(self, engine):
        ctx = {"flag": True}
        assert engine._eval_condition("$flow.flag != false", ctx) is True

    # 6. $flow.result == None — None 比較
    def test_eq_none(self, engine):
        ctx = {"result": None}
        assert engine._eval_condition("$flow.result == None", ctx) is True

    # 7. $flow.result != None — None 否定比較
    def test_ne_none(self, engine):
        ctx = {"result": "something"}
        assert engine._eval_condition("$flow.result != None", ctx) is True

    # 8. $flow.text > 80 — 数値変換不能 → False
    def test_gt_non_numeric(self, engine):
        ctx = {"text": "abc"}
        assert engine._eval_condition("$flow.text > 80", ctx) is False

    # 9. $flow.missing > 0 — 未定義変数 → False
    def test_gt_missing(self, engine):
        ctx = {}
        assert engine._eval_condition("$flow.missing > 0", ctx) is False


# =====================================================================
# Wave 27-B: ステップ出力の自動 ctx 格納
# =====================================================================


class TestAutoOutputStorage:
    """27-B: _execute_handler_step_async の出力格納テスト。"""

    # 10. 明示 output キー指定 → ctx 直下に格納（None も格納）
    def test_explicit_output_stores_none(self, engine):
        handler_key = "test:return_none"

        async def _handler(args, ctx):
            return None

        engine.interface_registry._store[handler_key] = _handler

        step = {
            "id": "s1",
            "type": "handler",
            "handler": handler_key,
            "output": "my_result",
        }
        ctx = {"_flow_id": "test", "_flow_execution_id": "e1"}

        async def run():
            return await engine._execute_handler_step_async(step, ctx)

        new_ctx, result = asyncio.run(run())
        assert "my_result" in new_ctx
        assert new_ctx["my_result"] is None

    # 11. output 未指定 + id あり → ctx["_step_out.{id}"] に格納
    def test_auto_output_with_id(self, engine):
        handler_key = "test:return_value"

        async def _handler(args, ctx):
            return {"data": 42}

        engine.interface_registry._store[handler_key] = _handler

        step = {"id": "analyze", "type": "handler", "handler": handler_key}
        ctx = {"_flow_id": "test", "_flow_execution_id": "e1"}

        async def run():
            return await engine._execute_handler_step_async(step, ctx)

        new_ctx, result = asyncio.run(run())
        assert "_step_out.analyze" in new_ctx
        assert new_ctx["_step_out.analyze"] == {"data": 42}

    # 12. output 未指定 + 結果 None → 格納されない
    def test_auto_output_none_not_stored(self, engine):
        handler_key = "test:return_none2"

        async def _handler(args, ctx):
            return None

        engine.interface_registry._store[handler_key] = _handler

        step = {"id": "noop", "type": "handler", "handler": handler_key}
        ctx = {"_flow_id": "test", "_flow_execution_id": "e1"}

        async def run():
            return await engine._execute_handler_step_async(step, ctx)

        new_ctx, result = asyncio.run(run())
        assert "_step_out.noop" not in new_ctx

    # 13. id: registry のステップ → ctx["registry"] は上書きされない
    def test_auto_output_no_ctx_pollution(self, engine):
        handler_key = "test:return_reg"

        async def _handler(args, ctx):
            return {"registered": True}

        engine.interface_registry._store[handler_key] = _handler

        step = {"id": "registry", "type": "handler", "handler": handler_key}
        ctx = {
            "_flow_id": "test",
            "_flow_execution_id": "e1",
            "registry": "original_value",
        }

        async def run():
            return await engine._execute_handler_step_async(step, ctx)

        new_ctx, result = asyncio.run(run())
        assert new_ctx["registry"] == "original_value"
        assert new_ctx["_step_out.registry"] == {"registered": True}


# =====================================================================
# Wave 27-C: function.call ステップタイプ
# =====================================================================


class _FakeResp:
    """capability_executor.execute の戻り値スタブ。"""

    def __init__(self, success: bool, output: Any = None, error: str = None):
        self.success = success
        self.output = output
        self.error = error


class TestFunctionStep:
    """27-C: _execute_function_step_async テスト。"""

    # 14. _principal_id なし → 実行拒否エラー
    def test_no_principal_id(self, engine):
        step = {"id": "fn1", "type": "function", "function": "pack:do_thing"}
        ctx = {"_flow_execution_id": "e1"}

        async def run():
            return await engine._execute_function_step_async(step, ctx)

        new_ctx, result = asyncio.run(run())
        assert result["_error"] == "no trusted flow principal in ctx"
        assert "_step_out.fn1" in new_ctx

    # 15. capability_executor 不在 → エラー返却
    def test_no_executor(self, engine):
        step = {"id": "fn2", "type": "function", "function": "pack:do_thing"}
        ctx = {"_flow_execution_id": "e1", "_flow_run_principal_id": "test_pack"}

        with patch.dict(_sys.modules, {"core_runtime.di_container": MagicMock()}):
            mock_di = _sys.modules["core_runtime.di_container"]
            mock_container = MagicMock()
            mock_container.get_or_none.return_value = None
            mock_di.get_container.return_value = mock_container

            async def run():
                return await engine._execute_function_step_async(step, ctx)

            new_ctx, result = asyncio.run(run())
            assert result["_error"] == "capability_executor not available"

    # 16. capability_executor は退役済み → 明示 output にフェイルクローズエラー
    def test_retired_executor_explicit_output(self, engine):
        step = {
            "id": "fn3",
            "type": "function",
            "function": "pack:analyze",
            "args": {"text": "hello"},
            "output": "analyze_result",
        }
        ctx = {"_flow_execution_id": "e1", "_flow_run_principal_id": "test_pack"}

        async def run():
            return await engine._execute_function_step_async(step, ctx)

        new_ctx, result = asyncio.run(run())
        assert result == {"_error": "capability_executor not available"}
        assert new_ctx["_step_out.analyze_result"] == result
        assert "_step_out.fn3" not in new_ctx

    # 17. capability_executor は退役済み → 自動格納にもフェイルクローズエラー
    def test_retired_executor_auto_output(self, engine):
        step = {
            "id": "fn4",
            "type": "function",
            "function": "pack:analyze",
            "args": {},
        }
        ctx = {"_flow_execution_id": "e1", "_flow_run_principal_id": "test_pack"}

        async def run():
            return await engine._execute_function_step_async(step, ctx)

        new_ctx, result = asyncio.run(run())
        assert result == {"_error": "capability_executor not available"}
        assert new_ctx["_step_out.fn4"] == result

    # 18. capability_executor は退役済み → DI 注入不可能でフェイルクローズ
    def test_retired_executor_cannot_be_injected(self, engine):
        step = {
            "id": "fn5",
            "type": "function",
            "function": "pack:bad_call",
            "args": {},
            "output": "bad_result",
        }
        ctx = {"_flow_execution_id": "e1", "_flow_run_principal_id": "test_pack"}

        mock_executor = MagicMock()
        mock_executor.execute.return_value = _FakeResp(
            success=True, output={"score": 1}
        )

        with patch.dict(_sys.modules, {"core_runtime.di_container": MagicMock()}):
            mock_di = _sys.modules["core_runtime.di_container"]
            mock_container = MagicMock()
            mock_container.get_or_none.return_value = mock_executor
            mock_di.get_container.return_value = mock_container

            async def run():
                return await engine._execute_function_step_async(step, ctx)

            new_ctx, result = asyncio.run(run())
            # DI 経由の注入は廃止: tombstone が常にフェイルクローズさせる。
            assert result == {"_error": "capability_executor not available"}
            assert new_ctx["_step_out.bad_result"] == result
            mock_executor.execute.assert_not_called()
