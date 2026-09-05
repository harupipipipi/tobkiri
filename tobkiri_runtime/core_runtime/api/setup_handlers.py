"""Generic Host orchestration for application-composed setup operations."""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping

from ..profile_runtime_port import require_profile_runtime


logger = logging.getLogger(__name__)


class SetupHandlersMixin:
    """Run one explicitly-confirmed Profile setup transaction through the Host."""

    _dispatch_session: Any = None

    @staticmethod
    def _setup_resolution_denied_response(
        runtime: Any,
        error: BaseException,
    ) -> Dict[str, Any] | None:
        """Map an application Profile denial to the typed setup response."""

        if not runtime.is_resolution_denied(error):
            return None
        return {
            "error": str(error),
            "status_code": 409,
            "state": "activation_denied",
            "write_set": [],
        }

    @staticmethod
    def _setup_listing(
        *,
        active: bool = False,
        activation_denied: bool = False,
        denial_diagnostic: str | None = None,
    ) -> Mapping[str, Any]:
        """Request the application's complete setup presentation from one catalog."""

        runtime = require_profile_runtime()
        from ..bootstrap.profile_capture import (
            _bundle_root,
            prepare_bootstrap_profile_confirmation,
        )

        catalog = runtime.load_catalog(_bundle_root())
        confirmation = prepare_bootstrap_profile_confirmation()
        return runtime.setup_listing(
            catalog,
            confirmation,
            active=active,
            activation_denied=activation_denied,
            denial_diagnostic=denial_diagnostic,
        )

    @staticmethod
    def _recommended_default_profile_preview() -> Dict[str, Any]:
        """Compatibility adapter for callers of the former preview helper."""

        runtime = require_profile_runtime()
        return dict(runtime.setup_preview(SetupHandlersMixin._setup_listing()))

    def _setup_list_packs(self) -> Dict[str, Any]:
        """Return the application's setup listing after Host state capture."""

        from ..bootstrap.profile_capture import (
            active_profile_exists,
            capture_active_profile,
        )

        active = False
        activation_denied = False
        denial_diagnostic: str | None = None
        if active_profile_exists():
            try:
                capture_active_profile()
                active = True
            except Exception as error:
                runtime = require_profile_runtime()
                if runtime.is_reconfirmation_required(error):
                    denial_diagnostic = str(error)
                elif runtime.is_resolution_denied(error):
                    activation_denied = True
                    denial_diagnostic = str(error)
                else:
                    raise
        runtime = require_profile_runtime()
        try:
            listing = self._setup_listing(
                active=active,
                activation_denied=activation_denied,
                denial_diagnostic=denial_diagnostic,
            )
        except Exception as error:
            response = self._setup_resolution_denied_response(runtime, error)
            if response is not None:
                return response
            raise
        return dict(listing)

    def _setup_install_pack(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Ask the application to authorize a request before Host activation."""

        runtime = require_profile_runtime()
        decision = runtime.setup_activation_decision(body, None)
        if decision.response is not None:
            return dict(decision.response)
        try:
            listing = self._setup_listing()
        except Exception as error:
            response = self._setup_resolution_denied_response(runtime, error)
            if response is not None:
                return response
            raise
        decision = runtime.setup_activation_decision(body, listing)
        if decision.response is not None:
            return dict(decision.response)
        confirmation = decision.confirmation
        if confirmation is None:
            raise RuntimeError("application setup decision has no activation input")

        from ..bootstrap.profile_capture import (
            activation_audit_receipt,
            capture_bootstrap_profile,
        )

        lifecycle = getattr(self.__class__, "app_lifecycle_manager", None)
        dispatch_session: Any = None
        try:
            if lifecycle is not None and hasattr(lifecycle, "activate_bootstrap_profile"):
                activated = lifecycle.activate_bootstrap_profile(confirmation)
                if not isinstance(activated, tuple) or len(activated) != 2:
                    raise RuntimeError("application activation result is invalid")
                active_profile, dispatch_session = activated
            else:
                active_profile = capture_bootstrap_profile(confirmation=confirmation)
            audit_receipt = activation_audit_receipt(active_profile)
        except Exception:
            return dict(runtime.setup_activation_failure())
        finally:
            # This process still serves the stale HostProfileControl handler.
            # The activated capture is validation-only and must not survive
            # until the Launcher cold-restarts into a freshly published tuple.
            close = getattr(dispatch_session, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.exception("activated restart-only dispatch session did not close")
        result = dict(runtime.setup_activation_success(active_profile, audit_receipt))
        if result.get("state") == "active":
            # The receipt has been durably committed, but this HTTP handler
            # remains bound to HostProfileControl until the Launcher performs
            # the cold handoff.
            result["restart_required"] = True
        return result

    @staticmethod
    def _retired_state() -> Dict[str, Any]:
        """Return the Pack-owned response for a removed setup operation."""

        return dict(require_profile_runtime().retired_setup_response())

    @classmethod
    def _retired_setup_complete_state(cls) -> Dict[str, Any]:
        """Return the Pack-owned no-write response for the retired route."""

        return dict(require_profile_runtime().retired_setup_response(route="/api/setup/complete"))

    def _setup_grant_all_ok(self, _setup_pack_id: str) -> Dict[str, Any]:
        """Reject the retired blanket approval surface."""

        return self._retired_state()

    def _setup_revoke_all_ok(self, _setup_pack_id: str) -> Dict[str, Any]:
        """Reject the retired blanket approval surface."""

        return self._retired_state()

    def _setup_get_migration_status(self) -> Dict[str, Any]:
        """Report that retired setup migration has no Host write authority."""

        return self._retired_state()


__all__ = ["SetupHandlersMixin"]
