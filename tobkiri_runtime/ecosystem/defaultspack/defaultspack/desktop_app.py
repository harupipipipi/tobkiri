from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import types
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core_runtime.panel_auth import PanelAuthManager

_DIAGNOSTIC_ENV_KEYS = (
    "DEFAULTS_HTTP_HOST",
    "DEFAULTS_HTTP_PORT",
    "RUMI_DEFAULTSPACK_OPEN_BROWSER",
    "RUMI_DEFAULTSPACK_PORT",
    "RUMI_DEFAULTSPACK_SURFACE",
    "RUMI_LOG_DIR",
    "RUMI_PROFILE_SURFACE",
    "RUMI_USER_DATA",
)
_IMPORT_PATH_READY = False
_SEALED_SCOPE = None


def _pack_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _sealed_app_root() -> Path | None:
    """Return the explicitly authorized sealed app root."""
    if _SEALED_SCOPE is None:
        return None
    return _SEALED_SCOPE.app_root_for(__file__)


def _configure_persistent_user_state() -> None:
    """Bind Defaultspack state to launcher-owned storage without migration.

    Production startup never reads a replaceable bundle's historical state.
    Legacy state import is an explicit offline maintenance operation.
    """
    user_data = os.environ.get("RUMI_USER_DATA", "").strip()
    if not user_data:
        return

    persistent_root = Path(user_data).expanduser()
    agent_runtime_dir = persistent_root / "defaultspack" / "shared" / "agent_runtime"
    os.environ.setdefault("RUMI_DEFAULTSPACK_AGENT_RUNTIME_DIR", str(agent_runtime_dir))
    os.environ.setdefault(
        "RUMI_DEFAULTSPACK_AGENT_TRANSCRIPT_DIR",
        str(agent_runtime_dir / "transcripts"),
    )

    configured_settings = os.environ.get("RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH", "").strip()
    settings_path = (
        Path(configured_settings).expanduser()
        if configured_settings
        else persistent_root / "defaultspack" / "shared" / "frontend_settings.json"
    )
    if not configured_settings:
        os.environ["RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH"] = str(settings_path)


def _ensure_import_path() -> None:
    global _IMPORT_PATH_READY
    sealed_app_root = _sealed_app_root()
    if sealed_app_root is not None:
        pack_root = _pack_root()
        for authorized_root in reversed((sealed_app_root, pack_root)):
            root = str(authorized_root)
            if root not in sys.path:
                sys.path.insert(0, root)
        _install_ecosystem_defaultspack_alias(
            pack_root,
            ecosystem_dirs=[sealed_app_root / "ecosystem"],
        )
        _IMPORT_PATH_READY = True
        return

    pack_root = _pack_root()
    configured_roots = (
        os.environ.get("RUMI_APP_DIR"),
        os.environ.get("RUMI_CORE_DIR"),
        os.environ.get("REPO"),
    )
    for path in (
        pack_root,
        pack_root.parents[1],
        *(Path(root) for root in configured_roots if root),
    ):
        root = str(path)
        if root not in sys.path:
            sys.path.insert(0, root)
    _install_ecosystem_defaultspack_alias(pack_root)
    _IMPORT_PATH_READY = True


def _install_ecosystem_defaultspack_alias(
    pack_root: Path,
    *,
    ecosystem_dirs: list[Path] | None = None,
) -> None:
    """Expose a managed pack root as ecosystem.defaultspack.

    Repo installs naturally import ``ecosystem.defaultspack`` via
    ``tobkiri_runtime/ecosystem/defaultspack``. Managed pack versions live under
    user-data without that parent ``ecosystem`` directory, but some legacy
    modules still import the canonical package path.
    """
    ecosystem_dirs = ecosystem_dirs or _candidate_ecosystem_dirs(pack_root)
    ecosystem = sys.modules.get("ecosystem")
    if ecosystem is None:
        ecosystem = types.ModuleType("ecosystem")
        ecosystem.__path__ = [str(path) for path in ecosystem_dirs]  # type: ignore[attr-defined]
        sys.modules["ecosystem"] = ecosystem
    elif not hasattr(ecosystem, "__path__"):
        ecosystem.__path__ = [str(path) for path in ecosystem_dirs]  # type: ignore[attr-defined]
    else:
        paths = list(getattr(ecosystem, "__path__", []))
        for ecosystem_dir in ecosystem_dirs:
            ecosystem_path = str(ecosystem_dir)
            if ecosystem_path not in paths:
                paths.insert(0, ecosystem_path)
        ecosystem.__path__ = paths  # type: ignore[attr-defined]

    defaultspack = sys.modules.get("ecosystem.defaultspack")
    pack_path = str(pack_root)
    if defaultspack is None:
        defaultspack = types.ModuleType("ecosystem.defaultspack")
        defaultspack.__path__ = [pack_path]  # type: ignore[attr-defined]
        defaultspack.__package__ = "ecosystem.defaultspack"
        sys.modules["ecosystem.defaultspack"] = defaultspack
    else:
        paths = list(getattr(defaultspack, "__path__", []))
        if pack_path not in paths:
            paths.insert(0, pack_path)
            defaultspack.__path__ = paths  # type: ignore[attr-defined]
    setattr(ecosystem, "defaultspack", defaultspack)


def _candidate_ecosystem_dirs(pack_root: Path) -> list[Path]:
    candidates: list[Path] = []

    if pack_root.parent.name == "ecosystem":
        candidates.append(pack_root.parent)

    for root in (
        os.environ.get("RUMI_APP_DIR"),
        os.environ.get("RUMI_CORE_DIR"),
        os.environ.get("REPO"),
    ):
        if root:
            candidates.append(Path(root) / "ecosystem")

    for entry in sys.path:
        if entry:
            candidates.append(Path(entry) / "ecosystem")

    resolved: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            path = candidate.resolve()
        except OSError:
            path = candidate
        key = str(path)
        if key in seen or not candidate.exists() or not candidate.is_dir():
            continue
        seen.add(key)
        resolved.append(candidate)
    return resolved


def prepare_for_sealed_dispatch(scope: object) -> None:
    """Bind sealed imports and the Launcher-issued PackVM bundle identity."""
    global _SEALED_SCOPE
    if _SEALED_SCOPE is not None and _SEALED_SCOPE is not scope:
        raise RuntimeError("Defaultspack sealed scope was already initialized")
    app_root_for = getattr(scope, "app_root_for", None)
    if not callable(app_root_for):
        raise RuntimeError("Defaultspack sealed scope lacks an application root")
    sealed_app_root = app_root_for(__file__)
    if not isinstance(sealed_app_root, Path):
        raise RuntimeError("Defaultspack sealed scope returned an invalid app root")
    _SEALED_SCOPE = scope
    _ensure_import_path()
    from core_runtime.packaged_application_bundle import (
        install_packvm_bundle_binding_from_sealed_scope,
    )

    install_packvm_bundle_binding_from_sealed_scope(scope, __file__)


def _url() -> str:
    port = (
        os.environ.get("DEFAULTS_HTTP_PORT") or os.environ.get("RUMI_DEFAULTSPACK_PORT") or "8766"
    )
    return f"http://127.0.0.1:{port}/chat"


def _require_own_bind() -> bool:
    """Return whether this process must prove ownership of its HTTP listener."""
    return (
        os.environ.get("RUMI_DEFAULTSPACK_REQUIRE_OWN_BIND") == "1"
        or os.environ.get("RUMI_DEFAULTSPACK_DEBUG_ISOLATION") == "1"
    )


def _configure_http_environment() -> None:
    """Normalize loopback HTTP settings and validate isolated debug ports."""
    port = (
        os.environ.get("RUMI_DEFAULTSPACK_PORT") or os.environ.get("DEFAULTS_HTTP_PORT") or "8766"
    )
    os.environ.setdefault("DEFAULTS_HTTP_HOST", "127.0.0.1")
    os.environ["DEFAULTS_HTTP_PORT"] = port
    os.environ["RUMI_DEFAULTSPACK_PORT"] = port
    if not _require_own_bind():
        return
    if os.environ["DEFAULTS_HTTP_HOST"] != "127.0.0.1":
        raise RuntimeError(
            "RUMI_DEFAULTSPACK_REQUIRE_OWN_BIND requires DEFAULTS_HTTP_HOST=127.0.0.1"
        )
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise RuntimeError(
            "RUMI_DEFAULTSPACK_REQUIRE_OWN_BIND requires a decimal localhost "
            "port between 1 and 65535"
        )


def _parse_cli_args(argv: list[str]) -> None:
    """Parse launcher arguments before runtime setup or imports."""
    parser = argparse.ArgumentParser(description="Launch the Tobkiri Defaultspack desktop app.")
    parser.parse_args(argv)


def _surface_url(url: str) -> str:
    """Return a token-free URL for the desktop surface.

    Local credentials belong to the captured Host session.  They must never be
    serialized into browser history, diagnostics, or a URL fragment.
    """
    return url.partition("#")[0]


def _restore_active_profile_contracts(packvm_lifecycle: Any):
    """Capture and verify the exact persisted Defaults activation and UI map."""

    from core_runtime.authority.v4 import AuthorityStore
    from core_runtime.bootstrap.production_v4 import capture_production_dispatch
    from core_runtime.bootstrap.profile_capture import (
        _bundle_root,
        active_default_profile_exists,
        capture_default_profile,
        runtime_user_data_root,
    )
    from core_runtime.di_container import get_container
    from core_runtime.frontend_contract_routes import (
        load_frontend_contract_bindings,
    )
    from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog
    from tobkiri_host.runtime import install_dispatch_session

    if not active_default_profile_exists():
        raise RuntimeError("Defaults v4 activation is not committed")
    bundle_root = _bundle_root()
    ecosystem_root = _pack_root().parent
    active = capture_default_profile()
    catalog = BundledCatalog.load(bundle_root)
    application = catalog.packs.get("runtime.tauri.application.default")
    if application is None:
        raise RuntimeError("Defaults application Pack is not selected")
    bindings = load_frontend_contract_bindings(
        Path(__file__).with_name("frontend_contract_map.v4.json"),
        application,
    )
    session = capture_production_dispatch(
        active,
        bundle_root=bundle_root,
        ecosystem_root=ecosystem_root,
        authority_store=AuthorityStore(runtime_user_data_root() / "authority" / "v4.sqlite3"),
        packvm_provisioner=packvm_lifecycle,
        packvm_readiness_reader=packvm_lifecycle.readiness_snapshot,
        frontend_contract_bindings=bindings,
    )
    install_dispatch_session(get_container(), session)
    _write_launch_event(
        "profile_contract_restore_complete",
        profile_id=session.profile_id,
        plan_digest=session.plan_digest,
        route_count=len(bindings),
        snapshot_type=type(session).__name__,
    )
    return session, bindings


def _diagnostic_log_path() -> Path:
    explicit = os.environ.get("RUMI_DEFAULTSPACK_LAUNCH_LOG")
    if explicit:
        return _validate_mutable_diagnostic_path(Path(explicit).expanduser())

    log_dir = os.environ.get("RUMI_LOG_DIR")
    if log_dir:
        return _validate_mutable_diagnostic_path(
            Path(log_dir).expanduser() / "defaultspack-launch.jsonl"
        )

    user_data = os.environ.get("RUMI_USER_DATA")
    if user_data:
        return _validate_mutable_diagnostic_path(
            Path(user_data).expanduser().parent / "logs" / "defaultspack-launch.jsonl"
        )

    return Path(tempfile.gettempdir()) / "rumi-defaultspack-launch.jsonl"


def _validate_mutable_diagnostic_path(path: Path) -> Path:
    """Reject launch diagnostics that would mutate sealed app resources."""
    if not path.is_absolute():
        raise ValueError("Defaultspack launch log path must be absolute")
    candidate = path.resolve(strict=False)
    sealed_app_root = _sealed_app_root()
    if sealed_app_root is not None:
        protected = sealed_app_root.resolve(strict=True)
        if candidate == protected or candidate.is_relative_to(protected):
            raise ValueError("Defaultspack launch log path must be outside sealed app resources")
    return candidate


def _safe_cwd() -> str:
    try:
        return str(Path.cwd())
    except OSError:
        return "<unavailable>"


def _diagnostic_env() -> dict[str, str]:
    return {key: value for key in _DIAGNOSTIC_ENV_KEYS if (value := os.getenv(key))}


def _write_launch_event(event: str, **fields: object) -> None:
    payload = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event": event,
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "cwd": _safe_cwd(),
        **fields,
    }
    try:
        path = _diagnostic_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
    except Exception:
        # Launch diagnostics must never make the user-facing app fail to open.
        pass


def _port_owner_snapshot(port: str) -> list[dict[str, str]]:
    if not port.isdigit():
        return []
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-F", "pcL"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except Exception:
        return []
    if result.returncode != 0 or not result.stdout:
        return []

    owners: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in result.stdout.splitlines():
        if not raw_line:
            continue
        key, value = raw_line[0], raw_line[1:]
        if key == "p":
            if current:
                owners.append(current)
            current = {"pid": value}
        elif key == "c":
            current["command"] = value
        elif key == "L":
            current["user"] = value
    if current:
        owners.append(current)
    return owners


def _port_from_url(url: str) -> str:
    try:
        return url.split(":", 2)[2].split("/", 1)[0]
    except IndexError:
        return ""


def _wait_until_ready(url: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    health_url = url.split("/chat", 1)[0].rstrip("/") + "/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=1.0) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    return False


def _wait_until_chat_ready(url: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                body = response.read(2048).decode("utf-8", "ignore")
                if 200 <= response.status < 300 and ("<title>" in body or 'id="root"' in body):
                    return True
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.2)
    return False


def _require_host_panel_auth_manager() -> PanelAuthManager:
    """Return the singleton bound to the exact Launcher-owned Host contract."""
    from core_runtime.host_contract import host_contract_value
    from core_runtime.panel_auth import get_panel_auth_manager

    bootstrap_secret = host_contract_value("panel_bootstrap_secret")
    if not bootstrap_secret:
        raise RuntimeError("Launcher-owned panel bootstrap secret is required")
    manager = get_panel_auth_manager()
    if not manager.validate_bootstrap_secret(bootstrap_secret):
        raise RuntimeError("Panel auth manager is not bound to the active Host contract")
    return manager


def main(argv: list[str] | None = None) -> int:
    if argv is not None:
        _parse_cli_args(argv)
    if not _IMPORT_PATH_READY:
        _ensure_import_path()
    _configure_persistent_user_state()
    _configure_http_environment()
    url = _url()
    port = _port_from_url(url)
    _write_launch_event(
        "start",
        env=_diagnostic_env(),
        log_path=str(_diagnostic_log_path()),
        port=port,
        url=url,
    )
    from core_runtime.app_lifecycle_manager import (
        AppLifecycleManager,
        mark_panel_ready,
        mark_profile_reconfirmation_required,
    )
    from ecosystem.defaultspack.domain.runtime_v4 import (
        ProfileReconfirmationRequired,
    )
    from core_runtime.packvm_lifecycle_v4 import PackVMLifecycleV4

    packvm_lifecycle = PackVMLifecycleV4()
    lifecycle = AppLifecycleManager(packvm_lifecycle=packvm_lifecycle)
    reconfirmation_error: str | None = None
    try:
        dispatch_session, contract_bindings = _restore_active_profile_contracts(packvm_lifecycle)
    except ProfileReconfirmationRequired as error:
        dispatch_session, contract_bindings = None, ()
        reconfirmation_error = str(error)
        _write_launch_event(
            "profile_reconfirmation_required",
            denial_diagnostic=reconfirmation_error,
            port=port,
            url=url,
        )
    try:
        from domain.integrations.secrets import load_integration_secrets_into_env

        load_integration_secrets_into_env()
    except Exception as exc:
        _write_launch_event("secrets_load_skipped", error=repr(exc), port=port, url=url)
    from core_runtime.api.web_mounts import WebMountEntry
    from core_runtime.pack_api_server import PackAPIServer

    ui_root = _pack_root() / "ui"
    web_mounts: tuple[WebMountEntry, ...] = (
        {
            "path_prefix": "/chat",
            "web_root": ui_root,
            "spa_fallback": True,
            "index_file": "shell.html",
            "auth_required": True,
        },
        {
            "path_prefix": "/static",
            "web_root": ui_root,
            "spa_fallback": False,
            "index_file": "shell.html",
            "auth_required": True,
        },
    )
    auth = _require_host_panel_auth_manager()
    server = PackAPIServer(
        host="127.0.0.1",
        port=int(port),
        panel_auth_manager=auth,
        dispatch_session=dispatch_session,
        app_lifecycle_manager=lifecycle,
        contract_bindings=contract_bindings,
        web_mounts=web_mounts,
        packvm_lifecycle=packvm_lifecycle,
    )
    _write_launch_event("server_start_attempt", port=port, url=url)
    try:
        server.start()
    except OSError as exc:
        _write_launch_event(
            "server_start_oserror",
            error=repr(exc),
            existing_ready=False,
            own_bind_required=True,
            port=port,
            port_owners=_port_owner_snapshot(port),
            url=url,
        )
        raise
    _write_launch_event("server_started", port=port, url=url)
    if reconfirmation_error is None:
        mark_panel_ready()
    else:
        mark_profile_reconfirmation_required(reconfirmation_error)

    health_ready = _wait_until_ready(url)
    chat_ready = _wait_until_chat_ready(url)
    _write_launch_event(
        "readiness_complete",
        chat_ready=chat_ready,
        health_ready=health_ready,
        port=port,
        url=url,
    )

    from defaultspack.native_webview import open_desktop_surface

    login_code = str(auth.issue_login_code()["code"])
    launch_url = f"{url}?{urllib.parse.urlencode({'code': login_code})}"
    surface_result = open_desktop_surface(launch_url, title="Tobkiri")
    _write_launch_event(
        "surface_opened",
        port=port,
        reused_existing_server=False,
        surface_result=surface_result,
        url=url,
    )
    if surface_result == "webview":
        if server is not None:
            server.stop()
            _write_launch_event("server_stopped_after_webview", port=port, url=url)
        return 0
    stop = False

    def _handle_signal(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        while not stop:
            time.sleep(0.5)
    finally:
        server.stop()
        _write_launch_event("server_stopped", port=port, url=url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
