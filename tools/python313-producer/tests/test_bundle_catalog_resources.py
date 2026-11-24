"""Inert raw-resource DATA tests; never import the original runtime or guest."""

import base64
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bundle_catalog_resources as source


def fixture():
    """Use repeated raw bytes to distinguish occurrence order from blob sharing."""
    lock = b'{"source_fixture":"lock"}'
    raw = b'{"source_fixture":"entry"}'
    rows = [
        {"index": 0, "path": "entry.json", "reread": False, "blob": 0},
        {"index": 0, "path": "entry.json", "reread": True, "blob": 0},
    ]

    def encode(value):
        return base64.b64encode(value).decode()

    witness = {
        "format": "tobkiri.bundle-source-bytes/1",
        "lock": source.identity(lock),
        "reads": [
            {
                "index": 0,
                "path": "entry.json",
                "reread": row["reread"],
                **source.identity(raw),
            }
            for row in rows
        ],
    }
    return {
        "format": "tobkiri.bundle-catalog-comparison/1",
        "oracle_commit": "54e7da008997a3eb45876cbe18459dba9902cc9d",
        "payload": json.dumps(
            {
                "format": "tobkiri.bundle-defaults/2",
                "lock_base64": encode(lock),
                "blobs": [encode(raw)],
                "reads": rows,
            }
        ),
        "source_identity": witness,
        "reread_mutation": {
            "occurrence": 1,
            "bytes_base64": encode(b"{}"),
            "expected_error": "inert original refusal fixture",
        },
    }


class RawResourcesTests(unittest.TestCase):
    def test_exact_raw_bytes_and_all_occurrences_survive_without_runtime_base64(self):
        cases = fixture()
        members, receipt = source.resources(cases)
        index = json.loads(members[source.INDEX])
        self.assertEqual(
            members[source.ROOT + "lock.json"], b'{"source_fixture":"lock"}'
        )
        self.assertEqual(
            members[source.ROOT + "blobs/0.json"], b'{"source_fixture":"entry"}'
        )
        self.assertEqual(index["reads"][0]["blob"], index["reads"][1]["blob"])
        self.assertEqual([row["reread"] for row in index["reads"]], [False, True])
        self.assertEqual(index["source_identity"], cases["source_identity"])
        self.assertNotIn("carrier", index)
        self.assertIsNone(receipt["diagnostic_mutation"])
        self.assertGreater(
            receipt["logical_source_bytes"], receipt["owned_source_bytes"]
        )

    def test_diagnostic_changes_only_the_selected_reread_and_has_distinct_identity(
        self,
    ):
        cases = fixture()
        original = copy.deepcopy(cases)
        positive, first = source.resources(cases)
        negative, second = source.resources(cases, diagnostic_reread=True)
        self.assertEqual(cases, original)
        self.assertEqual(
            negative[source.ROOT + "lock.json"], positive[source.ROOT + "lock.json"]
        )
        self.assertEqual(
            negative[source.ROOT + "blobs/0.json"],
            positive[source.ROOT + "blobs/0.json"],
        )
        self.assertEqual(negative[source.ROOT + "blobs/1.json"], b"{}")
        rows = json.loads(negative[source.INDEX])["reads"]
        self.assertEqual([row["blob"] for row in rows], [0, 1])
        self.assertNotEqual(
            first["source_identity_sha256"], second["source_identity_sha256"]
        )
        self.assertEqual(second["diagnostic_mutation"]["occurrence"], 1)

    def test_changed_source_boolean_ordinal_and_unreferenced_blobs_are_rejected(self):
        for mutation in ("bytes", "ordinal", "unreferenced"):
            cases = fixture()
            value = json.loads(cases["payload"])
            if mutation == "bytes":
                value["blobs"][0] = base64.b64encode(b"changed").decode()
            elif mutation == "ordinal":
                value["reads"][0]["blob"] = True
            else:
                value["blobs"].append("e30=")
            cases["payload"] = json.dumps(value)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                source.resources(cases)

    def test_actual_compiled_manifest_and_diagnostic_role_must_match_every_member(self):
        cases = fixture()
        members, receipt = source.resources(cases)
        proof = {
            "source_manifest": [
                {"destination": name, **source.identity(raw)}
                for name, raw in sorted(members.items())
            ]
        }
        source.verify_members(proof, {"embedded_resources": receipt}, cases)
        changed = copy.deepcopy(proof)
        changed["source_manifest"][0]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            source.verify_members(changed, {"embedded_resources": receipt}, cases)
        extra = copy.deepcopy(proof)
        extra["source_manifest"].append({"destination": source.ROOT + "extra.json"})
        with self.assertRaises(ValueError):
            source.verify_members(extra, {"embedded_resources": receipt}, cases)
        _, diagnostic = source.resources(cases, diagnostic_reread=True)
        with self.assertRaises(ValueError):
            source.verify_members(proof, {"embedded_resources": diagnostic}, cases)


if __name__ == "__main__":
    unittest.main()
