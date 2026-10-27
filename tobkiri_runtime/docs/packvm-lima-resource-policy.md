# PackVM Lima resource policy

The production macOS PackVM uses a fixed 4 GiB sparse primary disk. This is a
virtual growth ceiling, not an up-front 4 GiB allocation, but provisioning still
reserves enough host capacity for the disk to reach that ceiling. A Pack must
not be able to turn sparse allocation into an out-of-space failure elsewhere on
the host.

## Evidence and minimum

The pinned 2026-08-07 Ubuntu Jammy arm64 and amd64 qcow2 headers both declare a
2,361,393,152-byte (2.199 GiB) virtual image. Their downloads are 703,594,496
and 734,327,808 bytes respectively. Lima 2.2's VZ path converts qcow2 to a raw
sparse disk, rejects a configured size below the original virtual image, and
does not support shrinking an existing primary disk.

The enforced minimum is the sum of these independently bounded allocations:

| Allocation | Bound |
| --- | ---: |
| Pinned base-image virtual size | 2,361,393,152 bytes |
| Retained digest-addressed artifacts | 768 MiB |
| Free space preserved during materialization | 512 MiB |
| OS, supervisor, and log growth budget | 512 MiB |
| Calculated minimum | 3.949 GiB |
| Configured sparse ceiling | 4 GiB |

Each materialization remains limited to 512 MiB. The guest supervisor also
measures all retained artifacts without following symlinks, refuses cumulative
storage above 768 MiB, and refuses a new write unless its content, bounded
metadata allowance, and the 512 MiB free-space reserve all fit. Later OS or log
growth can therefore make materialization fail closed; it cannot silently spend
the reserve.

## Host preflight

Immediately before `limactl start`, the Host checks the filesystem containing
the user's Lima home. The required free capacity is:

```text
4 GiB disk growth ceiling + 512 MiB host reserve
    + 2.199 GiB qcow2-to-raw conversion + required image download
```

That is 7,896,825,856 bytes (7.35 GiB) for a fresh arm64 download and
7,927,559,168 bytes (7.38 GiB) for a fresh amd64 download. If only the exact
pinned qcow2 source is already cached, the requirement is 7,193,231,360 bytes
(6.70 GiB).
Provisioning fails before creating or starting an instance when this capacity is
not available or cannot be measured.

A source-cache hit is recognized only at Lima's URL-keyed cache location when every
directory and file is regular, local-user-owned state, the recorded source URL
is exact, the byte length is exact, and the cached data hashes to the pinned
SHA-256 digest. The same check is repeated when the reviewed plan is consumed;
a changed or removed entry invalidates the ceremony.

Lima 2.2 retains a VZ-compatible `imgconv/raw` next to the qcow2. On cache reuse,
Lima compares that raw image with its adjacent digest metadata, but that metadata
does not independently prove that the raw bytes are the conversion of Tobkiri's
pinned qcow2. Tobkiri therefore rejects a pre-existing converted raw cache entry.
A verified source-only hit is safe because Lima must create the conversion from
the qcow2 bytes Tobkiri just hashed. Shared, legacy, URL-only,
digest-metadata-only, and pre-converted cache entries are not trusted.

The previous `rumi-managed-runtime` instance is not resized, deleted, or adopted.
The policy applies only to the dedicated `tobkiri-packvm-v4` lifecycle. Existing
disks are never shrunk in place.

## Destructive identity binding

Stop and delete re-authenticate the Host attestation or failed-provision recovery
proof immediately before each destructive `limactl` call. Verification covers
the canonical non-symlink dedicated `LIMA_HOME`, its filesystem identity, the
fixed instance directory identity, the current Lima config, pinned image and
Host inputs, and—while the guest is running—the machine ID, installed supervisor
digest, and supervisor challenge. A replaced same-name instance is classified as
foreign/orphaned and is not mutated. The user's default `~/.lima` namespace is
never a command target.

Lima's destructive interface accepts an instance name rather than an open
directory descriptor, so another local process with access to the dedicated Lima
home could still race in the interval after final verification and before Lima
resolves that name. Tobkiri minimizes this unavoidable external race by using the
verified executable, fixed instance name, and minimal environment pinned to the
dedicated `LIMA_HOME`; failed-provision stop/delete also re-verifies between the
two commands. The final GUI validation must still exercise this behavior against
the supported real Lima build.

## Low-space hosts

A host with only 1-2 GiB free cannot safely provision this image. The pinned
base image alone has a 2.199 GiB virtual size, and VZ conversion may coexist with
the compressed download. Lowering the disk below the enforced minimum merely to
run an end-to-end test would remove the artifact, reserve, or system-growth
budget and is not a supported tier.
