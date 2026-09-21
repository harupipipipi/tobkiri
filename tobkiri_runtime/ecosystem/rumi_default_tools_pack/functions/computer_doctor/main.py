from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from domain.computer import create_default_computer_tool_service


def run(context, args):
    try:
        svc = create_default_computer_tool_service()
        base = svc.doctor()

        checks: list[dict] = []

        if sys.platform == "darwin":
            # AX trusted check
            try:
                from domain.computer.mac.ax import ax_is_trusted

                trusted = ax_is_trusted()
                checks.append({
                    "name": "accessibility_trusted",
                    "status": "pass" if trusted else "warn",
                    "reason": "AXIsProcessTrusted" if trusted else "Accessibility permission not granted",
                })
            except Exception as e:
                checks.append({"name": "accessibility_trusted", "status": "fail", "reason": str(e)})

            # TCC screen recording
            try:
                from domain.computer.mac.helper import tcc_screen_recording_granted

                granted = tcc_screen_recording_granted()
                checks.append({
                    "name": "screen_recording",
                    "status": "pass" if granted else "warn",
                    "reason": "Screen recording permitted" if granted else "Screen recording not granted",
                })
            except Exception as e:
                checks.append({"name": "screen_recording", "status": "fail", "reason": str(e)})

            # CGEvent smoke test
            try:
                from domain.computer.mac.cgevent import cgevent_smoke_test

                result = cgevent_smoke_test()
                checks.append({
                    "name": "cgevent",
                    "status": "pass" if result["available"] else "warn",
                    "reason": "; ".join(result["notes"]),
                })
            except Exception as e:
                checks.append({"name": "cgevent", "status": "fail", "reason": str(e)})

            # ScreenCaptureKit
            try:
                from domain.computer.mac.screencapture import screen_capture_kit_available

                avail = screen_capture_kit_available()
                checks.append({
                    "name": "screen_capture_kit",
                    "status": "pass" if avail else "warn",
                    "reason": "ScreenCaptureKit available" if avail else "ScreenCaptureKit not available (CLI fallback exists)",
                })
            except Exception as e:
                checks.append({"name": "screen_capture_kit", "status": "fail", "reason": str(e)})
        else:
            checks.append({"name": "platform", "status": "warn", "reason": f"Platform {sys.platform} - Mac checks skipped"})

        return {
            "platform": sys.platform,
            "checks": checks,
            **base,
        }
    except Exception as e:
        return {"action": "computer.doctor", "error": str(e)}
