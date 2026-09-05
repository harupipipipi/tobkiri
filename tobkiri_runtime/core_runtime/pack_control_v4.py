"""Captured Host control plane for Pack v4 catalog/profile mutations."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from tobkiri_host.errors import HostCoreError
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.validation import validate_document
from tobkiri_protocol.secure_persistence import (
    SecureDirectory,
    SecurePersistenceError,
)
from .external_pack_catalog_v4 import (
    control_catalog_revision,
    external_pack_content_digest,
    load_admitted_pack_catalog as load_pack_catalog,
    resolve_admitted_pack_root as resolve_pack_root,
)
from .profile_runtime_port import require_profile_runtime

HOST_PROFILE_CONTROL_OPERATIONS = frozenset(
    {
        "profile.catalog.read",
        "profile.change.resolve",
        "profile.change.review",
        "profile.change.approve",
        "profile.change.activate",
        "operation.status.read",
    }
)

PACK_CONTROL_CONTRACT = "tobkiri.host.pack-control.v4"
CONTROL_PRESENTATION_CONTRACT = "tobkiri.host.control-presentation.v4"
CONTROL_PRESENTATION_OPERATIONS = frozenset(
    {
        "profile.change.activate",
        "profile.change.approve",
        "profile.change.resolve",
        "profile.change.review",
        "profile.catalog.read",
        "operation.status.read",
        "profile.read",
        "settings.read",
        "topology.contracts.read",
        "topology.operations.read",
        "topology.packs.read",
        "topology.principals.read",
    }
)
PACK_CONTROL_OPERATIONS = frozenset(
    {
        "catalog.read",
        "dashboard.read",
        "pack.install",
        "approval.candidate",
        "approval.approve",
        "approval.revoke",
        "pack.enable",
        "pack.disable",
        "pack.status",
        "profile.reload",
        "runtime.restart",
    }
)
_CANDIDATE_TTL_SECONDS = 120.0
_PERSISTENCE_STORES: dict[Path, SecureDirectory] = {}
_PERSISTENCE_STORES_LOCK = threading.RLock()
NO_ACTIVE_PROFILE_REVISION = canonical_digest(
    {
        "schema": "io.tobkiri.profile-predecessor.v1",
        "state": "none",
        "field": "revision",
    }
)
NO_ACTIVE_PLAN_DIGEST = canonical_digest(
    {"schema": "io.tobkiri.profile-predecessor.v1", "state": "none", "field": "plan"}
)
NO_ACTIVE_ACTIVATION_ID = "activation:none"


class RuntimeSurfacePort(Protocol):
    """Neutral runtime read surface used by Host control contracts."""

    def bind_capability_reader(self, reader: Callable[[], Mapping[str, object]]) -> None: ...

    def cancel_pending_reads(self) -> None: ...

    def close(self) -> None: ...

    def read_profile(
        self,
        *,
        expected_profile_revision: str | None = None,
        expected_plan_digest: str | None = None,
        profile_id: str | None = None,
        selected_profile_id: str | None = None,
    ) -> Mapping[str, object]: ...

    def read_profile_catalog(
        self,
        *,
        session_id: str | None = None,
        profile_id: str | None = None,
        selected_profile_id: str | None = None,
    ) -> Mapping[str, object]: ...

    def read_settings(
        self,
        *,
        profile_id: str | None = None,
        selected_profile_id: str | None = None,
    ) -> Mapping[str, object]: ...

    def read_advanced(
        self,
        view: str,
        *,
        expected_profile_revision: str | None = None,
        expected_plan_digest: str | None = None,
        profile_id: str | None = None,
        selected_profile_id: str | None = None,
    ) -> Mapping[str, object]: ...


class ProfileChangePort(Protocol):
    """Neutral Profile mutation projection supplied by the active app."""


class RuntimeSurfaceFactory(Protocol):
    """Build application-specific surfaces without importing them in core."""

    def __call__(
        self,
        **kwargs: object,
    ) -> tuple[RuntimeSurfacePort, ProfileChangePort]:
        """Return the surface and change service for this Host capture."""


class PackControlDenied(HostCoreError):
    """A Pack control request failed its captured authority boundary."""

    code = "pack_control_denied"


class PackControlInvalidRequest(PackControlDenied):
    """A Pack control request is structurally invalid."""

    code = "pack_control_invalid_request"


class PackControlConflict(PackControlDenied):
    """A Pack control request conflicts with committed lifecycle state."""

    code = "pack_control_conflict"


class PackControlStaleRevision(PackControlConflict):
    """A Pack control request is bound to a stale captured revision."""

    code = "pack_control_stale_revision"


class PackControlDigestMismatch(PackControlConflict):
    """A Pack control request does not match its captured digest binding."""

    code = "pack_control_digest_mismatch"


class PackControlUnapproved(PackControlDenied):
    """A Pack control request lacks Host-owned approval evidence."""

    code = "pack_control_unapproved"


class PackControlUnavailable(PackControlDenied):
    """The authoritative Pack control backend is unavailable."""

    code = "pack_control_unavailable"


class PackControlTimedOut(PackControlDenied):
    """A Pack control request exceeded its bounded execution deadline."""

    code = "pack_control_timeout"


@dataclass(frozen=True)
class _Binding:
    profile_id: str
    workspace_id: str
    profile_revision: str
    plan_digest: str
    catalog_revision: str


@dataclass(frozen=True)
class _ApprovalCandidate:
    candidate_id: str
    session_id: str
    pack_id: str
    snapshot_digest: str
    profile_revision: str
    catalog_revision: str
    expires_at: float


class HostProfileControlSession:
    """Catalog-bound Host control session available before Profile activation.

    This session exposes no Pack, workspace, Conversation, credential, or Shell
    operation.  Its freshness fence is the immutable Profile catalog plus the
    continued absence of a Host-global active pointer.
    """

    _OPERATIONS = HOST_PROFILE_CONTROL_OPERATIONS

    def __init__(
        self,
        *,
        bundle_root: Path | None = None,
        user_data_root: Path | None = None,
        runtime_surface_factory: RuntimeSurfaceFactory | None = None,
    ) -> None:
        from .bootstrap.profile_capture import host_profile_catalog
        from .bootstrap.profile_capture import runtime_user_data_root
        from .profile_catalog_v4 import profile_catalog_digest

        self._bundle_root = bundle_root
        self._user_data_root = (
            Path(user_data_root).resolve()
            if user_data_root is not None
            else runtime_user_data_root()
        )
        from .authority.v4 import AuthorityStore

        with AuthorityStore(self._user_data_root / "authority" / "v4.sqlite3") as authority:
            self._security_epoch = authority.security_epoch

        def load_catalog() -> Any:
            return (
                host_profile_catalog(
                    bundle_root=bundle_root,
                    user_data_root=self._user_data_root,
                )
                if bundle_root is not None
                else host_profile_catalog()
            )

        self._catalog_loader = load_catalog
        self._catalog_digest = profile_catalog_digest(load_catalog())
        if runtime_surface_factory is None:
            raise PackControlUnavailable("runtime surface composition is unavailable")
        self._runtime_surface, self._profile_changes = runtime_surface_factory(
            catalog_loader=load_catalog,
            bundle_root=bundle_root,
            user_data_root=self._user_data_root,
        )

    session_kind = "host_profile_control"
    principal_id = "tobkiri.host.profile-control"
    execution_profile_id = None

    @property
    def plan_digest(self) -> str:
        return self._catalog_digest

    @property
    def profile_id(self) -> str:
        """Return the empty identity used while no Profile is active."""

        return ""

    @property
    def profile_revision(self) -> str:
        """Return the empty revision used while no Profile is active."""

        return ""

    @property
    def activation_id(self) -> str:
        """Return the empty activation used while no Profile is active."""

        return ""

    @property
    def security_epoch(self) -> int:
        """Return the Authority epoch captured for this Host control session."""

        return self._security_epoch

    def assert_current(self) -> None:
        """Fence the session to the same catalog and an empty active pointer."""

        from .active_profile_store_v4 import ActiveProfileStore
        from .authority.v4 import AuthorityStore
        from .profile_catalog_v4 import profile_catalog_digest

        if ActiveProfileStore(self._user_data_root).load(verify_snapshot=True) is not None:
            raise PackControlStaleRevision("Host active Profile is no longer empty")
        with AuthorityStore(self._user_data_root / "authority" / "v4.sqlite3") as authority:
            if authority.security_epoch != self._security_epoch:
                raise PackControlStaleRevision("Host Authority epoch changed")
        if not hmac.compare_digest(
            profile_catalog_digest(self._catalog_loader()), self._catalog_digest
        ):
            raise PackControlStaleRevision("Host Profile catalog changed")

    def assert_operation_ready(self, contract_id: str, operation_id: str) -> None:
        """Reject every operation outside the bootstrap-safe Host surface."""

        if contract_id != CONTROL_PRESENTATION_CONTRACT or operation_id not in self._OPERATIONS:
            raise PackControlUnapproved("operation is unavailable before Profile activation")

    def provider_metadata(self, contract_id: str) -> tuple[Mapping[str, Any], ...]:
        if contract_id != CONTROL_PRESENTATION_CONTRACT:
            return ()
        return tuple(
            {
                "provider_id": "tobkiri.host.control-presentation",
                "contract_id": CONTROL_PRESENTATION_CONTRACT,
                "operation_id": operation_id,
                "principal_id": self.principal_id,
                "execution_profile_id": None,
                "catalog_digest": self.plan_digest,
            }
            for operation_id in sorted(self._OPERATIONS)
        )

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
        *,
        version_range: str | None = ">=4,<5",
    ) -> Mapping[str, Any]:
        """Invoke one Host-owned Profile ceremony operation."""

        del version_range
        self.assert_operation_ready(contract_id, operation_id)
        arguments = dict(payload)
        session_id = _required(arguments.pop("_session_id", None), "session binding")
        if "approved" in arguments or "approval_token" in arguments:
            raise PackControlUnapproved("client approval assertions are not trusted")
        try:
            if operation_id == "profile.catalog.read":
                selected = arguments.pop("selected_profile_id", None)
                _require_empty(arguments)
                return self._catalog_payload(
                    session_id=_panel_session_root(session_id),
                    selected_profile_id=str(selected) if selected is not None else None,
                )
            if operation_id == "operation.status.read":
                request_id = _required(arguments.pop("request_id", None), "request identity")
                _require_empty(arguments)
                from .control_reconciliation_v4 import ControlReconciliationStore

                return ControlReconciliationStore(
                    self._user_data_root / "control" / "reconciliation-v4.sqlite3"
                ).operation_status(
                    request_id,
                    session_id=_panel_session_root(session_id),
                )
            action = operation_id.removeprefix("profile.change.")
            handler = getattr(self._profile_changes, action)
            result = handler(arguments, session_id=_panel_session_root(session_id))
            if action == "activate" and result.get("state") == "active":
                result = {
                    **result,
                    "authoritative_snapshot": self._runtime_surface.read_profile(),
                }
            return result
        except Exception as error:
            as_dict = getattr(error, "as_dict", None)
            if callable(as_dict):
                return as_dict()
            raise

    def _catalog_payload(
        self, *, session_id: str, selected_profile_id: str | None
    ) -> Mapping[str, Any]:
        from .control_reconciliation_v4 import ControlReconciliationStore
        from .profile_catalog_v4 import bundle_lock_digest, project_profile_catalog

        catalog = self._catalog_loader()
        candidates = ControlReconciliationStore(
            self._user_data_root / "control" / "reconciliation-v4.sqlite3"
        ).profile_candidates(session_id=session_id)
        candidate_map = {
            str(record["review"]["profile"]["profile_id"]): record for record in candidates
        }
        return {
            "host_operation_api_version": "io.tobkiri.host.operation.v1",
            "surface": "profiles",
            "state": "catalog_ready",
            "host_catalog_digest": self._catalog_digest,
            "bundle_lock_digest": bundle_lock_digest(catalog),
            "data": project_profile_catalog(
                catalog,
                None,
                candidates=candidate_map,
                selected_profile_id=selected_profile_id,
            ),
            "write_set": [],
        }

    def close(self) -> None:
        self._runtime_surface.close()

    def cancel_pending_reads(self) -> None:
        self._runtime_surface.cancel_pending_reads()


class CapturedPackControlSession:
    """One immutable-v4-profile control session with explicit recapture points."""

    def __init__(
        self,
        binding: _Binding,
        *,
        packvm_readiness_reader: Callable[[], Mapping[str, Any]] | None = None,
        active_profile_loader: Callable[[], Any] | None = None,
        bundle_root: Path | None = None,
        runtime_surface_factory: RuntimeSurfaceFactory | None = None,
    ) -> None:
        self._binding = binding
        self._lock = threading.RLock()
        self._candidates: dict[str, _ApprovalCandidate] = {}
        catalog_loader = None
        if bundle_root is not None:

            def load_catalog() -> Any:
                """Union the supplied artifact bundle with Host registry Profiles."""

                from .bootstrap.profile_capture import host_profile_catalog

                return host_profile_catalog(bundle_root=bundle_root)

            catalog_loader = load_catalog
        if runtime_surface_factory is None:
            raise PackControlUnavailable("runtime surface composition is unavailable")
        self._runtime_surface, self._profile_changes = runtime_surface_factory(
            snapshot_loader=active_profile_loader,
            catalog_loader=catalog_loader,
            packvm_readiness_reader=packvm_readiness_reader,
            bundle_root=bundle_root,
        )

    @classmethod
    def capture(
        cls,
        *,
        active: Any | None = None,
        packvm_readiness_reader: Callable[[], Mapping[str, Any]] | None = None,
        runtime_surface_factory: RuntimeSurfaceFactory | None = None,
    ) -> "CapturedPackControlSession":
        """Capture the active Profile and canonical catalog revisions."""
        return cls(
            _capture_binding(active),
            packvm_readiness_reader=packvm_readiness_reader,
            runtime_surface_factory=runtime_surface_factory,
        )

    @property
    def profile_id(self) -> str:
        return self._binding.profile_id

    @property
    def plan_digest(self) -> str:
        return self._binding.plan_digest

    def provider_metadata(self, contract_id: str) -> tuple[Mapping[str, Any], ...]:
        """Expose only this Host-owned qualified control provider."""
        if contract_id == CONTROL_PRESENTATION_CONTRACT:
            return (
                {
                    "provider_id": "tobkiri.host.control-presentation",
                    "contract_id": CONTROL_PRESENTATION_CONTRACT,
                    "operations": sorted(CONTROL_PRESENTATION_OPERATIONS),
                    "profile_id": self.profile_id,
                    "plan_digest": self.plan_digest,
                },
            )
        if contract_id != PACK_CONTROL_CONTRACT:
            return ()
        return (
            {
                "provider_id": "tobkiri.host.pack-control",
                "contract_id": PACK_CONTROL_CONTRACT,
                "operations": sorted(PACK_CONTROL_OPERATIONS),
                "profile_id": self.profile_id,
                "plan_digest": self.plan_digest,
            },
        )

    def bind_capability_reader(
        self,
        reader: Callable[[], Mapping[str, Any]],
    ) -> None:
        """Bind the Host's exact PackAPI capability snapshot once."""

        self._runtime_surface.bind_capability_reader(reader)

    def cancel_pending_reads(self) -> None:
        """Fence reads owned by a stopping HTTP server without closing capture."""

        self._runtime_surface.cancel_pending_reads()

    def close(self) -> None:
        """Release the captured runtime read boundary idempotently."""

        self._runtime_surface.close()

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
        *,
        version_range: str = ">=4,<5",
    ) -> Mapping[str, Any]:
        """Invoke one qualified operation after exact session/profile checks."""
        del version_range
        from .bootstrap.profile_capture import profile_capture_scope

        if contract_id == CONTROL_PRESENTATION_CONTRACT:
            with profile_capture_scope():
                return self._invoke_control_presentation(operation_id, payload)
        if contract_id != PACK_CONTROL_CONTRACT:
            raise PackControlUnapproved("contract is absent from the captured Host session")
        if operation_id not in PACK_CONTROL_OPERATIONS:
            raise PackControlUnapproved("operation is absent from the captured Host session")
        arguments = dict(payload)
        session_id = _required(arguments.pop("_session_id", None), "session binding")
        self._reject_identity_override(arguments)
        if "approved" in arguments:
            raise PackControlUnapproved("client approval assertions are not trusted")
        with profile_capture_scope():
            if operation_id == "approval.revoke":
                # A known read-only denial must not queue behind another
                # control mutation.  The preflight is fail-closed: if the
                # approval is absent or invalid it raises before acquiring the
                # session lock, while an approved request's fresh capture is
                # compared again at the lock boundary.
                current_binding, _active = self._capture_current_binding()
                self._require_captured_binding(current_binding)
                self._raise_known_revoke_denial(arguments)
                from .bootstrap.profile_capture import (
                    invalidate_profile_capture_scope,
                )

                invalidate_profile_capture_scope()
                current_binding, _active = self._capture_current_binding()
                with self._lock:
                    self._require_captured_binding(current_binding)
                    return self._revoke_approval(arguments)
            from .bootstrap.profile_capture import invalidate_profile_capture_scope

            if operation_id not in {
                "catalog.read",
                "dashboard.read",
                "pack.status",
            }:
                # The HTTP boundary has already checked the captured session.
                # Every operation that can mutate authority, Profile state, or
                # restart behavior must discard that snapshot before its own
                # binding check. Read-only projections may reuse it only within
                # this one explicit operation scope.
                invalidate_profile_capture_scope()
            if operation_id == "profile.reload":
                with self._lock:
                    self._recapture()
                    return self._status(arguments)
            current_binding, active = self._capture_current_binding()
            with self._lock:
                self._require_captured_binding(current_binding)
                if operation_id == "catalog.read":
                    return self._catalog_payload(active_snapshot=active)
                if operation_id == "dashboard.read":
                    return self._dashboard(active_snapshot=active)
                if operation_id == "pack.install":
                    return self._install(arguments)
                if operation_id == "approval.candidate":
                    return self._approval_candidate(arguments, session_id)
                if operation_id == "approval.approve":
                    return self._approve(arguments, session_id)
                if operation_id == "pack.enable":
                    return self._set_enabled(arguments, True)
                if operation_id == "pack.disable":
                    return self._set_enabled(arguments, False)
                if operation_id == "pack.status":
                    return self._status(arguments, active_snapshot=active)
                if operation_id == "runtime.restart":
                    from .restart_control import request_kernel_restart

                    request_kernel_restart()
                    return {"restart_requested": True, **self._binding_payload()}
        raise PackControlUnapproved("qualified operation is unavailable")

    def _invoke_control_presentation(
        self,
        operation_id: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Serve declared Launcher surfaces only through the captured Broker."""

        if operation_id not in CONTROL_PRESENTATION_OPERATIONS:
            raise PackControlUnapproved("control presentation operation is not declared")
        arguments = dict(payload)
        session_id = _required(arguments.pop("_session_id", None), "session binding")
        selected_profile_id = arguments.pop("selected_profile_id", None)
        allow_selected_profile = operation_id in {
            "profile.read",
            "profile.catalog.read",
            "profile.change.resolve",
            "settings.read",
        }
        allow_selected_profile = allow_selected_profile or operation_id.startswith("topology.")
        self._reject_identity_override(
            arguments,
            allow_selected_profile=allow_selected_profile,
        )
        if "approved" in arguments or "approval_token" in arguments:
            raise PackControlUnapproved("client approval assertions are not trusted")
        try:
            if operation_id == "profile.read":
                requested_profile_id = arguments.pop("profile_id", None)
                if selected_profile_id is not None and requested_profile_id is not None:
                    if str(selected_profile_id) != str(requested_profile_id):
                        raise PackControlInvalidRequest("browsing Profile selectors disagree")
                requested_profile_id = (
                    selected_profile_id if selected_profile_id is not None else requested_profile_id
                )
                profile_result = self._runtime_surface.read_profile(
                    profile_id=(
                        str(requested_profile_id) if requested_profile_id is not None else None
                    ),
                    expected_profile_revision=_optional_string(
                        arguments.pop("expected_profile_revision", None)
                    ),
                    expected_plan_digest=_optional_string(
                        arguments.pop("expected_plan_digest", None)
                    ),
                )
                return {**dict(profile_result), **_require_empty(arguments)}
            if operation_id == "profile.catalog.read":
                requested_profile_id = arguments.pop("profile_id", None)
                if selected_profile_id is not None and requested_profile_id is not None:
                    if str(selected_profile_id) != str(requested_profile_id):
                        raise PackControlInvalidRequest("browsing Profile selectors disagree")
                requested_profile_id = (
                    selected_profile_id if selected_profile_id is not None else requested_profile_id
                )
                _require_empty(arguments)
                return self._runtime_surface.read_profile_catalog(
                    session_id=_panel_session_root(session_id),
                    profile_id=(
                        str(requested_profile_id) if requested_profile_id is not None else None
                    ),
                )
            if operation_id == "operation.status.read":
                request_id = _required(
                    arguments.pop("request_id", None),
                    "operation request identity",
                )
                _require_empty(arguments)
                from .bootstrap.profile_capture import runtime_user_data_root
                from .control_reconciliation_v4 import (
                    ControlReconciliationConflictError,
                    ControlReconciliationError,
                    ControlReconciliationStore,
                    ControlReconciliationUnavailableError,
                )

                try:
                    return ControlReconciliationStore(
                        runtime_user_data_root() / "control" / "reconciliation-v4.sqlite3"
                    ).operation_status(
                        request_id,
                        session_id=_panel_session_root(session_id),
                    )
                except ControlReconciliationConflictError as error:
                    raise PackControlConflict(
                        "operation status conflicts with its session binding"
                    ) from error
                except (
                    ControlReconciliationUnavailableError,
                    ControlReconciliationError,
                ) as error:
                    raise PackControlUnavailable("operation status is unavailable") from error
            if operation_id == "settings.read":
                requested_profile_id = arguments.pop("profile_id", None)
                if selected_profile_id is not None and requested_profile_id is not None:
                    if str(selected_profile_id) != str(requested_profile_id):
                        raise PackControlInvalidRequest("browsing Profile selectors disagree")
                requested_profile_id = (
                    selected_profile_id if selected_profile_id is not None else requested_profile_id
                )
                _require_empty(arguments)
                return self._runtime_surface.read_settings(
                    profile_id=(
                        str(requested_profile_id) if requested_profile_id is not None else None
                    )
                )
            if operation_id.startswith("topology."):
                view = operation_id.removeprefix("topology.").removesuffix(".read")
                requested_profile_id = arguments.pop("profile_id", None)
                if (
                    selected_profile_id is not None
                    and requested_profile_id is not None
                    and str(selected_profile_id) != str(requested_profile_id)
                ):
                    raise PackControlInvalidRequest("browsing Profile selectors disagree")
                requested_profile_id = (
                    selected_profile_id if selected_profile_id is not None else requested_profile_id
                )
                revision = _optional_string(arguments.pop("expected_profile_revision", None))
                plan_digest = _optional_string(arguments.pop("expected_plan_digest", None))
                _require_empty(arguments)
                return self._runtime_surface.read_advanced(
                    view,
                    expected_profile_revision=revision,
                    expected_plan_digest=plan_digest,
                    profile_id=(
                        str(requested_profile_id) if requested_profile_id is not None else None
                    ),
                )
            if operation_id == "profile.change.resolve" and selected_profile_id is not None:
                requested_profile_id = arguments.get("profile_id")
                if requested_profile_id is not None and str(requested_profile_id) != str(
                    selected_profile_id
                ):
                    raise PackControlInvalidRequest("browsing Profile selectors disagree")
                arguments["profile_id"] = str(selected_profile_id)
            action = operation_id.removeprefix("profile.change.")
            handler = getattr(self._profile_changes, action)
            result = handler(arguments, session_id=_panel_session_root(session_id))
            if action == "activate":
                self._recapture()
                result = {
                    **result,
                    "authoritative_snapshot": self._runtime_surface.read_profile(),
                }
            return result
        except Exception as error:
            as_dict = getattr(error, "as_dict", None)
            if callable(as_dict):
                return as_dict()
            raise

    def _reject_identity_override(
        self,
        arguments: Mapping[str, Any],
        *,
        allow_selected_profile: bool = False,
    ) -> None:
        expected = {
            "profile_id": self._binding.profile_id,
            "workspace_id": self._binding.workspace_id,
            "profile_revision": self._binding.profile_revision,
            "plan_digest": self._binding.plan_digest,
            "catalog_revision": self._binding.catalog_revision,
        }
        for key, value in expected.items():
            if key == "profile_id" and allow_selected_profile:
                continue
            supplied = arguments.get(key)
            if supplied is not None and not hmac.compare_digest(str(supplied), value):
                if key == "profile_revision":
                    raise PackControlStaleRevision("captured profile_revision does not match")
                raise PackControlDigestMismatch(f"captured {key} does not match")

    def _capture_current_binding(self) -> tuple[_Binding, Any]:
        """Capture current Profile authority without holding session state."""

        from .bootstrap.profile_capture import capture_active_profile

        try:
            active = capture_active_profile()
        except Exception as error:
            raise PackControlDigestMismatch(
                "active v4 Profile session is missing or invalid"
            ) from error
        return _capture_binding(active), active

    def _require_captured_binding(self, current: _Binding) -> None:
        """Compare a fresh external capture at the session lock boundary."""

        if current != self._binding:
            raise PackControlStaleRevision("captured Profile session is stale")

    def _recapture(self) -> None:
        from .bootstrap.profile_capture import invalidate_profile_capture_scope

        invalidate_profile_capture_scope()
        self._binding = _capture_binding()
        self._candidates.clear()

    def _raise_known_revoke_denial(self, arguments: Mapping[str, Any]) -> None:
        """Reject a read-only revoke denial before taking the session lock."""

        pack_id = _installed_pack(arguments, self._binding)
        if pack_id in _required_profile_pack_ids(self._binding.profile_id):
            raise PackControlUnapproved("required Pack approval cannot be revoked")
        record = load_pack_catalog()[pack_id]
        approved, reason = _approval_status(pack_id, record, self._binding)
        if not approved:
            _raise_approval_failure(reason)

    def _catalog_payload(
        self,
        *,
        active_snapshot: Any | None = None,
    ) -> dict[str, Any]:
        return _catalog_payload(self._binding, active_snapshot=active_snapshot)

    def _install(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        pack_id, record, root = _pack(arguments)
        content_digest = _pack_snapshot(pack_id, root)
        state = _read_control_state(self._binding.profile_id)
        state[pack_id] = {
            "artifact_digest": _record_digest(record),
            "pack_artifact_digest": _pack_manifest_artifact_digest(pack_id),
            "content_digest": content_digest,
            "catalog_revision": self._binding.catalog_revision,
        }
        _write_control_state(self._binding.profile_id, state)
        return {"pack_id": pack_id, "installed": True, **self._binding_payload()}

    def _approval_candidate(self, arguments: Mapping[str, Any], session_id: str) -> dict[str, Any]:
        pack_id = _installed_pack(arguments, self._binding)
        snapshot_digest = _pack_snapshot(pack_id, resolve_pack_root(pack_id))
        candidate_id = secrets.token_urlsafe(32)
        candidate = _ApprovalCandidate(
            candidate_id=candidate_id,
            session_id=session_id,
            pack_id=pack_id,
            snapshot_digest=snapshot_digest,
            profile_revision=self._binding.profile_revision,
            catalog_revision=self._binding.catalog_revision,
            expires_at=time.time() + _CANDIDATE_TTL_SECONDS,
        )
        self._candidates[candidate_id] = candidate
        return {
            "candidate_id": candidate_id,
            "pack_id": pack_id,
            "snapshot_digest": candidate.snapshot_digest,
            "expires_in": int(_CANDIDATE_TTL_SECONDS),
            **self._binding_payload(),
        }

    def _approve(self, arguments: Mapping[str, Any], session_id: str) -> dict[str, Any]:
        pack_id = _installed_pack(arguments, self._binding)
        candidate_id = _required(arguments.get("candidate_id"), "approval candidate")
        candidate = self._candidates.pop(candidate_id, None)
        if candidate is None:
            raise PackControlConflict("approval candidate is missing or already used")
        if (
            candidate.expires_at <= time.time()
            or candidate.session_id != session_id
            or candidate.pack_id != pack_id
            or candidate.profile_revision != self._binding.profile_revision
            or candidate.catalog_revision != self._binding.catalog_revision
        ):
            raise PackControlStaleRevision("approval candidate binding is invalid or stale")
        current_digest = _pack_snapshot(pack_id, resolve_pack_root(pack_id))
        if not hmac.compare_digest(current_digest, candidate.snapshot_digest):
            raise PackControlDigestMismatch("Pack contents changed after approval was requested")
        _persist_approval(
            pack_id,
            current_digest,
            self._binding,
            approval_nonce=candidate.candidate_id,
        )
        self._recapture()
        return {
            "pack_id": pack_id,
            "approved": True,
            "approval_status": "approved",
            **self._binding_payload(),
        }

    def _revoke_approval(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        pack_id = _installed_pack(arguments, self._binding)
        if pack_id in _required_profile_pack_ids(self._binding.profile_id):
            raise PackControlUnapproved("required Pack approval cannot be revoked")
        record = load_pack_catalog()[pack_id]
        approval = _load_valid_approval(pack_id, record, self._binding)
        approval_revision = str(approval["approval_revision"])
        state, profile = _active_profile()
        active_pack_ids = [str(item) for item in profile.get("packs") or []]
        if pack_id == str(profile.get("base_pack") or ""):
            raise PackControlUnapproved("the active Base Pack approval cannot be revoked")
        artifact_digest = _pack_manifest_artifact_digest(pack_id)
        from .authority.v4 import AuthorityStore

        authority_path = _user_data_root() / "authority" / "v4.sqlite3"
        try:
            with AuthorityStore(authority_path) as authority:
                revocation_id, grant_ids = authority.revoke_pack_approval(
                    pack_id=pack_id,
                    approval_revision=approval_revision,
                    profile_id=self._binding.profile_id,
                    activation_id=str(state["activation"]["activation_id"]),
                    artifact_digest=artifact_digest,
                    reason=f"Pack approval revoked: {pack_id}",
                )
        except Exception as error:
            raise PackControlUnavailable("Pack approval revocation was not committed") from error

        if pack_id in active_pack_ids:
            active_pack_ids.remove(pack_id)
            try:
                _activate_pack_set(state, active_pack_ids)
            except PackControlDenied:
                raise
            except Exception as error:
                raise PackControlUnavailable(
                    "Pack approval was fenced but Profile deactivation failed"
                ) from error
        _persist_revoked_approval(
            pack_id,
            approval,
            revocation_id=revocation_id,
        )
        self._recapture()
        return {
            "pack_id": pack_id,
            "approved": False,
            "enabled": False,
            "approval_status": "revoked",
            "approval_revision": approval_revision,
            "revocation_id": revocation_id,
            "revoked_grant_count": len(grant_ids),
            **self._binding_payload(),
        }

    def _set_enabled(self, arguments: Mapping[str, Any], enabled: bool) -> dict[str, Any]:
        pack_id = _installed_pack(arguments, self._binding)
        if not enabled and pack_id in _required_profile_pack_ids(self._binding.profile_id):
            raise PackControlUnapproved("required Pack cannot be disabled")
        record = load_pack_catalog()[pack_id]
        approved, reason = _approval_status(pack_id, record, self._binding)
        if enabled and not approved:
            _raise_approval_failure(reason)
        state, profile = _active_profile()
        packs = [str(item) for item in profile.get("packs") or []]
        if enabled and pack_id in packs:
            return {"pack_id": pack_id, "enabled": True, **self._binding_payload()}
        if not enabled and pack_id not in packs:
            return {"pack_id": pack_id, "enabled": False, **self._binding_payload()}
        if enabled and pack_id not in packs:
            packs.append(pack_id)
        if not enabled and pack_id in packs:
            if pack_id == str(profile.get("base_pack") or ""):
                raise PackControlUnapproved("the active Base Pack cannot be disabled")
            packs.remove(pack_id)
        _activate_pack_set(state, packs)
        self._recapture()
        return {"pack_id": pack_id, "enabled": enabled, **self._binding_payload()}

    def _status(
        self,
        arguments: Mapping[str, Any],
        *,
        active_snapshot: Any | None = None,
    ) -> dict[str, Any]:
        pack_id = str(arguments.get("pack_id") or "").strip()
        catalog = self._catalog_payload(active_snapshot=active_snapshot)
        if not pack_id:
            return catalog
        match = next((item for item in catalog["packs"] if item["pack_id"] == pack_id), None)
        if match is None:
            raise PackControlInvalidRequest("Pack is absent from the canonical v4 catalog")
        return match

    def _dashboard(
        self,
        *,
        active_snapshot: Any | None = None,
    ) -> dict[str, Any]:
        """Return the finite Home projection from the captured Pack state."""

        catalog = self._catalog_payload(active_snapshot=active_snapshot)
        packs = catalog["packs"]
        enabled = sum(1 for item in packs if item["enabled"] is True)
        return {
            "packs": {
                "total": len(packs),
                "enabled": enabled,
                "disabled": len(packs) - enabled,
            },
            "flows": {"total": 0},
            "kernel": {"status": "running", "uptime": None},
            "profile": None,
            "supervisor": None,
            **self._binding_payload(),
        }

    def _binding_payload(self) -> dict[str, str]:
        return {
            "profile_id": self._binding.profile_id,
            "workspace_id": self._binding.workspace_id,
            "profile_revision": self._binding.profile_revision,
            "plan_digest": self._binding.plan_digest,
            "catalog_revision": self._binding.catalog_revision,
        }


def capture_pack_control_session(
    *,
    active: Any | None = None,
    packvm_readiness_reader: Callable[[], Mapping[str, Any]] | None = None,
    active_profile_loader: Callable[[], Any] | None = None,
    bundle_root: Path | None = None,
    runtime_surface_factory: RuntimeSurfaceFactory | None = None,
) -> CapturedPackControlSession:
    """Capture the Pack control session used by the production HTTP surface."""
    return CapturedPackControlSession(
        _capture_binding(active),
        packvm_readiness_reader=packvm_readiness_reader,
        active_profile_loader=active_profile_loader,
        bundle_root=bundle_root,
        runtime_surface_factory=runtime_surface_factory,
    )


def capture_pack_control_catalog(*, active: Any | None = None) -> Mapping[str, Any]:
    """Capture the authoritative lifecycle projection for the active Profile."""

    binding = _capture_binding() if active is None else _binding_for_resolved(active.resolved)
    return _catalog_payload(binding, active_snapshot=active)


class CapturedPackCatalogReader:
    """Finite read-only Pack catalog provider bound to one active Profile."""

    def __init__(self, binding: _Binding) -> None:
        self._binding = binding

    @classmethod
    def capture(cls) -> "CapturedPackCatalogReader":
        """Capture the current committed Profile and catalog revisions."""

        return cls(_capture_binding())

    @property
    def binding(self) -> _Binding:
        """Return the immutable captured binding for Host authority wiring."""

        return self._binding

    def read(self) -> dict[str, Any]:
        """Read the catalog only while the committed snapshot remains current."""

        if _capture_binding() != self._binding:
            raise PackControlStaleRevision("captured Profile session is stale")
        return _catalog_payload(self._binding)


def capture_pack_catalog_reader() -> CapturedPackCatalogReader:
    """Capture the finite catalog Provider used by production dispatch."""

    return CapturedPackCatalogReader.capture()


def capture_valid_pack_approval(pack_id: str) -> Mapping[str, Any]:
    """Load one exact current signed optional-Pack approval for Authority capture."""

    binding = _capture_binding()
    record = load_pack_catalog().get(pack_id)
    if record is None:
        raise PackControlInvalidRequest("Pack is absent from the canonical v4 catalog")
    return _load_valid_approval(pack_id, record, binding)


def _binding_payload(binding: _Binding) -> dict[str, str]:
    return {
        "profile_id": binding.profile_id,
        "workspace_id": binding.workspace_id,
        "profile_revision": binding.profile_revision,
        "plan_digest": binding.plan_digest,
        "catalog_revision": binding.catalog_revision,
    }


def _catalog_payload(
    binding: _Binding,
    *,
    active_snapshot: Any | None = None,
) -> dict[str, Any]:
    installed = _read_control_state(binding.profile_id)
    state, active_profile = _active_profile(active_snapshot)
    active = set(active_profile.get("packs") or [])
    active_grant_bindings = _active_grant_bindings(state)
    required_pack_ids = _required_profile_pack_ids(
        binding.profile_id,
        active_snapshot=active_snapshot,
    )
    plan_bindings = {
        (str(item.get("contract_id") or ""), str(item.get("operation_id") or ""))
        for item in state["resolved_plan"].get("bindings") or []
        if isinstance(item, Mapping)
    }
    packs = []
    for pack_id, record in sorted(load_pack_catalog().items()):
        is_installed = pack_id in installed or pack_id in active
        if pack_id in installed:
            _require_install_binding(pack_id, record, installed[pack_id], binding)
        # Packs committed to the immutable active Profile are baseline
        # capabilities, not optional installs.  Their trust is established by
        # the captured Profile and bundle validation; requiring a mutable
        # optional-pack approval would make the baseline unavailable after a
        # fresh installation.  Optional packs keep the normal approval path.
        is_committed_baseline = pack_id in required_pack_ids and pack_id in active
        if is_committed_baseline:
            approved, reason = True, None
        else:
            approved, reason = _approval_status(
                pack_id,
                record,
                binding,
            )
        status = "approved" if approved else "installed"
        declared_operations = _declared_operations(record)
        invokable_operation_keys = {
            (operation["contract_id"], operation["operation_id"])
            for operation in declared_operations
            if (
                pack_id in active
                and approved
                and (operation["contract_id"], operation["operation_id"]) in plan_bindings
                and (operation["contract_id"], operation["operation_id"]) in active_grant_bindings
            )
        }
        operations = [
            {
                **operation,
                "invokable": (operation["contract_id"], operation["operation_id"])
                in invokable_operation_keys,
            }
            for operation in declared_operations
        ]
        packs.append(
            {
                "pack_id": pack_id,
                "name": str(record.get("display_name") or pack_id),
                "version": str(record.get("version") or "0.0.0"),
                "description": str(record.get("description") or ""),
                "is_core": record.get("kind") == "base",
                "required": pack_id in required_pack_ids,
                "installed": is_installed,
                "enabled": pack_id in active and approved,
                "approved": bool(approved),
                "approval_status": status,
                "approval_reason": reason,
                "hash_valid": True if approved else None,
                "critical_changed": reason == "hash_mismatch",
                "approval_issues": [] if approved else [reason or "approval_required"],
                "artifact_digest": _record_digest(record),
                "capabilities": _capability_projection(record),
                "flows": [str(operation["operation_id"]) for operation in declared_operations],
                "dependencies": sorted(
                    str(dependency) for dependency in (record.get("dependencies") or {})
                ),
                "operations_api_version": "io.tobkiri.pack-operations.v1",
                "operations": operations,
                **_binding_payload(binding),
            }
        )
    return {"packs": packs, "count": len(packs), **_binding_payload(binding)}


def _required_profile_pack_ids(
    profile_id: str,
    *,
    active_snapshot: Any | None = None,
) -> frozenset[str]:
    """Return the static Pack closure declared by one registry Profile.

    The active snapshot may also contain approved optional Packs. Those Packs
    remain subject to their mutable approval records, so they must not become
    baseline Packs merely because they are present in the active effective set.
    """

    if active_snapshot is not None:
        active_profile_id = str(active_snapshot.resolved.profile["profile_id"])
        if active_profile_id != profile_id:
            raise PackControlDigestMismatch("active Profile does not match the lifecycle binding")
    from .bootstrap.profile_capture import host_profile_catalog

    catalog = host_profile_catalog()
    source = catalog.profiles.get(profile_id)
    if source is None:
        raise PackControlDigestMismatch("selected Profile is unavailable")
    selected = [str(item["pack_id"]) for item in source["packs"]]
    pending = list(selected)
    while pending:
        current_id = pending.pop(0)
        manifest = catalog.packs.get(current_id)
        if manifest is None:
            raise PackControlDigestMismatch("selected Profile dependency is unavailable")
        for dependency_id in manifest["requirements"]["pack_dependencies"]:
            dependency = str(dependency_id)
            if dependency not in selected:
                selected.append(dependency)
                pending.append(dependency)
    return frozenset(selected)


def _active_grant_bindings(state: Mapping[str, Any]) -> set[tuple[str, str]]:
    """Return current plan operations with a live, non-revoked Grant.

    A valid Pack approval file is necessary but not sufficient for an
    invokable projection.  The captured production Authority store must also
    contain the exact current activation Grant for that operation.
    """

    activation = state.get("activation")
    profile = state.get("resolved_profile")
    if not isinstance(activation, Mapping) or not isinstance(profile, Mapping):
        return set()
    expected_activation = str(activation.get("activation_id") or "")
    expected_profile = str(profile.get("profile_id") or "")
    expected_epoch = activation.get("security_epoch")
    from .authority.v4 import FunctionPrincipal

    plan_bindings: set[tuple[str, str, str]] = set()
    for item in state.get("resolved_plan", {}).get("bindings") or []:
        if not isinstance(item, Mapping) or not isinstance(item.get("function_principal"), Mapping):
            continue
        try:
            principal = FunctionPrincipal.from_dict(item["function_principal"])
        except Exception:
            continue
        plan_bindings.add(
            (
                str(item.get("contract_id") or ""),
                str(item.get("operation_id") or ""),
                principal.principal_id,
            )
        )
    if not expected_activation or not expected_profile or not isinstance(expected_epoch, int):
        return set()
    from .authority.v4 import AuthorityStore

    try:
        with AuthorityStore(_user_data_root() / "authority" / "v4.sqlite3") as authority:
            result: set[tuple[str, str]] = set()
            for grant in authority.list_grants():
                if (
                    grant.revoked
                    or grant.profile_id != expected_profile
                    or grant.activation_id != expected_activation
                    or grant.security_epoch != expected_epoch
                    or authority.is_revoked("grant", grant.grant_id)
                ):
                    continue
                dimensions = grant.scope.dimensions
                contracts = tuple(dimensions.get("contract", ()))
                operations = tuple(dimensions.get("operation", ()))
                if len(contracts) != 1 or len(operations) != 1:
                    continue
                candidate = (contracts[0], operations[0], grant.target.principal_id)
                if candidate in plan_bindings:
                    result.add((contracts[0], operations[0]))
            return result
    except Exception:
        return set()


def _capability_projection(record: Mapping[str, Any]) -> list[dict[str, str]]:
    """Project only capability names declared by the generated v4 catalog."""

    capabilities = record.get("capabilities")
    if not isinstance(capabilities, list):
        return []
    return [
        {
            "name": capability,
            "description": f"Pack-declared capability: {capability}.",
        }
        for capability in sorted({str(item).strip() for item in capabilities if str(item).strip()})
    ]


def _declared_operations(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return deterministic operation metadata from the v4 catalog record."""

    result: list[dict[str, Any]] = []
    contracts = record.get("provided_contracts")
    if not isinstance(contracts, list):
        return result
    for contract in contracts:
        if not isinstance(contract, Mapping):
            continue
        contract_id = str(contract.get("contract_id") or "").strip()
        provider_id = str(contract.get("provider_id") or "").strip()
        required = contract.get("required_capabilities")
        required_capabilities = (
            sorted({str(item).strip() for item in required if str(item).strip()})
            if isinstance(required, list)
            else []
        )
        operations = contract.get("operations")
        if not isinstance(operations, list):
            continue
        for operation in operations:
            if not isinstance(operation, Mapping):
                continue
            operation_id = str(operation.get("id") or "").strip()
            if not contract_id or not provider_id or not operation_id:
                continue
            result.append(
                {
                    "contract_id": contract_id,
                    "operation_id": operation_id,
                    "provider_id": provider_id,
                    "function_id": provider_id,
                    "required_capabilities": required_capabilities,
                    "capabilities": required_capabilities,
                    "input_schema": dict(operation.get("input_schema") or {}),
                }
            )
    return sorted(
        result,
        key=lambda item: (item["contract_id"], item["operation_id"]),
    )


def _capture_binding(active: Any | None = None) -> _Binding:
    state, profile = _active_profile(active)
    resolved_profile = state["resolved_profile"]
    catalog_revision = control_catalog_revision()
    profile_revision = "sha256:" + _digest(resolved_profile)
    catalog = load_pack_catalog()
    selected = tuple(str(item or "").strip() for item in profile.get("packs") or [])
    if not selected or len(selected) != len(set(selected)):
        raise PackControlDigestMismatch("active v4 Profile effective set is empty or duplicated")
    if any(pack_id not in catalog for pack_id in selected):
        raise PackControlDigestMismatch("active v4 Profile contains an unknown Pack")
    for pack_id in selected:
        resolve_pack_root(pack_id)
    return _Binding(
        profile_id=str(profile["profile_id"]),
        workspace_id=str(profile.get("workspace_id") or profile["profile_id"]),
        profile_revision=profile_revision,
        plan_digest=str(state["resolved_plan"]["plan_digest"]),
        catalog_revision=catalog_revision,
    )


def _binding_for_resolved(resolved: Any) -> _Binding:
    """Bind pre-activation Pack checks to the candidate Profile, not active state."""

    profile = resolved.profile
    plan = resolved.plan
    profile_id = str(profile.get("profile_id") or "")
    _safe_identity(profile_id, "Profile ID")
    return _Binding(
        profile_id=profile_id,
        workspace_id=profile_id,
        profile_revision=str(plan["profile_revision"]),
        plan_digest=str(plan["plan_digest"]),
        catalog_revision=control_catalog_revision(),
    )


def _active_profile(
    active: Any | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        if active is None:
            from .bootstrap.profile_capture import capture_active_profile

            active = capture_active_profile()
    except Exception as error:
        raise PackControlDigestMismatch(
            "active v4 Profile session is missing or invalid"
        ) from error
    resolved = active.resolved.profile
    installable_pack_ids = frozenset(load_pack_catalog())
    profile = {
        "profile_id": resolved["profile_id"],
        "workspace_id": resolved["profile_id"],
        "base_pack": resolved["base"]["pack_id"],
        "packs": [
            str(item["pack_id"])
            for item in resolved["packs"]
            if item.get("role") != "application" and str(item["pack_id"]) in installable_pack_ids
        ],
    }
    _safe_identity(profile.get("profile_id"), "Profile ID")
    _safe_identity(
        profile.get("workspace_id") or profile.get("profile_id"),
        "workspace ID",
    )
    return {
        "resolved_profile": dict(resolved),
        "resolved_plan": dict(active.resolved.plan),
        "activation": dict(active.activation),
    }, profile


def resolve_profile_pack_set(
    pack_ids: list[str],
    *,
    profile_id: str | None = None,
    expected_profile_definition_digest: str | None = None,
    expected_profile_catalog_digest: str | None = None,
    expected_bundle_lock_digest: str | None = None,
    bundle_root: Path | None = None,
    user_data_root: Path | None = None,
) -> Any:
    """Resolve a candidate Pack closure without activating or persisting it.

    This is the resolve step used by the Launcher recovery ceremony.  Pack
    installation and Pack approval remain prerequisites, but neither a client
    assertion nor resolving the candidate changes runtime state.
    """

    from .authority.v4 import AuthorityStore
    from .bootstrap.profile_capture import (
        _authority_reference,
        _authority_snapshot_digest,
        _edge_key,
        host_profile_catalog,
    )

    user_data = Path(user_data_root).resolve() if user_data_root is not None else _user_data_root()
    trusted_active_resolution = profile_id is None
    if profile_id is None:
        from .bootstrap.profile_capture import capture_active_profile

        profile_id = str(capture_active_profile().resolved.profile["profile_id"])
    catalog = (
        host_profile_catalog(bundle_root=bundle_root, user_data_root=user_data)
        if bundle_root is not None
        else host_profile_catalog(user_data_root=user_data)
    )
    from .profile_catalog_v4 import require_profile_catalog_binding

    authoritative_bindings = (
        expected_profile_definition_digest,
        expected_profile_catalog_digest,
        expected_bundle_lock_digest,
    )
    if any(value is not None for value in authoritative_bindings):
        if not all(isinstance(value, str) and value for value in authoritative_bindings):
            raise PackControlDigestMismatch("Profile catalog binding is incomplete")
        try:
            require_profile_catalog_binding(
                catalog,
                profile_id=profile_id,
                expected_definition_digest=str(expected_profile_definition_digest),
                expected_catalog_digest=str(expected_profile_catalog_digest),
                expected_bundle_lock_digest=str(expected_bundle_lock_digest),
            )
        except ValueError as error:
            raise PackControlDigestMismatch(
                "Profile catalog binding is stale or invalid"
            ) from error
    elif not trusted_active_resolution:
        raise PackControlUnapproved("selected Profile requires exact catalog bindings")
    external_packs = dict(catalog.packs)
    pending = list(dict.fromkeys(pack_ids))
    requested_closure: set[str] = set()
    while pending:
        pack_id = pending.pop(0)
        if pack_id not in requested_closure:
            requested_closure.add(pack_id)
        manifest = external_packs.get(pack_id)
        if manifest is None:
            record = load_pack_catalog().get(pack_id)
            if record is None:
                raise PackControlInvalidRequest("Pack is absent from the canonical v4 catalog")
            root = resolve_pack_root(pack_id)
            manifest_path = root / "pack.v4.json"
            if manifest_path.is_symlink() or not manifest_path.is_file():
                raise PackControlDigestMismatch("Pack v4 manifest is unavailable")
            try:
                manifest = validate_document(manifest_path.read_bytes(), "pack")
            except Exception as error:
                raise PackControlDigestMismatch("Pack v4 manifest is invalid") from error
            external_normal = record.get("authority") == "host-signed-external-normal-v4"
            if manifest["pack"]["id"] != pack_id or (
                external_normal
                and (
                    manifest["pack"]["kind"] != "normal_sandbox"
                    or manifest["pack"]["artifact_digest"] != record.get("artifact_digest")
                )
            ):
                raise PackControlDigestMismatch("Pack v4 manifest identity is inconsistent")
            external_packs[pack_id] = manifest
        dependencies = set(manifest["requirements"]["pack_dependencies"])
        pending.extend(
            dependency for dependency in sorted(dependencies) if dependency not in requested_closure
        )
    runtime = require_profile_runtime()
    catalog = runtime.catalog_with_packs(catalog, external_packs)
    source = catalog.profiles.get(profile_id)
    if source is None:
        raise PackControlInvalidRequest("selected Profile is unavailable")
    if all(value is not None for value in authoritative_bindings):
        definition_pack_ids = {
            str(item["pack_id"]) for item in source["packs"] if item.get("role") != "application"
        }
        if set(pack_ids) != definition_pack_ids or len(pack_ids) != len(definition_pack_ids):
            raise PackControlDigestMismatch(
                "selected Profile Pack set does not match its canonical definition"
            )

    requested = tuple(dict.fromkeys(str(pack_id) for pack_id in pack_ids))
    if len(requested) != len(pack_ids) or any(
        pack_id not in catalog.packs for pack_id in requested
    ):
        raise PackControlInvalidRequest("requested Profile Pack set is invalid")

    authority_path = user_data / "authority" / "v4.sqlite3"
    with AuthorityStore(authority_path) as authority:
        effective_bundle_root = Path(catalog.root)
        bundle_lock_digest = (
            "sha256:"
            + hashlib.sha256((effective_bundle_root / "bundle.lock.json").read_bytes()).hexdigest()
        )
        snapshot_digest = _authority_snapshot_digest(authority, bundle_lock_digest)
        bindings = {
            _edge_key(edge): _authority_reference(edge, snapshot_digest)
            for edge in source["requested_edges"]
        }
        verified_digests = {
            str(manifest["pack"]["artifact_digest"]) for manifest in catalog.packs.values()
        }
        baseline = runtime.resolve_profile(
            catalog,
            profile_id,
            approved_artifact_digests=verified_digests,
            authority_snapshot_digest=snapshot_digest,
            authority_bindings=bindings,
            security_epoch=authority.security_epoch,
        )
        mandatory = {
            str(item["pack_id"])
            for item in baseline.profile["packs"]
            if item.get("role") != "application"
        }
        if all(value is not None for value in authoritative_bindings):
            requested = tuple(dict.fromkeys((*requested, *sorted(mandatory))))
        if not mandatory.issubset(requested):
            raise PackControlUnapproved("the selected Profile Pack set omits a mandatory Pack")
        optional_pack_ids = tuple(pack_id for pack_id in requested if pack_id not in mandatory)
        optional_dependencies = {
            str(dependency_id)
            for pack_id in optional_pack_ids
            for dependency_id in catalog.packs[pack_id]["requirements"]["pack_dependencies"]
            if str(dependency_id) in optional_pack_ids
        }
        # A resolved Profile persists its complete effective Pack closure.
        # Only closure roots were selected by the user; projecting every
        # dependency again as a Shell-owned optional Pack would give the same
        # Pack conflicting callers and broader authority than its signed
        # parent dependency permits.
        additional_pack_ids = tuple(
            pack_id for pack_id in optional_pack_ids if pack_id not in optional_dependencies
        )
        if optional_pack_ids and not additional_pack_ids:
            raise PackControlUnapproved("optional Pack dependency closure has no root")
        # Optional Pack operations are part of the immutable resolved Profile,
        # so mint the exact authority references before resolving the plan.
        # The resolver derives only the selected Pack/dependency closure and
        # binds every operation to the selected Shell caller.
        for edge in runtime.dynamic_profile_edges(catalog, profile_id, additional_pack_ids):
            bindings[_edge_key(edge)] = _authority_reference(edge, snapshot_digest)
        approved_digests = {str(item["artifact_digest"]) for item in baseline.lock["effective_set"]}
        installed = _read_control_state(profile_id)
        binding = _binding_for_resolved(baseline)
        selected_optional = requested_closure - mandatory
        for pack_id in sorted(selected_optional):
            record = load_pack_catalog()[pack_id]
            if pack_id not in installed:
                raise PackControlConflict("Pack must be installed before activation")
            _require_install_binding(
                pack_id,
                record,
                installed[pack_id],
                binding,
            )
            approved, reason = _approval_status(pack_id, record, binding)
            if not approved:
                _raise_approval_failure(reason)
            approved_digests.add(str(catalog.packs[pack_id]["pack"]["artifact_digest"]))
        resolved = runtime.resolve_profile(
            catalog,
            profile_id,
            approved_artifact_digests=approved_digests,
            authority_snapshot_digest=snapshot_digest,
            authority_bindings=bindings,
            security_epoch=authority.security_epoch,
            additional_pack_ids=additional_pack_ids,
        )
    return resolved


def activate_resolved_profile_pack_set(
    resolved: Any,
    *,
    activation_id: str,
    expected_profile_revision: str,
    expected_plan_digest: str,
    expected_activation_id: str,
    bundle_root: Path | None = None,
    user_data_root: Path | None = None,
) -> Mapping[str, Any]:
    """Activate one reviewed candidate if its captured predecessor is current."""

    from .authority.v4 import AuthorityStore
    from .bootstrap.profile_capture import (
        _ensure_profile_workspace,
        host_profile_catalog,
    )
    from .active_profile_store_v4 import (
        ActiveProfileStore,
        ActiveProfileStoreConflict,
    )

    user_data = Path(user_data_root).resolve() if user_data_root is not None else _user_data_root()
    profile_id = str(resolved.profile["profile_id"])
    workspace = _ensure_profile_workspace(user_data, profile_id)
    pointers = ActiveProfileStore(user_data)
    expects_no_active = (
        hmac.compare_digest(expected_profile_revision, NO_ACTIVE_PROFILE_REVISION)
        and hmac.compare_digest(expected_plan_digest, NO_ACTIVE_PLAN_DIGEST)
        and hmac.compare_digest(expected_activation_id, NO_ACTIVE_ACTIVATION_ID)
    )
    predecessor = pointers.load(verify_snapshot=True)
    if predecessor is None and not expects_no_active:
        raise PackControlStaleRevision("reviewed Profile predecessor is absent")
    if predecessor is not None and expects_no_active:
        raise PackControlStaleRevision("reviewed empty predecessor is stale")
    candidate_profile_revision = str(resolved.plan["profile_revision"])
    candidate_plan_digest = str(resolved.plan["plan_digest"])
    candidate_lock_digest = str(resolved.lock["lock_digest"])
    pointer_is_candidate = predecessor is not None and (
        predecessor.profile_id == profile_id
        and hmac.compare_digest(predecessor.profile_revision, candidate_profile_revision)
        and hmac.compare_digest(predecessor.plan_digest, candidate_plan_digest)
        and hmac.compare_digest(predecessor.lock_digest, candidate_lock_digest)
        and hmac.compare_digest(predecessor.activation_id, activation_id)
    )
    predecessor_is_expected = predecessor is not None and (
        hmac.compare_digest(predecessor.profile_revision, expected_profile_revision)
        and hmac.compare_digest(predecessor.plan_digest, expected_plan_digest)
        and hmac.compare_digest(predecessor.activation_id, expected_activation_id)
    )
    if not expects_no_active and not predecessor_is_expected and not pointer_is_candidate:
        if predecessor is None:
            raise PackControlStaleRevision("reviewed Profile predecessor is absent")
        if (
            predecessor.profile_id == profile_id
            and hmac.compare_digest(predecessor.profile_revision, candidate_profile_revision)
            and hmac.compare_digest(predecessor.plan_digest, candidate_plan_digest)
        ):
            raise require_profile_runtime().denied("activation predecessor is stale")
        raise PackControlStaleRevision("reviewed Profile predecessor is stale")
    catalog = (
        host_profile_catalog(bundle_root=bundle_root, user_data_root=user_data)
        if bundle_root is not None
        else host_profile_catalog()
    )
    with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
        store = require_profile_runtime().activation_store(
            root=workspace / "activation",
            workspace=workspace,
            profile_id=profile_id,
            authority=authority,
            catalog=catalog,
        )
        active_pointer = workspace / "activation" / "active.json"
        activation: Mapping[str, Any]
        if active_pointer.is_file():
            active = store.load_active_snapshot()
            if pointer_is_candidate:
                if (
                    not hmac.compare_digest(str(active.activation["activation_id"]), activation_id)
                    or active.resolved != resolved
                ):
                    raise PackControlConflict(
                        "Host active pointer is bound to another Profile activation"
                    )
                activation = dict(active.activation)
            elif hmac.compare_digest(str(active.activation["activation_id"]), activation_id):
                if active.resolved != resolved:
                    raise PackControlConflict(
                        "activation identity is bound to another resolved Profile"
                    )
                activation = dict(active.activation)
            else:
                created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                activation = store.activate(
                    resolved,
                    activation_id=activation_id,
                    created_at=created_at,
                    expected_predecessor_profile_revision=str(
                        active.resolved.plan["profile_revision"]
                    ),
                    expected_predecessor_plan_digest=str(active.resolved.plan["plan_digest"]),
                    expected_predecessor_activation_id=str(active.activation["activation_id"]),
                )
        else:
            if pointer_is_candidate:
                raise PackControlConflict(
                    "Host active pointer references a missing Profile activation"
                )
            created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            activation = store.activate(
                resolved,
                activation_id=activation_id,
                created_at=created_at,
            )
    envelope_path = (
        workspace
        / "activation"
        / "activations"
        / f"{activation_id.removeprefix('activation:')}.json"
    )
    try:
        envelope = json.loads(envelope_path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise PackControlConflict("activated Profile envelope is unavailable") from error
    if not pointer_is_candidate:
        try:
            pointers.commit_activation(
                activation,
                activation_snapshot=envelope,
                activation_snapshot_path=envelope_path.relative_to(user_data).as_posix(),
                expected=predecessor,
                catalog_revision=str(resolved.plan["catalog_revision"]),
            )
        except ActiveProfileStoreConflict:
            # A concurrent retry may have published this exact candidate
            # between the predecessor read and the CAS.  The workspace
            # activation above already verified the same immutable records, so
            # an exact pointer match is a successful idempotent replay.
            current = pointers.require(verify_snapshot=True)
            if not (
                current.profile_id == profile_id
                and hmac.compare_digest(current.profile_revision, candidate_profile_revision)
                and hmac.compare_digest(current.plan_digest, candidate_plan_digest)
                and hmac.compare_digest(current.lock_digest, candidate_lock_digest)
                and hmac.compare_digest(current.activation_id, activation_id)
            ):
                raise
    from .bootstrap.profile_capture import cache_active_profile

    cache_active_profile(
        require_profile_runtime().active_profile(resolved, activation),
        user_data=user_data,
    )
    return activation


def _activate_pack_set(state: Mapping[str, Any], pack_ids: list[str]) -> None:
    """Compatibility wrapper for the existing Pack lifecycle transaction."""

    profile = state.get("resolved_profile")
    plan = state.get("resolved_plan")
    if not isinstance(profile, Mapping) or not isinstance(plan, Mapping):
        raise PackControlConflict("active v4 Profile binding is unavailable")
    profile_id = str(profile.get("profile_id") or "")
    resolved = resolve_profile_pack_set(pack_ids)
    activate_resolved_profile_pack_set(
        resolved,
        activation_id=(
            f"activation:{profile_id}-"
            + resolved.plan["plan_digest"].removeprefix("sha256:")[:16]
            + "-"
            + secrets.token_hex(8)
        ),
        expected_profile_revision=str(plan.get("profile_revision") or ""),
        expected_plan_digest=str(plan.get("plan_digest") or ""),
        expected_activation_id=str(state.get("activation", {}).get("activation_id") or ""),
    )


def _control_state_path(profile_id: str) -> Path:
    _safe_identity(profile_id, "Profile ID")
    return _user_data_root() / "pack_control" / f"{profile_id}.v4.json"


def _persistence_store() -> SecureDirectory:
    """Return the process-pinned Pack control persistence boundary."""

    root = (_user_data_root() / "pack_control").absolute()
    with _PERSISTENCE_STORES_LOCK:
        store = _PERSISTENCE_STORES.get(root)
        if store is None:
            try:
                store = SecureDirectory(root, create=True)
            except (OSError, SecurePersistenceError) as error:
                raise PackControlUnavailable("Pack control persistence is unavailable") from error
            _PERSISTENCE_STORES[root] = store
        return store


def _approval_store(profile_id: str) -> SecureDirectory:
    """Return the process-pinned approval root for one canonical Profile."""

    _safe_identity(profile_id, "Profile ID")
    root = (_user_data_root() / "pack_control" / "approvals" / profile_id).absolute()
    with _PERSISTENCE_STORES_LOCK:
        store = _PERSISTENCE_STORES.get(root)
        if store is None:
            try:
                store = SecureDirectory(root, create=True)
            except (OSError, SecurePersistenceError) as error:
                raise PackControlUnavailable("Pack approval persistence is unavailable") from error
            _PERSISTENCE_STORES[root] = store
        return store


def _control_state_relative(profile_id: str) -> Path:
    _safe_identity(profile_id, "Profile ID")
    return Path(f"{profile_id}.v4.json")


def _panel_session_root(authority_session_id: str) -> str:
    """Recover the authenticated panel binding from a Host-derived session ID."""

    parts = authority_session_id.split(".")
    if (
        len(parts) != 3
        or re.fullmatch(r"[0-9a-f]{64}", parts[0]) is None
        or re.fullmatch(r"[0-9a-f]{24}", parts[1]) is None
        or re.fullmatch(r"[1-9][0-9]*", parts[2]) is None
    ):
        raise PackControlUnapproved("authenticated panel session binding is invalid")
    return parts[0]


def _read_control_state(profile_id: str) -> dict[str, Any]:
    value = _read_control_envelope(profile_id)
    if not value:
        return {}
    value = _migrate_control_envelope(profile_id, value)
    installed = dict(value["installed"])
    if any(pack_id not in load_pack_catalog() for pack_id in installed):
        raise PackControlDigestMismatch("Pack control state contains an unknown Pack")
    return installed


def _read_control_envelope(profile_id: str) -> dict[str, Any]:
    """Read the complete Profile-scoped control envelope without projecting it."""

    try:
        store = _persistence_store()
        relative = _control_state_relative(profile_id)
        if not store.exists(relative):
            return {}
        value = json.loads(store.read_bytes(relative))
    except (OSError, SecurePersistenceError, json.JSONDecodeError) as error:
        raise PackControlUnavailable("Pack control state is unreadable") from error
    if not isinstance(value, Mapping) or not isinstance(value.get("installed"), Mapping):
        raise PackControlDigestMismatch("Pack control state is invalid")
    return dict(value)


def _migrate_control_envelope(profile_id: str, envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Commit the legacy Pack-state migration as one atomic replacement."""

    from .profile_definition_store_v4 import ProfileDefinitionStore
    from .profile_projection_migration import (
        RETIREMENTS,
        migrate_pack_control_envelope,
    )

    profile = ProfileDefinitionStore(_user_data_root()).get_profile(profile_id)
    if profile is None:
        return dict(envelope)
    projections = profile.profile.get("content_projections") or []
    enabled = {
        str(item.get("source_legacy_pack_id") or "")
        for item in projections
        if isinstance(item, Mapping)
    }
    approval_digests: dict[str, str] = {}
    for retirement in RETIREMENTS:
        legacy_id = retirement.legacy_pack_id
        try:
            store = _approval_store(profile_id)
            relative = _approval_relative(profile_id, legacy_id)
            if store.exists(relative):
                approval_digests[legacy_id] = (
                    "sha256:" + hashlib.sha256(store.read_bytes(relative)).hexdigest()
                )
        except (OSError, SecurePersistenceError):
            continue
    migrated, receipt = migrate_pack_control_envelope(
        envelope,
        profile_id=profile_id,
        profile_revision=profile.profile_revision,
        enabled_pack_ids=enabled,
        approval_digests=approval_digests,
    )
    if receipt is not None and migrated != envelope:
        _atomic_json(_control_state_relative(profile_id), migrated)
    return dict(migrated)


def _write_control_state(profile_id: str, installed: Mapping[str, Any]) -> None:
    envelope = _read_control_envelope(profile_id)
    _atomic_json(
        _control_state_relative(profile_id),
        {
            **envelope,
            "version": "io.tobkiri.pack-control-state.v4",
            "profile_id": profile_id,
            "installed": dict(installed),
        },
    )


def _atomic_json(
    path: Path,
    value: Mapping[str, Any],
    *,
    store: SecureDirectory | None = None,
) -> None:
    data = (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    try:
        (store or _persistence_store()).write_bytes_atomic(path, data)
    except (OSError, SecurePersistenceError) as error:
        raise PackControlUnavailable("Pack control persistence is unavailable") from error


def _pack(arguments: Mapping[str, Any]) -> tuple[str, Mapping[str, Any], Path]:
    pack_id = _required(arguments.get("pack_id"), "Pack ID")
    record = load_pack_catalog().get(pack_id)
    if record is None:
        raise PackControlInvalidRequest("Pack is absent from the canonical v4 catalog")
    root = resolve_pack_root(pack_id)
    return pack_id, record, root


def _installed_pack(arguments: Mapping[str, Any], binding: _Binding) -> str:
    pack_id, record, _root = _pack(arguments)
    active = set(_active_profile()[1].get("packs") or [])
    installed = _read_control_state(binding.profile_id)
    if pack_id not in active and pack_id not in installed:
        raise PackControlConflict("Pack must be installed before this operation")
    entry = installed.get(pack_id)
    if entry is not None:
        _require_install_binding(pack_id, record, entry, binding)
    return pack_id


def _require_install_binding(
    pack_id: str,
    record: Mapping[str, Any],
    entry: object,
    binding: _Binding,
) -> None:
    del binding
    try:
        root = resolve_pack_root(pack_id)
        content_digest = _pack_snapshot(pack_id, root)
        pack_artifact_digest = _pack_manifest_artifact_digest(pack_id)
    except PackControlDenied:
        raise
    except Exception as error:
        raise PackControlDigestMismatch(
            f"installed Pack binding is stale or tampered: {pack_id}"
        ) from error
    if (
        not isinstance(entry, Mapping)
        or entry.get("artifact_digest") != _record_digest(record)
        or entry.get("pack_artifact_digest") != pack_artifact_digest
        or entry.get("content_digest") != content_digest
    ):
        raise PackControlDigestMismatch(f"installed Pack binding is stale or tampered: {pack_id}")


def _approval_path(profile_id: str, pack_id: str) -> Path:
    _safe_identity(profile_id, "Profile ID")
    _safe_identity(pack_id, "Pack ID")
    return _user_data_root() / "pack_control" / "approvals" / profile_id / f"{pack_id}.json"


def _approval_relative(profile_id: str, pack_id: str) -> Path:
    _safe_identity(profile_id, "Profile ID")
    _safe_identity(pack_id, "Pack ID")
    return Path(f"{pack_id}.json")


def _authority_key() -> bytes:
    from .hmac_key_manager import generate_or_load_signing_key

    return generate_or_load_signing_key(_user_data_root() / "pack_control" / ".authority_key")


def _user_data_root() -> Path:
    """Return the same Host-owned state root as the captured activation."""

    from .bootstrap.profile_capture import runtime_user_data_root

    return runtime_user_data_root()


def _persist_approval(
    pack_id: str,
    content_digest: str,
    binding: _Binding,
    *,
    approval_nonce: str,
) -> None:
    record = load_pack_catalog()[pack_id]
    payload = {
        "version": "io.tobkiri.pack-approval.v4",
        "pack_id": pack_id,
        "owner": str(record.get("pack_id") or ""),
        "profile_id": binding.profile_id,
        "workspace_id": binding.workspace_id,
        "catalog_revision": binding.catalog_revision,
        "artifact_digest": _record_digest(record),
        "content_digest": content_digest,
        "captured_profile_revision": binding.profile_revision,
        "approval_nonce": approval_nonce,
        "approved_at": int(time.time()),
    }
    payload["approval_revision"] = "sha256:" + _digest(
        {
            key: payload[key]
            for key in (
                "pack_id",
                "owner",
                "profile_id",
                "workspace_id",
                "catalog_revision",
                "artifact_digest",
                "content_digest",
                "approval_nonce",
            )
        }
    )
    payload["signature"] = hmac.new(
        _authority_key(),
        _canonical_bytes(payload),
        hashlib.sha256,
    ).hexdigest()
    _atomic_json(
        _approval_relative(binding.profile_id, pack_id),
        payload,
        store=_approval_store(binding.profile_id),
    )


def _persist_revoked_approval(
    pack_id: str,
    approval: Mapping[str, Any],
    *,
    revocation_id: str,
) -> None:
    payload = {key: value for key, value in approval.items() if key != "signature"}
    payload.update(
        {
            "revoked": True,
            "revoked_at": int(time.time()),
            "revocation_id": revocation_id,
        }
    )
    payload["signature"] = hmac.new(
        _authority_key(),
        _canonical_bytes(payload),
        hashlib.sha256,
    ).hexdigest()
    profile_id = str(payload["profile_id"])
    _atomic_json(
        _approval_relative(profile_id, pack_id),
        payload,
        store=_approval_store(profile_id),
    )


def _approval_status(
    pack_id: str,
    record: Mapping[str, Any],
    binding: _Binding,
) -> tuple[bool, str | None]:
    try:
        payload = json.loads(
            _approval_store(binding.profile_id).read_bytes(
                _approval_relative(binding.profile_id, pack_id)
            )
        )
    except FileNotFoundError:
        return False, "approval_required"
    except (OSError, SecurePersistenceError, json.JSONDecodeError):
        return False, "approval_unreadable"
    if not isinstance(payload, dict):
        return False, "approval_invalid"
    if payload.get("revoked") is True:
        return False, "approval_revoked"
    signature = str(payload.pop("signature", ""))
    expected_signature = hmac.new(
        _authority_key(),
        _canonical_bytes(payload),
        hashlib.sha256,
    ).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected_signature):
        return False, "approval_signature_invalid"
    expected = {
        "version": "io.tobkiri.pack-approval.v4",
        "pack_id": pack_id,
        "owner": pack_id,
        "profile_id": binding.profile_id,
        "workspace_id": binding.workspace_id,
        "catalog_revision": binding.catalog_revision,
        "artifact_digest": _record_digest(record),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        return False, "approval_binding_invalid"
    revision = _approval_revision(payload)
    if payload.get("approval_revision") != revision:
        return False, "approval_revision_invalid"
    from .authority.v4 import AuthorityStore

    try:
        with AuthorityStore(_user_data_root() / "authority" / "v4.sqlite3") as authority:
            if authority.is_revoked("approval", revision):
                return False, "approval_revoked"
    except Exception:
        return False, "approval_authority_unavailable"
    try:
        current = _pack_snapshot(pack_id, resolve_pack_root(pack_id))
    except PackControlDenied:
        return False, "pack_integrity_invalid"
    if not hmac.compare_digest(str(payload.get("content_digest") or ""), current):
        return False, "hash_mismatch"
    return True, None


def _raise_approval_failure(reason: str | None) -> None:
    """Raise the typed Pack control failure represented by approval status."""

    normalized = reason or "approval_required"
    if normalized == "approval_revoked":
        raise PackControlConflict(normalized)
    if normalized in {"approval_unreadable", "approval_authority_unavailable"}:
        raise PackControlUnavailable(normalized)
    if normalized in {
        "approval_invalid",
        "approval_signature_invalid",
        "approval_binding_invalid",
        "approval_revision_invalid",
        "hash_mismatch",
        "pack_integrity_invalid",
    }:
        raise PackControlDigestMismatch(normalized)
    raise PackControlUnapproved(normalized)


def _load_valid_approval(
    pack_id: str,
    record: Mapping[str, Any],
    binding: _Binding,
) -> dict[str, Any]:
    relative = _approval_relative(binding.profile_id, pack_id)
    approved, reason = _approval_status(pack_id, record, binding)
    if not approved:
        _raise_approval_failure(reason)
    try:
        raw = _approval_store(binding.profile_id).read_bytes(relative)
        if not hmac.compare_digest(
            raw,
            _approval_store(binding.profile_id).read_bytes(relative),
        ):
            raise PackControlConflict("Pack approval changed during revocation")
        payload = json.loads(raw)
    except (OSError, SecurePersistenceError) as error:
        raise PackControlUnavailable("Pack approval is unreadable") from error
    except json.JSONDecodeError as error:
        raise PackControlDigestMismatch("Pack approval is invalid") from error
    if not isinstance(payload, dict):
        raise PackControlDigestMismatch("Pack approval is invalid")
    return payload


def _approval_revision(payload: Mapping[str, Any]) -> str:
    keys = [
        "pack_id",
        "owner",
        "profile_id",
        "workspace_id",
        "catalog_revision",
        "artifact_digest",
        "content_digest",
    ]
    if payload.get("approval_nonce") is not None:
        keys.append("approval_nonce")
    return "sha256:" + _digest({key: payload.get(key) for key in keys})


def _pack_manifest_artifact_digest(pack_id: str) -> str:
    manifest_path = resolve_pack_root(pack_id) / "pack.v4.json"
    try:
        manifest = validate_document(manifest_path.read_bytes(), "pack")
    except Exception as error:
        raise PackControlDigestMismatch("Pack v4 manifest is invalid") from error
    if manifest["pack"]["id"] != pack_id:
        raise PackControlDigestMismatch("Pack v4 manifest identity is inconsistent")
    return str(manifest["pack"]["artifact_digest"])


def _pack_snapshot(pack_id: str, root: Path) -> str:
    external_digest = external_pack_content_digest(pack_id)
    if external_digest is not None:
        return external_digest
    if root.is_symlink() or not root.is_dir():
        raise PackControlDigestMismatch("cataloged Pack root is missing or symlinked")
    resolved_root = root.resolve(strict=True)
    files: dict[str, str] = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as scan:
            entries = sorted(scan, key=lambda entry: entry.name)
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink():
                raise PackControlDigestMismatch("cataloged Pack contains a symlink")
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(resolved_root):
                raise PackControlDigestMismatch("cataloged Pack path escapes its boundary")
            relative = path.relative_to(root).as_posix()
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not files:
        raise PackControlDigestMismatch("cataloged Pack has no verifiable artifacts")
    return "sha256:" + _digest({"pack_id": pack_id, "files": files})


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _required(value: object, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise PackControlInvalidRequest(f"{label} is required")
    return normalized


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip() if isinstance(value, str) else ""
    if not normalized:
        raise PackControlInvalidRequest("optional binding must be a non-empty string")
    return normalized


def _require_empty(value: Mapping[str, Any]) -> dict[str, Any]:
    if value:
        raise PackControlInvalidRequest("control presentation payload has unknown fields")
    return {}


def _safe_identity(value: object, label: str) -> str:
    normalized = _required(value, label)
    allowed = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
    if (
        len(normalized) > 128
        or normalized in {".", ".."}
        or any(character not in allowed for character in normalized)
    ):
        raise PackControlInvalidRequest(f"{label} is invalid")
    return normalized


def _record_digest(record: Mapping[str, Any]) -> str:
    return "sha256:" + _digest(record)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "CapturedPackCatalogReader",
    "CapturedPackControlSession",
    "PACK_CONTROL_CONTRACT",
    "PACK_CONTROL_OPERATIONS",
    "CONTROL_PRESENTATION_CONTRACT",
    "CONTROL_PRESENTATION_OPERATIONS",
    "PackControlConflict",
    "PackControlDenied",
    "PackControlDigestMismatch",
    "PackControlInvalidRequest",
    "PackControlStaleRevision",
    "PackControlTimedOut",
    "PackControlUnavailable",
    "PackControlUnapproved",
    "capture_pack_catalog_reader",
    "capture_pack_control_session",
    "capture_pack_control_catalog",
    "capture_valid_pack_approval",
]
