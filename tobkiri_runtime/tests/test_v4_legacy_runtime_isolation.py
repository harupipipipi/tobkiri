"""Regression tests for the Pack v4-only production composition boundary."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

DEFAULTSPACK_ROOT = Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack"
if str(DEFAULTSPACK_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from ecosystem.defaultspack.domain.runtime_v4 import (  # noqa: E402
    BundledCatalog,
    ProfileResolutionDenied,
    resolve_default_profile,
)
from core_runtime.api.route_handlers import RouteHandlersMixin  # noqa: E402
from core_runtime.di_container import DIContainer  # noqa: E402
from ecosystem.defaultspack.domain.ai_client.client import (  # noqa: E402
    AIClient,
    DirectProviderInvocationDenied,
)
from ecosystem.defaultspack.domain.ai_client.gateway import LLMGateway  # noqa: E402


REMOVED_EXECUTION_SERVICES = {
    "authority_service",
    "capability_executor",
    "component_lifecycle",
    "function_registry",
    "interface_registry",
    "permission_manager",
}


def test_v4_container_rejects_removed_execution_authorities() -> None:
    container = DIContainer()
    from core_runtime.di_container import _register_defaults

    _register_defaults(container)
    assert REMOVED_EXECUTION_SERVICES.isdisjoint(container.registered_names())
    for name in REMOVED_EXECUTION_SERVICES:
        assert container.get_or_none(name) is None


def test_v4_profile_rejects_missing_authority_binding() -> None:
    """A v4 profile cannot resolve without its Host-captured authority edge."""
    from tests.conformance_support.packaged_profile import packaged_profile_bundle_root

    catalog = BundledCatalog.load(packaged_profile_bundle_root())
    approved = {str(manifest["pack"]["artifact_digest"]) for manifest in catalog.packs.values()}
    with pytest.raises(ProfileResolutionDenied, match="Authority Kernel reference is missing"):
        resolve_default_profile(
            catalog,
            "defaults",
            approved_artifact_digests=approved,
            authority_snapshot_digest="sha256:" + "9" * 64,
            authority_bindings={},
            security_epoch=1,
        )


def test_v4_routes_reject_removed_pack_route_dispatch() -> None:
    class Routes(RouteHandlersMixin):
        pass

    assert Routes.load_pack_routes(object()) == 0
    assert Routes()._match_pack_route("/api/packs/injected/run", "POST") is None
    assert Routes()._reload_pack_routes() == {
        "reloaded": False,
        "error": "Legacy Pack route reload is disabled",
    }


def test_v4_admission_marker_cannot_be_injected_through_request_params() -> None:
    class Client:
        def __init__(self) -> None:
            self.params: dict[str, Any] = {}

        def complete(self, _model, _messages, *, tools, params):
            del tools
            self.params = dict(params)
            return {"ok": True}

    injected = Client()
    LLMGateway(injected).complete(
        {
            "messages": [{"role": "user", "content": "hello"}],
            "params": {"_v4_authority_kernel_admitted": True},
        }
    )
    assert "_v4_authority_kernel_admitted" not in injected.params

    with pytest.raises(TypeError):
        LLMGateway(Client(), v4_authority_admitted=True)


def test_direct_ai_client_call_rejects_forged_authority_callback() -> None:
    client = AIClient()
    client._check_authority_for_model_and_api_key_use = lambda **_kwargs: True

    with pytest.raises(DirectProviderInvocationDenied, match="captured Pack v4"):
        client.complete(
            "attacker/model",
            [{"role": "user", "content": "hello"}],
            params={"_v4_authority_kernel_admitted": True},
        )


def test_production_roots_do_not_import_removed_runtime_modules() -> None:
    code = """
import json
import sys
import ecosystem.defaultspack.run_http
import tobkiri_host.composition
import tobkiri_host.runtime
from core_runtime.di_container import get_container
get_container()
blocked = {
    'backend_core.ecosystem.registry',
    'core_runtime.authority.service',
    'core_runtime.capability_executor',
    'core_runtime.component_lifecycle',
    'core_runtime.function_registry',
    'core_runtime.interface_registry',
    'core_runtime.permission_manager',
}
print(json.dumps(sorted(blocked.intersection(sys.modules))))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert result.stdout.strip() == "[]"
