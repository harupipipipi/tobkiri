"""Real-engine parity checks for Cloudflare Workers Python definitions."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from ecosystem.rumi_cloudflare_worker_python_pack.runtime.worker import (
    TOOL_ALIASES,
    _definitions,
    create_definition_contribution,
)


ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = ROOT / "scripts" / "wasm" / "build_cloudflare_worker_definitions.py"


def _load_builder() -> Any:
    """Load the builder from its command path for direct conformance checks."""

    spec = importlib.util.spec_from_file_location(
        "cloudflare_worker_definitions_wasm_builder", BUILD_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = _load_builder()


def _require_wasmtime() -> Any:
    """Skip engine conformance checks when the optional engine is unavailable."""

    return pytest.importorskip("wasmtime")


def _invoke_component(
    binary: bytes, operation: str, payload: dict[str, object]
) -> tuple[str, str]:
    """Invoke the component with Wasmtime's component engine API."""

    wasmtime = _require_wasmtime()
    from wasmtime.component import Component, Linker

    engine = wasmtime.Engine()
    component = Component(engine, binary)
    store = wasmtime.Store(engine)
    function = Linker(engine).instantiate(store, component).get_func(store, "invoke")
    assert function is not None
    result = function(
        store,
        operation,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
    function.post_return(store)
    return result.tag, result.payload


def test_component_is_deterministic_and_has_no_imports() -> None:
    """The definitions catalog needs no Host, network, or WASI import."""

    wasmtime = _require_wasmtime()
    from wasmtime.component import Component

    first = BUILD.build_component()
    assert first == BUILD.build_component()
    assert first.startswith(b"\x00asm")
    engine = wasmtime.Engine()
    component = Component(engine, first)
    assert list(component.type.imports(engine)) == []


def test_builder_writes_only_the_requested_component(tmp_path: Path) -> None:
    """Building produces a caller-selected artifact without Pack mutation."""

    _require_wasmtime()
    output = tmp_path / "cloudflare-worker-definitions.wasm"

    assert BUILD.write_component(output) == 0
    assert output.read_bytes() == BUILD.build_component()


@pytest.mark.parametrize(
    "operation,payload",
    (
        ("list", {}),
        ("catalog", {"approved": True, "nested": {"value": [1, "two", None]}}),
    ),
)
def test_component_matches_native_worker_catalog(
    operation: str, payload: dict[str, object]
) -> None:
    """Both valid catalog operations return the Worker-owned definitions."""

    expected = create_definition_contribution(None)(operation, payload)
    assert expected == {"aliases": TOOL_ALIASES, "definitions": _definitions()}

    tag, content = _invoke_component(BUILD.build_component(), operation, payload)

    assert tag == "ok"
    assert json.loads(content) == expected


@pytest.mark.parametrize("operation", ("invoke", "unknown"))
def test_component_rejects_the_same_invalid_operations_as_native(operation: str) -> None:
    """Invalid catalog operations remain errors when executed by Wasmtime."""

    with pytest.raises(
        ValueError, match="unknown Workers Python catalog operation"
    ):
        create_definition_contribution(None)(operation, {})

    tag, message = _invoke_component(BUILD.build_component(), operation, {})

    assert tag == "err"
    assert message == BUILD.ERROR_MESSAGE
