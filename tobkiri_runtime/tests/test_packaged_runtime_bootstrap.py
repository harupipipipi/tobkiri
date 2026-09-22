"""Packaged Pack v4 bootstrap regression tests."""

from __future__ import annotations

import http.cookiejar
import json
import os
import socket
import stat
import threading
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
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
from core_runtime.panel_auth import PanelAuthManager, reset_panel_auth_manager_for_tests
from core_runtime.pack_api_server import PackAPIServer
from tobkiri_host.broker import RequestBroker
from tobkiri_host.runtime import V4DispatchSession


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


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

    monkeypatch.setattr(runtime_bootstrap, "active_default_profile_exists", lambda: True)
    monkeypatch.setattr(
        runtime_bootstrap,
        "capture_default_profile",
        require_reconfirmation,
    )
    monkeypatch.setattr(runtime_bootstrap, "resolve_runtime_port", lambda: 8765)
    monkeypatch.setattr(
        runtime_bootstrap,
        "initialize_pack_api_server",
        lambda **_kwargs: SetupServer(),
    )

    kernel = Kernel()
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
    monkeypatch.setattr(runtime_bootstrap, "active_default_profile_exists", lambda: False)
    monkeypatch.setattr(runtime_bootstrap, "resolve_runtime_port", lambda: 8765)
    monkeypatch.setattr(
        runtime_bootstrap,
        "initialize_pack_api_server",
        lambda **_kwargs: RunningServer(),
    )

    reset_container()
    first_kernel = Kernel()
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
    second_kernel = Kernel()
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
    monkeypatch.setattr(runtime_bootstrap, "active_default_profile_exists", lambda: False)
    monkeypatch.setattr(runtime_bootstrap, "resolve_runtime_port", lambda: 8765)
    monkeypatch.setattr(
        runtime_bootstrap,
        "initialize_pack_api_server",
        lambda **_kwargs: RunningServer(),
    )

    reset_container()
    first_kernel = Kernel()
    try:
        first_kernel.run_startup_until(first_kernel.API_INIT_STEP)
        original_token = token_cache.read_text(encoding="utf-8")
        rotated_token = get_hmac_key_manager().rotate()
        assert rotated_token != original_token
        assert token_cache.read_text(encoding="utf-8") == original_token
    finally:
        first_kernel.shutdown()

    reset_container()
    refreshed_kernel = Kernel()
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
        "active_default_profile_exists",
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
        Kernel().run_startup_until(Kernel.API_INIT_STEP)

    assert server_started is False
    assert not (tmp_path / ".desktop_api_token").exists()


def test_public_kernel_first_start_requires_confirmed_defaults_transaction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The canonical public bootstrap serves ready HTTP from isolated data."""
    coordination_timeout_seconds = 30
    port = _free_port()
    monkeypatch.setenv("RUMI_PORT", str(port))
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path / "user_data"))
    monkeypatch.setenv("RUMI_LOG_DIR", str(tmp_path / "logs"))
    reset_panel_auth_manager_for_tests(PanelAuthManager(bootstrap_secret="first-request-bootstrap"))
    original_refresh = PackAPIServer._refresh_runtime_capture
    refresh_entered = threading.Event()
    release_refresh = threading.Event()
    refresh_completed = threading.Event()
    activation_started = threading.Event()
    activation_response_received = threading.Event()

    def delayed_refresh(
        server: PackAPIServer,
        activated_session=None,
        *,
        lifecycle_generation: int,
    ) -> None:
        refresh_entered.set()
        assert release_refresh.wait(timeout=coordination_timeout_seconds)
        try:
            original_refresh(
                server,
                activated_session,
                lifecycle_generation=lifecycle_generation,
            )
        finally:
            refresh_completed.set()

    monkeypatch.setattr(PackAPIServer, "_refresh_runtime_capture", delayed_refresh)

    kernel = Kernel()
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

        def activate() -> dict[str, object]:
            activation_started.set()
            with urlopen(request, timeout=coordination_timeout_seconds) as response:
                result = json.load(response)["data"]
            activation_response_received.set()
            return result

        with ThreadPoolExecutor(max_workers=1) as executor:
            activation = executor.submit(activate)
            try:
                assert activation_started.wait(timeout=coordination_timeout_seconds)
                if not refresh_entered.wait(timeout=coordination_timeout_seconds):
                    try:
                        activation.result(timeout=5)
                    except TimeoutError as error:
                        raise AssertionError(
                            "defaults activation did not reach runtime refresh "
                            "within the bounded coordination window"
                        ) from error
                    pytest.fail(
                        "defaults activation returned before runtime refresh started"
                    )
                assert activation_response_received.wait(
                    timeout=coordination_timeout_seconds
                )
                activated = activation.result(timeout=coordination_timeout_seconds)
                assert not release_refresh.is_set()
            finally:
                release_refresh.set()
        assert refresh_completed.wait(timeout=coordination_timeout_seconds)
        assert activated["state"] == "active"
        assert activated["audit_receipt"]["state"] == "committed"
        assert activated["audit_receipt"]["activation_id"] == activated["activation_id"]
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        bootstrap_request = Request(
            f"http://127.0.0.1:{port}/api/panel/auth/bootstrap",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Rumi-Desktop-Bootstrap": "first-request-bootstrap",
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
        with urlopen(
            f"http://127.0.0.1:{port}/health",
            timeout=coordination_timeout_seconds,
        ) as response:
            ready = json.load(response)["data"]
        assert ready["runtime_ready"] is True
    finally:
        release_refresh.set()
        kernel.shutdown()


def test_clean_bootstrap_captures_and_restarts_without_legacy_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A fresh Host persists one exact Defaults activation and reloads it."""
    user_data = tmp_path / "clean-home"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    from core_runtime.bootstrap.profile_capture import (
        prepare_default_profile_confirmation,
    )

    first = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    restarted = capture_default_profile()

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

    kernel = Kernel()
    monkeypatch.setenv("RUMI_PORT", str(_free_port()))
    try:
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
