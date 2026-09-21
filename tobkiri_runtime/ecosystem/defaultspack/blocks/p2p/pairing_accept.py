from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error, ok
from blocks.p2p._helpers import settings_from
from domain.p2p.pairing import PairingManager


def run(input_data, context=None):
    input_data = input_data if isinstance(input_data, dict) else {}
    code = str(input_data.get("code") or input_data.get("pairing_code") or "").strip()
    if not code:
        return error("pairing code is required", "INVALID_INPUT")
    settings = settings_from(input_data, context)
    if not settings.enabled:
        return error("P2P is disabled", "P2P_DISABLED")
    result = PairingManager(settings.store_path).accept_pairing(
        code,
        peer_id=str(input_data.get("peer_id") or ""),
        peer_fingerprint=str(input_data.get("peer_fingerprint") or input_data.get("fingerprint") or ""),
        peer_label=str(input_data.get("peer_label") or input_data.get("label") or ""),
        hmac_secret=str(input_data.get("hmac_secret") or input_data.get("shared_secret") or ""),
        capabilities=input_data.get("capabilities") if isinstance(input_data.get("capabilities"), list) else None,
        allowed_company_ids=input_data.get("allowed_company_ids") if isinstance(input_data.get("allowed_company_ids"), list) else None,
    )
    if not result.get("ok"):
        return error(str(result.get("reason") or "pairing accept failed"), str(result.get("code") or "PAIRING_FAILED"))
    return ok(result)
