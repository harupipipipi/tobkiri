#!/usr/bin/env bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"

Xvfb "${DISPLAY}" -screen 0 1440x900x24 >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!
fluxbox >/tmp/fluxbox.log 2>&1 &
FLUXBOX_PID=$!
x11vnc -display "${DISPLAY}" -nopw -forever -shared >/tmp/x11vnc.log 2>&1 &
X11VNC_PID=$!

cleanup() {
  kill "${X11VNC_PID}" "${FLUXBOX_PID}" "${XVFB_PID}" >/dev/null 2>&1 || true
}

trap cleanup EXIT

python3 - <<'PY'
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

home = Path.home()
browser_state = home / ".mimo-worker-browser"
browser_state.mkdir(parents=True, exist_ok=True)

assignment_path = os.environ.get("WORKER_ASSIGNMENT_FILE", "").strip()
status_path = os.environ.get("WORKER_STATUS_FILE", "").strip()
assignment = {}
if assignment_path:
    try:
        assignment = json.loads(Path(assignment_path).read_text(encoding="utf-8"))
    except Exception as exc:
        assignment = {"load_error": str(exc)}

start_url = os.environ.get("START_URL", "").strip() or str(assignment.get("qa_target") or "").strip()
browser_launch = {"attempted": False, "start_url": start_url}
if start_url:
    subprocess.Popen(
        [
            "python3",
            "-m",
            "playwright",
            "open",
            start_url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    browser_launch = {"attempted": True, "start_url": start_url}

if status_path:
    payload = {
        "worker_id": os.environ.get("WORKER_ID", ""),
        "persona_id": os.environ.get("WORKER_PERSONA_ID", ""),
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "assignment": assignment,
        "browser_launch": browser_launch,
        "display": os.environ.get("DISPLAY", ":99"),
    }
    path = Path(status_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
PY

while true; do
  sleep 3600
done
