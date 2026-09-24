from __future__ import annotations

import pytest

from ecosystem.defaultspack.transport import http
from domain.safety import local_auth_exchange
from domain.safety.local_auth_exchange import (
    LOCAL_AUTH_SCOPE,
    LocalAuthAudience,
    LocalAuthExchangeStore,
)
from domain.safety.local_auth_secret import configured_local_auth_environment_tokens


def audience(**changes: str) -> LocalAuthAudience:
    values = {
        "origin": "http://127.0.0.1:8766",
        "window_id": "defaultspack-main",
        "process_id": "launcher-42",
        "device_id": "device-42",
        "nonce": "nonce-42",
        "scope": LOCAL_AUTH_SCOPE,
    }
    values.update(changes)
    return LocalAuthAudience.from_mapping(values)


def bound_headers(token: str, target: LocalAuthAudience) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Origin": target.origin,
        "X-Rumi-Local-Auth-Window": target.window_id,
        "X-Rumi-Local-Auth-Process": target.process_id,
        "X-Rumi-Local-Auth-Device": target.device_id,
        "X-Rumi-Local-Auth-Nonce": target.nonce,
        "X-Rumi-Local-Auth-Scope": target.scope,
    }


def test_exchange_is_single_use_and_session_is_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    store = LocalAuthExchangeStore()
    monkeypatch.setattr(local_auth_exchange, "_STORE", store)
    target = audience()
    issued = store.issue("local-ui:subject", target)
    redeemed = store.redeem(str(issued["exchange_code"]), target)
    token = str(redeemed["session_token"])

    assert store.authorize(token, target)
    assert http._local_auth_token_authorized(bound_headers(token, target))
    assert not http._local_auth_token_authorized(
        bound_headers(token, audience(window_id="other-window"))
    )
    assert not http._local_auth_token_authorized(
        bound_headers(token, audience(origin="http://localhost:8766"))
    )
    with pytest.raises(ValueError, match="already consumed"):
        store.redeem(str(issued["exchange_code"]), target)


def test_legacy_launcher_tokens_are_deduplicated_at_safety_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUMI_DEFAULTSPACK_LOCAL_TOKEN", "launcher-token")
    monkeypatch.setenv("RUMI_API_TOKEN", "launcher-token")
    monkeypatch.setenv("RUMI_TOKEN", "issued-token")

    assert configured_local_auth_environment_tokens() == (
        "launcher-token",
        "issued-token",
    )


def test_wrong_audience_rejects_and_revokes_exchange() -> None:
    store = LocalAuthExchangeStore()
    target = audience()
    issued = store.issue("local-ui:subject", target)
    code = str(issued["exchange_code"])

    with pytest.raises(PermissionError, match="audience"):
        store.redeem(code, audience(nonce="attacker-nonce"))
    with pytest.raises(ValueError, match="already consumed"):
        store.redeem(code, target)


def test_exchange_and_session_expire_without_persistence() -> None:
    now = [1000.0]
    store = LocalAuthExchangeStore(now=lambda: now[0])
    target = audience()
    first = store.issue("local-ui:subject", target)
    now[0] += local_auth_exchange.EXCHANGE_TTL_SECONDS + 1
    with pytest.raises(ValueError, match="expired"):
        store.redeem(str(first["exchange_code"]), target)

    second = store.issue("local-ui:subject", target)
    redeemed = store.redeem(str(second["exchange_code"]), target)
    now[0] += local_auth_exchange.SESSION_TTL_SECONDS + 1
    assert not store.authorize(str(redeemed["session_token"]), target)


@pytest.mark.parametrize(
    "changes",
    [
        {"origin": "https://example.invalid"},
        {"origin": "file://local"},
        {"origin": "http://user@127.0.0.1:8766"},
        {"scope": "admin"},
        {"window_id": ""},
        {"nonce": "x" * 161},
    ],
)
def test_audience_validation_fails_closed(changes: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        audience(**changes)
