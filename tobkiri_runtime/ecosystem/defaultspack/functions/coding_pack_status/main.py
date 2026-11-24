from __future__ import annotations

import sys
from pathlib import Path

PACK_ROOT = Path(__file__).resolve().parents[2]
RUMI_ROOT = PACK_ROOT.parents[1]
for path in (str(PACK_ROOT), str(RUMI_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from domain.function_runtime.dispatcher import run_defaultspack_function


def run(context, args):
    return run_defaultspack_function("coding_pack_status", args, context)
