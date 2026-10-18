from __future__ import annotations



from blocks._common import ok
from blocks.p2p._helpers import settings_from
from domain.p2p.peer_store import PeerStore


def run(input_data, context=None):
    settings = settings_from(input_data, context)
    peers = PeerStore(settings.store_path).list_peers()
    return ok(
        {
            "p2p": settings.as_dict(),
            "peer_count": len(peers),
            "approved_peer_count": len([peer for peer in peers if peer.get("status") == "approved"]),
        }
    )
