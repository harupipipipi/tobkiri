"""Actual public source compiler selection; no activation/Flow execution claims."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml
from core_runtime.profile_authoring import build_named_profile
from tobkiri_host.artifact_compiler import compile_pack_root

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT.parents[2]
# Required owned author-path bootstrap for standalone tests.
sys.path.insert(0, str(ROOT))
from author_profile import render_tauri_intent  # noqa: E402 -- author bootstrap
from build_pack import build  # noqa: E402 -- author bootstrap


class NamedProfileTests(unittest.TestCase):
    def release(self, output, pack, intent):
        return build_named_profile(
            output, template_bundle=RUNTIME_ROOT / "ecosystem/defaultspack/v4",
            intent=intent, additional_pack_roots=(pack,),
        )

    def test_selected_catalog_format_and_exact_projection(self):
        with tempfile.TemporaryDirectory(prefix=".profile-selected-", dir=ROOT) as tmp:
            pack = build(Path(tmp) / "pack")
            flow = yaml.safe_load((pack / "content/flows/acceptance.temporal.turn.flow.yaml")
                                  .read_text())
            self.assertEqual(flow["flow_id"], "acceptance.temporal.turn")
            self.assertEqual(flow["steps"][0]["type"], "function")
            self.assertEqual(flow["steps"][0]["function"],
                             "acceptance.temporal.context.reduce")
            self.assertEqual(set(flow["inputs"]), {"namespace", "state", "event"})
            self.assertTrue((pack / "content/prompts/acceptance.temporal.rules.system.md")
                            .read_text().startswith("# Tobkiri temporal context rules"))
            release = self.release(Path(tmp) / "release", pack, render_tauri_intent())
            profile = json.loads((release / "authored.profile.v5.json").read_text())
            projection = profile["content_projections"][0]
            manifest = json.loads((pack / "pack.v4.json").read_text())
            self.assertEqual(projection["source_artifact_digest"],
                             manifest["pack"]["artifact_digest"])
            self.assertEqual(projection["source_pack_id"], "acceptance.temporal.context")
            self.assertEqual(projection["file_count"], 4)
            lock = json.loads((release / "authored.profile.lock.v5.json").read_text())
            self.assertEqual(lock["activation_authority"], "unbound")

    def test_unselected_projection_and_removed_pack(self):
        with tempfile.TemporaryDirectory(prefix=".profile-unselected-", dir=ROOT) as tmp:
            pack = build(Path(tmp) / "pack")
            for select_pack in (True, False):
                intent = render_tauri_intent(temporal_selected=select_pack,
                                            projection_selected=False)
                release = self.release(Path(tmp) / f"release-{select_pack}", pack,
                                       intent)
                profile = json.loads((release / "authored.profile.v5.json").read_text())
                self.assertEqual(profile["content_projections"], [])
                members = {entry["pack_id"] for entry in profile["packs"]}
                self.assertEqual("acceptance.temporal.context" in members, select_pack)

    def test_removed_pack_with_retained_projection_denied(self):
        with tempfile.TemporaryDirectory(prefix=".profile-denied-", dir=ROOT) as tmp:
            pack = build(Path(tmp) / "pack")
            intent = render_tauri_intent(temporal_selected=False,
                                        projection_selected=True)
            with self.assertRaises(Exception) as error:
                self.release(Path(tmp) / "release", pack, intent)
            self.assertIn("projection Pack is unselected", str(error.exception))
            message = f"{type(error.exception).__name__}: {error.exception}\n"
            (ROOT / "removed-pack-projection-denial.log").write_text(message)
            self.assertFalse((Path(tmp) / "release").exists())

    def test_rebuilt_renamed_pack_has_explicit_selection(self):
        with tempfile.TemporaryDirectory(prefix=".profile-renamed-", dir=ROOT) as tmp:
            pack_id = "independent.temporal.renamed"
            function_id = "independent.temporal.function"
            pack = build(Path(tmp) / "pack", pack_id, function_id)
            intent = render_tauri_intent(temporal_pack_id=pack_id,
                                        temporal_function_id=function_id,
                                        profile_id="independent.temporal.named")
            release = self.release(Path(tmp) / "release", pack, intent)
            profile = json.loads((release / "authored.profile.v5.json").read_text())
            self.assertEqual(profile["content_projections"][0]["source_pack_id"], pack_id)
            compiled = compile_pack_root(pack)
            self.assertEqual(compiled.routes[(f"{pack_id}.v1", "temporal.reduce")]
                             ["function_id"], function_id)
            workflow = json.loads((pack / f"content/workflows/{pack_id}.workflow.intent.v1.json")
                                  .read_text())
            request = workflow["steps"][1]["request"]
            contract = json.loads((pack / "contracts.v4.json").read_text())["contracts"][0]
            self.assertEqual(request["contract_id"], contract["contract_id"])
            self.assertEqual(request["contract_revision_digest"],
                             contract["revision_digest"])
            self.assertEqual(request["operation_id"], "timing.project.owner")
            self.assertEqual(request["function_id"], function_id)
            self.assertNotIn("function_principal_id", request)
            flow = yaml.safe_load((pack / "content/flows/acceptance.temporal.turn.flow.yaml")
                                  .read_text())
            self.assertEqual(flow["steps"][0]["function"], function_id)


if __name__ == "__main__":
    unittest.main()
