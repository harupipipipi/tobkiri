# Core Control Panel

The canonical frontend source for this panel lives in `../../../../../tobkiri_launcher/frontend`.

This pack serves the built static artifact at `/panel` from `web/`. Both the browser route (`http://127.0.0.1:8765/panel/`) and the Tobkiri Launcher bootstrap flow use the same artifact.

The Tauri `splash` screen remains separate and is only used before the kernel is ready.
