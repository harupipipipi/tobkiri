"""Validate the real limited-ABI extension, preinitialize and execute on CI."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

sys.path.insert(0, "/validation-deps")
sys.path.insert(0, "/")

LIMIT = 32 * 1024 * 1024
INTERFACE = "tobkiri-ui:rpds-probe/probe@1.0.0"
FUEL = 2_000_000_000
SECONDS = 30


def require_boundary(imports: list[str], exports: list[str], *, inner: bool) -> None:
    """Keep the producer's private initializer inside the composed component."""
    expected = {INTERFACE, "exports"} if inner else {INTERFACE}
    if imports or set(exports) != expected or len(exports) != len(expected):
        raise ValueError("UI probe has unexpected authority imports or exports")


def compose_candidate(inner: Path, stage: Path, environment: dict[str, str]) -> dict:
    """Hide the saved producer initializer using the unchanged SDK WAC helper."""
    from composition import compose
    from wasmtime import Config, Engine
    from wasmtime.component import Component

    original = read(inner)
    config = Config()
    config.parallel_compilation = False
    with Engine(config) as engine, Component(engine, original) as component:
        imports = list(component.type.imports(engine))
        exports = list(component.type.exports(engine))
        require_boundary(imports, exports, inner=True)
    receipt = compose(
        Path("/wac"), inner, stage, stage, "ui-rpds-probe", [INTERFACE], environment
    )
    if read(inner) != original:
        raise ValueError("inner probe changed during composition")
    return {
        "inner_sha256": hashlib.sha256(original).hexdigest(),
        "inner_imports": imports,
        "inner_exports": sorted(exports),
        "composition": receipt,
    }


def read(path: Path, *, maximum: int = LIMIT) -> bytes:
    """Bound generated artifacts before passing them to the actual validator."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("regular probe artifact required")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if not data or len(data) > maximum:
        raise ValueError("probe artifact exceeds existing Pack bound")
    return data


def built_extension(work: Path) -> tuple[Path, dict]:
    """Select Cargo's root cdylib output without guessing the target's suffix."""
    transcript = read(work / "cargo-artifacts.jsonl", maximum=4 * 1024 * 1024)
    lines = transcript.splitlines()
    if len(lines) > 512 or any(len(line) > 256 * 1024 for line in lines):
        raise ValueError("Cargo artifact metadata exceeds its bound")
    candidates, finished = [], []
    for line in lines:
        item = json.loads(line)
        if type(item) is not dict:
            raise ValueError("Cargo artifact metadata must contain objects")
        if item.get("reason") == "build-finished":
            finished.append(item.get("success"))
        if item.get("reason") != "compiler-artifact":
            continue
        target = item.get("target", {})
        if item.get("manifest_path") != "/rpds/Cargo.toml":
            continue
        if (
            target.get("name") != "rpds"
            or target.get("kind") != ["cdylib"]
            or target.get("crate_types") != ["cdylib"]
            or target.get("src_path") != "/rpds/src/lib.rs"
            or item.get("profile", {}).get("test") is not False
        ):
            continue
        filenames = item.get("filenames")
        if type(filenames) is not list or len(filenames) != 1:
            raise ValueError("root extension must have exactly one Cargo output")
        path = Path(filenames[0])
        if (
            not path.is_absolute()
            or path.parent != work / "target/wasm32-wasip2/release"
        ):
            raise ValueError("root extension is outside the selected target directory")
        candidates.append((path, item))
    if finished != [True] or len(candidates) != 1:
        raise ValueError("Cargo did not report one successful root extension build")
    path, artifact = candidates[0]
    return path, {
        "transcript_sha256": hashlib.sha256(transcript).hexdigest(),
        "artifact": artifact,
    }


def custom_sections(data: bytes) -> list[str]:
    """Read only bounded core-Wasm section envelopes, without executing them."""
    if data[:8] != b"\0asm\x01\0\0\0":
        raise ValueError("extension is not core Wasm")
    at = 8

    def integer(end: int) -> int:
        nonlocal at
        value = 0
        for shift in range(0, 35, 7):
            if at >= end:
                raise ValueError("truncated Wasm section")
            byte = data[at]
            at += 1
            if shift == 28 and byte > 15:
                raise ValueError("oversized Wasm section integer")
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError("oversized Wasm section integer")

    names = []
    while at < len(data):
        kind = data[at]
        at += 1
        size = integer(len(data))
        end = at + size
        if end > len(data):
            raise ValueError("Wasm section extends outside artifact")
        if kind == 0:
            length = integer(end)
            if length > 4096 or at + length > end or len(names) >= 256:
                raise ValueError("Wasm custom-section metadata exceeds bound")
            names.append(data[at : at + length].decode("utf-8"))
        at = end
    return names


def observe(component_path: Path) -> dict:
    """Run a real post-snapshot export with no imports and original small limits."""
    from wasmtime import Config, Engine, Store
    from wasmtime.component import Component, Linker

    config = Config()
    config.parallel_compilation = False
    config.consume_fuel = True
    config.epoch_interruption = True
    with Engine(config) as engine, Component(engine, read(component_path)) as component:
        require_boundary(
            list(component.type.imports(engine)),
            list(component.type.exports(engine)),
            inner=False,
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
            errors = []

            def expire() -> None:
                if not finished.is_set():
                    try:
                        engine.increment_epoch()
                    except BaseException as error:
                        errors.append(type(error).__name__)

            timer = threading.Timer(SECONDS, expire)
            timer.daemon = True
            start = time.monotonic()
            timer.start()
            returned, cleaned = False, False
            try:
                instance = linker.instantiate(store, component)
                index = instance.get_export_index(store, INTERFACE)
                if index is None:
                    raise ValueError("probe interface missing")
                function_index = instance.get_export_index(store, "run", index)
                if function_index is None:
                    raise ValueError("probe operation missing")
                function = instance.get_func(store, function_index)
                if function is None:
                    raise ValueError("probe export is not a function")
                raw = function(store)
                returned = True
                try:
                    if type(raw) is not str or len(raw.encode("utf-8")) > 4096:
                        raise ValueError("probe output outside its small DATA bound")
                    result = json.loads(raw)
                finally:
                    function.post_return(store)
                    cleaned = True
            finally:
                finished.set()
                timer.cancel()
                timer.join()
            if (
                errors
                or time.monotonic() - start >= SECONDS
                or not (returned and cleaned)
            ):
                raise ValueError("probe did not finish its original bounded lifetime")
            target = result["target"]
            if (
                target["python"] != [3, 13, 16]
                or target["pointer_bits"] != 32
                or not isinstance(target["soabi"], str)
                or "313" not in target["soabi"]
                or "wasm32" not in target["soabi"]
                or not target["extension_file"].endswith("rpds.abi3.so")
                or result["original"] != {"before": 1}
                or result["extended"] != {"before": 1, "after": 2}
                or result["removed"] != {"after": 2}
                or result["missing_default"] != 7
                or result["set_values"] != ["two"]
            ):
                raise ValueError("real target/extension operations differ")
            return {
                "returned": returned,
                "post_return": cleaned,
                "result": result,
                "fuel_consumed": FUEL - store.get_fuel(),
                "imports": [],
            }


def main() -> None:
    """Require actual limited-API evidence before giving the library an abi3 name."""
    from wasmtime import Config, Engine, MemoryType, Module, TableType

    work = Path("/work")
    extension_path, build_artifact = built_extension(work)
    extension = read(extension_path)
    sections = custom_sections(extension)
    if "dylink.0" not in sections:
        raise ValueError("extension has no dynamic-link metadata")
    proof = list((work / "target/wasm32-wasip2/release/build").glob("pyo3-*/output"))
    limited = [
        path for path in proof if "cargo:rustc-cfg=Py_LIMITED_API" in path.read_text()
    ]
    if not limited:
        raise ValueError("actual PyO3 build did not enable the limited API")
    config = Config()
    config.parallel_compilation = False
    with Engine(config) as engine, Module(engine, extension) as module:
        imports = [
            {"module": item.module, "name": item.name, "kind": type(item.type).__name__}
            for item in module.imports
        ]
        exports = [item.name for item in module.exports]
        if (
            "PyInit_rpds" not in exports
            or not any(isinstance(item.type, MemoryType) for item in module.imports)
            or not any(isinstance(item.type, TableType) for item in module.imports)
        ):
            raise ValueError("extension is missing its Python/shared-memory/table ABI")
    stage = work / "stage"
    stage.mkdir()
    for name in ("app.py", "probe.wit"):
        shutil.copyfile(Path("/probe-source") / name, stage / name)
    (stage / "rpds.abi3.so").write_bytes(extension)
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
    command = ["/producer", "-d", "probe.wit", "-w", "ui-rpds-probe"]
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
    if read(stage / "rpds.abi3.so") != extension or any(
        (stage / name).read_bytes() != (Path("/probe-source") / name).read_bytes()
        for name in ("app.py", "probe.wit")
    ):
        raise ValueError("captured extension or probe source changed during preinit")
    composition = compose_candidate(stage / "component.wasm", stage, environment)
    component = stage / "outer.wasm"
    result = observe(component)
    report = {
        "format": "tobkiri.ui-rpds-probe/1",
        "passed": True,
        "extension_sha256": hashlib.sha256(extension).hexdigest(),
        "cargo_build": build_artifact,
        "component_sha256": hashlib.sha256(read(component)).hexdigest(),
        **composition,
        "custom_sections": sections,
        "extension_imports": imports,
        "extension_exports": exports,
        "actual_limited_api": True,
        "observation": result,
        "normal_pack_operation": False,
        "native_capture_binding": False,
        "full_setup_gui": False,
        "production_enabled": False,
    }
    (work / "probe.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
