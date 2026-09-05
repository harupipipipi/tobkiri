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
from typing import Any, Callable

from ..app_lifecycle_manager import (
    AppLifecycleManager,
    mark_panel_ready,
    mark_profile_reconfirmation_required,
    mark_runtime_ready,
    reset_runtime_readiness,
)
from ..authority.v4 import AuthorityStore
from ..pack_api_server import (
    HTTPApplicationPresentation,
    CapabilitySnapshotFactory,
    PackAPIServer,
    RuntimeCaptureFactory,
    initialize_pack_api_server,
)
from ..pack_control_v4 import (
    HostProfileControlSession,
    RuntimeSurfaceFactory,
)
from ..global_contracts.http_contract_dispatch import HTTPContractBinding
from ..runtime_port import resolve_runtime_port
from tobkiri_host.runtime import V4DispatchSession, install_dispatch_session
from .production_v4 import capture_production_dispatch
from .profile_capture import (
    _bundle_root,
    active_profile_exists,
    capture_active_profile,
    runtime_user_data_root,
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

    def __init__(
        self,
        *,
        packvm_lifecycle: Any | None = None,
        runtime_capture_factory: RuntimeCaptureFactory | None = None,
        capability_snapshot_factory: CapabilitySnapshotFactory | None = None,
        application_presentation: HTTPApplicationPresentation | None = None,
        host_profile_bindings_factory: (
            Callable[[], tuple[HTTPContractBinding, ...]] | None
        ) = None,
        runtime_surface_factory: RuntimeSurfaceFactory | None = None,
    ) -> None:
        self._lock = RLock()
        self._server: PackAPIServer | None = None
        self._dispatch_session: V4DispatchSession | HostProfileControlSession | None = (
            None
        )
        self._packvm_lifecycle = packvm_lifecycle
        self._runtime_capture_factory = runtime_capture_factory
        self._capability_snapshot_factory = capability_snapshot_factory
        self._application_presentation = application_presentation
        self._host_profile_bindings_factory = host_profile_bindings_factory
        self._runtime_surface_factory = runtime_surface_factory
        self._lifecycle = AppLifecycleManager(
            packvm_lifecycle=self._packvm_lifecycle,
            runtime_capture_factory=self._runtime_capture_factory,
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

            user_data = runtime_user_data_root()
            _prepare_desktop_api_token(user_data)
            bundle_root = _bundle_root()
            dispatch_session: (
                V4DispatchSession | HostProfileControlSession | None
            ) = None
            bindings_factory = self._host_profile_bindings_factory
            if bindings_factory is None:
                raise RuntimeError("application HTTP composition is unavailable")
            contract_bindings = bindings_factory()
            reconfirmation_error: str | None = None
            if active_profile_exists():
                try:
                    active = capture_active_profile()
                    authority_store = AuthorityStore(
                        user_data / "authority" / "v4.sqlite3"
                    )
                    capture_factory = self._runtime_capture_factory
                    if capture_factory is None:
                        raise RuntimeError(
                            "application runtime capture composition is unavailable"
                        )
                    inputs = capture_factory(active)
                    contract_bindings = inputs.contract_bindings
                    try:
                        dispatch_session = capture_production_dispatch(
                            active,
                            bundle_root=inputs.bundle_root,
                            ecosystem_root=inputs.ecosystem_root,
                            authority_store=authority_store,
                            packvm_provisioner=inputs.packvm_backend_factory,
                            packvm_readiness_reader=(
                                self._packvm_lifecycle.readiness_snapshot
                                if self._packvm_lifecycle is not None
                                else None
                            ),
                            http_contract_bindings=contract_bindings,
                            activation_snapshot_loader=(
                                inputs.activation_snapshot_loader
                            ),
                            runtime_surface_factory=inputs.runtime_surface_factory,
                            capability_binding_snapshot_factory=(
                                inputs.capability_binding_snapshot_factory
                            ),
                            capability_binding_selector=(
                                inputs.capability_binding_selector
                            ),
                            credential_store_factory=(
                                inputs.credential_store_factory
                            ),
                        )
                    except Exception:
                        authority_store.close()
                        raise
                    self._dispatch_session = dispatch_session
                except Exception as error:
                    from ..profile_runtime_port import require_profile_runtime

                    if not require_profile_runtime().is_reconfirmation_required(error):
                        raise
                    reconfirmation_error = str(error)
            else:
                dispatch_session = HostProfileControlSession(
                    bundle_root=bundle_root,
                    user_data_root=user_data,
                    runtime_surface_factory=self._runtime_surface_factory,
                )
                self._dispatch_session = dispatch_session
            port = resolve_runtime_port()
            try:
                self._server = initialize_pack_api_server(
                    host="127.0.0.1",
                    port=port,
                    dispatch_session=dispatch_session,
                    app_lifecycle_manager=self._lifecycle,
                    contract_bindings=contract_bindings,
                    runtime_capture_factory=self._runtime_capture_factory,
                    capability_snapshot_factory=self._capability_snapshot_factory,
                    application_presentation=self._application_presentation,
                    packvm_lifecycle=self._packvm_lifecycle,
                )
            except Exception:
                close = getattr(self._dispatch_session, "close", None)
                if callable(close):
                    close()
                self._dispatch_session = None
                raise
            if (
                dispatch_session is not None
                and getattr(dispatch_session, "session_kind", None)
                != "host_profile_control"
            ):
                install_dispatch_session(get_container(), dispatch_session)
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
            from ..restart_control import is_kernel_restart_requested

            if is_kernel_restart_requested():
                return {
                    "status": "restart_required",
                    "runtime_ready": False,
                }
            if self._lifecycle.check_setup_status().get("needs_setup") is True:
                return {
                    "status": "setup_required",
                    "runtime_ready": False,
                }
            if not active_profile_exists():
                return {
                    "status": "profile_activation_required",
                    "runtime_ready": False,
                    "profile_ceremony_available": True,
                }
            if self._server is not None and not self._server._contract_routes:
                from ..di_container import get_container

                session = get_container().get_or_none("v4_dispatch_session")
                if session is None:
                    raise RuntimeError("captured v4 dispatch session is unavailable")
                capture_factory = self._runtime_capture_factory
                if capture_factory is None:
                    raise RuntimeError(
                        "application runtime capture composition is unavailable"
                    )
                bindings = capture_factory(None).contract_bindings
                port = self._server.port
                self._server.stop()
                self._server = initialize_pack_api_server(
                    host="127.0.0.1",
                    port=port,
                    dispatch_session=session,
                    app_lifecycle_manager=self._lifecycle,
                    contract_bindings=bindings,
                    runtime_capture_factory=self._runtime_capture_factory,
                    capability_snapshot_factory=self._capability_snapshot_factory,
                    application_presentation=self._application_presentation,
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
