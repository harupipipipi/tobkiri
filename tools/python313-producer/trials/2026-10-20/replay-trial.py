"""Real v2 guest replay inside the separately enforced networkless supervisor."""
import json
from pathlib import Path
import sys

sys.path.insert(0, "/validation-deps")
sys.path.insert(0, "/replay-source/scripts/wasm")
from replay_ordered_tool_bridge_v2 import replay

result = replay(
    Path("/artifacts/component.wasm"),
    Path("/replay-source/packs/local.ai-tool-bridge-ordered-python/"
         "integer-v2.observations.json"),
    fuel=2_000_000_000,
    timeout=30,
)
Path("/work/replay.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps({key: result[key] for key in (
    "replay_succeeded", "wasm_execution_performed", "all_admitted_parity",
    "all_nonfinite_boundaries_verified", "all_lifecycles_completed",
    "guest_matches_reviewed_producer", "native_core_acceptance",
)}))
raise SystemExit(0 if result["replay_succeeded"] else 1)
