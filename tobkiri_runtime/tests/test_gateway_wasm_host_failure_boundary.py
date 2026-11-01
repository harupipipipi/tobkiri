"""Freeze the Host failure boundary relevant to the Gateway Wasm migration.

These tests execute the original Gateway orchestration and tool bridge through
GlobalContractClient, RequestBroker, and ExactHostProviderBackendV4. The session,
authority, admission, invocation context, effectful artifact metadata, and other
Gateway dependencies are fixtures, not production bootstrap, V4DispatchSession,
or _InvocationSession. There is no guest, native, or Wasm execution and no model
API or paid traffic.

Unlike FakeContractClient's direct bridge calls, the Broker owns provider-error
classification here. Its terminal errors must not become retryable Gateway
invalid_response errors, which could start a second generation.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
import math
import socket
import sys
import traceback
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest

from core_runtime.global_contract_dispatch import (
    GlobalContractClient,
    GlobalContractInvocationError,
)
from core_runtime.host_provider_backend_v4 import (
    ExactHostProviderBackendV4,
    HostProviderContributionV4,
)
from ecosystem.rumi_ai_gateway_pack.runtime import gateway
from ecosystem.rumi_ai_tool_bridge_pack.runtime.bridge import (
    create_tool_intent_operation,
)
from tests.test_ai_gateway_pack import FakeContractClient
from tests.test_tobkiri_host_execution_integration import (
    BrokerFixture,
    context,
    fixture_artifact,
    frame,
    make_broker,
)
from tobkiri_host.backends import BackendRegistry
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.contracts import OperationCatalog, OperationRoute
from tobkiri_host.errors import AuthorizationError, ProviderExecutionError
from tobkiri_host.models import ExecutionKind, OpaqueAuthorityRef


_DECISION_TIME = 1_700_000_000.0
_REQUEST_ID = "gateway-host-boundary-request"
_PROVIDER_FAILURES = ("invalid_response", "integer4301", "nonfinite")
_DENIALS = ("static_denied", "final_denied", "recheck_denied")


@pytest.fixture(autouse=True)
def _offline_deterministic_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Block sockets and scope clock/diagnostics adjustments to this test."""
    network_attempts: list[str] = []

    def forbid_network(*_args: object, **_kwargs: object) -> NoReturn:
        network_attempts.append("socket access")
        raise AssertionError("network is forbidden in the Host boundary fixture")

    monkeypatch.setattr(socket, "socket", forbid_network)
    monkeypatch.setattr(socket, "create_connection", forbid_network)
    monkeypatch.setattr(socket, "socketpair", forbid_network)
    # Replace only this module's clock reference, never the shared time module.
    monkeypatch.setattr(
        gateway, "time", SimpleNamespace(time=lambda: _DECISION_TIME)
    )
    monkeypatch.setattr(gateway, "_DIAGNOSTICS", [])
    try:
        yield
    finally:
        assert not network_attempts


@dataclass
class _Boundary:
    fixture: BrokerFixture
    client: FakeContractClient
    envelopes: list[RequestEnvelope]
    bridge_entries: list[tuple[str, Mapping[str, Any]]]
    bridge_outputs: list[dict[str, Any]]
    bridge_errors: list[Exception]
    session_errors: list[Exception]
    client_errors: list[Exception]
    context_checks: list[str]


@pytest.fixture
def host_boundary(
    scenario: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_Boundary]:
    """Wire only the bridge edge through the real Host execution boundary."""
    fixture = make_broker(
        timeout_ms=1000,
        fail_static=scenario == "static_denied",
        max_workers=1,
    )
    try:
        item = fixture_artifact(timeout_ms=1000)
        operation = replace(
            item.functions[0].operations[0],
            contract_id=gateway.TOOL_BRIDGE_CONTRACT,
            operation_id=gateway.TOOL_BRIDGE_GENERATE_OPERATION,
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            idempotency="none",
        )
        item = replace(
            item,
            functions=(replace(item.functions[0], operations=(operation,)),),
            variants=(
                replace(
                    item.variants[0],
                    execution_kind=ExecutionKind.HOST_EXTENSION,
                    os="any",
                    architecture="any",
                    backend="host.fixture",
                ),
            ),
        )
        envelopes: list[RequestEnvelope] = []
        bridge_entries: list[tuple[str, Mapping[str, Any]]] = []
        bridge_outputs: list[dict[str, Any]] = []
        bridge_errors: list[Exception] = []
        session_errors: list[Exception] = []
        client_errors: list[Exception] = []
        context_checks: list[str] = []

        def invocation_context(request: RequestEnvelope) -> Any:
            envelopes.append(request)
            return SimpleNamespace(
                envelope=request,
                assert_current=lambda: context_checks.append("current"),
            )

        def contribution_invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: Any,
        ) -> Mapping[str, Any]:
            assert invocation.envelope is envelopes[-1]
            bridge_entries.append((operation_id, dict(payload)))
            try:
                result = create_tool_intent_operation(None)("normalize", payload)
            except Exception as error:
                bridge_errors.append(error)
                raise
            bridge_outputs.append(result)
            return result

        backend = ExactHostProviderBackendV4(
            (
                HostProviderContributionV4(
                    contract_id=operation.contract_id,
                    contract_version=operation.contract_version,
                    operation_id=operation.operation_id,
                    principal_id="authority:notification-send",
                    artifact_digest=item.digest,
                    implementation_digest=item.functions[0].implementation_digest,
                    domain_id="domain:notification",
                    invoke=contribution_invoke,
                ),
            ),
            backend_id="host.fixture",
            profile_id=context().profile_id,
            plan_digest=context().plan_digest,
            security_epoch=context().security_epoch,
            invocation_context=invocation_context,
        )
        fixture.broker._catalog = OperationCatalog(
            (item,),
            (
                OperationRoute(
                    contract_id=operation.contract_id,
                    operation_id=operation.operation_id,
                    artifact_digest=item.digest,
                    function_id=item.functions[0].function_id,
                    variant_id=item.variants[0].variant_id,
                    execution_domain_profile="host.fixture",
                    materialization_mode="on_demand",
                    target_principal_ref=OpaqueAuthorityRef(
                        "authority:notification-send"
                    ),
                ),
            ),
        )
        fixture.broker._backends = BackendRegistry((backend,))

        def deny_final(*_args: object, **_kwargs: object) -> NoReturn:
            fixture.events.append("authority_final")
            raise PermissionError("fixture final grant denied")

        def deny_recheck(*_args: object, **_kwargs: object) -> NoReturn:
            fixture.events.append("authority_effect_recheck")
            raise PermissionError("fixture effect-boundary grant denied")

        if scenario == "final_denied":
            monkeypatch.setattr(
                fixture.authority, "authorize_and_issue_lease", deny_final
            )
        elif scenario == "recheck_denied":
            monkeypatch.setattr(
                fixture.authority, "recheck_effect_boundary", deny_recheck
            )

        class FixtureSession:
            """A finite fixture adapter, not the production dispatch session."""

            profile_id = context().profile_id
            plan_digest = context().plan_digest

            def provider_metadata(
                self, _contract_id: str
            ) -> tuple[Mapping[str, Any], ...]:
                return ()

            def invoke(
                self,
                contract_id: str,
                operation_id: str,
                payload: Mapping[str, Any],
                *,
                version_range: str | None = None,
            ) -> Mapping[str, Any]:
                try:
                    return fixture.broker.invoke(
                        replace(
                            frame(),
                            contract_id=contract_id,
                            operation_id=operation_id,
                            version_range=version_range,
                            payload=payload,
                            idempotency_key=None,
                        ),
                        context(),
                        effect_scope={},
                    )
                except Exception as error:
                    session_errors.append(error)
                    raise

        bridge_client = GlobalContractClient(
            session=FixtureSession(),
            allowed_contract_ids=frozenset({gateway.TOOL_BRIDGE_CONTRACT}),
            consumer_pack_id="rumi_ai_gateway_pack",
        )
        arguments = {
            "invalid_response": "{",
            "integer4301": '{"n":' + "9" * 4301 + "}",
            "nonfinite": '{"n":NaN}',
        }.get(scenario, "{}")

        class RoutedClient(FakeContractClient):
            """Keep other dependencies local; do not fake the bridge boundary."""

            def invoke(
                self,
                contract_id: str,
                operation: str,
                payload: dict[str, Any],
                *,
                provider_instance_id: str | None = None,
            ) -> dict[str, Any]:
                if contract_id == gateway.TOOL_BRIDGE_CONTRACT:
                    assert provider_instance_id is None
                    self.calls.append((contract_id, operation, None, dict(payload)))
                    try:
                        return bridge_client.invoke(contract_id, operation, payload)
                    except Exception as error:
                        client_errors.append(error)
                        raise
                result = super().invoke(
                    contract_id,
                    operation,
                    payload,
                    provider_instance_id=provider_instance_id,
                )
                if contract_id == gateway.GENERATE_PROVIDER_CONTRACT:
                    result["tool_intents"] = [
                        {"name": "echo", "arguments": arguments}
                    ]
                elif contract_id == gateway.HEALTH_CONTRACT:
                    for provider in result["providers"]:
                        provider["observed_at"] = _DECISION_TIME
                return result

        yield _Boundary(
            fixture=fixture,
            client=RoutedClient(),
            envelopes=envelopes,
            bridge_entries=bridge_entries,
            bridge_outputs=bridge_outputs,
            bridge_errors=bridge_errors,
            session_errors=session_errors,
            client_errors=client_errors,
            context_checks=context_checks,
        )
    finally:
        fixture.broker.close()
        # These synchronous fixtures have no pending I/O. Join the owned pool
        # as well as closing the Broker so no worker survives the test.
        fixture.broker._executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("scenario", (*_PROVIDER_FAILURES, *_DENIALS))
def test_host_bridge_failure_does_not_restart_generation(
    scenario: str,
    host_boundary: _Boundary,
) -> None:
    """Keep error custody, audit, and cleanup without a second generation."""
    boundary = host_boundary
    fixture = boundary.fixture
    authorization_denied = scenario in {"static_denied", "final_denied"}
    expected_type = (
        AuthorizationError if authorization_denied else ProviderExecutionError
    )
    if scenario == "integer4301":
        assert sys.get_int_max_str_digits() == 4300
    with pytest.raises(expected_type) as raised:
        gateway.create_generate_operation(boundary.client)(
            "generate",
            {
                "request_id": _REQUEST_ID,
                "messages": [],
                "allow_failover": True,
                "idempotency_key": "fixture-replay-safe-generation",
                "requirements": {
                    "modalities": ["text"],
                    "preferred_model_id": "model-a",
                },
            },
        )

    error = raised.value
    assert type(error) is expected_type
    assert error.code == ("denied" if authorization_denied else "provider_failed")
    # The real client and Gateway propagate the Broker error itself unchanged.
    assert len(boundary.session_errors) == len(boundary.client_errors) == 1
    assert boundary.session_errors[0] is error
    assert boundary.client_errors[0] is error
    assert not isinstance(error, GlobalContractInvocationError)
    contracts = [call[0] for call in boundary.client.calls]
    assert contracts.count(gateway.GENERATE_PROVIDER_CONTRACT) == 1
    assert contracts.count(gateway.TOOL_BRIDGE_CONTRACT) == 1
    assert contracts.count(gateway.FAILOVER_CONTRACT) == 0
    # Generation precedes normalization; absence of usage projection is not
    # evidence of no charge in an actual provider-backed request.
    assert contracts.index(gateway.GENERATE_PROVIDER_CONTRACT) < contracts.index(
        gateway.TOOL_BRIDGE_CONTRACT
    )
    assert gateway.USAGE_CONTRACT not in contracts
    generation_call = next(
        call
        for call in boundary.client.calls
        if call[0] == gateway.GENERATE_PROVIDER_CONTRACT
    )
    assert generation_call[2] == "adapter-a"
    assert generation_call[3]["tools"] == []
    assert generation_call[3]["attempt"] == 1

    entered = scenario in _PROVIDER_FAILURES
    assert len(boundary.bridge_entries) == int(entered)
    assert len(boundary.envelopes) == int(entered)
    if entered:
        operation_id, payload = boundary.bridge_entries[0]
        assert operation_id == gateway.TOOL_BRIDGE_GENERATE_OPERATION
        assert payload["request_id"] == _REQUEST_ID
        envelope = boundary.envelopes[0]
        assert envelope.contract_id == gateway.TOOL_BRIDGE_CONTRACT
        assert envelope.target_principal.value == "authority:notification-send"
        assert envelope.target_domain.value == "domain:notification"
        assert envelope.context.request_id == "request-1"
        assert envelope.context.plan_digest == context().plan_digest
    else:
        assert boundary.bridge_errors == []
        assert boundary.bridge_outputs == []
        assert boundary.context_checks == []
        assert "audit_dispatched" not in fixture.events
        assert type(error.__cause__) is PermissionError

    if scenario in {"invalid_response", "integer4301"}:
        assert len(boundary.bridge_errors) == 1
        assert error.__cause__ is boundary.bridge_errors[0]
        assert boundary.bridge_outputs == []
        assert boundary.context_checks == ["current"]
        if scenario == "invalid_response":
            assert type(error.__cause__) is GlobalContractInvocationError
            assert error.__cause__.code == "invalid_response"
        else:
            assert type(error.__cause__) is ValueError
    elif scenario == "nonfinite":
        # The unchanged bridge returns NaN; failure occurs later in the
        # Broker's finite audit digest, not in original normalization.
        assert boundary.bridge_errors == []
        assert len(boundary.bridge_outputs) == 1
        intent = boundary.bridge_outputs[0]["intents"][0]
        assert math.isnan(intent["arguments"]["n"])
        assert intent["authority_granted"] is False
        assert intent["approved"] is False
        assert intent["approval_status"] == "unrequested"
        assert intent["executes"] is False
        assert boundary.context_checks == ["current", "current"]
        assert type(error.__cause__) is ValueError
        assert any(
            entry.name == "_digest"
            and entry.filename.replace("\\", "/").endswith("tobkiri_host/broker.py")
            for entry in traceback.extract_tb(error.__cause__.__traceback__)
        )

    audit_expected = [] if authorization_denied else [("provider_failed", False)]
    assert fixture.audit.failures == audit_expected
    assert "audit_committed" not in fixture.events
    assert fixture.events.count("audit_failed") == len(audit_expected)
    if scenario == "static_denied":
        assert fixture.events == ["authority_static"]
        assert not fixture.admission.released
    else:
        assert fixture.admission.released
        assert fixture.events.count("queue_reserved") == 1
        assert fixture.events.count("reservation_released") == 1
        assert fixture.events[-1] == "reservation_released"
    assert fixture.authority.fenced == (
        [] if authorization_denied else ["request-1"]
    )
    assert not fixture.broker.has_undrained_requests()
