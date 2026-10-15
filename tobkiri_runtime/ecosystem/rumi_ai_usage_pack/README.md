# Rumi AI Usage Pack

This pack owns provider-neutral token estimates and usage-cost normalization.
The bundled tokenizer explicitly reports an estimate; it never presents the
result as an exact provider tokenizer count. Cost remains unknown unless both
usage sides and both catalog rates are known. Currency and pricing revision are
carried with the result.

Validation was not executed by the implementation agent. Independent testing
is required before merge, including unknown values, malformed usage, overflow,
pricing revision, deterministic estimates, and pack replacement/removal.

