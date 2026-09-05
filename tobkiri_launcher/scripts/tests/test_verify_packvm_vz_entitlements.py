from __future__ import annotations

import importlib.util
import plistlib
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "verify_packvm_vz_entitlements.py"
SPEC = importlib.util.spec_from_file_location(
    "verify_packvm_vz_entitlements_tests", SCRIPT
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load {SCRIPT}")
VERIFIER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VERIFIER
SPEC.loader.exec_module(VERIFIER)


def test_exact_packvm_vz_entitlement_is_accepted() -> None:
    VERIFIER.verify_entitlements(
        plistlib.dumps({"com.apple.security.virtualization": True})
    )


@pytest.mark.parametrize(
    "entitlements",
    (
        {},
        {"com.apple.security.virtualization": False},
        {"com.apple.security.virtualization": "true"},
        {
            "com.apple.security.virtualization": True,
            "com.apple.security.get-task-allow": True,
        },
    ),
)
def test_packvm_vz_entitlement_drift_is_rejected(
    entitlements: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="entitlements are not exact"):
        VERIFIER.verify_entitlements(plistlib.dumps(entitlements))
