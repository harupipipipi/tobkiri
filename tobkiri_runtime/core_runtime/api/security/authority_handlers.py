"""Authority service HTTP handler mixin."""

from __future__ import annotations

from .._helpers import _SAFE_ERROR_MSG, _log_internal_error


def _authority_service():
    from ...authority import get_authority_service

    return get_authority_service()


class AuthorityHandlersMixin:
    """Handlers for /api/authority/*."""

    def _authority_requests(self, status: str = "all") -> dict:
        try:
            self._sync_defaultspack_pack_authority_requests()
            return _authority_service().list_requests(
                status,
                actor_principal=getattr(self, "_authenticated_principal", None),
            )
        except Exception as exc:
            _log_internal_error("authority_requests", exc)
            return {"error": _SAFE_ERROR_MSG}

    def _authority_request(self, request_id: str) -> dict:
        try:
            return _authority_service().get_request(
                request_id,
                actor_principal=getattr(self, "_authenticated_principal", None),
            )
        except Exception as exc:
            _log_internal_error("authority_request", exc)
            return {"success": False, "error": _SAFE_ERROR_MSG}

    def _authority_grants(self, principal_id: str = "") -> dict:
        try:
            return _authority_service().list_grants(
                principal_id,
                actor_principal=getattr(self, "_authenticated_principal", None),
            )
        except Exception as exc:
            _log_internal_error("authority_grants", exc)
            return {"error": _SAFE_ERROR_MSG}

    def _authority_check(self, body: dict) -> dict:
        try:
            resource = body.get("resource") if isinstance(body.get("resource"), dict) else {}
            actor_principal = getattr(self, "_authenticated_principal", None)
            if actor_principal is not None and not bool(getattr(actor_principal, "core_role", False)):
                principal_id = getattr(actor_principal, "principal_id", "")
                profile_id = getattr(actor_principal, "profile_id", "")
                node_id = None
                graph_id = None
            else:
                principal_id = str(body.get("principal_id") or "")
                profile_id = body.get("profile_id")
                node_id = body.get("node_id")
                graph_id = body.get("graph_id")
            decision = _authority_service().check(
                principal_id=str(principal_id or ""),
                permission_id=str(body.get("permission_id") or ""),
                resource=resource,
                reason=str(body.get("reason") or ""),
                conversation_id=body.get("conversation_id"),
                profile_id=profile_id,
                node_id=node_id,
                graph_id=graph_id,
                request_id=body.get("request_id"),
                approval_token=body.get("approval_token"),
                consume_approval_token=bool(body.get("consume_approval_token") is True),
            )
            return decision.to_dict()
        except Exception as exc:
            _log_internal_error("authority_check", exc)
            return {"error": _SAFE_ERROR_MSG}

    def _authority_approve(self, request_id: str, body: dict) -> dict:
        try:
            config = body.get("config") if isinstance(body.get("config"), dict) else None
            related_permissions = body.get("related_permissions")
            result = _authority_service().approve_request(
                request_id,
                scope=str(body.get("scope") or "once"),
                config=config,
                expires_in_seconds=body.get("expires_in_seconds"),
                related_permissions=related_permissions if isinstance(related_permissions, (list, tuple)) else None,
                ui_operator=body.get("ui_operator") if isinstance(body.get("ui_operator"), dict) else None,
                actor_principal=getattr(self, "_authenticated_principal", None),
                attestation=body.get("attestation") if isinstance(body.get("attestation"), dict) else None,
            )
            self._apply_defaultspack_pack_authority_decision(request_id, result, decision="approve", body=body)
            return result
        except Exception as exc:
            _log_internal_error("authority_approve", exc)
            return {"success": False, "error": _SAFE_ERROR_MSG}

    def _authority_challenge(self, request_id: str, body: dict) -> dict:
        try:
            return _authority_service().create_approval_challenge(
                request_id,
                decision=str(body.get("decision") or "approve"),
                scope=str(body.get("scope") or "once"),
                expires_in_seconds=body.get("expires_in_seconds"),
                actor_principal=getattr(self, "_authenticated_principal", None),
            )
        except Exception as exc:
            _log_internal_error("authority_challenge", exc)
            return {"success": False, "error": _SAFE_ERROR_MSG}

    def _authority_deny(self, request_id: str, body: dict) -> dict:
        try:
            result = _authority_service().deny_request(
                request_id,
                reason=str(body.get("reason") or ""),
                persist=bool(body.get("persist") or body.get("remember")),
                ui_operator=body.get("ui_operator") if isinstance(body.get("ui_operator"), dict) else None,
                actor_principal=getattr(self, "_authenticated_principal", None),
                attestation=body.get("attestation") if isinstance(body.get("attestation"), dict) else None,
            )
            self._apply_defaultspack_pack_authority_decision(request_id, result, decision="deny", body=body)
            return result
        except Exception as exc:
            _log_internal_error("authority_deny", exc)
            return {"success": False, "error": _SAFE_ERROR_MSG}

    def _authority_delete_grant(self, principal_id: str, permission_id: str) -> dict:
        try:
            return _authority_service().delete_grant(
                principal_id,
                permission_id,
                actor_principal=getattr(self, "_authenticated_principal", None),
            )
        except Exception as exc:
            _log_internal_error("authority_delete_grant", exc)
            return {"success": False, "error": _SAFE_ERROR_MSG}

    def _authority_events(self, limit: int = 200) -> dict:
        try:
            return _authority_service().events(
                limit,
                actor_principal=getattr(self, "_authenticated_principal", None),
            )
        except Exception as exc:
            _log_internal_error("authority_events", exc)
            return {"error": _SAFE_ERROR_MSG}

    @staticmethod
    def _sync_defaultspack_pack_authority_requests() -> None:
        try:
            from ecosystem.defaultspack.backend.pack_extension.authority_bridge import (
                sync_pending_pack_requests_to_authority,
            )

            sync_pending_pack_requests_to_authority()
        except Exception:
            return

    def _apply_defaultspack_pack_authority_decision(
        self,
        request_id: str,
        result: dict,
        *,
        decision: str,
        body: dict,
    ) -> None:
        if not isinstance(result, dict) or not result.get("success"):
            return
        try:
            from ecosystem.defaultspack.backend.pack_extension.authority_bridge import (
                apply_pack_decision_for_authority_request,
            )

            bridge = apply_pack_decision_for_authority_request(
                request_id,
                decision=decision,
                reviewer=self._authority_reviewer_label(),
                notes=str(body.get("decision_notes") or body.get("notes") or body.get("reason") or ""),
            )
            if not bridge.get("skipped"):
                result["pack_request_result"] = bridge
        except Exception as exc:
            result["pack_request_result"] = {"success": False, "error": str(exc)}

    def _authority_reviewer_label(self) -> str:
        principal = getattr(self, "_authenticated_principal", None)
        if isinstance(principal, dict):
            role = str(principal.get("role") or "").strip()
            device_id = str(principal.get("device_id") or "").strip()
            if role or device_id:
                return ":".join(value for value in (role, device_id) if value)
        return "authority"
