"""Fail-closed replacement for legacy capability executor security tests."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.legacy_authority_contracts import (
    assert_legacy_service_fails_closed,
    assert_retired_module_absent,
)


def test_capability_executor_authority_is_physically_retired() -> None:
    """No old executor can become an execution authority by import."""
    assert_retired_module_absent("core_runtime.capability_executor")


def test_legacy_authority_service_rejects_execution() -> None:
    """The explicit tombstone rejects the former approval/execution workflow."""
    assert_legacy_service_fails_closed()


def test_retired_capability_executor_tombstone_fails_closed() -> None:
    """The executor tombstone raises the explicit retirement error."""
    from core_runtime.legacy_runtime_removed import removed_capability_executor

    with pytest.raises(
        RuntimeError,
        match="capability_executor is unavailable in Pack v4 production runtime",
    ):
        removed_capability_executor()


def test_pack_function_runtime_retired_conventions_fail_closed() -> None:
    """Non-block calling conventions reach the tombstone, never a dead import."""
    from core_runtime.pack_function_runtime import execute_function_entry

    entry = SimpleNamespace(
        pack_id="pack_x",
        function_id="fn",
        qualified_name="pack_x:fn",
        function_dir="/tmp",
        entrypoint="main.py:run",
        calling_convention="subprocess",
        permission_id=None,
        is_builtin=False,
    )
    for convention in ("subprocess", "python_docker", "python_host", "command"):
        entry.calling_convention = convention
        with pytest.raises(
            RuntimeError, match="capability_executor is unavailable"
        ):
            execute_function_entry(entry, {}, {})


def test_capability_installer_executor_resolution_fails_closed() -> None:
    """The installer's lazy executor lookup is an explicit tombstone."""
    from core_runtime.capability_installer import _get_executor

    with pytest.raises(RuntimeError, match="capability_executor is unavailable"):
        _get_executor()


def test_capability_proxy_initialize_fails_closed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proxy startup cannot resurrect the retired executor service."""
    monkeypatch.setenv("RUMI_CAPABILITY_SOCK_DIR", str(tmp_path))
    from core_runtime.capability_proxy import HostCapabilityProxyServer

    with pytest.raises(RuntimeError, match="capability_executor is unavailable"):
        HostCapabilityProxyServer().initialize()


def test_kernel_flow_function_steps_fail_closed() -> None:
    """Both kernel flow function paths tombstone the retired executor."""
    import asyncio

    from core_runtime.kernel_flow_execution import KernelFlowExecutionMixin

    class _Engine(KernelFlowExecutionMixin):
        def __init__(self) -> None:
            self.diagnostics = MagicMock()
            self.interface_registry = MagicMock()
            self.config = MagicMock()
            self._executor = None
            self._flow = None

        def _resolve_value(self, value, ctx, depth=0):
            return value

    engine = _Engine()

    async_step = {"id": "fn", "type": "function", "function": "pack:fn"}
    async_ctx = {
        "_flow_execution_id": "e1",
        "_flow_run_principal_id": "pack",
    }

    async def run_async():
        return await engine._execute_function_step_async(async_step, async_ctx)

    new_ctx, result = asyncio.run(run_async())
    assert result == {"_error": "capability_executor not available"}
    assert new_ctx["_step_out.fn"] == result

    sync_step = {"id": "fn", "function": "pack:fn"}
    sync_ctx = {
        "_flow_run_principal_id": "pack",
        "_flow_defaults": {"fail_soft": True},
    }
    aborted = engine._execute_flow_step(sync_step, phase="test", ctx=sync_ctx)
    assert aborted is False
    assert "_step_out.fn" not in sync_ctx
