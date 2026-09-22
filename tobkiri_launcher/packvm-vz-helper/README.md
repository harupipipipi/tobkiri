# Tobkiri PackVM VZ helper

`tobkiri-packvm-vz-helper` is the macOS-only, signed sidecar boundary for the
Pack v4 Python VM.  It deliberately has no TCP/IP, NAT, bridged, file-sharing,
graphics, audio, USB, or host-directory devices.  Guest communication is only
over one `VZVirtioSocketDeviceConfiguration`; serial output is directed to a
separate diagnostic file descriptor, never the JSON protocol stream.

The executable reads one bounded canonical JSON request per stdin line and
writes one bounded canonical JSON response per stdout line. It is started with
one inherited, read-only Host-to-helper key descriptor:

```text
tobkiri-packvm-vz-helper --agent-key-fd 3
```

The production protocol is
`io.tobkiri.macos-vz-supervisor.v1`. Every Host request has a fresh `host_nonce`
and exact launch-binding digest. The outer response is authenticated with
`agent_mac` (HMAC-SHA-256 over canonical bytes using the inherited per-domain
key). Its `payload` is the guest's complete Ed25519-signed response envelope;
the helper does not normalize, re-sign, or otherwise modify that envelope.
The Host independently verifies the guest signature, public key, binding
digests, request identity, and guest challenge. This keeps the helper from
becoming a guest signing oracle.

The direct operations are `launch`, `invoke`, `bridge_result`, `cancel`, and
`terminate`. A domain keeps one long-lived helper process, which performs
`prepare_efi_store` before launch through the legacy authenticated bootstrap
operation. The production EFI configuration attaches only the unique writable
COW disk at device zero, followed by read-only agent and configuration ISO
seeds. The digest-pinned base image is launch provenance only and is never
attached to the guest. Every launch asset rejects symlinks, hardlinks,
world-writable files, path escapes, and digest changes.

The helper does not create a host mount or a host Unix socket. It has no
directory-sharing device and no network device. Its required serial console is
attached to the operating system null sink, so production bindings never add an
unattested diagnostics file. It reports the exact cleanup acknowledgement only
after VZ devices are detached. The inherited key is held only in memory and its
descriptor is closed immediately after it is read.

`launch` is fail-closed unless the binary has a valid code signature and the
`com.apple.security.virtualization` entitlement.  That means `swift run` and
unit tests can exercise protocol validation but cannot start a VM.

## Build

```bash
cd tobkiri_launcher/packvm-vz-helper
swift test
swift build -c release
```

`build_packvm_vz_helper.sh` stages the sidecar plus the immutable provisioning
descriptor/template set. It download-verifies the pinned bubblewrap package
with HTTPS and a no-redirect policy, but never bundles a raw image or private
key. The packaging layer must sign this executable with
`Entitlements/tobkiri-packvm-vz-helper.entitlements`, place it in the app's
sidecar location, and pass the inherited key FD. Only then may the Host
register the resulting supervisor as `macos-vz`.
