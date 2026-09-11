"""Real-engine checks for the import-free worker's component boundary."""

from __future__ import annotations

import hashlib
import base64
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from tobkiri_host.errors import InvalidArtifactError, ProviderExecutionError
from tobkiri_host.wasm_component import PureComponent
from tobkiri_host.wasm_worker import ComponentWorker

wasmtime = pytest.importorskip("wasmtime")


def worker_command(*, isolated: bool = True) -> tuple[str, ...]:
    """Pin fixture roots explicitly; never read import configuration from a request."""
    roots = [str(Path(__file__).resolve().parents[1]),
             str(Path(wasmtime.__file__).resolve().parent.parent)]
    code = (
        "import sys, runpy; sys.path[:0] = " + repr(roots) + "; "
        "runpy.run_module('tobkiri_host.wasm_component', run_name='__main__')"
    )
    return (sys.executable, *(["-I"] if isolated else []), "-B", "-c", code)


def invoke_worker(
    request: object,
    *,
    isolated: bool = True,
    rss_limit: int | None = None,
) -> subprocess.CompletedProcess:
    """Launch a real child; fixture roots are trusted, never read from its request."""
    return subprocess.run(
        worker_command(isolated=isolated),
        input=json.dumps(request).encode(), capture_output=True,
        env=(
            {}
            if rss_limit is None
            else {"TOBKIRI_WASM_WORKER_RSS_LIMIT_BYTES": str(rss_limit)}
        ),
        close_fds=True, timeout=15, check=False,
    )


def worker_request() -> dict[str, object]:
    """Return a complete request for an import-free fixture component."""
    binary = component()
    return {
        "artifact": base64.b64encode(binary).decode(),
        "digest": "sha256:" + hashlib.sha256(binary).hexdigest(),
        "operation_id": "inspect", "payload": {},
    }


def test_child_worker_compiles_and_returns_one_result() -> None:
    result = invoke_worker(worker_request())
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"status": "ok", "data": {"ok": True}}
    assert result.stderr == b""


def test_supervised_real_component_returns_after_child_exit() -> None:
    owned = ComponentWorker(worker_command())
    assert owned.invoke(worker_request(), cancelled=threading.Event()) == {"ok": True}
    assert owned._process is None


def test_child_worker_rejects_success_above_os_peak_rss_limit() -> None:
    result = invoke_worker(worker_request(), rss_limit=1)
    assert result.returncode == 3
    assert json.loads(result.stdout) == {
        "status": "error", "code": "wasm_worker_memory_limit"
    }
    assert result.stderr == b""


@pytest.mark.parametrize("change", [
    {"digest": "sha256:" + "0" * 64},
    {"artifact": "not-base64!"},
    {"payload": []},
    {"operation_id": None},
    {"python_path": "/untrusted"},
])
def test_child_worker_rejects_invalid_frames_without_echo(change: dict) -> None:
    request = worker_request()
    request.update(change)
    result = invoke_worker(request)
    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "status": "error", "code": "wasm_worker_failed"
    }
    assert result.stderr == b""


def test_child_worker_refuses_nonisolated_python() -> None:
    result = invoke_worker(worker_request(), isolated=False)
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == b""


def component(body: str = "i32.const 0", *, output: str = '{"ok":true}') -> bytes:
    """Build a tiny component with the same ABI as the Shell Policy guest."""
    content = output.encode("utf-8")
    descriptor = bytes(4) + (32).to_bytes(4, "little") + len(content).to_bytes(4, "little")
    descriptor_wat = "".join(f"\\{byte:02x}" for byte in descriptor)
    content_wat = "".join(f"\\{byte:02x}" for byte in content)
    return bytes(
        wasmtime.wat2wasm(
            f"""
        (component
          (core module $guest
            (memory (export "memory") 1)
            (data (i32.const 0) "{descriptor_wat}")
            (data (i32.const 32) "{content_wat}")
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


@pytest.mark.parametrize("output", [
    '{"ok":true,"ok":false}', '{"value":NaN}', '{"value":Infinity}',
    '{"value":1.5}', '{"value":9007199254740992}', '{"value":"\\ud800"}',
    '{"value":' + '[' * 65 + '0' + ']' * 65 + '}', '[]',
])
def test_guest_output_requires_unambiguous_protocol_json(output: str) -> None:
    engine = guest(component(output=output))
    with pytest.raises(ProviderExecutionError, match="not a JSON object"):
        engine.invoke("inspect", {})
    with pytest.raises(ProviderExecutionError, match="consumed"):
        engine.invoke("inspect", {})


@pytest.mark.parametrize("payload", [
    {"value": float("nan")}, {"value": 1.5}, {"value": 2**53},
    {"value": "\ud800"}, {1: "ambiguous key"},
])
def test_noncanonical_input_does_not_consume_guest(payload: dict) -> None:
    engine = guest(component())
    with pytest.raises(ValueError):
        engine.invoke("inspect", payload)
    assert engine.invoke("inspect", {}) == {"ok": True}
