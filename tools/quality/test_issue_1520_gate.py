"""Negative fixtures for the migration gate; never weaken production checks."""
import importlib.util
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("issue_gate", Path(__file__).with_name("issue_1520_gate.py"))
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class BoundaryTests(unittest.TestCase):
    def inventory_fixture(self, root: Path) -> tuple[Path, Path]:
        """Write an evidenced source-bound row for the final-gate tests."""
        source = root / "tobkiri_runtime/core_runtime/canonical.py"
        source.parent.mkdir(parents=True)
        source.write_text("VALUE = 1\n")
        digest = hashlib.sha256()
        digest.update(source.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(source.read_bytes()).digest())
        (root / "proof.json").write_text("{}")
        path = root / "migration/inventory.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"units": [{
            "legacy_unit": source.relative_to(root).as_posix(),
            "source_tree_sha256": digest.hexdigest(),
            "target": "core", "status": "core+vectors", "evidence": ["proof.json"],
        }]}))
        return path, source

    def test_test_block_cannot_hide_later_production(self):
        source = '#[cfg(test)] mod tests { let s = "}"; fn t() { } }\nPackId::parse("local.exposed");'
        self.assertNotIn('let s', gate.without_unit_tests(source))
        self.assertIn('local.exposed', gate.without_unit_tests(source))

    def test_literal_pack_and_listener_are_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "crates/tobkiri-core/src/lib.rs"
            src.parent.mkdir(parents=True)
            src.write_text('PackId::parse("local.fixture"); use std::net::TcpListener;')
            errors = gate.check_sources(root)
            self.assertTrue(any('constructor' in e for e in errors))
            self.assertTrue(any('listening' in e for e in errors))

    def test_pack_defined_label_is_denied_without_constructor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = root / "packs/fixture/pack.json"
            pack.parent.mkdir(parents=True)
            pack.write_text(json.dumps({'id': 'local.fixture', 'shapes': [{'name': 'local.fixture.chat'}]}))
            src = root / "crates/tobkiri-core/src/lib.rs"
            src.parent.mkdir(parents=True)
            src.write_text('const ROUTE: &str = "local.fixture.chat";')
            self.assertTrue(gate.check_sources(root))

    def test_unresolved_or_evidenceless_inventory_cannot_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "migration/inventory.json"
            path.parent.mkdir(parents=True)
            for row in ({'legacy_unit': 'one', 'status': 'todo'}, {'legacy_unit': 'one', 'status': 'core+vectors', 'evidence': []}):
                path.write_text(json.dumps({'units': [row]}))
                self.assertTrue(gate.check_inventory(root))

    def test_release_test_gate_not_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "crates/tobkiri-gate/Cargo.toml"
            path.parent.mkdir(parents=True)
            path.write_text('[features]\ndefault=["test-gate"]\n')
            self.assertTrue(gate.check_sources(root))

    def test_external_test_module_requires_cfg_test_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "crates/tobkiri-core/src"
            src.mkdir(parents=True)
            test = src / "fixture.rs"
            test.write_text('PackId::parse("local.fixture");')
            owner = src / "lib.rs"
            owner.write_text('#[cfg(test)]\n#[path="fixture.rs"]\nmod tests;')
            self.assertFalse(gate.check_sources(root))
            owner.write_text('#[path="fixture.rs"]\nmod tests;')
            self.assertTrue(gate.check_sources(root))

    def test_platform_test_module_requires_test_in_every_build_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "crates/tobkiri-core/src"
            src.mkdir(parents=True)
            (src / "fixture.rs").write_text('PackId::parse("local.fixture");')
            owner = src / "lib.rs"
            for platform in ("unix", "windows"):
                with self.subTest(platform=platform):
                    owner.write_text(
                        f'#[cfg(all(test, {platform}))]\n'
                        '#[path="fixture.rs"]\nmod tests;'
                    )
                    self.assertFalse(gate.check_sources(root))
                    owner.write_text(
                        f'#[cfg(any(test, {platform}))]\n'
                        '#[path="fixture.rs"]\nmod tests;'
                    )
                    self.assertTrue(gate.check_sources(root))

    def test_platform_test_block_cannot_hide_following_production(self):
        source = (
            '#[cfg(all(test, unix))] mod tests { fn t() {} }\n'
            'PackId::parse("local.exposed");'
        )
        remaining = gate.without_unit_tests(source)
        self.assertNotIn("fn t", remaining)
        self.assertIn("local.exposed", remaining)

    def test_final_host_check_includes_legacy_pack_names_and_frontend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = root / "tobkiri_runtime/ecosystem/legacy_pack/pack.v4.json"
            pack.parent.mkdir(parents=True)
            pack.write_text(json.dumps({'pack': {'id': 'legacy_pack'}, 'contracts': []}))
            src = root / "tobkiri_launcher/frontend/src/main.tsx"
            src.parent.mkdir(parents=True)
            src.write_text('const pack = "legacy_pack";')
            self.assertFalse(gate.check_sources(root))
            self.assertTrue(gate.check_sources(root, complete=True))

    def test_evidence_string_alone_is_not_a_completion_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "migration/inventory.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'units': [{
                'legacy_unit': 'one', 'status': 'ported+parity',
                'evidence': ['missing.json'], 'parity_fixture': 'missing-fixture.json',
            }]}))
            self.assertTrue(gate.check_inventory(root))

    def test_host_cannot_import_authority_directly_or_under_an_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "crates/tobkiri-cli/Cargo.toml"
            path.parent.mkdir(parents=True)
            path.write_text('[dependencies]\ntobkiri-core={path="../tobkiri-core"}\n')
            self.assertFalse(gate.check_dependencies(root))
            path.write_text('[dependencies]\nauthority={package="tobkiri-gate",path="../tobkiri-gate"}\n')
            self.assertTrue(gate.check_dependencies(root))

    def test_final_inventory_gate_rejects_stale_completed_source_and_missing_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, source = self.inventory_fixture(root)
            self.assertEqual(gate.check_inventory(root), [])
            source.write_text("VALUE = 2\n")
            self.assertTrue(any("completed source differs" in error
                                for error in gate.check_inventory(root)))
            source.write_text("VALUE = 1\n")
            source.with_name("added.py").write_text("VALUE = 3\n")
            self.assertTrue(any("missing legacy unit:" in error
                                for error in gate.check_inventory(root)))

    def test_final_inventory_gate_preserves_checked_paths_and_uses_trusted_validator(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "repository"
            root.mkdir()
            path, _ = self.inventory_fixture(root)
            (path.parent / "inventory.py").write_text("raise AssertionError('untrusted')\n")
            self.assertEqual(gate.check_inventory(root), [])
            (base / "outside.json").write_text("{}")
            document = json.loads(path.read_text())
            document["units"][0]["evidence"] = ["../outside.json"]
            path.write_text(json.dumps(document))
            self.assertTrue(any("outside repository" in error
                                for error in gate.check_inventory(root)))


if __name__ == "__main__":
    unittest.main()
