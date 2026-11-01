"""Actual standalone bridge replay; never native Core acceptance."""
import runpy
import sys
sys.path.insert(0, '/validation-deps')
sys.path.insert(0, '/replay-source/scripts/wasm')
sys.argv = ['replay_ordered_tool_bridge_v2.py', '--artifact', '/artifact/component.wasm', '--oracle', '/replay-source/packs/local.ai-tool-bridge-ordered-python/integer-v2.observations.json', '--output', '/work/replay.json']
runpy.run_module('replay_ordered_tool_bridge_v2', run_name='__main__')
