from __future__ import annotations



from blocks._common import error, ok
from blocks.p2p._helpers import settings_from
from domain.p2p.peer_store import PeerStore


def run(input_data, context=None):
    input_data = input_data if isinstance(input_data, dict) else {}
    settings = settings_from(input_data, context)
    store = PeerStore(settings.store_path)
    method = str(input_data.get("_method") or "GET").upper()
    action = str(input_data.get("action") or "").strip().lower()
    if method == "GET" and not action:
        return ok({"peers": store.list_peers()})

    peer_id = str(input_data.get("peer_id") or input_data.get("id") or "").strip()
    if not peer_id:
        return error("peer_id is required", "INVALID_INPUT")
    if action == "block" or method == "DELETE":
        return ok({"peer": store.block_peer(peer_id, reason=str(input_data.get("reason") or "")).as_dict()})
    if action in {"approve", "upsert", ""}:
        if not settings.enabled:
            return error("P2P is disabled", "P2P_DISABLED")
        try:
            peer = store.approve_peer(
                peer_id,
                fingerprint=str(input_data.get("fingerprint") or ""),
                hmac_secret=str(input_data.get("hmac_secret") or input_data.get("shared_secret") or ""),
                capabilities=input_data.get("capabilities") if isinstance(input_data.get("capabilities"), list) else None,
                allowed_company_ids=input_data.get("allowed_company_ids") if isinstance(input_data.get("allowed_company_ids"), list) else None,
                label=str(input_data.get("label") or ""),
                metadata=input_data.get("metadata") if isinstance(input_data.get("metadata"), dict) else None,
            )
        except ValueError as exc:
            return error(str(exc), "INVALID_INPUT")
        return ok({"peer": peer.as_dict()})
    return error("unsupported peer action", "INVALID_INPUT")
