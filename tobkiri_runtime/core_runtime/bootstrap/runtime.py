"""Canonical bootstrap for the packaged Tobkiri runtime.

This module deliberately does not reconstruct the retired registry-driven
runtime.  It owns only the Host HTTP surface required to expose a
Launcher-captured Pack v4 activation.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any

from ..app_lifecycle_manager import (
    AppLifecycleManager,
    mark_panel_ready,
    mark_profile_reconfirmation_required,
    mark_runtime_ready,
    reset_runtime_readiness,
)
from ..authority.v4 import AuthorityStore
from ..pack_api_server import PackAPIServer, initialize_pack_api_server
from ..frontend_contract_routes import (
    FrontendContractBinding,
    load_frontend_contract_bindings,
)
from ..runtime_port import resolve_runtime_port
from tobkiri_host.runtime import V4DispatchSession, install_dispatch_session
from .production_v4 import capture_production_dispatch
from .profile_capture import (
    _bundle_root,
    active_default_profile_exists,
    capture_default_profile,
    runtime_user_data_root,
)
from ecosystem.defaultspack.domain.runtime_v4 import (
    BundledCatalog,
    ProfileReconfirmationRequired,
)


logger = logging.getLogger(__name__)


def _persist_desktop_api_token_cache(user_data: Path, api_token: str) -> Path:
    """Atomically publish the active local API token for the desktop Launcher."""

    if not api_token or api_token != api_token.strip():
        raise RuntimeError("active local API token is unavailable")

    destination = user_data.parent / ".desktop_api_token"
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(destination.parent),
        prefix=f"{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            descriptor = -1
            output.write(api_token)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        if os.name != "nt":
            destination.chmod(0o600)
        return destination
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _prepare_desktop_api_token(user_data: Path) -> Path:
    """Initialize the canonical HMAC store and publish its active token cache."""

    from ..hmac_key_manager import initialize_hmac_key_manager

    manager = initialize_hmac_key_manager(keys_path=str(user_data / "hmac_keys.json"))
    return _persist_desktop_api_token_cache(user_data, manager.get_active_key())


class Kernel:
    """Start and stop the canonical packaged Pack v4 Host surface.

    The public name is the Launcher bootstrap contract.  Unlike the retired
    Kernel implementation, this class performs no Pack discovery, legacy
    manifest projection, interface registration, or authority reconstruction.
    """

    API_INIT_STEP = "api_init"
    owns_host_http_surface = True

    def __init__(self) -> None:
        from ..packvm_lifecycle_v4 import PackVMLifecycleV4

        self._lock = RLock()
        self._server: PackAPIServer | None = None
        self._dispatch_session: V4DispatchSession | None = None
        self._packvm_lifecycle = PackVMLifecycleV4()
        self._lifecycle = AppLifecycleManager(
            packvm_lifecycle=self._packvm_lifecycle,
        )

    def run_startup_until(self, step_id: str) -> dict[str, Any]:
        """Start the authenticated Host HTTP surface through ``step_id``."""
        if step_id != self.API_INIT_STEP:
            raise ValueError(f"unsupported Pack v4 bootstrap step: {step_id}")
        with self._lock:
            if self._server is not None and self._server.is_running():
                return {"status": "already_running", "step_id": step_id}
            reset_runtime_readiness()
            from ..di_container import get_container

            runtime_root = Path(__file__).resolve().parents[2]
            user_data = runtime_user_data_root()
            _prepare_desktop_api_token(user_data)
            dispatch_session = None
            contract_bindings: tuple[FrontendContractBinding, ...] = ()
            reconfirmation_error: str | None = None
            if active_default_profile_exists():
                try:
                    active = capture_default_profile()
                    authority_store = AuthorityStore(
                        user_data / "authority" / "v4.sqlite3"
                    )
                    bundle_root = _bundle_root()
                    catalog = BundledCatalog.load(bundle_root)
                    contract_bindings = load_frontend_contract_bindings(
                        runtime_root
                        / "ecosystem"
                        / "defaultspack"
                        / "defaultspack"
                        / "frontend_contract_map.v4.json",
                        catalog.packs["runtime.tauri.application.default"],
                    )
                    try:
                        dispatch_session = capture_production_dispatch(
                            active,
                            bundle_root=bundle_root,
                            ecosystem_root=runtime_root / "ecosystem",
                            authority_store=authority_store,
                            packvm_provisioner=self._packvm_lifecycle,
                            packvm_readiness_reader=(
                                self._packvm_lifecycle.readiness_snapshot
                            ),
                            frontend_contract_bindings=contract_bindings,
                        )
                    except Exception:
                        authority_store.close()
                        raise
                    install_dispatch_session(get_container(), dispatch_session)
                    self._dispatch_session = dispatch_session
                except ProfileReconfirmationRequired as error:
                    reconfirmation_error = str(error)
            port = resolve_runtime_port()
            self._server = initialize_pack_api_server(
                host="127.0.0.1",
                port=port,
                dispatch_session=dispatch_session,
                app_lifecycle_manager=self._lifecycle,
                contract_bindings=contract_bindings,
                packvm_lifecycle=self._packvm_lifecycle,
            )
            if reconfirmation_error is None:
                mark_panel_ready()
            else:
                mark_profile_reconfirmation_required(reconfirmation_error)
            return {"status": "ok", "step_id": step_id, "port": port}

    def run_startup_remaining(self) -> dict[str, Any]:
        """Publish readiness after the v4 Host surface is live."""
        with self._lock:
            if self._server is None or not self._server.is_running():
                raise RuntimeError("Pack v4 Host surface is not running")
            if self._lifecycle.check_setup_status().get("needs_setup") is True:
                return {
                    "status": "setup_required",
                    "runtime_ready": False,
                }
            if self._server is not None and not self._server._contract_routes:
                from ..di_container import get_container

                session = get_container().get_or_none("v4_dispatch_session")
                if session is None:
                    raise RuntimeError("captured v4 dispatch session is unavailable")
                runtime_root = Path(__file__).resolve().parents[2]
                catalog = BundledCatalog.load(_bundle_root())
                bindings = load_frontend_contract_bindings(
                    runtime_root
                    / "ecosystem"
                    / "defaultspack"
                    / "defaultspack"
                    / "frontend_contract_map.v4.json",
                    catalog.packs["runtime.tauri.application.default"],
                )
                port = self._server.port
                self._server.stop()
                self._server = initialize_pack_api_server(
                    host="127.0.0.1",
                    port=port,
                    dispatch_session=session,
                    app_lifecycle_manager=self._lifecycle,
                    contract_bindings=bindings,
                    packvm_lifecycle=self._packvm_lifecycle,
                )
            mark_runtime_ready()
            return {"status": "ok", "runtime_ready": True}

    def run_startup(self) -> dict[str, Any]:
        """Run the complete packaged bootstrap for headless callers."""
        result = self.run_startup_until(self.API_INIT_STEP)
        result.update(self.run_startup_remaining())
        return result

    def shutdown(self) -> None:
        """Stop the Host HTTP surface if this bootstrap started it."""
        with self._lock:
            if self._server is not None:
                try:
                    self._server.stop()
                finally:
                    self._server = None
            if self._dispatch_session is not None:
                try:
                    self._dispatch_session.close()
                finally:
                    self._dispatch_session = None


__all__ = ["Kernel"]
