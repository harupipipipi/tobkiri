# Tobkiri Launcher start guide

Tobkiri Launcher is the desktop shell for the Tobkiri runtime. The packaged
macOS app can be opened from Finder. For source development in a checkout,
start it from `tobkiri_launcher/frontend` so the shell can find and launch the
local runtime kernel.

## Open an already built Launcher on Windows

If the Launcher has already been built in this checkout, it can be started
without a terminal:

1. Open File Explorer and navigate to the checkout's
   `tobkiri_launcher\src-tauri\target\x86_64-pc-windows-msvc\debug` folder.
2. Double-click `tobkiri-launcher.exe`.
3. If setup is offered, choose **Open Setup**, review the listed Defaults
   Profile, select its confirmation checkbox, and choose **Activate Defaults
   Profile**. Submit it only once. Verification can take several minutes while
   the Host restarts; if **Verify activation** becomes available, choose it.
   Do not submit the Profile again while verification is pending. An existing
   active Profile may hide **Open Setup**. If the Launcher instead reports a
   stale Profile revision or unavailable Host Pack service, stop and record
   that error; this is not a verified activation or a usable Pack catalog.

The current Windows build can activate a Profile but cannot provision the
PackVM needed to launch Defaultspack Chat and Pack functions. Home disables
**Launch** and links to [Windows PackVM support issue #1494](https://github.com/harupipipipi/tobkiri/issues/1494).
Docker Desktop exposing `/dev/kvm` to Linux containers does not provide this
Launcher with a Windows PackVM backend. Use the Apple Silicon macOS package
below for a complete GUI and PackVM test until Windows support is implemented.

The executable exists only after the Windows development build has completed.
An executable left in this folder can predate the current checkout. Its version
label does not identify the source commit, so an old executable cannot prove
current-HEAD behavior. Build the intended revision before acceptance testing.
For a normal installed copy, open **Tobkiri Launcher** from the Start menu.

## Open a packaged Launcher on Apple Silicon macOS

Use a completed macOS DMG installer. The ordinary release DMG contains
**Tobkiri Launcher.app**. A DMG downloaded from the **Desktop Installers**
workflow contains **Tobkiri Launcher CI E2E.app** and is marked
non-publishable; use it only to test that workflow's build. These packaged apps
include the signed PackVM helper and its provisioning manifests. A locally
built **Tobkiri Launcher Developer.app** does not gain those files merely by
opening it in Finder.

1. In Finder, double-click the DMG, then drag the app onto its **Applications**
   shortcut. Open **Applications** in Finder and double-click the copied app.
   This distribution is ad-hoc signed and not notarized. If macOS blocks the
   first open, and you trust the copy you downloaded, use **System Settings** →
   **Privacy & Security** → **Open Anyway**, then confirm **Open**. See
   [Apple's instructions](https://support.apple.com/en-us/102445).
2. In the Launcher, choose **Open Setup**. Review the Defaults Profile, select
   its confirmation checkbox, and choose **Activate Defaults Profile**. If
   prompted, choose **Verify activation**.
3. Open **Packs** and find **PackVM lifecycle**. On Apple Silicon, choose
   **Prepare plan** if the doctor says **Not ready**. Review the displayed image
   source, size, digests, and required free space. The guest image is not in
   the DMG; first-time provisioning may download the pinned 3 GiB image.
4. If the plan is available and you agree with those facts, select its approval
   checkbox, choose **Record explicit consent**, then **Provision PackVM**.
   Wait for provisioning to finish and for the doctor to show
   **Healthy and attested**. If it remains **Not ready**, read the displayed
   reason before trying again.
5. Return to **Home** and choose **Launch Defaults Profile** to open the
   Defaultspack interface.

The installer workflow checks the packaged helper and Launcher startup, but a
successful workflow does not by itself show that a PackVM guest can boot on
your Mac. Follow the in-app status for that device.

## Build and start from source

```bash
cd tobkiri_launcher/frontend
npm install
npm run tauri -- dev
```

The launcher owns the kernel bootstrap and panel connection. `defaultspack`
is opened from the launcher after the panel is ready; starting a second
kernel manually can cause bootstrap `401` responses or a blank panel.

For the full troubleshooting guide, including ports, approval, and managed
pack details, see [`rumi_viewer_start.md`](./rumi_viewer_start.md).

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
