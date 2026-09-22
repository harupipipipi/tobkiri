"""Tests for the sole live Defaults Profile v4 setup transaction."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core_runtime.api.setup_handlers import SetupHandlersMixin


class _Handler(SetupHandlersMixin):
    pass


def _preview() -> dict[str, object]:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "tobkiri_protocol"
        / "fixtures"
        / "defaults_setup_v4.canonical.json"
    )
    return json.loads(fixture.read_text(encoding="utf-8"))[
        "recommended_default_profile"
    ]


def _request(*, confirmed: bool = True) -> dict[str, object]:
    return {
        "setup_api_version": "io.tobkiri.setup-state.v4",
        "operation_id": "defaults.activate",
        "confirmed": confirmed,
        "confirmation": _preview()["confirmation"],
    }


def _active() -> SimpleNamespace:
    return SimpleNamespace(
        resolved=SimpleNamespace(
            profile={"profile_id": "defaults"},
            plan={
                "profile_revision": "profile-revision:test",
                "plan_digest": "sha256:" + "1" * 64,
            },
        ),
        activation={
            "activation_id": "activation:test",
            "security_epoch": 7,
            "fencing_token": 11,
            "profile_authority_snapshot_digest": "sha256:" + "4" * 64,
        },
    )


def test_setup_lists_one_typed_finite_v4_transaction() -> None:
    with patch.object(
        SetupHandlersMixin,
        "_recommended_default_profile_preview",
        return_value=_preview(),
    ):
        result = _Handler()._setup_list_packs()

    assert result["setup_api_version"] == "io.tobkiri.setup-state.v4"
    assert result["state"] == "review_required"
    assert result["recommended_default_profile"] == _preview()
    assert result["required_transaction"] == [
        "catalog.verify",
        "profile.resolve",
        "authority.snapshot",
        "activation.prepare",
        "activation.commit",
        "runtime.capture",
    ]


def test_setup_rejects_tampered_confirmation() -> None:
    with patch.object(
        SetupHandlersMixin,
        "_recommended_default_profile_preview",
        return_value=_preview(),
    ):
        request = _request()
        request["confirmation"] = {**request["confirmation"], "security_epoch": 8}
        result = _Handler()._setup_install_pack(request)

    assert result["status_code"] == 409
    assert result["state"] == "review_required"
    assert result["write_set"] == []


def test_setup_rejects_tampered_or_extra_shell_digest_fields() -> None:
    with patch.object(
        SetupHandlersMixin,
        "_recommended_default_profile_preview",
        return_value=_preview(),
    ):
        for shell_change in (
            {"executable_artifact_digest": "sha256:" + "0" * 64},
            {"untrusted_digest": "sha256:" + "f" * 64},
        ):
            request = _request()
            confirmation = request["confirmation"]
            request["confirmation"] = {
                **confirmation,
                "shell": {**confirmation["shell"], **shell_change},
            }
            result = _Handler()._setup_install_pack(request)

            assert result["status_code"] == 409
            assert result["state"] == "review_required"
            assert result["write_set"] == []


def test_setup_requires_explicit_confirmation() -> None:
    with patch.object(
        SetupHandlersMixin,
        "_recommended_default_profile_preview",
        return_value=_preview(),
    ):
        result = _Handler()._setup_install_pack(_request(confirmed=False))

    assert result["status_code"] == 409
    assert result["state"] == "confirmation_required"


def test_setup_completes_canonical_capture_without_restart() -> None:
    with (
        patch.object(
            SetupHandlersMixin,
            "_recommended_default_profile_preview",
            return_value=_preview(),
        ),
        patch(
            "core_runtime.bootstrap.profile_capture.capture_default_profile",
            return_value=_active(),
        ) as capture,
        patch(
            "core_runtime.bootstrap.profile_capture.activation_audit_receipt",
            return_value={
                "reservation_id": "activation-reservation:test",
                "state": "committed",
                "activation_id": "activation:test",
                "fencing_token": 11,
            },
        ),
    ):
        result = _Handler()._setup_install_pack(_request())

    capture.assert_called_once_with(confirmation=_preview()["confirmation"])
    assert result == {
        "setup_api_version": "io.tobkiri.setup-state.v4",
        "state": "active",
        "profile_id": "defaults",
        "profile_revision": "profile-revision:test",
        "plan_digest": "sha256:" + "1" * 64,
        "activation_id": "activation:test",
        "security_epoch": 7,
        "fencing_token": 11,
        "authority_snapshot_digest": "sha256:" + "4" * 64,
        "audit_receipt": {
            "reservation_id": "activation-reservation:test",
            "state": "committed",
            "activation_id": "activation:test",
            "fencing_token": 11,
        },
        "restart_required": False,
    }


def test_non_v4_install_shape_is_retired_without_capture() -> None:
    with patch(
        "core_runtime.bootstrap.profile_capture.capture_default_profile"
    ) as capture:
        result = _Handler()._setup_install_pack({"setup_pack_ids": ["legacy"]})

    capture.assert_not_called()
    assert result["status_code"] == 410
    assert result["state"] == "legacy_setup_retired"


def test_second_approval_and_runtime_migration_surfaces_are_retired() -> None:
    handler = _Handler()
    for result in (
        handler._setup_grant_all_ok("legacy"),
        handler._setup_revoke_all_ok("legacy"),
        handler._setup_get_migration_status(),
    ):
        assert result["status_code"] == 410
        assert result["state"] == "legacy_setup_retired"


def test_real_preview_is_exact_and_integrity_checked() -> None:
    preview = SetupHandlersMixin._recommended_default_profile_preview()
    from tests.conformance_support.packaged_profile import load_packaged_profile_catalog

    assert preview["profile_id"] == "defaults"
    assert preview["base_pack"] == "defaults-basepack"
    assert preview["shell"]["provider_id"] == "shell.tauri.default"
    variant = load_packaged_profile_catalog().shells["shell.tauri.default"][
        "launch"
    ]["variants"][0]
    assert preview["confirmation"]["shell"]["executable_artifact_digest"] == (
        variant["entrypoint_digest"]
    )
    assert len(preview["pack_ids"]) == len(set(preview["pack_ids"]))
    assert preview["conversation_provider"]
