"""Pure archive/marker fixtures. No Cargo, Core, DATA or network acceptance."""

from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

RECOVERY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RECOVERY))
import build_deepthink_ledger as ledger

build = ledger.build
reader = ledger.reader
BASE_PORTS = [
    "local.harness-chat.llm-provider@1.0",
    "local.harness-chat.structured-llm-provider@1.0",
    "local.harness-agent.tool-provider@1.0",
    "local.harness-chat.assistant-output@1.0",
    "local.harness-chat.generate@1.0",
    "local.deepthink.workflow@1.0",
    "local.harness-chat.conversation-store@1.0",
    "local.deepthink.continuation-flow@1.0",
    "local.deepthink.continuation-input@1.0",
]


class LedgerBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archives = self.root / "archives"
        self.archives.mkdir()
        self.counter = 0

    def archive(self, identity, files, permissions=None, edit=None):
        manifest = build.base_manifest(identity.removeprefix("local."), {
            "kind": "wasm", "component": "component.wasm",
            "toolchain": "EXPLICIT-FIXTURE-NOT-ABI-VALIDATED",
            "config": {"path": "config.json"},
        }, deepcopy(permissions or build.permission_set()))
        content = {"component.wasm": b"EXPLICIT-NOT-WASM-FIXTURE",
                   "config.json": b"{}", **files}
        if edit:
            edit(manifest)
        manifest["files"] = [
            {"path": name, "size": len(raw), "sha256": build.sha256(raw)}
            for name, raw in sorted(content.items())
        ]
        build.normalize_manifest(manifest)
        content["pack.json"] = build.canonical(manifest)
        self.counter += 1
        archive_path = self.archives / f"{self.counter}-{identity}.tpk"
        self.tar(archive_path, content)
        return archive_path, manifest

    @staticmethod
    def tar(path, content):
        with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as archive:
            for name, raw in sorted(content.items()):
                entry = tarfile.TarInfo(name)
                entry.size = len(raw)
                entry.mode = 0o644
                archive.addfile(entry, io.BytesIO(raw))

    def reader_archive(self):
        return self.archive(reader.PACK_ID, {
            "wit/deps/reader.wit": (
                reader.SOURCE / "wit/deps/reader.wit"
            ).read_bytes(),
        }, reader.reader_permissions(), lambda manifest: manifest["ports"].update(
            provides=[build.port(reader.VIEW.rsplit("@", 1)[0])],
        ))

    def input_archive(self, edit=None, wit=None):
        def manifest_edit(manifest):
            manifest["ports"].update(
                provides=[build.port(view.rsplit("@", 1)[0]) for view in ledger.input_adapter.PUBLIC_VIEWS],
                consumes=ledger.input_adapter.consumed_ports(),
            )
            if edit:
                edit(manifest)
        return self.archive(ledger.input_adapter.PACK_ID, {
            "wit/deps/deepthink.wit": wit if wit is not None else (
                ledger.PACKS / "shapes/deepthink.wit"
            ).read_bytes(),
        }, ledger.input_adapter.permissions(), manifest_edit)

    def extend_profile(self, *args, **kwargs):
        if "input_archive" not in kwargs:
            kwargs["input_archive"] = self.input_archive()[0]
        return ledger.extend_profile(*args, **kwargs)

    @staticmethod
    def pin(path, manifest):
        return {
            "id": manifest["id"], "version": manifest["version"],
            "sha256": build.sha256(build.canonical(manifest)),
            "source": "file:archives/" + path.name,
        }

    def baseline(self, existing_reader=False, with_id=False):
        provider_path, provider_manifest = self.archive(ledger.PROVIDER, {
            "config.json": b'{"model":"fixture","max_tokens":0}\n',
        })
        config = {
            "environment": {"provider_pack_hash": build.sha256(build.canonical(provider_manifest)),
                            "output_token_limit": "provider_default"},
            "max_tokens": 0, "max_review_rounds": 2,
            "public_fixture_label": "日本語",
        }
        # Deliberately unlike write_json: preserving semantic equivalence is
        # insufficient because Settings identity depends on the exact bytes.
        raw_config = json.dumps(config, ensure_ascii=False, indent=3).encode() + b"\n\n"
        permissions = deepcopy(build.permission_set(ports=BASE_PORTS, private=True))
        permissions["loops"] = {
            "max_iterations": 512, "max_wall_ms": 3600000,
            "max_cost_units": 100000,
        }
        deepthink_path, deepthink_manifest = self.archive(
            ledger.DEEPTHINK, {"config.json": raw_config}, permissions,
        )
        tool_path, tool_manifest = self.archive("local.fixture-tool", {
            "config.json": b'{"immutable":true,"max_tokens":0}',
        })
        ceiling = deepcopy(build.permission_set(
            ports=BASE_PORTS, private=True, network=[reader.ORIGIN], secrets=True,
        ))
        ceiling["loops"] = deepcopy(permissions["loops"])
        ceiling["secrets"][0]["optional"] = False
        document = {
            "format": "tobkiri.published-profile/1", "name": "Frozen fixture",
            "ceiling": ceiling,
            "user_permissions": deepcopy(build.permission_set(
                ports=["local.deepthink.continuation-input@1.0"],
            )),
            "bindings": {"local.deepthink.continuation-input@1": ledger.DEEPTHINK},
            "pack_limits": {
                ledger.DEEPTHINK: deepcopy(permissions),
                ledger.PROVIDER: deepcopy(provider_manifest["permissions"]),
                "local.fixture-tool": deepcopy(tool_manifest["permissions"]),
            },
            "packs": [self.pin(provider_path, provider_manifest),
                      self.pin(deepthink_path, deepthink_manifest),
                      self.pin(tool_path, tool_manifest)],
        }
        if with_id:
            document["id"] = "prof_explicit_fixture"
        if existing_reader:
            path, manifest = self.reader_archive()
            document["packs"].append(self.pin(path, manifest))
            document["bindings"][reader.VIEW.rsplit("@", 1)[0] + "@1"] = reader.PACK_ID
            document["pack_limits"][reader.PACK_ID] = deepcopy(manifest["permissions"])
            document["pack_limits"][reader.PACK_ID]["loops"]["max_iterations"] = 1
            document["ceiling"]["ports"].append(reader.VIEW)
            document["user_permissions"]["ports"].append(reader.VIEW)
        path = self.root / "baseline.json"
        build.write_json(path, document)
        return path, document, raw_config, deepthink_manifest

    def ledger_archive(self, raw_config, old_manifest, edit=None):
        def manifest_edit(manifest):
            manifest["ports"]["provides"] = [build.port("local.deepthink.workflow"),
                                              build.port("local.deepthink.input-context"),
                                              build.port("local.deepthink.tool-receipt-reader"),
                                              build.port("local.deepthink.goal-review-worker")]
            manifest["ports"]["consumes"] = [
                build.port(view.rsplit("@", 1)[0]) for view in sorted(ledger.ADDED_PORTS)
            ]
            manifest["flows"] = [f"flows/{name}.flow.json"
                                 for name in ("continuation", "regeneration", "control", "tool-diagnostics", "goal-review")]
            manifest["shapes"] = [
                {"name": ledger.DEEPTHINK + "." + name, "version": "1.0",
                 "kind": kind, "wit": "wit/deps/deepthink.wit"}
                for name, kind in (("workflow", "port"), ("records", "data"),
                                   ("continuation-input", "port"), ("continuation-flow", "flow"),
                                   ("regeneration-flow", "flow"), ("control-flow", "flow"),
                                   ("input-context", "port"),
                                   *((name, "port") for name in ledger.build_deepthink.NEW_PORT_SHAPES),
                                   *((name, "flow") for name in ledger.build_deepthink.NEW_FLOW_SHAPES))
            ]
            if edit:
                edit(manifest)
        return self.archive(ledger.DEEPTHINK, {
            "config.json": raw_config,
            "flows/regeneration.flow.json": build.canonical({"implements": ledger.REGENERATION}),
            "flows/continuation.flow.json": build.canonical({
                "implements": "local.deepthink.continuation-flow@1.0"}),
            "flows/control.flow.json": build.canonical({"implements": ledger.CONTROL}),
            "flows/tool-diagnostics.flow.json": build.canonical({"implements": "local.deepthink.tool-diagnostics-flow@1.0"}),
            "flows/goal-review.flow.json": build.canonical({"implements": "local.deepthink.goal-review-flow@1.0"}),
            "wit/deps/deepthink.wit": (ledger.PACKS / "shapes/deepthink.wit").read_bytes(),
            "wit/deps/generation-reader.wit": (
                reader.SOURCE / "wit/deps/reader.wit"
            ).read_bytes(),
        }, ledger._ledger_permissions(old_manifest["permissions"]), manifest_edit)

    def ready(self, **kwargs):
        baseline, original, config, old = self.baseline(**kwargs)
        new, _ = self.ledger_archive(config, old)
        reader_path, _ = self.reader_archive()
        return baseline, original, config, old, new, reader_path

    def test_profile_reuses_other_archives_exactly_and_preserves_absent_identity(self):
        baseline, original, config, _, new, reader_path = self.ready()
        baseline_bytes = baseline.read_bytes()
        before = {pin["id"]: (self.root / pin["source"][5:]).read_bytes()
                  for pin in original["packs"]}
        output = self.extend_profile(baseline, new, reader_path, self.root / "ready")
        profile = json.loads(output.read_bytes())
        self.assertNotIn("id", profile)
        self.assertEqual(profile["name"], original["name"])
        self.assertEqual(baseline.read_bytes(), baseline_bytes)
        self.assertEqual(len(profile["packs"]), len(original["packs"]) + 2)
        by_id = {pin["id"]: pin for pin in profile["packs"]}
        for pin in original["packs"]:
            if pin["id"] == ledger.DEEPTHINK:
                continue
            copied = by_id[pin["id"]]
            self.assertEqual({key: copied[key] for key in ("id", "version", "sha256")},
                             {key: pin[key] for key in ("id", "version", "sha256")})
            self.assertEqual((output.parent / copied["source"][5:]).read_bytes(), before[pin["id"]])
            self.assertEqual(profile["pack_limits"][pin["id"]], original["pack_limits"][pin["id"]])
        for policy in ("ceiling", "user_permissions"):
            self.assertEqual({key: value for key, value in profile[policy].items() if key != "ports"},
                             {key: value for key, value in original[policy].items() if key != "ports"})
        self.assertEqual(profile["pack_limits"][ledger.DEEPTHINK]["loops"],
                         original["pack_limits"][ledger.DEEPTHINK]["loops"])
        self.assertEqual(profile["pack_limits"][ledger.DEEPTHINK]["wasm"],
                         original["pack_limits"][ledger.DEEPTHINK]["wasm"])
        self.assertEqual(set(profile["ceiling"]["ports"]),
                         set(original["ceiling"]["ports"]) | ledger.ADDED_PORTS)
        receipt = json.loads((output.parent / "PREPARATION.json").read_bytes())
        self.assertEqual(receipt["exact_deepthink_config_sha256"], build.sha256(config))
        self.assertTrue(receipt["identity_assigned_by_core_command"])
        for key in ("core_admission_performed", "runtime_acceptance_performed", "profile_applied",
                    "checkpoint_changes", "other_packs_rebuilt"):
            self.assertFalse(receipt[key])
        self.assertEqual(receipt["model_calls"], 0)
        self.assertEqual(receipt["lookup_calls"], 0)
        self.assertEqual(receipt["selected_pack_count"], len(profile["packs"]))
        self.assertEqual(len(receipt["copied_packs"]), len(profile["packs"]))
        self.assertEqual(profile["bindings"]["local.deepthink.continuation-input@1"],
                         ledger.input_adapter.PACK_ID)
        self.assertEqual(profile["bindings"]["local.deepthink.input-context@1"], ledger.DEEPTHINK)
        self.assertEqual(set(profile["user_permissions"]["ports"]),
                         set(original["user_permissions"]["ports"]) | {reader.VIEW} | ledger.NEW_PUBLIC_VIEWS)
        permissions = profile["pack_limits"][ledger.input_adapter.PACK_ID]
        self.assertFalse(any(permissions["storage"].values()))
        self.assertEqual(permissions["secrets"], [])
        self.assertEqual(permissions["network"]["origins"], [])
        self.assertEqual(set(permissions["ports"]), {
            *ledger.input_adapter.PUBLIC_VIEWS, *ledger.input_adapter.FLOW_VIEWS,
        })

    def test_existing_reader_is_retained_once_with_restrictions_and_id_preserved(self):
        baseline, original, _, _, new, reader_path = self.ready(existing_reader=True, with_id=True)
        output = self.extend_profile(baseline, new, reader_path, self.root / "replace")
        profile = json.loads(output.read_bytes())
        self.assertEqual(profile["id"], original["id"])
        self.assertEqual(len(profile["packs"]), len(original["packs"]) + 1)
        self.assertEqual(sum(pin["id"] == reader.PACK_ID for pin in profile["packs"]), 1)
        self.assertEqual(profile["pack_limits"][reader.PACK_ID]["loops"]["max_iterations"], 1)
        receipt = json.loads((output.parent / "PREPARATION.json").read_bytes())
        self.assertFalse(receipt["old_reader_replaced"])
        self.assertTrue(receipt["existing_reader_bytes_retained"])

    def test_existing_input_nonport_limits_are_kept_while_new_views_are_explicit(self):
        baseline, original, _, _, new, reader_path = self.ready()
        old_adapter, old_manifest = self.input_archive()
        original["packs"].append(self.pin(old_adapter, old_manifest))
        old_limits = deepcopy(old_manifest["permissions"])
        old_limits["ports"] = [ledger.input_adapter.INPUT_VIEW,
            "local.deepthink.control-flow@1.0", "local.deepthink.continuation-flow@1.0",
            "local.deepthink.regeneration-flow@1.0"]
        old_limits["loops"]["max_iterations"] = 1
        original["pack_limits"][ledger.input_adapter.PACK_ID] = old_limits
        original["bindings"]["local.deepthink.continuation-input@1"] = ledger.input_adapter.PACK_ID
        build.write_json(baseline, original)
        output = self.extend_profile(baseline, new, reader_path, self.root / "old-adapter")
        profile = json.loads(output.read_bytes())
        current = profile["pack_limits"][ledger.input_adapter.PACK_ID]
        nonport_current = {key: deepcopy(value) for key, value in current.items() if key != "ports"}
        nonport_original = {key: deepcopy(value) for key, value in old_limits.items() if key != "ports"}
        # Manifest normalization writes the optional null; permission intersection
        # omits it. Compare all non-port fields after that one canonical default.
        for permissions in (nonport_current, nonport_original):
            permissions["network"].setdefault("user_origins", None)
        self.assertEqual(nonport_current, nonport_original)
        self.assertEqual(set(current["ports"]), set(ledger.input_adapter.PUBLIC_VIEWS) | set(ledger.input_adapter.FLOW_VIEWS))
        for view in ledger.NEW_PUBLIC_VIEWS:
            self.assertIn(view, profile["user_permissions"]["ports"])
        for view in ("local.deepthink.tool-receipt-reader@1.0", "local.deepthink.goal-review-worker@1.0"):
            self.assertNotIn(view, profile["user_permissions"]["ports"])
            self.assertNotIn(view, current["ports"])

    def test_new_shape_binding_cannot_replace_an_unrelated_provider(self):
        for view in ("local.deepthink.tool-diagnostics@1", "local.deepthink.goal-review-worker@1"):
            baseline, original, _, _, new, reader_path = self.ready()
            original["bindings"][view] = "local.unrelated"
            build.write_json(baseline, original)
            output = self.root / ("bad-new-binding-" + str(self.counter))
            with self.assertRaisesRegex(ValueError, "binding belongs"):
                self.extend_profile(baseline, new, reader_path, output)
            self.assertFalse(output.exists())

    def test_config_semantically_equal_but_different_bytes_is_rejected(self):
        baseline, _, config, old, _, reader_path = self.ready()
        changed, _ = self.ledger_archive(build.canonical(json.loads(config)), old)
        with self.assertRaisesRegex(ValueError, "configuration bytes"):
            self.extend_profile(baseline, changed, reader_path, self.root / "bad-config")
        self.assertFalse((self.root / "bad-config").exists())

    def test_numeric_limits_and_nonport_authority_cannot_change(self):
        baseline, _, config, old, _, reader_path = self.ready()
        for mutation in ("loops", "network", "storage"):
            with self.subTest(mutation=mutation):
                def edit(manifest):
                    if mutation == "loops":
                        manifest["permissions"]["loops"]["max_iterations"] = 16
                    elif mutation == "network":
                        manifest["permissions"]["network"]["origins"] = [reader.ORIGIN]
                    else:
                        manifest["permissions"]["storage"]["public_write"] = True
                changed, _ = self.ledger_archive(config, old, edit)
                with self.assertRaisesRegex(ValueError, "authority or numeric"):
                    self.extend_profile(baseline, changed, reader_path, self.root / mutation)
                self.assertFalse((self.root / mutation).exists())

    def test_ceiling_is_intersected_without_enlarging_old_pack_limits(self):
        baseline, original, _, _, new, reader_path = self.ready()
        original["ceiling"]["wasm"]["output_bytes"] = 1024
        original["pack_limits"][ledger.DEEPTHINK]["loops"]["max_iterations"] = 100
        build.write_json(baseline, original)
        output = self.extend_profile(baseline, new, reader_path, self.root / "intersection")
        result = json.loads(output.read_bytes())
        self.assertEqual(result["pack_limits"][ledger.DEEPTHINK]["wasm"]["output_bytes"], 1024)
        self.assertEqual(result["pack_limits"][reader.PACK_ID]["wasm"]["output_bytes"], 1024)
        self.assertEqual(result["pack_limits"][ledger.DEEPTHINK]["loops"]["max_iterations"], 100)

    def test_invalid_pin_duplicate_and_nonfile_sources_reject_before_output(self):
        for mutation in ("hash", "duplicate", "https", "directory", "identity"):
            with self.subTest(mutation=mutation):
                baseline, profile, _, _, new, reader_path = self.ready()
                if mutation == "hash":
                    profile["packs"][0]["sha256"] = "sha256:" + "0" * 64
                elif mutation == "duplicate":
                    profile["packs"].append(deepcopy(profile["packs"][0]))
                elif mutation == "https":
                    profile["packs"][0]["source"] = "https://invalid.example/pack.tpk"
                elif mutation == "directory":
                    profile["packs"][0]["source"] = "file:archives"
                else:
                    profile["packs"][0]["id"] = "../escape"
                build.write_json(baseline, profile)
                destination = self.root / ("invalid-" + mutation)
                with self.assertRaises(ValueError):
                    self.extend_profile(baseline, new, reader_path, destination)
                self.assertFalse(destination.exists())

    def test_provider_pin_capture_mismatch_rejects(self):
        baseline, profile, config, _, new, reader_path = self.ready()
        changed = json.loads(config)
        changed["environment"]["provider_pack_hash"] = "sha256:" + "0" * 64
        path, manifest = self.archive(ledger.DEEPTHINK, {"config.json": build.canonical(changed)})
        profile["packs"][1] = self.pin(path, manifest)
        build.write_json(baseline, profile)
        with self.assertRaisesRegex(ValueError, "captured provider"):
            self.extend_profile(baseline, new, reader_path, self.root / "bad-provider")

    def test_reader_authority_binding_and_wit_mismatch_reject(self):
        baseline, profile, config, old, new, reader_path = self.ready()
        profile["bindings"][reader.VIEW.rsplit("@", 1)[0] + "@1"] = "local.someone-else"
        build.write_json(baseline, profile)
        with self.assertRaisesRegex(ValueError, "binding"):
            self.extend_profile(baseline, new, reader_path, self.root / "binding")
        del profile["bindings"][reader.VIEW.rsplit("@", 1)[0] + "@1"]
        build.write_json(baseline, profile)
        permissions = reader.reader_permissions()
        permissions["secrets"][0]["reveal"] = True
        broad, _ = self.archive(reader.PACK_ID, {}, permissions)
        with self.assertRaisesRegex(ValueError, "reader authority"):
            self.extend_profile(baseline, new, broad, self.root / "broad")
        changed_reader, _ = self.archive(reader.PACK_ID, {
            "wit/deps/reader.wit": b"different-public-shape",
        }, reader.reader_permissions(), lambda manifest: manifest["ports"].update(
            provides=[build.port(reader.VIEW.rsplit("@", 1)[0])],
        ))
        with self.assertRaisesRegex(ValueError, "dependency WIT"):
            self.extend_profile(baseline, new, changed_reader, self.root / "wit")

    def test_adapter_cannot_gain_effects_redeclare_owner_or_change_dependencies(self):
        baseline, _, _, _, new, reader_path = self.ready()
        for mutation in ("storage", "network", "shape", "consumes", "missing-type", "wit"):
            with self.subTest(mutation=mutation):
                def edit(manifest):
                    if mutation == "storage":
                        manifest["permissions"]["storage"]["private"] = True
                    elif mutation == "network":
                        manifest["permissions"]["network"]["origins"] = [reader.ORIGIN]
                    elif mutation == "shape":
                        manifest["shapes"] = [{"name": "local.deepthink.continuation-input"}]
                    elif mutation == "consumes":
                        manifest["ports"]["consumes"].append(build.port("local.deepthink.input-context"))
                    elif mutation == "missing-type":
                        manifest["ports"]["consumes"] = [port for port in manifest["ports"]["consumes"]
                            if port["shape"] != "local.deepthink.workflow"]
                adapter, _ = self.input_archive(edit, wit=(b"different" if mutation == "wit" else None))
                destination = self.root / ("adapter-" + mutation)
                with self.assertRaisesRegex(ValueError, "input adapter"):
                    self.extend_profile(baseline, new, reader_path, destination,
                                        input_archive=adapter)
                self.assertFalse(destination.exists())

    def test_existing_reader_bytes_and_unrelated_input_binding_cannot_change(self):
        baseline, profile, _, _, new, reader_path = self.ready(existing_reader=True)
        different, _ = self.archive(reader.PACK_ID, {
            "config.json": b'{"different":true}',
            "wit/deps/reader.wit": (reader.SOURCE / "wit/deps/reader.wit").read_bytes(),
        }, reader.reader_permissions(), lambda manifest: manifest["ports"].update(
            provides=[build.port(reader.VIEW.rsplit("@", 1)[0])],
        ))
        with self.assertRaisesRegex(ValueError, "existing reader archive bytes"):
            self.extend_profile(baseline, new, different, self.root / "changed-reader")
        profile["bindings"]["local.deepthink.continuation-input@1"] = "local.someone-else"
        build.write_json(baseline, profile)
        with self.assertRaisesRegex(ValueError, "input binding"):
            self.extend_profile(baseline, new, reader_path, self.root / "changed-input")

    def test_existing_output_is_never_overwritten(self):
        baseline, _, _, _, new, reader_path = self.ready()
        destination = self.root / "existing"
        destination.mkdir()
        sentinel = destination / "sentinel"
        sentinel.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            self.extend_profile(baseline, new, reader_path, destination)
        with self.assertRaises(FileExistsError):
            ledger.stage_from_frozen(self.root / "no-target", baseline, destination)
        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertEqual(list(destination.iterdir()), [sentinel])

    def test_input_change_during_copy_never_publishes_profile(self):
        baseline, _, _, _, new, reader_path = self.ready()
        original_copy = ledger._copy
        calls = 0
        def mutate_after_copy(*args):
            nonlocal calls
            result = original_copy(*args)
            calls += 1
            if calls == 1:
                baseline.write_bytes(baseline.read_bytes() + b"\n")
            return result
        destination = self.root / "changed-during-copy"
        with patch.object(ledger, "_copy", mutate_after_copy):
            with self.assertRaisesRegex(ValueError, "baseline changed"):
                self.extend_profile(baseline, new, reader_path, destination)
        self.assertFalse((destination / "profile.json").exists())
        self.assertFalse((destination / "PREPARATION.json").exists())

    def test_archive_unsafe_member_and_duplicate_json_reject(self):
        baseline, _, _, _, new, reader_path = self.ready()
        for name in ("../escape", "./component.wasm", "component\\wasm"):
            with self.subTest(name=name):
                invalid = self.archives / "invalid.tpk"
                self.tar(invalid, {name: b"x", "pack.json": b"{}"})
                with self.assertRaises(ValueError):
                    self.extend_profile(baseline, invalid, reader_path, self.root / "unsafe")
        baseline.write_bytes(b'{"packs":[],"packs":[]}')
        with self.assertRaisesRegex(ValueError, "duplicate JSON"):
            self.extend_profile(baseline, new, reader_path, self.root / "duplicate-json")

    def artifact(self, profile="release"):
        path = self.root / f"target/wasm32-wasip2/{profile}/tobkiri_pack_deepthink.wasm"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(ledger.COMPONENT_HEADER + b"EXPLICIT-NOT-ABI-VALIDATED")
        return path

    def test_stage_exact_bytes_zero_tokens_limits_and_new_flow_bindings(self):
        baseline, _, config, old = self.baseline()
        artifact = self.artifact()
        destination = self.root / "stage"
        result = ledger.stage_from_frozen(self.root / "target", baseline, destination)
        self.assertEqual((destination / "config.json").read_bytes(), config)
        self.assertEqual(json.loads(config)["max_tokens"], 0)
        self.assertEqual(result["permissions"]["loops"], old["permissions"]["loops"])
        self.assertEqual(result["permissions"]["wasm"], old["permissions"]["wasm"])
        self.assertEqual(set(result["permissions"]["ports"]), set(BASE_PORTS) | ledger.ADDED_PORTS)
        self.assertEqual((destination / "component.wasm").read_bytes(), artifact.read_bytes())
        bound = {item["path"]: item for item in result["files"]}
        for relative in ("config.json", "component.build.json", "flows/regeneration.flow.json",
                         "flows/control.flow.json", "wit/control-flow.shape.json",
                         "wit/input-context.shape.json",
                         "wit/deps/generation-reader.wit", "wit/deps/core-v1-1.wit"):
            self.assertEqual(bound[relative]["sha256"], reader.file_sha(destination / relative))
        self.assertEqual(json.loads((destination / "wit/workflow.shape.json").read_bytes())["streams"]["regenerate-phase"], "chat-event")
        provenance = json.loads((destination / "component.build.json").read_bytes())
        self.assertEqual(provenance["build_profile"], "release")
        self.assertFalse(provenance["incremental_frozen_baseline"]["other_packs_rebuilt"])
        self.assertEqual(provenance["incremental_frozen_baseline"]["exact_config_sha256"], build.sha256(config))
        self.assertEqual(provenance["sources"]["packs/recovery/build_deepthink_ledger.py"], reader.file_sha(Path(ledger.__file__)))
        self.assertEqual(result["ports"]["provides"], [
            build.port("local.deepthink.workflow"), build.port("local.deepthink.input-context"),
            build.port("local.deepthink.tool-receipt-reader"), build.port("local.deepthink.goal-review-worker"),
        ])

    def test_stage_has_no_debug_fallback_or_invalid_component_acceptance(self):
        baseline, _, _, _ = self.baseline()
        self.artifact("debug")
        with self.assertRaises(FileNotFoundError):
            ledger.stage_from_frozen(self.root / "target", baseline, self.root / "missing")
        self.assertFalse((self.root / "missing").exists())
        self.artifact().write_bytes(b"\0asm\1\0\0\0CORE-MODULE-NOT-COMPONENT")
        with self.assertRaisesRegex(ValueError, "component header"):
            ledger.stage_from_frozen(self.root / "target", baseline, self.root / "invalid-component")
        self.assertFalse((self.root / "invalid-component").exists())

    def test_stage_detects_artifact_change_after_copy(self):
        baseline, _, _, _ = self.baseline()
        artifact = self.artifact()
        original_stage = ledger.build_deepthink.stage
        def changed_stage(*args, **kwargs):
            result = original_stage(*args, **kwargs)
            artifact.write_bytes(artifact.read_bytes() + b"changed")
            return result
        with patch.object(ledger.build_deepthink, "stage", changed_stage):
            with self.assertRaisesRegex(ValueError, "stage input changed"):
                ledger.stage_from_frozen(self.root / "target", baseline, self.root / "raced-stage")


if __name__ == "__main__":
    unittest.main()
