from __future__ import annotations

import sys
from pathlib import Path

PACK_ROOT = Path(__file__).resolve().parents[1]
ECOSYSTEM_DIR = PACK_ROOT.parent
RUMI_ROOT = ECOSYSTEM_DIR.parent
DEFAULTSPACK_ROOT = ECOSYSTEM_DIR / "defaultspack"

for path in (RUMI_ROOT, PACK_ROOT, DEFAULTSPACK_ROOT):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

try:
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
except ImportError:  # pragma: no cover - direct function execution fallback
    from domain.tool.browser_computer import BrowserComputerController


def has_computer_action_approval(
    context: object, payload: dict, action: str
) -> bool:
    """Verify the operation token at the standalone function boundary."""
    # Subprocess context is serializable input, not the in-process Host seal.
    # Even server-looking flags cannot replace the action/arguments token check.
    del context
    controller = BrowserComputerController()
    safe_payload = controller._safe_payload(payload)
    return controller._consume_approval(payload, action, safe_payload)


def computer_action_approval_required(payload: dict, action: str) -> dict:
    """Issue an approval challenge for a high-risk computer action."""
    controller = BrowserComputerController()
    return controller._approval_required(action, controller._safe_payload(payload))
