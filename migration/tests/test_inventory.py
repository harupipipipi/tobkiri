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


if __name__=="__main__": unittest.main()
