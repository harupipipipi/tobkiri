"""Fixed CI schema-call checkpoints, staged only in the diagnostic artifact.

The producer fills the closed tag inventory from authenticated original DATA.
This helper neither opens resources nor reports Guest fuel/authority claims.
"""

import ordered_json_v2 as wire


_SCHEMA_TAGS = ()  # Filled only by the explicit diagnostic source derivation.
_TAG_IDS = {name: index for index, (_kind, name) in enumerate(_SCHEMA_TAGS)}
_STAGES = ("check-schema-start", "check-schema-end", "schema-store-entry")
_sequence = 0


def checkpoint(stage: str, schema_name: str | None = None) -> None:
    """Emit one bounded event at the actual callsite; failures are not ignored."""
    global _sequence
    if type(stage) is not str or stage not in _STAGES:
        raise RuntimeError("unknown schema checkpoint stage")
    if stage == "schema-store-entry":
        if schema_name is not None:
            raise RuntimeError("schema-store checkpoint must have no tag")
        tag = None
    else:
        if type(schema_name) is not str or schema_name not in _TAG_IDS:
            raise RuntimeError("schema checkpoint name is outside its closed inventory")
        tag = _TAG_IDS[schema_name]
    if type(_sequence) is not int or not 0 <= _sequence <= 140:
        raise RuntimeError("schema checkpoint sequence exceeds its fixed bound")
    # Saturate after one overflow notification, including failed-schema retries.
    if _sequence == 140:
        return
    _sequence += 1
    from wit_world.imports.bindings import inspect

    # Only fixed strings, a bounded sequence and a declared numeric tag cross
    # the import. Its acknowledgment is deliberately not a validation result.
    inspect(wire.dumps({
        "format": "io.tobkiri.native-schema-checkpoint.v1",
        "sequence": _sequence, "stage": stage, "schema_tag": tag,
    }))
