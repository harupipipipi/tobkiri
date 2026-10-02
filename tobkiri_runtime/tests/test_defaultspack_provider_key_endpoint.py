import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ecosystem" / "defaultspack"))

from blocks.ai.provider_key import _builtin_provider_endpoint


def test_opencode_zen_uses_versioned_openai_compatible_endpoint() -> None:
    assert _builtin_provider_endpoint("opencode-zen") == "https://opencode.ai/zen/v1"
