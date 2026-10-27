"""Offline boundaries for the separate inactive poll/schema consumer."""

import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
build_schema = importlib.import_module("build_schema")
driver = importlib.import_module("schema_probe.driver")


def _write_private(path: Path, contents: bytes | str) -> None:
    if isinstance(contents, str):
        path.write_text(contents)
    else:
        path.write_bytes(contents)
    path.chmod(0o600)


def _inactive_trace_derivation():
    original_manifest = {"bytes": 10, "sha256": "a" * 64, "mode": "0o644"}
    wall_manifest = {"bytes": 11, "sha256": "b" * 64, "mode": "0o644"}
    monotonic_manifest = {"bytes": 12, "sha256": "c" * 64, "mode": "0o644"}
    traced_manifest = {"bytes": 13, "sha256": "d" * 64, "mode": "0o644"}
    original_stub = {"bytes": 20, "sha256": "e" * 64, "mode": "0o644"}
    wall_stub = {"bytes": 21, "sha256": "f" * 64, "mode": "0o644"}
    monotonic_stub = {"bytes": 22, "sha256": "1" * 64, "mode": "0o644"}
    original_link = {
        "bytes": 30,
        "sha256": build_schema.ci_link_trace_source.ORIGINAL_LINK_SHA256,
        "mode": "0o644",
    }
    traced_link = {"bytes": 31, "sha256": "2" * 64, "mode": "0o644"}
    wall = {
        "feature": "ui-wall-clock",
        "interface_prefix": "wasi:clocks/wall-clock@",
        "functions": ["now", "resolution"],
        "stub_wasi_retained": True,
        "changed_files": {
            "Cargo.toml": {"original": original_manifest, "candidate": wall_manifest},
            "src/stubwasi.rs": {"original": original_stub, "candidate": wall_stub},
        },
    }
    mono = {
        "feature": "ui-monotonic-clock",
        "feature_dependencies": ["ui-wall-clock"],
        "interface_prefix": "wasi:clocks/monotonic-clock@",
        "import_interface": "tobkiri-ui:clock/monotonic@1.0.0",
        "functions": ["now", "resolution"],
        "result": "u64 nanoseconds",
        "other_exports": "original signatures and unreachable bodies",
        "stub_wasi_retained": True,
        "changed_files": {
            "Cargo.toml": {
                "wall_clock_candidate": wall_manifest,
                "candidate": monotonic_manifest,
            },
            "src/stubwasi.rs": {
                "wall_clock_candidate": wall_stub,
                "candidate": monotonic_stub,
            },
        },
    }
    trace = {
        "feature": build_schema.ci_link_trace_source.FEATURE,
        "upstream_link_sha256": build_schema.ci_link_trace_source.ORIGINAL_LINK_SHA256,
        "path": "/work/link-trace",
        "maximum_members": 128,
        "maximum_file_bytes": 32 * 1024 * 1024,
        "maximum_total_bytes": 64 * 1024 * 1024,
        "stage": "actual selected libraries and linked component before snapshot",
        "final_component_binding_verified": False,
        "final_provider_resolution_verified": False,
        "production_enabled": False,
        "changed_files": {
            "Cargo.toml": {"before": monotonic_manifest, "after": traced_manifest},
            "src/link.rs": {"before": original_link, "after": traced_link},
        },
    }
    source = {
        "files": {
            "Cargo.toml": traced_manifest,
            "src/stubwasi.rs": monotonic_stub,
            "src/link.rs": traced_link,
            "libc.so.zst": {"bytes": 40, "sha256": "3" * 64, "mode": "0o644"},
        },
        "original_files": {
            "Cargo.toml": original_manifest,
            "src/stubwasi.rs": original_stub,
            "src/link.rs": original_link,
        },
        "ui_wall_clock": wall,
        "ui_monotonic_clock": mono,
        "ci_link_trace": trace,
    }
    marker = {
        "format": "tobkiri.ui-empty-poll-ci-asset/1",
        "build_receipt": {"bytes": 50, "sha256": "4" * 64},
        "source_pin": {"revision": "fixed"},
        "derivation": {"source": "fixed"},
        "original_libc": {"bytes": 41, "sha256": "5" * 64},
        "candidate_libc": {"bytes": 40, "sha256": "3" * 64},
        "changed_asset": "libc.so.zst",
        "runtime_asset_override": False,
        "scope": "inactive CI original/candidate comparison producer only",
        "c_abi_verified": False,
        "final_provider_resolution_verified": False,
        "cold_schema_verified": False,
        "production_enabled": False,
    }
    source["ci_link_trace"] = trace
    cli = {
        "ui_wall_clock_candidate": wall,
        "ui_monotonic_clock_candidate": mono,
        "ui_empty_poll_libc_candidate": marker,
        "ci_link_trace_candidate": trace,
    }
    return cli, source


class InactivePollConsumerTests(unittest.TestCase):
    def test_run_refuses_inactive_mode_without_all_predecessor_inputs(self):
        missing = Path("/must-not-be-read")
        with (
            patch.object(build_schema.sys, "platform", "linux"),
            patch.object(build_schema.sys, "version_info", (3, 13, 16)),
            patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=True),
            self.assertRaisesRegex(ValueError, "all four exact predecessor inputs"),
        ):
            build_schema.run(
                *([missing] * 6),
                allow_ui_wall_clock=True,
                allow_ui_monotonic_clock=True,
                allow_inactive_ui_libc=True,
                allow_ci_link_trace=True,
            )

    def test_poll_candidate_and_trace_are_refused_without_the_full_opt_in(self):
        cli, source = _inactive_trace_derivation()
        with self.assertRaisesRegex(ValueError, "exact opt-in"):
            build_schema.verify_clock_variant(
                cli, source, allow_ui_wall_clock=True, allow_ui_monotonic_clock=True
            )
        with self.assertRaisesRegex(ValueError, "monotonic"):
            build_schema.verify_clock_variant(
                cli,
                source,
                allow_ui_wall_clock=True,
                allow_ui_monotonic_clock=True,
                allow_inactive_ui_libc=True,
            )

    def test_clock_monotonic_and_trace_receipts_must_form_one_chain(self):
        cli, source = _inactive_trace_derivation()
        build_schema.verify_clock_variant(
            cli,
            source,
            allow_ui_wall_clock=True,
            allow_ui_monotonic_clock=True,
            allow_inactive_ui_libc=True,
            allow_ci_link_trace=True,
        )

        bad_chain = copy.deepcopy(source)
        bad_chain["ci_link_trace"]["changed_files"]["Cargo.toml"]["before"] = source[
            "ui_wall_clock"
        ]["changed_files"]["Cargo.toml"]["candidate"]
        bad_cli = copy.deepcopy(cli)
        bad_cli["ci_link_trace_candidate"] = bad_chain["ci_link_trace"]
        with self.assertRaisesRegex(ValueError, "ancestry"):
            build_schema.verify_clock_variant(
                bad_cli,
                bad_chain,
                allow_ui_wall_clock=True,
                allow_ui_monotonic_clock=True,
                allow_inactive_ui_libc=True,
                allow_ci_link_trace=True,
            )

        bad_source = copy.deepcopy(source)
        bad_source["files"]["src/link.rs"] = {
            **bad_source["files"]["src/link.rs"],
            "sha256": "9" * 64,
        }
        bad_cli = copy.deepcopy(cli)
        bad_cli["ci_link_trace_candidate"] = bad_source["ci_link_trace"]
        with self.assertRaisesRegex(ValueError, "final inventory"):
            build_schema.verify_clock_variant(
                bad_cli,
                bad_source,
                allow_ui_wall_clock=True,
                allow_ui_monotonic_clock=True,
                allow_inactive_ui_libc=True,
                allow_ci_link_trace=True,
            )

    def test_raw_c_predecessor_authentication_requires_exact_successful_metadata(self):
        pins = json.loads(build_schema.POLL_REFERENCE_PIN_PATH.read_bytes())
        run = {
            "id": pins["run"],
            "head_sha": pins["head"],
            "path": pins["workflow"],
            "repository": {"full_name": pins["repository"]},
            "status": "completed",
            "conclusion": "success",
        }
        job = {
            "id": pins["job"],
            "run_id": pins["run"],
            "name": "actual-c-reference",
            "status": "completed",
            "conclusion": "success",
            "steps": [
                {"name": name, "status": "completed", "conclusion": "success"}
                for name in build_schema.POLL_REFERENCE_REQUIRED_STEPS
            ],
        }
        artifact = {
            "id": pins["artifact"],
            "name": pins["artifact_name"],
            "expired": False,
            "size_in_bytes": pins["artifact_bytes"],
            "digest": "sha256:" + pins["artifact_sha256"],
            "workflow_run": {"id": pins["run"], "head_sha": pins["head"]},
        }
        build_schema._authenticate_poll_reference(pins, run, job, artifact)

        bad_job = copy.deepcopy(job)
        bad_job["steps"][0]["conclusion"] = "skipped"
        with self.assertRaisesRegex(ValueError, "run/job/artifact"):
            build_schema._authenticate_poll_reference(pins, run, bad_job, artifact)

        bad_artifact = copy.deepcopy(artifact)
        bad_artifact["digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "run/job/artifact"):
            build_schema._authenticate_poll_reference(pins, run, job, bad_artifact)

    def test_schema_trace_observes_modules_without_a_c_helper_requirement(self):
        module = b"\0asm\x01\0\0\0fixture-module"
        preinit = b"\0asm\x0d\0\x01\0preinit-component"
        final = b"\0asm\x0d\0\x01\0final-stubbed-component"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "link-trace"
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            _write_private(root / "0000.wasm", module)
            _write_private(root / "preinit-linked-component.wasm", preinit)
            manifest = (
                "tobkiri-ci-link-inputs/1\n"
                + "0\t"
                + b"libc.so".hex()
                + "\tfalse\t"
                + str(len(module))
                + "\n"
            )
            _write_private(root / "libraries.tsv", manifest)
            _write_private(
                root / "complete.tsv",
                f"linked-before-snapshot\t1\t{len(preinit)}\n",
            )
            report = driver.observe_link_trace(root, final)

        self.assertEqual(report["libraries"][0]["name"], "libc.so")
        self.assertNotEqual(report["preinit_component"], report["final_component"])
        self.assertEqual(
            report["final_component"],
            {
                "bytes": len(final),
                "sha256": hashlib.sha256(final).hexdigest(),
            },
        )
        self.assertTrue(report["actual_input_observation"])
        self.assertFalse(report["final_provider_resolution_verified"])
        self.assertFalse(report["production_enabled"])

    def test_schema_trace_rejects_stale_completion_and_extra_members(self):
        module = b"\0asm\x01\0\0\0fixture-module"
        preinit = b"\0asm\x0d\0\x01\0preinit-component"
        final = b"\0asm\x0d\0\x01\0final-component"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "link-trace"
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            _write_private(root / "0000.wasm", module)
            _write_private(root / "preinit-linked-component.wasm", preinit)
            _write_private(
                root / "libraries.tsv",
                "tobkiri-ci-link-inputs/1\n"
                + "0\t"
                + b"libc.so".hex()
                + "\tfalse\t"
                + str(len(module))
                + "\n",
            )
            _write_private(root / "complete.tsv", "linked-before-snapshot\t1\t0\n")
            with self.assertRaisesRegex(ValueError, "completion"):
                driver.observe_link_trace(root, final)

            _write_private(
                root / "complete.tsv",
                f"linked-before-snapshot\t1\t{len(preinit)}\n",
            )
            (root / "libraries.tsv").chmod(0o644)
            with self.assertRaisesRegex(ValueError, "private"):
                driver.observe_link_trace(root, final)
            (root / "libraries.tsv").chmod(0o600)

            bad_module = b"not-a-core-module".ljust(len(module), b"!")
            _write_private(root / "0000.wasm", bad_module)
            with self.assertRaisesRegex(ValueError, "core Wasm"):
                driver.observe_link_trace(root, final)
            _write_private(root / "0000.wasm", module)

            _write_private(root / "unexpected.txt", "extra")
            with self.assertRaisesRegex(ValueError, "extra"):
                driver.observe_link_trace(root, final)


if __name__ == "__main__":
    unittest.main()
