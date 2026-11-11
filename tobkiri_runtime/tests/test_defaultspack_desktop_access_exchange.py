from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ecosystem.defaultspack.backend.sandbox.desktop_access_exchange import (
    DesktopAccessExchange,
)
from ecosystem.defaultspack.blocks.sandbox import api


FAKE_BINDING = {
    "audience": "https://fake-audience.invalid/desktop",
    "origin": "https://fake-origin.invalid",
    "principal_id": "fake-principal-1001",
    "device_id": "fake-device-1001",
    "session_id": "fake-session-1001",
}
FAKE_SEAT = "fake-seat-1001"


def _issue(authority: DesktopAccessExchange, **overrides):
    values = {
        **FAKE_BINDING,
        "seat_id": FAKE_SEAT,
        "operations": ["desktop.read", "desktop.frame"],
        "code_ttl_seconds": 30,
        "credential_ttl_seconds": 60,
        **overrides,
    }
    return authority.issue(**values)


def test_exchange_is_one_time_atomic_and_persists_no_bearer_secrets(tmp_path) -> None:
    authority = DesktopAccessExchange(tmp_path / "fake-exchange.json")
    issued = _issue(authority)
    code = issued["exchange_code"]
    barrier = threading.Barrier(8)

    def redeem() -> dict:
        barrier.wait()
        return authority.exchange(code, context=FAKE_BINDING)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: redeem(), range(8)))

    successes = [item for item in results if item.get("ok") is True]
    assert len(successes) == 1
    assert {item.get("code") for item in results if not item.get("ok")} == {
        "DESKTOP_EXCHANGE_CODE_REPLAYED"
    }
    credential = successes[0]["session_credential"]
    persisted = (tmp_path / "fake-exchange.json").read_text(encoding="utf-8")
    assert code not in persisted
    assert credential not in persisted
    assert "code_hash" in persisted
    assert "credential_hash" in persisted


def test_exchange_rejects_every_wrong_trusted_binding(tmp_path) -> None:
    for key in FAKE_BINDING:
        authority = DesktopAccessExchange(tmp_path / f"fake-{key}.json")
        issued = _issue(authority)
        wrong = {**FAKE_BINDING, key: f"wrong-{key}-1001"}
        result = authority.exchange(issued["exchange_code"], context=wrong)
        assert result["code"] == "DESKTOP_EXCHANGE_BINDING_MISMATCH"
        valid = authority.exchange(issued["exchange_code"], context=FAKE_BINDING)
        assert valid["ok"] is True


def test_credential_rejects_wrong_seat_operation_and_each_context_binding(tmp_path) -> None:
    authority = DesktopAccessExchange(tmp_path / "fake-scope.json")
    exchanged = authority.exchange(_issue(authority)["exchange_code"], context=FAKE_BINDING)
    credential = exchanged["session_credential"]
    wrong_seat = authority.authorize(
        credential, seat_id="fake-other-seat-1001", operation="desktop.read", context=FAKE_BINDING
    )
    wrong_operation = authority.authorize(
        credential, seat_id=FAKE_SEAT, operation="desktop.delete", context=FAKE_BINDING
    )
    assert wrong_seat["code"] == "DESKTOP_SESSION_SEAT_MISMATCH"
    assert wrong_operation["code"] == "DESKTOP_OPERATION_NOT_AUTHORIZED"
    for key in FAKE_BINDING:
        wrong = {**FAKE_BINDING, key: f"wrong-{key}-1001"}
        result = authority.authorize(
            credential, seat_id=FAKE_SEAT, operation="desktop.read", context=wrong
        )
        assert result["code"] == "DESKTOP_SESSION_BINDING_MISMATCH"


def test_expiry_rotation_revocation_and_metadata_redaction(tmp_path) -> None:
    now = [1_000.0]
    authority = DesktopAccessExchange(tmp_path / "fake-lifecycle.json", clock=lambda: now[0])
    first = authority.exchange(_issue(authority)["exchange_code"], context=FAKE_BINDING)
    first_credential = first["session_credential"]
    second = authority.exchange(_issue(authority)["exchange_code"], context=FAKE_BINDING)
    assert authority.authorize(
        first_credential, seat_id=FAKE_SEAT, operation="desktop.read", context=FAKE_BINDING
    )["code"] == "DESKTOP_SESSION_CREDENTIAL_REVOKED"
    second_credential = second["session_credential"]
    metadata = authority.list_metadata(seat_id=FAKE_SEAT)
    serialized = json.dumps(metadata)
    assert "hash" not in serialized
    assert first_credential not in serialized
    assert second_credential not in serialized
    assert authority.revoke(second["credential_id"], reason="fake-policy-change") is True
    assert authority.authorize(
        second_credential, seat_id=FAKE_SEAT, operation="desktop.read", context=FAKE_BINDING
    )["code"] == "DESKTOP_SESSION_CREDENTIAL_REVOKED"
    expiring = authority.exchange(
        _issue(authority, credential_ttl_seconds=1)["exchange_code"], context=FAKE_BINDING
    )
    now[0] += 2
    assert authority.authorize(
        expiring["session_credential"], seat_id=FAKE_SEAT, operation="desktop.read", context=FAKE_BINDING
    )["code"] == "DESKTOP_SESSION_CREDENTIAL_EXPIRED"


def test_expired_code_and_seat_policy_revocation(tmp_path) -> None:
    now = [50.0]
    authority = DesktopAccessExchange(tmp_path / "fake-expiry.json", clock=lambda: now[0])
    issued = _issue(authority, code_ttl_seconds=1)
    now[0] += 2
    assert authority.exchange(issued["exchange_code"], context=FAKE_BINDING)["code"] == (
        "DESKTOP_EXCHANGE_CODE_EXPIRED"
    )
    active = authority.exchange(_issue(authority)["exchange_code"], context=FAKE_BINDING)
    assert authority.revoke_seat(FAKE_SEAT, reason="fake-destroy") == 1
    assert authority.authorize(
        active["session_credential"], seat_id=FAKE_SEAT, operation="desktop.read", context=FAKE_BINDING
    )["code"] == "DESKTOP_SESSION_CREDENTIAL_REVOKED"


def test_api_rejects_all_legacy_key_fields_with_migration_error() -> None:
    fake_service = SimpleNamespace()
    api._reset_service_for_tests(fake_service)
    payloads = [
        {"access_key": "FAKE-LEGACY-KEY-1001"},
        {"desktop_access_key": "FAKE-LEGACY-KEY-1001"},
        {"access": {"access_key": "FAKE-LEGACY-KEY-1001"}},
        {"_headers": {"X-Rumi-Desktop-Access-Key": "FAKE-LEGACY-KEY-1001"}},
    ]
    try:
        for fields in payloads:
            result = api.run({"_handler": "desktop_get", "seat_id": FAKE_SEAT, **fields})
            assert result["error"]["code"] == "DESKTOP_ACCESS_KEY_MIGRATION_REQUIRED"
            assert "FAKE-LEGACY-KEY-1001" not in json.dumps(result)
    finally:
        api._reset_service_for_tests(None)


def test_api_issue_exchange_list_revoke_and_operation_scope(tmp_path) -> None:
    manager = SimpleNamespace(state_dir=tmp_path)
    service = SimpleNamespace(manager=manager)
    api._reset_service_for_tests(service)
    trusted_context = {
        "source": "defaultspack_local_ui",
        "trusted_audience": FAKE_BINDING["audience"],
        "trusted_origin": FAKE_BINDING["origin"],
        "authenticated_principal_id": FAKE_BINDING["principal_id"],
        "authenticated_device_id": FAKE_BINDING["device_id"],
        "authenticated_session_id": FAKE_BINDING["session_id"],
    }
    try:
        issued = api.run(
            {
                "_handler": "desktop_exchange_issue",
                "seat_id": FAKE_SEAT,
                "operations": ["desktop.frame"],
            },
            trusted_context,
        )
        exchanged = api.run(
            {
                "_handler": "desktop_exchange_redeem",
                "exchange_code": issued["data"]["exchange_code"],
                "principal_id": "fake-client-spoofed-principal",
                "origin": "https://fake-client-spoofed.invalid",
            },
            trusted_context,
        )
        credential = exchanged["data"]["session_credential"]
        denied = api.run(
            {
                "_handler": "desktop_get",
                "seat_id": FAKE_SEAT,
                "desktop_session_credential": credential,
            },
            trusted_context,
        )
        listed = api.run(
            {"_handler": "desktop_grants_list", "seat_id": FAKE_SEAT}, trusted_context
        )
        revoked = api.run(
            {
                "_handler": "desktop_grant_revoke",
                "seat_id": FAKE_SEAT,
                "grant_id": exchanged["data"]["credential_id"],
            },
            trusted_context,
        )
    finally:
        api._reset_service_for_tests(None)

    assert issued["status"] == "ok"
    assert exchanged["status"] == "ok"
    assert denied["error"]["code"] == "DESKTOP_OPERATION_NOT_AUTHORIZED"
    assert listed["status"] == "ok"
    assert credential not in json.dumps(listed)
    assert "credential_hash" not in json.dumps(listed)
    assert revoked["data"]["revoked"] is True


@pytest.mark.parametrize("fields", [{}, {"approved": None}, {"approved": 1},
                                    {"approved": "true"}, {"approved": []}])
def test_access_grant_requires_explicit_bool_without_calling_manager(fields):
    manager = Mock()
    result = api._desktop_access_grant(
        SimpleNamespace(manager=manager),
        {"seat_id": FAKE_SEAT, "request_id": "request-1", **fields},
        {"principal_id": "owner-1"},
    )
    assert result["_http_status"] == 400
    assert result["error"]["code"] == "INVALID_INPUT"
    manager.grant_desktop_access_request.assert_not_called()


@pytest.mark.parametrize("approved", [True, False])
def test_access_grant_passes_explicit_owner_decision_unchanged(approved):
    manager = Mock()
    manager.grant_desktop_access_request.return_value = {"ok": True}
    result = api._desktop_access_grant(
        SimpleNamespace(manager=manager),
        {"seat_id": FAKE_SEAT, "request_id": "request-1", "approved": approved},
        {"principal_id": "owner-1"},
    )
    assert result["status"] == "ok"
    manager.grant_desktop_access_request.assert_called_once_with(
        FAKE_SEAT, "request-1", owner_id="owner-1", approved=approved,
    )
