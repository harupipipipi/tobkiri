"""Packaged Pack v4 bootstrap regression tests."""

from __future__ import annotations

import http.cookiejar
import json
import os
import socket
import stat
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

import pytest

from core_runtime.authority.v4 import AuthorityStoreError
from core_runtime.app_lifecycle_manager import get_runtime_readiness
from core_runtime.bootstrap.runtime import Kernel
from core_runtime.bootstrap.profile_capture import capture_default_profile
from core_runtime.di_container import get_container, reset_container
from core_runtime.hmac_key_manager import get_hmac_key_manager
from core_runtime.panel_auth import reset_panel_auth_manager_for_tests
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    create_defaultspack_kernel,
)
from tobkiri_host.broker import RequestBroker
from tobkiri_host.runtime import V4DispatchSession


_LAUNCHER_BOOTSTRAP_REVISION = (
    "sha256:cce92a9b1d3092cdac63ba80b39e5d3a17d0905f3a716241250e8ac724095580"
)
_LAUNCHER_BOOTSTRAP_PLAN = (
    "sha256:2a08fdc2de1e0d5e51d2f248b0984d4510db442e6905bcebc2984a44d23131a5"
)


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _kernel() -> Kernel:
    """Build the application-composed Host rather than an unconfigured core."""

    from tests.conformance_support.packaged_profile import packaged_profile_bundle_root

    return create_defaultspack_kernel(bundle_root=packaged_profile_bundle_root())


def _publish_launcher_contract(
    user_data: Path,
    *,
    profile_id: str,
    profile_revision: str,
    activation_id: str,
    plan_digest: str,
    bootstrap_secret: str,
) -> Path:
    """Simulate the Launcher's owner-only atomic contract promotion."""

    from tests.conformance_support.host_contract import host_contract

    user_data.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        user_data.chmod(0o700)
    path = user_data / "host_contract.json"
    replacement = user_data / f".host-contract-{uuid.uuid4().hex}.tmp"
    replacement.write_text(
        json.dumps(
            host_contract(
                profile_id=profile_id,
                profile_revision=profile_revision,
                activation_id=activation_id,
                plan_digest=plan_digest,
                values={"panel_bootstrap_secret": bootstrap_secret},
            ),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    if os.name != "nt":
        replacement.chmod(0o600)
    os.replace(replacement, path)
    if os.name != "nt":
        path.chmod(0o600)
    return path


def test_superseded_packaged_artifact_starts_ui_ready_reconfirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid predecessor transition serves setup instead of wedging startup."""

    import core_runtime.bootstrap.runtime as runtime_bootstrap
    from ecosystem.defaultspack.domain.runtime_v4 import (
        ProfileReconfirmationRequired,
    )

    diagnostic = (
        "active Profile Shell artifact identity was superseded by the verified "
        "packaged release; explicit reconfirmation is required"
    )
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path / "user_data"))

    class SetupServer:
        port = 8765
        _contract_routes: tuple[object, ...] = ()

        @staticmethod
        def is_running() -> bool:
            return True

        @staticmethod
        def stop() -> None:
            return None

    def require_reconfirmation() -> None:
        raise ProfileReconfirmationRequired(diagnostic)

    monkeypatch.setattr(runtime_bootstrap, "active_profile_exists", lambda: True)
    monkeypatch.setattr(
        runtime_bootstrap,
        "capture_active_profile",
        require_reconfirmation,
    )
    monkeypatch.setattr(runtime_bootstrap, "resolve_runtime_port", lambda: 8765)
    monkeypatch.setattr(
        runtime_bootstrap,
        "initialize_pack_api_server",
        lambda **_kwargs: SetupServer(),
    )

    kernel = _kernel()
    try:
        result = kernel.run_startup_until(kernel.API_INIT_STEP)
        readiness = get_runtime_readiness()
        assert result == {"status": "ok", "step_id": "api_init", "port": 8765}
        assert readiness == {
            "panel_ready": True,
            "runtime_ready": False,
            "runtime_status": "profile_reconfirmation_required",
            "runtime_error": diagnostic,
        }
    finally:
        kernel.shutdown()


def test_kernel_bootstrap_publishes_and_reuses_desktop_api_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh and restarted Kernels publish the authoritative local token."""

    import core_runtime.bootstrap.runtime as runtime_bootstrap

    class RunningServer:
        port = 8765
        _contract_routes: tuple[object, ...] = ()

        @staticmethod
        def is_running() -> bool:
            return True

        @staticmethod
        def stop() -> None:
            return None

    user_data = tmp_path / "user_data"
    token_cache = tmp_path / ".desktop_api_token"
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    monkeypatch.setattr(runtime_bootstrap, "active_profile_exists", lambda: False)
    monkeypatch.setattr(runtime_bootstrap, "resolve_runtime_port", lambda: 8765)
    monkeypatch.setattr(
        runtime_bootstrap,
        "initialize_pack_api_server",
        lambda **_kwargs: RunningServer(),
    )

    reset_container()
    first_kernel = _kernel()
    try:
        first_kernel.run_startup_until(first_kernel.API_INIT_STEP)
        first_token = get_hmac_key_manager().get_active_key()
        assert (user_data / "hmac_keys.json").is_file()
        assert token_cache.read_text(encoding="utf-8") == first_token
        if os.name != "nt":
            assert stat.S_IMODE(token_cache.stat().st_mode) == 0o600
    finally:
        first_kernel.shutdown()

    reset_container()
    second_kernel = _kernel()
    try:
        second_kernel.run_startup_until(second_kernel.API_INIT_STEP)
        restarted_token = get_hmac_key_manager().get_active_key()
        assert restarted_token == first_token
        assert token_cache.read_text(encoding="utf-8") == restarted_token
        assert not tuple(tmp_path.glob(".desktop_api_token.*.tmp"))
    finally:
        second_kernel.shutdown()


def test_kernel_bootstrap_refreshes_desktop_api_token_after_hmac_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subsequent bootstrap replaces the cache after the HMAC token rotates."""

    import core_runtime.bootstrap.runtime as runtime_bootstrap

    class RunningServer:
        port = 8765
        _contract_routes: tuple[object, ...] = ()

        @staticmethod
        def is_running() -> bool:
            return True

        @staticmethod
        def stop() -> None:
            return None

    user_data = tmp_path / "user_data"
    token_cache = tmp_path / ".desktop_api_token"
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    monkeypatch.setattr(runtime_bootstrap, "active_profile_exists", lambda: False)
    monkeypatch.setattr(runtime_bootstrap, "resolve_runtime_port", lambda: 8765)
    monkeypatch.setattr(
        runtime_bootstrap,
        "initialize_pack_api_server",
        lambda **_kwargs: RunningServer(),
    )

    reset_container()
    first_kernel = _kernel()
    try:
        first_kernel.run_startup_until(first_kernel.API_INIT_STEP)
        original_token = token_cache.read_text(encoding="utf-8")
        rotated_token = get_hmac_key_manager().rotate()
        assert rotated_token != original_token
        assert token_cache.read_text(encoding="utf-8") == original_token
    finally:
        first_kernel.shutdown()

    reset_container()
    refreshed_kernel = _kernel()
    try:
        refreshed_kernel.run_startup_until(refreshed_kernel.API_INIT_STEP)
        assert token_cache.read_text(encoding="utf-8") == rotated_token
    finally:
        refreshed_kernel.shutdown()


def test_kernel_bootstrap_fails_closed_when_token_cache_cannot_be_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Host HTTP surface must not start without the Launcher token cache."""

    import core_runtime.bootstrap.runtime as runtime_bootstrap

    user_data = tmp_path / "user_data"
    server_started = False
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    monkeypatch.setattr(
        runtime_bootstrap,
        "active_profile_exists",
        lambda: False,
    )

    def reject_cache(_user_data: Path, _api_token: str) -> Path:
        raise OSError("simulated token cache failure")

    def start_server(**_kwargs: object) -> object:
        nonlocal server_started
        server_started = True
        raise AssertionError("server must not start")

    monkeypatch.setattr(
        runtime_bootstrap,
        "_persist_desktop_api_token_cache",
        reject_cache,
    )
    monkeypatch.setattr(runtime_bootstrap, "initialize_pack_api_server", start_server)

    reset_container()
    with pytest.raises(OSError, match="simulated token cache failure"):
        _kernel().run_startup_until(Kernel.API_INIT_STEP)

    assert server_started is False
    assert not (tmp_path / ".desktop_api_token").exists()


def test_public_kernel_first_start_requires_confirmed_defaults_transaction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Activation flushes its receipt, then cold-restarts under a new contract."""
    coordination_timeout_seconds = 30
    port = _free_port()
    user_data = tmp_path / "user_data"
    bootstrap_secret = "first-request-bootstrap"
    monkeypatch.setenv("RUMI_PORT", str(port))
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    monkeypatch.setenv("RUMI_LOG_DIR", str(tmp_path / "logs"))
    contract_path = _publish_launcher_contract(
        user_data,
        profile_id="defaults",
        profile_revision=_LAUNCHER_BOOTSTRAP_REVISION,
        activation_id="activation:bootstrap-template",
        plan_digest=_LAUNCHER_BOOTSTRAP_PLAN,
        bootstrap_secret=bootstrap_secret,
    )
    monkeypatch.setenv("TOBKIRI_HOST_CONTRACT_PATH", str(contract_path))
    reset_panel_auth_manager_for_tests(capture_launcher_credential=True)
    from core_runtime.restart_control import (
        clear_kernel_restart_request,
        is_kernel_restart_requested,
    )

    clear_kernel_restart_request()
    kernel = _kernel()
    try:
        kernel.run_startup_until("api_init")
        remaining = kernel.run_startup_remaining()
        assert remaining["status"] == "setup_required"
        with urlopen(
            f"http://127.0.0.1:{port}/health",
            timeout=coordination_timeout_seconds,
        ) as response:
            envelope = json.load(response)
        assert envelope["success"] is True
        assert envelope["data"]["panel_ready"] is True
        assert envelope["data"]["runtime_ready"] is False

        # The temporary Launcher identity may authenticate only this bootstrap
        # panel session. It is not the execution identity projected by health.
        assert "activation_id" not in envelope["data"]
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        bootstrap_request = Request(
            f"http://127.0.0.1:{port}/api/panel/auth/bootstrap",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Rumi-Desktop-Bootstrap": bootstrap_secret,
            },
            data=b"{}",
        )
        with opener.open(
            bootstrap_request,
            timeout=coordination_timeout_seconds,
        ) as response:
            old_login_code = json.load(response)["data"]["code"]
        exchange_request = Request(
            f"http://127.0.0.1:{port}/api/panel/auth/exchange",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{port}",
            },
            data=json.dumps({"code": old_login_code}).encode(),
        )
        with opener.open(exchange_request, timeout=coordination_timeout_seconds):
            pass
        assert getattr(kernel._server, "handler_class")._dispatch_session.session_kind == (
            "host_profile_control"
        )
        control_session = getattr(kernel._server, "handler_class")._dispatch_session
        assert control_session.session_kind == "host_profile_control"
        assert get_container().get_or_none("v4_dispatch_session") is None

        with urlopen(
            f"http://127.0.0.1:{port}/api/setup/packs",
            timeout=coordination_timeout_seconds,
        ) as response:
            setup = json.load(response)["data"]
        assert setup["state"] == "review_required"
        confirmation = setup["recommended_default_profile"]["confirmation"]
        from tests.conformance_support.packaged_profile import load_packaged_profile_catalog

        catalog = load_packaged_profile_catalog()
        variant = catalog.shells["shell.tauri.default"]["launch"]["variants"][0]
        assert confirmation["shell"]["executable_artifact_digest"] == variant["entrypoint_digest"]
        request = Request(
            f"http://127.0.0.1:{port}/api/setup/packs/install",
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps(
                {
                    "setup_api_version": "io.tobkiri.setup-state.v4",
                    "operation_id": "defaults.activate",
                    "confirmed": True,
                    "confirmation": confirmation,
                }
            ).encode(),
        )
        with opener.open(request, timeout=coordination_timeout_seconds) as response:
            activated = json.load(response)["data"]
        assert activated["state"] == "active"
        assert activated["audit_receipt"]["state"] == "committed"
        assert activated["audit_receipt"]["activation_id"] == activated["activation_id"]
        assert is_kernel_restart_requested() is True
        assert kernel.run_startup_remaining() == {
            "status": "restart_required",
            "runtime_ready": False,
        }
        assert get_runtime_readiness()["runtime_ready"] is False
        # The stale control handler remains local to this server and the newly
        # constructed active capture is never installed into the global slot.
        assert getattr(kernel._server, "handler_class")._dispatch_session is control_session
        assert get_container().get_or_none("v4_dispatch_session") is None
        with pytest.raises(urllib.error.HTTPError) as stale_cookie:
            opener.open(
                f"http://127.0.0.1:{port}/api/v4/packvm/doctor",
                timeout=coordination_timeout_seconds,
            )
        assert stale_cookie.value.code == 401
    finally:
        kernel.shutdown()

    # A stale or tampered Launcher contract cannot create an active handler.
    # The failed cold start also must not publish its candidate into the
    # process-global dispatch slot before contract validation succeeds.
    clear_kernel_restart_request()
    reset_container()
    reset_panel_auth_manager_for_tests(capture_launcher_credential=True)
    rejected_restart = _kernel()
    try:
        with pytest.raises(RuntimeError, match="Host contract"):
            rejected_restart.run_startup_until("api_init")
        assert get_container().get_or_none("v4_dispatch_session") is None
    finally:
        rejected_restart.shutdown()

    # This is the external Launcher promotion: it atomically replaces the
    # bootstrap marker only after the activation response has been received.
    active = capture_default_profile()
    _publish_launcher_contract(
        user_data,
        profile_id=str(active.resolved.profile["profile_id"]),
        profile_revision=str(active.resolved.plan["profile_revision"]),
        activation_id=str(active.activation["activation_id"]),
        plan_digest=str(active.resolved.plan["plan_digest"]),
        bootstrap_secret=bootstrap_secret,
    )
    clear_kernel_restart_request()
    reset_container()
    reset_panel_auth_manager_for_tests(capture_launcher_credential=True)

    restarted = _kernel()
    try:
        restarted.run_startup_until("api_init")
        assert restarted.run_startup_remaining() == {
            "status": "ok",
            "runtime_ready": True,
        }
        with urlopen(
            f"http://127.0.0.1:{port}/health",
            timeout=coordination_timeout_seconds,
        ) as response:
            ready = json.load(response)["data"]
        assert ready["runtime_ready"] is True
        assert ready["activation_id"] == active.activation["activation_id"]

        # A process-local cookie from HostProfileControl cannot cross the cold
        # handoff, while a new active-session exchange works immediately.
        with pytest.raises(urllib.error.HTTPError) as old_cookie:
            opener.open(
                f"http://127.0.0.1:{port}/api/v4/packvm/doctor",
                timeout=coordination_timeout_seconds,
            )
        assert old_cookie.value.code == 401

        bootstrap_request = Request(
            f"http://127.0.0.1:{port}/api/panel/auth/bootstrap",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Rumi-Desktop-Bootstrap": bootstrap_secret,
            },
            data=b"{}",
        )
        with opener.open(
            bootstrap_request,
            timeout=coordination_timeout_seconds,
        ) as response:
            login_code = json.load(response)["data"]["code"]
        exchange_request = Request(
            f"http://127.0.0.1:{port}/api/panel/auth/exchange",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{port}",
            },
            data=json.dumps({"code": login_code}).encode(),
        )
        with opener.open(exchange_request, timeout=coordination_timeout_seconds):
            pass
        for contract_path in ("/api/home/dashboard", "/api/pack-control/catalog"):
            first_request = Request(
                "http://127.0.0.1:"
                f"{port}/api/contracts/defaultspack/" + quote(f"GET {contract_path}", safe=""),
                headers={"X-Tobkiri-Request-ID": str(uuid.uuid4())},
            )
            with opener.open(
                first_request,
                timeout=coordination_timeout_seconds,
            ) as response:
                first_payload = json.load(response)
            assert first_payload["success"] is True
        with pytest.raises(urllib.error.HTTPError) as replay:
            urlopen(request, timeout=coordination_timeout_seconds)
        assert replay.value.code == 401
    finally:
        restarted.shutdown()


def test_clean_bootstrap_captures_and_restarts_without_legacy_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A fresh Host persists one exact Defaults activation and reloads it."""
    user_data = tmp_path / "clean-home"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    from core_runtime.bootstrap.profile_capture import (
        prepare_default_profile_confirmation,
    )

    first = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    restarted = capture_default_profile()

    from core_runtime.host_contract import bind_host_contract
    from tests.conformance_support.host_contract import host_contract

    contract = host_contract(
        profile_id=str(first.resolved.profile["profile_id"]),
        profile_revision=str(first.resolved.plan["profile_revision"]),
        activation_id=str(first.activation["activation_id"]),
        plan_digest=str(first.resolved.plan["plan_digest"]),
    )

    assert first.activation == restarted.activation
    assert first.resolved.plan == restarted.resolved.plan
    assert first.resolved.profile["base"]["pack_id"] == "defaults-basepack"
    assert first.resolved.profile["shell"]["provider_id"] == "shell.tauri.default"
    providers = [
        binding
        for binding in first.resolved.plan["bindings"]
        if binding["contract_id"] == "conversation.turn.v1"
    ]
    assert len(providers) == 1
    assert not (user_data / "settings" / "startup_profiles.json").exists()

    kernel = _kernel()
    monkeypatch.setenv("RUMI_PORT", str(_free_port()))
    try:
        with bind_host_contract(contract):
            kernel.run_startup_until("api_init")
            session = get_container().get("v4_dispatch_session")
        assert isinstance(session, V4DispatchSession)
        assert isinstance(session.broker, RequestBroker)
        assert session.authority_control is not None
        assert session.profile_id == "defaults"
        assert session.plan_digest == first.resolved.plan["plan_digest"]
    finally:
        kernel.shutdown()
        kernel.shutdown()

    assert session.owned_authority_store is not None
    with pytest.raises(AuthorityStoreError, match="closed"):
        _ = session.owned_authority_store.security_epoch
    authority_path = user_data / "authority" / "v4.sqlite3"
    renamed_path = user_data / "authority" / "v4-renamed.sqlite3"
    authority_path.rename(renamed_path)
    renamed_path.rename(authority_path)
