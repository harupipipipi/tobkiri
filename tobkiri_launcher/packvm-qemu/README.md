# Portable PackVM distribution

The Linux target is an **amd64 QEMU/KVM VM** with q35/UEFI, a private guest
disk, a dedicated virtio-serial agent channel, and no guest NIC or host folder
mount. The guest agent still checks artifact identities and signatures and
runs Pack code under its own bubblewrap/seccomp policy inside the VM.

## End-user installation goal

The Tobkiri installer must supply the VM runtime, firmware, guest image, and
guest dependencies. Users should not install Docker Desktop, QEMU, Python,
or bubblewrap separately. A static QEMU build is a **release engineering
input**, not an instruction for end users to compile or install software.

Hardware requirements cannot be bundled: Linux needs supported CPU
virtualization, KVM enabled by its kernel, and usable `/dev/kvm` permissions.
Some hosts need an administrator to enable virtualization, grant the correct
device access, or reboot. Containers and VMs may not expose nested KVM at all.
An installer must diagnose these conditions and ask before privileged changes.
It must not silently use TCG, the host Python interpreter, Docker, or a native
shared-kernel sandbox when hardware virtualization is unavailable.

**Current delivery scope:** the builders stage digest-pinned local inputs and
the Launcher release build copies their entire verified closure into its normal
resource tree. The manifest digest is compiled into Launcher, then passed through
the sealed role scope and independently checked against the outer resource
inventory. This code does not by itself produce vetted third-party binaries,
install system packages, grant KVM access, or sign an installer. Actual vendor
artifacts and clean-machine installer/VM acceptance remain release gates.

The Windows profile uses QEMU/WHPX and the same amd64 guest. Its builder is
`tobkiri_runtime/scripts/build_windows_packvm_bundle.py`, with the same arguments
and repeated `--qemu-dll NAME LOCAL_PATH sha256:HEX` entries for every bundled
DLL. It checks PE32+ amd64 normal and delay imports, including transitive imports,
against adjacent bundled DLLs and a fixed Windows system/API-set allowlist.
It never assumes the user has compiler runtimes or QEMU installed. WHPX must be
available in Windows; enabling its optional feature can require administrator
approval and reboot. Hardware virtualization can also require firmware setup.

### Launcher build integration

Release engineering supplies `TOBKIRI_PACKVM_BUNDLE_ROOT` and a separately
expected `TOBKIRI_PACKVM_MANIFEST_SHA256` **to the build**, not to end users.
`build.rs` rejects absent or incomplete inputs, verifies every file and the
closed inventory, copies the bundle to `gen/app/packvm-qemu`, and embeds its
manifest digest. Tauri's existing `gen/app` resource mapping includes all these
files in the installer. Runtime environment variables cannot substitute another
manifest or bundle for this compiled identity.

The sealed bootstrap binds portable resources to the same outer application
inventory as Python. Linux checks the running Launcher's installation identity
and ownership, rejects installation directories writable by another user, and
uses descriptor-anchored verified read-only snapshots with lifetime-bound
cleanup. This protects against corrupt updates and cross-user/path replacement;
arbitrary malicious code already running as the same user remains outside that
boundary. The historical `linux-immutable-package-v1` label does not claim that
user-owned files are immutable to their owner. Windows has a separate
Authenticode/ACL/held-handle boundary and requires the release build's expected
signer certificate digest; vendor hashes cannot replace that caller identity.

## Reproducible staging contract

Run `tobkiri_runtime/scripts/build_linux_packvm_bundle.py --help` from the
repository root. Supply an existing absolute runtime root, a new output
directory, an uncredentialed HTTPS image provenance URL, and these local inputs
with a separately expected `sha256:<64 lowercase hex>` digest for each:

- `--qemu` / `--qemu-sha256`: static little-endian amd64 ELF QEMU
- `--firmware-code` / `--firmware-code-sha256`: matching read-only OVMF code
- `--firmware-vars` / `--firmware-vars-sha256`: matching clean OVMF variables
- `--image` / `--image-sha256`: reviewed amd64 raw guest image
- `--config` / `--config-sha256`: this directory's cloud-init template
- `--bubblewrap` / `--bubblewrap-sha256`: reviewed amd64 Debian bubblewrap package
- `--bubblewrap-descriptor` / `--bubblewrap-descriptor-sha256`: its descriptor
- `--expected-agent-sha256`: the separately reviewed digest of the deterministic
  guest zipapp generated from the exact source revision by
  `tobkiri_runtime/scripts/build_packvm_guest_bundle.py`

The builder has no default third-party digests. Do not invent them or treat
hashing an untrusted download as provenance verification. Verify upstream
release signatures or signed distribution metadata first, record the exact
version/source/build recipe, and supply those verified identities. Input files
must be regular single-link files without symlink traversal. The output is
published only after all input hashes agree. No supplied executable is run.

The bubblewrap descriptor has exactly:

```json
{
  "schema": "io.tobkiri.packvm-qemu-bubblewrap-descriptor.v1",
  "package": "bubblewrap",
  "version": "0.12.0-1~deb13u1",
  "architecture": "amd64",
  "source": {
    "url": "https://deb.debian.org/debian/pool/main/b/bubblewrap/bubblewrap_0.12.0-1~deb13u1_amd64.deb",
    "sha256": "REPLACE_WITH_VERIFIED_PACKAGE_DIGEST",
    "size_bytes": 0
  }
}
```

The placeholders above deliberately fail validation. Replace them with the
verified package identity and size, then pin the descriptor itself separately.

The manifest is `packvm-qemu-provisioning.v1.json`, schema
`io.tobkiri.packvm-qemu-provisioning.v1`. Its exact top-level fields are
`schema`, `architecture` (`amd64`), `accelerator` (`kvm`), `files`,
`image_source`, and `qemu_dependencies`. Each `files` entry has only `path`,
`sha256`, and `size_bytes`:

| Slot | Relative path |
| --- | --- |
| qemu | bin/qemu-system-x86_64 |
| firmware_code | firmware/OVMF_CODE.fd |
| firmware_vars | firmware/OVMF_VARS.fd |
| image | images/packvm-amd64.raw |
| agent | provisioning/runner.py |
| config | provisioning/cloud_init_template.yaml |
| service | provisioning/guest_service_template.v1.json |
| bubblewrap | provisioning/bubblewrap_amd64.deb |
| bubblewrap_descriptor | provisioning/bubblewrap_descriptor.v1.json |

The generated service launches the guest runner with `--serve-virtio-serial`;
the channel's fixed guest path is
`/dev/virtio-ports/io.tobkiri.packvm.agent`. The historical wire protocol string
`io.tobkiri.macos-vz-supervisor.v1` is retained for compatibility and does not
identify the new substrate as macOS. Per-domain key material is generated only
by the runtime; it is never part of this immutable bundle.

### Executable dependency closure

The Linux profile rejects ELF `PT_INTERP`, `DT_NEEDED`, and dynamic search paths and
emits `qemu_dependencies: []`. A typical distro QEMU binary is dynamic and is
therefore **not accepted**. Copying that binary and a few libraries would not
create a self-contained distribution: an absolute ELF interpreter, transitive
libraries, modules, and firmware could still resolve outside the bundle.

Release engineering must build and review static, headless QEMU with modules
disabled and only the necessary target/devices. ELF inspection does not prove
that a binary is QEMU, identify build flags, rule out every runtime `dlopen`, or
certify compatibility. Pinned provenance, an audited build recipe and a real
VM boot in the final installer environment remain necessary. The fixed launch
must select the bundled firmware explicitly and avoid ambient ROM/module/data
lookup. Dynamic closure/loader relocation requires a separate reviewed design.

### Release gates still required

1. Reproducible QEMU/firmware/guest builds, provenance, license notices, source
   availability, vulnerability maintenance and an SBOM
2. End-to-end installer validation of the implemented build-pinned sealed
   bindings, including the platform's caller identity and permission checks
3. Installer staging of the entire closure and capacity reporting for downloads,
   unpacking, the base image, per-domain writable disks, and rollback
4. Real KVM tests from a clean installation: guest boot, signed challenges,
   artifact tampering, no-NIC/no-host-mount policy, broker continuations,
   cancellation, process ownership, teardown, crash recovery, and residual-zero
   resource cleanup
5. Clear unavailable-state UX for absent KVM, denied KVM access, unsupported
   architecture, incomplete assets, insufficient disk, and corrupted files

Passing packaging unit tests does not satisfy these gates or prove a VM boot.
The existing published desktop/release job matrix is still macOS-only until
portable vendor-asset production and native installer jobs are added and pass.

## Choice of substrate and package size

[QEMU's security policy](https://www.qemu.org/docs/master/system/security.html)
supports hardware virtualization with approved machine types, including q35.
It explicitly excludes TCG from guest-isolation security guarantees. TCG could
be a separately identified development/conformance aid, not an automatic
production fallback. KVM avoids software CPU translation; no Tobkiri workload
latency or boot-time claim has been measured here.

[Firecracker](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md)
also requires KVM and recommends its jailer in production. It is a viable
future specialized microVM backend, but needs a direct-kernel/rootfs boot
pipeline and its own supervisor/jailer lifecycle. It does not solve no-KVM
hosts. QEMU matches this repository's existing EFI/cloud-init guest approach.

For scale, [Debian's amd64 QEMU system package](https://packages.debian.org/trixie/qemu-system-x86)
was about 10 MB compressed and 64 MB installed when reviewed, **excluding** its
many dependencies, firmware, and guest image. Those are not size estimates for
Tobkiri's static build. The existing macOS raw guest is 3 GiB; the Linux image's
actual packaged/download/expanded sizes must be measured. Do not advertise a
small one-file VM without including the image and writable storage budget.

Native [bubblewrap](https://github.com/containers/bubblewrap), seccomp and
[Landlock](https://docs.kernel.org/userspace-api/landlock.html) can construct a
shared-kernel sandbox, with different host-kernel exposure and capability
requirements. Bubblewrap explicitly is not a complete ready-made policy.
Any such option needs a distinct mode and independent tests; it must not
inherit PackVM attestation or VM isolation claims. [gVisor's systrap platform](https://gvisor.dev/docs/architecture_guide/platforms/)
is a no-KVM userspace-kernel alternative, but requires separate OCI/rootless
integration and compatibility tests rather than reusing the VM label.

## Redistribution and security maintenance

QEMU is distributed under [GPL version 2, with component-specific licenses](https://www.qemu.org/docs/master/about/license.html).
Bundling its binary requires meeting the applicable license obligations,
including notices and corresponding-source distribution or an applicable
source offer. Static linking adds the linked components' license obligations.
Firmware, Linux, Debian packages and the guest filesystem have their own
licenses; audit the actual closure and ship its notices/source information.
Firecracker is [Apache-2.0](https://github.com/firecracker-microvm/firecracker/blob/main/LICENSE),
but that does not change the guest kernel/rootfs licensing requirements.

Use [official QEMU signed source releases](https://www.qemu.org/download/) or
authenticated distribution packages. Review signatures, build options and
security advisories; record upstream versions and supply updates through the
Tobkiri installer. Hash-pinning alone cannot establish authenticity or guarantee
that an old artifact is safe. This is an engineering compliance checklist;
release owners should review the exact distribution's licensing obligations.
