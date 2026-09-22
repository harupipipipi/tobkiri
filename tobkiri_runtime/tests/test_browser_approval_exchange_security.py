from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.safety.browser_approval_exchange import (  # noqa: E402
    BrowserApprovalAudience,
    BrowserApprovalExchangeStore,
)
from core_runtime.host_contract import bind_host_contract  # noqa: E402


def _audience(**changes: str) -> BrowserApprovalAudience:
    values = {
        "request_id": "fake-request-1",
        "principal_id": "local-ui:fake-principal-digest",
        "device_id": "fake-device-1",
        "origin": "http://127.0.0.1:8766",
        "window_id": "fake-window-1",
        "nonce": "fake-client-nonce-1",
    }
    values.update(changes)
    return BrowserApprovalAudience(**values)


def test_exchange_stores_only_digest_and_binds_every_audience_field() -> None:
    store = BrowserApprovalExchangeStore()
    issued = store.issue(_audience(), now=100, ttl_seconds=60)
    code = issued["exchange_code"]

    assert code not in repr(store._records)
    for field, wrong_value in (
        ("request_id", "fake-request-wrong"),
        ("principal_id", "local-ui:fake-other-principal"),
        ("device_id", "fake-device-wrong"),
        ("origin", "http://localhost:8766"),
        ("window_id", "fake-window-wrong"),
        ("nonce", "fake-client-nonce-wrong"),
    ):
        result = store.redeem(code, _audience(**{field: wrong_value}), now=101)
        assert result == {"success": False, "reason": "audience_mismatch"}
    assert store.redeem(code, _audience(), now=101)["success"] is True


def test_exchange_expiry_and_nonsecret_identifier_revocation() -> None:
    store = BrowserApprovalExchangeStore()
    expired = store.issue(_audience(), now=100, ttl_seconds=15)
    assert store.redeem(expired["exchange_code"], _audience(), now=115) == {
        "success": False,
        "reason": "expired",
    }

    issued = store.issue(_audience(request_id="fake-request-2"), now=200)
    audience = _audience(request_id="fake-request-2")
    assert store.revoke_by_id(issued["exchange_id"], audience, now=201)["success"]
    assert store.redeem(issued["exchange_code"], audience, now=201) == {
        "success": False,
        "reason": "revoked",
    }


def test_sixteen_way_redeem_is_atomic_and_single_use() -> None:
    store = BrowserApprovalExchangeStore()
    issued = store.issue(_audience(), now=100)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(
            pool.map(
                lambda _index: store.redeem(
                    issued["exchange_code"], _audience(), now=101
                ),
                range(16),
            )
        )

    assert sum(result.get("success") is True for result in results) == 1
    assert sum(result.get("reason") == "consumed" for result in results) == 15


def test_redeem_vs_revoke_has_exactly_one_settlement() -> None:
    store = BrowserApprovalExchangeStore()
    issued = store.issue(_audience(), now=100)

    with ThreadPoolExecutor(max_workers=2) as pool:
        redeem = pool.submit(
            store.redeem, issued["exchange_code"], _audience(), now=101
        )
        revoke = pool.submit(
            store.revoke_by_id, issued["exchange_id"], _audience(), now=101
        )
        results = [redeem.result(), revoke.result()]

    assert sum(result.get("success") is True for result in results) == 1
    assert {result.get("reason") for result in results if not result.get("success")} in (
        {"consumed"},
        {"revoked"},
    )


def test_legacy_url_cleanup_preserves_only_nonsensitive_parameters() -> None:
    import urllib.parse

    from ecosystem.defaultspack.transport.http import (
        _legacy_browser_url_without_credentials,
    )

    parsed = urllib.parse.urlsplit(
        "/approval?request_id=fake-request-1&browser_approval_token=fake-old"
        "&approval_browser_token=fake-old-2"
    )
    clean = _legacy_browser_url_without_credentials(parsed)

    assert clean == "/approval?request_id=fake-request-1"
    assert "fake-old" not in clean


def test_exchange_transport_rejects_fake_bearer_and_other_loopback_port() -> None:
    from ecosystem.defaultspack.transport.http import (
        _browser_exchange_transport_error,
    )

    base = {
        "Authorization": "Bearer fake-configured-local-token",
        "Origin": "http://127.0.0.1:8766",
        "Host": "127.0.0.1:8766",
        "X-Rumi-CSRF": "fake-csrf-marker",
    }

    with bind_host_contract(
        {
            "schema_version": "tobkiri.host-contract.v1",
            "profile_id": "profile:test",
            "values": {"desktop_api_token": "fake-configured-local-token"},
        }
    ):
        assert _browser_exchange_transport_error(base) is None
        fake_bearer = {**base, "Authorization": "Bearer fake-attacker-token"}
        assert _browser_exchange_transport_error(fake_bearer) == (
            401,
            "local auth token required",
            "AUTH_REQUIRED",
        )
        wrong_port = {**base, "Origin": "http://127.0.0.1:9999"}
        assert _browser_exchange_transport_error(wrong_port)[2] == "ORIGIN_DENIED"
