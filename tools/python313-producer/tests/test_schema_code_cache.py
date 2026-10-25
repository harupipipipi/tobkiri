"""Small stdlib-only checks for the inactive preinit code-cache experiment."""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "schema_probe"))

import build_schema  # noqa: E402
import driver  # noqa: E402


class SchemaPreinitCodeCacheTests(unittest.TestCase):
    def test_derivation_is_exact_and_does_not_modify_frozen_baseline_sources(self):
        names = ("app.py", "exercise.py", "captured_loader.py", "probe.wit")
        paths = {name: HERE / "schema_probe" / name for name in names}
        baseline = {name: path.read_bytes() for name, path in paths.items()}
        helper = (HERE / "schema_probe/code_cache.py").read_bytes()

        derived, provenance = driver.derive_preinit_code_cache_sources(baseline, helper)

        self.assertEqual(set(derived), {"app.py", "captured_loader.py"})
        self.assertEqual(
            {name: path.read_bytes() for name, path in paths.items()}, baseline
        )
        self.assertTrue(provenance["diagnostic_only"])
        self.assertFalse(provenance["original_acceptance_claimed"])
        self.assertFalse(provenance["production_enabled"])
        self.assertTrue(provenance["no_new_wit_imports_or_exports"])
        self.assertFalse(provenance["lazy_source_priming"]["module_bodies_executed"])
        self.assertEqual(provenance["lazy_source_priming"]["max_attempts"], 512)
        self.assertEqual(
            provenance["lazy_source_priming"]["max_source_bytes"], 8 * 1024 * 1024
        )
        self.assertEqual(
            provenance["files"]["app.py"]["baseline"]["sha256"],
            hashlib.sha256(baseline["app.py"]).hexdigest(),
        )
        self.assertEqual(
            provenance["files"]["app.py"]["derived"]["sha256"],
            hashlib.sha256(derived["app.py"]).hexdigest(),
        )
        self.assertEqual(
            provenance["files"]["captured_loader.py"]["derived"]["sha256"],
            hashlib.sha256(derived["captured_loader.py"]).hexdigest(),
        )
        self.assertEqual(
            provenance["helper"]["sha256"], hashlib.sha256(helper).hexdigest()
        )
        expected_app = baseline["app.py"]

        def replace_once(source, old, new):
            self.assertEqual(source.count(old), 1)
            return source.replace(old, new, 1)

        expected_app = replace_once(
            expected_app,
            b"import sysconfig\n\nfrom captured_loader import install\n",
            b"import sysconfig\nimport code_cache\ncode_cache.configure()\n\n"
            b"from captured_loader import install\n",
        )
        expected_app = replace_once(
            expected_app,
            b"_FINDER.seal()\n",
            b"code_cache.prime_sources(_FINDER)\ncode_cache.seal()\n_FINDER.seal()\n",
        )
        expected_app = replace_once(
            expected_app,
            b'                "target": _TARGET,\n'
            b"            }\n"
            b"        )\n\n"
            b"    def probe_monotonic(self) -> str:\n",
            b'                "target": _TARGET,\n'
            b'                "_preinit_code_cache_json": code_cache.report(),\n'
            b"            }\n"
            b"        )\n\n"
            b"    def probe_monotonic(self) -> str:\n",
        )
        expected_app = replace_once(
            expected_app,
            b'                    "target": _TARGET,\n'
            b'                    "exception": {\n',
            b'                    "target": _TARGET,\n'
            b'                    "_code_cache_runtime": code_cache.runtime_stats(),\n'
            b'                    "exception": {\n',
        )
        expected_app = replace_once(
            expected_app,
            b'                "target": _TARGET,\n'
            b'                "observation": observation,\n',
            b'                "target": _TARGET,\n'
            b'                "_code_cache_runtime": code_cache.runtime_stats(),\n'
            b'                "observation": observation,\n',
        )
        expected_loader = replace_once(
            baseline["captured_loader.py"],
            b"from types import MappingProxyType\n",
            b"from types import MappingProxyType\n\nimport code_cache\n",
        )
        expected_loader = replace_once(
            expected_loader,
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
        )
        self.assertEqual(derived["app.py"], expected_app)
        self.assertEqual(derived["captured_loader.py"], expected_loader)
        for name in ("exercise.py", "probe.wit"):
            self.assertEqual(derived.get(name), None)
            self.assertEqual(
                provenance["unchanged_files"][name]["baseline_sha256"],
                hashlib.sha256(baseline[name]).hexdigest(),
            )
            self.assertEqual(
                provenance["unchanged_files"][name]["derived_sha256"],
                hashlib.sha256(baseline[name]).hexdigest(),
            )
        self.assertIn(b"code_cache.configure()", derived["app.py"])
        self.assertLess(
            derived["app.py"].index(b"code_cache.configure()"),
            derived["app.py"].index(b"from captured_loader import install"),
        )
        self.assertIn(
            b"code_cache.prime_sources(_FINDER)\ncode_cache.seal()\n_FINDER.seal()",
            derived["app.py"],
        )
        self.assertIn(
            b"if fullname != self.name:\n            raise ImportError(fullname)",
            derived["captured_loader.py"],
        )
        self.assertIn(
            b"self.finder.tree.files[self.source]", derived["captured_loader.py"]
        )
        self.assertIn(b"code_cache.get_code(", derived["captured_loader.py"])
        self.assertEqual(derived["app.py"].count(b"_preinit_code_cache_json"), 1)
        self.assertNotIn(b"diagnostic-profile", baseline["probe.wit"])
        self.assertNotIn(
            b"diagnostic-profile", derived["app.py"] + derived["captured_loader.py"]
        )

    def test_guest_report_is_bounded_and_bound_to_derived_source_and_toolchain(self):
        names = ("app.py", "exercise.py", "captured_loader.py", "probe.wit")
        baseline = {name: (HERE / "schema_probe" / name).read_bytes() for name in names}
        helper = (HERE / "schema_probe/code_cache.py").read_bytes()
        derived, provenance = driver.derive_preinit_code_cache_sources(baseline, helper)
        inventory = {
            name: {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            for name, raw in baseline.items()
        }
        inventory.update(
            {
                name: {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                for name, raw in derived.items()
            }
        )
        inventory["code_cache.py"] = {
            "bytes": len(helper),
            "sha256": hashlib.sha256(helper).hexdigest(),
        }
        captured = {"files": inventory}
        cli_identity = {"bytes": 123, "sha256": "a" * 64}
        toolchain = {
            "componentize_cli": cli_identity,
            "authenticated_candidate_cli": cli_identity,
        }
        report = {
            "format": "tobkiri.schema-preinit-code-cache/1",
            "generation_phase": "componentize-py Wasm CPython preinit",
            "generation_source": "captured_loader.Finder.find_spec(...).loader.get_code(fullname) for closed .py paths; Loader.get_code compiles exact captured bytes with mode=exec, flags=0, dont_inherit=True, optimize=-1; no bodies execute",
            "sealed_before_snapshot": True,
            "runtime_cache_use_observed": False,
            "diagnostic_only": True,
            "original_acceptance_claimed": False,
            "production_enabled": False,
            "audit_event_difference": {
                "compile": "preinit generation rather than runtime for cache entries",
                "marshal_loads": "runtime cache hits add marshal.loads audit events",
                "identical_to_original": False,
            },
            "interpreter": {
                "implementation": "cpython",
                "python_version": [3, 13, 16],
                "pointer_bits": 32,
                "soabi": "cpython-313-wasm32-wasi",
                "optimize": 0,
                "marshal_version": 4,
            },
            "preinit_compile_calls": 1,
            "preinit_cached_calls": 1,
            "preinit_prime": {
                "attempts": 1,
                "completed": 1,
                "source_bytes": len(baseline["exercise.py"]),
                "max_attempts": 512,
                "max_source_bytes": 8 * 1024 * 1024,
                "skipped_counts": {
                    "seen_before_prime": 0,
                    "not_importable": 0,
                    "shadowed_path": 0,
                    "source_limit": 0,
                    "entry_limit": 0,
                    "attempt_limit": 0,
                    "compile_error_fallback": 0,
                },
                "module_bodies_executed": False,
            },
            "cache_entry_count": 1,
            "cache_payload_sha256": hashlib.sha256(bytes.fromhex("b" * 64)).hexdigest(),
            "uncached_counts": {
                "entry_limit": 0,
                "metadata_limit": 0,
                "memory_limit": 0,
                "marshal_error": 0,
            },
            "entries": [
                {
                    "fullname": "jsonschema",
                    "source_name": "exercise.py",
                    "source_bytes": len(baseline["exercise.py"]),
                    "source_sha256": inventory["exercise.py"]["sha256"],
                    "filename": "/work/stage/exercise.py",
                    "compile_mode": "exec",
                    "compile_flags": 0,
                    "dont_inherit": True,
                    "optimize_argument": -1,
                    "actual_optimize": 0,
                    "marshal_version": 4,
                    "code_graph_bytes": 100,
                    "cache_bytes": 12,
                    "cache_sha256": "b" * 64,
                }
            ],
            "accounting": {
                "source_tree_accounted_bytes": 1024,
                "cache_bytes": 12,
                "metadata_record_bytes": 400,
                "retained_code_graph_estimate_bytes": 100,
                "retained_accounted_bytes": 2048,
                "report_text_bytes": 1024,
                "peak_temporary_bytes": 4096,
                "peak_code_graph_bytes": 100,
                "peak_marshal_buffer_bytes": 12,
                "peak_combined_store_bytes": 8192,
                "max_cache_accounted_bytes": 8 * 1024 * 1024,
                "max_temporary_bytes": 16 * 1024 * 1024,
                "capture_limit_bytes": 64 * 1024 * 1024,
                "store_limit_bytes": 128 * 1024 * 1024,
            },
        }
        report_text = json.dumps(
            report, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        for _ in range(4):
            report["accounting"]["report_text_bytes"] = len(report_text.encode("ascii"))
            report_text = json.dumps(
                report, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )
        validated = driver.validate_preinit_code_cache_report(
            report_text,
            captured=captured,
            provenance=provenance,
            toolchain=toolchain,
        )
        self.assertEqual(validated["cache_entry_count"], 1)

        oversized = dict(report)
        oversized["accounting"] = dict(report["accounting"])
        oversized["accounting"]["retained_accounted_bytes"] = 8 * 1024 * 1024 + 1
        with self.assertRaises(ValueError):
            driver.validate_preinit_code_cache_report(
                json.dumps(oversized),
                captured=captured,
                provenance=provenance,
                toolchain=toolchain,
            )

        wrong_source = dict(report)
        wrong_source["entries"] = [dict(report["entries"][0], source_sha256="c" * 64)]
        with self.assertRaises(ValueError):
            driver.validate_preinit_code_cache_report(
                json.dumps(wrong_source),
                captured=captured,
                provenance=provenance,
                toolchain=toolchain,
            )

    def test_runtime_path_returns_fresh_marshaled_objects_without_compile_fallback(
        self,
    ):
        helper_path = HERE / "schema_probe/code_cache.py"
        spec = importlib.util.spec_from_file_location(
            "fresh_schema_code_cache", helper_path
        )
        cache = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cache)
        source = b"captured source bytes"
        filename = "/work/stage/pkg.py"
        cache._INTERPRETER = cache._interpreter_record()
        identity, digest = cache._source_identity("pkg", "pkg.py", source, filename)
        payload = b"guest-created marshal payload"
        record = {
            "fullname": "pkg",
            "source_name": "pkg.py",
            "source_bytes": len(source),
            "source_sha256": digest,
            "filename": filename,
            "compile_mode": "exec",
            "compile_flags": 0,
            "dont_inherit": True,
            "optimize_argument": -1,
            "actual_optimize": cache.sys.flags.optimize,
            "marshal_version": cache.marshal.version,
            "cache_sha256": hashlib.sha256(payload).hexdigest(),
            "code_graph_bytes": 128,
            "cache_bytes": len(payload),
        }
        cache._CACHE = {identity: {"record": record, "payload": payload}}
        cache._CACHE_GRAPH_BYTES = 128
        cache._RECORD_BYTES = 32
        cache._CONFIGURED = True
        cache._OPEN = True
        cache.seal()
        self.assertIsNone(cache._CACHE)
        self.assertIsNone(cache._INTERPRETER)
        self.assertIsNone(cache._PREINIT_SEEN)
        frozen = cache._frozen_view()
        with self.assertRaises(TypeError):
            frozen[0][0][2][0] = ("changed", "value")
        # Mutable construction aliases cannot change the sealed tuple graph.
        record["source_sha256"] = "changed after seal"

        class FakeCode:
            def __init__(self, co_filename):
                self.co_filename = co_filename

        returned = [FakeCode(filename), FakeCode(filename)]
        with (
            patch.object(cache.types, "CodeType", FakeCode),
            patch.object(cache.marshal, "loads", side_effect=returned),
            patch.object(
                cache,
                "_owned_size",
                side_effect=AssertionError("runtime must not rescan the sealed cache"),
            ),
            patch(
                "builtins.compile",
                side_effect=AssertionError("cache hit must not compile"),
            ),
        ):
            first = cache.get_code("pkg", "pkg.py", source, filename, 4096)
            second = cache.get_code("pkg", "pkg.py", source, filename, 4096)

        self.assertIsNot(first, second)
        self.assertEqual(cache.runtime_stats()["cache_hits"], 2)
        self.assertEqual(cache.runtime_stats()["marshal_loads"], 2)
        cache._CACHE = {}
        with (
            patch(
                "builtins.compile",
                side_effect=AssertionError("must refuse inconsistent accounting"),
            ),
            self.assertRaisesRegex(ValueError, "structure or generation changed"),
        ):
            cache.get_code("pkg", "pkg.py", source, filename, 4096)

    def test_preinit_generation_rejects_host_cpython(self):
        helper_path = HERE / "schema_probe/code_cache.py"
        spec = importlib.util.spec_from_file_location(
            "host_must_not_generate_schema_code_cache", helper_path
        )
        cache = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cache)
        with self.assertRaisesRegex(RuntimeError, "fixed Wasm CPython preinit"):
            cache.configure()
        self.assertFalse(cache._OPEN)
        self.assertFalse(cache._CONFIGURED)

    def test_priming_uses_finder_resolution_without_executing_module_bodies(self):
        helper_path = HERE / "schema_probe/code_cache.py"
        spec = importlib.util.spec_from_file_location(
            "isolated_schema_code_cache_priming", helper_path
        )
        cache = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cache)
        cache._CONFIGURED = True
        cache._OPEN = True
        cache._INTERPRETER = cache._interpreter_record()

        class FakeCode:
            def __init__(self, co_filename):
                self.co_filename = co_filename

        class FakeSpec:
            def __init__(self, loader):
                self.loader = loader

        class FakeTree:
            def __init__(self):
                self.files = {
                    "bad.py": b"syntax-error-source",
                    "dual.py": b"shadowed-module-source",
                    "dual/__init__.py": b"package-source",
                    "pkg/__init__.py": b"package-init-source",
                    "pkg/lazy.py": b"lazy-module-source",
                }
                self.accounted_bytes = 512

        class FakeLoader:
            def __init__(self, finder, name, source):
                self.finder, self.name, self.source = finder, name, source

            def get_code(self, fullname):
                if fullname != self.name:
                    raise ImportError(fullname)
                source = self.finder.tree.files[self.source]
                return cache.get_code(
                    fullname,
                    self.source,
                    source,
                    self.finder.root + "/" + self.source,
                    self.finder.tree.accounted_bytes,
                )

            def exec_module(self, _module):
                raise AssertionError("priming must never execute module bodies")

        class FakeFinder:
            def __init__(self):
                self.tree = FakeTree()
                self.root = "/captured"
                self.loaders = []

            def find_spec(self, fullname):
                relative = fullname.replace(".", "/")
                for source, _package in (
                    (relative + "/__init__.py", True),
                    (relative + ".py", False),
                ):
                    if source in self.tree.files:
                        loader = FakeLoader(self, fullname, source)
                        self.loaders.append(loader)
                        return FakeSpec(loader)
                return None

        calls = []

        def fake_compile(source, filename, mode, *, dont_inherit):
            calls.append((source, filename, mode, dont_inherit))
            if source == b"syntax-error-source":
                raise SyntaxError("deferred until runtime")
            return FakeCode(filename)

        finder = FakeFinder()
        with (
            patch.object(cache.types, "CodeType", FakeCode),
            patch.object(cache, "_code_graph_size", return_value=128),
            patch.object(cache.marshal, "dumps", return_value=b"fake-marshal-data"),
            patch("builtins.compile", side_effect=fake_compile),
        ):
            cache.prime_sources(finder)
            cache.seal()
            with self.assertRaises(SyntaxError):
                cache.get_code(
                    "bad",
                    "bad.py",
                    b"syntax-error-source",
                    "/captured/bad.py",
                    finder.tree.accounted_bytes,
                )

        self.assertEqual(cache._PREINIT_PRIME_CALLS, 4)
        self.assertEqual(cache._PREINIT_PRIME_COMPLETED, 3)
        self.assertEqual(cache._PREINIT_PRIME_SKIPPED["compile_error_fallback"], 1)
        self.assertEqual(cache._PREINIT_PRIME_SKIPPED["shadowed_path"], 1)
        self.assertEqual(cache._RUNTIME_COMPILES, 1)
        self.assertIsNone(cache._PREINIT_SEEN)
        guest_report = json.loads(cache.report())
        self.assertEqual(
            guest_report["accounting"]["retained_accounted_bytes"],
            cache._cache_accounted_bytes(),
        )
        self.assertEqual(
            guest_report["accounting"]["peak_temporary_bytes"],
            cache._PEAK_TEMPORARY_BYTES,
        )
        self.assertEqual(
            {call[0] for call in calls},
            {
                b"syntax-error-source",
                b"package-source",
                b"package-init-source",
                b"lazy-module-source",
            },
        )
        self.assertTrue(all(call[2:] == ("exec", True) for call in calls))
        self.assertTrue(all(loader.name for loader in finder.loaders))
        self.assertNotIn(
            "dual.py",
            {entry["source_name"] for entry in guest_report["entries"]},
        )

    def test_controller_is_default_off_and_workflow_is_nonaccepting_text_only(self):
        common = dict(
            allow_ui_wall_clock=True,
            allow_ui_monotonic_clock=True,
            allow_inactive_ui_libc=True,
            allow_ci_link_trace=True,
            predecessor_paths=(Path("a"), Path("b"), Path("c"), Path("d")),
        )
        build_schema._validate_schema_preinit_code_cache(False, **common)
        build_schema._validate_schema_preinit_code_cache(True, **common)
        with self.assertRaises(ValueError):
            build_schema._validate_schema_preinit_code_cache(
                True,
                **{
                    **common,
                    "predecessor_paths": (Path("a"), None, Path("c"), Path("d")),
                },
            )
        with self.assertRaises(ValueError):
            build_schema._validate_schema_preinit_code_cache(
                True, **{**common, "allow_ci_link_trace": False}
            )
        controller_source = (HERE / "build_schema.py").read_text()
        driver_source = (HERE / "schema_probe/driver.py").read_text()
        helper_source = (HERE / "schema_probe/code_cache.py").read_text()
        self.assertIn("schema_preinit_code_cache: bool = False", controller_source)
        self.assertIn(
            'parser.add_argument("--schema-preinit-code-cache", action="store_true")',
            controller_source,
        )
        self.assertIn(
            "diagnostic-only preinit code-cache comparison never accepts", driver_source
        )
        self.assertIn('"dont_inherit": True', helper_source)
        self.assertIn('"compile_flags": 0', helper_source)
        self.assertIn('"optimize_argument": -1', helper_source)
        workflow = (
            HERE.parents[1] / ".github/workflows/issue-1520-ui-schema-code-cache.yml"
        ).read_text()
        self.assertIn("--schema-preinit-code-cache", workflow)
        self.assertIn("if: always()", workflow)
        self.assertNotIn("continue-on-error", workflow)
        self.assertNotIn("outer.wasm", workflow)
        self.assertNotIn("component.wasm", workflow)
        self.assertIn("schema-preinit-code-cache.json", workflow)
        self.assertIn("schema-preinit-code-cache-provenance.json", workflow)


if __name__ == "__main__":
    unittest.main()
