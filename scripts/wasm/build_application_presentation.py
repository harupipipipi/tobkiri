"""Build an import-free Defaultspack presentation component from pinned source.

This command produces a conformance artifact.  It does not register a
production backend or modify any Pack manifest or selected executable.
"""

from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


TOOLS = {"componentize-py": "0.25.0", "wasmtime": "48.0.0"}


def capture_source(source: Path, expected_sha256: str) -> bytes:
    """Capture caller-selected source bytes against an explicit build pin."""
    if len(expected_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in expected_sha256
    ):
        raise ValueError("source SHA-256 must be 64 lowercase hexadecimal characters")
    if source.is_symlink() or not source.is_file():
        raise ValueError("source must be a regular Python file, not a symlink")
    content = source.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("source SHA-256 does not match the requested build pin")
    return content


def build(output: Path, *, source: Path, source_sha256: str) -> dict[str, object]:
    """Build and verify one presentation component using pinned tools."""
    source_bytes = capture_source(source, source_sha256)
    from wasmtime import Config, Engine
    from wasmtime.component import Component

    for tool, expected in TOOLS.items():
        if version(tool) != expected:
            raise ValueError(f"{tool} must be version {expected}")
    if output.suffix != ".wasm" or output.is_symlink():
        raise ValueError("output must be a regular .wasm artifact")
    compiler = Path(sys.executable).parent / "componentize-py"
    if not compiler.is_file():
        raise ValueError("componentize-py must be installed beside this Python")
    output = output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    directory = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="tobkiri-presentation-wasm-") as temporary:
        stage = Path(temporary)
        # Compile the verified capture, never reopen a mutable source path.
        (stage / "presentation.py").write_bytes(source_bytes)
        shutil.copyfile(directory / "pack.wit", stage / "pack.wit")
        shutil.copyfile(
            directory / "application_presentation_component.py", stage / "app.py"
        )
        # No user credentials, HOME, or environment variables enter preinit.
        environment = {
            "PATH": os.pathsep.join((str(compiler.parent), os.defpath)),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
        }
        command = [str(compiler), "-d", "pack.wit", "-w", "pack"]
        for arguments in (
            ["bindings", "."],
            ["componentize", "--stub-wasi", "app", "-o", "presentation.wasm"],
        ):
            subprocess.run(
                [*command, *arguments],
                cwd=stage,
                env=environment,
                check=True,
                timeout=180,
            )
        config = Config()
        config.parallel_compilation = False
        engine = Engine(config)
        component = Component.from_file(engine, str(stage / "presentation.wasm"))
        if component.type.imports(engine):
            raise ValueError(
                "Application Presentation component must not import Host capabilities"
            )
        binary = (stage / "presentation.wasm").read_bytes()
        provenance: dict[str, object] = {
            "schema": "io.tobkiri.wasm-build-evidence.v1",
            "source_sha256": hashlib.sha256(
                (stage / "presentation.py").read_bytes()
            ).hexdigest(),
            "wit_sha256": hashlib.sha256((stage / "pack.wit").read_bytes()).hexdigest(),
            "adapter_sha256": hashlib.sha256(
                (stage / "app.py").read_bytes()
            ).hexdigest(),
            "artifact_sha256": hashlib.sha256(binary).hexdigest(),
            "artifact_bytes": len(binary),
            "tools": TOOLS,
            "imports": [],
            "production_enabled": False,
        }
        output.write_bytes(binary)
        output.with_suffix(".build.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return provenance


def main() -> None:
    """Build one explicit output without modifying runtime selection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-sha256", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            build(args.output, source=args.source, source_sha256=args.source_sha256),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
