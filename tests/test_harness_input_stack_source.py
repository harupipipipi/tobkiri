"""Source-level checks for the inactive captured-input verifier probe."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "scripts/wasm"),
    str(ROOT / "tools/python313-producer/harness_input_stack"),
    str(ROOT / "packs/local.ai-tool-bridge-ordered-python"),
    str(ROOT / "tobkiri_runtime"),
]

import prepare_harness_input_stack as prepare  # noqa: E402 - controlled source paths.
import logic  # noqa: E402
from ordered_json_v2 import dumps, loads  # noqa: E402
from tobkiri_protocol.errors import ProtocolError  # noqa: E402
from tobkiri_protocol.platform_artifact import (  # noqa: E402
    artifact_digest,
    verify_platform_artifact,
)


def _variant(path: str, digest: str, entrypoint_digest: str) -> dict[str, str]:
    return {
        "relative_path": path,
        "entrypoint": path,
        "artifact_digest": digest,
        "entrypoint_digest": entrypoint_digest,
        "platform": "linux",
        "architecture": "x86_64",
        "bundle_identity": "io.tobkiri.input-stack-fixture",
    }


def _frame(path: str, content: bytes, variant: dict[str, str]) -> str:
    return dumps({
        "files": [{"path": path, "size": len(content)}],
        "variant": variant,
    })


class ProbeLogicTests(unittest.TestCase):
    """Exercise the original verifier through finite captured DATA only."""

    def test_resource_failure_is_not_reclassified_as_invalid_frame(self) -> None:
        probe = logic.Logic()
        with patch.object(logic, "loads", side_effect=RecursionError("original decoder limit")):
            with self.assertRaises(RecursionError):
                probe.init("{}")
        with self.assertRaises(logic.NotInitializedError):
            probe.run(lambda *_: b"")

    def test_init_is_strict_atomic_and_single_use(self) -> None:
        probe = logic.Logic()
        with self.assertRaises(logic.NotInitializedError):
            probe.run(lambda *_: b"")

        invalid = [
            "{\"files\":[],\"files\":[],\"variant\":{}}",
            dumps({"files": [], "variant": {}}),
            dumps({"files": [{"path": "../escape", "size": 1}],
                   "variant": _variant("x", "sha256:" + "0" * 64,
                                       "sha256:" + "0" * 64)}),
            dumps({"files": [{"path": "a", "size": True}],
                   "variant": _variant("a", "sha256:" + "0" * 64,
                                       "sha256:" + "0" * 64)}),
            dumps({"files": [], "variant": {
                **_variant("x", "sha256:" + "0" * 64, "sha256:" + "0" * 64),
                "extra": "rejected",
            }}),
        ]
        for payload in invalid:
            with self.subTest(payload=payload[:80]):
                with self.assertRaises(logic.InvalidInputError):
                    probe.init(payload)
                self.assertIsNone(probe._initialized)

        accepted = dumps({"files": [], "variant": _variant(
            "x", "sha256:" + "0" * 64, "sha256:" + "0" * 64,
        )})
        probe.init(accepted)
        state = probe._initialized
        with self.assertRaises(logic.AlreadyInitializedError):
            probe.init(accepted)
        with self.assertRaises(logic.AlreadyInitializedError):
            probe.init("not-json")
        self.assertIs(probe._initialized, state)

    def test_original_verifier_repeats_chunks_and_returns_relative_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact_root = Path(temporary).resolve()
            raw = bytearray(index % 251 for index in range(2 * 65_536 + 17))
            raw[:6] = bytes.fromhex("7f454c460201")
            raw[18:20] = (62).to_bytes(2, "little")
            content = bytes(raw)
            artifact_path = artifact_root / "artifact"
            artifact_path.write_bytes(content)
            variant = _variant(
                "artifact",
                artifact_digest(artifact_path),
                "sha256:" + hashlib.sha256(content).hexdigest(),
            )

            # Independent native oracle: call the untouched source directly
            # over the real temporary filesystem fixture.
            native_result = verify_platform_artifact(artifact_root, variant)
            expected_relative = native_result.relative_to(artifact_root).as_posix()

            probe = logic.Logic()
            probe.init(_frame("artifact", content, variant))
            observed: list[tuple[int, int, int, bytes]] = []

            def read_range(ordinal: int, offset: int, length: int) -> bytes:
                self.assertEqual(ordinal, 0)
                returned = content[offset:offset + length]
                observed.append((ordinal, offset, length, returned))
                return returned

            first = loads(probe.run(read_range))
            first_trace = list(observed)
            observed.clear()
            second = loads(probe.run(read_range))

            self.assertEqual(first["outcome"], {
                "status": "verified",
                "returned_relative_path": expected_relative,
            })
            self.assertEqual(second["outcome"], first["outcome"])
            self.assertEqual(first["calls"], second["calls"])
            self.assertTrue(first["local_marker_same_object"])
            self.assertTrue(second["local_marker_same_object"])

            expected_ranges = [
                (0, 0, 65_536),
                (0, 65_536, 65_536),
                (0, 131_072, 17),
                (0, 0, 65_536),
                (0, 65_536, 65_536),
                (0, 131_072, 17),
                (0, 0, 64),
            ]
            self.assertEqual(
                [(ordinal, offset, length) for ordinal, offset, length, _ in first_trace],
                expected_ranges,
            )
            self.assertEqual(
                [(item["ordinal"], item["offset"], item["length"], item["returned"])
                 for item in first["calls"]],
                [(*item, item[2]) for item in expected_ranges],
            )
            self.assertEqual(
                [returned for _, offset, length, returned in first_trace],
                [content[offset:offset + length] for _, offset, length in expected_ranges],
            )

    def test_protocol_error_observation_matches_the_original_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact_root = Path(temporary).resolve()
            content = b"present but deliberately pinned to different bytes"
            artifact_path = artifact_root / "artifact"
            artifact_path.write_bytes(content)
            actual_digest = artifact_digest(artifact_path)
            wrong_digest = "sha256:" + (
                ("0" if actual_digest[7] != "0" else "1") + actual_digest[8:]
            )
            variant = _variant(
                "artifact",
                wrong_digest,
                "sha256:" + hashlib.sha256(content).hexdigest(),
            )
            with self.assertRaises(ProtocolError) as native_error:
                verify_platform_artifact(artifact_root, variant)

            probe = logic.Logic()
            probe.init(_frame("artifact", content, variant))
            output = loads(probe.run(
                lambda ordinal, offset, length: content[offset:offset + length]
            ))
            self.assertEqual(output["outcome"], {
                "status": "protocol_error",
                "error_type": (
                    f"{type(native_error.exception).__module__}."
                    f"{type(native_error.exception).__qualname__}"
                ),
                "error_args": list(native_error.exception.args),
            })
            self.assertTrue(output["calls"])
            self.assertTrue(output["local_marker_same_object"])


class SourceStageTests(unittest.TestCase):
    """Keep the WIT boundary and all copied source bytes hash-bound."""

    def test_source_capsule_and_plan_have_only_the_declared_interfaces(self) -> None:
        files = prepare.captured()
        provenance = json.loads(files["provenance.json"])
        plan = prepare.plan_for(files)

        self.assertEqual(plan["format"], "tobkiri.python-build/3")
        self.assertEqual(plan["world"], "native-input-stack")
        self.assertEqual(plan["outer_exports"], [prepare.PROBE_INTERFACE])
        self.assertEqual(plan["allowed_imports"], [prepare.CAPTURED_INTERFACE])
        self.assertEqual(provenance["outer_exports"], [prepare.PROBE_INTERFACE])
        self.assertEqual(provenance["allowed_imports"], [prepare.CAPTURED_INTERFACE])
        self.assertTrue(provenance["source_capsule_only"])
        self.assertFalse(provenance["source_capsule_executable"])
        self.assertEqual(
            files["wit/deps/captured-inputs/native-inputs.wit"],
            prepare.bounded_read(ROOT / prepare.NATIVE_INPUT_WIT),
        )
        wit = files["wit/probe.wit"].decode("utf-8")
        self.assertIn("package tobkiri-ui:native-input-stack@1.0.0;", wit)
        self.assertIn("interface probe", wit)
        self.assertIn("enum error-code", wit)
        self.assertIn("invalid-input,", wit)
        self.assertIn("already-initialized,", wit)
        self.assertIn("record error", wit)
        self.assertIn("init: func(payload: string) -> result<_, error>;", wit)
        self.assertIn("run: func() -> string;", wit)
        self.assertIn("import tobkiri-ui:inputs/captured@1.0.0;", wit)
        self.assertNotIn("tobkiri:core/", wit)
        self.assertNotIn("lifecycle", wit)
        self.assertEqual(provenance["method_rewrites"], [])
        self.assertFalse(provenance["build_performed"])
        self.assertFalse(provenance["wasm_execution_performed"])
        self.assertFalse(provenance["production_enabled"])
        self.assertFalse(provenance["native_os_proof_performed"])
        self.assertFalse(provenance["actual_os_owner_proof"])
        self.assertFalse(provenance["whole_setup_performed"])
        self.assertFalse(provenance["whole_setup_compatible"])
        self.assertFalse(provenance["sync_stack_async_wait_proven"])
        self.assertFalse(provenance["native_authority_created"])

        app = files["python/app.py"].decode("utf-8")
        self.assertIn("from componentize_py_types import Err", app)
        self.assertIn("from wit_world.exports.probe import Error, ErrorCode", app)
        self.assertIn("ErrorCode.INVALID_INPUT", app)
        self.assertIn("ErrorCode.ALREADY_INITIALIZED", app)
        self.assertIn("captured.read_range(ordinal, offset, length)", app)
        self.assertIn("return self._logic.run(_read_range)", app)
        self.assertNotIn("tobkiri.core", app)

        source_records = {item["staged_as"]: item for item in provenance["sources"]}
        self.assertEqual(set(source_records), set(files) - {"provenance.json"})
        for staged_as, record in source_records.items():
            self.assertEqual(files[staged_as], prepare.bounded_read(ROOT / record["source_path"]))
            self.assertEqual(hashlib.sha256(files[staged_as]).hexdigest(), record["sha256"])

    def test_complete_original_function_bodies_are_hash_bound(self) -> None:
        files = prepare.captured()
        provenance = json.loads(files["provenance.json"])
        bodies = provenance["original_function_bodies"]
        actual_symbols = {}
        for item in bodies:
            actual_symbols.setdefault(item["source_path"], set()).add(item["symbol"])
        for source_path, expected_symbols in prepare.FUNCTIONS.items():
            self.assertEqual(actual_symbols[source_path], set(expected_symbols))
        for item in bodies:
            source = prepare.bounded_read(ROOT / item["source_path"])
            selected = source[item["source_start_byte"]:item["source_end_byte"]]
            self.assertEqual(hashlib.sha256(selected).hexdigest(), item["sha256"])
            self.assertTrue(item["complete_definition"])
            self.assertIn(item["source_path"], {
                record["source_path"] for record in provenance["sources"]
            })

        metadata_digest = provenance.pop("metadata_sha256")
        self.assertEqual(
            hashlib.sha256(prepare._canonical_json_bytes(provenance)).hexdigest(),
            metadata_digest,
        )

    def test_fresh_private_stage_and_source_change_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary).resolve() / "stage"
            original_files = prepare.captured()
            plan = prepare.stage(output)
            self.assertEqual(json.loads((output / "plan.json").read_bytes()), plan)
            for relative, content in original_files.items():
                self.assertEqual((output / relative).read_bytes(), content)
            original_stage = {
                relative: (output / relative).read_bytes()
                for relative in ("plan.json", "provenance.json")
            }
            with self.assertRaises(FileExistsError):
                prepare.stage(output)
            self.assertEqual(
                {relative: (output / relative).read_bytes()
                 for relative in ("plan.json", "provenance.json")},
                original_stage,
            )

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary).resolve() / "changed-source"
            original_read = prepare.bounded_read
            source_path = ROOT / prepare.SOURCES["python/logic.py"]
            calls = 0

            def changed_on_second_capture(path: Path, limit: int = 8 * 1024 * 1024) -> bytes:
                nonlocal calls
                raw = original_read(path, limit)
                if Path(path) == source_path:
                    calls += 1
                    if calls >= 2:
                        return raw + b"\n"
                return raw

            with patch.object(prepare, "bounded_read", side_effect=changed_on_second_capture):
                with self.assertRaisesRegex(ValueError, "source changed during staging"):
                    prepare.stage(output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
