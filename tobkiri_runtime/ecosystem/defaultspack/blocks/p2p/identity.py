from __future__ import annotations



from blocks._common import ok
from blocks.p2p._helpers import settings_from
from domain.p2p.identity import load_or_create_identity, rotate_identity


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def run(input_data, context=None):
    input_data = input_data if isinstance(input_data, dict) else {}
    settings = settings_from(input_data, context)
    if _truthy(input_data.get("rotate")):
        label = str(input_data.get("label") or "") if "label" in input_data else None
        identity = rotate_identity(store_path=settings.store_path, label=label)
    else:
        identity = load_or_create_identity(store_path=settings.store_path, label=str(input_data.get("label") or ""))
    return ok({"identity": identity.as_dict(), "p2p": settings.as_dict()})
