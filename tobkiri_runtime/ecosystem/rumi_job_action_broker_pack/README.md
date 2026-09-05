# Rumi Job Action Broker Pack

Maps an exact `action_id` to one selected `rumi.action.job.adapter.v1`
provider by its manifest `instance_key`. It never scans installed packages or
imports target implementations. A profile-scoped ledger rejects idempotency-key
payload changes and returns recorded results for safe replay.

