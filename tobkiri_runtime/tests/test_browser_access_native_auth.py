"""Exact-message MAC and replay fences for native browser admission."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac

import pytest

from core_runtime.browser_access_native_auth import BrowserAccessNativeAuth

SECRET = "fixture-native-secret"
PATH = "/api/panel/browser-access/context"
BODY = b'{"request_id":"fixture"}'
NONCE = "a" * 64


def proof(timestamp="1000", nonce=NONCE, path=PATH, body=BODY):
    message = (
        f"tobkiri.browser-access.request.v1\n{timestamp}\n{nonce}\nPOST\n"
        f"{path}\n{hashlib.sha256(body).hexdigest()}"
    )
    return hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()


@pytest.fixture
def auth():
    clock = [1000.0]
    return BrowserAccessNativeAuth(SECRET, clock=lambda: clock[0]), clock


def test_valid_mac_is_one_time_and_invalid_mac_does_not_consume(auth):
    manager, _ = auth
    assert not manager.verify_request("1000", NONCE, "b" * 64, PATH, BODY)
    assert manager.verify_request("1000", NONCE, proof(), PATH, BODY)
    assert not manager.verify_request("1000", NONCE, proof(), PATH, BODY)


@pytest.mark.parametrize("timestamp", ["969", "1031", "-1000", "1000.0", "", None])
def test_timestamp_fails_closed(auth, timestamp):
    manager, _ = auth
    assert not manager.verify_request(timestamp, NONCE, proof(timestamp), PATH, BODY)


@pytest.mark.parametrize("timestamp", ["970", "1030"])
def test_clock_skew_boundary_allowed(auth, timestamp):
    manager, _ = auth
    assert manager.verify_request(timestamp, NONCE, proof(timestamp), PATH, BODY)


@pytest.mark.parametrize("nonce", ["a" * 63, "A" * 64, "g" * 64, "", None])
def test_nonce_shape_rejected(auth, nonce):
    manager, _ = auth
    assert not manager.verify_request("1000", nonce, proof(nonce=nonce), PATH, BODY)


def test_body_and_exact_path_are_bound(auth):
    manager, _ = auth
    assert not manager.verify_request("1000", NONCE, proof(), PATH, BODY + b" ")
    decision = "/api/panel/browser-access/decision"
    assert not manager.verify_request("1000", NONCE, proof(), decision, BODY)
    assert not manager.verify_request("1000", NONCE, proof(), PATH + "?x=1", BODY)
    assert not manager.verify_request(
        "1000", NONCE, proof(), "/api/panel/auth/bootstrap", BODY
    )
    assert manager.verify_request("1000", NONCE, proof(path=decision), decision, BODY)


def test_oversized_body_and_empty_secret_fail_closed(auth):
    manager, _ = auth
    body = b"x" * 4097
    assert not manager.verify_request("1000", NONCE, proof(body=body), PATH, body)
    empty = BrowserAccessNativeAuth("", clock=lambda: 1000)
    assert not empty.verify_request("1000", NONCE, proof(), PATH, BODY)
    assert empty.response_proof("1000", NONCE, PATH, 200, BODY) == ""


def test_response_mac_binds_exact_status_and_body(auth):
    manager, _ = auth
    message = (
        f"tobkiri.browser-access.response.v1\n1000\n{NONCE}\nPOST\n"
        f"{PATH}\n200\n{hashlib.sha256(BODY).hexdigest()}"
    )
    expected = hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    assert manager.response_proof("1000", NONCE, PATH, 200, BODY) == expected
    assert manager.response_proof("1000", NONCE, PATH, 401, BODY) != expected
    assert manager.response_proof("1000", NONCE, PATH, 200, BODY + b" ") != expected
    assert manager.response_proof("1000", NONCE, "/other", 200, BODY) == ""


def test_nonce_capacity_expiry_and_timestamp_replay(auth):
    manager, clock = auth
    for index in range(128):
        nonce = f"{index:064x}"
        assert manager.verify_request("1000", nonce, proof(nonce=nonce), PATH, BODY)
    nonce = "f" * 64
    assert not manager.verify_request("1000", nonce, proof(nonce=nonce), PATH, BODY)
    clock[0] = 1060
    assert not manager.verify_request("1000", NONCE, proof(), PATH, BODY)
    assert manager.verify_request("1060", nonce, proof("1060", nonce), PATH, BODY)
    assert len(manager._nonces) == 1


def test_concurrent_duplicate_mac_has_one_winner(auth):
    manager, _ = auth

    def attempt(_):
        return manager.verify_request("1000", NONCE, proof(), PATH, BODY)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(16)))
    assert sum(results) == 1
