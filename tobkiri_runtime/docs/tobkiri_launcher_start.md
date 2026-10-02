# Tobkiri Launcher start guide

Tobkiri Launcher owns Host startup, the panel connection and native Authority.
Defaults requires a verified active Profile and a healthy attested PackVM.
Starting Python, serving the frontend, or opening `/panel/` in a browser does
not complete those steps.

Choose one workflow: a packaged installer, or a source-development session.
A Developer build uses checkout-local state and must not borrow the installed
app's activation or credentials.

## Build and start from source

Complete the repository [Setup](../../README.md#setup) first. In particular:

- Use the repo-root `.venv` with the pinned runtime and development requirements
- Use Node.js 22.22.0+ and the frontend's locked dependencies (`npm ci`)
- Install Rust, the platform's Tauri 2 native build prerequisites and the Cargo
  Tauri CLI 2.x. The npm CLI does not satisfy the nested `cargo tauri` command
- Start from clean, committed source with a matching generated source manifest

From the repo root, confirm `git status --short` has no source changes and
`cargo tauri --version` succeeds. The commands below start a **foreground
session**; leave that terminal running until you finish using the Launcher.

macOS, with the explicit ad-hoc Developer overlay:

```bash
source .venv/bin/activate
cd tobkiri_launcher/frontend
npm run tauri -- dev --config src-tauri/tauri.macos.dev.conf.json
```

Linux:

```bash
source .venv/bin/activate
cd tobkiri_launcher/frontend
npm run tauri -- dev
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
cd tobkiri_launcher\frontend
npm run tauri -- dev
```

The npm wrapper changes to `tobkiri_launcher` before invoking Tauri, so the
`src-tauri/...` configuration path is relative to that directory. The hook
checks free space, builds the panel, prepares `uv`, builds the Pack Shell and
Defaults Shell, and generates a Defaults bundle bound to the committed source.
The native Launcher then starts its Host and connects to the panel. First
builds can take substantial time and disk space.

If frontend generation changes tracked files, review and commit the intended
artifacts before retrying. A source-manifest or provenance rejection is a
stopping condition, not a reason to edit seals or disable the checks.

`npm run dev` alone starts a frontend development server. It does not start
Tobkiri Launcher or establish native Authority. Do not start a separate Host or
`pack-shell` process first. A copied debug executable or a leftover Developer
app bundle is not a substitute for this foreground workflow. Preparing a
standalone Developer bundle is a separate native packaging task; these commands
are not instructions to open that bundle after stopping the development session.

## Activate and launch Defaults

1. In the Launcher, choose **Open Setup** if offered. Review the Defaults
   Profile, select its confirmation checkbox, then choose **Activate Defaults
   Profile** once
2. Wait for verification while the Host restarts. If **Verify activation** is
   offered, use it rather than resubmitting activation. An already-active
   Profile may hide **Open Setup**
3. Open **Packs** → **PackVM lifecycle**. If a verified provisioning plan is
   offered, review its image source, size, digests and required free space
4. If you agree with that plan, select its approval checkbox, choose **Record
   explicit consent**, then **Provision PackVM**. Wait for **Healthy and
   attested**. If the doctor remains **Not ready**, read its reason before retrying
5. Return to **Home** and choose **Launch Defaults Profile**

A stale Profile revision, unavailable Host Pack service or failed verification
is not a successful activation. Record the exact error. Profile activation
alone does not demonstrate that Defaults Chat or Pack functions can run.

## PackVM and platform readiness

The VM assets and accelerator must match the actual build and device:

- Apple Silicon macOS uses the signed Virtualization.framework PackVM helper
  and its verified provisioning manifests. Packaged builds prepared with those
  assets can offer the pinned guest-image provisioning plan. The guest image
  is not supplied merely by installing Python or opening a Developer app
- Linux x86_64 has a QEMU/KVM backend. Complete reviewed VM assets must be bound
  into the build, and the device must provide usable `/dev/kvm` access
- Windows x86_64 has a QEMU/WHPX backend. Complete reviewed VM assets, applicable
  signer bindings and a usable Windows hypervisor are required. Docker Desktop
  exposing `/dev/kvm` in a Linux container does not satisfy the native WHPX path

An ordinary source build can have no bound portable VM assets. The build accepts
that absence for Launcher development; it does not turn an unprovisioned device
into a working Defaults environment. The Linux/Windows asset source and expected
manifest digest are paired **build inputs**, not runtime approval credentials.
Do not point a running Launcher at arbitrary VM directories or set environment
flags to bypass attestation. See the build inputs in
[`build.rs`](../../tobkiri_launcher/src-tauri/build.rs) and
[`packvm_bundle.rs`](../../tobkiri_launcher/src-tauri/src/packvm_bundle.rs).

Use the in-app lifecycle doctor to establish readiness on your device. A passed
source check, successful native compilation, or installer workflow does not
prove that a real guest boots or that a Defaults window is usable.

## Open an already built Launcher on Windows

For a normal installed copy, open **Tobkiri Launcher** from the Start menu.
For a completed checkout development build, the executable is under
`tobkiri_launcher\src-tauri\target\debug` or the selected target's
`target\x86_64-pc-windows-msvc\debug` directory. Open the matching
`tobkiri-launcher.exe` only while its development workflow and prepared artifacts
remain available; follow [Activate and launch Defaults](#activate-and-launch-defaults).

An executable exists only after a native build. A file left there can predate
current HEAD; its version label does not establish the source commit or VM
readiness. Rebuild the intended revision before acceptance testing.

## Open a packaged Launcher on Apple Silicon macOS

Use a completed DMG for the intended revision. The ordinary release name is
**Tobkiri Launcher.app**. The **Desktop Installers** workflow produces
**Tobkiri Launcher CI E2E.app**, which is a non-publishable test artifact.
A locally built **Tobkiri Launcher Developer.app** is a separate artifact and
must not be treated as an installed production app.

1. In Finder, open the DMG and drag the app onto its **Applications** shortcut
2. Open the copied app in **Applications**. Follow the artifact's distribution
   and signing notes. If macOS blocks a trusted unsigned/ad-hoc copy, Apple's
   [open-anyway instructions](https://support.apple.com/en-us/102445) explain
   the explicit user decision; do not disable Gatekeeper globally
3. Follow [Activate and launch Defaults](#activate-and-launch-defaults)

The package must contain the verified PackVM helper and provisioning resources
before it can offer that platform's setup plan. First-time provisioning may
require the pinned multi-GiB guest-image download. Review the size and storage
requirements shown by the actual plan before consenting. Opening the app or
passing installer CI is not evidence that a guest can boot on your Mac.

## Health diagnostics and approval

After the Launcher starts the Host, use a second terminal with the same `.venv`
at the repo root:

```bash
python -m app --health
```

This reads the already-running Host's `/health` at `127.0.0.1:8765`, or the port
selected by `RUMI_PORT`. It never starts a Host or binds its listen port. No
listener yields `status: "down"` and a nonzero exit code. Inspect the complete
response, including `runtime_ready`, `needs_setup` and any error. A health response
is not a receipt for Profile activation, a VM boot or a Defaults launch.

`python -m tobkiri` intentionally rejects startup without a Launcher-injected
Pack v4 activation snapshot. The installed legacy `rumi_ai` module remains a
compatibility path with the same fail-closed boundary. `python -m app` and
`--headless` do not supply that authority or replace setup.

Perform approval through the Launcher-owned native approval window. A regular
browser page or Bearer token is not approver authority. Do not use the retired
`RUMI_AUTO_APPROVE_LOCAL` recipe, manually grant capabilities, rewrite signed
JSON, or pass approval credentials in URLs to work around setup failures.

## Troubleshooting

- **Missing development uv / Python:** verify the repo-root `.venv`, pinned
  requirements and `uv --version`. The preparation script expects the repo's
  `.venv/bin/python3` or `.venv\Scripts\python.exe`
- **`cargo: no such command: tauri`:** install the Cargo Tauri CLI 2.x. The npm
  Tauri dependency alone does not provide this nested command
- **Dirty-source or source-manifest rejection:** keep committed source and
  generated artifacts from the same revision. Review the mismatch and rebuild;
  do not fabricate provenance
- **Insufficient disk space:** the Launcher preflight requires 5 GiB by default,
  with additional space needed for caches, Rust artifacts and VM storage
- **Bootstrap `401` / blank panel:** check whether an old Host owns port 8765.
  Stop only the identified old Tobkiri process gracefully, then restart the
  matching Launcher session. Do not invent API tokens to repair bootstrap
- **PackVM `Not ready`:** read the lifecycle doctor's exact reason. Missing
  build-bound resources, unsupported device features and stale registration are
  different failures; a Profile retry does not fix them

On macOS/Linux, listener inspection is read-only:

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN
pgrep -fl 'tobkiri|rumi_ai|python.*-m app'
```

Do not treat old screenshots, a version label, synthetic UI tests or Python
imports as proof of an installed/native launch. Acceptance should identify the
exact source revision/artifact, native window, verified activation and device's
PackVM doctor result separately.

## Storage re-registration

If PackVM reports a changed storage device number, use **Prepare plan** in its
lifecycle panel. A storage re-registration proposal is available only when the
authenticated registration, directory paths and inodes, private ownership, and
original image still verify. Review both storage locations and the previous and
current device numbers. Legacy registrations contain no volume UUID, so these
checks cannot establish whether storage was moved or restored.

Only if you recognize the displayed storage, select the additional storage
confirmation as well as the registration confirmation, then record consent and
update the registration once. This retains the original registration and leaves
the image, VM disks, firmware, domain files, and user data in place. It does not
start or restart a VM. A changed path, inode, owner, or image remains unavailable;
do not edit the attestation or delete the VM to work around that refusal.
