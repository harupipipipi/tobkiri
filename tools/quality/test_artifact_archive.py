"""Source contracts for the private 90-day artifact archive chain."""
from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
PINS = ROOT / "tools/python313-producer/artifact-archive-pins.json"
ARCHIVE = WORKFLOWS / "issue-1520-artifact-archive.yml"
EXPIRING_RUNS = ("39956025119", "40182231971")
EXPIRING_ARTIFACTS = (
    "python313-producer-cli",
    "python313-original-components",
    "ui-rpds-inactive-component",
)


class ArtifactArchiveContract(unittest.TestCase):
    def test_pins_are_the_only_place_naming_the_expiring_source_runs(self) -> None:
        pins = json.loads(PINS.read_text())
        self.assertEqual(pins["format"], "tobkiri.artifact-archive-pins/1")
        self.assertEqual(
            {pin["source"]["run_id"] for pin in pins["artifacts"].values()},
            {int(run) for run in EXPIRING_RUNS},
        )
        for path in sorted(WORKFLOWS.glob("issue-1520-*.yml")):
            source = path.read_text()
            for run in EXPIRING_RUNS:
                with self.subTest(workflow=path.name, run=run):
                    self.assertNotIn(run, source)
            for name in EXPIRING_ARTIFACTS:
                with self.subTest(workflow=path.name, artifact=name):
                    self.assertNotRegex(
                        source, rf"download-artifact@v4\n\s+with:\n\s+name: {re.escape(name)}\n")

    def test_consumers_restore_only_through_the_verifying_archive_fetch(self) -> None:
        fetches = 0
        for path in sorted(WORKFLOWS.glob("issue-1520-*.yml")):
            source = path.read_text()
            for match in re.finditer(r"artifact_archive\.py fetch\n\s+--kind (\S+)", source):
                fetches += 1
                with self.subTest(workflow=path.name, kind=match.group(1)):
                    self.assertIn(match.group(1), {"producer-cli", "original-components", "rpds"})
                    self.assertNotIn("continue-on-error", source)
        self.assertGreaterEqual(fetches, 20)

    def test_archive_workflow_is_private_read_only_and_never_publishes(self) -> None:
        source = ARCHIVE.read_text()
        self.assertIn("permissions:\n  contents: read\n  actions: read\n", source)
        self.assertEqual(source.count("retention-days: 90"), 2)
        self.assertNotIn("contents: write", source)
        for publishing in ("gh release", "action-gh-release", "packages: write", "ghcr", "npm publish",
                           "aws ", "gsutil", "upload-pages-artifact"):
            self.assertNotIn(publishing, source)

    def test_pins_name_trusted_refs_and_a_pinned_archive_run(self) -> None:
        pins = json.loads(PINS.read_text())
        self.assertEqual(pins["archive_workflow_path"], ".github/workflows/issue-1520-artifact-archive.yml")
        self.assertTrue(pins["trusted_refs"])
        for kind, pin in pins["artifacts"].items():
            with self.subTest(kind=kind):
                self.assertIsInstance(pin["archive_run"], int)
                self.assertEqual(pin["archive_attempt"], 1)
                self.assertEqual(
                    set(pin["source"]),
                    {"run_id", "run_attempt", "head_sha", "workflow_path", "artifact_name", "artifact_id"},
                )

    def test_existing_byte_pins_stay_in_their_source_of_truth(self) -> None:
        schema = (ROOT / "tools/python313-producer/build_schema.py").read_text()
        self.assertEqual(len(re.findall(r'(?m)^EXTENSION_SHA256 = "[0-9a-f]{64}"', schema)), 1)
        self.assertEqual(len(re.findall(r'(?m)^COMPONENT_SHA256 = "[0-9a-f]{64}"', schema)), 1)
        tool = (ROOT / "tools/python313-producer/artifact_archive.py").read_text()
        for name in ("EXTENSION_SHA256", "COMPONENT_SHA256", "RECIPE_SHA256", "STAGED_SHA256"):
            self.assertIn(f'"{name}"', tool)


if __name__ == "__main__":
    unittest.main()
