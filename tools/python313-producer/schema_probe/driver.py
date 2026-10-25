"""Build the complete schema candidate, then compare actual post-snapshot calls."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
from types import ModuleType

sys.path[:0] = ["/validation-deps", "/", "/probe-source"]
INTERFACE = "tobkiri-ui:schema-probe/probe@1.0.0"
FUEL = 2_000_000_000
SECONDS = 30
OUTPUT_LIMIT = 4 * 1024 * 1024
STDLIB_FILES = {
    "_colorize.py",
    "linecache.py",
    "logging/__init__.py",
    "logging/config.py",
    "logging/handlers.py",
    "locale.py",
    "selectors.py",
    "_strptime.py",
    "calendar.py",
    "signal.py",
    "string.py",
    "subprocess.py",
    "textwrap.py",
    "token.py",
    "tokenize.py",
    "traceback.py",
}


def read(
    path: Path, maximum: int = OUTPUT_LIMIT, *, allow_empty: bool = False
) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("regular schema input required")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if (not raw and not allow_empty) or len(raw) > maximum:
        raise ValueError("schema artifact exceeds its existing bound")
    return raw


def load_exception_diagnostics(path: Path, expected_source: bytes) -> ModuleType:
    """Execute only the exact captured helper source at its supplied path."""
    import importlib.util

    source = read(path)
    if source != expected_source:
        raise ValueError("captured schema diagnostic helper changed")
    spec = importlib.util.spec_from_file_location(
        "_tobkiri_schema_exception_diagnostics", path
    )
    if spec is None or spec.loader is None:
        raise ValueError("captured schema diagnostic helper loader unavailable")
    module = importlib.util.module_from_spec(spec)
    # Compile these exact read-only bytes instead of allowing a cached pyc or
    # adding the helper directory to sys.path in isolated mode.
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def stage_inputs(stage: Path, control: dict) -> dict:
    """Capture exact package/stdlib source and separate extension provenance."""
    files, distributions = {}, {}
    wrapper = None
    declared, members = 0, 0
    diagnostic_mode = control.get("schema_exception_diagnostics", False)
    if type(diagnostic_mode) is not bool:
        raise ValueError("explicit boolean schema diagnostics mode required")
    if diagnostic_mode and not all(
        control.get(name) is True
        for name in (
            "allow_ui_wall_clock",
            "allow_ui_monotonic_clock",
            "allow_inactive_ui_libc",
            "allow_ci_link_trace",
        )
    ):
        raise ValueError("schema diagnostics require the complete inactive candidate")
    stdlib = control["stdlib"]
    if (
        stdlib["format"] != "tobkiri.python313-stdlib-inputs/1"
        or stdlib["outcome"] != "success"
        or set(stdlib["files"]) != STDLIB_FILES
    ):
        raise ValueError("fixed original CPython source selection required")
    for pin in control["inputs"]:
        for name, entry in pin["files"].items():
            if pin["role"] == "native-oracle" and (
                not name.startswith("rpds/") or name.endswith(".so")
            ):
                continue
            members += 1
            declared += entry["bytes"] + len(name.encode("utf-8")) + 128
    for name, entry in stdlib["files"].items():
        members += 1
        declared += entry["bytes"] + len(name.encode("utf-8")) + 128
    if members > 4096 or declared > control["input_limit"]:
        raise ValueError("complete source and resource index exceed the input bound")
    for pin in control["inputs"]:
        native = pin["role"] == "native-oracle"
        root = Path("/inputs/packages") / (
            "rpds-py-native" if native else pin["distribution"]
        )
        for name, entry in pin["files"].items():
            if native and (not name.startswith("rpds/") or name.endswith(".so")):
                continue
            raw = read(root / name, allow_empty=True)
            if (
                len(raw) != entry["bytes"]
                or hashlib.sha256(raw).hexdigest() != entry["sha256"]
            ):
                raise ValueError("captured wheel member changed")
            if name in files:
                raise ValueError("schema wheel namespace collision")
            files[name] = raw
            if not native and name.endswith(".dist-info/METADATA"):
                if pin["distribution"] in distributions:
                    raise ValueError("duplicate distribution metadata")
                distributions[pin["distribution"]] = name.split("/")[0]
            if native and name == "rpds/__init__.py":
                wrapper = {
                    "wheel_sha256": pin["sha256"],
                    "member": name,
                    "sha256": entry["sha256"],
                    "bytes": len(raw),
                }
    if wrapper is None or len(distributions) != 5:
        raise ValueError("complete original schema packages and rpds wrapper required")
    for name, entry in stdlib["files"].items():
        raw = read(Path("/stdlib/source") / name, allow_empty=True)
        if (
            len(raw) != entry["bytes"]
            or hashlib.sha256(raw).hexdigest() != entry["sha256"]
        ):
            raise ValueError("captured original CPython source changed")
        if name in files:
            raise ValueError("stdlib/package source namespace collision")
        files[name] = raw
    for name, raw in files.items():
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    extension = read(Path("/rpds.abi3.so"))
    if hashlib.sha256(extension).hexdigest() != control["extension_sha256"]:
        raise ValueError("actual rpds extension changed")
    (stage / "rpds/rpds.abi3.so").write_bytes(extension)
    probe_files = ["app.py", "exercise.py", "captured_loader.py", "probe.wit"]
    if diagnostic_mode:
        probe_files.append("exception_diagnostics.py")
    baseline_app = None
    helper_source = None
    for name in probe_files:
        target = stage / name
        if target.exists():
            raise ValueError("package collides with probe source")
        raw = read(Path("/probe-source") / name)
        target.write_bytes(raw)
        if name == "app.py":
            baseline_app = raw
        elif name == "exception_diagnostics.py":
            helper_source = raw
    diagnostic_provenance = None
    if diagnostic_mode:
        if baseline_app is None or helper_source is None:
            raise ValueError("frozen baseline app and diagnostic helper required")
        helper = load_exception_diagnostics(
            Path("/probe-source/exception_diagnostics.py"), helper_source
        )
        derived_app, diagnostic_provenance = helper.derive_diagnostic_app(
            baseline_app, helper_source
        )
        if read(stage / "app.py") != baseline_app:
            raise ValueError("baseline app changed before diagnostic derivation")
        (stage / "app.py").write_bytes(derived_app)
    data_parts = [
        "# Authenticated original wheel and CPython source members.\nFILES = {\n"
    ]
    size = sum(len(raw) for raw in files.values()) + len(extension)
    for name, raw in files.items():
        literal = repr(name) + ": " + repr(raw) + ",\n"
        size += len(literal.encode())
        if size > control["input_limit"]:
            raise ValueError(
                "captured source plus literal copies exceed the input bound"
            )
        data_parts.append(literal)
    data_parts.append("}\nDISTRIBUTIONS = " + repr(distributions) + "\n")
    data = "".join(data_parts)
    generated = stage / "wheel_data.py"
    if generated.exists():
        raise ValueError("package collides with fixed resource capture")
    generated.write_text(data)
    inventory = {}
    for path in stage.rglob("*"):
        if path.is_file():
            raw = read(path, control["input_limit"], allow_empty=True)
            inventory[path.relative_to(stage).as_posix()] = {
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
    total = sum(row["bytes"] for row in inventory.values())
    if total > control["input_limit"]:
        raise ValueError("complete code/resource/metadata capture exceeds 64 MiB")
    result = {
        "files": inventory,
        "input_bytes": total,
        "wrapper": wrapper,
        "distribution_directories": distributions,
        "captured_files": len(files),
        "stdlib_source": {
            "source_pin": stdlib["source_pin"],
            "archive": stdlib["archive"],
            "files": stdlib["files"],
        },
    }
    if diagnostic_provenance is not None:
        result["schema_exception_diagnostics"] = diagnostic_provenance
    return result


def observe_link_trace(root: Path, expected: bytes) -> dict:
    """Capture bounded Linker inputs and final component as separate stages."""
    root_info = root.lstat()
    if (
        not root.is_absolute()
        or root.resolve() != root
        or not stat.S_ISDIR(root_info.st_mode)
        or stat.S_IMODE(root_info.st_mode) != 0o700
    ):
        raise ValueError("fresh physical schema Linker trace directory required")
    core_header = b"\0asm\x01\0\0\0"
    component_header = b"\0asm\x0d\0\x01\0"

    def identity(path: Path, maximum: int) -> dict:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= maximum
            or stat.S_IMODE(before.st_mode) != 0o600
        ):
            raise ValueError("regular private Linker trace member required")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            total = 0
            while block := stream.read(65536):
                total += len(block)
                if total > maximum:
                    raise ValueError("schema Linker trace member exceeds its bound")
                digest.update(block)
        after = path.lstat()
        fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns")
        if total != before.st_size or any(
            getattr(before, field) != getattr(after, field) for field in fields
        ):
            raise ValueError("schema Linker trace member changed while read")
        return {"bytes": total, "sha256": digest.hexdigest()}

    def capture(path: Path, maximum: int) -> tuple[bytes, dict]:
        observed = identity(path, maximum)
        raw = read(path, maximum)
        if (
            len(raw) != observed["bytes"]
            or hashlib.sha256(raw).hexdigest() != observed["sha256"]
        ):
            raise ValueError("schema Linker trace control changed while decoded")
        return raw, observed

    manifest_path = root / "libraries.tsv"
    manifest_raw, manifest_identity = capture(manifest_path, 2 * 1024 * 1024)
    rows = manifest_raw.split(b"\n")[:-1]
    if (
        not rows
        or not manifest_raw.endswith(b"\n")
        or rows[0] != b"tobkiri-ci-link-inputs/1"
        or not 1 <= len(rows) - 1 <= 128
    ):
        raise ValueError("actual schema Linker manifest format or count differs")
    libraries = []
    module_names = set()
    module_bytes = 0
    files = {"libraries.tsv": manifest_identity}
    canonical_manifest = [b"tobkiri-ci-link-inputs/1\n"]
    for ordinal, row in enumerate(rows[1:]):
        fields = row.split(b"\t")
        if len(fields) != 4:
            raise ValueError("actual schema Linker manifest row is malformed")
        ordinal_text, name_hex, dl_openable, size_text = fields
        if (
            ordinal_text != str(ordinal).encode("ascii")
            or not 2 <= len(name_hex) <= 8192
            or len(name_hex) % 2
            or any(char not in b"0123456789abcdef" for char in name_hex)
            or dl_openable not in {b"true", b"false"}
            or not size_text.isdigit()
            or size_text.startswith(b"0")
            or len(size_text) > 8
        ):
            raise ValueError("actual schema Linker manifest identity differs")
        try:
            name = bytes.fromhex(name_hex.decode("ascii")).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("actual schema Linker name encoding differs") from error
        if not name or len(name.encode("utf-8")) > 4096:
            raise ValueError("actual schema Linker name exceeds its bound")
        if name.encode("utf-8").hex().encode("ascii") != name_hex:
            raise ValueError("actual schema Linker name encoding is not canonical")
        declared_bytes = int(size_text)
        if declared_bytes > 32 * 1024 * 1024:
            raise ValueError("actual schema Linker module exceeds its bound")
        canonical_manifest.append(
            b"\t".join(
                (
                    str(ordinal).encode("ascii"),
                    name_hex,
                    dl_openable,
                    str(declared_bytes).encode("ascii"),
                )
            )
            + b"\n"
        )
        module_name = f"{ordinal:04}.wasm"
        module_names.add(module_name)
        module_path = root / module_name
        module = identity(module_path, 32 * 1024 * 1024)
        if declared_bytes != module["bytes"]:
            raise ValueError("actual schema Linker module size differs")
        with module_path.open("rb") as stream:
            if stream.read(8) != core_header:
                raise ValueError("schema Linker input is not a core Wasm module")
        module_bytes += declared_bytes
        files[module_name] = module
        libraries.append(
            {
                "ordinal": ordinal,
                "name": name,
                "dl_openable": dl_openable == b"true",
                **module,
            }
        )
    if b"".join(canonical_manifest) != manifest_raw:
        raise ValueError("actual schema Linker manifest is not canonical")
    preinit_path = root / "preinit-linked-component.wasm"
    preinit = identity(preinit_path, 32 * 1024 * 1024)
    with preinit_path.open("rb") as stream:
        if stream.read(8) != component_header:
            raise ValueError("pre-snapshot Linker output is not a Wasm component")
    final_component = {
        "bytes": len(expected),
        "sha256": hashlib.sha256(expected).hexdigest(),
    }
    if expected[:8] != component_header:
        raise ValueError("final schema CLI output is not a Wasm component")
    complete_path = root / "complete.tsv"
    complete_raw, complete_identity = capture(complete_path, 4096)
    expected_complete = (
        f"linked-before-snapshot\t{len(libraries)}\t{preinit['bytes']}\n".encode(
            "ascii"
        )
    )
    if complete_raw != expected_complete:
        raise ValueError("Linker trace completion record differs")
    files["preinit-linked-component.wasm"] = preinit
    files["complete.tsv"] = complete_identity
    actual_files = set()
    for path in root.iterdir():
        if not stat.S_ISREG(path.lstat().st_mode) or path.resolve() != path:
            raise ValueError("schema Linker trace contains a nonregular member")
        actual_files.add(path.name)
    expected_files = {
        "libraries.tsv",
        "preinit-linked-component.wasm",
        "complete.tsv",
        *module_names,
    }
    if actual_files != expected_files:
        raise ValueError("schema Linker trace contains missing or extra members")
    total_bytes = manifest_identity["bytes"] + module_bytes
    total_bytes += preinit["bytes"] + complete_identity["bytes"]
    if total_bytes > 64 * 1024 * 1024:
        raise ValueError("complete schema Linker trace exceeds its existing bound")
    return {
        "format": "tobkiri.ci-link-input-observation/1",
        "files": files,
        "libraries": libraries,
        "preinit_component": preinit,
        "final_component": final_component,
        "stage": "pre-snapshot Linker output and final CLI component recorded separately",
        "total_bytes": total_bytes,
        "actual_input_observation": True,
        "final_provider_resolution_verified": False,
        "production_enabled": False,
    }


def boundary(
    component,
    engine,
    *,
    inner: bool,
    allow_clock: bool = False,
    allow_monotonic: bool = False,
):
    imports = component.type.imports(engine)
    exports = list(component.type.exports(engine))
    if allow_monotonic and not allow_clock:
        raise ValueError("monotonic candidate requires explicit wall-clock mode")
    if allow_clock:
        from ui_clock import validate_clock_import

        if allow_monotonic:
            from ui_monotonic import (
                IMPORT_NAME,
                validate_clock_import as validate_monotonic,
            )

            if len(imports) != 2 or IMPORT_NAME not in imports:
                raise ValueError(
                    "UI candidate must import only the two explicit clock interfaces"
                )
            validate_monotonic(IMPORT_NAME, imports[IMPORT_NAME], engine)
            wall_imports = {
                name: item for name, item in imports.items() if name != IMPORT_NAME
            }
        else:
            wall_imports = imports
        if len(wall_imports) != 1:
            raise ValueError("UI candidate must import only one wall-clock interface")
        for name, item in wall_imports.items():
            validate_clock_import(name, item, engine)
    elif imports:
        raise ValueError("schema component has unexpected authority imports")
    expected = {INTERFACE, "exports"} if inner else {INTERFACE}
    if set(exports) != expected or len(exports) != len(expected):
        raise ValueError("schema component has unexpected authority imports or exports")
    return {"imports": sorted(imports), "exports": sorted(exports)}


def observe(
    path: Path,
    *,
    allow_clock: bool = False,
    allow_monotonic: bool = False,
    operation_name: str = "run",
    arguments: tuple = (),
    expected_denial: bool = False,
) -> dict:
    """Invoke the real component with unchanged Store/fuel/deadline limits."""
    from wasmtime import Config, Engine, Store, WasmtimeError
    from wasmtime.component import Component, Linker

    config = Config()
    config.parallel_compilation = False
    config.consume_fuel = True
    config.epoch_interruption = True
    with (
        Engine(config) as engine,
        Component(engine, read(path, 32 * 1024 * 1024)) as component,
    ):
        typed = boundary(
            component,
            engine,
            inner=False,
            allow_clock=allow_clock,
            allow_monotonic=allow_monotonic,
        )
        with Store(engine) as store, Linker(engine) as linker:
            store.set_limits(
                memory_size=128 * 1024 * 1024,
                table_elements=65536,
                instances=64,
                tables=64,
                memories=64,
            )
            store.set_fuel(FUEL)
            store.set_epoch_deadline(1)
            finished = threading.Event()
            clock_errors = []

            def expire():
                if not finished.is_set():
                    try:
                        engine.increment_epoch()
                    except BaseException as error:
                        clock_errors.append(type(error).__name__)

            timer = threading.Timer(SECONDS, expire)
            timer.daemon = True
            start = time.monotonic()
            timer.start()
            post_return = False
            bridge = None
            monotonic_bridge = None
            budget = None
            denial = None
            try:
                if allow_clock:
                    from ui_clock import ClockBudget, WallClockBridge

                    budget = ClockBudget(start + SECONDS)
                    wall_import = next(
                        name
                        for name in typed["imports"]
                        if name.startswith("wasi:clocks/wall-clock@")
                    )
                    bridge = WallClockBridge(
                        wall_import, start + SECONDS, budget=budget
                    )
                    bridge.install(linker)
                    if allow_monotonic:
                        from ui_monotonic import IMPORT_NAME, MonotonicClockBridge

                        monotonic_bridge = MonotonicClockBridge(IMPORT_NAME, budget)
                        monotonic_bridge.install(linker)
                instance = linker.instantiate(store, component)
                index = instance.get_export_index(store, INTERFACE)
                if index is None:
                    raise ValueError("schema interface missing")
                operation = instance.get_export_index(store, operation_name, index)
                if operation is None:
                    raise ValueError("schema operation missing")
                function = instance.get_func(store, operation)
                if function is None:
                    raise ValueError("schema operation is not a function")
                try:
                    value = function(store, *arguments)
                except WasmtimeError as error:
                    if (
                        not expected_denial
                        or "wasm `unreachable` instruction executed" not in str(error)
                    ):
                        raise
                    denial = {
                        "kind": arguments[0],
                        "trap": "wasm unreachable",
                        "store_retired": True,
                        "wasm_stack": str(error)[:4096],
                    }
                    result = None
                else:
                    try:
                        if expected_denial:
                            raise ValueError("non-clock host IO did not trap")
                        if type(value) is not str or len(value.encode()) > OUTPUT_LIMIT:
                            raise ValueError(
                                "schema observation exceeds original JSON boundary"
                            )
                        result = json.loads(value)
                    finally:
                        function.post_return(store)
                        post_return = True
            finally:
                finished.set()
                timer.cancel()
                timer.join()
            if (
                clock_errors
                or time.monotonic() - start >= SECONDS
                or store.get_fuel() <= 0
                or (budget is not None and not budget.healthy)
                or (not post_return and denial is None)
            ):
                raise ValueError(
                    "schema operation did not finish its original bounded lifetime"
                )
            return {
                "result": result,
                "fuel_consumed": FUEL - store.get_fuel(),
                "post_return": post_return,
                "denial": denial,
                "clock": bridge.report() if bridge is not None else None,
                "monotonic_clock": (
                    monotonic_bridge.report() if monotonic_bridge is not None else None
                ),
                "clock_budget": budget.report() if budget is not None else None,
                "clock_callbacks_completed": budget.healthy
                if budget is not None
                else True,
                **typed,
            }


def clock_composition_wit(
    stage: Path,
    import_name: str,
    *,
    allow_monotonic: bool = False,
) -> Path:
    """Declare the verified clock import for WAC without changing guest sources."""
    root = stage / "composition-wit"
    dependency = root / "deps/wasi-clocks"
    dependency.mkdir(parents=True)
    original = read(stage / "probe.wit").decode()
    anchor = "world ui-schema-probe {\n"
    if original.count(anchor) != 1:
        raise ValueError("original schema world changed before clock composition")
    version = import_name.rsplit("@", 1)[1]
    declarations = "  import " + import_name + ";\n"
    if allow_monotonic:
        from ui_monotonic import IMPORT_NAME

        declarations += "  import " + IMPORT_NAME + ";\n"
        monotonic = root / "deps/tobkiri-ui-clock"
        monotonic.mkdir()
        (monotonic / "monotonic.wit").write_text(
            "package tobkiri-ui:clock@1.0.0;\ninterface monotonic {\n"
            "  now: func() -> u64;\n  resolution: func() -> u64;\n}\n"
        )
    (root / "probe.wit").write_text(original.replace(anchor, anchor + declarations, 1))
    (dependency / "wall-clock.wit").write_text(
        "package wasi:clocks@" + version + ";\ninterface wall-clock {\n"
        "  record datetime { seconds: u64, nanoseconds: u32 }\n"
        "  now: func() -> datetime;\n  resolution: func() -> datetime;\n}\n"
    )
    return root


def main():
    from composition import compose
    from wasmtime import Config, Engine
    from wasmtime.component import Component

    work = Path("/work")
    control = json.loads(read(Path("/control.json")))
    allow_clock = control.get("allow_ui_wall_clock", False)
    allow_monotonic = control.get("allow_ui_monotonic_clock", False)
    allow_link_trace = control.get("allow_ci_link_trace", False)
    diagnostic_mode = control.get("schema_exception_diagnostics", False)
    if any(
        type(value) is not bool
        for value in (
            allow_clock,
            allow_monotonic,
            allow_link_trace,
            diagnostic_mode,
        )
    ):
        raise ValueError("explicit boolean schema probe mode required")
    if allow_monotonic and not allow_clock:
        raise ValueError("monotonic candidate requires explicit wall-clock mode")
    if allow_link_trace and not (allow_clock and allow_monotonic):
        raise ValueError("Linker observation requires the explicit two-clock candidate")
    if diagnostic_mode and not all(
        control.get(name) is True
        for name in (
            "allow_ui_wall_clock",
            "allow_ui_monotonic_clock",
            "allow_inactive_ui_libc",
            "allow_ci_link_trace",
        )
    ):
        raise ValueError("schema diagnostics require the complete inactive candidate")
    original = json.loads(read(work / "native.json"))
    stage = work / "stage"
    stage.mkdir()
    captured = stage_inputs(stage, control)
    diagnostic_provenance = captured.get("schema_exception_diagnostics")
    if diagnostic_provenance is not None:
        (work / "schema-exception-diagnostics.json").write_text(
            json.dumps(diagnostic_provenance, indent=2) + "\n"
        )
    (work / "stage-inputs.json").write_text(json.dumps(captured, indent=2) + "\n")
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/work/home",
        "TMPDIR": "/work/tmp",
        "VIRTUAL_ENV": "/empty-venv",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "RAYON_NUM_THREADS": "1",
        "TOKIO_WORKER_THREADS": "1",
    }
    command = ["/producer", "-d", "probe.wit", "-w", "ui-schema-probe"]
    trace_root = work / "link-trace"
    if allow_link_trace and (trace_root.exists() or trace_root.is_symlink()):
        raise ValueError("each schema componentization requires a fresh private trace")
    subprocess.run(
        [*command, "bindings", "."], cwd=stage, env=environment, check=True, timeout=180
    )
    subprocess.run(
        [*command, "componentize", "--stub-wasi", "app", "-o", "component.wasm"],
        cwd=stage,
        env=environment,
        check=True,
        timeout=600,
    )
    config = Config()
    config.parallel_compilation = False
    inner = read(stage / "component.wasm", 32 * 1024 * 1024)
    link_trace = observe_link_trace(trace_root, inner) if allow_link_trace else None
    with Engine(config) as engine, Component(engine, inner) as component:
        inner_typed = boundary(
            component,
            engine,
            inner=True,
            allow_clock=allow_clock,
            allow_monotonic=allow_monotonic,
        )
    wit = (
        clock_composition_wit(
            stage,
            next(
                name
                for name in inner_typed["imports"]
                if name.startswith("wasi:clocks/wall-clock@")
            ),
            allow_monotonic=allow_monotonic,
        )
        if allow_clock
        else stage
    )
    generated_wit = (
        {
            path.relative_to(stage).as_posix(): hashlib.sha256(read(path)).hexdigest()
            for path in wit.rglob("*.wit")
        }
        if allow_clock
        else {}
    )
    if (
        sum(
            path.stat().st_size
            for path in stage.rglob("*")
            if path.is_file() and path.name != "component.wasm"
        )
        > control["input_limit"]
    ):
        raise ValueError("complete generated clock/source capture exceeds 64 MiB")
    composition = compose(
        Path("/wac"),
        stage / "component.wasm",
        stage,
        wit,
        "ui-schema-probe",
        [INTERFACE],
        environment,
    )
    final_source_bytes = sum(
        path.stat().st_size
        for path in stage.rglob("*")
        if path.is_file() and path.name not in {"component.wasm", "outer.wasm"}
    )
    if final_source_bytes > control["input_limit"]:
        raise ValueError("final source and composition copies exceed 64 MiB")
    if read(stage / "component.wasm", 32 * 1024 * 1024) != inner:
        raise ValueError("schema inner changed during composition")
    clock_probes = None
    if allow_clock:
        start_wall = time.time_ns()
        clock_probe = observe(
            stage / "outer.wasm",
            allow_clock=True,
            allow_monotonic=allow_monotonic,
            operation_name="probe-clock",
        )
        end_wall = time.time_ns()
        result = clock_probe["result"]
        precision_ns = clock_probe["clock"]["resolution_nanoseconds_ceiling"]
        if (
            type(result.get("unix_time_ns")) is not int
            or not start_wall <= result["unix_time_ns"] <= end_wall
            or clock_probe["clock"]["calls"]["now"] <= 0
            or clock_probe["clock"]["calls"]["resolution"] <= 0
            or precision_ns is None
            or result.get("resolution") != precision_ns / 1_000_000_000
        ):
            raise ValueError("real wall-clock epoch, units or resolution did not match")
        clock_probes = {
            "clock": clock_probe,
            "host_window_ns": [start_wall, end_wall],
            "non_clock_denials": [],
            "production_ui_clock_connected": False,
        }
        try:
            if allow_monotonic:
                start_monotonic = time.monotonic_ns()
                probe = observe(
                    stage / "outer.wasm",
                    allow_clock=True,
                    allow_monotonic=True,
                    operation_name="probe-monotonic",
                )
                end_monotonic = time.monotonic_ns()
                clock_probes["monotonic"] = probe
                clock_probes["monotonic_host_window_ns"] = [
                    start_monotonic,
                    end_monotonic,
                ]
                result = probe["result"]
                precision = probe["monotonic_clock"]["resolution_nanoseconds_ceiling"]
                if (
                    type(result.get("before_ns")) is not int
                    or type(result.get("after_ns")) is not int
                    or not start_monotonic
                    <= result["before_ns"]
                    <= result["after_ns"]
                    <= end_monotonic
                    or precision is None
                    or result.get("resolution") != precision / 1_000_000_000
                    or result.get("monotonic") is not True
                    or result.get("adjustable") is not False
                    or probe["monotonic_clock"]["calls"]["now"] < 2
                    or probe["monotonic_clock"]["calls"]["resolution"] <= 0
                ):
                    raise ValueError(
                        "real monotonic units, ordering or resolution did not match"
                    )
            kinds = (
                ("filesystem", "random", "subscribe", "poll")
                if allow_monotonic
                else ("filesystem", "random")
            )
            for kind in kinds:
                denial = observe(
                    stage / "outer.wasm",
                    allow_clock=True,
                    allow_monotonic=allow_monotonic,
                    operation_name="probe-denied-io",
                    arguments=(kind,),
                    expected_denial=True,
                )
                clock_probes["non_clock_denials"].append(denial)
                if kind in {"subscribe", "poll"} and not any(
                    name in denial["denial"]["wasm_stack"]
                    for name in ("subscribe", "poll", "clock_nanosleep")
                ):
                    raise ValueError(
                        "subscription/poll denial did not identify its actual WASI path"
                    )
        finally:
            (work / "ui-clock-probes.json").write_text(
                json.dumps(clock_probes, indent=2) + "\n"
            )
    actual = observe(
        stage / "outer.wasm",
        allow_clock=allow_clock,
        allow_monotonic=allow_monotonic,
    )
    (work / "wasm-observation.json").write_text(json.dumps(actual, indent=2) + "\n")
    result = actual["result"]
    if allow_clock and actual["clock"]["calls"]["now"] <= 0:
        raise ValueError(
            "post-snapshot original logging did not reach the real wall clock"
        )
    if allow_monotonic and actual["monotonic_clock"]["calls"]["now"] <= 0:
        raise ValueError(
            "post-snapshot original selector did not reach the real monotonic clock"
        )
    if "exception" in result:
        raise ValueError(
            "original schema operation failed: "
            + json.dumps(result["exception"])[:3000]
        )
    if diagnostic_mode:
        raise ValueError(
            "diagnostic-only schema run requires the original candidate failure"
        )
    target, io = result["target"], result["runtime_io"]
    if (
        target["python"] != [3, 13, 16]
        or target["pointer_bits"] != 32
        or "wasm32" not in target["soabi"]
        or "313" not in target["soabi"]
        or not target["extension_file"].endswith("rpds/rpds.abi3.so")
    ):
        raise ValueError("actual schema target/extension identity differs")
    # Preserve original import state: jsonschema.protocols is already eager in
    # the real native wheel, while the other two paths are first-use imports.
    required_lazy = {
        name
        for name, loaded in original["observation"]["lazy_before"].items()
        if loaded is False
    }
    if (
        result["observation"] != original["observation"]
        or not {"referencing.retrieval", "packaging.version"} <= required_lazy
        or not required_lazy <= set(io["lazy_modules"])
        or io["resource_reads"] <= 0
        or io["metadata_queries"] <= 0
        or result["observation"]["optional_helpers_imported"]
    ):
        raise ValueError("original schema semantics or first late IO did not match")
    for name, entry in captured["files"].items():
        raw = read(stage / name, control["input_limit"], allow_empty=True)
        if (
            len(raw) != entry["bytes"]
            or hashlib.sha256(raw).hexdigest() != entry["sha256"]
        ):
            raise ValueError("original package or captured resource data changed")
    if any(
        hashlib.sha256(read(stage / name)).hexdigest() != expected
        for name, expected in generated_wit.items()
    ):
        raise ValueError("clock composition WIT changed during execution")
    report = {
        "format": "tobkiri.ui-schema-probe/1",
        "outcome": "success",
        "passed": True,
        "native_python": original["python"],
        "observation": actual,
        "input_inventory": captured,
        "inner": inner_typed,
        "composition": composition,
        "artifact_sha256": hashlib.sha256(
            read(stage / "outer.wasm", 32 * 1024 * 1024)
        ).hexdigest(),
        "native_capture_binding": False,
        "full_setup_gui": False,
        "production_enabled": False,
        "clock_probes": clock_probes,
        "generated_clock_wit": generated_wit,
        "final_source_bytes": final_source_bytes,
    }
    if link_trace is not None:
        report.update(
            link_input_trace=link_trace,
            trace_observed=True,
            final_provider_resolution_verified=False,
        )
    (work / "schema.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
