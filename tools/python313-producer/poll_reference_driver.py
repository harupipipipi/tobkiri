"""Build and call one genuine-WASIp2 C oracle inside the separate CI jail."""

from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

INTERFACE = "tobkiri-ui:libc-poll-oracle/probe@1.0.0"
FUEL = 2_000_000_000
SECONDS = 30


def require_captured_stage(stage: Path, control: dict) -> None:
    """Rebind captured source/helper after each producer invocation."""
    from poll_link_trace import FILE_LIMIT, HELPER_NAME, fingerprint

    for name in ("app.py", "probe.wit"):
        if fingerprint(stage / name, FILE_LIMIT) != control["probe_inputs"][name]:
            raise ValueError("captured probe source changed during production")
    if fingerprint(stage / HELPER_NAME, FILE_LIMIT) != control["helper"]:
        raise ValueError("captured helper changed during production")


def observe(component_path: Path) -> dict:
    """Use genuine WASIp2 in this oracle only, with a fresh bounded Store."""
    from poll_link_trace import FILE_LIMIT, fingerprint
    from poll_reference_results import decode
    from wasmtime import Config, Engine, Store, WasiConfig
    from wasmtime.component import Component, Linker

    if importlib.metadata.version("wasmtime") != "48.0.0":
        raise ValueError("the exact existing Wasmtime48 validation wheel is required")
    identity = fingerprint(component_path, FILE_LIMIT)
    data = component_path.read_bytes()
    config = Config()
    config.parallel_compilation = False
    config.consume_fuel = True
    config.epoch_interruption = True
    with Engine(config) as engine, Component(engine, data) as component:
        imports = list(component.type.imports(engine))
        exports = list(component.type.exports(engine))
        allowed = {
            "wasi:cli",
            "wasi:clocks",
            "wasi:filesystem",
            "wasi:io",
            "wasi:random",
            "wasi:sockets",
        }
        if (
            not imports
            or any(name.partition("/")[0] not in allowed for name in imports)
            or len(exports) != 2
            or set(exports) != {INTERFACE, "exports"}
        ):
            raise ValueError(
                "nonstub reference imports or public/private exports differ"
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
            # Do not inherit stdio, argv, environment, preopens or networking.
            # No HTTP linker and no unknown-import trap/fake provider is used.
            store.set_wasi(WasiConfig())
            linker.add_wasip2()
            finished = threading.Event()
            timer_errors = []

            def expire() -> None:
                if not finished.is_set():
                    try:
                        engine.increment_epoch()
                    except BaseException as error:
                        timer_errors.append(type(error).__name__)

            timer = threading.Timer(SECONDS, expire)
            timer.daemon = True
            started = time.monotonic()
            returned, post_return = False, False
            timer.start()
            try:
                instance = linker.instantiate(store, component)
                interface = instance.get_export_index(store, INTERFACE)
                if interface is None:
                    raise ValueError("public C probe interface missing")
                index = instance.get_export_index(store, "run", interface)
                if index is None:
                    raise ValueError("public C probe operation missing")
                function = instance.get_func(store, index)
                if function is None:
                    raise ValueError("public C probe operation is not a function")
                raw = function(store)
                returned = True
                try:
                    result = decode(raw)
                finally:
                    function.post_return(store)
                    post_return = True
                fuel = FUEL - store.get_fuel()
            finally:
                finished.set()
                timer.cancel()
                timer.join()
            elapsed = time.monotonic() - started
            if timer_errors or elapsed >= SECONDS or not returned or not post_return:
                raise ValueError("reference exceeded its original bounded lifetime")
        # Store and Linker are physically closed before accepting this result.
        if fingerprint(component_path, FILE_LIMIT) != identity:
            raise ValueError("reference component changed during its invocation")
        return {
            "component": identity,
            "imports": imports,
            "exports": sorted(exports),
            "genuine_wasip2": True,
            "inherited_arguments_environment_stdio_preopens_network": False,
            "private_initializer_called": False,
            "returned": returned,
            "post_return": post_return,
            "store_retired": True,
            "epoch_timer_joined": True,
            "fuel_consumed": fuel,
            "elapsed_seconds": elapsed,
            "result": result,
        }


def run() -> None:
    """Keep one variant's preparation, actual trace and invocation independent."""
    from poll_link_trace import FILE_LIMIT, HELPER_NAME, fingerprint, read_trace

    if sys.platform != "linux":
        raise ValueError("the C reference driver is restricted to its Linux CI jail")
    work = Path("/work")
    control_path = Path("/control.json")
    fingerprint(control_path, 4 * 1024 * 1024, private=False)
    control = json.loads(control_path.read_bytes())
    if set(control) != {"variant", "helper", "producer", "probe_inputs"} or control[
        "variant"
    ] not in ("original", "candidate"):
        raise ValueError("explicit original/candidate reference control required")
    producer = Path("/producer")
    helper = Path("/helper.abi3.so")
    if (
        fingerprint(producer, 128 * 1024 * 1024, private=False) != control["producer"]
        or fingerprint(helper, FILE_LIMIT, private=False) != control["helper"]
        or set(control["probe_inputs"])
        != {"app.py", "probe.wit", "_tobkiri_poll_oracle.c"}
    ):
        raise ValueError("actual restored CLI/helper/input source identities differ")
    source = Path("/probe-source")
    for name, identity in control["probe_inputs"].items():
        if fingerprint(source / name, 4 * 1024 * 1024, private=False) != identity:
            raise ValueError("original C or wrapper/WIT source changed")
    stage = work / "stage"
    stage.mkdir(mode=0o700)
    for name in ("app.py", "probe.wit"):
        shutil.copyfile(source / name, stage / name)
    shutil.copyfile(helper, stage / HELPER_NAME)
    require_captured_stage(stage, control)
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
    command = ["/producer", "-d", "probe.wit", "-w", "libc-poll-oracle"]
    report = {
        "format": "tobkiri.ui-empty-poll-reference/1",
        "outcome": "pending",
        "control": control,
        "componentization": False,
        "guest_execution": False,
        "final_provider_resolution_verified": False,
        "c_abi_verified": False,
        "cold_schema_verified": False,
        "production_enabled": False,
    }
    try:
        subprocess.run(
            [*command, "bindings", "."],
            cwd=stage,
            env=environment,
            check=True,
            timeout=180,
        )
        require_captured_stage(stage, control)
        before = {
            p.relative_to(stage).as_posix(): fingerprint(p, FILE_LIMIT)
            for p in stage.rglob("*")
            if p.is_file()
        }
        subprocess.run(
            [*command, "componentize", "app", "-o", "component.wasm"],
            cwd=stage,
            env=environment,
            check=True,
            timeout=600,
        )
        report["componentization"] = True
        require_captured_stage(stage, control)
        if {
            p.relative_to(stage).as_posix(): fingerprint(p, FILE_LIMIT)
            for p in stage.rglob("*")
            if p.is_file() and p.relative_to(stage).as_posix() != "component.wasm"
        } != before:
            raise ValueError("probe/helper/generated bindings changed during preinit")
        component = stage / "component.wasm"
        report["stage_inputs"] = before
        report["trace"] = read_trace(work / "link-trace", control["helper"], component)
        report["observation"] = observe(component)
        report["guest_execution"] = True
        report["outcome"] = "success"
    except BaseException as error:
        report["outcome"] = "failed"
        report["failure_type"] = type(error).__name__
        raise
    finally:
        (work / "reference.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    sys.path[:0] = ["/validation-deps", "/"]
    run()
