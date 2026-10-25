"""Compare the real locked JSON Schema closure in isolated native/Wasm CI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys

from build_native import GIB, HERE, fingerprint, sandbox_command
from build_rpds import physical, verify_validation
from build_wasi import run_stage
import fetch_rpds
import fetch_schema
import fetch_schema_stdlib
import fetch_sdk
import runtime_inputs
import poll_comparison_inputs
import poll_reference_results
import ci_link_trace_source

RPDS_HEAD = "53163ed815c62c4853bbed3d8a082c0764e3084f"
EXTENSION_SHA256 = "ace6bfa6de9f435f12380e9c11b1b7ed0a9645b721bdcdf634fb02cdbbff720b"
COMPONENT_SHA256 = "56d765060d8f2f9078a31cf2e96bdd591d737545250464afcb9bc9cdbc91c551"
INPUT_LIMIT = 64 * 1024 * 1024
POLL_PIN_PATH = HERE / "empty-poll-comparison-inputs.json"
POLL_REFERENCE_PIN_PATH = HERE / "schema-poll-reference-inputs.json"
POLL_REFERENCE_REQUIRED_STEPS = {
    "Check comparison custody and unchanged finite C inputs offline",
    "Authenticate actual supplier run and all mandatory successful stages",
    "Rehash every decoded CLI helper receipt and pair before execution",
    "Restore the unchanged pinned validation tools and private namespace",
    "Call the same raw C cases with genuine WASIp2 in two private Stores",
}


def verify_clock_variant(
    cli: dict,
    source: dict,
    *,
    allow_ui_wall_clock: bool,
    allow_ui_monotonic_clock: bool = False,
    allow_inactive_ui_libc: bool = False,
    allow_ci_link_trace: bool = False,
) -> None:
    """Never accept a new producer mode through the historical consumer option."""
    if (allow_inactive_ui_libc or allow_ci_link_trace) and not (
        allow_ui_wall_clock
        and allow_ui_monotonic_clock
        and allow_inactive_ui_libc
        and allow_ci_link_trace
    ):
        raise ValueError(
            "inactive poll candidate requires explicit wall, monotonic, and trace modes"
        )
    poll_marker = cli.get("ui_empty_poll_libc_candidate")
    if allow_inactive_ui_libc:
        if not isinstance(poll_marker, dict):
            raise ValueError("separate explicit inactive poll consumer required")
        expected_marker_fields = {
            "format",
            "build_receipt",
            "source_pin",
            "derivation",
            "original_libc",
            "candidate_libc",
            "changed_asset",
            "runtime_asset_override",
            "scope",
            "c_abi_verified",
            "final_provider_resolution_verified",
            "cold_schema_verified",
            "production_enabled",
        }
        if (
            set(poll_marker) != expected_marker_fields
            or poll_marker.get("format") != "tobkiri.ui-empty-poll-ci-asset/1"
            or poll_marker.get("changed_asset") != "libc.so.zst"
            or poll_marker.get("runtime_asset_override") is not False
            or poll_marker.get("scope")
            != "inactive CI original/candidate comparison producer only"
            or any(
                poll_marker.get(name) is not False
                for name in (
                    "c_abi_verified",
                    "final_provider_resolution_verified",
                    "cold_schema_verified",
                    "production_enabled",
                )
            )
        ):
            raise ValueError("inactive libc marker scope or false flags differ")
    elif poll_marker is not None:
        raise ValueError("inactive libc producer requires its exact opt-in")

    trace = cli.get("ci_link_trace_candidate")
    source_trace = source.get("ci_link_trace")
    if allow_ci_link_trace:
        _verify_link_trace_derivation(cli, source)
    elif trace is not None or source_trace is not None:
        raise ValueError("CI Linker observation requires its exact opt-in")

    mono = cli.get("ui_monotonic_clock_candidate")
    if allow_ui_monotonic_clock:
        if not allow_ui_wall_clock:
            raise ValueError("monotonic candidate requires explicit wall-clock mode")
        if (
            not isinstance(mono, dict)
            or mono != source.get("ui_monotonic_clock")
            or mono.get("feature") != "ui-monotonic-clock"
            or mono.get("feature_dependencies") != ["ui-wall-clock"]
            or mono.get("interface_prefix") != "wasi:clocks/monotonic-clock@"
            or mono.get("import_interface") != "tobkiri-ui:clock/monotonic@1.0.0"
            or mono.get("functions") != ["now", "resolution"]
            or mono.get("result") != "u64 nanoseconds"
            or mono.get("other_exports") != "original signatures and unreachable bodies"
            or mono.get("stub_wasi_retained") is not True
            or set(mono.get("changed_files", {})) != {"Cargo.toml", "src/stubwasi.rs"}
        ):
            raise ValueError("separate explicit monotonic producer derivation required")
    elif mono is not None or source.get("ui_monotonic_clock") is not None:
        raise ValueError(
            "monotonic producer requires its separate consuming controller"
        )
    clock_variant = cli.get("ui_wall_clock_candidate")
    if allow_ui_wall_clock:
        if (
            not isinstance(clock_variant, dict)
            or clock_variant != source.get("ui_wall_clock")
            or clock_variant.get("feature") != "ui-wall-clock"
            or clock_variant.get("interface_prefix") != "wasi:clocks/wall-clock@"
            or clock_variant.get("functions") != ["now", "resolution"]
            or clock_variant.get("stub_wasi_retained") is not True
            or set(clock_variant.get("changed_files", {}))
            != {"Cargo.toml", "src/stubwasi.rs"}
        ):
            raise ValueError("separate explicit UI clock producer derivation required")
    elif clock_variant is not None or source.get("ui_wall_clock") is not None:
        raise ValueError("UI clock producer requires an explicit controller option")
    if allow_ui_monotonic_clock:
        for name, entry in mono["changed_files"].items():
            final_before_trace = (
                source_trace["changed_files"][name]["before"]
                if allow_ci_link_trace and name == "Cargo.toml"
                else source.get("files", {}).get(name)
            )
            if (
                entry.get("wall_clock_candidate")
                != clock_variant["changed_files"][name].get("candidate")
                or entry.get("candidate") != final_before_trace
                or not entry.get("candidate")
            ):
                raise ValueError(
                    "monotonic producer source ancestry or final inventory differs"
                )
    elif allow_ui_wall_clock and allow_ci_link_trace:
        for name, entry in clock_variant["changed_files"].items():
            final_before_trace = (
                source_trace["changed_files"][name]["before"]
                if name == "Cargo.toml"
                else source.get("files", {}).get(name)
            )
            if entry.get("candidate") != final_before_trace or not entry.get(
                "candidate"
            ):
                raise ValueError("wall-clock producer source ancestry differs")


def _verify_link_trace_derivation(cli: dict, source: dict) -> None:
    """Close the clock-to-trace source delta without changing producer files."""
    trace = cli.get("ci_link_trace_candidate")
    if not isinstance(trace, dict) or trace != source.get("ci_link_trace"):
        raise ValueError("saved CLI and source Linker trace receipts differ")
    expected_keys = {
        "feature",
        "upstream_link_sha256",
        "path",
        "maximum_members",
        "maximum_file_bytes",
        "maximum_total_bytes",
        "stage",
        "final_component_binding_verified",
        "final_provider_resolution_verified",
        "production_enabled",
        "changed_files",
    }
    changed = trace.get("changed_files")
    if (
        set(trace) != expected_keys
        or trace.get("feature") != ci_link_trace_source.FEATURE
        or trace.get("upstream_link_sha256")
        != ci_link_trace_source.ORIGINAL_LINK_SHA256
        or trace.get("path") != "/work/link-trace"
        or trace.get("maximum_members") != 128
        or trace.get("maximum_file_bytes") != 32 * 1024 * 1024
        or trace.get("maximum_total_bytes") != 64 * 1024 * 1024
        or trace.get("stage")
        != "actual selected libraries and linked component before snapshot"
        or trace.get("final_component_binding_verified") is not False
        or trace.get("final_provider_resolution_verified") is not False
        or trace.get("production_enabled") is not False
        or not isinstance(changed, dict)
        or set(changed) != {"Cargo.toml", "src/link.rs"}
    ):
        raise ValueError("saved Linker trace derivation or scope differs")
    source_files = source.get("files", {})
    original_files = source.get("original_files", {})
    for name, entry in changed.items():
        if (
            not isinstance(entry, dict)
            or set(entry) != {"before", "after"}
            or entry["after"] != source_files.get(name)
            or not isinstance(entry["before"], dict)
            or not isinstance(entry["after"], dict)
        ):
            raise ValueError("Linker trace source changes do not match final inventory")
    link_before = changed["src/link.rs"]["before"]
    if (
        link_before != original_files.get("src/link.rs")
        or link_before.get("sha256") != ci_link_trace_source.ORIGINAL_LINK_SHA256
    ):
        raise ValueError("Linker trace does not descend from the original wrapper")
    manifest_before = changed["Cargo.toml"]["before"]
    clock = cli.get("ui_wall_clock_candidate")
    mono = cli.get("ui_monotonic_clock_candidate")
    predecessor = (
        mono["changed_files"]["Cargo.toml"]["candidate"]
        if isinstance(mono, dict)
        else clock["changed_files"]["Cargo.toml"]["candidate"]
        if isinstance(clock, dict)
        else original_files.get("Cargo.toml")
    )
    if manifest_before != predecessor:
        raise ValueError("clock-to-Linker-trace Cargo.toml ancestry differs")


def _validate_poll_reference_pins(pins: dict) -> None:
    """Require the complete committed identity of the actual raw-C predecessor."""
    if (
        pins.get("format") != "tobkiri.ui-schema-poll-reference-inputs/1"
        or pins.get("repository") != "harupipipipi/tobkiri"
        or pins.get("workflow") != ".github/workflows/issue-1520-ui-poll-reference.yml"
        or type(pins.get("head")) is not str
        or re.fullmatch(r"[0-9a-f]{40}", pins["head"]) is None
        or any(
            type(pins.get(name)) is not int or pins[name] <= 0
            for name in (
                "run",
                "job",
                "artifact",
                "artifact_bytes",
                "comparison_bytes",
            )
        )
        or pins.get("artifact_name") != "ui-empty-poll-reference-evidence"
        or type(pins.get("artifact_sha256")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", pins["artifact_sha256"]) is None
        or pins.get("comparison_sha256")
        != "b1fe73827415f27a3c4245b86cbce2224a618963f6e9e79cd7dd7c4cdc6431bc"
        or type(pins.get("files")) is not dict
        or set(pins["files"]) != {"comparison.json"}
        or pins["files"]["comparison.json"]
        != {
            "bytes": pins.get("comparison_bytes"),
            "sha256": pins.get("comparison_sha256"),
        }
    ):
        raise ValueError("complete pinned raw-C predecessor identity required")


def _authenticate_poll_reference(
    pins: dict, run: dict, job: dict, artifact: dict
) -> None:
    """Authenticate the prior raw-C comparison run and its exact evidence artifact."""
    _validate_poll_reference_pins(pins)
    if (
        run.get("id") != pins["run"]
        or run.get("head_sha") != pins["head"]
        or run.get("path") != pins["workflow"]
        or run.get("repository", {}).get("full_name") != pins["repository"]
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or job.get("id") != pins["job"]
        or job.get("run_id") != pins["run"]
        or job.get("name") != "actual-c-reference"
        or job.get("status") != "completed"
        or job.get("conclusion") != "success"
        or not POLL_REFERENCE_REQUIRED_STEPS
        <= {
            step.get("name")
            for step in job.get("steps", [])
            if step.get("status") == "completed" and step.get("conclusion") == "success"
        }
        or artifact.get("id") != pins["artifact"]
        or artifact.get("name") != pins["artifact_name"]
        or artifact.get("expired") is not False
        or artifact.get("size_in_bytes") != pins["artifact_bytes"]
        or artifact.get("digest") != "sha256:" + pins["artifact_sha256"]
        or artifact.get("workflow_run", {}).get("id") != pins["run"]
        or artifact.get("workflow_run", {}).get("head_sha") != pins["head"]
    ):
        raise ValueError("actual raw-C predecessor run/job/artifact differs")


def _verify_raw_c_predecessor(
    path: Path, pins: dict, supplier_pins: dict
) -> tuple[dict, dict]:
    """Verify the recorded comparison only as its saved inactive attestation."""
    _validate_poll_reference_pins(pins)
    identity = fingerprint(path, 4 * 1024 * 1024)
    if identity != pins["files"]["comparison.json"]:
        raise ValueError("actual raw-C predecessor report bytes differ")
    report = json.loads(path.read_bytes())
    if (
        type(report) is not dict
        or report.get("format") != "tobkiri.ui-empty-poll-comparison/1"
        or report.get("outcome") != "success"
        or report.get("supplier") != supplier_pins
        or report.get("guest_execution") is not True
        or report.get("raw_c_observations_match") is not True
        or any(
            report.get(name) is not False
            for name in (
                "final_provider_resolution_verified",
                "tls_storage_provider_verified",
                "c_abi_verified",
                "cold_schema_verified",
                "normal_pack_operation",
                "full_setup_gui",
                "production_enabled",
            )
        )
        or set(report.get("variants", {})) != {"original", "candidate"}
    ):
        raise ValueError("saved raw-C predecessor scope or false flags differ")
    trace_comparison = report.get("trace_comparison", {})
    if (
        trace_comparison.get("actual_input_pair_matches") is not True
        or trace_comparison.get("changed_libraries") != ["libc.so"]
        or trace_comparison.get("same_helper_and_generated_bindings") is not True
        or trace_comparison.get("final_provider_resolution_verified") is not False
        or trace_comparison.get("tls_storage_provider_verified") is not False
    ):
        raise ValueError("saved raw-C trace comparison or scope flags differ")
    references = {}
    expected_helper = supplier_pins["files"][poll_comparison_inputs.HELPER]
    for variant, prefix in poll_comparison_inputs.PREFIXES.items():
        saved = report["variants"][variant]
        reference = saved.get("reference")
        if (
            saved.get("outcome") != "success"
            or not isinstance(reference, dict)
            or reference.get("format") != "tobkiri.ui-empty-poll-reference/1"
            or reference.get("outcome") != "success"
            or reference.get("guest_execution") is not True
            or reference.get("componentization") is not True
            or any(
                reference.get(name) is not False
                for name in (
                    "final_provider_resolution_verified",
                    "c_abi_verified",
                    "cold_schema_verified",
                    "production_enabled",
                )
            )
        ):
            raise ValueError("saved raw-C variant execution is incomplete")
        expected_producer = supplier_pins["files"][
            prefix + "/work/target/release/componentize-py"
        ]
        control = reference.get("control", {})
        observation = reference.get("observation", {})
        trace = reference.get("trace", {})
        if (
            control.get("variant") != variant
            or control.get("producer") != expected_producer
            or control.get("helper") != expected_helper
            or trace.get("helper") != expected_helper
            or trace.get("actual_input_observation") is not True
            or trace.get("final_provider_resolution_verified") is not False
            or trace.get("tls_storage_provider_verified") is not False
            or trace.get("production_enabled") is not False
            or any(
                observation.get(name) is not True
                for name in (
                    "genuine_wasip2",
                    "returned",
                    "post_return",
                    "store_retired",
                    "epoch_timer_joined",
                )
            )
            or observation.get("private_initializer_called") is not False
            or observation.get("inherited_arguments_environment_stdio_preopens_network")
            is not False
        ):
            raise ValueError("saved raw-C component or Store scope differs")
        references[variant] = reference
    behavior = poll_reference_results.compare(
        references["original"]["observation"]["result"],
        references["candidate"]["observation"]["result"],
    )
    if report.get("behavior_comparison") != behavior:
        raise ValueError("saved finite raw-C comparison result differs")
    summary = {
        "report": identity,
        "run": pins["run"],
        "job": pins["job"],
        "artifact": pins["artifact"],
        "outcome": report["outcome"],
        "guest_execution": report["guest_execution"],
        "raw_c_observations_match": report["raw_c_observations_match"],
        "scope_flags": {
            name: report[name]
            for name in (
                "final_provider_resolution_verified",
                "tls_storage_provider_verified",
                "c_abi_verified",
                "cold_schema_verified",
                "normal_pack_operation",
                "full_setup_gui",
                "production_enabled",
            )
        },
    }
    return summary, identity


def verify_inactive_poll_predecessors(
    bundle: Path,
    authentication: Path,
    reference_report: Path,
    reference_authentication: Path,
    producer: Path,
    cli: dict,
    source: dict,
) -> tuple[dict, dict]:
    """Authenticate the actual pair and saved C oracle before schema execution."""
    for path in (
        bundle,
        authentication,
        reference_report.parent,
        reference_authentication,
    ):
        physical(path)
    supplier_pins = poll_comparison_inputs.read_json(POLL_PIN_PATH)
    poll_comparison_inputs.authenticate(
        supplier_pins,
        *(
            poll_comparison_inputs.read_json(authentication / name)
            for name in ("run.json", "job.json", "artifact.json")
        ),
    )
    supplier = poll_comparison_inputs.verify_bundle(bundle, supplier_pins)
    expected_producer = bundle / poll_comparison_inputs.PREFIXES["candidate"]
    if producer.resolve() != expected_producer.resolve():
        raise ValueError("schema producer must be the authenticated candidate CLI")
    pair = supplier["pair"]
    if (
        pair.get("candidate", {}).get("producer") != cli
        or pair.get("candidate", {}).get("source") != source
        or cli.get("ui_empty_poll_libc_candidate")
        != pair.get("candidate", {})
        .get("producer", {})
        .get("ui_empty_poll_libc_candidate")
    ):
        raise ValueError("candidate CLI/source do not match saved pair provenance")
    marker = cli["ui_empty_poll_libc_candidate"]
    libc = source.get("files", {}).get("libc.so.zst")
    candidate_assets = pair.get("candidate", {}).get("assets", {})
    packaged_libc = candidate_assets.get("outputs", {}).get("libc.so.zst")
    if (
        not isinstance(libc, dict)
        or not isinstance(packaged_libc, dict)
        or candidate_assets.get("ui_empty_poll_candidate") != marker
        or packaged_libc != {name: libc.get(name) for name in ("bytes", "sha256")}
    ):
        raise ValueError("candidate libc marker/package differs from saved provenance")

    reference_pins = poll_comparison_inputs.read_json(POLL_REFERENCE_PIN_PATH)
    reference_auth = tuple(
        poll_comparison_inputs.read_json(reference_authentication / name)
        for name in ("run.json", "job.json", "artifact.json")
    )
    _authenticate_poll_reference(reference_pins, *reference_auth)
    c_summary, c_identity = _verify_raw_c_predecessor(
        reference_report, reference_pins, supplier_pins
    )
    identities = {
        str(POLL_PIN_PATH): fingerprint(POLL_PIN_PATH),
        str(POLL_REFERENCE_PIN_PATH): fingerprint(POLL_REFERENCE_PIN_PATH),
        str(reference_report): c_identity,
    }
    for name, identity in supplier_pins["files"].items():
        identities[str(bundle / name)] = identity
    for auth_dir in (authentication, reference_authentication):
        for name in ("run.json", "job.json", "artifact.json"):
            path = auth_dir / name
            identities[str(path)] = fingerprint(path, 4 * 1024 * 1024)
    summary = {
        "supplier": {
            name: supplier_pins[name]
            for name in (
                "head",
                "run",
                "job",
                "artifact",
                "artifact_bytes",
                "artifact_sha256",
            )
        },
        "candidate_cli": supplier_pins["files"][
            poll_comparison_inputs.PREFIXES["candidate"]
            + "/work/target/release/componentize-py"
        ],
        "candidate_marker": marker,
        "raw_c_predecessor": c_summary,
        "production_enabled": False,
    }
    return summary, identities


def verify_inputs(
    inputs: Path,
    producer: Path,
    rpds: Path,
    validation: Path,
    stdlib: Path,
    *,
    allow_ui_wall_clock: bool = False,
    allow_ui_monotonic_clock: bool = False,
    allow_inactive_ui_libc: bool = False,
    allow_ci_link_trace: bool = False,
) -> dict:
    """Bind all restored archives and actual prior artifacts before execution."""
    for path in (inputs, producer, rpds, validation, stdlib):
        physical(path)
    fetch_schema.verify(inputs)
    fetch_schema_stdlib.verify(stdlib)
    verify_validation(
        validation, runtime_inputs.report(validation / "validation-inputs.json")
    )
    if fingerprint(rpds / "work/stage/rpds.abi3.so")["sha256"] != EXTENSION_SHA256:
        raise ValueError("rpds extension differs from the actual successful ABI probe")
    if fingerprint(rpds / "work/stage/outer.wasm")["sha256"] != COMPONENT_SHA256:
        raise ValueError("rpds predecessor component differs")
    fingerprint(rpds / "work/probe.json", 4 * 1024 * 1024)
    prior = json.loads((rpds / "work/probe.json").read_bytes())
    if not (
        prior["passed"] is True
        and prior["actual_limited_api"] is True
        and prior["observation"]["post_return"] is True
        and prior["extension_sha256"] == EXTENSION_SHA256
    ):
        raise ValueError("actual rpds predecessor observation is incomplete")
    cli = runtime_inputs.report(producer / "cli-build.json")
    if (
        cli["producer_cli"] is not True
        or cli["inputs_unchanged"] is not True
        or fingerprint(producer / "work/target/release/componentize-py")
        != cli["binary"]
    ):
        raise ValueError("saved producer identity differs")
    source_pins = [
        value
        for name, value in cli["predecessors"].items()
        if Path(name).name == "cli-source.json"
    ]
    if source_pins != [fingerprint(producer / "cli-source.json")]:
        raise ValueError("saved producer source receipt differs")
    fingerprint(producer / "cli-source.json", 4 * 1024 * 1024)
    source = json.loads((producer / "cli-source.json").read_bytes())
    verify_clock_variant(
        cli,
        source,
        allow_ui_wall_clock=allow_ui_wall_clock,
        allow_ui_monotonic_clock=allow_ui_monotonic_clock,
        allow_inactive_ui_libc=allow_inactive_ui_libc,
        allow_ci_link_trace=allow_ci_link_trace,
    )
    return {
        str(path): fingerprint(path)
        for path in (
            inputs / "schema-inputs.json",
            producer / "cli-build.json",
            producer / "cli-source.json",
            rpds / "work/probe.json",
            rpds / "rpds-build.json",
            validation / "validation-inputs.json",
            stdlib / "stdlib-inputs.json",
        )
    }


def _validate_schema_exception_diagnostics(
    enabled: bool,
    *,
    allow_ui_wall_clock: bool,
    allow_ui_monotonic_clock: bool,
    allow_inactive_ui_libc: bool,
    allow_ci_link_trace: bool,
    predecessor_paths: tuple[Path | None, ...],
) -> None:
    """Restrict diagnostics to the complete inactive candidate input set."""
    if type(enabled) is not bool:
        raise ValueError("explicit boolean schema exception diagnostics mode required")
    if enabled and not (
        all(
            type(value) is bool and value
            for value in (
                allow_ui_wall_clock,
                allow_ui_monotonic_clock,
                allow_inactive_ui_libc,
                allow_ci_link_trace,
            )
        )
        and len(predecessor_paths) == 4
        and all(isinstance(path, Path) for path in predecessor_paths)
    ):
        raise ValueError(
            "schema exception diagnostics require the complete inactive candidate"
        )


def _validate_schema_stage_profile(
    enabled: bool,
    *,
    allow_ui_wall_clock: bool,
    allow_ui_monotonic_clock: bool,
    allow_inactive_ui_libc: bool,
    allow_ci_link_trace: bool,
    predecessor_paths: tuple[Path | None, ...],
) -> None:
    """Restrict profiling to the same complete, inactive candidate inputs."""
    if type(enabled) is not bool:
        raise ValueError("explicit boolean schema stage-profile mode required")
    if enabled and not (
        all(
            type(value) is bool and value
            for value in (
                allow_ui_wall_clock,
                allow_ui_monotonic_clock,
                allow_inactive_ui_libc,
                allow_ci_link_trace,
            )
        )
        and len(predecessor_paths) == 4
        and all(isinstance(path, Path) for path in predecessor_paths)
    ):
        raise ValueError(
            "schema stage profile requires the complete inactive candidate"
        )


def run(
    inputs: Path,
    producer: Path,
    rpds: Path,
    validation: Path,
    stdlib: Path,
    destination: Path,
    *,
    allow_ui_wall_clock: bool = False,
    allow_ui_monotonic_clock: bool = False,
    allow_inactive_ui_libc: bool = False,
    allow_ci_link_trace: bool = False,
    schema_exception_diagnostics: bool = False,
    schema_stage_profile: bool = False,
    poll_bundle: Path | None = None,
    poll_authentication: Path | None = None,
    poll_reference_report: Path | None = None,
    poll_reference_authentication: Path | None = None,
) -> None:
    """Execute original wheels and the Wasm candidate only in supervised Linux CI."""
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("schema execution is restricted to Linux CI")
    if sys.version_info[:3] != (3, 13, 16):
        raise ValueError("native original CPython 3.13.16 reference required")
    predecessor_paths = (
        poll_bundle,
        poll_authentication,
        poll_reference_report,
        poll_reference_authentication,
    )
    if allow_inactive_ui_libc or allow_ci_link_trace:
        if not (
            allow_ui_wall_clock
            and allow_ui_monotonic_clock
            and allow_inactive_ui_libc
            and allow_ci_link_trace
            and all(path is not None for path in predecessor_paths)
        ):
            raise ValueError(
                "inactive poll schema mode requires all four exact predecessor inputs"
            )
    elif any(path is not None for path in predecessor_paths):
        raise ValueError("poll predecessor inputs require their exact explicit opt-in")
    _validate_schema_exception_diagnostics(
        schema_exception_diagnostics,
        allow_ui_wall_clock=allow_ui_wall_clock,
        allow_ui_monotonic_clock=allow_ui_monotonic_clock,
        allow_inactive_ui_libc=allow_inactive_ui_libc,
        allow_ci_link_trace=allow_ci_link_trace,
        predecessor_paths=predecessor_paths,
    )
    _validate_schema_stage_profile(
        schema_stage_profile,
        allow_ui_wall_clock=allow_ui_wall_clock,
        allow_ui_monotonic_clock=allow_ui_monotonic_clock,
        allow_inactive_ui_libc=allow_inactive_ui_libc,
        allow_ci_link_trace=allow_ci_link_trace,
        predecessor_paths=predecessor_paths,
    )
    if schema_exception_diagnostics and schema_stage_profile:
        raise ValueError("schema exception and stage-profile modes are separate")
    if (
        not destination.is_absolute()
        or destination.parent.resolve() != destination.parent
        or shutil.disk_usage(destination.parent).free < fetch_schema.DISK_FLOOR
    ):
        raise ValueError("new physical destination and existing disk floor required")
    identities = verify_inputs(
        inputs,
        producer,
        rpds,
        validation,
        stdlib,
        allow_ui_wall_clock=allow_ui_wall_clock,
        allow_ui_monotonic_clock=allow_ui_monotonic_clock,
        allow_inactive_ui_libc=allow_inactive_ui_libc,
        allow_ci_link_trace=allow_ci_link_trace,
    )
    poll_predecessor = None
    if allow_inactive_ui_libc:
        cli = runtime_inputs.report(producer / "cli-build.json")
        source_receipt = json.loads((producer / "cli-source.json").read_bytes())
        poll_predecessor, poll_identities = verify_inactive_poll_predecessors(
            poll_bundle,
            poll_authentication,
            poll_reference_report,
            poll_reference_authentication,
            producer,
            cli,
            source_receipt,
        )
        identities.update(poll_identities)
    destination.mkdir(mode=0o700)
    work = destination / "work"
    work.mkdir(mode=0o700)
    for name in ("home", "tmp", "empty-venv"):
        (work / name).mkdir(mode=0o700)
    source = HERE / "schema_probe"
    scripts = fetch_sdk.inventory(source)
    native_prefix = Path(sys.base_prefix).resolve(strict=True)
    native_python = Path(sys.executable).resolve(strict=True)
    if not native_python.is_relative_to(native_prefix):
        raise ValueError("native interpreter is outside its explicit read-only prefix")
    records = runtime_inputs.report(inputs / "schema-inputs.json")["inputs"]
    control = destination / "control.json"
    control_data = {
        "inputs": records,
        "extension_sha256": EXTENSION_SHA256,
        "input_limit": INPUT_LIMIT,
        "stdlib": runtime_inputs.report(stdlib / "stdlib-inputs.json"),
        "allow_ui_wall_clock": allow_ui_wall_clock,
        "allow_ui_monotonic_clock": allow_ui_monotonic_clock,
        "native_paths": [
            "/inputs/packages/"
            + (
                "rpds-py-native"
                if pin["role"] == "native-oracle"
                else pin["distribution"]
            )
            for pin in records
        ],
    }
    if allow_ci_link_trace:
        control_data["allow_ci_link_trace"] = True
    if schema_exception_diagnostics:
        control_data.update(
            allow_inactive_ui_libc=True,
            schema_exception_diagnostics=True,
        )
    if schema_stage_profile:
        control_data.update(
            allow_inactive_ui_libc=True,
            schema_stage_profile=True,
        )
    control.write_text(json.dumps(control_data, indent=2) + "\n")
    composition = HERE.parents[1] / "sdk/python/composition.py"
    control_identity = fingerprint(control)
    composition_identity = fingerprint(composition)
    report = {
        "format": "tobkiri.ui-schema-candidate/1",
        "outcome": "pending",
        "inputs": identities,
        "probe_source": scripts,
        "native_python": {
            "version": list(sys.version_info[:3]),
            "executable": fingerprint(native_python),
        },
        "rpds_source_head": RPDS_HEAD,
        "rpds_extension_source": {
            "commit": fetch_rpds.COMMIT,
            "archive_sha256": fetch_rpds.ARCHIVE_SHA256,
            "version": fetch_rpds.VERSION,
            "pyinit": "PyInit_rpds",
        },
        "stages": [],
        "guest_executed": False,
        "native_capture_binding": False,
        "full_setup_gui": False,
        "production_enabled": False,
        "ui_wall_clock_candidate": allow_ui_wall_clock,
        "ui_monotonic_clock_candidate": allow_ui_monotonic_clock,
    }
    if allow_inactive_ui_libc:
        report.update(
            ui_empty_poll_libc_candidate=True,
            ci_link_trace_candidate=True,
            trace_observed=False,
            poll_predecessor=poll_predecessor,
            c_abi_verified=False,
            final_provider_resolution_verified=False,
            cold_schema_verified=False,
        )
    if schema_exception_diagnostics:
        report["schema_exception_diagnostics"] = {
            "enabled": True,
            "diagnostic_only": True,
            "original_failure_must_remain_failure": True,
            "production_enabled": False,
            "provenance_available": False,
        }
    if schema_stage_profile:
        report["schema_stage_profile"] = {
            "enabled": True,
            "diagnostic_only": True,
            "original_acceptance_claimed": False,
            "production_enabled": False,
            "provenance_available": False,
            "profile_available": False,
        }
    try:
        native_recipe = destination / "native.sh"
        native_recipe.write_text(
            "set -eu\nexport PYTHONHASHSEED=0\n"
            + shlex.quote(str(native_python))
            + " -I -S -B /probe-source/native_oracle.py\n"
        )
        command = sandbox_command(
            None,
            work,
            native_recipe,
            (
                (native_prefix, str(native_prefix)),
                (source, "/probe-source"),
                (inputs, "/inputs"),
                (control, "/control.json"),
            ),
            address_space_bytes=4 * GIB,
        )
        report["stages"].append(
            run_stage("schema-original-native", command, work, destination / "native")
        )
        native = fingerprint(work / "native.json", 4 * 1024 * 1024)
        wasm_recipe = destination / "wasm.sh"
        wasm_recipe.write_text(
            "set -eu\numask 077\n/usr/bin/python3 -I -S -B /probe-source/driver.py\n"
        )
        command = sandbox_command(
            None,
            work,
            wasm_recipe,
            (
                (source, "/probe-source"),
                (inputs, "/inputs"),
                (control, "/control.json"),
                (rpds / "work/stage/rpds.abi3.so", "/rpds.abi3.so"),
                (stdlib, "/stdlib"),
                (producer / "work/target/release/componentize-py", "/producer"),
                (validation / "wasmtime", "/validation-deps"),
                (validation / "wac-cli", "/wac"),
                (composition, "/composition.py"),
                (work / "empty-venv", "/empty-venv"),
            ),
            address_space_bytes=8 * GIB,
        )
        report["stages"].append(
            run_stage(
                "schema-component-import-run", command, work, destination / "wasm"
            )
        )
        observed = runtime_inputs.report(work / "schema.json")
        if (
            observed.get("passed") is not True
            or fingerprint(work / "native.json") != native
        ):
            raise ValueError("unchanged original schema observations did not match")
        if allow_ci_link_trace:
            trace = observed.get("link_input_trace")
            if (
                not isinstance(trace, dict)
                or trace.get("format") != "tobkiri.ci-link-input-observation/1"
                or trace.get("actual_input_observation") is not True
                or trace.get("stage")
                != "pre-snapshot Linker output and final CLI component recorded separately"
                or not isinstance(trace.get("libraries"), list)
                or not 1 <= len(trace["libraries"]) <= 128
                or not isinstance(trace.get("preinit_component"), dict)
                or not isinstance(trace.get("final_component"), dict)
                or trace.get("final_component")
                != fingerprint(work / "stage/component.wasm", 32 * 1024 * 1024)
                or trace.get("final_provider_resolution_verified") is not False
                or trace.get("production_enabled") is not False
            ):
                raise ValueError("actual schema component trace binding is incomplete")
            report["trace_observed"] = True
            report["schema_link_input_trace"] = {
                "format": trace["format"],
                "libraries": len(trace["libraries"]),
                "total_bytes": trace["total_bytes"],
                "preinit_component": trace["preinit_component"],
                "final_component": trace["final_component"],
                "actual_input_observation": trace["actual_input_observation"],
                "final_provider_resolution_verified": False,
                "production_enabled": False,
            }
        elif observed.get("link_input_trace") is not None:
            raise ValueError("unrequested schema Linker trace appeared")
        identities_after = verify_inputs(
            inputs,
            producer,
            rpds,
            validation,
            stdlib,
            allow_ui_wall_clock=allow_ui_wall_clock,
            allow_ui_monotonic_clock=allow_ui_monotonic_clock,
            allow_inactive_ui_libc=allow_inactive_ui_libc,
            allow_ci_link_trace=allow_ci_link_trace,
        )
        if allow_inactive_ui_libc:
            cli = runtime_inputs.report(producer / "cli-build.json")
            source_receipt = json.loads((producer / "cli-source.json").read_bytes())
            predecessor_after, poll_identities = verify_inactive_poll_predecessors(
                poll_bundle,
                poll_authentication,
                poll_reference_report,
                poll_reference_authentication,
                producer,
                cli,
                source_receipt,
            )
            if predecessor_after != poll_predecessor:
                raise ValueError("inactive poll/C predecessor identity changed")
            identities_after.update(poll_identities)
        if (
            identities_after != identities
            or fetch_sdk.inventory(source) != scripts
            or fingerprint(control) != control_identity
            or fingerprint(composition) != composition_identity
        ):
            raise ValueError("captured schema input, source, or control changed")
        report.update(
            outcome="success",
            guest_executed=True,
            observation=fingerprint(work / "schema.json"),
            original=native,
        )
    except BaseException:
        report["outcome"] = "failed"
        raise
    finally:
        if schema_exception_diagnostics:
            diagnostic_report = report["schema_exception_diagnostics"]
            provenance_path = work / "schema-exception-diagnostics.json"
            try:
                if provenance_path.is_symlink() or not provenance_path.is_file():
                    raise ValueError("diagnostic app provenance file is unavailable")
                provenance_identity = fingerprint(provenance_path, 512 * 1024)
                with provenance_path.open("rb") as stream:
                    provenance_raw = stream.read(512 * 1024 + 1)
                if len(provenance_raw) > 512 * 1024:
                    raise ValueError("diagnostic app provenance exceeds its bound")
                if (
                    len(provenance_raw) != provenance_identity["bytes"]
                    or hashlib.sha256(provenance_raw).hexdigest()
                    != provenance_identity["sha256"]
                    or fingerprint(provenance_path, 512 * 1024) != provenance_identity
                ):
                    raise ValueError("diagnostic app provenance changed while read")
                provenance = json.loads(provenance_raw)
                if (
                    not isinstance(provenance, dict)
                    or provenance.get("format")
                    != "tobkiri.schema-exception-diagnostic-app/1"
                    or provenance.get("diagnostic_only") is not True
                    or provenance.get("production_enabled") is not False
                ):
                    raise ValueError("diagnostic app provenance shape differs")
                diagnostic_report.update(
                    provenance=provenance,
                    provenance_file=provenance_identity,
                    provenance_available=True,
                )
                observation_path = work / "wasm-observation.json"
                if observation_path.is_file() and not observation_path.is_symlink():
                    diagnostic_report["wasm_observation"] = fingerprint(
                        observation_path, 4 * 1024 * 1024
                    )
            except BaseException:
                # Do not replace the original build or guest exception with a
                # secondary failure while reading optional diagnostic evidence.
                diagnostic_report["provenance_available"] = False
        if schema_stage_profile:
            profile_report = report["schema_stage_profile"]

            def read_profile_json(path: Path, maximum: int) -> tuple[dict, dict]:
                if path.is_symlink() or not path.is_file():
                    raise ValueError(
                        "schema stage-profile evidence file is unavailable"
                    )
                identity = fingerprint(path, maximum)
                with path.open("rb") as stream:
                    raw = stream.read(maximum + 1)
                if (
                    len(raw) > maximum
                    or len(raw) != identity["bytes"]
                    or hashlib.sha256(raw).hexdigest() != identity["sha256"]
                    or fingerprint(path, maximum) != identity
                ):
                    raise ValueError("schema stage-profile evidence changed while read")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("schema stage-profile evidence is not an object")
                return value, identity

            try:
                provenance, provenance_identity = read_profile_json(
                    work / "schema-stage-profile-provenance.json", 1024 * 1024
                )
                if (
                    provenance.get("format")
                    != "tobkiri.schema-stage-profile-derivation/1"
                    or provenance.get("diagnostic_only") is not True
                    or provenance.get("original_acceptance_claimed") is not False
                    or provenance.get("production_enabled") is not False
                ):
                    raise ValueError("schema profile derivation provenance differs")
                profile_report.update(
                    provenance_file=provenance_identity,
                    provenance_format=provenance["format"],
                    derived_source_files={
                        name: {
                            "baseline": row["baseline"],
                            "derived": row["derived"],
                            "diff_sha256": row["diff_sha256"],
                        }
                        for name, row in provenance["files"].items()
                    },
                    provenance_available=True,
                )
            except BaseException:
                profile_report["provenance_available"] = False
            try:
                profile, profile_identity = read_profile_json(
                    work / "schema-stage-profile.json", 1024 * 1024
                )
                if (
                    profile.get("format") != "tobkiri.schema-stage-profile/1"
                    or profile.get("diagnostic_only") is not True
                    or profile.get("production_enabled") is not False
                    or profile.get("outcome") != "diagnostic_only"
                ):
                    raise ValueError("schema runtime profile report differs")
                profile_report.update(
                    profile_file=profile_identity,
                    profile_summary={
                        name: profile[name]
                        for name in (
                            "execution_status",
                            "event_count",
                            "metadata_bytes",
                            "get_code_call_count",
                            "get_code_completed_count",
                            "shared_callback_budget",
                            "shared_callback_budget_healthy",
                            "fuel_observations_monotonic",
                            "callback_failed",
                        )
                    },
                    profile_available=True,
                )
            except BaseException:
                profile_report["profile_available"] = False
            observation_path = work / "wasm-observation.json"
            if observation_path.is_file() and not observation_path.is_symlink():
                try:
                    profile_report["wasm_observation"] = fingerprint(
                        observation_path, 4 * 1024 * 1024
                    )
                except BaseException:
                    pass
        (destination / "schema-build.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("inputs", "producer", "rpds", "validation", "stdlib", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--allow-ui-wall-clock", action="store_true")
    parser.add_argument("--allow-ui-monotonic-clock", action="store_true")
    parser.add_argument("--allow-inactive-ui-libc", action="store_true")
    parser.add_argument("--allow-ci-link-trace", action="store_true")
    parser.add_argument("--schema-exception-diagnostics", action="store_true")
    parser.add_argument("--schema-stage-profile", action="store_true")
    for name in (
        "poll-bundle",
        "poll-authentication",
        "poll-reference-report",
        "poll-reference-authentication",
    ):
        parser.add_argument("--" + name, type=Path)
    run(**vars(parser.parse_args()))
