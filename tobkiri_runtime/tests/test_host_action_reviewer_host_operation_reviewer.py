"""Deterministic canonical-provider fixtures, never live-model evidence."""

import json
import time
from core_runtime.bootstrap.action_review_v4.host_operation_reviewer import HostOperationReviewer

SCOPE = {"profile_id": "p", "authority_id": "private-authority-handle"}
REVIEW_SCOPE = {
    "allowed_operations": ["file.write"],
    "allowed_roots": ["/workspace"],
    "limits": {"max_bytes": 256},
}
ARGS = {"path": "hello.txt", "content": "hello"}


def response(request, decision="approve", reason="Within requested scope."):
    return {
        "status": "ok",
        "request_id": request["request_id"],
        "model_id": "fixture/model",
        "provider_instance_id": "fixture",
        "output": json.dumps({"decision": decision, "reason": reason}),
        "tool_intents": [],
        "finish_reason": "stop",
    }


def review(callback, **overrides):
    values = dict(
        operation="file.write",
        arguments=ARGS,
        scope=SCOPE,
        turn_id="t1",
        model_reference="side-model",
        review_scope=REVIEW_SCOPE,
    )
    values.update(overrides)
    return HostOperationReviewer(callback, timeout_seconds=0.02).review(**values)


def test_exact_binding_and_no_authority_outbound():
    captured = []

    def complete(request):
        captured.append(request)
        return response(request)

    verdict = review(complete)
    assert verdict.decision == "approve"
    assert verdict.matches(
        "file.write",
        ARGS,
        scope=SCOPE,
        turn_id="t1",
        model_reference="side-model",
        review_scope=REVIEW_SCOPE,
    )
    assert not verdict.matches(
        "file.write",
        {**ARGS, "content": "changed"},
        scope=SCOPE,
        turn_id="t1",
        review_scope=REVIEW_SCOPE,
    )
    assert not verdict.matches(
        "file.write", ARGS, scope=SCOPE, turn_id="t2", review_scope=REVIEW_SCOPE
    )
    assert not verdict.matches(
        "file.write",
        ARGS,
        scope=SCOPE,
        turn_id="t1",
        model_reference="another",
        review_scope=REVIEW_SCOPE,
    )
    assert captured[0]["tools"] == []
    payload = json.loads(captured[0]["messages"][1]["content"])
    assert payload["arguments"] == ARGS
    assert "private-authority-handle" not in json.dumps(captured)
    assert review(complete).review_id != verdict.review_id


def test_missing_model_and_secrets_stop_without_provider():
    def unexpected(request):
        raise AssertionError("Must not reach provider")

    assert review(unexpected, model_reference="").decision == "unavailable"
    assert review(unexpected, arguments={"password": "secret"}).decision == "unavailable"


def test_provider_error_and_timeout_stop_without_exception_leaks():
    def error(request):
        raise RuntimeError("secret credentials")

    verdict = review(error)
    assert verdict.decision == "unavailable"
    assert "secret" not in verdict.reason

    def slow(request):
        time.sleep(0.1)
        return response(request)

    started = time.monotonic()
    assert review(slow).decision == "unavailable"
    assert time.monotonic() - started < 0.09


def test_deny_and_malformed_output_fail_closed():
    assert review(lambda r: response(r, "deny", "Destructive scope.")).decision == "deny"
    for output in [
        '```json\n{"decision":"approve","reason":"ok"}\n```',
        '{"decision":"approve"}',
        '{"decision":"approve","reason":""}',
        '{"decision":"escalate","reason":"ok"}',
        '{"decision":{},"reason":"ok"}',
        '{"decision":[],"reason":"ok"}',
    ]:
        assert review(lambda r: {**response(r), "output": output}).decision == "unavailable"
    assert review(lambda r: {**response(r), "request_id": "replay"}).decision == "unavailable"
    assert review(lambda r: {**response(r), "model_id": ""}).decision == "unavailable"
    assert (
        review(lambda r: {**response(r), "tool_intents": [{"operation": "exec"}]}).decision
        == "unavailable"
    )


def test_capacity_retained_until_late_provider_returns():
    import threading

    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocked(request):
        calls.append(request)
        entered.set()
        release.wait(2)
        return response(request)

    reviewer = HostOperationReviewer(blocked, timeout_seconds=0.02, max_concurrent=1)
    values = dict(
        operation="file.write",
        arguments=ARGS,
        scope=SCOPE,
        turn_id="t1",
        model_reference="side-model",
        review_scope=REVIEW_SCOPE,
    )
    try:
        assert reviewer.review(**values).decision == "unavailable"
        assert entered.is_set()
        verdict = reviewer.review(**values)
        assert "capacity" in verdict.reason
        assert len(calls) == 1
    finally:
        release.set()


def test_cancellation_discards_late_approval():
    import threading

    entered, release, cancelled = (threading.Event() for _ in range(3))
    output = []

    def blocked(request):
        entered.set()
        release.wait(2)
        return response(request)

    reviewer = HostOperationReviewer(blocked, timeout_seconds=1, max_concurrent=1)
    values = dict(
        operation="file.write",
        arguments=ARGS,
        scope=SCOPE,
        turn_id="t1",
        model_reference="side-model",
        review_scope=REVIEW_SCOPE,
    )
    caller = threading.Thread(
        target=lambda: output.append(reviewer.review(**values, cancellation=cancelled))
    )
    caller.start()
    try:
        assert entered.wait(0.5)
        cancelled.set()
        caller.join(0.5)
        assert not caller.is_alive()
        assert output[0].decision == "unavailable"
        release.set()
        assert output[0].decision == "unavailable"
    finally:
        release.set()
        caller.join(1)


def test_stale_capture_and_close_fail_closed():
    import threading

    entered, release = threading.Event(), threading.Event()
    current = [True]
    output = []
    args = dict(ARGS)

    def blocked(request):
        entered.set()
        release.wait(2)
        return response(request)

    reviewer = HostOperationReviewer(blocked, timeout_seconds=1)
    values = dict(
        operation="file.write",
        arguments=args,
        scope=SCOPE,
        turn_id="t1",
        model_reference="side-model",
        review_scope=REVIEW_SCOPE,
    )
    caller = threading.Thread(
        target=lambda: output.append(
            reviewer.review(**values, capture_is_current=lambda: current[0])
        )
    )
    caller.start()
    try:
        assert entered.wait(0.5)
        args["content"] = "mutated after Host snapshot"
        caller.join(0.5)
        assert not caller.is_alive()
        assert output[0].decision == "unavailable"
        current[0] = False
        assert (
            reviewer.review(**values, capture_is_current=lambda: current[0]).decision
            == "unavailable"
        )
        reviewer.close()
        assert reviewer.review(**values).decision == "unavailable"
    finally:
        release.set()
        caller.join(1)


def test_host_boundaries_reach_provider_and_bind_verdict():
    seen = []

    def out_of_scope(request):
        payload = json.loads(request["messages"][1]["content"])
        seen.append(payload)
        assert payload["review_scope"] == REVIEW_SCOPE
        assert payload["arguments"]["path"] == "/elsewhere/private.txt"
        return response(request, "deny", "Destination exceeds the allowed root.")

    verdict = review(out_of_scope, arguments={"path": "/elsewhere/private.txt"})
    assert verdict.decision == "deny"
    assert verdict.review_scope_digest
    assert not verdict.matches(
        "file.write",
        {"path": "/elsewhere/private.txt"},
        scope=SCOPE,
        turn_id="t1",
        review_scope={**REVIEW_SCOPE, "allowed_roots": ["/"]},
    )
    assert review(lambda r: response(r), review_scope=None).decision == "unavailable"
    assert (
        review(lambda r: response(r), review_scope={"allowed_operations": ["file.write"]}).decision
        == "unavailable"
    )
    assert (
        review(
            lambda r: response(r), review_scope={**REVIEW_SCOPE, "grant": "opaque:handle"}
        ).decision
        == "unavailable"
    )


def test_recursive_sensitive_arguments_and_reason_scrubbing():
    calls = []

    def complete(request):
        calls.append(request)
        return response(request, "deny", "Private-user-content needs review.")

    verdict = review(complete, arguments={"nested": [{"password": "secret"}]})
    assert verdict.decision == "unavailable"
    assert calls == []
    verdict = review(complete, arguments={"nested": [{"content": "Private-user-content"}]})
    assert verdict.decision == "deny"
    assert "Private-user-content" not in verdict.reason
