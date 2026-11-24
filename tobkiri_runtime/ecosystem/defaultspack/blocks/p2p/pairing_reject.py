from __future__ import annotations



from blocks._common import error, ok
from blocks.p2p._helpers import settings_from
from domain.p2p.pairing import PairingManager


def run(input_data, context=None):
    input_data = input_data if isinstance(input_data, dict) else {}
    code = str(input_data.get("code") or input_data.get("pairing_code") or "").strip()
    if not code:
        return error("pairing code is required", "INVALID_INPUT")
    settings = settings_from(input_data, context)
    result = PairingManager(settings.store_path).reject_pairing(code, reason=str(input_data.get("reason") or ""))
    if not result.get("ok"):
        return error(str(result.get("reason") or "pairing reject failed"), str(result.get("code") or "PAIRING_FAILED"))
    return ok(result)
