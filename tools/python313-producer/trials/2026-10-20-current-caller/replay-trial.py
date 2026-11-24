"""Run the reviewed current two-component graph, never native Core acceptance."""
import sys
sys.path.insert(0, '/validation-deps')
sys.path.insert(0, '/replay-source/scripts/wasm')
from replay_gateway_tool_caller_current import main
sys.argv = ['replay_gateway_tool_caller_current.py',
    '--caller', '/caller/component.wasm', '--provider', '/provider/component.wasm',
    '--oracle', '/inputs/oracle.json', '--pins', '/inputs/pins.json',
    '--pins-sha256', 'd8ed300d82dd36e2bf09cd2988850fb0a2291d604b37da0fc72a48345f91c916',
    '--wac', '/wac', '--output', '/work/replay.json']
raise SystemExit(main())
