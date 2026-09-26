import os
import sys

_PACK_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_DEFAULTSPACK_ROOT = os.path.join(os.path.dirname(_PACK_ROOT), "defaultspack")
for _path in (_PACK_ROOT, _DEFAULTSPACK_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from blocks._common import error, ok
from ecosystem.rumi_operations_team_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime


def run(input_data, context):
    try:
        return ok(MimoCodingCompanyRuntime().manifest())
    except Exception as exc:
        return error("MiMo coding company manifest failed: " + str(exc), "MIMO_CODING_COMPANY_ERROR")
