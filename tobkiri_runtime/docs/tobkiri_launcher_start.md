# Tobkiri Launcher start guide

Tobkiri Launcher is the desktop shell for the Tobkiri runtime. In a checkout,
start it from `tobkiri_launcher/frontend` so the shell can find and launch the
local runtime kernel.

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
