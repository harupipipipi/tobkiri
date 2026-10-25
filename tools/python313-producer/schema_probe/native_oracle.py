"""Execute only the original locked wheels through ordinary native imports."""

import json
from pathlib import Path
import sys

# The interpreter is invoked with -I -S. No ambient site-package or .pth runs.
control = json.loads(Path("/control.json").read_bytes())
sys.path[:0] = ["/probe-source", *control["native_paths"]]
from exercise import exercise

if sys.version_info[:3] != (3, 13, 16):
    raise ValueError("the original locked native CPython 3.13.16 oracle is required")
result = exercise()
Path("/work/native.json").write_text(
    json.dumps(
        {"python": list(sys.version_info[:3]), "observation": result},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
)
