"""Fresh-interpreter native comparison; bootstrap imports are deliberately closed."""

import sys
import os
import json

FORMAT = "tobkiri.schema-kernel-native-worker/1"
MAX_CONTROL = 4 * 1024 * 1024


def _read(path: str, maximum: int) -> bytes:
    """Read one bounded regular file without importing path/tooling modules."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if before.st_mode & 0o170000 != 0o100000 or before.st_size > maximum:
            raise ValueError("bounded regular worker input required")
        chunks, size = [], 0
        while True:
            chunk = os.read(descriptor, min(65536, maximum - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > maximum:
                raise ValueError("native worker input bound exceeded")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if any(getattr(before, name) != getattr(after, name) for name in fields):
            raise ValueError("native worker input changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _control(path: str, output: str) -> dict:
    value = json.loads(_read(path, MAX_CONTROL))
    fields = {"source", "inputs", "reference", "extension", "module_dir",
              "package_paths", "stdlib_paths", "stdlib_overlay", "profile_calls",
              "extension_identity", "output"}
    if type(value) is not dict or set(value) != fields:
        raise ValueError("closed native worker control required")
    paths = [value[name] for name in (
        "source", "inputs", "reference", "extension", "module_dir",
        "stdlib_overlay", "output",
    )]
    for name, minimum, maximum in (("package_paths", 6, 6), ("stdlib_paths", 1, 3)):
        items = value[name]
        if type(items) is not list or not minimum <= len(items) <= maximum:
            raise ValueError("complete native worker import paths required")
        paths.extend(items)
    if any(type(item) is not str or not os.path.isabs(item)
           or os.path.normpath(item) != item or "\x00" in item for item in paths):
        raise ValueError("absolute native worker paths required")
    identity = value["extension_identity"]
    if (type(identity) is not dict or set(identity) != {"bytes", "sha256"}
            or type(identity["bytes"]) is not int
            or not 0 < identity["bytes"] <= 32 * 1024 * 1024
            or type(identity["sha256"]) is not str
            or len(identity["sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in identity["sha256"])
            or type(value["profile_calls"]) is not bool
            or value["output"] != output
            or os.path.dirname(value["extension"]) != value["module_dir"]
            or os.path.basename(value["extension"])
            != "_tobkiri_schema_kernel.abi3.so"):
        raise ValueError("native worker extension/output identity differs")
    return value


def _write_result(path: str, receipt: dict) -> None:
    encoded = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode()
    if len(encoded) > MAX_CONTROL:
        raise ValueError("native worker result bound exceeded")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)


def _compare(control: dict, receipt: dict, kernel: object) -> None:
    """Import tooling only after the extension-first absence guard has passed."""
    receipt["stage"] = "native-bootstrap-extension-proof"
    import hashlib
    from pathlib import Path

    extension = Path(control["extension"])
    raw = _read(str(extension), 32 * 1024 * 1024)
    identity = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if (extension.resolve(strict=True) != extension
            or Path(kernel.__file__).resolve(strict=True) != extension
            or identity != control["extension_identity"]
            or kernel.production_enabled is not False
            or kernel.implemented_handler_count != 36):
        raise ValueError("actual source-built private extension import differs")
    import urllib.parse as parse

    parse_path = Path(control["stdlib_overlay"]) / "urllib" / "parse.py"
    if (Path(parse.__file__).resolve(strict=True) != parse_path
            or getattr(parse, "_tobkiri_translated_coerce_args", False) is not True):
        raise ValueError("stdlib coercion was not translated before its first import")
    receipt["bootstrap"].update(
        extension_identity=identity, urllib_parse_origin=str(parse_path),
        translated_coerce_args=True,
    )
    receipt["stage"] = "native-comparison-imports"
    import importlib.util
    import struct
    from types import SimpleNamespace

    import build_schema_kernel as builder
    import fetch_sdk
    from schema_kernel_observation import first_difference

    args = SimpleNamespace(source=Path(control["source"]),
                           profile_calls=control["profile_calls"])
    source = fetch_sdk.inventory(args.source)
    reference = json.loads(builder.fingerprint_and_read(
        Path(control["reference"]), 4 * 1024 * 1024))
    if (reference.get("format") != "tobkiri.schema-kernel-reference/1"
            or reference.get("oracle_commit")
            != "54e7da008997a3eb45876cbe18459dba9902cc9d"
            or reference.get("original_schema_count") != 42
            or len(reference.get("cases", [])) != 64):
        raise ValueError("complete original 42+22 reference required")
    from jsonschema import Draft202012Validator, exceptions, _keywords
    if getattr(_keywords, "_tobkiri_translated_keywords", False) is not True:
        raise ValueError("public keyword definitions were not translated before import")
    from jsonschema_specifications import REGISTRY
    from schema_kernel_api_checks import (exercise_api, exercise_unsupported_native,
                                          exercise_implementation_diagnostics)
    from schema_kernel_observation import meta_nodes, node_digest, observe

    spec = importlib.util.spec_from_file_location(
        "schema_adapter", args.source / "adapter.py")
    if spec is None or spec.loader is None:
        raise ValueError("actual native adapter source unavailable")
    adapter = importlib.util.module_from_spec(spec)
    sys.modules["schema_adapter"] = adapter
    spec.loader.exec_module(adapter)
    adapter.install_standard_classes()
    receipt["stage"] = "native-metadata-comparison"
    actual_metadata = {
        "schema": node_digest(Draft202012Validator.META_SCHEMA),
        "registry": {uri: node_digest(REGISTRY.contents(uri))
                     for uri in sorted(REGISTRY)},
        "formats": sorted(Draft202012Validator.FORMAT_CHECKER.checkers),
    }
    expected_metadata = {
        "schema": node_digest(reference["meta_schema"]),
        "registry": {row["uri"]: node_digest(row["schema"])
                     for row in reference["meta_registry"]},
        "formats": reference["meta_format_checker_names"],
    }
    difference = first_difference(expected_metadata, actual_metadata)
    if difference is not None:
        receipt["difference"] = difference
        raise ValueError("native meta registry/checker observation differs")
    receipt["metadata"] = actual_metadata
    receipt["stage"] = "native-schema-comparison"
    known_nodes = meta_nodes()
    profiler = None
    if args.profile_calls:
        import cProfile
        profiler = cProfile.Profile()
    coerce_before = kernel.coerce_args_calls()
    for index, case in enumerate(reference["cases"]):
        receipt.update(case_index=index, case_name=case["name"])
        schema = case["schema"]
        try:
            if profiler is None:
                Draft202012Validator.check_schema(schema)
            else:
                # Exactly the same actual call, once per case. Keep observer,
                # metadata preparation and later API probes outside this scope.
                profiler.runcall(Draft202012Validator.check_schema, schema)
        except exceptions.SchemaError as error:
            actual = {"valid": False, "error": observe(error, schema, known_nodes)}
        else:
            actual = {"valid": True, "error": None}
        difference = first_difference(case["expected"], actual)
        if difference is not None:
            receipt["difference"] = difference
            raise ValueError("native schema result differs from original reference")
    if profiler is not None:
        import io
        import pstats
        text = io.StringIO()
        stats = pstats.Stats(profiler, stream=text)
        stats.sort_stats("cumulative").print_stats(35)
        stats.sort_stats("time").print_stats(35)
        report = text.getvalue()
        if len(report.encode()) > 24 * 1024:
            raise ValueError("bounded native call profile exceeded its text limit")
        receipt["call_profile"] = {
            "format": "tobkiri.schema-native-call-profile/1",
            "scope": "only the original 64 check_schema calls; observer/API excluded",
            "native_only": True, "wasm_fuel_measurement": False,
            "benchmark_claim": False, "qualification": False,
            "note": ("cProfile overhead differs for Python and C; use call paths, "
                     "not a speed claim"),
            "top_cumulative_and_self_time": report,
        }
    coerce_entries = kernel.coerce_args_calls() - coerce_before
    if type(coerce_entries) is not int or coerce_entries <= 0:
        raise ValueError("actual schema comparisons did not enter native argument coercion")
    schema_entries = kernel.keyword_entry_count()
    if type(schema_entries) is not int or schema_entries <= 0:
        raise ValueError("original 64 schema calls did not enter Rust definitions")
    receipt["stage"] = "native-api-comparison"
    api = exercise_api()
    if len(api) != 19 or not all(value is True for value in api.values()):
        raise ValueError("all nineteen actual API groups required")
    receipt["stage"] = "native-implementation-diagnostics"
    implementation_diagnostics = builder.validate_implementation_diagnostics(
        exercise_implementation_diagnostics())
    receipt["stage"] = "native-unsupported-comparison"
    unsupported = exercise_unsupported_native()
    entries = kernel.keyword_entry_count()
    if type(entries) is not int or entries <= 0:
        raise ValueError("actual public keyword definitions did not enter Rust")
    if fetch_sdk.inventory(args.source) != source:
        raise ValueError("native kernel source changed during comparison")
    receipt.update(stage="native-comparison", outcome="success",
                   native_64_success=True, api_19_success=True, api_checks=api,
                   implementation_diagnostics=implementation_diagnostics,
                   unsupported_native=unsupported, keyword_entries=entries,
                   schema_keyword_entries=schema_entries, coerce_comparison_entries=coerce_entries,
                   pointer_bits=struct.calcsize("P") * 8)


def main() -> None:
    """Fail on prior urllib.parse import; never delete/reload an existing module."""
    receipt = {"format": FORMAT, "outcome": "failure", "stage": "native-bootstrap"}
    output = None
    try:
        if len(sys.argv) != 2 or not os.path.isabs(sys.argv[1]):
            raise ValueError("one absolute native worker control path required")
        output = os.path.join(os.path.dirname(sys.argv[1]), "native-worker-result.json")
        if "urllib.parse" in sys.modules:
            raise ValueError("urllib.parse was imported before native worker bootstrap")
        receipt["bootstrap"] = {"urllib_parse_absent_at_start": True}
        if (sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true"
                or sys.version_info[:3] != (3, 13, 16)):
            raise RuntimeError("native worker requires pinned Linux GitHub Actions")
        receipt["stage"] = "native-bootstrap-control"
        control = _control(sys.argv[1], output)
        tooling = os.path.dirname(os.path.abspath(__file__))
        sys.path[:] = list(dict.fromkeys([
            control["module_dir"], control["stdlib_overlay"],
            *control["package_paths"], tooling, *control["stdlib_paths"],
        ]))
        if "_tobkiri_schema_kernel" in sys.modules:
            raise ValueError("native kernel was imported before worker path admission")
        import hashlib
        extension_bytes = _read(control["extension"], 32 * 1024 * 1024)
        if (len(extension_bytes) != control["extension_identity"]["bytes"]
                or hashlib.sha256(extension_bytes).hexdigest()
                != control["extension_identity"]["sha256"]):
            raise ValueError("native extension identity differs before import")
        del extension_bytes
        if "urllib.parse" in sys.modules:
            raise ValueError("bootstrap verification preloaded urllib.parse")
        receipt["bootstrap"]["extension_authenticated_before_import"] = True
        receipt["stage"] = "native-bootstrap-extension-import"
        import _tobkiri_schema_kernel as kernel

        if "urllib.parse" in sys.modules:
            raise ValueError("kernel import preloaded urllib.parse before translation")
        receipt["bootstrap"].update(
            urllib_parse_absent_after_kernel_import=True,
            kernel_import_precedes_pathlib_and_tooling=True,
        )
        _compare(control, receipt, kernel)
    except BaseException as error:
        receipt["outcome"] = "failure"
        receipt["error"] = {
            "type": type(error).__module__ + "." + type(error).__qualname__,
            "message": str(error)[:1500],
        }
        raise
    finally:
        if output is not None:
            _write_result(output, receipt)
        else:
            print(json.dumps(receipt, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
