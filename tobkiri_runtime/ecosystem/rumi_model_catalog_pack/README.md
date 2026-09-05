# Rumi Model Catalog Pack

This pack is the authoritative declarative owner of the provider and model
catalog that previously lived under defaultspack. Wave 5 relocates the existing
catalog unchanged; it adds no provider and changes no model catalog entry.

The global catalog operation verifies the complete resource-tree digest before
returning normalized, provider-neutral routing descriptors. It never imports
provider execution code, reads credentials, probes remote services, or claims
remote availability. Execution adapter versions remain independent.

Validation was not executed by the implementation agent. Independent testing
is required before merge, including startup, complete catalog equivalence,
integrity rejection, pack removal, routing joins, and rollback.
