"""Build the complete schema candidate, then compare actual post-snapshot calls."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

sys.path[:0] = ["/validation-deps", "/"]
INTERFACE = "tobkiri-ui:schema-probe/probe@1.0.0"
FUEL = 2_000_000_000
SECONDS = 30
OUTPUT_LIMIT = 4 * 1024 * 1024


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


def stage_inputs(stage: Path, control: dict) -> dict:
    """Copy exact wheel members and record separate target/wrapper provenance."""
    files, distributions = {}, {}
    wrapper = None
    declared, members = 0, 0
    for pin in control["inputs"]:
        for name, entry in pin["files"].items():
            if pin["role"] == "native-oracle" and (
                not name.startswith("rpds/") or name.endswith(".so")
            ):
                continue
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
    for name, raw in files.items():
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    extension = read(Path("/rpds.abi3.so"))
    if hashlib.sha256(extension).hexdigest() != control["extension_sha256"]:
        raise ValueError("actual rpds extension changed")
    (stage / "rpds/rpds.abi3.so").write_bytes(extension)
    for name in ("app.py", "exercise.py", "captured_loader.py", "probe.wit"):
        target = stage / name
        if target.exists():
            raise ValueError("package collides with probe source")
        target.write_bytes(read(Path("/probe-source") / name))
    data_parts = [
        "# Derived only from authenticated original wheel members.\nFILES = {\n"
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
    return {
        "files": inventory,
        "input_bytes": total,
        "wrapper": wrapper,
        "distribution_directories": distributions,
        "captured_files": len(files),
    }


def boundary(component, engine, *, inner: bool):
    imports, exports = (
        list(component.type.imports(engine)),
        list(component.type.exports(engine)),
    )
    expected = {INTERFACE, "exports"} if inner else {INTERFACE}
    if imports or set(exports) != expected or len(exports) != len(expected):
        raise ValueError("schema component has unexpected authority imports or exports")
    return {"imports": imports, "exports": sorted(exports)}


def observe(path: Path) -> dict:
    """Invoke the real component with unchanged Store/fuel/deadline limits."""
    from wasmtime import Config, Engine, Store
    from wasmtime.component import Component, Linker

    config = Config()
    config.parallel_compilation = False
    config.consume_fuel = True
    config.epoch_interruption = True
    with (
        Engine(config) as engine,
        Component(engine, read(path, 32 * 1024 * 1024)) as component,
    ):
        typed = boundary(component, engine, inner=False)
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
            try:
                instance = linker.instantiate(store, component)
                index = instance.get_export_index(store, INTERFACE)
                if index is None:
                    raise ValueError("schema interface missing")
                operation = instance.get_export_index(store, "run", index)
                if operation is None:
                    raise ValueError("schema operation missing")
                function = instance.get_func(store, operation)
                if function is None:
                    raise ValueError("schema operation is not a function")
                value = function(store)
                try:
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
            if clock_errors or time.monotonic() - start >= SECONDS or not post_return:
                raise ValueError(
                    "schema operation did not finish its original bounded lifetime"
                )
            return {
                "result": result,
                "fuel_consumed": FUEL - store.get_fuel(),
                "post_return": post_return,
                **typed,
            }


def main():
    from composition import compose
    from wasmtime import Config, Engine
    from wasmtime.component import Component

    work = Path("/work")
    control = json.loads(read(Path("/control.json")))
    original = json.loads(read(work / "native.json"))
    stage = work / "stage"
    stage.mkdir()
    captured = stage_inputs(stage, control)
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
    with Engine(config) as engine, Component(engine, inner) as component:
        inner_typed = boundary(component, engine, inner=True)
    composition = compose(
        Path("/wac"),
        stage / "component.wasm",
        stage,
        stage,
        "ui-schema-probe",
        [INTERFACE],
        environment,
    )
    if read(stage / "component.wasm", 32 * 1024 * 1024) != inner:
        raise ValueError("schema inner changed during composition")
    actual = observe(stage / "outer.wasm")
    (work / "wasm-observation.json").write_text(json.dumps(actual, indent=2) + "\n")
    result = actual["result"]
    target, io = result["target"], result["runtime_io"]
    if (
        target["python"] != [3, 13, 16]
        or target["pointer_bits"] != 32
        or "wasm32" not in target["soabi"]
        or "313" not in target["soabi"]
        or not target["extension_file"].endswith("rpds/rpds.abi3.so")
    ):
        raise ValueError("actual schema target/extension identity differs")
    required_lazy = {
        "referencing.retrieval",
        "jsonschema.protocols",
        "packaging.version",
    }
    if (
        result["observation"] != original["observation"]
        or any(result["observation"]["lazy_before"].values())
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
    }
    (work / "schema.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
