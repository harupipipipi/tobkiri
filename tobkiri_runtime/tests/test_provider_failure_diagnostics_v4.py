"""Provider diagnostics use captured identity and retain original stream outcomes."""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from core_runtime.request_failure_diagnostics import RequestFailureDiagnostics
from ecosystem.rumi_provider_adapters_pack.runtime.streaming import stream_request


@pytest.mark.parametrize("failure", ["claim", "publish", "normalize", "finalize"])
def test_stream_failure_uses_closed_host_identity_and_finite_stage(
    failure: str,
) -> None:
    """The public failure stays fixed while Host evidence identifies its stage."""
    records = []
    diagnostics = RequestFailureDiagnostics(
        "request." + "a" * 32,
        parent_request_id="request." + "b" * 32,
        sink=records.append,
    )

    class Client:
        def invoke(
            self,
            _contract: str,
            _operation: str,
            payload: Mapping[str, Any],
        ) -> None:
            if payload["phase"] == failure:
                raise RuntimeError("progress-secret-canary")

        def stream_json_with_credential(self, **kwargs: Any) -> None:
            if failure == "normalize":
                kwargs["on_event"]({"data": {"error": "response-secret-canary"}})
            elif failure != "finalize":
                kwargs["on_event"](
                    {"data": {"choices": [{"delta": {"content": "first"}}]}}
                )

    with pytest.raises(GlobalContractInvocationError) as rejected:
        stream_request(
            Client(),
            {
                "request_id": "forged-secret-canary",
                "progress_id": "progress",
                "deadline": 1,
            },
            {"provider_instance_id": "provider-secret-canary"},
            body={"messages": ["body-secret-canary"]},
            endpoint="https://secret.example",
            headers={"Authorization": "secret-canary"},
            diagnostics=diagnostics,
        )
    assert rejected.value.code == "stream_failed"
    assert len(records) == 1
    assert records[0]["request_id"] == "request." + "a" * 32
    assert (
        records[0]["stage"]
        == {
            "claim": "progress_claim",
            "publish": "progress_publish",
            "normalize": "normalize",
            "finalize": "finalize",
        }[failure]
    )
    assert records[0]["reason"] == (
        "progress" if failure in {"claim", "publish"} else "parser"
    )
    assert "canary" not in str(records)


def test_captured_provider_factory_ignores_payload_correlations(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Host capture links the authenticated parent and never trusts payload IDs."""
    import json
    from threading import Event
    from types import SimpleNamespace

    from ecosystem.rumi_provider_adapters_pack.runtime import adapter

    function_id = "rumi_provider_adapters_pack.provider.compatibility.stream"
    binding = SimpleNamespace(
        function=SimpleNamespace(
            function_id=function_id, implementation_digest="sha256:implementation"
        ),
        operation=SimpleNamespace(
            contract_id="fixture.contract",
            contract_version="1.0.0",
            operation_id="fixture.operation",
        ),
        principal_ref=SimpleNamespace(value="fixture.principal"),
        artifact=SimpleNamespace(digest="sha256:artifact"),
    )
    capture = adapter.ProviderAdapterHostFactoryV4(function_id).capture(
        SimpleNamespace(
            provider_bindings=(binding,),
            domain_ids={
                (
                    "fixture.contract",
                    "fixture.operation",
                    "fixture.principal",
                ): "fixture.domain",
            },
        )
    )

    class Invocation:
        envelope = SimpleNamespace(
            context=SimpleNamespace(request_id="request." + "a" * 32),
            cancellation_requested=Event(),
            deadline_monotonic=10,
        )
        parent_invocation = SimpleNamespace(
            envelope=SimpleNamespace(
                context=SimpleNamespace(request_id="request." + "b" * 32),
            )
        )

        def assert_current(self) -> None:
            pass

        def contract_client(self, **_kwargs: Any) -> object:
            return object()

    def fail_connection(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("secret-connection-canary")

    monkeypatch.setattr(adapter, "_connection", fail_connection)
    with pytest.raises(ValueError):
        capture.contributions[0].invoke(
            "fixture.operation",
            {"request_id": "request." + "c" * 32},
            Invocation(),
        )
    records = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "core_runtime.request_failure_diagnostics"
    ]
    assert len(records) == 1
    assert records[0]["request_id"] == "request." + "a" * 32
    assert records[0]["parent_request_id"] == "request." + "b" * 32
    assert records[0]["stage"] == "prepare"
    assert "canary" not in caplog.text
