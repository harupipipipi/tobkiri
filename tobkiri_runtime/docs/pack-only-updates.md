# Pack updates with a fixed Launcher and Host data owner

Signed Normal Packs and compatible successors of existing bundled Host
Extensions can be admitted as immutable external revisions. Importing a Pack
does not activate it. A Profile selects an exact artifact digest, then resolves,
reviews, approves, and activates that revision through the existing ceremony.
Neither operation replaces the Launcher executable or the registered VM.

## Host Extension update boundary

A Host Extension successor must retain its Pack identity, publisher lineage,
Contracts and schemas, operation effects and limits, exported functions,
dependency edges, capabilities, network and secret requirements, Host
contribution declarations, and execution variants. Only executable bytes,
their resulting content identities, and a newer semantic version may change.
Normal Packs cannot become Host Extensions. New Host Extensions, Base Packs,
and Shell replacements are outside this update path.

The publisher signature is verified under Host-owned policy. The first
publisher-key enrollment uses the separate public-key picker and Native trust
confirmation; a publisher name in a manifest is not authentication. Existing
publisher keys and revocations remain authoritative for subsequent updates.
Native confirmation binds the complete reviewed signed envelope, public key,
source selection, and artifact digest through policy writing and CAS admission.
Changed source fails closed rather than installing a different signed build.

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
