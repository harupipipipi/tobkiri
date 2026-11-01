"""Pure derivation negative cases; synthetic upstream evidence is never executed."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "derivation", HERE / "verify_derivation.py"
)
assert SPEC and SPEC.loader
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


class DerivationTests(unittest.TestCase):
    """Validate exact package metadata even when registry bytes are unchanged."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "src").mkdir()
        for name in ("Cargo.toml", "Cargo.lock", "src/upstream_strip.rs"):
            shutil.copyfile(HERE / name, self.root / name)
        self.upstream_lock = self.root / "upstream.lock"
        self.upstream_lock.write_bytes((HERE / "Cargo.lock").read_bytes())
        self.upstream_build = self.root / "upstream-build.txt"
        source = (HERE / "src/upstream_strip.rs").read_text()
        self.upstream_build.write_text(
            "fn strip(input:" + source.split("pub fn strip(input:", 1)[1]
        )
        evidence = json.loads((HERE / "upstream-lock-identities.json").read_text())
        evidence["upstream_registry_packages"] = evidence["registry_packages"]
        evidence["upstream_lock_sha256"] = hashlib.sha256(
            self.upstream_lock.read_bytes()
        ).hexdigest()
        evidence["upstream_build_rs_sha256"] = hashlib.sha256(
            self.upstream_build.read_bytes()
        ).hexdigest()
        (self.root / "upstream-lock-identities.json").write_text(json.dumps(evidence))
        patch = mock.patch.object(VERIFY, "HERE", self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def test_valid_synthetic_derivation(self) -> None:
        self.assertEqual(VERIFY.verify(self.upstream_lock, self.upstream_build)
                         ["registry_packages"], 32)

    def test_unexpected_manifest_sections(self) -> None:
        path = self.root / "Cargo.toml"
        original = path.read_text()
        for section in ("target.'cfg(unix)'.build-dependencies", "dev-dependencies",
                        "build-dependencies", "replace", "patch.crates-io"):
            with self.subTest(section=section):
                path.write_text(original + f'\n[{section}]\n'
                                'evil = { git = "https://example.invalid/fork" }\n')
                with self.assertRaisesRegex(ValueError, "exact allowed schema"):
                    VERIFY.verify(self.upstream_lock, self.upstream_build)

    def test_changed_root_version(self) -> None:
        path = self.root / "Cargo.lock"
        source = path.read_text().replace(
            'name = "tobkiri-python313-packager"\nversion = "0.1.0"',
            'name = "tobkiri-python313-packager"\nversion = "99.0.0"',
        )
        path.write_text(source)
        with self.assertRaisesRegex(ValueError, "local package identity"):
            VERIFY.verify(self.upstream_lock, self.upstream_build)

    def test_changed_root_edges(self) -> None:
        path = self.root / "Cargo.lock"
        source = path.read_text()
        start = source.index('name = "tobkiri-python313-packager"')
        end = source.index("[[package]]", start)
        source = source[:start] + ('name = "tobkiri-python313-packager"\n'
                                   'version = "0.1.0"\ndependencies = []\n') + source[end:]
        path.write_text(source)
        with self.assertRaisesRegex(ValueError, "local package identity"):
            VERIFY.verify(self.upstream_lock, self.upstream_build)

    def test_injected_edge_even_with_matching_local_evidence(self) -> None:
        path = self.root / "Cargo.lock"
        path.write_text(path.read_text().replace(
            'name = "anyhow"\n', 'dependencies = ["zstd"]\nname = "anyhow"\n'))
        import tomllib
        evidence_path = self.root / "upstream-lock-identities.json"
        evidence = json.loads(evidence_path.read_text())
        evidence["registry_packages"] = [p for p in tomllib.loads(path.read_text())
                                         ["package"] if "source" in p]
        evidence_path.write_text(json.dumps(evidence))
        with self.assertRaisesRegex(ValueError, "edge not in upstream"):
            VERIFY.verify(self.upstream_lock, self.upstream_build)

    def test_malformed_resolved_edge(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported"):
            VERIFY.resolved_edges({"dependencies": ["zstd 1 unexpected"]}, [])



class GraphTests(unittest.TestCase):
    """Check dependency closure independently of the pinned real registry graph."""

    def setUp(self) -> None:
        self.root = {"name": "root", "version": "1", "dependencies": ["dep"]}
        self.dep = {"name": "dep", "version": "1", "source": "registry"}

    def test_valid_graph(self) -> None:
        VERIFY.validate_graph([self.root, self.dep], ("root", "1"))

    def test_duplicate_package(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate lock"):
            VERIFY.validate_graph([self.root, self.dep, self.dep], ("root", "1"))

    def test_dangling_edge(self) -> None:
        with self.assertRaisesRegex(ValueError, "dangling"):
            VERIFY.validate_graph([self.root], ("root", "1"))

    def test_ambiguous_edge(self) -> None:
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            VERIFY.validate_graph(
                [self.root, self.dep, {**self.dep, "version": "2"}], ("root", "1")
            )

    def test_unreachable_package(self) -> None:
        with self.assertRaisesRegex(ValueError, "unreachable"):
            VERIFY.validate_graph(
                [self.root, self.dep, {**self.dep, "name": "extra"}], ("root", "1")
            )

    def test_duplicate_edge(self) -> None:
        self.root["dependencies"] = ["dep", "dep"]
        with self.assertRaisesRegex(ValueError, "duplicate dependency"):
            VERIFY.validate_graph([self.root, self.dep], ("root", "1"))


if __name__ == "__main__":
    unittest.main()
