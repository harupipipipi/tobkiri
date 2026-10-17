# Native CLI edits to an existing Profile

```text
tobkiri profile apply --profile PROFILE_ID --definition EDITED_DEFINITION.json --expected-head sha256:CURRENT_64_LOWERCASE_HEX_DIGITS
```

All three arguments are required. The digest and target ID are validated by
the argument parser before Core opens DATA. The definition is a bounded StrictV1
`tobkiri.profile/1` or `tobkiri.published-profile/1` document. A full Profile must
have exactly the target ID; apply never replaces or lowercases that identity.
A publication has no ID/name, so its structural fields are materialized with the
existing target's ID and current name. Publication fields cannot carry approval
records, secrets or private data. Unknown fields and duplicate keys are rejected.

The CLI reads the definition once and rejects a foreign identity before Core
opens. It loads the existing Profile and compares the expected head before
resolving Pack sources, verifying new artifacts or requesting edit approvals.
A missing Profile is an error. Relative local Pack paths resolve beside the
definition using the same resolver as create/import; the stored revision uses
absolute local locators. The definition never supplies a trusted approval.

The only write entry is `Core::save_profile(proposed, Some(expected_head))`.
Core independently verifies Pack hashes/WIT, compiles the Profile, requests normal
trusted approval for added Pack permissions and for the new revision's User
permissions, then uses the existing process-shared head CAS. Non-interactive
operation continues to fail if a trusted prompt is required. No new Profile,
database patch, approval automation, private-data copy or secret operation is
used. A concurrent edit can still win after the precheck; the final CAS rejects
that conflict and apply does not reload or retry automatically. Core's normal
recovery and immutable revision/approval records can exist before a rejected
final CAS; a failed apply does not imply that all underlying history was erased.

After success the CLI emits one StrictV1 JSON receipt containing only:

- `profile`: the unchanged target ID.
- `applied_head`: the digest returned by the committed Core save.
- `observed_head` and `observed_epoch`: one subsequent locked Core snapshot.
- `applied_revision_is_current`: whether that snapshot still names the applied
  revision. If another update already won, the observed epoch belongs to that
  later head, and is not presented as the epoch of this apply.

Reading this receipt does not start a Session or guest. If the save commits but
the head/epoch snapshot fails, the command reports that the update **committed**
and includes its applied digest in the error. Reload the actual state before a
further edit; there is no rollback or implicit retry.

## Verification scope

Portable tests cover required typed arguments, malformed/stale-looking digests,
foreign-ID rejection before DATA exists, publication identity/name materialization,
policy preservation, relative locator resolution and post-commit snapshot reporting.
Windows native tests use actual secure DATA, verified local data artifacts, normal
Core saves, and the existing trusted-host fixture pattern. They cover same-ID
head/epoch changes, published edits, stale heads before source/approval work,
denied Pack/User permission additions, invalid artifact pins and missing targets.
They do not execute a model, external tool, Vault, network or production Profile.

Root owns Cargo/native execution for this workstream. Source fixtures alone do
not constitute a test-pass receipt or activation of a production Profile.
