"""Real-engine checks for the import-free worker's component boundary."""

from __future__ import annotations

import hashlib
import threading

import pytest

from tobkiri_host.errors import InvalidArtifactError, ProviderExecutionError
from tobkiri_host.wasm_component import PureComponent

wasmtime = pytest.importorskip("wasmtime")


def component(body: str = "i32.const 0") -> bytes:
    """Build a tiny component with the same ABI as the Shell Policy guest."""
    return bytes(
        wasmtime.wat2wasm(
            r"""
        (component
          (core module $guest
            (memory (export "memory") 1)
            (data (i32.const 0) "\00\00\00\00\20\00\00\00\0b\00\00\00")
            (data (i32.const 32) "{\22ok\22:true}")
            (func (export "realloc") (param i32 i32 i32 i32) (result i32)
              i32.const 4096)
            (func (export "invoke") (param i32 i32 i32 i32) (result i32)
    """
            + body
            + r"""))
          (core instance $guest (instantiate $guest))
          (func (export "invoke")
            (param "operation-id" string) (param "payload-json" string)
            (result (result string (error string)))
            (canon lift (core func $guest "invoke")
              (memory $guest "memory") (realloc (func $guest "realloc")))))
    """
        )
    )


def guest(binary: bytes) -> PureComponent:
    """Pin the exact fixture bytes as the supervisor would."""
    return PureComponent(binary, "sha256:" + hashlib.sha256(binary).hexdigest())


def test_result_and_request_isolation() -> None:
    engine = guest(component())
    assert engine.invoke("inspect", {}) == {"ok": True}
    with pytest.raises(ProviderExecutionError, match="consumed"):
        engine.invoke("inspect", {})


def test_digest_mismatch_is_rejected() -> None:
    with pytest.raises(InvalidArtifactError, match="digest"):
        PureComponent(component(), "sha256:" + "0" * 64)


def test_matching_digest_does_not_make_invalid_wasm_executable() -> None:
    with pytest.raises(InvalidArtifactError, match="component is invalid"):
        guest(b"not a component")


def test_host_import_is_rejected_before_instantiation() -> None:
    binary = bytes(wasmtime.wat2wasm('(component (import "host" (func)))'))
    with pytest.raises(InvalidArtifactError, match="cannot import"):
        guest(binary)


def test_infinite_guest_exhausts_fuel() -> None:
    engine = guest(component("(loop $forever br $forever) unreachable"))
    with pytest.raises(ProviderExecutionError, match="exceeded its limits"):
        engine.invoke("inspect", {}, fuel=1000)


def test_memory_limit_rejects_instantiation() -> None:
    with pytest.raises(ProviderExecutionError, match="exceeded its limits"):
        guest(component()).invoke("inspect", {}, memory_bytes=1024)


@pytest.mark.parametrize("field", ["fuel", "memory_bytes"])
@pytest.mark.parametrize("value", [True, False, 1.5, "1024", None, 0, -1, 2**64])
def test_limits_require_bounded_exact_integers(field: str, value: object) -> None:
    """Reject malformed limits before consuming a request or calling the guest."""
    engine = guest(component())
    with pytest.raises(ValueError, match="limits are invalid"):
        engine.invoke("inspect", {}, **{field: value})
    assert engine.invoke("inspect", {}) == {"ok": True}


def test_cancel_before_invocation_permanently_fences_request() -> None:
    engine = guest(component())
    engine.cancel()
    with pytest.raises(ProviderExecutionError, match="cancelled"):
        engine.invoke("inspect", {})
    with pytest.raises(ProviderExecutionError, match="consumed"):
        engine.invoke("inspect", {})


def test_cancel_interrupts_an_executing_guest() -> None:
    engine = guest(component("(loop $forever br $forever) unreachable"))
    timer = threading.Timer(0.01, engine.cancel)
    timer.start()
    try:
        with pytest.raises(ProviderExecutionError) as failure:
            engine.invoke("inspect", {})
        # Distinguish actual engine interruption from fuel or a pre-call fence.
        assert "wasm trap: interrupt" in str(failure.value.__context__)
    finally:
        timer.cancel()
        timer.join()


def test_oversized_input_does_not_enter_guest() -> None:
    engine = guest(component())
    with pytest.raises(ValueError, match="transport limit"):
        engine.invoke("inspect", {"text": "x" * (1024 * 1024)})
