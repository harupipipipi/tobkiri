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
