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
            manifest["ports"]["consumes"] = [
                build.port(view.rsplit("@", 1)[0]) for view in sorted(ledger.ADDED_PORTS)
            ]
            manifest["flows"] = ["flows/regeneration.flow.json"]
            if edit:
                edit(manifest)
        return self.archive(ledger.DEEPTHINK, {
            "config.json": raw_config,
            "flows/regeneration.flow.json": build.canonical({"implements": ledger.REGENERATION}),
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
        output = ledger.extend_profile(baseline, new, reader_path, self.root / "ready")
        profile = json.loads(output.read_bytes())
        self.assertNotIn("id", profile)
        self.assertEqual(profile["name"], original["name"])
        self.assertEqual(baseline.read_bytes(), baseline_bytes)
        self.assertEqual(len(profile["packs"]), len(original["packs"]) + 1)
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

    def test_existing_reader_is_replaced_once_with_restrictions_and_id_preserved(self):
        baseline, original, _, _, new, reader_path = self.ready(existing_reader=True, with_id=True)
        output = ledger.extend_profile(baseline, new, reader_path, self.root / "replace")
        profile = json.loads(output.read_bytes())
        self.assertEqual(profile["id"], original["id"])
        self.assertEqual(len(profile["packs"]), len(original["packs"]))
        self.assertEqual(sum(pin["id"] == reader.PACK_ID for pin in profile["packs"]), 1)
        self.assertEqual(profile["pack_limits"][reader.PACK_ID]["loops"]["max_iterations"], 1)
        self.assertTrue(json.loads((output.parent / "PREPARATION.json").read_bytes())["old_reader_replaced"])

    def test_config_semantically_equal_but_different_bytes_is_rejected(self):
        baseline, _, config, old, _, reader_path = self.ready()
        changed, _ = self.ledger_archive(build.canonical(json.loads(config)), old)
        with self.assertRaisesRegex(ValueError, "configuration bytes"):
            ledger.extend_profile(baseline, changed, reader_path, self.root / "bad-config")
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
                    ledger.extend_profile(baseline, changed, reader_path, self.root / mutation)
                self.assertFalse((self.root / mutation).exists())

    def test_ceiling_is_intersected_without_enlarging_old_pack_limits(self):
        baseline, original, _, _, new, reader_path = self.ready()
        original["ceiling"]["wasm"]["output_bytes"] = 1024
        original["pack_limits"][ledger.DEEPTHINK]["loops"]["max_iterations"] = 100
        build.write_json(baseline, original)
        output = ledger.extend_profile(baseline, new, reader_path, self.root / "intersection")
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
                    ledger.extend_profile(baseline, new, reader_path, destination)
                self.assertFalse(destination.exists())

    def test_provider_pin_capture_mismatch_rejects(self):
        baseline, profile, config, _, new, reader_path = self.ready()
        changed = json.loads(config)
        changed["environment"]["provider_pack_hash"] = "sha256:" + "0" * 64
        path, manifest = self.archive(ledger.DEEPTHINK, {"config.json": build.canonical(changed)})
        profile["packs"][1] = self.pin(path, manifest)
        build.write_json(baseline, profile)
        with self.assertRaisesRegex(ValueError, "captured provider"):
            ledger.extend_profile(baseline, new, reader_path, self.root / "bad-provider")

    def test_reader_authority_binding_and_wit_mismatch_reject(self):
        baseline, profile, config, old, new, reader_path = self.ready()
        profile["bindings"][reader.VIEW.rsplit("@", 1)[0] + "@1"] = "local.someone-else"
        build.write_json(baseline, profile)
        with self.assertRaisesRegex(ValueError, "binding"):
            ledger.extend_profile(baseline, new, reader_path, self.root / "binding")
        del profile["bindings"][reader.VIEW.rsplit("@", 1)[0] + "@1"]
        build.write_json(baseline, profile)
        permissions = reader.reader_permissions()
        permissions["secrets"][0]["reveal"] = True
        broad, _ = self.archive(reader.PACK_ID, {}, permissions)
        with self.assertRaisesRegex(ValueError, "reader authority"):
            ledger.extend_profile(baseline, new, broad, self.root / "broad")
        changed_reader, _ = self.archive(reader.PACK_ID, {
            "wit/deps/reader.wit": b"different-public-shape",
        }, reader.reader_permissions(), lambda manifest: manifest["ports"].update(
            provides=[build.port(reader.VIEW.rsplit("@", 1)[0])],
        ))
        with self.assertRaisesRegex(ValueError, "dependency WIT"):
            ledger.extend_profile(baseline, new, changed_reader, self.root / "wit")

    def test_existing_output_is_never_overwritten(self):
        baseline, _, _, _, new, reader_path = self.ready()
        destination = self.root / "existing"
        destination.mkdir()
        sentinel = destination / "sentinel"
        sentinel.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            ledger.extend_profile(baseline, new, reader_path, destination)
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
                ledger.extend_profile(baseline, new, reader_path, destination)
        self.assertFalse((destination / "profile.json").exists())
        self.assertFalse((destination / "PREPARATION.json").exists())

    def test_archive_unsafe_member_and_duplicate_json_reject(self):
        baseline, _, _, _, new, reader_path = self.ready()
        for name in ("../escape", "./component.wasm", "component\\wasm"):
            with self.subTest(name=name):
                invalid = self.archives / "invalid.tpk"
                self.tar(invalid, {name: b"x", "pack.json": b"{}"})
                with self.assertRaises(ValueError):
                    ledger.extend_profile(baseline, invalid, reader_path, self.root / "unsafe")
        baseline.write_bytes(b'{"packs":[],"packs":[]}')
        with self.assertRaisesRegex(ValueError, "duplicate JSON"):
            ledger.extend_profile(baseline, new, reader_path, self.root / "duplicate-json")

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
                         "wit/deps/generation-reader.wit", "wit/deps/core-v1-1.wit"):
            self.assertEqual(bound[relative]["sha256"], reader.file_sha(destination / relative))
        self.assertEqual(json.loads((destination / "wit/workflow.shape.json").read_bytes())["streams"]["regenerate-phase"], "chat-event")
        provenance = json.loads((destination / "component.build.json").read_bytes())
        self.assertEqual(provenance["build_profile"], "release")
        self.assertFalse(provenance["incremental_frozen_baseline"]["other_packs_rebuilt"])
        self.assertEqual(provenance["incremental_frozen_baseline"]["exact_config_sha256"], build.sha256(config))
        self.assertEqual(provenance["sources"]["packs/recovery/build_deepthink_ledger.py"], reader.file_sha(Path(ledger.__file__)))

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
