"""Inactive v2 Pack byte-validation tests, never guest or native proof.

The synthetic component header and fabricated receipt only exercise structural
validation. They cannot establish producer, Wasm or Core execution success.
"""

from __future__ import annotations

import copy
import ctypes
import errno
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/wasm"))
import materialize_ordered_tool_bridge_v2 as materializer
import replay_ordered_tool_bridge_v2 as replay
import stage_ordered_tool_bridge_v2 as staging

ORACLE = staging.PACK / "integer-v2.observations.json"
InputFixture = tuple[Path, Path, dict[str, str]]


@pytest.fixture
def inputs(tmp_path: Path) -> InputFixture:
    """Create inert structural inputs, never compile or execute a component."""
    staged = tmp_path / "stage"
    plan = staging.stage(staged)
    artifact = tmp_path / "structural-fixture-only.wasm"
    artifact.write_bytes(b"\0asm\x0d\0\x01\0")
    manifest = [
        {**item, "bytes": (staged / item["source"]).stat().st_size}
        for item in plan["sources"]
    ]
    receipt = {
        "format": "tobkiri.component-build/2",
        "language": "python",
        "artifact_sha256": materializer.digest(artifact.read_bytes()),
        "artifact_bytes": artifact.stat().st_size,
        "build_plan_sha256": materializer.digest((staged / "plan.json").read_bytes()),
        "source_manifest": manifest,
        "source_manifest_sha256": materializer.digest(materializer.canonical(manifest)),
        "world": plan["world"],
        "imports": sorted(plan["allowed_imports"]),
        "exports": sorted(plan["outer_exports"]),
    }
    path = artifact.with_suffix(".build.json")
    path.write_bytes(materializer.canonical(receipt))
    return artifact, path, {
        "artifact_sha256": materializer.digest(artifact.read_bytes()),
        "receipt_sha256": materializer.digest(path.read_bytes()),
    }


def run(inputs: InputFixture, output: Path, oracle: Path = ORACLE) -> dict[str, Any]:
    """Run only materialization of the synthetic, non-runnable test fixture."""
    artifact, _, pins = inputs
    return materializer.materialize(artifact, oracle, output, **pins)


def rewrite_receipt(inputs: InputFixture, receipt: dict[str, Any]) -> None:
    """Re-pin deliberately mutated receipts to test more than the caller hash."""
    _, path, pins = inputs
    path.write_bytes(materializer.canonical(receipt))
    pins["receipt_sha256"] = materializer.digest(path.read_bytes())


def test_exact_versioned_mapping_file_hashes_and_inactive_scope(
    inputs: InputFixture, tmp_path: Path
) -> None:
    output = tmp_path / "pack"
    report = run(inputs, output)
    manifest_bytes = (output / "pack.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest["format"] == "tobkiri.pack/1"
    assert manifest["id"] == "local.ai-tool-bridge-ordered-python"
    assert manifest["version"] == "2.0.0"
    assert manifest["publisher_key"] is None
    assert manifest["shapes"] == [
        {
            "name": materializer.SHAPE,
            "version": "2.0",
            "kind": "port",
            "wit": "wit/pack-v2.wit",
        }
    ]
    assert manifest["ports"] == {
        "provides": [
            {"shape": materializer.SHAPE, "version": "2.0", "optional": False}
        ],
        "consumes": [],
        "untyped": [],
    }
    assert manifest["runtime"] == {
        "kind": "wasm",
        "component": "component.wasm",
        "toolchain": "python/build-receipt-pinned",
        "config": {"path": "config.json"},
    }
    assert (output / "config.json").read_bytes() == b"{}"
    assert json.loads((output / "wit/bridge.shape.json").read_bytes()) == {
        "cardinality": "one", "streams": {}, "callbacks": {}, "conformance": None
    }
    permissions = manifest["permissions"]
    assert permissions["ports"] == []
    assert permissions["native"] is False
    assert permissions["network"] == {"origins": []}
    assert permissions["fs"] == permissions["host_io"] == permissions["proc"] == []
    assert manifest["inputs"] == manifest["flows"] == manifest["overlays"] == []
    assert manifest["stores"] == [] and manifest["ui"] is None
    expected_files = {
        "component.wasm", "component.build.json", "config.json",
        "wit/pack-v2.wit", "wit/deps/core.wit", "wit/bridge.shape.json",
    }
    assert {item["path"] for item in manifest["files"]} == expected_files
    assert {
        path.relative_to(output).as_posix()
        for path in output.rglob("*") if path.is_file()
    } == expected_files | {"pack.json"}
    for item in manifest["files"]:
        data = (output / item["path"]).read_bytes()
        assert item["size"] == len(data)
        assert item["sha256"] == "sha256:" + materializer.digest(data)
    assert (output / "component.wasm").read_bytes() == inputs[0].read_bytes()
    assert (output / "component.build.json").read_bytes() == inputs[1].read_bytes()
    assert (output / "wit/pack-v2.wit").read_bytes() == (
        staging.PACK / "wit/pack-v2.wit"
    ).read_bytes()
    assert (output / "wit/deps/core.wit").read_bytes() == (
        ROOT / "crates/tobkiri-wit/wit/core/core.wit"
    ).read_bytes()
    assert report["pack_manifest_sha256"] == materializer.digest(manifest_bytes)
    assert report["format"] == "tobkiri.ordered-python-pack-materialization/2"
    assert report["validation_scope"] == "byte-identity-only-not-execution"
    assert report["protocol"] == "ordered-finite-json-v2"
    assert report["interface"] == staging.INTERFACE
    assert report["pack_id"] == manifest["id"]
    assert report["pack_version"] == "2.0.0"
    assert report["shape"] == materializer.SHAPE
    assert report["shape_version"] == "2.0"
    assert report["case_count"] == 33
    assert report["frozen_case_counts"] == {
        "success": 18, "original-invalid": 12, "excluded-not-parity": 3
    }
    assert report["artifact_sha256"] == inputs[2]["artifact_sha256"]
    assert report["receipt_sha256"] == inputs[2]["receipt_sha256"]
    observations, binding = staging.reviewed_inputs()
    document = json.loads(observations)
    assert report["source_observations_sha256"] == staging.OBSERVATIONS_SHA256
    assert report["source_observations_bindings_sha256"] == (
        document["source_bindings_sha256"]
    )
    assert report["source_stage_bindings_sha256"] == materializer.digest(binding)
    assert report["original_oracle_sha256"] == document["oracle_sha256"]
    assert report["original_frozen_inputs_sha256"] == document["oracle_freeze_sha256"]
    assert report["outer_budget"] == materializer.envelope_bounds(document)
    for key in (
        "build_performed", "wasm_execution_performed", "native_core_acceptance",
        "core_runtime_acceptance", "full_host_replay_performed", "production_enabled",
    ):
        assert report[key] is False
    assert not list(tmp_path.glob(".ordered-pack-v2-*"))


def test_fabricated_receipt_execution_claims_never_become_acceptance(
    inputs: InputFixture, tmp_path: Path
) -> None:
    receipt = json.loads(inputs[1].read_bytes())
    receipt.update(
        build_performed=True, wasm_execution_performed=True,
        native_core_acceptance=True, core_runtime_acceptance=True,
        full_host_replay_performed=True, production_enabled=True,
        replay_succeeded=True,
    )
    rewrite_receipt(inputs, receipt)
    report = run(inputs, tmp_path / "pack")
    for key in (
        "build_performed", "wasm_execution_performed", "native_core_acceptance",
        "core_runtime_acceptance", "full_host_replay_performed", "production_enabled",
    ):
        assert report[key] is False
    assert "replay_succeeded" not in report


@pytest.mark.parametrize("target", [0, 1])
@pytest.mark.parametrize("mutation", ["missing", "tamper", "symlink"])
def test_bad_component_or_receipt_fails_before_publish(
    inputs: InputFixture, tmp_path: Path, target: int, mutation: str
) -> None:
    path = inputs[target]
    if mutation == "tamper":
        path.write_bytes(path.read_bytes() + b" ")
    else:
        retained = path.with_name(path.name + ".retained")
        path.rename(retained)
        if mutation == "symlink":
            path.symlink_to(retained)
    with pytest.raises(ValueError):
        run(inputs, tmp_path / "pack")
    assert not (tmp_path / "pack").exists()
    assert not list(tmp_path.glob(".ordered-pack-v2-*"))


@pytest.mark.parametrize("key", ["artifact_sha256", "receipt_sha256"])
@pytest.mark.parametrize("pin", ["0" * 64, "ABCDEF" * 10 + "ABCD", "short"])
def test_wrong_or_noncanonical_explicit_pin_fails(
    inputs: InputFixture, tmp_path: Path, key: str, pin: str
) -> None:
    inputs[2][key] = pin
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        run(inputs, tmp_path / "pack")
    assert not (tmp_path / "pack").exists()


@pytest.mark.parametrize(
    "mutation", ["duplicate-key", "duplicate-source", "missing-source", "v1-abi"]
)
def test_reforged_receipt_still_requires_exact_reviewed_v2_plan(
    inputs: InputFixture, tmp_path: Path, mutation: str
) -> None:
    receipt = json.loads(inputs[1].read_bytes())
    if mutation == "duplicate-key":
        inputs[1].write_bytes(b'{"format":"duplicate",' + inputs[1].read_bytes()[1:])
        inputs[2]["receipt_sha256"] = materializer.digest(inputs[1].read_bytes())
    else:
        if mutation == "duplicate-source":
            receipt["source_manifest"].append(
                copy.deepcopy(receipt["source_manifest"][0])
            )
        elif mutation == "missing-source":
            receipt["source_manifest"].pop()
        else:
            receipt["exports"] = [
                "tobkiri:core/lifecycle@1.0.0",
                staging.INTERFACE.replace("2.0.0", "1.0.0"),
            ]
        receipt["source_manifest_sha256"] = materializer.digest(
            materializer.canonical(receipt["source_manifest"])
        )
        rewrite_receipt(inputs, receipt)
    with pytest.raises(ValueError):
        run(inputs, tmp_path / "pack")
    assert not (tmp_path / "pack").exists()


@pytest.mark.parametrize(
    "source", ["wit/pack-v2.wit", "wit/deps/core.wit", "reviewed-inputs-v2.json"]
)
@pytest.mark.parametrize("field", ["sha256", "bytes"])
def test_receipt_requires_exact_wit_and_source_stage_binding(
    inputs: InputFixture, tmp_path: Path, source: str, field: str
) -> None:
    receipt = json.loads(inputs[1].read_bytes())
    entry = next(
        item for item in receipt["source_manifest"] if item["destination"] == source
    )
    entry[field] = "0" * 64 if field == "sha256" else entry[field] + 1
    receipt["source_manifest_sha256"] = materializer.digest(
        materializer.canonical(receipt["source_manifest"])
    )
    rewrite_receipt(inputs, receipt)
    with pytest.raises(ValueError, match="complete reviewed v2 plan"):
        run(inputs, tmp_path / "pack")
    assert not (tmp_path / "pack").exists()


@pytest.mark.parametrize("mutation", ["v1", "changed", "symlink", "missing"])
def test_only_exact_v2_observations_are_accepted(
    inputs: InputFixture, tmp_path: Path, mutation: str
) -> None:
    path = tmp_path / "observations.json"
    if mutation == "v1":
        path.write_bytes((staging.PACK / "oracle.json").read_bytes())
    elif mutation == "changed":
        path.write_bytes(ORACLE.read_bytes() + b"\n")
    elif mutation == "symlink":
        path.symlink_to(ORACLE)
    with pytest.raises(ValueError):
        run(inputs, tmp_path / "pack", path)
    assert not (tmp_path / "pack").exists()


@pytest.mark.parametrize("target", ["artifact", "destination", "oracle"])
def test_symlink_ancestor_fails_closed(
    inputs: InputFixture, tmp_path: Path, target: str
) -> None:
    link = tmp_path / "linked"
    link.symlink_to(tmp_path, target_is_directory=True)
    artifact, receipt, pins = inputs
    output, oracle = tmp_path / "pack", ORACLE
    if target == "artifact":
        artifact = link / artifact.name
    elif target == "destination":
        output = link / output.name
    else:
        copied = tmp_path / "observations.json"
        copied.write_bytes(ORACLE.read_bytes())
        oracle = link / copied.name
    with pytest.raises(ValueError, match="symlink"):
        run((artifact, receipt, pins), output, oracle)
    assert not output.exists()


@pytest.mark.parametrize("kind", ["directory", "file", "dangling-symlink"])
def test_existing_output_never_replaced(
    inputs: InputFixture, tmp_path: Path, kind: str
) -> None:
    output = tmp_path / "pack"
    if kind == "directory":
        output.mkdir()
        (output / "keep").write_text("unchanged")
    elif kind == "file":
        output.write_text("unchanged")
    else:
        output.symlink_to(tmp_path / "absent")
    with pytest.raises(ValueError, match="fresh|symlink"):
        run(inputs, output)
    if kind == "directory":
        assert (output / "keep").read_text() == "unchanged"
    elif kind == "file":
        assert output.read_text() == "unchanged"
    else:
        assert output.is_symlink()


def test_missing_output_parent_is_not_created(
    inputs: InputFixture, tmp_path: Path
) -> None:
    output = tmp_path / "absent" / "pack"
    with pytest.raises(ValueError, match="existing trusted parent"):
        run(inputs, output)
    assert not output.parent.exists()


@pytest.mark.parametrize("wit", ["pack", "core"])
@pytest.mark.parametrize("mutation", ["tamper", "symlink"])
def test_wit_recapture_remains_bound_after_source_plan_validation(
    inputs: InputFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    wit: str, mutation: str,
) -> None:
    if wit == "pack":
        root = tmp_path / "pack-sources"
        relative = Path("wit/pack-v2.wit")
        source = staging.PACK / relative
        monkeypatch.setattr(materializer, "PACK", root)
    else:
        root = tmp_path / "core-sources"
        relative = Path("crates/tobkiri-wit/wit/core/core.wit")
        source = ROOT / relative
        monkeypatch.setattr(materializer, "ROOT", root)
    path = root / relative
    path.parent.mkdir(parents=True)
    if mutation == "tamper":
        path.write_bytes(source.read_bytes() + b"\n")
    else:
        path.symlink_to(source)
    with pytest.raises(ValueError, match="changed after source-plan|symlink"):
        run(inputs, tmp_path / "pack")
    assert not (tmp_path / "pack").exists()


@pytest.mark.parametrize("target", ["observations", "binding"])
def test_recaptured_source_evidence_cannot_drift(
    inputs: InputFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    observations, binding = staging.reviewed_inputs()
    if target == "observations":
        observations += b"\n"
    else:
        binding += b"\n"
    monkeypatch.setattr(
        materializer, "reviewed_inputs", lambda: (observations, binding)
    )
    with pytest.raises(ValueError, match="changed after"):
        run(inputs, tmp_path / "pack")
    assert not (tmp_path / "pack").exists()


def test_native_member_limit_is_passed_before_artifact_capture(
    inputs: InputFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = replay.bounded_read
    limits = []

    def bounded(path: Path, limit: int) -> bytes:
        if path == inputs[0]:
            limits.append(limit)
            raise ValueError("oversized evidence file")
        return original(path, limit)

    monkeypatch.setattr(replay, "bounded_read", bounded)
    with pytest.raises(ValueError, match="oversized"):
        run(inputs, tmp_path / "pack")
    assert limits == [128 * 1024 * 1024]
    assert not (tmp_path / "pack").exists()


def test_bounded_reader_never_reads_more_than_limit_plus_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bounded.wasm"
    path.write_bytes(b"x")
    stream = MagicMock()
    stream.__enter__.return_value = stream
    stream.read.return_value = b"x"
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: stream)
    assert staging.bounded_read(path, 7) == b"x"
    stream.read.assert_called_once_with(8)


@pytest.mark.parametrize("target", ["artifact", "receipt", "oracle"])
def test_oversized_evidence_is_rejected_before_stream_read(
    inputs: InputFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """Sparse size fixtures test bounds without allocating or reading big data."""
    oracle = ORACLE
    if target == "artifact":
        path, limit = inputs[0], 128 * 1024 * 1024
    elif target == "receipt":
        path, limit = inputs[1], 16 * 1024 * 1024
    else:
        oracle = tmp_path / "oversized-observations.json"
        path, limit = oracle, 16 * 1024 * 1024
    with path.open("wb") as stream:
        stream.truncate(limit + 1)
    original_open = Path.open

    def open_file(current: Path, *args: Any, **kwargs: Any) -> Any:
        if current == path:
            pytest.fail("an oversized evidence file must not be opened")
        return original_open(current, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(ValueError, match="oversized"):
        run(inputs, tmp_path / "pack", oracle)
    assert not (tmp_path / "pack").exists()


@pytest.mark.parametrize("binary", [b"", b"\0asm\x01\0\0\0", b"not a component"])
def test_wrong_component_header_is_not_materialized_even_with_reforged_receipt(
    inputs: InputFixture, tmp_path: Path, binary: bytes
) -> None:
    inputs[0].write_bytes(binary)
    receipt = json.loads(inputs[1].read_bytes())
    receipt["artifact_sha256"] = materializer.digest(binary)
    receipt["artifact_bytes"] = len(binary)
    inputs[2]["artifact_sha256"] = materializer.digest(binary)
    rewrite_receipt(inputs, receipt)
    with pytest.raises(ValueError, match="empty|not a Wasm component"):
        run(inputs, tmp_path / "pack")
    assert not (tmp_path / "pack").exists()


@pytest.mark.parametrize("side", ["request", "response"])
def test_outer_escaping_budget_is_not_the_inner_string_budget(side: str) -> None:
    case = {
        "operation": "normalize", "input_json": "{}", "classification": "success",
        "output_json": "{}",
    }
    case["input_json" if side == "request" else "output_json"] = '"' * (3 * 1024 * 1024)
    with pytest.raises(ValueError, match="outer-envelope"):
        materializer.envelope_bounds({"cases": [case]})


@pytest.mark.parametrize("side", ["request", "response"])
def test_outer_budget_accepts_exact_limit_and_rejects_one_byte_more(side: str) -> None:
    case = {
        "operation": "x", "input_json": "", "classification": "success",
        "output_json": "",
    }
    key = "input_json" if side == "request" else "output_json"
    overhead = (
        len(materializer.canonical({"operation": "x", "payload": ""}))
        if side == "request" else len(materializer.canonical(""))
    )
    case[key] = "x" * (materializer.MAX_OUTER_BYTES - overhead)
    bounds = materializer.envelope_bounds({"cases": [case]})
    measured = (
        "max_request_bytes" if side == "request" else "max_success_response_bytes"
    )
    assert bounds[measured] == materializer.MAX_OUTER_BYTES
    case[key] += "x"
    with pytest.raises(ValueError, match="outer-envelope"):
        materializer.envelope_bounds({"cases": [case]})


def test_outer_budget_keeps_integer_digits_duplicates_and_order_opaque() -> None:
    value = '{"z":123456789012345678901234567890,"a":-0.0,"a":1.0}'
    observations = {"cases": [
        {
            "operation": "x", "input_json": value,
            "classification": "success", "output_json": value,
        },
        {
            "operation": "x", "input_json": "{}",
            "classification": "excluded-not-parity", "output_json": "NaN",
        },
        {
            "operation": "x", "input_json": "{}",
            "classification": "original-invalid",
        },
    ]}
    before = copy.deepcopy(observations)
    measured = materializer.envelope_bounds(observations)
    assert measured["max_success_response_bytes"] == len(materializer.canonical(value))
    assert measured["max_request_bytes"] == len(
        materializer.canonical({"operation": "x", "payload": value})
    )
    assert observations == before


@pytest.mark.parametrize("failure", ["write", "rename", "appeared", "symlink"])
def test_partial_failure_never_publishes_and_cleans_private_partial(
    inputs: InputFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    output = tmp_path / "pack"
    original_open = Path.open

    def open_file(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path.name == "pack.json" and path.parent.name.startswith(
            ".ordered-pack-v2-"
        ):
            if failure == "write":
                raise OSError("injected write failure")
            if failure == "appeared":
                output.mkdir()
                (output / "keep").write_text("unchanged")
            elif failure == "symlink":
                output.symlink_to(tmp_path / "absent")
        return original_open(path, *args, **kwargs)

    def rename(source: Path, destination: Path) -> None:
        raise OSError("injected publication failure")

    monkeypatch.setattr(Path, "open", open_file)
    if failure == "rename":
        monkeypatch.setattr(materializer, "_publish_no_replace", rename)
    with pytest.raises((ValueError, OSError)):
        run(inputs, output)
    assert not list(tmp_path.glob(".ordered-pack-v2-*"))
    if failure == "appeared":
        assert (output / "keep").read_text() == "unchanged"
        assert not (output / "pack.json").exists()
    elif failure == "symlink":
        assert output.is_symlink()
        assert not (tmp_path / "absent").exists()
    else:
        assert not output.exists()


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_final_publication_race_cannot_replace_a_new_destination(
    inputs: InputFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Inject after the final exists check, not merely before it."""
    output = tmp_path / "pack"
    publish = materializer._publish_no_replace
    inode = []

    def race(source: Path, destination: Path) -> None:
        if kind == "directory":
            destination.mkdir()
        elif kind == "file":
            destination.write_bytes(b"unchanged")
        else:
            destination.symlink_to(tmp_path / "absent")
        inode.append(destination.lstat().st_ino)
        publish(source, destination)

    monkeypatch.setattr(materializer, "_publish_no_replace", race)
    with pytest.raises(FileExistsError):
        run(inputs, output)
    assert output.lstat().st_ino == inode[0]
    assert not list(tmp_path.glob(".ordered-pack-v2-*"))
    if kind == "directory":
        assert list(output.iterdir()) == []
    elif kind == "file":
        assert output.read_bytes() == b"unchanged"
    else:
        assert output.is_symlink()
        assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_unavailable_atomic_publication_fails_closed_and_cleans_partial(
    inputs: InputFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr(materializer.sys, "platform", platform)
    monkeypatch.setattr(materializer.ctypes, "CDLL", lambda *_args, **_kwargs: object())
    with pytest.raises(OSError, match="atomic no-replace publication requires"):
        run(inputs, tmp_path / "pack")
    assert not (tmp_path / "pack").exists()
    assert not list(tmp_path.glob(".ordered-pack-v2-*"))


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize(
    "error", [0, errno.EEXIST, errno.EINVAL, errno.ENOTSUP, errno.ENOSYS]
)
def test_atomic_publication_uses_exact_platform_abi_and_exclusive_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, error: int
) -> None:
    """Test libc call construction, not macOS filesystem execution on Linux."""
    function = MagicMock(return_value=-1 if error else 0)
    symbol = "renameat2" if platform == "linux" else "renamex_np"
    library = SimpleNamespace(**{symbol: function})
    load = MagicMock(return_value=library)
    monkeypatch.setattr(materializer.sys, "platform", platform)
    monkeypatch.setattr(materializer.ctypes, "CDLL", load)
    monkeypatch.setattr(materializer.ctypes, "get_errno", lambda: error)
    source, destination = tmp_path / "source", tmp_path / "destination"
    if error:
        with pytest.raises(OSError) as raised:
            materializer._publish_no_replace(source, destination)
        assert raised.value.errno == error
        assert raised.value.filename == str(destination)
    else:
        materializer._publish_no_replace(source, destination)
    load.assert_called_once_with(None, use_errno=True)
    assert function.restype is ctypes.c_int
    if platform == "linux":
        assert function.argtypes == [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
            ctypes.c_char_p, ctypes.c_uint,
        ]
        function.assert_called_once_with(
            -100, bytes(source), -100, bytes(destination), 1
        )
    else:
        assert function.argtypes == [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint
        ]
        function.assert_called_once_with(bytes(source), bytes(destination), 4)


def test_unsupported_platform_never_attempts_a_clobbering_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    load = MagicMock()
    monkeypatch.setattr(materializer.sys, "platform", "unsupported-test-platform")
    monkeypatch.setattr(materializer.ctypes, "CDLL", load)
    with pytest.raises(OSError, match="unsupported") as raised:
        materializer._publish_no_replace(tmp_path / "source", tmp_path / "destination")
    assert raised.value.errno == errno.ENOTSUP
    load.assert_not_called()


@pytest.mark.parametrize("target", ["source", "destination"])
def test_publication_rejects_embedded_nul_before_libc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    load = MagicMock()
    monkeypatch.setattr(materializer.ctypes, "CDLL", load)
    source, destination = tmp_path / "source", tmp_path / "destination"
    if target == "source":
        source = source.with_name(source.name + "\0ignored-suffix")
    else:
        destination = destination.with_name(destination.name + "\0ignored-suffix")
    with pytest.raises(ValueError, match="embedded null byte"):
        materializer._publish_no_replace(source, destination)
    load.assert_not_called()


def test_nul_destination_never_publishes_a_truncated_path(
    inputs: InputFixture, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="embedded null byte"):
        run(inputs, tmp_path / "pack\0ignored-suffix")
    assert not (tmp_path / "pack").exists()
    assert not list(tmp_path.glob(".ordered-pack-v2-*"))


def test_two_fresh_candidates_have_identical_bytes(
    inputs: InputFixture, tmp_path: Path
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    assert run(inputs, first) == run(inputs, second)
    assert {
        path.relative_to(first): path.read_bytes()
        for path in first.rglob("*") if path.is_file()
    } == {
        path.relative_to(second): path.read_bytes()
        for path in second.rglob("*") if path.is_file()
    }
