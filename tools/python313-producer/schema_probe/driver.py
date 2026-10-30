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
CACHE_RESERVED_BYTES = 8 * 1024 * 1024
STDLIB_FILES = {
    "urllib/__init__.py",
    "urllib/parse.py",
    "_colorize.py",
    "linecache.py",
    "logging/__init__.py",
    "logging/config.py",
    "logging/handlers.py",
    "locale.py",
    "selectors.py",
    "_strptime.py",
    "calendar.py",
    "csv.py",
    "quopri.py",
    "base64.py",
    "importlib/metadata/_adapters.py",
    "importlib/metadata/_text.py",
    "email/__init__.py",
    "email/_encoded_words.py",
    "email/_header_value_parser.py",
    "email/_parseaddr.py",
    "email/_policybase.py",
    "email/base64mime.py",
    "email/charset.py",
    "email/contentmanager.py",
    "email/encoders.py",
    "email/errors.py",
    "email/feedparser.py",
    "email/generator.py",
    "email/header.py",
    "email/headerregistry.py",
    "email/iterators.py",
    "email/message.py",
    "email/parser.py",
    "email/policy.py",
    "email/quoprimime.py",
    "email/utils.py",
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


def load_stage_profile(path: Path, expected_source: bytes) -> ModuleType:
    """Execute the exact captured stage-profile source without sys.path edits."""
    import importlib.util

    source = read(path)
    if source != expected_source:
        raise ValueError("captured schema stage-profile helper changed")
    spec = importlib.util.spec_from_file_location("_tobkiri_schema_stage_profile", path)
    if spec is None or spec.loader is None:
        raise ValueError("captured schema stage-profile loader unavailable")
    module = importlib.util.module_from_spec(spec)
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def derive_preinit_code_cache_sources(
    baseline_sources: dict[str, bytes], helper_source: bytes
) -> tuple[dict[str, bytes], dict[str, object]]:
    """Derive only private app/loader bytes for Wasm-preinit cache capture."""
    import difflib

    required = {"app.py", "captured_loader.py", "exercise.py", "probe.wit"}
    if set(baseline_sources) != required or type(helper_source) is not bytes:
        raise ValueError("complete frozen schema sources required for code-cache mode")
    if any(type(raw) is not bytes for raw in baseline_sources.values()):
        raise TypeError("frozen schema sources must be bytes")

    def replace_once(source: bytes, old: bytes, new: bytes, label: str) -> bytes:
        if source.count(old) != 1:
            raise ValueError(
                f"frozen {label} source no longer matches its exact anchor"
            )
        return source.replace(old, new, 1)

    app = replace_once(
        baseline_sources["app.py"],
        b"import sysconfig\n\nfrom captured_loader import install\n",
        b"import sysconfig\nimport code_cache\ncode_cache.configure()\n\n"
        b"from captured_loader import install\n",
        "app.py code-cache import",
    )
    app = replace_once(
        app,
        b"_FINDER.seal()\n",
        b"code_cache.prime_sources(_FINDER)\ncode_cache.seal()\n_FINDER.seal()\n",
        "app.py preinit seal",
    )
    app = replace_once(
        app,
        b'                "target": _TARGET,\n'
        b"            }\n"
        b"        )\n\n"
        b"    def probe_monotonic(self) -> str:\n",
        b'                "target": _TARGET,\n'
        b'                "_preinit_code_cache_json": code_cache.report(),\n'
        b"            }\n"
        b"        )\n\n"
        b"    def probe_monotonic(self) -> str:\n",
        "app.py existing clock DATA result",
    )
    app = replace_once(
        app,
        b'                    "target": _TARGET,\n                    "exception": {\n',
        b'                    "target": _TARGET,\n'
        b'                    "_code_cache_runtime": code_cache.runtime_stats(),\n'
        b'                    "exception": {\n',
        "app.py returned exception DATA",
    )
    app = replace_once(
        app,
        b'                "target": _TARGET,\n'
        b'                "observation": observation,\n',
        b'                "target": _TARGET,\n'
        b'                "_code_cache_runtime": code_cache.runtime_stats(),\n'
        b'                "observation": observation,\n',
        "app.py returned schema DATA",
    )

    loader = replace_once(
        baseline_sources["captured_loader.py"],
        b"from types import MappingProxyType\n",
        b"from types import MappingProxyType\n\nimport code_cache\n",
        "captured loader helper import",
    )
    loader = replace_once(
        loader,
        b"        return compile(\n"
        b"            self.finder.tree.files[self.source],\n"
        b'            self.finder.root + "/" + self.source,\n'
        b'            "exec",\n'
        b"            dont_inherit=True,\n"
        b"        )\n",
        b"        source = self.finder.tree.files[self.source]\n"
        b'        filename = self.finder.root + "/" + self.source\n'
        b"        return code_cache.get_code(\n"
        b"            fullname,\n"
        b"            self.source,\n"
        b"            source,\n"
        b"            filename,\n"
        b"            self.finder.tree.accounted_bytes,\n"
        b"        )\n",
        "captured loader get_code",
    )

    derived = {"app.py": app, "captured_loader.py": loader}
    provenance_files = {}
    for name in sorted(derived):
        before, after = baseline_sources[name], derived[name]
        diff = "".join(
            difflib.unified_diff(
                before.decode("utf-8").splitlines(keepends=True),
                after.decode("utf-8").splitlines(keepends=True),
                fromfile="baseline/" + name,
                tofile="preinit-code-cache/" + name,
            )
        )
        provenance_files[name] = {
            "baseline": {
                "bytes": len(before),
                "sha256": hashlib.sha256(before).hexdigest(),
            },
            "derived": {
                "bytes": len(after),
                "sha256": hashlib.sha256(after).hexdigest(),
            },
            "diff": diff,
            "diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        }
    unchanged = {}
    for name in ("exercise.py", "probe.wit"):
        raw = baseline_sources[name]
        digest = hashlib.sha256(raw).hexdigest()
        unchanged[name] = {
            "bytes": len(raw),
            "baseline_sha256": digest,
            "derived_sha256": digest,
        }
    provenance = {
        "format": "tobkiri.schema-preinit-code-cache-derivation/1",
        "diagnostic_only": True,
        "original_acceptance_claimed": False,
        "production_enabled": False,
        "files": provenance_files,
        "unchanged_files": unchanged,
        "helper": {
            "path": "code_cache.py",
            "bytes": len(helper_source),
            "sha256": hashlib.sha256(helper_source).hexdigest(),
        },
        "cache_generation": (
            "normal preinit Loader.get_code calls plus bounded enumeration through "
            "the exact Finder.find_spec mapping for closed captured .py paths; "
            "each selected Loader.get_code compiles and marshals in Wasm CPython "
            "before code_cache.seal; no module bodies execute"
        ),
        "lazy_source_priming": {
            "resolution": "the existing Finder.find_spec package-before-module mapping",
            "compile_call": "the selected Loader.get_code(fullname)",
            "module_bodies_executed": False,
            "max_attempts": 512,
            "max_source_bytes": 8 * 1024 * 1024,
            "max_source_bytes_per_entry": 1024 * 1024,
            "max_cache_entries": 512,
        },
        "no_new_wit_imports_or_exports": True,
    }
    if len(json.dumps(provenance, ensure_ascii=True).encode("ascii")) > 1024 * 1024:
        raise ValueError("schema code-cache derivation provenance exceeds one MiB")
    return derived, provenance


def validate_preinit_code_cache_report(
    report_text: str,
    *,
    captured: dict,
    provenance: dict,
    toolchain: dict,
) -> dict:
    """Validate bounded guest-generated cache metadata against captured inputs."""
    if type(report_text) is not str or len(report_text.encode("ascii")) > 1024 * 1024:
        raise ValueError("preinit code-cache report exceeds its one-MiB bound")
    report_text_bytes = len(report_text.encode("ascii"))
    report = json.loads(report_text)
    if not isinstance(report, dict):
        raise ValueError("preinit code-cache report must be an object")
    if (
        report.get("format") != "tobkiri.schema-preinit-code-cache/1"
        or report.get("generation_phase") != "componentize-py Wasm CPython preinit"
        or report.get("generation_source")
        != "captured_loader.Finder.find_spec(...).loader.get_code(fullname) for closed .py paths; Loader.get_code compiles exact captured bytes with mode=exec, flags=0, dont_inherit=True, optimize=-1; no bodies execute"
        or report.get("sealed_before_snapshot") is not True
        or report.get("runtime_cache_use_observed") is not False
        or report.get("diagnostic_only") is not True
        or report.get("original_acceptance_claimed") is not False
        or report.get("production_enabled") is not False
    ):
        raise ValueError("preinit code-cache report scope or snapshot phase differs")
    audit = report.get("audit_event_difference")
    if (
        not isinstance(audit, dict)
        or audit.get("identical_to_original") is not False
        or audit.get("compile")
        != "preinit generation rather than runtime for cache entries"
        or audit.get("marshal_loads")
        != "runtime cache hits add marshal.loads audit events"
    ):
        raise ValueError("preinit cache audit-event differences must remain explicit")
    prime = report.get("preinit_prime")
    prime_skips = prime.get("skipped_counts") if isinstance(prime, dict) else None
    expected_prime_skips = {
        "seen_before_prime",
        "not_importable",
        "shadowed_path",
        "source_limit",
        "entry_limit",
        "attempt_limit",
        "compile_error_fallback",
    }
    if (
        not isinstance(prime, dict)
        or type(prime.get("attempts")) is not int
        or type(prime.get("completed")) is not int
        or type(prime.get("source_bytes")) is not int
        or prime.get("max_attempts") != 512
        or prime.get("max_source_bytes") != 8 * 1024 * 1024
        or prime.get("module_bodies_executed") is not False
        or prime["attempts"] < 0
        or prime["attempts"] > prime["max_attempts"]
        or prime["completed"] < 0
        or prime["completed"] > prime["attempts"]
        or prime["source_bytes"] < 0
        or prime["source_bytes"] > prime["max_source_bytes"]
        or not isinstance(prime_skips, dict)
        or set(prime_skips) != expected_prime_skips
        or any(type(count) is not int or count < 0 for count in prime_skips.values())
    ):
        raise ValueError("preinit lazy-source priming exceeded its fixed bounds")
    if (
        provenance.get("format") != "tobkiri.schema-preinit-code-cache-derivation/1"
        or provenance.get("diagnostic_only") is not True
        or provenance.get("original_acceptance_claimed") is not False
        or provenance.get("production_enabled") is not False
        or provenance.get("no_new_wit_imports_or_exports") is not True
    ):
        raise ValueError("preinit code-cache derivation provenance differs")
    priming = provenance.get("lazy_source_priming")
    if (
        not isinstance(priming, dict)
        or priming.get("resolution")
        != "the existing Finder.find_spec package-before-module mapping"
        or priming.get("compile_call") != "the selected Loader.get_code(fullname)"
        or priming.get("module_bodies_executed") is not False
        or priming.get("max_attempts") != 512
        or priming.get("max_source_bytes") != 8 * 1024 * 1024
        or priming.get("max_source_bytes_per_entry") != 1024 * 1024
        or priming.get("max_cache_entries") != 512
    ):
        raise ValueError("closed-source priming derivation or bounds differ")
    interpreter = report.get("interpreter")
    if (
        not isinstance(interpreter, dict)
        or interpreter.get("implementation") != "cpython"
        or interpreter.get("python_version") != [3, 13, 16]
        or interpreter.get("pointer_bits") != 32
        or type(interpreter.get("optimize")) is not int
        or type(interpreter.get("marshal_version")) is not int
        or not isinstance(interpreter.get("soabi"), str)
        or "wasm32" not in interpreter["soabi"]
    ):
        raise ValueError("preinit code-cache interpreter identity is not the target")
    toolchain_identity = toolchain.get("componentize_cli")
    if (
        not isinstance(toolchain_identity, dict)
        or toolchain.get("authenticated_candidate_cli") != toolchain_identity
        or type(toolchain_identity.get("bytes")) is not int
        or toolchain_identity["bytes"] <= 0
        or not isinstance(toolchain_identity.get("sha256"), str)
        or len(toolchain_identity["sha256"]) != 64
        or any(char not in "0123456789abcdef" for char in toolchain_identity["sha256"])
    ):
        raise ValueError("preinit code-cache componentizer is not authenticated")

    inventory = captured.get("files")
    if not isinstance(inventory, dict):
        raise ValueError("preinit code-cache source inventory is unavailable")
    for name in ("app.py", "captured_loader.py"):
        baseline = provenance.get("files", {}).get(name, {}).get("baseline")
        derived = provenance.get("files", {}).get(name, {}).get("derived")
        if (
            not isinstance(baseline, dict)
            or not isinstance(derived, dict)
            or inventory.get(name, {}).get("sha256") != derived.get("sha256")
            or type(baseline.get("bytes")) is not int
            or type(derived.get("bytes")) is not int
        ):
            raise ValueError(
                "derived app or loader does not match its captured inventory"
            )
    for name in ("exercise.py", "probe.wit"):
        unchanged = provenance.get("unchanged_files", {}).get(name)
        if (
            not isinstance(unchanged, dict)
            or unchanged.get("baseline_sha256") != unchanged.get("derived_sha256")
            or inventory.get(name, {}).get("sha256") != unchanged.get("derived_sha256")
        ):
            raise ValueError("frozen exercise or WIT source changed")
    helper = provenance.get("helper")
    if not isinstance(helper, dict) or inventory.get("code_cache.py", {}).get(
        "sha256"
    ) != helper.get("sha256"):
        raise ValueError("preinit code-cache helper differs from its source inventory")

    entries = report.get("entries")
    uncached_counts = report.get("uncached_counts")
    expected_uncached_counts = {
        "entry_limit",
        "metadata_limit",
        "memory_limit",
        "marshal_error",
    }
    if (
        not isinstance(entries, list)
        or len(entries) > 512
        or not isinstance(uncached_counts, dict)
        or set(uncached_counts) != expected_uncached_counts
        or any(
            type(count) is not int or count < 0 for count in uncached_counts.values()
        )
        or report.get("cache_entry_count") != len(entries)
        or type(report.get("preinit_compile_calls")) is not int
        or type(report.get("preinit_cached_calls")) is not int
        or report["preinit_compile_calls"] < report["preinit_cached_calls"]
        or report["preinit_compile_calls"] < prime["completed"]
        or report["preinit_cached_calls"] < len(entries)
    ):
        raise ValueError("preinit cache entry or call counts exceed their bounds")
    cached_identities = set()
    cache_bytes = 0
    cache_payload_hashes = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("preinit cache entry must be an object")
        source_name = entry.get("source_name")
        source_record = inventory.get(source_name) if type(source_name) is str else None
        source_hash = entry.get("source_sha256")
        if (
            source_record is None
            or len(source_name) > 4096
            or type(entry.get("fullname")) is not str
            or not entry["fullname"]
            or len(entry["fullname"]) > 4096
            or type(entry.get("filename")) is not str
            or len(entry["filename"]) > 8192
            or type(entry.get("source_bytes")) is not int
            or entry["source_bytes"] != source_record.get("bytes")
            or entry["source_bytes"] > 1024 * 1024
            or source_hash != source_record.get("sha256")
            or entry.get("compile_mode") != "exec"
            or entry.get("compile_flags") != 0
            or entry.get("dont_inherit") is not True
            or entry.get("optimize_argument") != -1
            or entry.get("actual_optimize") != interpreter["optimize"]
            or entry.get("marshal_version") != interpreter["marshal_version"]
            or type(entry.get("code_graph_bytes")) is not int
            or entry["code_graph_bytes"] < 0
            or type(entry.get("cache_bytes")) is not int
            or entry["cache_bytes"] <= 0
            or not isinstance(entry.get("cache_sha256"), str)
            or len(entry["cache_sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in entry["cache_sha256"])
        ):
            raise ValueError(
                "preinit code-cache entry is not bound to a captured source"
            )
        cache_bytes += entry["cache_bytes"]
        cache_payload_hashes.append(bytes.fromhex(entry["cache_sha256"]))
        identity = (entry["fullname"], source_name, source_hash, entry["filename"])
        if identity in cached_identities:
            raise ValueError("preinit cache repeats a module identity")
        cached_identities.add(identity)
    aggregate_digest = hashlib.sha256(b"".join(cache_payload_hashes)).hexdigest()
    if report.get("cache_payload_sha256") != aggregate_digest:
        raise ValueError("preinit code-cache aggregate digest differs")

    accounting = report.get("accounting")
    if not isinstance(accounting, dict):
        raise ValueError("preinit code-cache accounting is unavailable")
    integer_fields = (
        "source_tree_accounted_bytes",
        "cache_bytes",
        "metadata_record_bytes",
        "retained_code_graph_estimate_bytes",
        "retained_accounted_bytes",
        "report_text_bytes",
        "peak_temporary_bytes",
        "peak_code_graph_bytes",
        "peak_marshal_buffer_bytes",
        "peak_combined_store_bytes",
        "max_cache_accounted_bytes",
        "max_temporary_bytes",
        "capture_limit_bytes",
        "store_limit_bytes",
    )
    if any(
        type(accounting.get(name)) is not int or accounting[name] < 0
        for name in integer_fields
    ):
        raise ValueError("preinit code-cache accounting is malformed")
    if (
        accounting["cache_bytes"] != cache_bytes
        or accounting["source_tree_accounted_bytes"] > 64 * 1024 * 1024
        or accounting["max_cache_accounted_bytes"] != 8 * 1024 * 1024
        or accounting["max_temporary_bytes"] != 16 * 1024 * 1024
        or accounting["capture_limit_bytes"] != 64 * 1024 * 1024
        or accounting["store_limit_bytes"] != 128 * 1024 * 1024
        or accounting["retained_accounted_bytes"]
        > accounting["max_cache_accounted_bytes"]
        or accounting["retained_accounted_bytes"]
        < accounting["cache_bytes"]
        + accounting["metadata_record_bytes"]
        + accounting["retained_code_graph_estimate_bytes"]
        or accounting["metadata_record_bytes"] > 1024 * 1024 - 4096
        or accounting["report_text_bytes"] != report_text_bytes
        or accounting["peak_temporary_bytes"] > accounting["max_temporary_bytes"]
        or accounting["peak_temporary_bytes"] < accounting["retained_accounted_bytes"]
        or accounting["peak_combined_store_bytes"] > accounting["store_limit_bytes"]
        or accounting["peak_combined_store_bytes"]
        < accounting["source_tree_accounted_bytes"] + accounting["peak_temporary_bytes"]
        or accounting["source_tree_accounted_bytes"]
        + accounting["retained_accounted_bytes"]
        > accounting["capture_limit_bytes"]
    ):
        raise ValueError("preinit code-cache accounting exceeds original bounds")
    return report


def _runtime_code_cache_stats(result: object) -> dict | None:
    if not isinstance(result, dict):
        return None
    stats = result.get("_code_cache_runtime")
    if not isinstance(stats, dict) or (
        stats.get("format") != "tobkiri.schema-preinit-code-cache/1"
        or stats.get("runtime_cache_use_observed") is not True
        or stats.get("diagnostic_only") is not True
        or stats.get("production_enabled") is not False
    ):
        return None
    names = ("cache_hits", "cache_misses", "runtime_compiles", "marshal_loads")
    if any(type(stats.get(name)) is not int or stats[name] < 0 for name in names):
        return None
    return {name: stats[name] for name in names} | {
        "runtime_cache_use_observed": True,
        "diagnostic_only": True,
        "production_enabled": False,
    }


def stage_inputs(stage: Path, control: dict) -> dict:
    """Capture exact package/stdlib source and separate extension provenance."""
    files, distributions = {}, {}
    wrapper = None
    declared, members = 0, 0
    diagnostic_mode = control.get("schema_exception_diagnostics", False)
    profile_mode = control.get("schema_stage_profile", False)
    code_cache_mode = control.get("schema_preinit_code_cache", False)
    if type(diagnostic_mode) is not bool:
        raise ValueError("explicit boolean schema diagnostics mode required")
    if type(profile_mode) is not bool:
        raise ValueError("explicit boolean schema stage-profile mode required")
    if type(code_cache_mode) is not bool:
        raise ValueError("explicit boolean schema preinit code-cache mode required")
    if sum((diagnostic_mode, profile_mode, code_cache_mode)) > 1:
        raise ValueError("schema diagnostic modes are mutually exclusive")
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
    if profile_mode and not all(
        control.get(name) is True
        for name in (
            "allow_ui_wall_clock",
            "allow_ui_monotonic_clock",
            "allow_inactive_ui_libc",
            "allow_ci_link_trace",
        )
    ):
        raise ValueError("schema profile requires the complete inactive candidate")
    if code_cache_mode and not all(
        control.get(name) is True
        for name in (
            "allow_ui_wall_clock",
            "allow_ui_monotonic_clock",
            "allow_inactive_ui_libc",
            "allow_ci_link_trace",
        )
    ):
        raise ValueError("schema code cache requires the complete inactive candidate")
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
    if profile_mode:
        probe_files.append("stage_profile.py")
    if code_cache_mode:
        probe_files.append("code_cache.py")
    baseline_sources = {}
    baseline_app = None
    helper_source = None
    profile_source = None
    code_cache_source = None
    for name in probe_files:
        target = stage / name
        if target.exists():
            raise ValueError("package collides with probe source")
        raw = read(Path("/probe-source") / name)
        target.write_bytes(raw)
        if name == "app.py":
            baseline_app = raw
            baseline_sources[name] = raw
        elif name in {"exercise.py", "captured_loader.py", "probe.wit"}:
            baseline_sources[name] = raw
        elif name == "exception_diagnostics.py":
            helper_source = raw
        elif name == "stage_profile.py":
            profile_source = raw
        elif name == "code_cache.py":
            code_cache_source = raw
    diagnostic_provenance = None
    profile_provenance = None
    code_cache_provenance = None
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
    if profile_mode:
        if profile_source is None or set(baseline_sources) != {
            "app.py",
            "exercise.py",
            "captured_loader.py",
            "probe.wit",
        }:
            raise ValueError("frozen baseline probe and stage-profile helper required")
        helper = load_stage_profile(
            Path("/probe-source/stage_profile.py"), profile_source
        )
        derived_sources, profile_provenance = helper.derive_stage_profile_sources(
            baseline_sources, profile_source
        )
        for name, baseline in baseline_sources.items():
            if read(stage / name) != baseline:
                raise ValueError(
                    "baseline probe source changed before profiling derivation"
                )
        for name, derived in derived_sources.items():
            (stage / name).write_bytes(derived)
    if code_cache_mode:
        if code_cache_source is None or set(baseline_sources) != {
            "app.py",
            "exercise.py",
            "captured_loader.py",
            "probe.wit",
        }:
            raise ValueError("frozen probe sources and code-cache helper required")
        derived_sources, code_cache_provenance = derive_preinit_code_cache_sources(
            baseline_sources, code_cache_source
        )
        code_cache_provenance["bounds"] = {
            "original_capture_limit_bytes": control["input_limit"],
            "reserved_cache_accounting_bytes": CACHE_RESERVED_BYTES,
            "original_store_memory_limit_bytes": 128 * 1024 * 1024,
            "preinit_prime_attempts": 512,
            "preinit_prime_source_bytes": 8 * 1024 * 1024,
            "module_bodies_executed_for_priming": False,
        }
        for name, baseline in baseline_sources.items():
            if read(stage / name) != baseline:
                raise ValueError(
                    "frozen schema source changed before code-cache derivation"
                )
        for name, derived in derived_sources.items():
            (stage / name).write_bytes(derived)
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
    if code_cache_mode and total + CACHE_RESERVED_BYTES > control["input_limit"]:
        raise ValueError(
            "schema code cache has no room within the original capture bound"
        )
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
    if profile_provenance is not None:
        result["schema_stage_profile"] = profile_provenance
    if code_cache_provenance is not None:
        result["schema_preinit_code_cache"] = code_cache_provenance
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
    allow_stage_profile: bool = False,
    stage_profile_api: ModuleType | None = None,
):
    imports = component.type.imports(engine)
    exports = list(component.type.exports(engine))
    if allow_monotonic and not allow_clock:
        raise ValueError("monotonic candidate requires explicit wall-clock mode")
    profile_imports = set()
    if allow_stage_profile:
        if stage_profile_api is None:
            raise ValueError("exact schema stage-profile import validator required")
        profile_imports = {stage_profile_api.PROFILE_IMPORT_NAME}
    if allow_clock:
        from ui_clock import validate_clock_import

        if allow_monotonic:
            from ui_monotonic import (
                IMPORT_NAME,
                validate_clock_import as validate_monotonic,
            )

            if (
                len(imports) != 2 + len(profile_imports)
                or IMPORT_NAME not in imports
                or not profile_imports <= set(imports)
            ):
                message = (
                    "UI candidate must import only two clocks and the exact profile interface"
                    if allow_stage_profile
                    else "UI candidate must import only the two explicit clock interfaces"
                )
                raise ValueError(message)
            validate_monotonic(IMPORT_NAME, imports[IMPORT_NAME], engine)
            remaining = {
                name: item
                for name, item in imports.items()
                if name != IMPORT_NAME and name not in profile_imports
            }
        else:
            if not profile_imports <= set(imports):
                raise ValueError("schema candidate lacks its exact profile interface")
            remaining = {
                name: item
                for name, item in imports.items()
                if name not in profile_imports
            }
        if len(remaining) != 1:
            raise ValueError("UI candidate must import only one wall-clock interface")
        for name, item in remaining.items():
            validate_clock_import(name, item, engine)
    elif set(imports) != profile_imports:
        if imports:
            raise ValueError("schema component has unexpected authority imports")
    if allow_stage_profile:
        for name in profile_imports:
            stage_profile_api.validate_import(name, imports[name], engine)
    if not allow_clock and not allow_stage_profile and imports:
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
    allow_stage_profile: bool = False,
    stage_profile_api: ModuleType | None = None,
    profile_output_path: Path | None = None,
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
            allow_stage_profile=allow_stage_profile,
            stage_profile_api=stage_profile_api,
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
            profile_bridge = None
            budget = None
            denial = None
            execution_status = "not_started"
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
                if allow_stage_profile:
                    if stage_profile_api is None or budget is None:
                        raise ValueError(
                            "schema profile requires its exact API and shared clock budget"
                        )
                    profile_bridge = stage_profile_api.StageProfileBridge(budget)
                    profile_bridge.install(linker)
                execution_status = "running"
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
                        execution_status = "failed"
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
                execution_status = "completed"
            except BaseException:
                execution_status = "failed"
                raise
            finally:
                finished.set()
                timer.cancel()
                timer.join()
                if profile_bridge is not None and profile_output_path is not None:
                    active_exception = sys.exc_info()[0] is not None
                    try:
                        profile_bridge.write_report(
                            profile_output_path, execution_status
                        )
                    except BaseException:
                        if not active_exception:
                            raise
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
            observation = {
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
            if profile_bridge is not None:
                observation["stage_profile"] = profile_bridge.report(execution_status)
            return observation


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
    profile_mode = control.get("schema_stage_profile", False)
    code_cache_mode = control.get("schema_preinit_code_cache", False)
    if any(
        type(value) is not bool
        for value in (
            allow_clock,
            allow_monotonic,
            allow_link_trace,
            diagnostic_mode,
            profile_mode,
            code_cache_mode,
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
    if sum((diagnostic_mode, profile_mode, code_cache_mode)) > 1:
        raise ValueError("schema diagnostic modes are mutually exclusive")
    if profile_mode and not all(
        control.get(name) is True
        for name in (
            "allow_ui_wall_clock",
            "allow_ui_monotonic_clock",
            "allow_inactive_ui_libc",
            "allow_ci_link_trace",
        )
    ):
        raise ValueError(
            "schema stage profile requires the complete inactive candidate"
        )
    if code_cache_mode and not all(
        control.get(name) is True
        for name in (
            "allow_ui_wall_clock",
            "allow_ui_monotonic_clock",
            "allow_inactive_ui_libc",
            "allow_ci_link_trace",
        )
    ):
        raise ValueError(
            "schema preinit code cache requires the complete inactive candidate"
        )
    original = json.loads(read(work / "native.json"))
    stage = work / "stage"
    stage.mkdir()
    captured = stage_inputs(stage, control)
    diagnostic_provenance = captured.get("schema_exception_diagnostics")
    if diagnostic_provenance is not None:
        (work / "schema-exception-diagnostics.json").write_text(
            json.dumps(diagnostic_provenance, indent=2) + "\n"
        )
    stage_profile_api = None
    profile_provenance = captured.get("schema_stage_profile")
    if profile_mode:
        if not isinstance(profile_provenance, dict):
            raise ValueError("schema stage-profile provenance is missing")
        helper_source = read(stage / "stage_profile.py")
        stage_profile_api = load_stage_profile(
            Path("/probe-source/stage_profile.py"), helper_source
        )
        (work / "schema-stage-profile-provenance.json").write_text(
            json.dumps(profile_provenance, indent=2) + "\n"
        )
    code_cache_provenance = captured.get("schema_preinit_code_cache")
    code_cache_toolchain = control.get("schema_preinit_code_cache_toolchain")
    cache_evidence_path = work / "schema-preinit-code-cache.json"
    cache_evidence = None

    def save_cache_evidence(**updates) -> None:
        if cache_evidence is None:
            return
        if cache_evidence_path.is_symlink():
            raise ValueError("schema code-cache evidence path became a symlink")
        cache_evidence.update(updates)
        encoded = json.dumps(cache_evidence, ensure_ascii=True, indent=2) + "\n"
        if len(encoded.encode("ascii")) > 2 * 1024 * 1024:
            raise ValueError("schema code-cache evidence exceeds its two-MiB bound")
        cache_evidence_path.write_text(encoded)

    if code_cache_mode:
        if not isinstance(code_cache_provenance, dict) or not isinstance(
            code_cache_toolchain, dict
        ):
            raise ValueError(
                "schema code-cache derivation and toolchain provenance required"
            )
        if code_cache_provenance.get("no_new_wit_imports_or_exports") is not True:
            raise ValueError("schema code-cache derivation may not add WIT authority")
        (work / "schema-preinit-code-cache-provenance.json").write_text(
            json.dumps(code_cache_provenance, indent=2) + "\n"
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
            allow_stage_profile=profile_mode,
            stage_profile_api=stage_profile_api,
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
    if (
        code_cache_mode
        and sum(
            path.stat().st_size
            for path in stage.rglob("*")
            if path.is_file() and path.name != "component.wasm"
        )
        + CACHE_RESERVED_BYTES
        > control["input_limit"]
    ):
        raise ValueError(
            "generated clock/source copies exceed the cache-reserved input bound"
        )
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
    if (
        code_cache_mode
        and final_source_bytes + CACHE_RESERVED_BYTES > control["input_limit"]
    ):
        raise ValueError("final source copies exceed the cache-reserved input bound")
    if read(stage / "component.wasm", 32 * 1024 * 1024) != inner:
        raise ValueError("schema inner changed during composition")
    clock_probes = None
    if allow_clock:
        start_wall = time.time_ns()
        clock_probe = observe(
            stage / "outer.wasm",
            allow_clock=True,
            allow_monotonic=allow_monotonic,
            allow_stage_profile=profile_mode,
            stage_profile_api=stage_profile_api,
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
        if code_cache_mode:
            report_text = result.get("_preinit_code_cache_json")
            preinit_cache = validate_preinit_code_cache_report(
                report_text,
                captured=captured,
                provenance=code_cache_provenance,
                toolchain=code_cache_toolchain,
            )
            outer_component = read(stage / "outer.wasm", 32 * 1024 * 1024)
            toolchain_evidence = {
                name: code_cache_toolchain[name]
                for name in (
                    "componentize_cli",
                    "cli_build_receipt",
                    "cli_source_receipt",
                    "authenticated_candidate_cli",
                    "componentize_bindings_command",
                    "componentize_snapshot_command",
                    "source_receipt_format",
                )
            }
            cache_evidence = {
                "format": "tobkiri.schema-preinit-code-cache-evidence/1",
                "diagnostic_only": True,
                "original_acceptance_claimed": False,
                "production_enabled": False,
                "preinit_cache_evidence_available": True,
                "preinit_cache": preinit_cache,
                "derivation": code_cache_provenance,
                "toolchain": toolchain_evidence,
                "pre_snapshot_component": {
                    "bytes": len(inner),
                    "sha256": hashlib.sha256(inner).hexdigest(),
                },
                "final_component": {
                    "bytes": len(outer_component),
                    "sha256": hashlib.sha256(outer_component).hexdigest(),
                },
                "preinit_observation": {
                    "operation": "existing probe-clock export on final composed component",
                    "clock_assertions_retained": True,
                    "target": result.get("target"),
                    "fuel_consumed": clock_probe.get("fuel_consumed"),
                    "clock_budget": clock_probe.get("clock_budget"),
                },
                "execution_outcome": "full_schema_run_not_started",
                "strict_comparison_outcome": "not_started",
                "runtime_cache_use": {
                    "observed": False,
                    "status": "not_observed_after_snapshot_yet",
                    "statistics": None,
                },
            }
            save_cache_evidence()
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
                    allow_stage_profile=profile_mode,
                    stage_profile_api=stage_profile_api,
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
                    allow_stage_profile=profile_mode,
                    stage_profile_api=stage_profile_api,
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
    try:
        actual = observe(
            stage / "outer.wasm",
            allow_clock=allow_clock,
            allow_monotonic=allow_monotonic,
            allow_stage_profile=profile_mode,
            stage_profile_api=stage_profile_api,
            profile_output_path=(work / "schema-stage-profile.json")
            if profile_mode
            else None,
        )
    except BaseException as error:
        if code_cache_mode:
            try:
                error_type = type(error)
                try:
                    message = str(error)[:4096]
                except BaseException:
                    message = "exception message unavailable"
                save_cache_evidence(
                    execution_outcome="wasm_observe_failed",
                    strict_comparison_outcome="not_completed",
                    runtime_cache_use={
                        "observed": False,
                        "status": "unavailable_after_guest_store_failure",
                        "statistics": None,
                        "reason": "the guest did not return DATA containing runtime counters",
                    },
                )
                failure = {
                    "format": "tobkiri.schema-preinit-code-cache-observation/1",
                    "outcome": "wasm_observe_failed",
                    "diagnostic_only": True,
                    "original_acceptance_claimed": False,
                    "production_enabled": False,
                    "exception": {
                        "type": error_type.__module__ + "." + error_type.__qualname__,
                        "message": message,
                    },
                    "runtime_cache_use": {
                        "observed": False,
                        "status": "unavailable_after_guest_store_failure",
                        "statistics": None,
                    },
                }
                (work / "wasm-observation.json").write_text(
                    json.dumps(failure, ensure_ascii=True, indent=2) + "\n"
                )
            except BaseException:
                # Diagnostic evidence cannot replace the original guest error.
                pass
        if profile_mode:
            error_type = type(error)
            failure = {
                "format": "tobkiri.schema-stage-profile-observation/1",
                "outcome": "failed",
                "diagnostic_only": True,
                "original_acceptance_claimed": False,
                "production_enabled": False,
                "exception": {
                    "type": error_type.__module__ + "." + error_type.__qualname__,
                    "message": str(error)[:4096],
                },
            }
            (work / "wasm-observation.json").write_text(
                json.dumps(failure, indent=2) + "\n"
            )
        raise
    (work / "wasm-observation.json").write_text(json.dumps(actual, indent=2) + "\n")
    if code_cache_mode:
        runtime_stats = _runtime_code_cache_stats(actual.get("result"))
        save_cache_evidence(
            execution_outcome="full_schema_run_returned",
            strict_comparison_outcome="pending_original_checks",
            runtime_cache_use={
                "observed": runtime_stats is not None,
                "status": (
                    "reported_by_returned_guest_DATA"
                    if runtime_stats is not None
                    else "unavailable_in_returned_guest_DATA"
                ),
                "statistics": runtime_stats,
            },
        )
    try:
        if profile_mode:
            raise ValueError(
                "diagnostic-only schema stage profile never accepts a candidate"
            )
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
            if code_cache_mode:
                save_cache_evidence(
                    strict_comparison_outcome="failed_original_guest_exception"
                )
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
        if code_cache_mode:
            strict_comparison = {
                "outcome": "matched",
                "full_original_observation_equal": (
                    result["observation"] == original["observation"]
                ),
                "original_case_corpus_completed": True,
                "original_package_and_resource_inventory_rechecked": True,
                "original_wall_and_monotonic_clock_assertions_retained": True,
                "non_clock_denial_probes_completed": len(
                    clock_probes["non_clock_denials"]
                ),
            }
            diagnostic_result = {
                "format": "tobkiri.ui-schema-preinit-code-cache-comparison/1",
                "outcome": "diagnostic_only",
                "passed": False,
                "diagnostic_only": True,
                "original_acceptance_claimed": False,
                "production_enabled": False,
                "strict_comparison": strict_comparison,
                "native_python": original["python"],
                "observation": actual,
                "input_inventory": captured,
                "inner": inner_typed,
                "composition": composition,
                "pre_snapshot_component_sha256": hashlib.sha256(inner).hexdigest(),
                "artifact_sha256": hashlib.sha256(
                    read(stage / "outer.wasm", 32 * 1024 * 1024)
                ).hexdigest(),
                "clock_probes": clock_probes,
                "generated_clock_wit": generated_wit,
                "final_source_bytes": final_source_bytes,
            }
            (work / "schema.json").write_text(
                json.dumps(diagnostic_result, ensure_ascii=True, indent=2) + "\n"
            )
            save_cache_evidence(
                strict_comparison_outcome="matched",
                strict_comparison=strict_comparison,
            )
            raise ValueError(
                "diagnostic-only preinit code-cache comparison never accepts a candidate"
            )
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
    finally:
        if (
            code_cache_mode
            and cache_evidence is not None
            and cache_evidence.get("strict_comparison_outcome")
            == "pending_original_checks"
        ):
            try:
                save_cache_evidence(strict_comparison_outcome="failed_original_checks")
            except BaseException:
                pass


if __name__ == "__main__":
    main()
