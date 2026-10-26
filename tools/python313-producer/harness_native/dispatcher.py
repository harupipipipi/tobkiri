"""Fixed whole-method DATA dispatch; not a native owner or approval path."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable

import ordered_json_v2 as wire

from .value_graph import (
    ImportedValues,
    InvalidFrame,
    InvalidReference,
    RecordType,
    Reference,
    Registry,
    Scope,
    UnsupportedValue,
)

METHODS = (
    "bundled_profile_root",
    "host_resource_root",
    "bootstrap_profile_id",
    "setup_profile_preview",
    "_confirmation",
    "bootstrap_confirmation",
    "profile_confirmation",
    "setup_listing",
    "setup_preview",
    "setup_activation_decision",
    "setup_activation_success",
    "setup_activation_failure",
    "retired_setup_response",
    "load_catalog",
    "catalog_with_packs",
    "catalog_with_profiles",
    "resolve_profile",
    "dynamic_profile_edges",
    "denied",
)
PREFIX = "io.tobkiri.native-"


class NotInitialized(Exception):
    """No invocation-local registry has been accepted."""


class AlreadyInitialized(Exception):
    """The accepted original scope cannot be replaced."""


class MethodRaised(Exception):
    """Private original exception retained; its text/fields are not disclosed."""

    def __init__(self, details: str) -> None:
        super().__init__("original exception fields are not disclosed")
        self.details = details


@dataclass(frozen=True)
class OriginalRuntime:
    """Only trusted bootstrap supplies these actual original objects/classes."""

    runtime: Any
    records: tuple[RecordType, ...]
    method_errors: tuple[type[Exception], ...]
    # None means actual native input admission is not connected. An explicit
    # tuple contains only preexisting opaque proxies supplied by bootstrap.
    native_inputs: tuple[tuple[str, Any], ...] | None = None


@dataclass(frozen=True)
class _State:
    runtime: Any
    registry: Registry
    method_errors: tuple[type[Exception], ...]
    native_inputs: tuple[tuple[str, Any], ...] | None


class Dispatcher:
    """Share one registry across all adapters in one guest interpreter.

    Its scope is DATA. The nativeKit caller and actual imported IO bindings must
    independently retain their real original owner/account/invocation. No frame
    constructs a class, chooses a module, runs code or replenishes a budget.
    """

    def __init__(
        self,
        bootstrap: Callable[[Scope], OriginalRuntime],
        *,
        init_failure_observer: Callable[[Scope, str, BaseException], None] | None = None,
        init_checkpoint_observer: Callable[[int], None] | None = None,
    ) -> None:
        self._bootstrap = bootstrap
        self._init_failure_observer = init_failure_observer
        self._init_checkpoint_observer = init_checkpoint_observer
        self._state: _State | None = None
        self._init_failure: BaseException | None = None
        self._init_failure_scope: Scope | None = None
        self._init_failure_phase: str | None = None

    def _current(self) -> _State:
        if self._state is None:
            raise NotInitialized()
        return self._state

    @staticmethod
    def _load(payload: str) -> dict[str, Any]:
        try:
            frame = wire.loads(payload)
        except wire.OrderedJSONError:
            raise InvalidFrame("invalid or oversized transport frame") from None
        if type(frame) is not dict:
            raise InvalidFrame("closed frame required")
        return frame

    def init(self, payload: str) -> None:
        """Validate the complete scope before loading and committing any state."""
        if self._state is not None:
            raise AlreadyInitialized()
        self._checkpoint(1)
        # A failed attempt is retained only until the next init starts. Scope
        # validation failures cannot accidentally report an older generation.
        self._init_failure = None
        self._init_failure_scope = None
        self._init_failure_phase = None
        frame = self._load(payload)
        self._checkpoint(2)
        if set(frame) != {"format", "scope"} or frame["format"] != PREFIX + "init.v1":
            raise InvalidFrame("invalid original initialization frame")
        scope = Scope.from_wire(frame["scope"])
        self._checkpoint(3)
        try:
            self._checkpoint(4)
            original = self._bootstrap(scope)
            self._checkpoint(5)
        except BaseException as error:
            self._observe_init_failure(scope, "original-factory", error)
            raise
        try:
            self._checkpoint(6)
            state = self._registry_state(scope, original)
            self._checkpoint(7)
        except BaseException as error:
            self._observe_init_failure(scope, "transport-registry", error)
            raise
        self._state = state

    def _checkpoint(self, sequence: int) -> None:
        """Ordinary observer errors cannot replace the original init behavior."""
        try:
            if self._init_checkpoint_observer is not None:
                self._init_checkpoint_observer(sequence)
        except BaseException:
            # Runtime traps and exhausted budgets remain outer failures; this
            # only contains ordinary Python observer exceptions.
            pass

    def _observe_init_failure(
        self, scope: Scope, phase: str, error: BaseException,
    ) -> None:
        """Retain the actual failure without making reporting a new failure."""
        self._init_failure = error
        self._init_failure_scope = scope
        self._init_failure_phase = phase
        try:
            if self._init_failure_observer is not None:
                self._init_failure_observer(scope, phase, error)
        except BaseException:
            # Reporting is best effort. This does not make native traps
            # catchable, and no reporting failure may replace the original.
            pass

    @staticmethod
    def _registry_state(scope: Scope, original: OriginalRuntime) -> _State:
        if (
            type(original) is not OriginalRuntime
            or type(original.method_errors) is not tuple
        ):
            raise UnsupportedValue("invalid trusted original runtime bundle")
        for cls in original.method_errors:
            if (
                not isinstance(cls, type)
                or not issubclass(cls, Exception)
                or issubclass(cls, (MemoryError, RecursionError))
            ):
                raise UnsupportedValue("execution failures cannot be method errors")
        registry = Registry(scope, original.records)
        records = {record.cls: record for record in original.records}
        for cls in original.method_errors:
            if cls not in records:
                raise UnsupportedValue(
                    "method exception class is not in original inventory"
                )
        # Dedicated provenance-aware exception field extraction is not connected
        # yet. Prevent describe, including nested descriptions, from exposing it.
        for record in original.records:
            if issubclass(record.cls, BaseException) and record.fields:
                raise UnsupportedValue("exception field disclosure is not connected")
        if original.native_inputs is not None:
            if type(original.native_inputs) is not tuple:
                raise UnsupportedValue("invalid trusted native input inventory")
            names = set()
            for item in original.native_inputs:
                if (
                    type(item) is not tuple
                    or len(item) != 2
                    or type(item[0]) is not str
                    or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", item[0])
                    or item[0] in names
                ):
                    raise UnsupportedValue("invalid trusted native input binding")
                record = records.get(type(item[1]))
                if record is None or not record.native_binding:
                    raise UnsupportedValue("native input must be an opaque proxy")
                names.add(item[0])
        return _State(
            original.runtime, registry, original.method_errors, original.native_inputs
        )

    def _frame(self, payload: str, kind: str, fields: set[str]) -> dict[str, Any]:
        state = self._current()
        frame = self._load(payload)
        if (
            set(frame) != {"format", "scope"} | fields
            or frame["format"] != PREFIX + kind + ".v1"
            or frame["scope"] != state.registry.scope.wire()
        ):
            raise InvalidFrame("invalid or foreign transport frame")
        return frame

    def _result(self, value: Any, *, set_items: bool = False) -> str:
        state = self._current()
        created: list[tuple[Reference, int]] = []
        try:
            ref, _ = state.registry._retain(value, set_items=set_items, created=created)
            return wire.dumps(
                {
                    "format": PREFIX + "value-result.v1",
                    "scope": state.registry.scope.wire(),
                    "root": ref.wire(),
                    "owned": [format(r.object_id, "x") for r, _ in created],
                }
            )
        except BaseException:
            state.registry._rollback(created)
            raise

    def _apply_original(self, action: Callable[[], Any]) -> Any:
        state = self._current()
        try:
            value = action()
        except (MemoryError, RecursionError):
            raise
        except Exception as error:
            if type(error) not in state.method_errors:
                raise UnsupportedValue(
                    "original method failure is not represented"
                ) from None
            # No str/repr, traceback, args, filename or cause/context disclosure.
            failure = MethodRaised("")
            failure.details = self._result(error)
            raise failure from None
        return value

    def call(self, method: str, payload: str) -> str:
        """Invoke a fixed export; preserve Python's original argument binding."""
        state = self._current()
        if method not in METHODS:
            raise InvalidFrame("method is not a fixed original export")
        frame = self._frame(payload, "call", {"args", "kwargs"})
        if type(frame["args"]) is not list or type(frame["kwargs"]) is not list:
            raise InvalidFrame("invalid original call arguments")
        args = [
            state.registry.borrow(state.registry.reference(ref))
            for ref in frame["args"]
        ]
        kwargs = {}
        for item in frame["kwargs"]:
            if (
                type(item) is not list
                or len(item) != 2
                or type(item[0]) is not str
                or item[0] in kwargs
            ):
                raise InvalidFrame("invalid or duplicate original keyword")
            kwargs[item[0]] = state.registry.borrow(state.registry.reference(item[1]))
        target = getattr(state.runtime, method)
        return self._result(self._apply_original(lambda: target(*args, **kwargs)))

    def import_values(self, payload: str) -> str:
        """Import only builtin DATA and existing issued references."""
        registry = self._current().registry

        def publish(result: ImportedValues) -> str:
            return wire.dumps(
                {"format": PREFIX + "values-imported.v1", **result.wire()}
            )

        return registry.import_values(payload, _publish=publish)

    def native_inputs(self, payload: str) -> str:
        """Publish existing native proxies without IO or target selection.

        Repeated publication reuses each live issued reference and reports only
        newly owned holds. Releasing a reference drops that transport hold, not
        bootstrap's original proxy. A later publication can issue a new ID for
        that same object; a released ID never becomes valid again. These IDs do
        not authorize effects or create native ownership.
        """
        self._frame(payload, "native-inputs", set())
        state = self._current()
        if state.native_inputs is None:
            raise UnsupportedValue("native input admission is not connected")
        created: list[tuple[Reference, int]] = []
        try:
            inputs = []
            for name, value in state.native_inputs:
                ref, _ = state.registry._retain(value, created=created)
                inputs.append([name, ref.wire()])
            return wire.dumps(
                {
                    "format": PREFIX + "native-inputs-published.v1",
                    "scope": state.registry.scope.wire(),
                    "inputs": inputs,
                    "owned": [format(ref.object_id, "x") for ref, _ in created],
                }
            )
        except BaseException:
            state.registry._rollback(created)
            raise

    def describe_value(self, payload: str) -> str:
        """Describe reviewed DATA fields, never arbitrary reflection."""
        frame = self._frame(payload, "value-describe", {"root"})
        registry = self._current().registry
        return registry.describe(registry.reference(frame["root"]))

    def copy_data_dictionary(self, payload: str) -> str:
        """Create the original caller's fresh outer dict, retaining nested aliases."""
        frame = self._frame(payload, "value-copy-dictionary", {"source"})
        registry = self._current().registry
        source = registry.borrow(registry.reference(frame["source"]), dict)
        return self._result(dict(source), set_items=True)

    def release_values(self, payload: str) -> None:
        """Validate a whole release batch before dropping its transport holds."""
        frame = self._frame(payload, "values-release", {"refs"})
        if type(frame["refs"]) is not list:
            raise InvalidFrame("invalid reference release list")
        registry = self._current().registry
        registry.release([registry.reference(ref) for ref in frame["refs"]])

    def append_data_item(self, payload: str) -> None:
        """Use the permitted original list; other aliases observe the append."""
        frame = self._frame(payload, "value-append", {"target", "item"})
        registry = self._current().registry
        target, item = (registry.reference(frame[k]) for k in ("target", "item"))
        registry.borrow(target, list)
        if not registry._permissions[target.object_id][0]:
            raise InvalidReference("list mutation was not enabled for this DATA node")
        self._apply_original(lambda: registry.append(target, item))

    def set_data_item(self, payload: str) -> None:
        """Use the permitted original dictionary without normalizing its errors."""
        frame = self._frame(payload, "value-set", {"target", "key", "item"})
        registry = self._current().registry
        target, key, item = (
            registry.reference(frame[k]) for k in ("target", "key", "item")
        )
        # Validate transport permissions before the original dictionary operation.
        registry.borrow(target, dict)
        if not registry._permissions[target.object_id][1]:
            raise InvalidReference(
                "dictionary mutation was not enabled for this DATA node"
            )
        self._apply_original(lambda: registry.set_item(target, key, item))
