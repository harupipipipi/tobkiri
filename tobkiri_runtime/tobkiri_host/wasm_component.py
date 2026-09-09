"""Import-free component execution inside a supervised Wasm worker.

This engine is not a production backend registration. Its caller must isolate
compilation in a worker, reserve resources, enforce a wall deadline, and bind
the artifact and request through Authority before invoking guest code.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_json, strict_loads

from .errors import InvalidArtifactError, ProviderExecutionError


class PureComponent:
    """One request's private engine, with no WASI or Host capability imports."""

    def __init__(self, binary: bytes, expected_digest: str) -> None:
        from wasmtime import Config, Engine, WasmtimeError
        from wasmtime.component import Component

        if not binary or len(binary) > 32 * 1024 * 1024:
            raise InvalidArtifactError("Wasm artifact size is outside the worker limit")
        actual = "sha256:" + hashlib.sha256(binary).hexdigest()
        if actual != expected_digest:
            raise InvalidArtifactError("Wasm artifact digest mismatch")
        config = Config()
        # Pulley interprets guest instructions without guest native JIT code.
        config.target = "pulley64"
        config.consume_fuel = True
        config.epoch_interruption = True
        self._engine = Engine(config)
        try:
            self._component = Component(self._engine, binary)
        except WasmtimeError:
            raise InvalidArtifactError("Wasm component is invalid") from None
        if self._component.type.imports(self._engine):
            raise InvalidArtifactError("pure Wasm components cannot import capabilities")
        self._cancelled = threading.Event()
        self._claimed = threading.Lock()

    def cancel(self) -> None:
        """Permanently fence this request and interrupt running guest execution."""
        self._cancelled.set()
        self._engine.increment_epoch()

    def invoke(
        self,
        operation_id: str,
        payload: Mapping[str, Any],
        *,
        fuel: int = 100_000_000,
        memory_bytes: int = 128 * 1024 * 1024,
    ) -> dict[str, Any]:
        """Invoke once with bounded guest memory, instructions, and JSON traffic.

        Limits may be reduced by the supervisor, never increased by a Pack.
        Guest limits do not bound compiler memory or replace worker accounting.
        """
        from wasmtime import Store, Trap, WasmtimeError
        from wasmtime.component import Linker, Variant

        if (
            type(fuel) is not int
            or type(memory_bytes) is not int
            or not 0 < fuel <= 100_000_000
            or not 0 < memory_bytes <= 128 * 1024 * 1024
        ):
            raise ValueError("Wasm invocation limits are invalid")
        if not isinstance(operation_id, str) or not 0 < len(operation_id) <= 1024:
            raise ValueError("Wasm operation identity is invalid")
        encoded_bytes = canonical_json(dict(payload))
        if len(encoded_bytes) > 1024 * 1024:
            raise ValueError("Wasm input exceeds the transport limit")
        encoded = encoded_bytes.decode("utf-8")
        if not self._claimed.acquire(blocking=False):
            raise ProviderExecutionError("Wasm request has already been consumed")
        # The lock is intentionally never released: a guest cannot retain state
        # across requests, and late cancellation cannot interrupt another call.
        store = Store(self._engine)
        store.set_limits(
            memory_size=memory_bytes,
            table_elements=100_000,
            instances=64,
            tables=64,
            memories=1,
        )
        store.set_fuel(fuel)
        store.set_epoch_deadline(1)
        if self._cancelled.is_set():
            raise ProviderExecutionError("Wasm request was cancelled")
        try:
            instance = Linker(self._engine).instantiate(store, self._component)
            function = instance.get_func(store, "invoke")
            if function is None:
                raise InvalidArtifactError("Wasm invoke export is missing")
            result = function(store, operation_id, encoded)
            function.post_return(store)
        except (Trap, WasmtimeError):
            raise ProviderExecutionError("Wasm execution failed or exceeded its limits") from None
        if self._cancelled.is_set():
            raise ProviderExecutionError("Wasm request was cancelled")
        if not isinstance(result, Variant) or result.tag != "ok":
            raise ProviderExecutionError("Wasm component rejected the request")
        if not isinstance(result.payload, str):
            raise ProviderExecutionError("Wasm output exceeds the transport limit")
        try:
            output = strict_loads(result.payload, max_bytes=1024 * 1024)
        except (ValueError, RecursionError):
            raise ProviderExecutionError("Wasm output is not a JSON object") from None
        if not isinstance(output, dict):
            raise ProviderExecutionError("Wasm output is not a JSON object")
        return output
