"""Local marker-byte/archive tests, not component ABI or Core acceptance."""

from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

RECOVERY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RECOVERY))
import build_generation_reader as reader


class ReaderBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archives = self.root / "old"
        self.archives.mkdir()

    def archive(self, identity, config=None, permissions=None, reader_pack=False):
        files = {"config.json": json.dumps(config or {}).encode()}
        manifest = reader.build.base_manifest(identity.removeprefix("local."),
            {"kind": "wasm", "component": "component.wasm", "toolchain": "fixture"},
            permissions or reader.build.permission_set())
        if reader_pack:
            manifest["ports"]["provides"] = [reader.build.port(reader.VIEW.rsplit("@", 1)[0])]
        files["component.wasm"] = b"EXPLICIT-NOT-WASM-FIXTURE"
        manifest["files"] = [{"path": key, "size": len(value), "sha256": reader.build.sha256(value)}
                             for key, value in sorted(files.items())]
        reader.build.normalize_manifest(manifest)
        digest = reader.build.sha256(reader.build.canonical(manifest))
        files["pack.json"] = reader.build.canonical(manifest)
        path = self.archives / (identity + ".tpk")
        with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as archive:
            for key, content in sorted(files.items()):
                info = tarfile.TarInfo(key)
                info.size = len(content)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(content))
        return path, digest

    def baseline(self):
        provider, provider_hash = self.archive("local.openrouter-mimo-provider")
        deepthink, deepthink_hash = self.archive("local.deepthink", {
            "environment": {"provider_pack_hash": provider_hash}, "unchanged_setting": 2,
        })
        current_reader, _ = self.archive(reader.PACK_ID, permissions=reader.reader_permissions(), reader_pack=True)
        ceiling = reader.build.permission_set(ports=["local.deepthink.workflow@1.0"], network=[reader.ORIGIN], secrets=True)
        document = {"id": "prof_fixture", "name": "Frozen baseline", "bindings": {},
                    "ceiling": ceiling, "user_permissions": reader.build.permission_set(),
                    "pack_limits": {}, "packs": [
                        {"id": "local.openrouter-mimo-provider", "version": "0.1.0", "sha256": provider_hash, "source": "file:old/" + provider.name},
                        {"id": "local.deepthink", "version": "0.1.0", "sha256": deepthink_hash, "source": "file:old/" + deepthink.name},
                    ]}
        baseline = self.root / "baseline.json"
        reader.build.write_json(baseline, document)
        return baseline, current_reader, document

    def test_profile_reuses_all_old_bytes_config_and_identity(self):
        baseline, package, original = self.baseline()
        originals = {path.name: path.read_bytes() for path in self.archives.glob("*.tpk")}
        output = reader.extend_profile(baseline, package, self.root / "ready")
        updated = json.loads(output.read_text())
        self.assertEqual(updated["id"], original["id"])
        self.assertEqual(updated["name"], original["name"])
        self.assertEqual(updated["ceiling"]["network"], original["ceiling"]["network"])
        self.assertEqual(updated["user_permissions"]["network"]["origins"], [])
        self.assertEqual(updated["user_permissions"]["secrets"], [])
        self.assertEqual(updated["ceiling"]["wasm"], original["ceiling"]["wasm"])
        self.assertIn(reader.VIEW, updated["user_permissions"]["ports"])
        for old, new in zip(original["packs"], updated["packs"]):
            for key in ("id", "version", "sha256"):
                self.assertEqual(new[key], old[key])
            copied = output.parent / new["source"][5:]
            self.assertEqual(copied.read_bytes(), originals[old["id"] + ".tpk"])
        receipt = json.loads((output.parent / "PREPARATION.json").read_text())
        self.assertEqual(receipt["declared_profile_id"], original["id"])
        self.assertFalse(receipt["identity_assigned_by_core_command"])
        self.assertFalse(receipt["core_admission_performed"])
        self.assertFalse(receipt["reconciliation_implemented"])
        self.assertTrue(receipt["deepthink_config_sha256"].startswith("sha256:"))

    def test_id_absent_definition_stays_id_absent_for_normal_core_apply(self):
        baseline, package, original = self.baseline()
        del original["id"]
        reader.build.write_json(baseline, original)
        before = baseline.read_bytes()
        output = reader.extend_profile(baseline, package, self.root / "idless")
        updated = json.loads(output.read_text())
        self.assertNotIn("id", updated)
        self.assertEqual(updated["name"], original["name"])
        self.assertEqual(baseline.read_bytes(), before)
        for old, new in zip(original["packs"], updated["packs"]):
            for key in ("id", "version", "sha256"):
                self.assertEqual(new[key], old[key])
        receipt = json.loads((output.parent / "PREPARATION.json").read_text())
        self.assertEqual(receipt["format"], "tobkiri.generation-reader-profile-preparation/2")
        self.assertIsNone(receipt["declared_profile_id"])
        self.assertTrue(receipt["identity_assigned_by_core_command"])
        self.assertFalse(receipt["core_admission_performed"])
        self.assertFalse(receipt["checkpoint_changes"])

    def test_mutated_or_directory_pin_rejects_before_output(self):
        for mutation in ("hash", "directory"):
            with self.subTest(mutation=mutation):
                baseline, package, document = self.baseline()
                if mutation == "hash":
                    document["packs"][0]["sha256"] = "sha256:" + "0" * 64
                else:
                    document["packs"][0]["source"] = "file:old"
                reader.build.write_json(baseline, document)
                destination = self.root / ("denied-" + mutation)
                with self.assertRaises(ValueError):
                    reader.extend_profile(baseline, package, destination)
                self.assertFalse(destination.exists())

    def test_changed_captured_provider_and_absent_authority_reject(self):
        baseline, package, document = self.baseline()
        _, changed = self.archive("local.deepthink", {"environment": {"provider_pack_hash": "sha256:" + "1" * 64}})
        document["packs"][1]["sha256"] = changed
        reader.build.write_json(baseline, document)
        with self.assertRaisesRegex(ValueError, "captured provider"):
            reader.extend_profile(baseline, package, self.root / "changed")
        baseline, package, document = self.baseline()
        document["ceiling"]["network"]["origins"] = []
        reader.build.write_json(baseline, document)
        with self.assertRaisesRegex(ValueError, "ceiling"):
            reader.extend_profile(baseline, package, self.root / "authority")

    def test_reader_extra_authority_rejects_without_broadening(self):
        baseline, _, _ = self.baseline()
        permissions = reader.reader_permissions()
        permissions["secrets"][0]["reveal"] = True
        package, _ = self.archive(reader.PACK_ID, permissions=permissions, reader_pack=True)
        with self.assertRaisesRegex(ValueError, "authority differs"):
            reader.extend_profile(baseline, package, self.root / "broad")
        self.assertFalse((self.root / "broad").exists())

    def test_existing_output_is_not_overwritten(self):
        baseline, package, _ = self.baseline()
        destination = self.root / "existing"
        destination.mkdir()
        marker = destination / "sentinel"
        marker.write_bytes(b"preserve")
        with self.assertRaises(FileExistsError):
            reader.extend_profile(baseline, package, destination)
        self.assertEqual(marker.read_bytes(), b"preserve")
        self.assertFalse((destination / "profile.json").exists())

    def test_stage_uses_explicit_release_and_never_debug_fallback(self):
        target = self.root / "target"
        debug = target / "wasm32-wasip2/debug/tobkiri_pack_openrouter_generation_reader.wasm"
        debug.parent.mkdir(parents=True)
        debug.write_bytes(b"\0asm\r\0\1\0NOT-ABI-VALIDATED")
        with self.assertRaises(FileNotFoundError):
            reader.stage(target, self.root / "stage")
        self.assertFalse((self.root / "stage").exists())

    def test_stage_binds_separate_guest_world_and_qualified_shape_dependency(self):
        # Marker component bytes only: this exercises artifact layout, not ABI
        # admission. Root must separately run the real manifest/Wasm resolver.
        packs = self.root / "packs"
        source = packs / "recovery/local.openrouter-generation-reader"
        for relative in ["Cargo.toml", "build.rs", "wit/pack.wit",
                         "wit/deps/reader.wit", "src/lib.rs", "src/reader.rs"]:
            destination = source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(reader.SOURCE / relative, destination)
        (source / "Cargo.lock").write_text("# Explicit marker fixture only\n")
        shutil.copyfile(reader.PACKS / "build.py", packs / "build.py")
        helper = packs / "recovery/build_generation_reader.py"
        shutil.copyfile(reader.__file__, helper)
        core = self.root / "crates/tobkiri-wit/wit/core/core.wit"
        core.parent.mkdir(parents=True)
        shutil.copyfile(reader.REPO / "crates/tobkiri-wit/wit/core/core.wit", core)
        target = self.root / "target"
        artifact = target / "wasm32-wasip2/release/tobkiri_pack_openrouter_generation_reader.wasm"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"\0asm\r\0\1\0EXPLICIT-NOT-ABI-VALIDATED")
        output = self.root / "qualified-stage"
        with patch.object(reader, "SOURCE", source), \
                patch.object(reader, "REPO", self.root), \
                patch.object(reader, "PACKS", packs), \
                patch.object(reader, "__file__", str(helper)):
            manifest = reader.stage(target, output)
        world = (output / "wit/pack.wit").read_text()
        shape = (output / "wit/deps/reader.wit").read_text()
        self.assertIn("package local:openrouter-generation-reader-guest@1.0.0;", world)
        self.assertIn("export local:openrouter-generation-reader/generation-reader@1.0.0;", world)
        self.assertNotIn("export generation-reader;", world)
        self.assertIn("package local:openrouter-generation-reader@1.0.0;", shape)
        self.assertFalse((output / "wit/reader.wit").exists())
        self.assertEqual(manifest["shapes"][0]["wit"], "wit/deps/reader.wit")
        bound = {item["path"]: item for item in manifest["files"]}
        for relative in ["wit/pack.wit", "wit/deps/reader.wit", "wit/deps/core.wit"]:
            self.assertEqual(bound[relative]["sha256"], reader.file_sha(output / relative))
        provenance = json.loads((output / "component.build.json").read_text())
        relative = "packs/recovery/local.openrouter-generation-reader/wit/deps/reader.wit"
        self.assertEqual(provenance["sources"][relative], reader.file_sha(source / "wit/deps/reader.wit"))

    def test_archive_rejects_unsafe_ids_and_duplicate_entries(self):
        package, _ = self.archive("local.fixture")
        with self.assertRaisesRegex(ValueError, "identity"):
            reader.inspect_archive(package, "../escape")
        with tarfile.open(package, "w", format=tarfile.USTAR_FORMAT) as archive:
            for _ in range(2):
                info = tarfile.TarInfo("pack.json")
                info.size = 2
                archive.addfile(info, io.BytesIO(b"{}"))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            reader.inspect_archive(package, "local.fixture")


if __name__ == "__main__":
    unittest.main()
