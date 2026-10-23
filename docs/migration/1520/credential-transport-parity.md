# Original credential transport behavior to preserve

The original Launcher Setup, dedicated approval window, provider settings and
complete chat remain the product flow. This document records existing behavior
that must survive the Rust host migration; it does not establish implementation
or runtime acceptance. The private credential snapshot candidate and current
Rust execution budgets do not establish this parity.

Issue #1520 R1.1/R1.2 keeps Pack identities and domain policy out of Core and
the host. R11.2 first moves existing Packs to Python-on-Wasm. Provider, model,
token and price policy belongs to the migrated Packs. Any missing Core primitive
must be generic and remain within the listed mechanisms, including `secrets`,
Gate, Flow, storage and audit.

## Authenticated material resolution

The original [material store](../../../tobkiri_runtime/tobkiri_host/credential_store.py)
checks the record MAC before resolving material. Resolution preserves all of:

| Existing check | Required migration behavior |
| --- | --- |
| Profile, consumer and provider instance | Compare actual authenticated execution/transport identity with the stored record. Payload identity is not proof. |
| Scope and resource binding | Preserve the original comparison and optional-value semantics. |
| Key version and purpose | Preserve the original requiredness, defaults and comparison rules. |
| Credential expiry | Preserve the original finite Unix timestamp and `<= now` rejection boundary. Absence means no credential expiry; migration must not restart it. |
| Required generation marker | Compare the complete normalized required and supplied markers, including absence versus presence. |

The marker is absent or contains exactly `enrollment_id` and `policy_digest`.
Preserve the original identifier coercion, trimming and bounds, strict string
digest validation, Python hexadecimal parsing and exact case-sensitive equality.
The policy digest covers the original canonical full policy representation,
including its legacy omission of zero-valued price fields.

The supplied marker originates from authenticated Host admission. Direct
`CredentialBrokerService.invoke("resolve", ...)` remains denied. A guest,
Broker payload, opaque handle or copied marker is not authenticated admission.

## Approval, send and lifetime

The original [native enrollment](../../../tobkiri_runtime/core_runtime/native_generation_budget.py)
checks dispatched configure/manage ancestry, the exact frozen plan and payload,
and the actual interactive approval before creating the enrollment. Independently
persisted authority and witness records must agree; missing or partial records
deny rather than silently disabling the required admission after restart.

The [transport budget](../../../tobkiri_runtime/core_runtime/generation_transport_budget.py)
binds Profile, consumer, provider, credential, key version, purpose, scope and
the exact endpoint/model/token/price policy. Its durable attempt is consumed
before send, remains spent after failure or an unknown outcome, denies duplicate
request attempts and persists across restart and the credential lifetime.

The actual [credential transport](../../../tobkiri_runtime/core_runtime/credential_transport.py)
validates the dispatched lease and its identity, activation, epoch, execution
domain, boot epoch and request binding. It rechecks liveness after reserving an
attempt. Internal retries and redirects must not bypass these conditions.

Credential revocation removes the encrypted record and denies future material
resolution. Already resolved in-flight material remains subject to the original
transport liveness and fencing checks. Preserve this boundary rather than
inventing a different revocation guarantee.

## Native integration gap

`CallCx` and the consumed `DispatchPermit` authenticate execution identity and
liveness. `AllocationFrameScope` authenticates registered origin and funding.
`BudgetContext` accounts for execution cost, iteration and deadlines; its API
explicitly does not prove compiled origin or authority. None provides the
original persistent enrollment, independent policy witness or lifetime attempt
ledger by itself.

The original Gate now retains descriptive `TrustedApprovalEvidence` on its
Grant, consumed permit and funded execution-entry evidence. It commits to the
complete typed prompt and records whether Gate accepted the trusted prompt
answer or verified a device attestation, including the original risk. An
unprompted effect has no such evidence, even after an interactive permission
installation. The snapshot hash is streamed without making another prompt or
canonical-value copy; retained metadata is inline and included in the existing
`sizeof`-based control inventories. Approval, consumption, cancellation, epoch
and argument checks retain their original behavior.

Core stores the existing entry, context, frozen-code metadata and single
lifecycle mutex in a funded invocation record. Native task, operation and
input owners remain in the actual reached state. Retaining or upgrading a
record reference does not own an execution guard or confer execution
authority. The state, record and funding blocks are prepaid together, and
the same original reservation remains charged until the last strong or weak
record carrier releases its physical storage. This separation supplies no
authenticated Pack-facing API by itself.

Private record links now retain the actual parent from Core's authenticated
dispatch objects. The links hold metadata strongly so a surviving child can
inspect the original entry after its parent's native execution retires. They
do not hold native execution owners or restore liveness. Flow continuations
and overlays preserve their entry's actual parent rather than inventing
intermediate dispatches; a completed native-input receipt is not a parent
invocation. Inline links are covered by the original size-based reservations.
Final-owner chains release iteratively, with each original reservation held
through payload destruction and any remaining weak allocation.

This description is not itself a live invocation or parent-chain proof, a GUI
surface identity, a durable enrollment or a Pack policy digest. Core privately
holds the actual leaf Grant and arguments. A fixed native Gate inspection now
authenticates an existing registered allocation scope, locks Core's original
admission and node mutexes, and uses a stack `TypedReachedInvocation` proof
borrowing those same guards. It returns an opaque, prepaid metadata reference.
Views and parent selection freshly check the same original live leaf, factory,
context, epoch, entry deadline and terminal state. Selecting an ancestor keeps
the original leaf binding; the historical ancestor need not still execute.
No caller-selected Grant, parent ID or serialized evidence is accepted. The
reference retains only metadata and its original funding, not native owners.

This native read-only API is not yet exposed through a Wasm host resource or
used by the Broker. It grants no effect authority. Secrets/network/storage must
independently retain their ordinary permissions and perform evidence
revalidation atomically with their actual admission; reading a view and later
submitting its JSON or Grant ID cannot supply that check. That effect-side
integration and the original durable enrollment/policy/witness semantics remain
open. Retaining historical metadata never extends authority or validity.

Keep these obligations open until the production create/use/revoke and
compensation path uses the same backend and passes original GUI-to-chat,
failure/unknown, expiry, revocation and restart cases. Do not infer completion
from a material-store unit test, decoded Registry adapter or opaque-header test.
