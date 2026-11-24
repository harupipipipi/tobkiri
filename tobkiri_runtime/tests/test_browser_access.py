"""Deterministic browser admission ownership checks."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import pytest
from core_runtime.browser_access import BrowserAccessError, BrowserAccessManager
from core_runtime.panel_auth import PanelAuthBinding, PanelAuthManager

ORIGIN = "http://127.0.0.1:8766"
TARGET = "/p/defaults/chat"


@pytest.fixture
def admission():
    clock = [100.0]
    auth = PanelAuthManager(bootstrap_secret="test-bootstrap")
    manager = BrowserAccessManager(auth, clock=lambda: clock[0])
    binding = PanelAuthBinding("defaults", "revision", "activation", "plan", 1)
    return manager, auth, binding, clock


def claim(manager, request, binding, origin=ORIGIN):
    return manager.claim(request["request_id"], request["proof"], binding, origin)


def test_approval_claim_mints_exactly_one_ordinary_session(admission):
    manager, auth, binding, _ = admission
    request = manager.create(binding, ORIGIN, TARGET)
    with pytest.raises(BrowserAccessError, match="not_approved"):
        claim(manager, request, binding)
    assert not auth._active_sessions
    assert "proof" not in manager.context(request["request_id"], binding)
    assert "proof" not in manager.pending(binding)[0]
    manager.decide(request["request_id"], binding, "approved")
    session = claim(manager, request, binding)
    assert session["target"] == TARGET
    assert auth.verify_session(session["session_id"], binding)["request_scope"] == ""
    with pytest.raises(BrowserAccessError, match="invalid_request"):
        claim(manager, request, binding)
    assert len(auth._active_sessions) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("activation_id", "other"),
        ("security_epoch", 2),
        ("profile_id", "other"),
        ("profile_revision", "other"),
        ("plan_digest", "other"),
    ],
)
def test_stale_capture_cannot_read_decide_or_claim(admission, field, value):
    manager, auth, binding, _ = admission
    request = manager.create(binding, ORIGIN, TARGET)
    stale = replace(binding, **{field: value})
    for operation in (
        lambda: manager.status(request["request_id"], request["proof"], stale, ORIGIN),
        lambda: manager.context(request["request_id"], stale),
        lambda: manager.decide(request["request_id"], stale, "approved"),
        lambda: claim(manager, request, stale),
    ):
        with pytest.raises(BrowserAccessError, match="invalid_request"):
            operation()
    assert manager.pending(stale) == [] and not auth._active_sessions


def test_claim_requires_proof_and_exact_origin(admission):
    manager, auth, binding, _ = admission
    request = manager.create(binding, ORIGIN, TARGET)
    manager.decide(request["request_id"], binding, "approved")
    with pytest.raises(BrowserAccessError, match="invalid_request"):
        manager.claim(request["request_id"], "wrong", binding, ORIGIN)
    with pytest.raises(BrowserAccessError, match="invalid_request"):
        claim(manager, request, binding, "http://localhost:8766")
    assert not auth._active_sessions
    assert request["proof"] not in repr(manager._requests)


def test_denial_and_expiry_fail_closed(admission):
    manager, auth, binding, clock = admission
    request = manager.create(binding, ORIGIN, TARGET)
    manager.decide(request["request_id"], binding, "denied")
    with pytest.raises(BrowserAccessError, match="already_decided"):
        manager.decide(request["request_id"], binding, "approved")
    with pytest.raises(BrowserAccessError, match="not_approved"):
        claim(manager, request, binding)
    clock[0] += 120
    with pytest.raises(BrowserAccessError, match="invalid_request"):
        manager.status(request["request_id"], request["proof"], binding, ORIGIN)
    assert manager.pending(binding) == [] and not auth._active_sessions


def test_idempotent_create_rate_and_queue_bounds(admission):
    manager, _, binding, clock = admission
    request = manager.create(binding, ORIGIN, TARGET)
    repeated = manager.create(binding, ORIGIN, TARGET, request["proof"])
    assert request["created"] is True and repeated.pop("created") is False
    assert repeated == {
        key: value for key, value in request.items() if key != "created"
    }
    for _ in range(2):
        manager.create(binding, ORIGIN, TARGET)
    with pytest.raises(BrowserAccessError, match="rate_limited"):
        manager.create(binding, ORIGIN, TARGET)
    clock[0] += 60
    manager.create(binding, ORIGIN, TARGET)
    with pytest.raises(BrowserAccessError, match="queue_full"):
        manager.create(binding, ORIGIN, TARGET)
    clock[0] += 120
    assert manager.create(binding, ORIGIN, TARGET)["status"] == "pending"


def test_concurrent_claim_has_one_winner(admission):
    manager, auth, binding, _ = admission
    request = manager.create(binding, ORIGIN, TARGET)
    manager.decide(request["request_id"], binding, "approved")

    def attempt(_):
        try:
            return claim(manager, request, binding)
        except BrowserAccessError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(16)))
    assert sum(result is not None for result in results) == 1
    assert len(auth._active_sessions) == 1


@pytest.mark.parametrize(
    "origin,target",
    [
        ("https://evil.example", TARGET),
        ("http://127.0.0.1:8766/evil", TARGET),
        (ORIGIN, "//evil.example"),
        (ORIGIN, "/p/other/chat"),
        (ORIGIN, "/p/defaults/../other"),
        (ORIGIN, "/p/defaults/%2e%2e"),
        (ORIGIN, TARGET + "?token=secret"),
    ],
)
def test_noncanonical_context_denied(admission, origin, target):
    manager, _, binding, _ = admission
    with pytest.raises(BrowserAccessError):
        manager.create(binding, origin, target)
    assert manager.pending(binding) == []


@pytest.mark.parametrize("proof", [None, "", [], 1])
def test_missing_or_malformed_proof_never_bypasses_claim(admission, proof):
    manager, auth, binding, _ = admission
    request = manager.create(binding, ORIGIN, TARGET)
    manager.decide(request["request_id"], binding, "approved")
    with pytest.raises(BrowserAccessError, match="invalid_request"):
        manager.claim(request["request_id"], proof, binding, ORIGIN)
    with pytest.raises(BrowserAccessError, match="invalid_request"):
        manager.status(request["request_id"], proof, binding, ORIGIN)
    assert not auth._active_sessions


def test_explicit_retry_after_denial_is_fresh_and_rate_limited(admission):
    manager, _, binding, _ = admission
    request = manager.create(binding, ORIGIN, TARGET)
    for _ in range(2):
        manager.decide(request["request_id"], binding, "denied")
        previous = request
        request = manager.create(binding, ORIGIN, TARGET, request["proof"])
        assert request["created"] is True
        assert request["request_id"] != previous["request_id"]
        assert request["proof"] != previous["proof"]
    manager.decide(request["request_id"], binding, "denied")
    with pytest.raises(BrowserAccessError, match="rate_limited"):
        manager.create(binding, ORIGIN, TARGET, request["proof"])


@pytest.mark.parametrize("origin", ["http://localhost:abc", "http://[bad:8766"])
def test_malformed_origin_is_sanitized(admission, origin):
    manager, _, binding, _ = admission
    with pytest.raises(BrowserAccessError, match="invalid_request"):
        manager.create(binding, origin, TARGET)
