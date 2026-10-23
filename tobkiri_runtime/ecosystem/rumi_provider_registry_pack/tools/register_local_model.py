"""Register an operator-approved local runner using this Pack's own registry.

Developer validation command only; not an exposed Host provider or Pack contract.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "tobkiri_runtime"))

from core_runtime.local_model_registration import register as register_local_model
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry


def register(
    *, user_data: Path, profile: str, provider: str, endpoint: str, model: str
) -> None:
    """Compose this registry with the Host's exact operator allowlist."""
    registry = ProviderRegistry(profile, user_data_root=user_data)
    register_local_model(
        user_data=user_data, profile=profile, provider=provider, endpoint=endpoint,
        model=model, registry=registry,
    )

def main() -> None:
    """Register only the exact operator-supplied Profile and loopback model."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-data", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--provider", default="provider.liquid-local")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    register(
        user_data=args.user_data,
        profile=args.profile,
        provider=args.provider,
        endpoint=args.endpoint,
        model=args.model,
    )
    print("Local model registered. Select its connection and model in Tobkiri.")


if __name__ == "__main__":
    main()
