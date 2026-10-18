"""Compatibility specifications for model profile ownership adapters."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_existing_profile_lookup_reads_owner_snapshot_only() -> None:
    from blocks.ai.routing.profiles import _existing

    result = _existing(
        {
            "profiles": [
                {"model_profile_id": "saved", "model_id": "example/model"}
            ]
        },
        "saved",
    )

    assert result == {
        "model_profile_id": "saved",
        "model_id": "example/model",
    }

