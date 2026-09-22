from __future__ import annotations

import pytest

from core_runtime.dependency_resolver import CircularDependencyError
from ecosystem.defaultspack.backend.module_state import ModuleState, ModuleStateManager
from ecosystem.defaultspack.backend.module_catalog import ModuleCatalog
from ecosystem.defaultspack.backend.dependency_manager import ModuleDependencyResolver


def test_register_and_states():
    sm = ModuleStateManager()
    sm.register_module("chat", default_state="enabled")
    assert sm.get_state("chat") == ModuleState.ENABLED
    sm.disable("chat")
    assert sm.get_state("chat") == ModuleState.DISABLED
    sm.enable("chat")
    assert sm.get_state("chat") == ModuleState.ENABLED


def test_failure_threshold_and_catalog():
    sm = ModuleStateManager()
    sm.register_module("tool", default_state="enabled", failure_threshold=2)
    sm.record_failure("tool", "err1")
    sm.record_failure("tool", "err2")
    assert sm.get_state("tool") == ModuleState.ERROR_DISABLED
    cat = ModuleCatalog(sm)
    assert cat.summary()["total"] == 1
    assert ModuleDependencyResolver(sm).resolve_load_order() == ["tool"]


def test_module_dependency_resolver_rejects_cycles() -> None:
    state = ModuleStateManager()
    state.register_module("alpha", dependencies=["bravo"])
    state.register_module("bravo", dependencies=["alpha"])

    with pytest.raises(CircularDependencyError):
        ModuleDependencyResolver(state).resolve_load_order()
