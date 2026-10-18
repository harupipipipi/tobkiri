"""The inventory must stay complete and may not disguise work as retirement."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

path=Path(__file__).resolve().parents[1]/"inventory.py"
spec=importlib.util.spec_from_file_location("inventory",path)
inventory=importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)


class InventoryTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[dict, Path]:
        """Create a small current source and an evidenced completed row."""
        source = root / "tobkiri_runtime/core_runtime/canonical.py"
        source.parent.mkdir(parents=True)
        source.write_text("VALUE = 1\n")
        (root / "proof.json").write_text("{}")
        unit = inventory.discover(root)[0]
        unit.update(target="core", status="core+vectors",
                    evidence=["proof.json"], target_provisional=False)
        return {"units": [unit]}, source

    def test_checked_in_inventory_covers_every_discovered_unit(self):
        document=json.loads(inventory.LEDGER.read_text())
        self.assertEqual(inventory.validate(document),[])
        self.assertTrue(any(unit["kind"]=="defaults-domain" for unit in document["units"]))
        self.assertFalse(any(unit["target"]=="retired" for unit in document["units"]))

    def test_todo_and_unevidenced_retirement_block_deletion(self):
        document=json.loads(inventory.LEDGER.read_text())
        self.assertTrue(inventory.validate(document,deletion=True))
        document["units"][0]["target"]="retired"
        document["units"][0]["status"]="retired+approved"
        self.assertTrue(any("retirement" in error for error in inventory.validate(document)))

    def test_refresh_keeps_reviewed_target_until_its_source_changes(self):
        prior = {
            "legacy_unit": "canonical.py", "source_tree_sha256": "original",
            "target": "core", "target_provisional": False,
            "status": "core+vectors", "evidence": ["proof.json"],
        }
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "inventory.json"
            ledger.write_text(json.dumps({"units": [prior]}))
            for digest, status, provisional in [
                ("original", "core+vectors", False), ("changed", "todo", True),
            ]:
                fresh = {**prior, "source_tree_sha256": digest,
                         "target_provisional": True, "status": "todo"}
                with patch.object(inventory, "LEDGER", ledger), patch.object(
                    inventory, "discover", return_value=[fresh]
                ):
                    actual = inventory.refresh()["units"][0]
                self.assertEqual(actual["status"], status)
                self.assertEqual(actual["target_provisional"], provisional)

    def test_completed_source_drift_fails_without_refresh_but_todo_keeps_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document, source = self.fixture(root)
            self.assertEqual(inventory.validate(document, deletion=True, root=root), [])
            source.write_text("VALUE = 2\n")
            errors = inventory.validate(document, deletion=True, root=root)
            self.assertTrue(any("completed source differs" in error for error in errors))
            document["units"][0]["status"] = "todo"
            self.assertEqual(inventory.validate(document, root=root), [])
            self.assertEqual(document["units"][0]["evidence"], ["proof.json"])
            self.assertTrue(any("unresolved" in error for error in
                                inventory.validate(document, deletion=True, root=root)))

    def test_new_discovered_source_cannot_be_omitted_from_completed_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document, source = self.fixture(root)
            source.with_name("added.py").write_text("VALUE = 3\n")
            errors = inventory.validate(document, deletion=True, root=root)
            self.assertTrue(any("missing legacy unit:" in error and "added.py" in error
                                for error in errors))

    def test_removed_source_retains_its_evidenced_replacement_row(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document, source = self.fixture(root)
            source.unlink()
            document["units"][0]["source_missing"] = True
            self.assertEqual(inventory.validate(document, deletion=True, root=root), [])
            document["units"][0]["status"] = "todo"
            self.assertTrue(any("unresolved" in error for error in
                                inventory.validate(document, deletion=True, root=root)))

    def test_evidence_and_parity_fixture_cannot_escape_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repository"
            root.mkdir()
            document, _ = self.fixture(root)
            outside = base / "outside.json"
            outside.write_text("{}")
            (root / "escape.json").symlink_to(outside)
            for field in ("evidence", "parity_fixture"):
                for reference in ("../outside.json", str(outside), "escape.json"):
                    with self.subTest(field=field, reference=reference):
                        row = {**document["units"][0],
                               field: [reference] if field == "evidence" else reference}
                        errors = inventory.validate({"units": [row]}, root=root)
                        self.assertTrue(any("outside repository" in error for error in errors))

    def test_parity_fixture_must_exist_even_when_evidence_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document, _ = self.fixture(root)
            document["units"][0].update(status="ported+parity", target="python-wasm",
                                        parity_fixture="missing.json")
            errors = inventory.validate(document, deletion=True, root=root)
            self.assertTrue(any("parity fixture missing" in error for error in errors))

    def test_empty_duplicate_and_unknown_status_inventories_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document, _ = self.fixture(root)
            self.assertTrue(inventory.validate({"units": []}, root=root))
            self.assertIn("duplicate legacy unit", inventory.validate(
                {"units": document["units"] * 2}, root=root))
            document["units"][0]["status"] = "complete"
            self.assertTrue(any("unsupported migration status" in error for error in
                                inventory.validate(document, root=root)))


if __name__=="__main__": unittest.main()
