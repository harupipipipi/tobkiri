"""Canonical captured readonly route quote; authorized read-session fixture."""

from types import SimpleNamespace
import pytest
from core_runtime.authority.v4 import AuthorityDenied, authority_digest
from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from tests.test_ai_gateway_host_provider_v4 import (
    _captured_provider,
    _Invocation,
    _RouteQuoteDispatch,
)
from ecosystem.rumi_ai_gateway_pack.runtime.gateway import (
    MODEL_PROFILE_CONTRACT,
    GENERATE_PROVIDER_CONTRACT,
)
from core_runtime.bootstrap.action_review_v4.reviewer_capture_factory import (
    CanonicalReviewerCaptureFactory,
    SETTINGS_TARGET,
    QUOTE_TARGET,
)


def setup_capture(model="registered-reviewer"):
    contribution, _ = _captured_provider(quote=True)

    class RegistryDispatch(_RouteQuoteDispatch):
        def invoke(self, contract, operation, payload, **kwargs):
            if (
                contract == MODEL_PROFILE_CONTRACT
                and payload.get("identifier") != "registered-reviewer"
            ):
                raise GlobalContractInvocationError(
                    "unresolved_profile", "missing registered fixture profile"
                )
            return super().invoke(contract, operation, payload, **kwargs)

    registry = RegistryDispatch(configured_provider=True)
    state = {"model": model, "revision": 1, "calls": [], "authenticated": []}

    class ReadSession:
        def invoke(self, contract, operation, payload):
            state["calls"].append((contract, operation, dict(payload)))
            if (contract, operation) == SETTINGS_TARGET:
                return {
                    "values": {"tools": {"approval_reviewer_model": state["model"]}},
                    "document_revision": state["revision"],
                }
            assert (contract, operation) == QUOTE_TARGET
            assert payload["model_profile_id"] == state["model"]
            safe = {k: v for k, v in payload.items() if k != "_session_id"}
            return contribution.invoke(operation, safe, _Invocation(registry))

    factory = CanonicalReviewerCaptureFactory(
        ReadSession(),
        session_id="fixture-authenticated-config-session",
        profile_id="profile-1",
        caller_principal_id="fixture-signed-review-caller",
        independent_capture={"signed-edge": "fixture"},
        bind_generate=lambda *args: (_ for _ in ()).throw(
            AssertionError("quote must not generate")
        ),
        assert_current=lambda: None,
        authenticate_read=lambda target, result: state["authenticated"].append(target),
        local_configuration_revision=lambda: (state["model"], state["revision"]),
        local_route_is_current=lambda route: True,
    )
    return factory, registry, state


def test_capture_registered_profile_actual_quote_before_native_approval():
    factory, registry, state = setup_capture()
    prepared = factory.capture()
    facts = prepared.native_facts
    assert facts["model_reference"] == "registered-reviewer"
    assert facts["route_binding"]["model_id"] == "fixture/model"
    assert facts["route_binding"]["provider_instance_id"] == "provider.fixture"
    assert isinstance(facts["route_binding"]["pricing"], dict)
    assert prepared.boundary.boundary_digest == authority_digest(facts)
    assert QUOTE_TARGET in state["authenticated"]
    assert not any(call[0] == GENERATE_PROVIDER_CONTRACT for call in registry.calls)
    reads = len(state["calls"])
    prepared.assert_current()
    prepared.boundary.snapshot()
    assert len(state["calls"]) == reads  # proof guard never dispatches
    prepared.refresh()
    assert len(state["calls"]) == reads + 2
    state["model"] = "changed-model"
    state["revision"] += 1
    with pytest.raises(AuthorityDenied):
        prepared.assert_current()


@pytest.mark.parametrize("model", ["", "not-registered", "fixture/model"])
def test_empty_or_unregistered_model_cannot_fallback_to_catalog_model(model):
    factory, registry, state = setup_capture(model)
    with pytest.raises((AuthorityDenied, GlobalContractInvocationError)):
        factory.capture()
    assert not any(call[0] == GENERATE_PROVIDER_CONTRACT for call in registry.calls)
