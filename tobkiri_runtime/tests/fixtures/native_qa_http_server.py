"""Subprocess HTTP harness for the final native GUI regression test."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


class _ApprovedPacks:
    """Provide host-owned approval evidence for the bundled QA packs."""

    def get_approval(self, _pack_id: str) -> object:
        """Return fixture approval evidence without persisting a grant."""
        return object()

    def is_pack_approved_and_verified(self, _pack_id: str) -> tuple[bool, str]:
        """Treat the shipped packs as verified for this isolated subprocess."""
        return True, "verified fixture"

    def get_verified_pack_trust(
        self,
        pack_ids: tuple[str, ...],
    ) -> dict[str, str]:
        """Return deterministic trust classes for the selected pack closure."""
        return {pack_id: "verified" for pack_id in pack_ids}


def _runtime_root() -> Path:
    """Return the repository runtime root containing ``core_runtime``."""
    return Path(__file__).resolve().parents[2]


def main() -> None:
    """Start the ephemeral HTTP server until the parent sends ``STOP``."""
    runtime_root = _runtime_root()
    defaultspack_root = runtime_root / "ecosystem" / "defaultspack"
    sys.path.insert(0, str(defaultspack_root))
    sys.path.insert(0, str(runtime_root))

    from core_runtime.capability_binding_registration import (
        register_pack_binding_handlers,
    )
    from core_runtime.di_container import get_container
    from core_runtime.interface_registry import InterfaceRegistry
    import core_runtime.approval_manager as approval_module
    import core_runtime.resolved_profile_scope as profile_scope
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    approval = _ApprovedPacks()
    approval_module.get_approval_manager = lambda: approval
    profile_scope.USER_DATA_DIR = Path(os.environ["RUMI_USER_DATA"])
    profile_scope.invalidate_persisted_resolved_profile()
    plan = profile_scope.persisted_resolved_profile()
    if plan is None:
        raise RuntimeError("native QA subprocess could not recover active profile")

    registry = InterfaceRegistry()
    get_container().set_instance("interface_registry", registry)
    registration = register_pack_binding_handlers(
        interface_registry=registry,
        approval_manager=approval,
        ecosystem_dir=str(runtime_root / "ecosystem"),
        effective_pack_ids=plan.effective_pack_set,
    )
    if not registration.ok:
        raise RuntimeError(
            "native QA subprocess binding registration failed: "
            + json.dumps(registration.diagnostics, sort_keys=True)
        )

    server = DefaultsHttpServer(None)
    server.start()
    if server._server is None:
        raise RuntimeError("native QA subprocess HTTP server did not start")
    ready_file = Path(os.environ["RUMI_QA_READY_FILE"])
    ready_file.write_text(
        json.dumps(
            {
                "port": int(server._server.server_port),
                "conversation_provider_count": len(
                    registry.get(
                        "global_contract.provider.rumi.resource.conversation.v1",
                        strategy="all",
                    )
                ),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    try:
        for line in sys.stdin:
            if line.strip() == "STOP":
                break
    finally:
        server.stop()


if __name__ == "__main__":
    main()
