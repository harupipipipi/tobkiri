"""Pure proposal tests: real Host state; recording opener; no network or keys."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO
import json
import multiprocessing
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.authority.v4_models import (
    GrantLifetime,
    InteractiveApprovalDecision,
    InteractiveApprovalRequest,
    authority_digest,
)
from core_runtime.credential_transport import (
    CredentialTransportDenied,
    HostBoundCredentialTransport,
)
from core_runtime.generation_transport_budget import (
    ENROLL_FUNCTION,
    ENROLL_OPERATION,
    GenerationBudgetPolicy,
    HostGenerationBudgetAdmission,
    PRICE_POLICY_FIELDS,
)
from core_runtime.global_contract_dispatch import (
    GlobalContractClient,
    GlobalContractInvocationError,
    HostGenerationBudgetError,
)
from ecosystem.rumi_ai_pipeline_pack.runtime.pipeline import (
    create_failover_operation,
)
from ecosystem.rumi_provider_adapters_pack.runtime import adapter, streaming
from tests import test_authority_v4_lifecycle as lifecycle
from tests.test_tobkiri_host_authority_v4_adapter import (
    _adapter,
    _context,
    _digest,
    _queries,
)
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.credential_store import HostCredentialMaterialStore, KEY_VERSION
from tobkiri_host.models import OpaqueAuthorityRef

FUNCTION = "rumi_provider_adapters_pack.provider.compatibility.generate"
OPERATION = "rumi_provider_adapters_pack.provider-generate"
CONTRACT = "tobkiri.service.ai.provider.generate.v1"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


class Response:
    """A local recording reply, never a socket response."""

    def __init__(self, value: bytes = b'{"choices": []}') -> None:
        self._value = BytesIO(value)

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int | None = None) -> bytes:
        return self._value.read(amount)


def approved_enrollment(
    harness: lifecycle._Harness,
    policy: GenerationBudgetPolicy,
    *,
    request_id: str = "operator-enrollment-1",
) -> None:
    """Persist a real interactive settlement in temporary Host state."""
    target = replace(
        harness.target,
        function_id=ENROLL_FUNCTION,
        operation_id=ENROLL_OPERATION,
    )
    scope = replace(
        harness.scope,
        capability="host.generation-budget",
        exact_request_digest=policy.digest,
    )
    request = InteractiveApprovalRequest(
        request_id=request_id,
        request_digest=policy.digest,
        caller=harness.caller,
        target=target,
        profile_id="profile-1",
        activation_id="activation-1",
        activation_digest=_digest("activation"),
        plan_digest=_digest("plan"),
        profile_authority_digest=_digest("4"),
        profile_revision="",
        security_epoch=1,
        fencing_token=7,
        caller_domain_id=harness.caller_domain.domain_id,
        caller_boot_epoch=1,
        target_domain_id=harness.target_domain.domain_id,
        target_boot_epoch=1,
        target_backend_digest=_digest("backend"),
        handle_namespace="host",
        base_scope=scope,
        invocation_owner_id="operator-owned-test",
        presentation_owner_principal_id=harness.caller.principal_id,
        presentation_owner_session_id="local-native-operator",
        caller_session_id="session-caller",
        caller_publisher_lineage="publisher.caller",
        target_publisher_lineage="publisher.target",
        created_at=1000,
        expires_at=1100,
    )
    approval = replace(
        harness.approval,
        approval_id="approval-" + request_id,
        target=target,
        snapshot_digest=request.digest,
    )
    grant = replace(
        harness.grant,
        grant_id="grant-" + request_id,
        target=target,
        approval_id=approval.approval_id,
        scope=scope,
        lifetime=GrantLifetime.ONE_SHOT,
        max_uses=1,
    )
    decision = InteractiveApprovalDecision(
        decision_id=request_id,
        request_id=request_id,
        request_snapshot_digest=request.digest,
        decision="approved",
        actor_id="local-user",
        decided_at=1000,
        security_epoch=1,
        ui_operator_digest=_digest("verified-native-window"),
        typed_confirmation_verified=False,
        approval_id=approval.approval_id,
        grant_id=grant.grant_id,
    )
    harness.store.create_interactive_approval_request(request)
    harness.store.settle_interactive_approval(
        decision,
        approval=approval,
        grant=grant,
    )


@pytest.fixture
def world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> dict[str, Any]:
    """Use real authority and material stores; only the final opener is local."""
    original = lifecycle._principal
    is_stream = getattr(request, "param", "generate") == "stream"
    priced = getattr(request, "param", "generate") == "priced"
    function = FUNCTION.replace(".generate", ".stream") if is_stream else FUNCTION
    operation = OPERATION.replace("-generate", "-stream") if is_stream else OPERATION
    contract = CONTRACT.replace("generate.v1", "stream.v1") if is_stream else CONTRACT

    def principal(seed: str, *, operation: str = "invoke"):
        value = original(seed, operation=operation)
        if seed == "b":
            return replace(
                value,
                function_id=function,
                operation_id=(
                    OPERATION.replace("-generate", "-stream")
                    if is_stream
                    else OPERATION
                ),
            )
        return value

    monkeypatch.setattr(lifecycle, "_principal", principal)
    harness = lifecycle._Harness(tmp_path / "primary", max_uses=100)
    credential = HostCredentialMaterialStore(user_data_root=tmp_path / "material")
    policy = GenerationBudgetPolicy(
        enrollment_id="host-test-enrollment-1",
        profile_id="profile-1",
        consumer_pack_id="rumi_provider_adapters_pack",
        connection_id="connection/openrouter:test-only",
        credential_handle="credential:synthetic-fresh-test-grant",
        credential_key_version=KEY_VERSION,
        endpoint=ENDPOINT,
        model="stepfun/step-5-preview" if priced else "google/gemma-4-31b-it:free",
        max_requests=3,
        max_output_tokens=2048,
        max_prompt_price_microusd_per_million=1_000_000 if priced else 0,
        max_completion_price_microusd_per_million=2_700_000 if priced else 0,
        external_key_limit_microusd=2_000_000 if priced else 0,
    )
    credential.create(
        secret_material={"api_key": "synthetic-test-only-no-real-key"},
        profile_id="profile-1",
        consumer_pack_id="rumi_provider_adapters_pack",
        provider_instance_id="connection/openrouter:test-only",
        scopes=["ai.generate", "ai.stream"],
        credential_handle=policy.credential_handle,
        generation_budget_binding={
            "enrollment_id": policy.enrollment_id,
            "policy_digest": policy.digest,
        },
    )
    witness = AuthorityStore(tmp_path / "witness" / "witness.sqlite3")
    guard = HostGenerationBudgetAdmission(
        policy=policy,
        authority_store=harness.store,
        witness_store=witness,
    )
    approved_enrollment(harness, policy)
    guard.enroll_after_interactive_approval(
        approval_request_id="operator-enrollment-1",
        now=1000,
    )
    return dict(
        harness=harness,
        credential=credential,
        policy=policy,
        witness=witness,
        guard=guard,
        calls=[],
        count=0,
        is_stream=is_stream,
        contract=contract,
        operation=operation,
    )


def transport(world: dict[str, Any], *, fail: bool = False):
    """Authorize a fresh envelope, retaining the same credential and budget."""
    harness, policy = world["harness"], world["policy"]
    world["count"] += 1
    context = _context(harness, request_id=f"request-{world['count']}")
    digest = _digest(context.request_id)
    authority = _adapter(harness)
    _, final = _queries(harness, context, digest)
    lease = authority.authorize_and_issue_lease(final)
    authority.recheck_effect_boundary(
        context,
        OpaqueAuthorityRef(harness.target.principal_id),
        lease,
    )
    envelope = RequestEnvelope(
        context=context,
        target_principal=OpaqueAuthorityRef(harness.target.principal_id),
        target_domain=OpaqueAuthorityRef(harness.target_domain.domain_id),
        contract_id=world["contract"],
        contract_version="1.0.0",
        operation_id=world["operation"],
        payload={},
        request_digest=digest,
        deadline_monotonic=9_999_999_999,
        lease=lease,
        idempotency_key=None,
    )
    owner = HostBoundCredentialTransport.from_authorized_envelope(
        envelope,
        provider_principal=harness.target,
        store=world["credential"],
        authority_store=harness.store,
        credential_handle=policy.credential_handle,
        credential_key_version=policy.credential_key_version,
        provider_instance_id=policy.connection_id,
        credential_scope="ai.stream" if world["is_stream"] else "ai.generate",
        credential_purpose="provider.invoke",
        endpoint_origin="https://openrouter.ai",
        current_security_epoch=lambda: harness.store.security_epoch,
        consumer_pack_id=policy.consumer_pack_id,
        generation_admission=world["guard"],
    )

    def opener(request, **kwargs):
        world["calls"].append(json.loads(request.data))
        if fail:
            raise TimeoutError("synthetic reply outcome unknown")
        if world["is_stream"]:
            return Response(
                b'data: {"choices":[{"index":0,"delta":{"content":"ok"},'
                b'"finish_reason":null}]}\n\n'
                b'data: {"choices":[{"index":0,"delta":{},'
                b'"finish_reason":"stop"}]}\n\n'
                b"data: [DONE]\n\n"
            )
        return Response()

    owner._opener = opener
    arguments = dict(
        endpoint=ENDPOINT,
        headers={},
        body={
            "model": policy.model,
            "messages": [],
            "max_tokens": 2048,
            "provider": {
                "allow_fallbacks": False,
                "max_price": {
                    "prompt": policy.max_prompt_price_microusd_per_million / 1_000_000,
                    "completion": policy.max_completion_price_microusd_per_million / 1_000_000,
                },
            },
        },
        credential_handle=policy.credential_handle,
        provider_instance_id=policy.connection_id,
        credential_scope="ai.stream" if world["is_stream"] else "ai.generate",
        credential_scheme="bearer",
        deadline=9_999_999_999,
    )
    return owner, arguments


@pytest.mark.parametrize("world", ["generate", "priced"], indirect=True)
def test_three_posts_then_fourth_denied_before_opener(world):
    for _ in range(3):
        owner, arguments = transport(world)
        assert owner.post_json(**arguments) == {"choices": []}
    owner, arguments = transport(world)
    with pytest.raises(CredentialTransportDenied) as denial:
        owner.post_json(**arguments)
    assert denial.value.code == "generation_budget_exhausted"
    assert len(world["calls"]) == 3


@pytest.mark.parametrize("world", ["generate", "priced"], indirect=True)
def test_timeout_and_restart_do_not_refund(world):
    owner, arguments = transport(world, fail=True)
    with pytest.raises(CredentialTransportDenied):
        owner.post_json(**arguments)
    policy, harness = world["policy"], world["harness"]
    world["guard"] = HostGenerationBudgetAdmission(
        policy=policy,
        authority_store=AuthorityStore(harness.store.path, clock=harness.clock),
        witness_store=AuthorityStore(world["witness"].path),
    )
    for _ in range(2):
        owner, arguments = transport(world)
        owner.post_json(**arguments)
    owner, arguments = transport(world)
    with pytest.raises(CredentialTransportDenied):
        owner.post_json(**arguments)
    assert len(world["calls"]) == 3


def test_duplicate_attempt_is_never_send_permission(world):
    owner, arguments = transport(world)
    owner.post_json(**arguments)
    with pytest.raises(CredentialTransportDenied) as denial:
        world["guard"].reserve(
            binding=owner.binding,
            contract_id=CONTRACT,
            endpoint=ENDPOINT,
            body=arguments["body"],
        )
    assert denial.value.code == "generation_budget_replay"
    assert len(world["calls"]) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        {"max_tokens": 2049},
        {"max_tokens": True},
        {"max_completion_tokens": 2048},
        {"model": "paid-model"},
        {"models": ["another-model:free"]},
        {"n": 2},
        {
            "provider": {
                "allow_fallbacks": True,
                "max_price": {"prompt": 0, "completion": 0},
            }
        },
        {
            "provider": {
                "allow_fallbacks": False,
                "max_price": {"prompt": 0.01, "completion": 0},
            }
        },
        {
            "provider": {
                "allow_fallbacks": False,
                "max_price": {"prompt": False, "completion": 0},
            }
        },
        {"extra_body": {"max_tokens": 4096}},
    ],
)
def test_guest_body_cannot_expand_policy(world, mutation):
    owner, arguments = transport(world)
    arguments["body"].update(mutation)
    with pytest.raises(CredentialTransportDenied):
        owner.post_json(**arguments)
    assert world["calls"] == []
    revision, _ = world["witness"].get_host_pending_effect(world["policy"].effect_id)
    assert revision == 1


@pytest.mark.parametrize("world", ["priced"], indirect=True)
@pytest.mark.parametrize("effort", ["low", "medium", "high"])
@pytest.mark.parametrize("control", ["thinking_level", "reasoning_effort"])
def test_priced_native_compiler_payload_reaches_guard(
    world, effort: str, control: str,
) -> None:
    from ecosystem.rumi_provider_adapters_pack.domain.ai_client.provider_compiler.openai_chat import (
        compile_chat_parameters,
    )
    from ecosystem.rumi_provider_registry_pack.runtime.model_access import (
        compile_connection_parameters,
    )
    from ecosystem.rumi_provider_registry_pack.runtime.provider_filters import (
        CAPABILITY_REVISION,
    )

    policy = world["policy"]
    connection = {
        "adapter_id": "openai-compatible",
        "endpoint": "https://openrouter.ai/api/v1",
        "metadata": {"catalog_provider_id": "openrouter"},
        "model_access": {
            "version": "tobkiri.connection-model-access.v1",
            "mode": "explicit",
            "model_ids": [policy.model],
            "native_filters": {
                "revision": CAPABILITY_REVISION,
                "routing": {"max_price": {"prompt": "1", "completion": "2.7"}},
            },
        },
    }
    policy_parameters = compile_connection_parameters(
        connection,
        policy.model,
        {"max_tokens": 2048, control: effort},
    )
    compiled = compile_chat_parameters(policy_parameters, endpoint=ENDPOINT)
    assert compiled["reasoning_effort"] == effort
    assert "thinking_level" not in compiled
    assert compiled["provider"] == {
        "allow_fallbacks": False,
        "max_price": {"prompt": 1, "completion": 2.7},
    }
    assert compiled["provider"]["allow_fallbacks"] is False
    owner, arguments = transport(world)
    arguments["body"].update(compiled)
    assert owner.post_json(**arguments) == {"choices": []}
    assert len(world["calls"]) == 1


@pytest.mark.parametrize("world", ["generate", "priced"], indirect=True)
@pytest.mark.parametrize("effort", ["none", "xhigh", "max", "", True, 1, {}, []])
def test_reasoning_cannot_expand_enrollment(world, effort: Any) -> None:
    owner, arguments = transport(world)
    arguments["body"]["reasoning_effort"] = effort
    with pytest.raises(CredentialTransportDenied):
        owner.post_json(**arguments)
    assert world["calls"] == []
    assert world["witness"].get_host_pending_effect(world["policy"].effect_id)[0] == 1


def test_legacy_zero_enrollment_still_rejects_supported_reasoning(world) -> None:
    owner, arguments = transport(world)
    arguments["body"]["reasoning_effort"] = "low"
    with pytest.raises(CredentialTransportDenied):
        owner.post_json(**arguments)
    assert world["calls"] == []


@pytest.mark.parametrize("world", ["priced"], indirect=True)
@pytest.mark.parametrize(
    "maximum, fallback",
    [
        ({"prompt": 0, "completion": 0}, False),
        ({"prompt": 0.000001, "completion": 0.0000027}, False),
        ({"prompt": 1.01, "completion": 2.7}, False),
        ({"prompt": 0.99, "completion": 2.7}, False),
        ({"prompt": 1, "completion": 2.71}, False),
        ({"prompt": 1, "completion": 2.69}, False),
        ({"prompt": True, "completion": 2.7}, False),
        ({"prompt": float("nan"), "completion": 2.7}, False),
        ({"prompt": float("inf"), "completion": 2.7}, False),
        ({"prompt": 1, "completion": 2.7, "request": 0}, False),
        ({"prompt": 1, "completion": 2.7}, True),
    ],
)
def test_priced_policy_rejects_different_rates_or_fallback(
    world,
    maximum,
    fallback,
) -> None:
    owner, arguments = transport(world)
    arguments["body"]["provider"] = {
        "max_price": maximum,
        "allow_fallbacks": fallback,
    }
    with pytest.raises(CredentialTransportDenied):
        owner.post_json(**arguments)
    assert world["calls"] == []
    assert world["witness"].get_host_pending_effect(world["policy"].effect_id)[0] == 1


@pytest.mark.parametrize("field", PRICE_POLICY_FIELDS)
@pytest.mark.parametrize("value", [True, 1.0, float("nan"), float("inf"), -1, 2**53, "1", None])
def test_policy_prices_require_bounded_integer_units(world, field, value) -> None:
    from core_runtime.native_generation_budget import NativeGenerationBudgetTemplate

    with pytest.raises(ValueError):
        replace(world["policy"], **{field: value})
    with pytest.raises(ValueError):
        NativeGenerationBudgetTemplate("defaults", "provider.main", **{field: value})


def test_legacy_zero_policy_serialization_and_digest_are_unchanged(world) -> None:
    expected = {
        "enrollment_id": "host-test-enrollment-1",
        "profile_id": "profile-1",
        "consumer_pack_id": "rumi_provider_adapters_pack",
        "connection_id": "connection/openrouter:test-only",
        "credential_handle": "credential:synthetic-fresh-test-grant",
        "credential_key_version": KEY_VERSION,
        "endpoint": ENDPOINT,
        "model": "google/gemma-4-31b-it:free",
        "max_requests": 3,
        "max_output_tokens": 2048,
    }
    assert world["policy"].to_dict() == expected
    assert world["policy"].digest == authority_digest(expected)


@pytest.mark.parametrize(
    "changed",
    [
        {"provider_function_id": "legacy.direct.compiler"},
        {"provider_instance_id": "new-connection"},
        {"credential_handle": "credential:new-grant"},
        {"credential_scope": "embedding"},
        {"profile_id": "another-profile"},
    ],
)
def test_alternate_host_routes_cannot_use_test_enrollment(world, changed):
    owner, arguments = transport(world)
    with pytest.raises(CredentialTransportDenied):
        world["guard"].reserve(
            binding=replace(owner.binding, **changed),
            contract_id=CONTRACT,
            endpoint=ENDPOINT,
            body=arguments["body"],
        )
    assert world["calls"] == []


@pytest.mark.parametrize("ending", ["deadline", "revoked", "credential_missing"])
def test_existing_host_checks_still_precede_reservation(world, ending):
    owner, arguments = transport(world)
    if ending == "deadline":
        arguments["deadline"] = 0
    elif ending == "revoked":
        world["harness"].store.revoke(
            target_kind="grant", target_id="grant-1", reason="test"
        )
    else:
        world["credential"].revoke(
            world["policy"].credential_handle,
            profile_id="profile-1",
        )
    with pytest.raises(CredentialTransportDenied):
        owner.post_json(**arguments)
    assert world["calls"] == []
    assert world["witness"].get_host_pending_effect(world["policy"].effect_id)[0] == 1


def test_missing_and_rolled_back_ledger_fail_closed(world):
    store, policy = world["witness"], world["policy"]
    with sqlite3.connect(store.path) as connection:
        old = connection.execute(
            "SELECT * FROM host_pending_effects WHERE effect_id=?",
            (policy.effect_id,),
        ).fetchone()
    owner, arguments = transport(world)
    owner.post_json(**arguments)
    # Restore only a valid authenticated snapshot of the witness row. HMAC
    # validity cannot disguise disagreement with the primary revision.
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "DELETE FROM host_pending_effects WHERE effect_id=?", (policy.effect_id,)
        )
        connection.execute(
            "INSERT INTO host_pending_effects VALUES (?,?,?,?,?,?,?)", old
        )
    owner, arguments = transport(world)
    with pytest.raises(CredentialTransportDenied):
        owner.post_json(**arguments)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "DELETE FROM host_pending_effects WHERE effect_id=?", (policy.effect_id,)
        )
    owner, arguments = transport(world)
    with pytest.raises(CredentialTransportDenied):
        owner.post_json(**arguments)
    assert len(world["calls"]) == 1


def test_interrupted_second_commit_is_terminal_not_refunded(world, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("synthetic storage unavailable")

    monkeypatch.setattr(world["witness"], "compare_and_swap_host_pending_effect", unavailable)
    owner, arguments = transport(world)
    with pytest.raises(CredentialTransportDenied):
        owner.post_json(**arguments)
    assert world["harness"].store.get_host_pending_effect(world["policy"].effect_id)[0] == 2
    assert world["witness"].get_host_pending_effect(world["policy"].effect_id)[0] == 1
    monkeypatch.undo()
    owner, arguments = transport(world)
    with pytest.raises(CredentialTransportDenied):
        owner.post_json(**arguments)
    assert world["calls"] == []


def test_competing_reservations_cannot_exceed_three(world):
    requests = [transport(world) for _ in range(8)]

    def attempt(item):
        owner, arguments = item
        try:
            world["guard"].reserve(
                binding=owner.binding,
                contract_id=CONTRACT,
                endpoint=ENDPOINT,
                body=arguments["body"],
            )
            return True
        except CredentialTransportDenied:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, requests))
    assert 1 <= sum(results) <= 3
    revision, state = world["harness"].store.get_host_pending_effect(
        world["policy"].effect_id
    )
    assert revision <= 4 and len(state["attempts"]) <= 3


def test_enrollment_replay_or_new_credential_requires_new_native_decision(world):
    guard = world["guard"]
    with pytest.raises(CredentialTransportDenied):
        guard.enroll_after_interactive_approval(
            approval_request_id="operator-enrollment-1",
            now=1000,
        )
    other = HostGenerationBudgetAdmission(
        policy=replace(
            world["policy"],
            enrollment_id="new-budget",
            credential_handle="credential:new-grant",
        ),
        authority_store=world["harness"].store,
        witness_store=world["witness"],
    )
    with pytest.raises(CredentialTransportDenied):
        other.enroll_after_interactive_approval(
            approval_request_id="operator-enrollment-1",
            now=1000,
        )
    with pytest.raises(CredentialTransportDenied):
        other.enroll_after_interactive_approval(
            approval_request_id="client-approved-true",
            now=1000,
        )


def test_budget_denial_crosses_pack_boundary_as_nonretryable(world):
    class Dispatch:
        def invoke(self, *args, **kwargs):
            return {}

        def provider_metadata(self, *args):
            return ()

    for _ in range(3):
        owner, arguments = transport(world)
        owner.post_json(**arguments)
    owner, arguments = transport(world)
    client = GlobalContractClient(
        Dispatch(),
        frozenset({CONTRACT}),
        world["policy"].consumer_pack_id,
        host_credential_transport=owner,
    )
    with pytest.raises(HostGenerationBudgetError):
        client.post_json_with_credential(**arguments)
    owner, arguments = transport(world)
    client = GlobalContractClient(
        Dispatch(),
        frozenset({CONTRACT}),
        world["policy"].consumer_pack_id,
        host_credential_transport=owner,
    )
    with pytest.raises(GlobalContractInvocationError) as failure:
        adapter._post(
            client,
            ENDPOINT,
            {},
            arguments["body"],
            {"deadline": arguments["deadline"]},
            connection={"provider_instance_id": world["policy"].connection_id},
            credential_handle=arguments["credential_handle"],
            credential_scope="ai.generate",
            credential_scheme="bearer",
        )
    assert failure.value.code == "generation_budget_denied"
    decision = create_failover_operation(None)(
        "decide",
        {
            "error_code": failure.value.code,
            "allow_failover": True,
            "idempotency_key": "test",
            "attempt": 1,
            "candidate_count": 8,
            "deadline": 2000,
            "decision_time": 1000,
        },
    )
    assert decision["allowed"] is False
    assert decision["checks"]["retryable_error"] is False
    assert len(world["calls"]) == 3


@pytest.mark.parametrize("world", ["stream"], indirect=True)
def test_streaming_uses_same_three_attempt_boundary_and_terminal_error(world):
    for _ in range(3):
        owner, arguments = transport(world)
        events = []
        assert owner.stream_json(**arguments, on_event=events.append) == {
            "stream_complete": True,
        }
        assert events[-1]["done"] is True
    owner, arguments = transport(world)
    client = GlobalContractClient(
        object(),
        frozenset({world["contract"]}),
        world["policy"].consumer_pack_id,
        host_credential_transport=owner,
    )
    with pytest.raises(GlobalContractInvocationError) as failure:
        streaming.stream_request(
            client,
            {"deadline": arguments["deadline"]},
            {"provider_instance_id": world["policy"].connection_id},
            body=arguments["body"],
            endpoint=ENDPOINT,
            headers={},
            credential_handle=arguments["credential_handle"],
            credential_scope="ai.stream",
        )
    assert failure.value.code == "generation_budget_denied"
    assert len(world["calls"]) == 3


@pytest.mark.parametrize("damage", ["delete_file", "tamper_ciphertext"])
def test_host_private_persistence_damage_denies_before_post(world, damage):
    witness = world["witness"]
    if damage == "delete_file":
        witness.path.unlink()
    else:
        with sqlite3.connect(witness.path) as connection:
            connection.execute(
                "UPDATE host_pending_effects SET encrypted_payload=? WHERE effect_id=?",
                (b"not-authenticated", world["policy"].effect_id),
            )
    owner, arguments = transport(world)
    with pytest.raises(CredentialTransportDenied):
        owner.post_json(**arguments)
    assert world["calls"] == []


def _process_reservation(
    primary_path: str,
    witness_path: str,
    policy: GenerationBudgetPolicy,
    binding: Any,
    body: dict[str, Any],
    ready: Any,
    results: Any,
) -> None:
    """Open fresh process-owned Host stores rather than inheriting handles."""
    guard = HostGenerationBudgetAdmission(
        policy=policy,
        authority_store=AuthorityStore(primary_path, clock=lambda: 1000.0),
        witness_store=AuthorityStore(witness_path),
    )
    ready.wait(20)
    try:
        guard.reserve(
            binding=binding, contract_id=CONTRACT, endpoint=ENDPOINT, body=body
        )
        results.put(True)
    except CredentialTransportDenied:
        results.put(False)


def test_independent_processes_cannot_over_reserve(world):
    context = multiprocessing.get_context("spawn")
    ready, results = context.Event(), context.Queue()
    processes = []
    for _ in range(4):
        owner, arguments = transport(world)
        process = context.Process(
            target=_process_reservation,
            args=(
                str(world["harness"].store.path),
                str(world["witness"].path),
                world["policy"],
                owner.binding,
                arguments["body"],
                ready,
                results,
            ),
        )
        process.start()
        processes.append(process)
    ready.set()
    try:
        outcomes = [results.get(timeout=30) for _ in processes]
        assert 1 <= sum(outcomes) <= 3
    finally:
        for process in processes:
            process.join(30)
            if process.is_alive():
                process.terminate()
                process.join(5)
            assert process.exitcode == 0
    revision, state = world["harness"].store.get_host_pending_effect(
        world["policy"].effect_id
    )
    assert revision <= 4 and len(state["attempts"]) <= 3


def test_restart_without_host_guard_cannot_resolve_enrolled_credential(world):
    owner, arguments = transport(world)
    owner._generation_admission = None
    with pytest.raises(CredentialTransportDenied) as failure:
        owner.post_json(**arguments)
    assert failure.value.code == "generation_budget_invalid"
    assert world["calls"] == []
    assert world["witness"].get_host_pending_effect(world["policy"].effect_id)[0] == 1


def test_legacy_direct_resolve_and_handle_replacement_are_denied(world):
    policy, credential = world["policy"], world["credential"]
    with pytest.raises(PermissionError):
        credential.resolve(
            policy.credential_handle,
            consumer_pack_id=policy.consumer_pack_id,
            provider_instance_id=policy.connection_id,
            scope="ai.generate",
            profile_id=policy.profile_id,
            key_version=policy.credential_key_version,
        )
    with pytest.raises(PermissionError):
        credential.create(
            secret_material={"api_key": "synthetic-replacement"},
            consumer_pack_id=policy.consumer_pack_id,
            provider_instance_id=policy.connection_id,
            scopes=["ai.generate"],
            profile_id=policy.profile_id,
            credential_handle=policy.credential_handle,
            generation_budget_binding=world["guard"].credential_budget_binding,
        )
    assert world["calls"] == []


def test_existing_unbounded_transport_preserves_lease_and_material_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the existing pure compatibility checks against the proposal overlay."""
    from tests import test_credential_broker_pack as existing

    existing.test_credential_material_is_encrypted_and_listing_is_redacted(
        tmp_path / "encryption",
    )
    existing.test_generic_resolution_is_denied_and_host_transport_applies_once(
        tmp_path / "one-shot",
        monkeypatch,
    )
    existing.test_host_transport_rejects_missing_approval_and_revocation(
        tmp_path / "approval",
    )


def test_existing_global_transport_exception_remains_severed(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The new terminal code does not weaken ordinary failure redaction."""
    from tests import test_credential_broker_pack as existing

    existing.test_global_client_normalizes_injected_transport_exception_without_chain(
        caplog,
        capsys,
    )


def test_missing_host_guard_remains_terminal_through_global_client(world):
    """A required credential marker denies before decrypt and forbids retries."""
    owner, arguments = transport(world)
    owner._generation_admission = None
    client = GlobalContractClient(
        object(), frozenset({CONTRACT}), world["policy"].consumer_pack_id,
        host_credential_transport=owner,
    )
    with pytest.raises(HostGenerationBudgetError):
        client.post_json_with_credential(**arguments)
    assert world["calls"] == []
