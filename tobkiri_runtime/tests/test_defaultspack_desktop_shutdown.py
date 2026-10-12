"""Exercise desktop ownership using a real captured-session cleanup boundary."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ecosystem" / "defaultspack"))

from defaultspack import desktop_app  # noqa: E402
from tobkiri_host.runtime import V4DispatchSession  # noqa: E402


@pytest.fixture
def desktop_shutdown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """Supply isolated owners without starting a server, VM, or native surface."""
    events: list[str] = []
    overlay = tmp_path / "guest.cow"
    overlay.write_bytes(b"ephemeral guest allocation")

    def release_overlay() -> None:
        events.append("release_guest")
        overlay.unlink()

    broker = Mock()
    broker.close.side_effect = lambda: events.append("close_broker")
    authority = Mock()
    authority.close.side_effect = lambda: events.append("close_authority")
    session = V4DispatchSession(
        broker=broker,
        context_for=Mock(),
        effect_scope_for=Mock(),
        providers={},
        profile_id="test-profile",
        plan_digest="test-plan",
        profile_revision="test-revision",
        activation_id="test-activation",
        owned_authority_store=authority,
        close_callbacks=(release_overlay,),
    )
    server = Mock()
    server.start.side_effect = lambda: events.append("start_server")
    server.stop.side_effect = lambda: events.append("drain_server")
    server.issue_panel_login_code.return_value = {"code": "test-login"}
    server_factory = Mock(return_value=server)
    surface = Mock(return_value="webview")
    for name, value in {
        "_configure_persistent_user_state": Mock(),
        "_configure_http_environment": Mock(),
        "_write_launch_event": Mock(),
        "_restore_active_profile_contracts": Mock(return_value=(session, ())),
        "_capture_launch_host_contract": Mock(return_value={}),
        "_require_host_panel_auth_manager": Mock(return_value=Mock()),
        "_wait_until_ready": Mock(return_value=True),
        "_wait_until_chat_ready": Mock(return_value=True),
        "_port_owner_snapshot": Mock(return_value=[]),
    }.items():
        monkeypatch.setattr(desktop_app, name, value)
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.isolation."
        "macos_vz_provisioner.default_packvm_provisioner",
        Mock(return_value=Mock()),
    )
    monkeypatch.setattr("core_runtime.packvm_lifecycle_v4.PackVMLifecycleV4", Mock())
    monkeypatch.setattr("core_runtime.app_lifecycle_manager.AppLifecycleManager", Mock())
    monkeypatch.setattr("core_runtime.di_container.get_container", Mock())
    for name in ("mark_panel_ready", "mark_runtime_ready"):
        monkeypatch.setattr(f"core_runtime.app_lifecycle_manager.{name}", Mock())
    monkeypatch.setattr("domain.integrations.secrets.load_integration_secrets_into_env", Mock())
    monkeypatch.setattr(
        "ecosystem.defaultspack.defaultspack.surface_contributions.defaultspack_web_mounts",
        Mock(return_value=()),
    )
    monkeypatch.setattr("core_runtime.pack_api_server.PackAPIServer", server_factory)
    monkeypatch.setattr("defaultspack.native_webview.open_desktop_surface", surface)
    return {
        "events": events,
        "overlay": overlay,
        "session": session,
        "authority": authority,
        "server": server,
        "server_factory": server_factory,
        "surface": surface,
    }


def test_blocking_webview_quit_drains_before_releasing_capture(
    desktop_shutdown: dict,
) -> None:
    state = desktop_shutdown
    assert desktop_app.main() == 0
    assert state["events"] == [
        "start_server",
        "drain_server",
        "close_broker",
        "release_guest",
        "close_authority",
    ]
    assert not state["overlay"].exists()
    state["server"].stop.assert_called_once_with()


@pytest.mark.parametrize(
    "failure_stage",
    [
        "host_contract",
        "server_constructor",
        "server_start",
        "panel_login",
        "surface",
        "surface_event",
    ],
)
def test_early_failure_releases_initial_capture(
    desktop_shutdown: dict,
    failure_stage: str,
) -> None:
    state = desktop_shutdown
    failure = OSError("test startup failure")
    if failure_stage == "host_contract":
        desktop_app._capture_launch_host_contract.side_effect = failure
    elif failure_stage == "server_constructor":
        state["server_factory"].side_effect = failure
    elif failure_stage == "server_start":
        state["server"].start.side_effect = failure
    elif failure_stage == "panel_login":
        failure = RuntimeError("test login failure")
        state["server"].issue_panel_login_code.side_effect = failure
    elif failure_stage == "surface":
        state["surface"].side_effect = failure
    else:

        def fail_surface_event(event: str, **_fields: object) -> None:
            if event == "surface_opened":
                raise failure

        desktop_app._write_launch_event.side_effect = fail_surface_event
    with pytest.raises(type(failure), match=str(failure)):
        desktop_app.main()
    assert not state["overlay"].exists()
    state["authority"].close.assert_called_once_with()
    if failure_stage in {"host_contract", "server_constructor"}:
        state["server"].stop.assert_not_called()
    else:
        assert state["events"].index("drain_server") < state["events"].index("release_guest")


def test_failed_chat_readiness_releases_capture(desktop_shutdown: dict) -> None:
    desktop_app._wait_until_chat_ready.return_value = False
    assert desktop_app.main() == 1
    assert not desktop_shutdown["overlay"].exists()
    desktop_shutdown["surface"].assert_not_called()
    desktop_shutdown["server"].stop.assert_called_once_with()


def test_owner_close_is_idempotent_after_server_refresh(desktop_shutdown: dict) -> None:
    state = desktop_shutdown

    def refreshed_surface(*_args: object, **_kwargs: object) -> str:
        # PackAPIServer retires and closes the initial capture after refresh.
        state["session"].close()
        return "webview"

    state["surface"].side_effect = refreshed_surface
    assert desktop_app.main() == 0
    assert state["events"].count("release_guest") == 1
    state["authority"].close.assert_called_once_with()


def test_failed_server_drain_keeps_capture_resources(desktop_shutdown: dict) -> None:
    state = desktop_shutdown
    state["server"].stop.side_effect = RuntimeError("server is still draining")
    with pytest.raises(RuntimeError, match="server is still draining"):
        desktop_app.main()
    assert state["overlay"].exists()
    state["session"].broker.close.assert_not_called()
    state["authority"].close.assert_not_called()
    # Explicitly finish the isolated test owner once the simulated drain ends.
    state["session"].close()


def test_shutdown_log_failure_does_not_skip_capture_release(
    desktop_shutdown: dict,
) -> None:
    def fail_shutdown_event(event: str, **_fields: object) -> None:
        if event == "server_stopped_after_webview":
            raise OSError("test diagnostic failure")

    desktop_app._write_launch_event.side_effect = fail_shutdown_event
    with pytest.raises(OSError, match="test diagnostic failure"):
        desktop_app.main()
    assert not desktop_shutdown["overlay"].exists()
    desktop_shutdown["authority"].close.assert_called_once_with()


@pytest.mark.parametrize("interrupt", [False, True])
def test_browser_loop_exit_releases_capture(
    desktop_shutdown: dict,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: bool,
) -> None:
    state = desktop_shutdown
    state["surface"].return_value = "browser"
    handlers: dict = {}

    def register_handler(signum: int, handler: object) -> None:
        handlers[signum] = handler

    def stop_loop(_seconds: float) -> None:
        if interrupt:
            raise KeyboardInterrupt
        handlers[desktop_app.signal.SIGTERM](desktop_app.signal.SIGTERM, None)

    monkeypatch.setattr(desktop_app.signal, "signal", register_handler)
    monkeypatch.setattr(desktop_app.time, "sleep", stop_loop)
    if interrupt:
        with pytest.raises(KeyboardInterrupt):
            desktop_app.main()
    else:
        assert desktop_app.main() == 0
    assert not state["overlay"].exists()
    assert state["events"].index("drain_server") < state["events"].index("release_guest")


def test_capture_cleanup_failure_propagates_and_preserves_retry_owner(
    desktop_shutdown: dict,
) -> None:
    state = desktop_shutdown
    release = Mock(side_effect=OSError("test allocation still busy"))
    session = replace(state["session"], close_callbacks=(release,))
    desktop_app._restore_active_profile_contracts.return_value = (session, ())
    with pytest.raises(RuntimeError, match="captured Provider cleanup is incomplete"):
        desktop_app.main()
    state["server"].stop.assert_called_once_with()
    assert state["overlay"].exists()
    state["authority"].close.assert_not_called()
    # The normal session API retains failed ownership and supports a later retry.
    release.side_effect = state["overlay"].unlink
    session.close()
    assert not state["overlay"].exists()
    state["authority"].close.assert_called_once_with()
