# Pack updates with a fixed Launcher and Host data owner

Signed Normal Packs and compatible successors of existing bundled Host
Extensions can be admitted as immutable external revisions. Importing a Pack
does not activate it. A Profile selects an exact artifact digest, then resolves,
reviews, approves, and activates that revision through the existing ceremony.
Neither operation replaces the Launcher executable or the registered VM.

The separate [UI asset producer](ui-asset-pack-authoring.md) stages unsigned
renderer inputs for separate signing, keeping the protected Defaults backend. Its
initial Native enrollment prerequisite and browser authority are documented
separately from ordinary executable Pack updates.

## Host Extension update boundary

A Host Extension successor must retain its Pack identity, publisher lineage,
Contracts and schemas, operation effects and limits, exported functions,
dependency edges, capabilities, network and secret requirements, Host
contribution declarations, and execution variants. Only executable bytes,
their resulting content identities, and a newer semantic version may change.
Normal Packs cannot become Host Extensions. New Host Extensions, Base Packs,
and Shell replacements are outside this update path.

For implementation-only successors, use
`core_runtime.host_extension_authoring.build_host_extension_successor` or
`scripts.stage_host_extension_successor_v4`. Supply an exact predecessor
artifact digest, a newer version and captured replacement bytes for existing
runtime paths. The producer preserves every individual Contract, including its
historical provenance, and checks the complete authority closure. Regenerating
Contracts from a changed whole-Pack source record can change that provenance
and is correctly rejected by compatibility validation. The producer writes
unsigned inputs; normal signing, Native authorization, admission and Profile
activation remain separate.

The publisher signature is verified under Host-owned policy. The first
publisher-key enrollment uses the separate public-key picker and Native trust
confirmation; a publisher name in a manifest is not authentication. Existing
publisher keys and revocations remain authoritative for subsequent updates.
Native confirmation binds the complete reviewed signed envelope, public key,
source selection, and artifact digest through policy writing and CAS admission.
Changed source fails closed rather than installing a different signed build.

## Native bootstrap support

The installed Launcher must recognize successor identities as well as the Host
runtime. Admission alone cannot make an older Launcher accept a different
artifact for an existing bundled Pack ID. The Native bootstrap supports
identity-only deferral for sandboxed provider successors and the compatible
Gateway and Provider Adapter Host Extension slots. It still verifies the signed
bundled predecessor,
the exact Profile, Lock and Plan pins, and the selected execution boundary.
Normal successors may use PackVM or WebAssembly; they cannot become Host
Extensions or remote providers through this path. Base, Shell, Application,
launch providers and Base backend providers remain protected.

Deferral does not load successor code or grant execution authority. The Host
must verify the signed admitted CAS, current activation and complete authority
graph. Native then requires the authenticated runtime identity to match the
selected identity before publishing the Host contract and again at readiness.
Rebuilding a development executable to obtain this initial bootstrap support
can change its executable-scoped data owner; it is not a credential-preserving
Pack update or a recovery step.

For the initial local macOS development upgrade, preserve the previous
Developer `.app`, stop its managed runtime, and start the new development
Launcher with `--tobkiri-continue-development-owner`. Native pickers select the
existing build folder (the parent of `user_data`) and the preserved previous
app. Native verifies both executable identities, the origin SHA namespace,
unredirected filesystem ownership, and exited managed processes before showing
the exact continuation review. It checks those facts again after confirmation,
before creating Host directories or publishing broker state. The existing
VM registration, Authority, credentials, conversations and consumed generation
budget remain in place; no private state is copied into a fresh owner.

This ad-hoc development continuation is explicitly **for the current Launcher
launch only**. Consent stays in Native memory, bound to this executable and
process lifetime. Pack activation does not rebuild or restart Launcher, so it
does not repeat this initial review. Restarting the ad-hoc Launcher requires a
fresh continuation review. An unsigned file or a legacy Keychain item cannot
authorize automatic reuse. Persistent development-owner authorization needs a
separately provisioned signing and OS-access design; it is not implemented by
this session-only path. Normal installed builds retain their existing stable
application data namespace.

Host compatibility is reverified on catalog reads. A Host Extension revision
uses a distinct authenticated catalog marker and must be explicitly pinned.
The running Profile keeps the bundled or previously selected revision until
the successor activation completes. Production capture uses the selected CAS
root, exact artifact principals, and exact contribution/domain bindings.

## Update sequence

1. In **Packs**, choose **Trust and add signed Pack**, select the signed Pack
   folder and its separate publisher public key, and review the Native prompt.
2. In **Profile → Pack versions**, select the admitted revision and save the
   version. This changes the Profile definition using a compare-and-swap check.
3. Resolve, review, approve, and activate the candidate. Approval remains
   separate from installation and publisher trust.

Admission can make an older catalog capture stale. Verified Profile recapture
or restarting the same installed executable refreshes that capture. Rebuilding
Launcher, replacing VM registration, and re-entering Provider keys are not
catalog recovery steps.

## Persistence and storage

Keep the same Host data root, Authority store, Profile, Provider connection,
encrypted credential owner, and both generation-budget stores. An update must
not reset consumed requests or broaden the approved model/price policy.
Conversations and persistent VM registration, base image, and attestation key
remain under their existing owners.

Per-invocation VM domains are temporary. The existing dead-owner reaper may
remove abandoned domains after checking their owner and safe private root;
it does not delete registered VM storage or user workspaces. APFS clones share
blocks, so measure reclaimed capacity with filesystem free space rather than
adding apparent raw-image sizes. Capacity checks remain enforced.

### Retaining an already active bundled Host Extension

A development checkout can advance while an active Profile still pins an older
bundled Host Extension. Host-owned retention preserves those exact old bytes;
it does not enroll a publisher or admit, select, approve, or activate a revision.
The origin comes from normal full active capture, including its Authority
reservation, epoch, fence, selected Shell, and final pointer checks.

The authenticated Plan's raw bundle-lock digest anchors the retained lock,
projection, source manifest, canonical source record, and exact finite artifact
inventory. A matching file path or an artifact index's own seal is insufficient.
Retention rejects extra files, directories, changed roles, symlinks, and owner
or publication races. Verification repeats the complete byte chain and current
activation checks before retained data can leave a read or materialization.

Only the exact active pin can use this store. Discovery without a pin and new
candidate resolution retain the current Source baseline. Signed external CAS
verification keeps priority; a missing, unsafe, or invalid canonical Source
root cannot be hidden by retention. Reading does not copy or publish artifacts,
although normal Authority-store initialization and activation recovery remain
part of canonical active capture.

An origin is bound to its activation and bundle digest. It expires when that
binding changes; an old container cannot be renamed into a new bundle's origin.
This mechanism cannot bypass Shell reconfirmation or make a partial predecessor
read into execution authority. Keep the old active plan until the ordinary
signed-successor ceremony has completed.

### Reading an admitted successor after Source advances

An active signed Host Extension successor must be revalidated against its
authenticated bundled baseline. A newer development Source version is not a
replacement for that baseline. `bundled_host_baseline_v4` provides a separate
read scope and immutable public-byte cache for this case.

The Host-only `retain_bundled_host_baseline` port accepts a normally issued
active origin and explicit, untrusted bundle, full-Pack and canonical-record
suppliers. It verifies the origin's raw bundle-lock digest, projection, full
manifest, source-record identity, complete quartet, inventory roles and every
runtime byte before exclusive publication. Bounded reads validate each file's
hash and cumulative size before retaining the next file. Foreign owners,
changed pointers, epochs, fences, source bytes or closed scopes are rejected.

These baseline bytes supply only the strict compatibility comparison. The
selected signed CAS root and active artifact pin remain unchanged. Neither
publication nor subsequent reads admit, approve or activate a Pack. New
admission explicitly clears the ambient baseline read context and retains its
current canonical Source checks. Consequently, a Source-only development
baseline repair does not by itself establish that the next signed candidate
can be admitted against a changed canonical Source boundary.

### Updating a currently active admitted Host Extension

`existing_host_extension_admission_v4.admit_signed_existing_host_extension_version`
provides a separate Host-owned port for a currently active, already admitted
Host Extension slot. Its normally issued Origin binds the owner, active pointer,
bundle, epoch and fence. The request owns an explicitly issued proof; an ambient
baseline read scope is insufficient. Unknown IDs, bundled-only slots and initial
renderer enrollment cannot use this port.

The candidate must preserve the complete authority closure of both the
authenticated frozen bundled baseline and the selected admitted predecessor.
Its version must exceed every compatible authenticated external revision, and
an existing version cannot acquire different bytes. Signature verification,
exact publisher key/version authorization and the Native review remain required.
The existing Native preview and confirmation fields are unchanged. Native
recaptures the proof at commit and binds the frozen baseline, active predecessor
and external revision through the opaque review digest.

Generic admission still clears the ambient read context and uses current
canonical Source. Neither admission port activates a revision. After publication,
an error can require reconciliation: a committed catalog cannot be described as
rolled back merely because later verification failed. The Native response states
that admission committed and reports the verification failure; it does not invite
an automatic resubmission.

Runtime `catalog.read`, `dashboard.read` and `pack.status` keep their exact root
and baseline read scopes open through projection. Physical-root, active Profile,
mutable control-state and approval checks complete before results are returned.
Mutation and restart operations do not borrow these read scopes.

## Development and verification

This capability requires Host runtime support. An explicit development
Launcher can load reviewed core Python changes from its source checkout while
keeping the same executable and Host data owner. That initial runtime repair
is not itself a Pack update. Fully sealed releases must include this support
through their authenticated release process; never patch a sealed stage or
relax its inventory checks. Keep bundled Pack bytes and catalog pins unchanged
when preparing an independently signed successor.

`test_host_extension_dispatch_update_v4.py` composes real signing, Native
admission, CAS selection, Profile activation, and production factory loading
with the same Authority and runtime owners. It does not prove a live UI update,
an actual VM boot, or a successful paid model conversation; those require
separate acceptance evidence.

`test_normal_packvm_production_update_v4.py` is an opt-in macOS test for a
signed Normal Python Pack through the registered production PackVM substrate.
It checks retained data and protected pins, then invokes the selected successor
with authenticated artifact and implementation identities. Set
`TOBKIRI_RUN_NATIVE_PACKVM_ACCEPTANCE=1` only on a provisioned test Host. Missing
native readiness fails an opted-in run. This test does not replace the registered
base VM or stop an unrelated live owner. The separate production Wasm test may
skip on platforms without its required hard resource boundary; a skip is not
evidence that a Pack was invoked.
