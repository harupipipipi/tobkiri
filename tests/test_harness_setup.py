"""Native source differential checks, not Wasm or full Setup acceptance.

The behavioral oracle imports the complete original application module. The
guest-binding doubles below check error mapping only; they grant no authority.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs/local.harness-setup-python"
sys.path[:0] = [str(ROOT / "scripts/wasm"), str(PACK / "python"),
                str(ROOT / "packs/local.ai-tool-bridge-ordered-python"),
                str(ROOT / "tobkiri_runtime")]

import prepare_harness_setup as prepare
import setup_adapter as adapter
from ordered_json_v2 import dumps, loads
from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
    DefaultspackProfileRuntime,
)
from core_runtime.profile_runtime_port import SetupActivationDecision


def request(confirmation: object, *, confirmed: object = True) -> dict:
    """Make an ordinary serialized v4 request without any native capability."""
    return {"setup_api_version": "io.tobkiri.setup-state.v4",
            "operation_id": "defaults.activate", "confirmed": confirmed,
            "confirmation": confirmation}


class SetupSourceTests(unittest.TestCase):
    """Compare original semantics and preserve source/build custody."""

    def setUp(self) -> None:
        self.runtime = DefaultspackProfileRuntime()

    def decision(self, body: dict, listing: dict | None) -> dict:
        """Compare complete original decisions, including ordered output bytes."""
        original = self.runtime.setup_activation_decision(body, listing)
        expected = {"confirmation": original.confirmation, "response": original.response}
        actual = adapter.invoke("decision", dumps({"body": body, "listing": listing}))
        self.assertEqual(actual, dumps(expected))
        return loads(actual)

    def test_exact_request_and_arbitrary_confirmation_order(self) -> None:
        confirmation = {"plan_digest": "recorded-plan", "profile_id": "defaults",
                        "bindings": [{"approved": True, "value": "日本語"}]}
        listing = {"recommended_default_profile": {"confirmation": confirmation}}
        actual = self.decision(request(confirmation), listing)
        self.assertEqual(actual["confirmation"], confirmation)
        self.assertIsNone(actual["response"])
        # These arbitrary facts are accepted DATA by the original pure seam;
        # neither this test nor the candidate validates/commits Host authority.

    def test_confirmation_mismatch_and_missing_preview_reject(self) -> None:
        listing = {"recommended_default_profile": {"confirmation": {"epoch": 4}}}
        for confirmation in ({"epoch": 3}, {}, None, [], "recorded"):
            with self.subTest(confirmation=confirmation):
                self.assertEqual(self.decision(request(confirmation), listing)
                                 ["response"]["state"], "review_required")
        for listing in ({}, {"recommended_default_profile": []},
                        {"recommended_default_profile": {"confirmation": []}}):
            self.decision(request({}), listing)

    def test_none_listing_preserves_original_host_preparation_seam(self) -> None:
        self.assertEqual(self.decision(request({"anything": True}), None)
                         ["confirmation"], {"anything": True})
        for confirmation in (None, [], "x", True):
            self.assertEqual(self.decision(request(confirmation), None)
                             ["response"]["state"], "review_required")

    def test_confirmation_requires_exact_true(self) -> None:
        for confirmed in (False, None, 0, 1, "true", [], {}):
            with self.subTest(confirmed=confirmed):
                response = self.decision(request({}, confirmed=confirmed), None)["response"]
                self.assertEqual(response["state"], "confirmation_required")
                self.assertEqual(response["write_set"], [])

    def test_exact_request_keys_and_original_operation_identity(self) -> None:
        ordinary = request({})
        cases = [{**ordinary, "approved": True},
                 {**ordinary, "operation_id": "harness.activate"},
                 {**ordinary, "setup_api_version": "io.tobkiri.setup-state.v3"}]
        cases += [{key: value for key, value in ordinary.items() if key != missing}
                  for missing in ordinary]
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(self.decision(body, None)["response"]["status_code"], 410)

    def test_success_is_exact_original_intermediate_projection(self) -> None:
        value = {"profile": {"profile_id": "defaults", "extra": "retained input"},
                 "plan": {"profile_revision": "revision", "plan_digest": "digest"},
                 "activation": {"activation_id": "activation", "security_epoch": 3,
                                "fencing_token": "fence",
                                "profile_authority_snapshot_digest": "authority"},
                 "audit_receipt": {"status": "recorded", "approved": True}}
        active = SimpleNamespace(resolved=SimpleNamespace(
            profile=value["profile"], plan=value["plan"]), activation=value["activation"])
        expected = self.runtime.setup_activation_success(active, value["audit_receipt"])
        actual = adapter.invoke("success", dumps(value))
        self.assertEqual(actual, dumps(expected))
        self.assertIs(loads(actual)["restart_required"], False)

    def test_incomplete_success_is_terminal_projection_error(self) -> None:
        value = {name: {} for name in ("profile", "plan", "activation", "audit_receipt")}
        with self.assertRaises(adapter.SetupProjectionError):
            adapter.invoke("success", dumps(value))

    def test_original_failure_and_retired_responses(self) -> None:
        self.assertEqual(adapter.invoke("failure", "{}"),
                         dumps(self.runtime.setup_activation_failure()))
        for route in (None, "", "/api/setup/packs/install", "日本語"):
            self.assertEqual(adapter.invoke("retired", dumps({"route": route})),
                             dumps(self.runtime.retired_setup_response(route=route)))

    def test_malformed_boundary_is_rejected_without_fallback(self) -> None:
        cases = [("decision", "[]"), ("decision", "{}"),
                 ("decision", '{"body":{},"listing":null,"approved":true}'),
                 ("decision", '{"body":{},"listing":null,"listing":{}}'),
                 ("success", '{"profile":{},"plan":[],"activation":{},"audit_receipt":{}}'),
                 ("failure", '{"approved":true}'), ("retired", '{"route":true}'),
                 ("unknown", "{}"), ("failure", "NaN")]
        for operation, payload in cases:
            with self.subTest(operation=operation, payload=payload):
                with self.assertRaises(adapter.SetupInputError):
                    adapter.invoke(operation, payload)

    def test_original_decision_invariant_is_preserved(self) -> None:
        for cls in (SetupActivationDecision, adapter.DefaultspackProfileRuntime
                    .setup_activation_decision.__globals__["SetupActivationDecision"]):
            for fields in ({}, {"confirmation": {}, "response": {}}):
                with self.assertRaises(ValueError):
                    cls(**fields)

    def test_committed_extraction_and_every_original_line(self) -> None:
        generated = prepare.verify()
        provenance = json.loads(generated["provenance.json"])
        module = generated["original_setup.py"]
        self.assertEqual(provenance["method_rewrites"], [])
        for definition in provenance["definitions"]:
            data = (ROOT / definition["source_path"]).read_bytes()
            selected = data[definition["source_start_byte"]:definition["source_end_byte"]]
            self.assertIn(selected, module)
        tree = ast.parse(module)
        runtime = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                       and node.name == "DefaultspackProfileRuntime")
        self.assertEqual([node.name for node in runtime.body], list(prepare.METHODS))
        self.assertNotIn(b"register_profile_runtime", module)
        self.assertNotIn(b"activation_store", module)

    def test_opaque_ordered_data_uses_string_pins_instead_of_core_json_encoding(self) -> None:
        sys.path.insert(0, str(ROOT / 'sdk/python'))
        try:
            from composition import world_export_names_from_sources
            wit = (PACK / 'wit/pack.wit').read_bytes()
            self.assertEqual(world_export_names_from_sources([
                ('pack.wit', wit), ('deps/core.wit',
                    (ROOT / 'crates/tobkiri-wit/wit/core/core.wit').read_bytes())],
                'pack', root_source='pack.wit'),
                ['local:harness-setup-python/setup@1.0.0', 'tobkiri:core/lifecycle@1.0.0'])
            for operation in ('decision', 'success', 'failure', 'retired',
                              'bootstrap-confirmation', 'profile-confirmation'):
                self.assertIn(f'{operation}: func(payload: string) -> result<string, error>;'.encode(), wit)
        finally:
            sys.path.pop(0)

    def test_stage_is_source_only_and_sdk_verifies_exact_plan(self) -> None:
        sys.path.insert(0, str(ROOT / "sdk/python"))
        try:
            import build_component
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary).resolve() / "source"
                plan = prepare.stage(output)
                actual, _, _, captures = build_component._verify_plan(output / "plan.json")
                self.assertEqual(actual, plan)
                self.assertEqual(len(captures), 11)
                self.assertEqual(plan["outer_exports"],
                                 ["tobkiri:core/lifecycle@1.0.0", prepare.INTERFACE])
                self.assertEqual({str(name) for name, _ in captures},
                                 {"app.py", "setup_adapter.py", "original_setup.py",
                                  "ordered_json.py", "ordered_json_v2.py", "wit/pack.wit",
                                  "wit/deps/tobkiri-core/core.wit",
                                  *{f"tobkiri_protocol/{name}.py" for name in
                                    ("__init__", "canonical", "errors", "ids")}})
                for name in ("__init__", "canonical", "errors", "ids"):
                    self.assertEqual(
                        (output / f"python/tobkiri_protocol/{name}.py").read_bytes(),
                        (ROOT / f"tobkiri_runtime/tobkiri_protocol/{name}.py").read_bytes())
                with self.assertRaises(FileExistsError):
                    prepare.stage(output)
        finally:
            sys.path.pop(0)

    def test_stale_original_source_cannot_be_staged(self) -> None:
        original = prepare.captured
        def stale() -> dict:
            files = original()
            files["original_setup.py"] += b"# changed\n"
            return files
        with patch.object(prepare, "captured", stale):
            with self.assertRaises(ValueError):
                prepare.verify()

    def test_actual_replay_inputs_are_complete_original_oracle_cases(self) -> None:
        spec = importlib.util.spec_from_file_location(
            'setup_standalone_replay', ROOT / 'scripts/wasm/replay_harness_setup.py')
        replay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(replay)
        self.assertIs(replay.DefaultspackProfileRuntime, DefaultspackProfileRuntime)
        cases = replay.source_cases()
        self.assertEqual(len(cases), 30)
        self.assertEqual(len({item['id'] for item in cases}), 30)
        self.assertEqual({item['operation'] for item in cases},
                         {'decision', 'success', 'failure', 'retired',
                          'bootstrap-confirmation', 'profile-confirmation'})
        for case in cases:
            if 'expected' in case:
                self.assertEqual(adapter.invoke(case['operation'], case['input']), case['expected'])
            else:
                error = (adapter.SetupInputError if case['expected_error']['code'] == 'invalid'
                         else adapter.SetupProjectionError)
                with self.assertRaises(error):
                    adapter.invoke(case['operation'], case['input'])

    def test_confirmation_digest_binds_optional_predecessors_without_native_authority(self) -> None:
        from tobkiri_protocol.canonical import canonical_digest
        spec = importlib.util.spec_from_file_location(
            'setup_confirmation_source_cases', ROOT / 'scripts/wasm/replay_harness_setup.py')
        replay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(replay)
        cases = replay.confirmation_cases(self.runtime)
        digests = []
        for case in cases[:5]:
            value = loads(case['input'])
            before = dumps(value)
            result = loads(adapter.invoke(case['operation'], before))
            digest = result.pop('confirmation_digest')
            self.assertEqual(digest, canonical_digest(result))
            self.assertEqual(dumps(value), before)
            self.assertEqual(result['operation_id'], 'defaults.activate')
            for key in ('definition_predecessor', 'pack_pin_predecessor'):
                self.assertEqual(key in result, value[key] is not None)
                if value[key] is not None:
                    self.assertEqual(result[key], value[key])
            digests.append(digest)
        self.assertEqual(len(set(digests)), 5)
        # Output object order is preserved while canonical identity ignores
        # source map order, using the original canonicalizer in both runtimes.
        results = [loads(adapter.invoke(item['operation'], item['input']))
                   for item in cases[5:7]]
        self.assertEqual(results[0]['confirmation_digest'], results[1]['confirmation_digest'])
        self.assertNotEqual(cases[5]['expected'], cases[6]['expected'])

    def test_native_oracle_export_keeps_source_answers_and_inactive_scope(self) -> None:
        spec = importlib.util.spec_from_file_location(
            'setup_native_export_test', ROOT / 'scripts/wasm/replay_harness_setup.py')
        replay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(replay)
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            artifact = parent / 'component.wasm'
            artifact.write_bytes(b'serialization fixture only; not actual Wasm')
            artifact.with_suffix('.build.json').write_bytes(b'fixture receipt')
            output = parent / 'native.json'
            # This unit checks export custody/serialization only. Real artifact
            # validation and execution are separate required CI jobs.
            with patch.object(replay, 'validated_artifact', return_value=(
                    artifact.read_bytes(), {'build_plan_sha256': 'fixture-plan'}, {}, b'{}')):
                replay.export_native_oracle(artifact, output)
                with self.assertRaises(FileExistsError):
                    replay.export_native_oracle(artifact, output)
            document = json.loads(output.read_bytes())
            self.assertEqual(document['cases'], replay.source_cases())
            self.assertEqual(len(document['source_bindings']), 16)
            self.assertEqual(len({item['path'] for item in document['source_bindings']}), 16)
            for flag in ('native_core_replay', 'full_setup_gui_tested', 'production_enabled'):
                self.assertIs(document[flag], False)

    def test_native_input_handoff_binds_bytes_and_refuses_links_or_env_injection(self) -> None:
        previous_path = sys.path[:]
        sys.path.insert(0, str(ROOT / 'tools/python313-producer'))
        try:
            spec = importlib.util.spec_from_file_location(
                'setup_native_input_test', ROOT / 'tools/python313-producer/setup_native_inputs.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with tempfile.TemporaryDirectory() as temporary:
                parent = Path(temporary).resolve()
                artifact, oracle = parent / 'component.wasm', parent / 'oracle.json'
                artifact.write_bytes(b'not executed')
                artifact.with_suffix('.build.json').write_bytes(b'fixture metadata')
                oracle.write_bytes(b'fixture oracle')
                output = module.values(artifact, oracle, parent / 'evidence')
                self.assertEqual(len(output), 7)
                self.assertNotEqual(output['TOBKIRI_TEST_SETUP_COMPONENT_SHA256'],
                                    output['TOBKIRI_TEST_SETUP_RECEIPT_SHA256'])
                with self.assertRaises(ValueError):
                    module.values(artifact, oracle, parent / 'bad\nINJECT=true')
                link = parent / 'link.json'
                link.symlink_to(oracle)
                with self.assertRaises(ValueError):
                    module.values(artifact, link, parent / 'evidence')
        finally:
            sys.path[:] = previous_path

    def test_symlink_and_existing_destination_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            target = parent / "target"
            target.mkdir()
            link = parent / "link"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(OSError):
                prepare._publish(link / "output", {"file": b"bytes"})
            with self.assertRaises(FileExistsError):
                prepare._publish(link, {"file": b"bytes"})
            self.assertEqual(list(target.iterdir()), [])


class BindingErrorTests(unittest.TestCase):
    """Generated-binding doubles cover fixed error mapping, not guest execution."""

    def test_error_codes_no_private_text_no_replay(self) -> None:
        class Err(Exception):
            pass
        class ErrorCode(Enum):
            INVALID = 1
            INTERNAL = 2
            LIMIT_EXCEEDED = 3
        @dataclass
        class Error:
            code: ErrorCode
            message: str
        world = ModuleType("wit_world")
        exports = ModuleType("wit_world.exports")
        exports.Lifecycle = exports.Setup = object
        world.exports = exports
        imports = ModuleType("wit_world.imports")
        types = ModuleType("wit_world.imports.types")
        types.Error, types.ErrorCode = Error, ErrorCode
        bindings = ModuleType("componentize_py_types")
        bindings.Err = Err
        modules = {"wit_world": world, "wit_world.exports": exports,
                   "wit_world.imports": imports, "wit_world.imports.types": types,
                   "componentize_py_types": bindings}
        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location("setup_binding_test", PACK / "python/app.py")
            app = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(app)
            cases = [(adapter.SetupInputError, ErrorCode.INVALID),
                     (adapter.SetupProjectionError, ErrorCode.INTERNAL),
                     (adapter.SetupOutputError, ErrorCode.INTERNAL),
                     (RecursionError, ErrorCode.LIMIT_EXCEEDED),
                     (MemoryError, ErrorCode.LIMIT_EXCEEDED)]
            for exception, code in cases:
                with patch.object(app, "invoke", side_effect=exception("private-token")) as invoke:
                    with self.assertRaises(Err) as raised:
                        app.Setup().decision("{}")
                    error = raised.exception.args[0]
                    self.assertEqual(error.code, code)
                    self.assertNotIn("private-token", error.message)
                    invoke.assert_called_once_with("decision", "{}")


if __name__ == "__main__":
    unittest.main()
