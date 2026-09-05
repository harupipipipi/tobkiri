"""Tests for the sole live Defaults Profile v4 setup transaction."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core_runtime.api.setup_handlers import SetupHandlersMixin
from core_runtime.bootstrap import profile_capture
from ecosystem.defaultspack.domain.runtime_v4 import ProfileResolutionDenied
from core_runtime.pack_api_server import PackAPIHandler
from core_runtime.panel_auth import PanelAuthManager


class _Handler(SetupHandlersMixin):
    pass


@pytest.fixture(autouse=True)
def _install_profile_runtime() -> None:
    """Compose the Pack port explicitly for this isolated Host-handler suite."""

    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        install_defaultspack_profile_runtime,
    )

    install_defaultspack_profile_runtime()


def _preview() -> dict[str, object]:
    return _listing()["recommended_default_profile"]


def _listing() -> dict[str, object]:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "tobkiri_protocol"
        / "fixtures"
        / "defaults_setup_v4.canonical.json"
    )
    return json.loads(fixture.read_text(encoding="utf-8"))


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
        "_setup_listing",
        return_value=_listing(),
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


def test_setup_reports_unavailable_development_shell_without_dropping_request() -> None:
    with patch.object(
        SetupHandlersMixin,
        "_setup_listing",
        side_effect=ProfileResolutionDenied(
            "Shell artifact is unavailable for this source/build: shell.tauri.default"
        ),
    ):
        result = _Handler()._setup_list_packs()

    assert result == {
        "error": "Shell artifact is unavailable for this source/build: shell.tauri.default",
        "status_code": 409,
        "state": "activation_denied",
        "write_set": [],
    }


def test_development_bundle_requires_exact_source_runtime_and_generated_artifacts(
    tmp_path: Path, monkeypatch,
) -> None:
    runtime_root = tmp_path / "tobkiri_runtime"
    runtime_root.mkdir()
    generated_root = (
        tmp_path
        / "tobkiri_launcher"
        / "src-tauri"
        / "target"
        / "dev-defaults"
    )
    bundle = generated_root / "v4"
    artifacts = generated_root / "platform-artifacts"
    bundle.mkdir(parents=True)
    artifacts.mkdir()
    monkeypatch.setenv("RUMI_ENVIRONMENT", "development")
    monkeypatch.setenv("RUMI_APP_DIR", str(runtime_root))

    assert profile_capture._development_bundle_root(runtime_root) == bundle

    monkeypatch.setenv("RUMI_APP_DIR", str(tmp_path / "different-runtime"))
    assert profile_capture._development_bundle_root(runtime_root) is None


def test_setup_rejects_tampered_confirmation() -> None:
    with patch.object(
        SetupHandlersMixin,
        "_setup_listing",
        return_value=_listing(),
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
        "_setup_listing",
        return_value=_listing(),
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
        "_setup_listing",
        return_value=_listing(),
    ):
        result = _Handler()._setup_install_pack(_request(confirmed=False))

    assert result["status_code"] == 409
    assert result["state"] == "confirmation_required"


def test_setup_completes_canonical_capture_and_requires_cold_restart() -> None:
    with (
        patch.object(
            SetupHandlersMixin,
            "_setup_listing",
            return_value=_listing(),
        ),
        patch(
            "core_runtime.bootstrap.profile_capture.capture_bootstrap_profile",
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
        "restart_required": True,
    }


def test_setup_keeps_the_control_handler_and_closes_restart_only_session() -> None:
    """Activation cannot publish a new session into the old HTTP handler."""

    class RestartOnlySession:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    restart_only_session = RestartOnlySession()
    control_session = object()
    _Handler._dispatch_session = control_session
    lifecycle = SimpleNamespace(
        activate_bootstrap_profile=lambda _confirmation: (_active(), restart_only_session)
    )
    _Handler.app_lifecycle_manager = lifecycle
    try:
        with (
            patch.object(
                SetupHandlersMixin,
                "_setup_listing",
                return_value=_listing(),
            ),
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
        preserved_handler_session = _Handler._dispatch_session
    finally:
        del _Handler.app_lifecycle_manager
        _Handler._dispatch_session = None

    assert result["state"] == "active"
    assert result["restart_required"] is True
    assert restart_only_session.close_calls == 1
    assert preserved_handler_session is control_session


def test_committed_setup_requests_restart_when_response_write_fails() -> None:
    """A post-commit transport failure cannot strand the stale control Host."""

    from core_runtime import restart_control

    handler_type = PackAPIHandler.canonical_v4_server_handler(
        panel_auth_manager=PanelAuthManager(bootstrap_secret="test-bootstrap"),
        dispatch_session=None,
        app_lifecycle_manager=None,
    )
    handler = object.__new__(handler_type)
    handler.path = "/api/setup/packs/install"
    handler._reset_request_state = lambda: None
    handler._handle_packvm_lifecycle = lambda _method, _path: False
    handler._handle_contract_request = lambda _method: False
    handler._is_retired_setup_complete_path = lambda: False
    handler._setup_pre_auth_allowed = lambda: True
    handler._parse_object_body = lambda: {}
    handler._setup_install_pack = lambda _body: {"state": "active"}
    events: list[str] = []

    def fail_after_commit(_result: object) -> None:
        events.append("send")
        raise OSError("simulated post-commit response failure")

    original_request_restart = restart_control.request_kernel_restart

    def record_restart() -> None:
        events.append("restart")
        original_request_restart()

    handler._send_mapping_result = fail_after_commit
    restart_control.clear_kernel_restart_request()
    try:
        with patch.object(
            restart_control,
            "request_kernel_restart",
            side_effect=record_restart,
        ):
            with pytest.raises(OSError, match="post-commit"):
                handler.do_POST()
        assert restart_control.is_kernel_restart_requested() is True
    finally:
        restart_control.clear_kernel_restart_request()

    assert events == ["send", "restart"]


def test_non_v4_install_shape_is_retired_without_capture() -> None:
    with patch("core_runtime.bootstrap.profile_capture.capture_bootstrap_profile") as capture:
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
    variant = load_packaged_profile_catalog().shells["shell.tauri.default"]["launch"]["variants"][0]
    assert (
        preview["confirmation"]["shell"]["executable_artifact_digest"]
        == (variant["entrypoint_digest"])
    )
    assert len(preview["pack_ids"]) == len(set(preview["pack_ids"]))
    assert preview["conversation_provider"]
