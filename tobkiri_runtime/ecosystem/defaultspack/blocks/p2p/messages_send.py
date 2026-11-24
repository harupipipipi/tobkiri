from __future__ import annotations



from blocks._common import error, ok
from blocks.p2p._helpers import settings_from
from domain.p2p.identity import load_or_create_identity
from domain.p2p.peer_store import PEER_APPROVED, PeerStore
from domain.p2p.protocol import make_envelope, sign_envelope


def run(input_data, context=None):
    input_data = input_data if isinstance(input_data, dict) else {}
    settings = settings_from(input_data, context)
    if not settings.enabled:
        return error("P2P is disabled", "P2P_DISABLED")
    peer_id = str(input_data.get("peer_id") or input_data.get("recipient_id") or "").strip()
    if not peer_id:
        return error("peer_id is required", "INVALID_INPUT")
    peer = PeerStore(settings.store_path).get_peer(peer_id)
    if peer is None:
        return error("unknown peer", "PEER_UNKNOWN")
    if peer.status != PEER_APPROVED:
        return error("peer is not approved", "PEER_UNAPPROVED")
    if not peer.hmac_secret:
        return error("peer shared secret missing", "SHARED_SECRET_MISSING")
    body = input_data.get("body")
    if not isinstance(body, dict):
        text = str(input_data.get("text") or input_data.get("message") or "").strip()
        body = {"text": text} if text else {}
    identity = load_or_create_identity(store_path=settings.store_path)
    envelope = make_envelope(
        sender_id=identity.node_id,
        recipient_id=peer.peer_id,
        message_type=str(input_data.get("type") or "message"),
        body=body,
        metadata=input_data.get("metadata") if isinstance(input_data.get("metadata"), dict) else {},
        ttl_seconds=int(input_data.get("ttl_seconds") or settings.envelope_ttl_seconds),
    )
    return ok({"envelope": sign_envelope(envelope, peer.hmac_secret), "peer": peer.as_dict()})
