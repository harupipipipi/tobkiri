# Tobkiri Model Catalog Pack

This Pack owns the declarative provider and model catalogs used by Tobkiri
Defaults. It verifies the resource digest before returning routing descriptors.
The bounded public OpenRouter inventory remains authoritative when explicitly
queried; stale snapshots never become fresh execution capability evidence.

`catalog/provider-setup.json` contains hosted endpoint presets and the complete
model choices imported by `scripts/sync_provider_model_catalog.py`. Sources,
retrieval dates and input digests are recorded in the JSON. models.dev data is
MIT licensed; the full notice is in `catalog/licenses/models.dev-MIT.txt`.
The Defaults UI projection is generated from this file. No startup or build
network request is needed to display provider or model choices.

The JSON contains no credentials. A catalog choice does not assert account
access, Provider health, tool capability trust or execution approval.
