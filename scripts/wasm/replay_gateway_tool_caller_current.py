"""Replay the current two-component Gateway helper DEVELOPMENT fixture.

The historical runner, artifact pins, source oracle and receipts stay untouched.
Invoke only inside the separately approved OS process-group supervisor: guest
fuel and whole-case epoch deadlines cannot bound WAC or Wasmtime compilation.
This runner confers no Core/Gate, billing, production or full-Gateway authority.
"""

from __future__ import annotations

import argparse
from importlib.metadata import version
import math
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any

import replay_gateway_finalizer as current_evidence
import replay_gateway_tool_caller as prior

reference = prior.reference
digest = prior.digest
_read = prior._read
_pin = prior._pin
WAC_SOURCE = prior.WAC_SOURCE
DEFAULT_FUEL = prior.DEFAULT_FUEL
DEFAULT_TIMEOUT = prior.DEFAULT_TIMEOUT
STORE_LIMITS = prior.STORE_LIMITS.copy()
CURRENT_ORACLE_SHA256 = (
    "854fa03de651c12cb7126d8f81e4f0c62e340a5a85f71eb50784e539e207bced"
)
COMPONENTS = {"caller": "caller", "provider": "bridge"}
NO_AUTHORITY = {
    **prior.NO_AUTHORITY,
    "build_performed": False,
    "authority_granted": False,
    "billing_authority_claimed": False,
    "full_gateway_migration_claimed": False,
    "historical_receipts_are_current_source_proof": False,
}


def runner_bindings() -> dict[str, str]:
    """Bind every imported local runner, observer, validator and stager module."""
    bindings = current_evidence.runner_bindings()
    name = Path(__file__).name
    return {**bindings, name: digest(_read(Path(__file__)))}


def validate_pins(pins: dict[str, Any]) -> None:
    """Require a separately reviewed current graph without replacing old pins."""
    fields = {
        *COMPONENTS, "source_observations_sha256", "wac_sha256",
        "runner_source_bindings_sha256", "composition_wac_sha256",
    }
    if (
        type(pins) is not dict or set(pins) != fields
        or any(
            type(pins[name]) is not dict
            or set(pins[name]) != {"artifact_sha256", "receipt_sha256"}
            for name in COMPONENTS
        )
    ):
        raise ValueError(
            "pins must bind current caller/provider, source, runner and WAC"
        )
    values = [pins[key] for key in fields - set(COMPONENTS)]
    values.extend(value for name in COMPONENTS for value in pins[name].values())
    if any(
        type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
        for value in values
    ):
        raise ValueError("all reviewed identities must be exact SHA-256 values")
    if pins["source_observations_sha256"] != CURRENT_ORACLE_SHA256:
        raise ValueError("source pin differs from the reviewed current reference")
    _pin(WAC_SOURCE, pins["composition_wac_sha256"], "fixed two-component WAC graph")
    _pin(
        reference.canonical(runner_bindings()),
        pins["runner_source_bindings_sha256"], "current runner source closure",
    )


def validate_evidence(
    caller: Path, provider: Path, oracle: Path, pins: dict[str, Any],
) -> dict[str, Any]:
    """Bind both current components to exact current source plans and 30 cases."""
    validate_pins(pins)
    paths = {"caller": caller, "provider": provider}
    components = {
        name: current_evidence.validate_component(kind, paths[name], pins[name])
        for name, kind in COMPONENTS.items()
    }
    # Never derive this expected identity from the input document or old pins.
    source_bytes, source = prior.validate_observations(oracle, CURRENT_ORACLE_SHA256)
    if len(source["cases"]) != 30:
        raise ValueError("current replay requires all 30 source cases")
    return {
        "components": components, "source_bytes": source_bytes, "source": source,
    }


def _base_result() -> dict[str, Any]:
    return {
        **prior._base_result(), **NO_AUTHORITY,
        "format": "tobkiri.gateway-tool-caller-current-development-replay/1",
        "scope": "current-source-two-component-not-native-core-acceptance",
        "reviewed_current_source_sha256": CURRENT_ORACLE_SHA256,
        "historical_evidence_scope": (
            "Separate from the preserved 92f38e3 artifacts, source and replay receipts"
        ),
    }


def _compose_and_replay(
    caller: Path, provider: Path, oracle: Path, pins: dict[str, Any], wac: Path,
    *, fuel: int = DEFAULT_FUEL, timeout: float = DEFAULT_TIMEOUT,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute only inside the independently approved external OS supervisor."""
    if type(fuel) is not int or not 0 < fuel <= DEFAULT_FUEL:
        raise ValueError("invalid guest fuel bound")
    if (
        type(timeout) not in (int, float) or not math.isfinite(timeout)
        or not 0 < timeout <= DEFAULT_TIMEOUT
    ):
        raise ValueError("invalid guest epoch timeout")
    if version("wasmtime") != "48.0.0":
        raise ValueError("current replay requires Wasmtime 48.0.0")
    if result is None:
        result = _base_result()
    before = runner_bindings()
    evidence = validate_evidence(caller, provider, oracle, pins)
    wac_bytes = _read(wac, 256 * 1024**2)
    _pin(wac_bytes, pins["wac_sha256"], "WAC executable")
    from wasmtime import Config, Engine
    from wasmtime.component import Component, Linker

    with tempfile.TemporaryDirectory(prefix="gateway-current-pair-") as temporary:
        workspace = Path(temporary)
        for name in COMPONENTS:
            (workspace / f"{name}.wasm").write_bytes(
                evidence["components"][name]["binary"],
            )
        (workspace / "replay.wac").write_bytes(WAC_SOURCE)
        executable = workspace / "wac"
        executable.write_bytes(wac_bytes)
        executable.chmod(0o700)
        command = [
            str(executable), "compose", str(workspace / "replay.wac"),
            "--dep", f"local:caller={workspace / 'caller.wasm'}",
            "--dep", f"local:provider={workspace / 'provider.wasm'}",
            "--deps-dir", str(workspace / "no-ambient-dependencies"),
            "-o", str(workspace / "composed.wasm"),
        ]
        # The external supervisor bounds this subprocess and compilation below.
        composed = subprocess.run(
            command, check=True, capture_output=True, text=True, cwd=workspace,
        )
        binary = _read(workspace / "composed.wasm", 512 * 1024**2)
    result.update(
        composition_sha256=digest(binary), composition_bytes=len(binary),
        composition_wac_sha256=digest(WAC_SOURCE), component_pins=pins,
        runner_source_bindings=before, replay_sha256=before[Path(__file__).name],
    )
    config = Config()
    config.consume_fuel = True
    config.epoch_interruption = True
    config.memory_reservation = STORE_LIMITS["memory_size"]
    result["engine_memory_reservation_bytes"] = STORE_LIMITS["memory_size"]
    result["engine_memory_reservation_scope"] = (
        "Unchanged 128 MiB development fixture virtual reservation; per-memory "
        "Store caps, tables, guards, growth, traps and external OS limits unchanged. "
        "This is not the separate native Core memory cap."
    )
    engine = Engine(config)
    component = Component(engine, binary)
    imports, exports = component.type.imports(engine), component.type.exports(engine)
    result.update(imports=sorted(imports), exports=sorted(exports))
    surface = {
        "imports": {
            name: prior._type_shape(item, engine) for name, item in imports.items()
        },
        "exports": {
            name: prior._type_shape(item, engine) for name, item in exports.items()
        },
    }
    prior.record_surface_diagnostics(result, surface)
    if set(imports) != prior.ALLOWED_IMPORTS or set(exports) != prior.EXPECTED_EXPORTS:
        raise ValueError("composition imports/exports differ from exact reviewed graph")
    prior.validate_surface(surface)
    result["typed_surface_validated"] = True
    linker = Linker(engine)
    linker.define_unknown_imports_as_traps(component)
    observations = result["observations"]
    for case in evidence["source"]["cases"]:
        observations.append(
            prior._replay_case(engine, component, linker, case, fuel, timeout),
        )
        result.update(prior.summarize(evidence["source"]["cases"], observations))
        result["replay_succeeded"] = False  # Final evidence revalidation is pending.
    after = validate_evidence(caller, provider, oracle, pins)
    if (
        evidence != after or runner_bindings() != before
        or _read(wac, 256 * 1024**2) != wac_bytes
    ):
        raise ValueError("current evidence, runner or WAC changed during replay")
    result.update(
        **prior.summarize(evidence["source"]["cases"], observations),
        component_source_evidence={
            name: {
                key: value for key, value in item.items()
                if key not in ("binary", "receipt_bytes")
            }
            for name, item in evidence["components"].items()
        },
        source_observations_sha256=digest(evidence["source_bytes"]),
        original_reference_runtime=evidence["source"]["runtime"],
        expected_guest_runtime=prior.EXPECTED_RUNTIME,
        guest_timeout_seconds=timeout,
        guest_timeout_scope=(
            "entire case including instantiation and four lifecycle calls"
        ),
        fuel_per_case=fuel, store_limits=STORE_LIMITS.copy(),
        composition_command=[
            "wac", "compose", "replay.wac", "--dep", "local:caller=caller.wasm",
            "--dep", "local:provider=provider.wasm",
            "--deps-dir", "no-ambient-dependencies", "-o", "composed.wasm",
        ],
        composition_tool_stderr=composed.stderr,
    )
    return result


def main() -> int:
    """Write a fresh current receipt without overwriting historical evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("caller", "provider", "oracle", "pins", "wac", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument(
        "--pins-sha256", required=True,
        help="Independently reviewed SHA-256 of the exact current pins JSON",
    )
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("replay output must be a new file")
    result = _base_result()
    try:
        raw = _read(args.pins)
        _pin(raw, args.pins_sha256, "explicit reviewed current pins document")
        pins = prior.provider_stage.strict_json(raw)
        result["pins_document_sha256"] = digest(raw)
        _compose_and_replay(
            args.caller, args.provider, args.oracle, pins, args.wac, result=result,
        )
    except Exception as exc:
        result["replay_succeeded"] = False
        result["runner_failure"] = {"type": type(exc).__name__, "message": str(exc)}
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(reference.json_bytes(result).decode())
    return 0 if result["replay_succeeded"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
