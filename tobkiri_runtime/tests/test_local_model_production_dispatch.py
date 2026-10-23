"""Hermetic production Broker wiring proof; no VM or external provider claims."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from tests import test_live_production_v4_dispatch as support
from tests.test_live_production_v4_dispatch import _deny_production_test_internet  # noqa: F401
from core_runtime import local_model_transport
from core_runtime.authority.v4 import AuthorityStore, FunctionPrincipal
from core_runtime.bootstrap.production_v4 import capture_production_dispatch
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile,
    prepare_default_profile_confirmation,
)
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    defaultspack_activation_snapshot_loader,
)
from ecosystem.defaultspack.domain.runtime_surface_v4 import create_runtime_surface_services
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry
from ecosystem.rumi_model_registry_pack.runtime.registry import ModelRegistry
from tobkiri_host.backends import BackendRegistry
from tobkiri_host.effects import ProviderOutcome
from tobkiri_protocol.secure_persistence import SecureDirectory


def test_real_broker_binds_local_transport_without_a_credential_store(tmp_path, monkeypatch):
    """Exercise nested registry/gateway/adapter and real lease checks, fake IO only."""
    import socket

    def deny_network(*_args, **_kwargs):
        pytest.fail("this production wiring test must not access a network")

    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    user_data = tmp_path / "local-production"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    active = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    provider = "provider.liquid-local"
    endpoint = "http://127.0.0.1:18080/v1"
    ProviderRegistry("defaults", user_data_root=user_data).save(
        {
            "provider_instance_id": provider,
            "adapter_id": "local-openai-compatible",
            "endpoint": endpoint,
            "credential_handle": None,
            "enabled": True,
        },
        expected_revision=0,
    )
    SecureDirectory(user_data / "host_local_models").write_bytes_atomic(
        "allowlist.json",
        json.dumps(
            {
                "version": "tobkiri.host.local-models.v1",
                "registrations": [
                    {
                        "profile_id": "defaults",
                        "provider_instance_id": provider,
                        "endpoint": endpoint,
                        "model_ids": ["lfm-local"],
                    }
                ],
            }
        ).encode(),
    )
    ModelRegistry("defaults", user_data_root=user_data).save(
        {
            "model_profile_id": "profile.liquid-local",
            "model_id": "lfm-local",
            "metadata": {"provider_connection_id": provider},
        },
        expected_revision=0,
    )
    observed = []

    class FakeSocket:
        def shutdown(self, _how):
            pass

    class FakeConnection:
        def __init__(self, host, port, *, timeout):
            assert (host, port) == ("127.0.0.1", 18080)
            assert 0 < timeout <= 30
            self.sock = FakeSocket()

        def connect(self):
            pass

        def request(self, method, path, payload, headers):
            assert method == "POST" and path == "/v1/chat/completions"
            assert "Authorization" not in headers
            observed.append(json.loads(payload))

        def getresponse(self):
            response = support._ProviderResponse()
            response.status = 200
            return response

        def close(self):
            pass

    monkeypatch.setattr(local_model_transport.http.client, "HTTPConnection", FakeConnection)
    binding = next(
        item
        for item in active.resolved.plan["bindings"]
        if item["contract_id"] == "conversation.turn.v1" and item["operation_id"] == "complete"
    )
    target = FunctionPrincipal.from_dict(binding["function_principal"])
    backend = support._CapturedBackend(support._digest("local-production-backend"))
    backend.target_executable_digest = target.function_implementation_digest

    def invoke_guest(envelope):
        request = {
            "profile_id": envelope.context.profile_id,
            "messages": envelope.payload["messages"],
            "model_reference": {"profile_id": "profile.liquid-local"},
            "deadline": int(time.time()) + 30,
        }
        response = backend.capability_bridge(envelope, support._ai_bridge_request(request))
        assert response["result"]["status"] == "ok", response
        return ProviderOutcome(response["result"]["value"])

    monkeypatch.setattr(backend, "invoke", invoke_guest)
    session = capture_production_dispatch(
        active,
        bundle_root=support._bundle_root(),
        ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
        authority_store=AuthorityStore(user_data / "authority" / "v4.sqlite3"),
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
        backends=BackendRegistry((backend,)),
        target_backend_digests={target.principal_id: backend.status.backend_digest},
    )
    try:
        for index in range(2):
            result = session.invoke(
                "conversation.turn.v1",
                "complete",
                {
                    "_session_id": "session.panel.local-production",
                    "messages": [{"role": "user", "content": f"local turn {index}"}],
                },
            )
            assert result["output"] == "production-ok"
    finally:
        session.close()
    assert len(observed) == 2
    assert all(item["model"] == "lfm-local" for item in observed)
