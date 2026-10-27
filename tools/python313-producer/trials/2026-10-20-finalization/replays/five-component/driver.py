"""Execute the reviewed exact five-component graph, not native Core acceptance."""
import sys
sys.path.insert(0, '/validation-deps')
sys.path.insert(0, '/replay-source/scripts/wasm')
from replay_gateway_finalizer import main
sys.argv = ['replay_gateway_finalizer.py',
    '--bridge', '/bridge/component.wasm', '--caller', '/caller/component.wasm',
    '--usage-cost', '/usage-cost/component.wasm',
    '--stream-normalizer', '/stream-normalizer/component.wasm',
    '--finalizer', '/finalizer/component.wasm', '--oracle', '/inputs/oracle.json',
    '--pins', '/inputs/pins.json',
    '--pins-sha256', 'c6d2b98def2bb07c95b29ec0e0f845a47c73c747884e297b60f26bc61d0d727b',
    '--wac', '/wac', '--output', '/work/replay.json']
raise SystemExit(main())
