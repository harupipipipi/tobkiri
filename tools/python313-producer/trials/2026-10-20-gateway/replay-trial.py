"""Run reviewed two-component development replay under external isolation."""
import sys

sys.path.insert(0, "/validation-deps")
sys.path.insert(0, "/replay-source/scripts/wasm")
from replay_gateway_tool_caller import main

sys.argv = [
    "replay_gateway_tool_caller.py",
    "--caller", "/caller/component.wasm",
    "--provider", "/provider/component.wasm",
    "--oracle", "/replay-source/tests/fixtures/issue1520/gateway-tool-caller.source.json",
    "--pins", "/inputs/pins.json", "--wac", "/wac",
    "--output", "/work/replay.json",
]
raise SystemExit(main())
