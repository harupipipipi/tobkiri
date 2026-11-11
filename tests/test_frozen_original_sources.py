"""The frozen original Profile bytes stay exact and cannot hide a covered edge change."""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/wasm"))

import frozen_original_sources as frozen  # noqa: E402

INVENTORIES = (
    "packs/local.ai-tool-bridge-ordered-python/python/inventory.json",
    "packs/local.ai-gateway-tool-caller-python/python/inventory.json",
    "packs/local.ai-gateway-finalizer-python/python/inventory.json",
    "packs/local.ai-stream-normalizer-python/python/inventory.json",
    "packs/local.ai-usage-cost-python/python/inventory.json",
)


def _contract_ids(value: object, found: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "contract_id" and isinstance(item, str):
                found.add(item)
            else:
                _contract_ids(item, found)
    elif isinstance(value, list):
        for item in value:
            _contract_ids(item, found)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class FrozenOriginalSourcesTest(unittest.TestCase):
    def test_every_inventory_pins_the_frozen_bytes_exactly(self) -> None:
        for inventory in INVENTORIES:
            document = json.loads((ROOT / inventory).read_bytes())
            pinned = {
                item["path"]: item
                for item in document["sources"]
                if item["path"] in frozen.FROZEN_SOURCES
            }
            self.assertEqual(set(pinned), set(frozen.FROZEN_SOURCES), inventory)
            for relative, record in pinned.items():
                data = frozen.frozen_override(ROOT / relative).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), record["sha256"])
                self.assertEqual(len(data), record["bytes"])

    def test_override_applies_only_to_the_two_frozen_paths(self) -> None:
        other = ROOT / "tobkiri_runtime/ecosystem/rumi_ai_gateway_pack/runtime/gateway.py"
        self.assertEqual(frozen.frozen_override(other), other)
        self.assertNotEqual(
            frozen.frozen_override(ROOT / frozen.FROZEN_SOURCES[0]),
            ROOT / frozen.FROZEN_SOURCES[0],
        )

    def test_live_profile_keeps_every_covered_contract_edge_unchanged(self) -> None:
        covered: set[str] = set()
        for inventory in INVENTORIES:
            _contract_ids(json.loads((ROOT / inventory).read_bytes()), covered)
        self.assertIn("tobkiri.service.ai.tool_intent.normalize.v1", covered)
        for relative in frozen.FROZEN_SOURCES:
            old = json.loads(frozen.frozen_override(ROOT / relative).read_bytes())
            new = json.loads((ROOT / relative).read_bytes())
            for name in ("requested_edges",):
                before = sorted(
                    _canonical(edge) for edge in old[name]
                    if edge["contract_id"] in covered
                )
                after = sorted(
                    _canonical(edge) for edge in new[name]
                    if edge["contract_id"] in covered
                )
                self.assertEqual(
                    before, after,
                    f"{relative}: a covered contract edge changed; re-freeze the "
                    "Python evidence instead of editing the snapshot",
                )


if __name__ == "__main__":
    unittest.main()
