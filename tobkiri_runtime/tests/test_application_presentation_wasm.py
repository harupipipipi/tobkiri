"""Real-component parity checks for the sealed Defaultspack presentation."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from tobkiri_host.errors import ProviderExecutionError
from tobkiri_host.wasm_component import PureComponent


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts" / "wasm"
SOURCE = (
    ROOT
    / "tobkiri_runtime"
    / "ecosystem"
    / "defaultspack"
    / "runtime"
    / "application_presentation.py"
)
OPERATION = "defaultspack.presentation.read"


def _load_module(path: Path, name: str) -> Any:
    """Load one source file without granting it repository package discovery."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD_MODULE = _load_module(
    SCRIPTS / "build_application_presentation.py",
    "application_presentation_wasm_builder",
)


def _digest(binary: bytes) -> str:
    """Return the Artifact digest form required by the worker engine."""
    return "sha256:" + hashlib.sha256(binary).hexdigest()


def _require_build_tools() -> None:
    """Skip real compilation when its exact optional toolchain is unavailable."""
    try:
        for tool, expected in BUILD_MODULE.TOOLS.items():
            if importlib.metadata.version(tool) != expected:
                pytest.skip(f"{tool} {expected} is required for component validation")
    except importlib.metadata.PackageNotFoundError as exc:
        pytest.skip(f"{exc.name} is required for component validation")
    if not (Path(sys.executable).parent / "componentize-py").is_file():
        pytest.skip("componentize-py must be installed beside the pytest interpreter")


def test_capture_requires_a_matching_explicit_source_pin(tmp_path: Path) -> None:
    """The builder has no implicit source selection or digest bypass."""
    source = tmp_path / "presentation.py"
    content = b"presentation source"
    source.write_bytes(content)

    expected_sha256 = hashlib.sha256(content).hexdigest()
    assert BUILD_MODULE.capture_source(source, expected_sha256) == content
    with pytest.raises(ValueError, match="does not match"):
        BUILD_MODULE.capture_source(source, "0" * 64)


def test_cli_requires_source_and_pin(tmp_path: Path) -> None:
    """The command never discovers an application source implicitly."""
    output = tmp_path / "presentation.wasm"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "build_application_presentation.py"),
            "--output",
            str(output),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert "--source" in result.stderr
    assert "--source-sha256" in result.stderr
    assert not output.exists()


@pytest.fixture(scope="module")
def component_artifact(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """Build one pinned component and retain its bytes for parity checks."""
    _require_build_tools()
    output = tmp_path_factory.mktemp("application-presentation-wasm") / "guest.wasm"
    source_sha256 = hashlib.sha256(SOURCE.read_bytes()).hexdigest()

    evidence = BUILD_MODULE.build(
        output,
        source=SOURCE,
        source_sha256=source_sha256,
    )

    binary = output.read_bytes()
    assert evidence["source_sha256"] == source_sha256
    assert evidence["artifact_sha256"] == hashlib.sha256(binary).hexdigest()
    assert evidence["artifact_bytes"] == len(binary)
    assert evidence["imports"] == []
    assert evidence["production_enabled"] is False
    recorded = output.with_suffix(".build.json").read_text(encoding="utf-8")
    assert json.loads(recorded) == evidence
    return binary


def test_component_has_no_imports(component_artifact: bytes) -> None:
    """The generated component exposes no Host or WASI capability import."""
    wasmtime = pytest.importorskip("wasmtime")
    from wasmtime.component import Component

    engine = wasmtime.Engine()
    component = Component(engine, component_artifact)
    assert not component.type.imports(engine)


@pytest.mark.parametrize(
    "payload",
    (
        {
            "kind": "ui",
            "model_options": [{"value": "selected", "label": "Selected"}],
            "profile_id": "profile-a",
            "_session_id": "presentation-session-a",
        },
        {"kind": "commands", "profile_id": "profile-a"},
    ),
    ids=("ui", "commands"),
)
def test_component_matches_native_presentation(
    component_artifact: bytes, payload: dict[str, object]
) -> None:
    """The Wasm guest returns the exact sealed UI and command projections."""
    native = _load_module(SOURCE, "native_application_presentation")
    expected = native.tobkiri_packvm_invoke(OPERATION, payload)

    actual = PureComponent(component_artifact, _digest(component_artifact)).invoke(
        OPERATION, payload
    )

    assert actual == expected


@pytest.mark.parametrize(
    ("operation_id", "payload"),
    (
        ("defaultspack.execute", {"kind": "commands"}),
        (OPERATION, {}),
        (OPERATION, {"kind": "commands", "model_options": []}),
        (OPERATION, {"kind": "ui", "model_options": [{"value": True, "label": "x"}]}),
        (OPERATION, {"kind": "ui", "_session_id": "x" * (64 * 1024)}),
    ),
    ids=(
        "operation",
        "missing-kind",
        "commands-models",
        "invalid-model",
        "input-limit",
    ),
)
def test_component_rejects_the_same_invalid_requests_as_native(
    component_artifact: bytes, operation_id: str, payload: dict[str, object]
) -> None:
    """Guest errors stay inside the error result and expose no source detail."""
    native = _load_module(SOURCE, "native_application_presentation_invalid")
    with pytest.raises(ValueError):
        native.tobkiri_packvm_invoke(operation_id, payload)

    component = PureComponent(component_artifact, _digest(component_artifact))
    with pytest.raises(ProviderExecutionError, match="component rejected"):
        component.invoke(operation_id, payload)
