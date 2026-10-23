# Pinned Windows PackVM development inputs

This recipe feeds `build_pinned_windows_packvm_bundle.py`, which expands
`.github/toolchains/windows-packvm-assets.v1.json` into the existing strict
Windows bundle builder. No binary assets belong in Git. The lock is a reviewed
build input, **not a signature or release trust authority**.

## Reviewed inputs

- QEMU 11.1.0 amd64, installer `qemu-w64-setup-20260811.exe`, from the Windows
  supplier linked by <https://www.qemu.org/download/>. Its 206,615,928 bytes
  match the supplier's HTTPS SHA-512 checksum. The selected executable and
  its 104 transitive redistributable DLLs are individually SHA-256 pinned.
- Firmware comes from the same installer: `share/edk2-x86_64-code.fd`
  (3,653,632 bytes) and `share/edk2-i386-vars.fd` (540,672 bytes). The vendor's
  `share/firmware/60-edk2-x86_64.json` explicitly pairs them for x86_64 q35.
  Their total pflash capacity is 4 MiB. The i386 name of the variable template
  does not mean an architecture mismatch.
- Debian 13 generic amd64 cloud image `20260914-2601`, 3,221,225,472 raw bytes.
  The tar archive and the extracted `disk.raw` both match Debian's published
  HTTPS SHA512SUMS. This is a real GPT disk with an EFI System Partition,
  `EFI/BOOT/BOOTX64.EFI`, and an ext4 root. No image signature was verified:
  the attempted `SHA512SUMS.sign` URL returned 404.
- Bubblewrap `0.12.0-1~deb13u1`, amd64: 56,876 bytes, SHA-256
  `70aca4fa8daeacb677ec00e8063eb586f08ae3d94b1f11e684370b5524c43431`.
  Its digest was authenticated through Debian trixie's signed InRelease,
  Packages.xz SHA-256, and package entry using the installed Debian archive
  keyring. This signature verification does not authenticate the separate
  cloud image or QEMU installer.

The raw guest's actual `/var/lib/dpkg/status` was read offline. Python 3.13,
python3-cryptography, cloud-init, systemd, udev, libc6, libcap2 and libselinux1
all report `install ok installed`; exact versions are in the lock. These are
sufficient for the declared bubblewrap package dependencies. Bubblewrap itself
is installed from the pinned agent seed, without downloading packages. The
existing cloud-init template mounts the two read-only seed volumes, validates
runner/config/artifact bindings, installs bubblewrap, and starts the root guest
runner on the private virtio serial channel. This static inspection is not a
successful boot or authenticated guest exchange.

## Materialize on the build/test machine

Use an empty input directory and official installed archive tools. Download
these exact URLs; do not follow `latest` aliases:

1. <https://qemu.weilnetz.de/w64/2026/qemu-w64-setup-20260811.exe>
2. <https://cloud.debian.org/images/cloud/trixie/20260914-2601/debian-13-generic-amd64-20260914-2601.tar.xz>
3. <https://deb.debian.org/debian/pool/main/b/bubblewrap/bubblewrap_0.12.0-1~deb13u1_amd64.deb>

Before extraction, compare the first two SHA-512 hashes with `provenance` in
the checked-in lock, and the bubblewrap SHA-256 with `files.bubblewrap`.
PowerShell `Get-FileHash -Algorithm SHA512` or `sha512sum` can do this.
Extract the QEMU installer **as an archive**, without launching the installer,
using 7-Zip into `extracted/`. Extract the Debian tar into `guest/` so the raw
image is `guest/disk.raw`. Retain the installer and vendor checksums separately
as provenance. The builder selects only the locked executable, DLLs and firmware;
it does not copy the full extracted distribution into the bundle.

Place the Debian package at `bubblewrap_amd64.deb`. Generate the descriptor with
this exact deterministic encoding (Python; no package execution):

```python
import json
from pathlib import Path
value = {
    "schema": "io.tobkiri.packvm-qemu-bubblewrap-descriptor.v1",
    "package": "bubblewrap", "version": "0.12.0-1~deb13u1",
    "architecture": "amd64",
    "source": {
        "url": "https://deb.debian.org/debian/pool/main/b/bubblewrap/bubblewrap_0.12.0-1~deb13u1_amd64.deb",
        "size_bytes": 56876,
        "sha256": "sha256:70aca4fa8daeacb677ec00e8063eb586f08ae3d94b1f11e684370b5524c43431",
    },
}
Path("bubblewrap_descriptor.v1.json").write_bytes(
    (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
)
```

Review/build the deterministic guest zipapp from the exact source revision with
`build_packvm_guest_bundle.py` and record its SHA-256 separately. Record the
reviewed cloud-init template SHA-256 separately too. Supply those explicit pins:

```sh
python -B tobkiri_runtime/scripts/build_pinned_windows_packvm_bundle.py \
  --lock .github/toolchains/windows-packvm-assets.v1.json \
  --inputs-root /absolute/path/to/inputs \
  --runtime-root tobkiri_runtime \
  --config tobkiri_launcher/packvm-qemu/Provisioning/cloud_init_template.yaml \
  --expected-config-sha256 sha256:REVIEWED_CONFIG_DIGEST \
  --expected-agent-sha256 sha256:REVIEWED_AGENT_DIGEST \
  --output /absolute/path/to/new-bundle
```

The output directory must not exist. Every input digest and the complete PE
normal/delay import closure must pass; unknown DLLs remain an error. A different
source revision changes the guest code pin and may change the bundle inventory.
Do not automatically replace a reviewed expected digest with whatever bytes
happen to be present.

## Remaining release gates

The QEMU supplier reports expired code-signing certificates on newer installers:
<https://qemu.weilnetz.de/w64/2026/>. No valid Authenticode result is claimed and
no certificate/trust-warning bypass is authorized by this recipe. This is
separate from Launcher/Python Authenticode admission, which must still pass its
existing trusted signer checks. Downloaded Windows QEMU was not executed during
asset inspection.

PE imports do not prove absence of every dynamic LoadLibrary/module/data lookup.
The general-purpose vendor QEMU has GUI and network-capable components even
though the fixed PackVM invocation disables display, NICs, default devices and
host mounts. Final clean-install WHPX boot, authenticated guest exchanges,
tamper rejection, process/pipe ownership, restart/cancellation and teardown
must be tested on Windows. Preserve the same security gates during those tests.

Before redistribution, review corresponding sources/licenses for QEMU, its DLL
closure, EDK2, Linux and Debian packages; the installer includes EDK2 notices.
This input lock does not supply a full redistribution notice/source package,
SBOM, security update policy, signed release or installer integration.
