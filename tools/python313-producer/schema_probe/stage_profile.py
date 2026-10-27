"""Diagnostic-only schema phase markers and bounded host profile evidence."""

from __future__ import annotations

PROFILE_IMPORT_NAME = "tobkiri-ui:schema-probe/diagnostic-profile@1.0.0"
PROFILE_INTERFACE = "diagnostic-profile"
MAX_EVENTS = 1024
MAX_LABEL_CHARS = 128
MAX_METADATA_BYTES = 1024 * 1024
MAX_EVENT_BYTES = MAX_METADATA_BYTES - 4096
MAX_FUEL = 2_000_000_000
FUEL_API_IDENTITY = {
    "wasmtime_version": "48.0.0",
    "source_tag": "48.0.0",
    "component_linker_source_blob": "6ce56ddcecac6b26285353f7dbb7ecc985e283db",
    "store_context_source_blob": "bd3a6c2a053bd8ac42ef92b3a0160466fc2cffd7",
    "component_types_source_blob": "8bd5e74b7ddea7f33296440648aa337052614d70",
    "component_public_exports_source_blob": "ba633508d16b924aaec2d110cd423cdef9ebab78",
    "context_method": "StoreContext._context",
    "c_function": "wasmtime_context_get_fuel",
    "error_conversion": "WasmtimeError._from_ptr",
    "context_retained_after_callback": False,
}

_CALLBACK = None
_ARMED = False
_EVER_ARMED = False


def set_callback(callback: object) -> None:
    """Bind the generated WIT import without calling it during preinit."""
    global _CALLBACK
    if _ARMED or _CALLBACK is not None or not callable(callback):
        raise RuntimeError("stage-profile callback setup is invalid")
    _CALLBACK = callback


def arm() -> None:
    """Enable phase markers only when the derived exported operation begins."""
    global _ARMED, _EVER_ARMED
    if _ARMED or _EVER_ARMED or _CALLBACK is None:
        raise RuntimeError("stage-profile emitter is not ready to arm")
    _ARMED = True
    _EVER_ARMED = True


def disarm() -> None:
    """Stop diagnostic markers after the unchanged exercise returns."""
    global _ARMED
    _ARMED = False


def checkpoint(phase: str, key: str) -> None:
    """Emit one bounded data-only marker when explicitly armed."""
    if not _ARMED:
        return
    if (
        type(phase) is not str
        or type(key) is not str
        or len(phase) > MAX_LABEL_CHARS
        or len(key) > MAX_LABEL_CHARS
    ):
        raise ValueError("schema profile labels exceed their 128-character bound")
    callback = _CALLBACK
    if callback is None:
        raise RuntimeError("schema profile callback disappeared after arming")
    callback(phase, key)


def validate_import(import_name: str, item: object, engine: object) -> None:
    """Require only checkpoint(phase: string, key: string) -> unit."""
    if import_name != PROFILE_IMPORT_NAME:
        raise ValueError("unsupported schema diagnostic-profile import name")
    import wasmtime.component as component_types

    if not isinstance(item, component_types.ComponentInstanceType):
        raise ValueError("schema diagnostic-profile must be a component instance")
    exports = item.exports(engine)
    if not isinstance(exports, dict) or set(exports) != {"checkpoint"}:
        raise ValueError("schema diagnostic-profile must export only checkpoint")
    function = exports["checkpoint"]
    if not isinstance(function, component_types.FuncType):
        raise ValueError("schema checkpoint must be a component FuncType")
    params = function.params
    if (
        not isinstance(params, list)
        or len(params) != 2
        or any(not isinstance(param, tuple) or len(param) != 2 for param in params)
        or type(params[0][0]) is not str
        or type(params[1][0]) is not str
        or params[0][0] != "phase"
        or params[1][0] != "key"
        or not isinstance(params[0][1], component_types.String)
        or not isinstance(params[1][1], component_types.String)
        or function.result is not None
    ):
        raise ValueError(
            "schema checkpoint must be (phase: string, key: string) -> unit"
        )


def _replace_once(source: bytes, old: bytes, new: bytes, name: str) -> bytes:
    if source.count(old) != 1:
        raise ValueError(f"frozen {name} source no longer matches its exact anchor")
    return source.replace(old, new, 1)


def derive_stage_profile_sources(
    baseline_sources: dict[str, bytes], helper_source: bytes
) -> tuple[dict[str, bytes], dict[str, object]]:
    """Derive private profile-only app, exercise, loader, and WIT source bytes."""
    import difflib
    import hashlib

    required = {"app.py", "exercise.py", "captured_loader.py", "probe.wit"}
    if set(baseline_sources) != required or type(helper_source) is not bytes:
        raise ValueError("complete frozen schema probe source set required")
    if any(type(raw) is not bytes for raw in baseline_sources.values()):
        raise TypeError("frozen schema probe sources must be bytes")

    app = _replace_once(
        baseline_sources["app.py"],
        b"import json\n",
        b"import json\nimport stage_profile\n",
        "app.py early profile import",
    )
    app = _replace_once(
        app,
        b"from exercise import exercise\n",
        b"from exercise import exercise\n"
        b"from wit_world.imports import diagnostic_profile\n"
        b"stage_profile.set_callback(diagnostic_profile.checkpoint)\n",
        "app.py import",
    )
    app = _replace_once(
        app,
        b"        try:\n            observation = exercise()\n",
        b"        try:\n"
        b"            stage_profile.arm()\n"
        b"            try:\n"
        b"                observation = exercise()\n"
        b"            finally:\n"
        b"                stage_profile.disarm()\n",
        "app.py run",
    )

    exercise = _replace_once(
        baseline_sources["exercise.py"],
        b"import json\n",
        b"import json\nfrom stage_profile import checkpoint as _profile_checkpoint\n",
        "exercise.py import",
    )
    exercise = _replace_once(
        exercise,
        b'    """Perform validation, late module/resource/metadata IO and public API calls."""\n'
        b"    import sys\n",
        b'    """Perform validation, late module/resource/metadata IO and public API calls."""\n'
        b'    _profile_checkpoint("import.begin", "exercise")\n'
        b"    import sys\n",
        "exercise.py import boundary start",
    )
    exercise = _replace_once(
        exercise,
        b"    import jsonschema_specifications\n",
        b"    import jsonschema_specifications\n"
        b'    _profile_checkpoint("import.end", "exercise")\n',
        "exercise.py import boundary end",
    )
    for import_name in (
        "referencing.retrieval",
        "jsonschema.protocols",
        "packaging.version",
        "packaging.specifiers",
    ):
        original_line = {
            "referencing.retrieval": b'    retrieval = importlib.import_module("referencing.retrieval")\n',
            "jsonschema.protocols": b'    protocols = importlib.import_module("jsonschema.protocols")\n',
            "packaging.version": b'    version = importlib.import_module("packaging.version")\n',
            "packaging.specifiers": b'    specifiers = importlib.import_module("packaging.specifiers")\n',
        }[import_name]
        variable = original_line.split(b" = ", 1)[0].strip()
        name_literal = import_name.encode("ascii")
        replacement = (
            b'    _profile_checkpoint("import.begin", "'
            + name_literal
            + b'")\n'
            + original_line
            + b'    _profile_checkpoint("import.end", "'
            + name_literal
            + b'")\n'
        )
        exercise = _replace_once(
            exercise,
            original_line,
            replacement,
            f"exercise.py import {variable.decode()}",
        )

    exercise = _replace_once(
        exercise,
        b"    for name in sorted(checker.checkers):\n",
        b"    for name in sorted(checker.checkers):\n"
        b'        _profile_checkpoint("format.begin", name)\n',
        "exercise.py format loop start",
    )
    exercise = _replace_once(
        exercise,
        b"        formats[name] = outcomes\n",
        b"        formats[name] = outcomes\n"
        b'        _profile_checkpoint("format.end", name)\n',
        "exercise.py format loop end",
    )
    exercise = _replace_once(
        exercise,
        b"    specification_resources = []\n",
        b"    specification_resources = []\n"
        b'    _profile_checkpoint("resource.begin", "jsonschema_specifications")\n',
        "exercise.py resource loop start",
    )
    exercise = _replace_once(
        exercise,
        b"        identifier = resource.id()\n",
        b"        identifier = resource.id()\n"
        b'        _profile_checkpoint("resource.item.begin", identifier)\n',
        "exercise.py resource item start",
    )
    exercise = _replace_once(
        exercise,
        b"        specification_resources.append(\n"
        b'            {"id": identifier, "sha256": hashlib.sha256(raw).hexdigest()}\n'
        b"        )\n",
        b"        specification_resources.append(\n"
        b'            {"id": identifier, "sha256": hashlib.sha256(raw).hexdigest()}\n'
        b"        )\n"
        b'        _profile_checkpoint("resource.item.end", identifier)\n',
        "exercise.py resource item end",
    )
    exercise = _replace_once(
        exercise,
        b'    root = resources.files("jsonschema_specifications")\n',
        b'    _profile_checkpoint("resource.end", "jsonschema_specifications")\n'
        b'    root = resources.files("jsonschema_specifications")\n',
        "exercise.py resource loop end",
    )
    exercise = _replace_once(
        exercise,
        b"    for name in PURE:\n        distribution = metadata.distribution(name)\n",
        b"    for name in PURE:\n"
        b'        _profile_checkpoint("record.begin", name)\n'
        b"        distribution = metadata.distribution(name)\n",
        "exercise.py RECORD scan start",
    )
    exercise = _replace_once(
        exercise,
        b'            "files": files,\n        }\n    persistent = rpds.HashTrieMap({"before": 1})\n',
        b'            "files": files,\n        }\n'
        b'        _profile_checkpoint("record.end", name)\n'
        b'    persistent = rpds.HashTrieMap({"before": 1})\n',
        "exercise.py RECORD scan end",
    )

    loader = _replace_once(
        baseline_sources["captured_loader.py"],
        b"        return compile(\n",
        b"        from stage_profile import checkpoint as _profile_checkpoint\n"
        b'        _profile_checkpoint("compile.before", fullname)\n'
        b"        code = compile(\n",
        "captured_loader.py compile start",
    )
    loader = _replace_once(
        loader,
        b"            dont_inherit=True,\n"
        b"        )\n\n"
        b"    def is_package(self, fullname):\n",
        b"            dont_inherit=True,\n"
        b"        )\n"
        b'        _profile_checkpoint("compile.after", fullname)\n'
        b"        return code\n\n"
        b"    def is_package(self, fullname):\n",
        "captured_loader.py compile end",
    )

    wit = _replace_once(
        baseline_sources["probe.wit"],
        b"world ui-schema-probe {\n  export probe;\n}\n",
        b"interface diagnostic-profile {\n"
        b"  checkpoint: func(phase: string, key: string);\n"
        b"}\n\n"
        b"world ui-schema-probe {\n"
        b"  import diagnostic-profile;\n"
        b"  export probe;\n"
        b"}\n",
        "probe.wit diagnostic import",
    )

    derived = {
        "app.py": app,
        "exercise.py": exercise,
        "captured_loader.py": loader,
        "probe.wit": wit,
    }
    diff_records = {}
    for name in sorted(required):
        before = baseline_sources[name]
        after = derived[name]
        diff = "".join(
            difflib.unified_diff(
                before.decode("utf-8").splitlines(keepends=True),
                after.decode("utf-8").splitlines(keepends=True),
                fromfile="baseline/" + name,
                tofile="profile/" + name,
            )
        )
        diff_records[name] = {
            "baseline": {
                "bytes": len(before),
                "sha256": hashlib.sha256(before).hexdigest(),
            },
            "derived": {
                "bytes": len(after),
                "sha256": hashlib.sha256(after).hexdigest(),
            },
            "diff": diff,
            "diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        }
    provenance = {
        "format": "tobkiri.schema-stage-profile-derivation/1",
        "diagnostic_only": True,
        "original_acceptance_claimed": False,
        "production_enabled": False,
        "profile_import": PROFILE_IMPORT_NAME,
        "files": diff_records,
        "helper": {
            "path": "stage_profile.py",
            "bytes": len(helper_source),
            "sha256": hashlib.sha256(helper_source).hexdigest(),
        },
        "fuel_api": dict(FUEL_API_IDENTITY),
        "bounds": {
            "fuel": MAX_FUEL,
            "memory_bytes": 128 * 1024 * 1024,
            "seconds": 30,
            "events": MAX_EVENTS,
            "label_characters": MAX_LABEL_CHARS,
            "metadata_bytes": MAX_METADATA_BYTES,
        },
        "profile_overhead_changes_fuel_and_elapsed_time": True,
    }
    return derived, provenance


def read_remaining_fuel(store_context: object) -> int:
    """Read fuel through the pinned Wasmtime 48 StoreContext C API seam."""
    import ctypes

    from wasmtime import WasmtimeError
    from wasmtime import _ffi as ffi

    remaining = ctypes.c_uint64(0)
    context = store_context._context()
    error = ffi.wasmtime_context_get_fuel(context, ctypes.byref(remaining))
    if error:
        raise WasmtimeError._from_ptr(error)
    value = remaining.value
    if type(value) is not int or not 0 <= value <= MAX_FUEL:
        raise ValueError("Wasmtime 48 returned fuel outside the pinned bound")
    return value


class StageProfileBridge:
    """Record bounded data-only events from one diagnostic component Store."""

    def __init__(self, budget: object) -> None:
        self._budget = budget
        self._events: list[dict[str, object]] = []
        self._metadata_bytes = 0
        self._last_fuel = None
        self._faulted = False
        self._installed = False

    def install(self, linker: object) -> None:
        if self._installed:
            raise RuntimeError("schema profile bridge is already installed")
        with linker.root() as root:
            with root.add_instance(PROFILE_IMPORT_NAME) as interface:
                interface.add_func("checkpoint", self._checkpoint)
        self._installed = True

    def _checkpoint(self, store_context: object, phase: str, key: str) -> None:
        import json

        if not self._installed:
            raise RuntimeError("schema profile bridge is not installed")
        try:
            self._budget.begin()
            if (
                type(phase) is not str
                or type(key) is not str
                or len(phase) > MAX_LABEL_CHARS
                or len(key) > MAX_LABEL_CHARS
            ):
                raise ValueError("schema profile labels exceed their bound")
            if len(self._events) >= MAX_EVENTS:
                raise ValueError("schema profile event limit exceeded")
            remaining = read_remaining_fuel(store_context)
            if self._last_fuel is not None and remaining > self._last_fuel:
                raise ValueError("Wasmtime remaining fuel increased between markers")
            event = {
                "index": len(self._events),
                "phase": phase,
                "key": key,
                "fuel_remaining": remaining,
            }
            encoded = json.dumps(
                event, ensure_ascii=True, separators=(",", ":")
            ).encode("ascii")
            if self._metadata_bytes + len(encoded) > MAX_EVENT_BYTES:
                raise ValueError("schema profile metadata limit exceeded")
            self._events.append(event)
            self._metadata_bytes += len(encoded)
            self._last_fuel = remaining
            self._budget.complete()
        except BaseException:
            self._faulted = True
            raise

    def report(self, execution_status: str) -> dict[str, object]:
        """Return bounded observations without retaining the StoreContext."""
        compile_begin = sum(
            event["phase"] == "compile.before" for event in self._events
        )
        compile_end = sum(event["phase"] == "compile.after" for event in self._events)
        return {
            "format": "tobkiri.schema-stage-profile/1",
            "outcome": "diagnostic_only",
            "execution_status": execution_status,
            "events": self._events,
            "event_count": len(self._events),
            "metadata_bytes": self._metadata_bytes,
            "get_code_call_count": compile_begin,
            "get_code_completed_count": compile_end,
            "shared_callback_budget": self._budget.report(),
            "shared_callback_budget_healthy": self._budget.healthy,
            "fuel_observations_monotonic": all(
                type(event["fuel_remaining"]) is int
                and 0 <= event["fuel_remaining"] <= MAX_FUEL
                and (
                    index == 0
                    or event["fuel_remaining"]
                    <= self._events[index - 1]["fuel_remaining"]
                )
                for index, event in enumerate(self._events)
            ),
            "callback_failed": self._faulted,
            "fuel_api": dict(FUEL_API_IDENTITY),
            "diagnostic_only": True,
            "production_enabled": False,
        }

    def write_report(self, path: object, execution_status: str) -> None:
        """Always persist bounded host events while the observer owns the run."""
        import json

        report = self.report(execution_status)
        encoded = (
            json.dumps(report, ensure_ascii=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
        if len(encoded) > MAX_METADATA_BYTES:
            raise ValueError("complete schema profile exceeds its metadata bound")
        path.write_bytes(encoded)
