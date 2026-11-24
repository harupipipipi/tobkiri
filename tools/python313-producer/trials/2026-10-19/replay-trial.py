"""Actual typed guest development replay; never native Core acceptance."""
from pathlib import Path
import json,sys
sys.path.insert(0,'/validation-deps')
sys.path.insert(0,'/replay-source/scripts/wasm')
from replay_ordered_tool_bridge import replay
result=replay(Path('/artifacts/component.wasm'),
              Path('/replay-source/packs/local.ai-tool-bridge-ordered-python/oracle.json'),
              fuel=2_000_000_000, timeout=30)
Path('/work/replay.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:result[k] for k in ['guest_runtime','all_admitted_parity',
 'all_exclusions_enforced','guest_matches_reviewed_producer','core_runtime_acceptance']}))
raise SystemExit(0 if result['all_admitted_parity'] and result['all_exclusions_enforced']
                 and result['guest_matches_reviewed_producer'] else 1)
