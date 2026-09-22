"""Authority facade over existing Rumi grant managers."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config_lattice import (
    AuthorityConfigError,
    AUTHORITY_RESOURCE_CONFIG_FIELDS,
    authority_constraints_from_config,
    authority_config_from_resource,
    meet_authority_configs,
    validate_authority_config,
)
from .approval_attestation import verify_mobile_approval_attestation
from .approval_challenge_store import (
    ApprovalChallengeStore,
    DEFAULT_MOBILE_APPROVAL_TOKEN_TTL_SECONDS,
)
from .device_key_registry import DeviceKeyRegistry
from .debug_cli_operator import verify_authority_debug_operator
from .models import AUTHORITY_PERMISSION_IDS, AuthorityDecision, AuthorityRequest
from .principal import build_principal_id, parse_principal_parts, principal_scope_candidates
from .request_store import AuthorityRequestStore, sanitize_authority_resource
from .ui_operator import ui_operator_audit_record, verify_ui_operator
from ..host_permissions import get_host_permission_definition


AUTHORITY_APPROVAL_SCOPES = frozenset({"once", "conversation", "profile", "node"})

RESOURCE_CONFIG_FIELDS = AUTHORITY_RESOURCE_CONFIG_FIELDS


class AuthorityService:
    def __init__(
        self,
        *,
        capability_grant_manager: Any = None,
        secrets_grant_manager: Any = None,
        network_grant_manager: Any = None,
        host_privilege_manager: Any = None,
        hmac_key_manager: Any = None,
        request_store: AuthorityRequestStore | None = None,
        approval_challenge_store: ApprovalChallengeStore | None = None,
        device_key_registry: DeviceKeyRegistry | None = None,
    ) -> None:
        self._capability_grant_manager = capability_grant_manager
        self._secrets_grant_manager = secrets_grant_manager
        self._network_grant_manager = network_grant_manager
        self._host_privilege_manager = host_privilege_manager
        self._request_store = request_store or AuthorityRequestStore(hmac_key_manager=hmac_key_manager)
        self._approval_challenge_store = approval_challenge_store or ApprovalChallengeStore(
            hmac_key_manager=hmac_key_manager
        )
        self._device_key_registry = device_key_registry or DeviceKeyRegistry()

    def register_device_key(
        self,
        *,
        profile_id: str,
        device_id: str,
        public_key: str | bytes,
    ) -> dict[str, Any]:
        record = self._device_key_registry.register_device_key(
            profile_id=profile_id,
            device_id=device_id,
            public_key=public_key,
        )
        self._request_store.audit(
            "authority_device_key_registered",
            {
                "profile_id": record.profile_id,
                "device_id": record.device_id,
                "key_id": record.key_id,
            },
        )
        return {"success": True, "device_key": record.to_dict()}

    def create_approval_challenge(
        self,
        request_id: str,
        *,
        decision: str = "approve",
        scope: str = "once",
        actor_principal: Any = None,
        expires_in_seconds: int | None = None,
    ) -> dict[str, Any]:
        request = self._request_store.get_request(request_id)
        if request is None:
            return {"success": False, "error": "Authority request not found", "status_code": 404}
        if not self._actor_mobile_approver(actor_principal):
            return {"success": False, "error": "Mobile approver role required", "status_code": 403}
        if not self._actor_can_access_request(request, actor_principal=actor_principal):
            return {"success": False, "error": "Authority request not found", "status_code": 404}
        if request.status != "pending":
            return {"success": False, "error": f"Authority request is {request.status}", "status_code": 409}
        if self._request_store.request_expired(request):
            self._request_store.set_request_status(request.request_id, "expired")
            return {"success": False, "error": "Authority request expired", "status_code": 409}

        decision = str(decision or "").strip().lower()
        if decision not in {"approve", "deny"}:
            return {"success": False, "error": "Approval challenge decision is invalid", "status_code": 400}
        scope = str(scope or "once").strip().lower()
        if decision == "approve" and scope != "once":
            return {
                "success": False,
                "error": "Mobile approver tokens may only issue one-shot approvals",
                "status_code": 403,
            }
        if decision == "deny" and scope not in {"once", ""}:
            return {
                "success": False,
                "error": "Mobile deny challenges cannot be persistent",
                "status_code": 403,
            }
        permission_id = "authority.request.approve" if decision == "approve" else "authority.request.deny"
        if not self._mobile_actor_has_route_grant(actor_principal, permission_id):
            return {"success": False, "error": "Mobile approver grant is not valid", "status_code": 403}

        profile_id = self._actor_profile_id(actor_principal)
        device_id = self._actor_device_id(actor_principal)
        token_id = self._actor_token_id(actor_principal)
        if not profile_id or not device_id or not token_id:
            return {"success": False, "error": "Mobile approver token is incomplete", "status_code": 403}
        if self._device_key_registry.get_device_key(profile_id=profile_id, device_id=device_id) is None:
            return {"success": False, "error": "Mobile device key is not registered", "status_code": 403}

        challenge = self._approval_challenge_store.issue_challenge(
            request=request,
            profile_id=profile_id,
            device_id=device_id,
            token_id=token_id,
            resource_hash=self._request_store.resource_hash(request.resource),
            decision=decision,
            scope="once",
            expires_in_seconds=expires_in_seconds,
            approval_expires_in_seconds=DEFAULT_MOBILE_APPROVAL_TOKEN_TTL_SECONDS,
        )
        self._request_store.audit(
            "authority_approval_challenge_issued",
            {
                "request_id": request.request_id,
                "challenge_id": challenge.challenge_id,
                "decision": decision,
                "profile_id": profile_id,
                "device_id": device_id,
                "token_id": token_id,
                "resource_hash": challenge.resource_hash,
            },
        )
        return {
            "success": True,
            "request_id": request.request_id,
            "challenge": challenge.payload_for_signature(),
            "payload_hash": challenge.payload_hash,
            "signature_algorithm": "ed25519",
            "signing_payload": "payload_hash_bytes",
        }

    @property
    def mode(self) -> str:
        from ..host_contract import host_contract_value

        value = host_contract_value("authority_mode").strip().lower() or "enforce"
        return value if value in {"off", "observe", "enforce"} else "enforce"

    def check(
        self,
        *,
        principal_id: str,
        permission_id: str,
        resource: dict[str, Any],
        reason: str = "",
        conversation_id: str | None = None,
        profile_id: str | None = None,
        node_id: str | None = None,
        graph_id: str | None = None,
        request_id: str | None = None,
        approval_token: str | None = None,
        consume_approval_token: bool = True,
    ) -> AuthorityDecision:
        return self._check(
            principal_id=principal_id,
            permission_id=permission_id,
            resource=resource,
            reason=reason,
            conversation_id=conversation_id,
            profile_id=profile_id,
            node_id=node_id,
            graph_id=graph_id,
            request_id=request_id,
            approval_token=approval_token,
            consume_approval_token=consume_approval_token,
        )

    def preflight_check(
        self,
        *,
        principal_id: str,
        permission_id: str,
        resource: dict[str, Any],
        reason: str = "",
        conversation_id: str | None = None,
        profile_id: str | None = None,
        node_id: str | None = None,
        graph_id: str | None = None,
        request_id: str | None = None,
        approval_token: str | None = None,
    ) -> AuthorityDecision:
        return self.check(
            principal_id=principal_id,
            permission_id=permission_id,
            resource=resource,
            reason=reason,
            conversation_id=conversation_id,
            profile_id=profile_id,
            node_id=node_id,
            graph_id=graph_id,
            request_id=request_id,
            approval_token=approval_token,
            consume_approval_token=False,
        )

    def consume_one_shot_approvals_atomically(
        self,
        items: list[dict[str, Any]],
    ) -> AuthorityDecision:
        normalized: list[dict[str, Any]] = []
        for item in items or []:
            permission_id = str(item.get("permission_id") or "").strip()
            principal_id = str(item.get("principal_id") or "").strip()
            raw_resource = item.get("resource")
            resource = self._normalize_resource(
                raw_resource if isinstance(raw_resource, dict) else {}
            )
            risk_level = self._risk_level(permission_id, resource)
            if permission_id not in AUTHORITY_PERMISSION_IDS:
                return self._decision(
                    False,
                    permission_id,
                    principal_id,
                    resource,
                    "Unknown authority permission",
                    risk_level,
                    request_id=str(item.get("request_id") or "") or None,
                    approval_required=True,
                )
            normalized.append(
                {
                    "request_id": str(item.get("request_id") or "").strip(),
                    "principal_id": principal_id,
                    "permission_id": permission_id,
                    "resource": resource,
                    "token": str(item.get("approval_token") or item.get("token") or "").strip(),
                }
            )

        if not normalized:
            return self._decision(True, "", "", {}, "No one-shot approvals to consume", "low")

        result = self._request_store.consume_one_shots_atomically(normalized)
        first = normalized[0]
        if result.get("success"):
            return self._decision(
                True,
                first["permission_id"],
                first["principal_id"],
                first["resource"],
                "One-shot approvals consumed",
                self._risk_level(first["permission_id"], first["resource"]),
            )

        failed_index = int(result.get("failed_index") or 0)
        failed_index = max(0, min(failed_index, len(normalized) - 1))
        failed = normalized[failed_index]
        reason = str(result.get("reason") or "one_shot_consume_failed")
        return self._decision(
            False,
            failed["permission_id"],
            failed["principal_id"],
            failed["resource"],
            f"One-shot approval could not be consumed: {reason}",
            self._risk_level(failed["permission_id"], failed["resource"]),
            request_id=failed["request_id"] or None,
            approval_required=True,
        )

    def _check(
        self,
        *,
        principal_id: str,
        permission_id: str,
        resource: dict[str, Any],
        reason: str = "",
        conversation_id: str | None = None,
        profile_id: str | None = None,
        node_id: str | None = None,
        graph_id: str | None = None,
        request_id: str | None = None,
        approval_token: str | None = None,
        consume_approval_token: bool = True,
    ) -> AuthorityDecision:
        permission_id = str(permission_id or "").strip()
        principal_id = str(principal_id or "").strip() or build_principal_id(
            profile_id=profile_id,
            graph_id=graph_id,
            node_id=node_id,
            conversation_id=conversation_id,
        )
        resource = self._normalize_resource(resource)
        risk_level = self._risk_level(permission_id, resource)
        reason = str(reason or "").strip() or f"{permission_id} requires approval"

        if permission_id not in AUTHORITY_PERMISSION_IDS:
            return self._decision(False, permission_id, principal_id, resource, "Unknown authority permission", risk_level)

        mode = self.mode
        if mode == "off":
            self._audit_check("allowed_off", principal_id, permission_id, resource)
            return self._decision(True, permission_id, principal_id, resource, "Authority disabled", risk_level)
        if mode == "observe":
            self._audit_check("observed", principal_id, permission_id, resource)
            return self._decision(True, permission_id, principal_id, resource, "Authority observe mode", risk_level)

        candidates = principal_scope_candidates(principal_id, conversation_id=conversation_id)
        deny = self._request_store.matching_deny(candidates, permission_id, resource)
        if deny is not None:
            self._audit_check("denied_explicit", principal_id, permission_id, resource)
            return self._decision(False, permission_id, principal_id, resource, str(deny.get("reason") or "Explicitly denied"), risk_level)

        if request_id and approval_token:
            token_allowed = self._request_store.consume_one_shot(
                request_id=request_id,
                principal_id=principal_id,
                permission_id=permission_id,
                resource=resource,
                token=approval_token,
            ) if consume_approval_token else self._request_store.one_shot_matches_request(
                request_id=request_id,
                principal_id=principal_id,
                permission_id=permission_id,
                resource=resource,
                token=approval_token,
            )
            if token_allowed:
                token_reason = "One-shot approval consumed" if consume_approval_token else "One-shot approval verified"
                return self._decision(True, permission_id, principal_id, resource, token_reason, risk_level)

        grant_match = self._matching_capability_grant(candidates, permission_id, resource)
        if grant_match is not None:
            matched_principal, config = grant_match
            self._audit_check("allowed_grant", matched_principal, permission_id, resource)
            return self._decision(
                True,
                permission_id,
                principal_id,
                resource,
                f"Granted by {matched_principal}",
                risk_level,
                grant_config=config,
            )

        self._audit_check("missing_grant", principal_id, permission_id, resource)
        request = self._request_store.create_request(
            principal_id=principal_id,
            permission_id=permission_id,
            resource=resource,
            reason=reason,
            risk_level=risk_level,
            conversation_id=conversation_id,
            profile_id=profile_id,
            node_id=node_id,
            graph_id=graph_id,
        )
        return self._decision(
            False,
            permission_id,
            principal_id,
            resource,
            reason,
            risk_level,
            request_id=request.request_id,
            approval_required=True,
        )

    @staticmethod
    def _settlement_failure_response(settlement: dict[str, Any]) -> dict[str, Any]:
        request = settlement.get("request")
        if request is None:
            return {"success": False, "error": "Authority request not found", "status_code": 404}
        status = getattr(request, "status", "pending")
        if status == "expired":
            return {"success": False, "error": "Authority request expired", "status_code": 409}
        return {"success": False, "error": f"Authority request is {status}", "status_code": 409}

    def approve_request(
        self,
        request_id: str,
        *,
        scope: str = "once",
        config: dict[str, Any] | None = None,
        expires_in_seconds: int | None = None,
        related_permissions: list[str] | tuple[str, ...] | None = None,
        ui_operator: dict[str, Any] | None = None,
        debug_cli_operator: dict[str, Any] | None = None,
        expected_digest: str = "",
        actor_principal: Any = None,
        attestation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = self._request_store.get_request(request_id)
        if request is None:
            return {"success": False, "error": "Authority request not found", "status_code": 404}
        if not self._actor_can_access_request(request, actor_principal=actor_principal):
            return {"success": False, "error": "Authority request not found", "status_code": 404}
        if request.status != "pending":
            return {"success": False, "error": f"Authority request is {request.status}", "status_code": 409}
        if self._request_store.request_expired(request):
            self._request_store.set_request_status(request.request_id, "expired")
            return {"success": False, "error": "Authority request expired", "status_code": 409}

        config = dict(config or {})
        scope = str(scope or "once").strip().lower()
        if scope not in AUTHORITY_APPROVAL_SCOPES:
            return {"success": False, "error": "Authority approval scope is invalid", "status_code": 400}
        mobile_approver = self._actor_mobile_approver(actor_principal)
        if mobile_approver and scope != "once":
            return {
                "success": False,
                "error": "Mobile approver tokens may only issue one-shot approvals",
                "status_code": 403,
            }
        if mobile_approver and related_permissions:
            return {
                "success": False,
                "error": "Mobile approver approvals may not bundle related permissions",
                "status_code": 403,
            }
        if mobile_approver and expires_in_seconds is not None:
            return {
                "success": False,
                "error": "Mobile approver approval TTL is fixed by the signed challenge",
                "status_code": 403,
            }
        mobile_attestation_audit: dict[str, Any] = {}
        if mobile_approver:
            if not self._mobile_actor_has_route_grant(actor_principal, "authority.request.approve"):
                return {"success": False, "error": "Mobile approver grant is not valid", "status_code": 403}
            attestation_result = verify_mobile_approval_attestation(
                request=request,
                actor_principal=actor_principal,
                decision="approve",
                scope=scope,
                attestation=attestation,
                challenge_store=self._approval_challenge_store,
                device_key_registry=self._device_key_registry,
                request_store=self._request_store,
            )
            if not attestation_result.ok:
                self._request_store.audit(
                    "authority_mobile_attestation_rejected",
                    {
                        "request_id": request.request_id,
                        "reason": attestation_result.error,
                        **attestation_result.audit,
                    },
                )
                return {
                    "success": False,
                    "error": attestation_result.error,
                    "status_code": attestation_result.status_code,
                }
            mobile_attestation_audit = attestation_result.audit
        confirmation_text = str(config.pop("confirmation_text", "") or "").strip()
        if self._typed_confirmation_required(request) and debug_cli_operator is None:
            if scope != "once":
                return {
                    "success": False,
                    "error": "Critical host approval must use once scope",
                    "status_code": 400,
                }
            confirmation_phrase = self._confirmation_phrase(request)
            if not confirmation_phrase or confirmation_text != confirmation_phrase:
                self._request_store.audit(
                    "authority_typed_confirmation_rejected",
                    {
                        "request_id": request.request_id,
                        "permission_id": request.permission_id,
                        "resource_hash": self._request_store.resource_hash(request.resource),
                    },
                )
                return {
                    "success": False,
                    "error": "Typed confirmation is required for this host operation",
                    "status_code": 400,
                }
        if mobile_approver:
            operator_audit = dict(mobile_attestation_audit)
        elif debug_cli_operator is not None:
            if str(debug_cli_operator.get("decision") or "") != "approve":
                return {"success": False, "error": "debug operator decision mismatch", "status_code": 403}
            if scope != "once" or related_permissions or expires_in_seconds is not None:
                return {
                    "success": False,
                    "error": "Delegated debug approval must be one-shot and unbundled",
                    "status_code": 400,
                }
            operator_ok, operator_error, operator_audit = verify_authority_debug_operator(
                request, expected_digest, debug_cli_operator
            )
            if not operator_ok:
                self._request_store.audit(
                    "authority_debug_cli_operator_rejected",
                    {"request_id": request.request_id, "reason": operator_error},
                )
                return {"success": False, "error": operator_error, "status_code": 403}
        else:
            operator_ok, operator_error, operator_payload = verify_ui_operator(ui_operator, request_id=request.request_id)
            if not operator_ok:
                self._request_store.audit(
                    "authority_ui_operator_rejected",
                    {"request_id": request.request_id, "reason": operator_error},
                )
                return {"success": False, "error": operator_error, "status_code": 403}
            operator_audit = ui_operator_audit_record(operator_payload)
        raw_expires = (
            mobile_attestation_audit.get("approval_expires_in_seconds")
            if mobile_approver
            else (expires_in_seconds or 86400)
        )
        if not isinstance(raw_expires, (str, int, float, bytes)):
            return {
                "success": False,
                "error": "Authority approval expiration is invalid",
                "status_code": 403 if mobile_approver else 400,
            }
        expires = int(raw_expires)
        if scope == "once":
            def settle_once(settled_request: AuthorityRequest) -> dict[str, Any]:
                issued_token_ids: list[str] = []
                try:
                    token = self._request_store.issue_one_shot(settled_request, expires_in_seconds=expires)
                    issued_token_ids.append(token["token_id"])
                    related = self._approve_related_once(
                        settled_request,
                        related_permissions=related_permissions,
                        expires_in_seconds=expires,
                        operator_audit=operator_audit,
                        issued_token_ids=issued_token_ids,
                    )
                except Exception:
                    self._request_store.revoke_one_shots(
                        issued_token_ids,
                        reason="approval_settlement_failed",
                    )
                    self._request_store.audit(
                        "authority_request_approval_failed",
                        {
                            "request_id": settled_request.request_id,
                            "scope": "once",
                            "principal_id": settled_request.principal_id,
                            "permission_id": settled_request.permission_id,
                            "error": "one_shot_settlement_failed",
                            **operator_audit,
                        },
                    )
                    raise
                return {"token": token, "related": related, "issued_token_ids": issued_token_ids}

            def rollback_once(_settled_request: AuthorityRequest, result: dict[str, Any] | None) -> None:
                self._rollback_related_request_statuses((result or {}).get("related") or [])
                self._request_store.revoke_one_shots(
                    (result or {}).get("issued_token_ids") or [],
                    reason="approval_settlement_failed",
                )

            try:
                settlement = self._request_store.settle_pending_request(
                    request.request_id,
                    "approved",
                    settle_once,
                    rollback_once,
                )
            except Exception:
                return {
                    "success": False,
                    "error": "One-shot authority approval failed",
                    "status_code": 500,
                    "reason": "one_shot_settlement_failed",
                }
            if not settlement.get("settled"):
                return self._settlement_failure_response(settlement)
            once_request = settlement["request"]
            settled_result = settlement.get("result") or {}
            token = settled_result["token"]
            related = settled_result["related"]
            self._audit_best_effort(
                "authority_request_approved",
                {
                    "request_id": once_request.request_id,
                    "scope": "once",
                    "principal_id": once_request.principal_id,
                    "permission_id": once_request.permission_id,
                    "resource_hash": self._request_store.resource_hash(once_request.resource),
                    **operator_audit,
                },
            )
            return {
                "success": True,
                "request_id": once_request.request_id,
                "approved": True,
                "scope": "once",
                "token": token["token"],
                "expires_at": token["expires_at"],
                "permission_id": once_request.permission_id,
                "related_approvals": related,
            }

        grant_principal = self._principal_for_scope(request, scope)
        if not grant_principal:
            return {"success": False, "error": "Scope cannot be resolved for authority request", "status_code": 400}
        try:
            grant_config = self._grant_config_for_persistent_approval(request.resource, config)
        except AuthorityConfigError as exc:
            return {"success": False, "error": str(exc), "status_code": 400}
        manager = self._capability_grant_manager
        if manager is None or not callable(getattr(manager, "grant_permission", None)):
            return {"success": False, "error": "CapabilityGrantManager unavailable", "status_code": 500}
        grant_snapshot = self._snapshot_capability_grant(manager, grant_principal)

        def settle_persistent(settled_request: AuthorityRequest) -> dict[str, Any]:
            try:
                manager.grant_permission(grant_principal, settled_request.permission_id, grant_config)
                related = self._approve_related_persistent(
                    settled_request,
                    grant_principal=grant_principal,
                    scope=scope,
                    config=config,
                    related_permissions=related_permissions,
                    operator_audit=operator_audit,
                )
            except AuthorityConfigError:
                self._restore_capability_grant(manager, grant_principal, grant_snapshot)
                self._request_store.audit(
                    "authority_request_approval_failed",
                    {
                        "request_id": settled_request.request_id,
                        "scope": scope,
                        "principal_id": grant_principal,
                        "permission_id": settled_request.permission_id,
                        "error": "persistent_grant_config_failed",
                        **operator_audit,
                    },
                )
                raise
            except Exception:
                self._restore_capability_grant(manager, grant_principal, grant_snapshot)
                self._request_store.audit(
                    "authority_request_approval_failed",
                    {
                        "request_id": settled_request.request_id,
                        "scope": scope,
                        "principal_id": grant_principal,
                        "permission_id": settled_request.permission_id,
                        "error": "persistent_grant_failed",
                        **operator_audit,
                    },
                )
                raise
            return {"related": related}

        def rollback_persistent(
            _settled_request: AuthorityRequest,
            result: dict[str, Any] | None,
        ) -> None:
            self._rollback_related_request_statuses((result or {}).get("related") or [])
            self._restore_capability_grant(manager, grant_principal, grant_snapshot)
            self._request_store.audit(
                "authority_request_approval_failed",
                {
                    "request_id": request.request_id,
                    "scope": scope,
                    "principal_id": grant_principal,
                    "permission_id": request.permission_id,
                    "error": "persistent_settlement_failed",
                    **operator_audit,
                },
            )

        try:
            settlement = self._request_store.settle_pending_request(
                request.request_id,
                "approved",
                settle_persistent,
                rollback_persistent,
            )
        except AuthorityConfigError as exc:
            return {"success": False, "error": str(exc), "status_code": 400}
        except Exception:
            return {
                "success": False,
                "error": "Persistent authority grant failed",
                "status_code": 500,
                "reason": "persistent_grant_failed",
            }
        if not settlement.get("settled"):
            return self._settlement_failure_response(settlement)
        persistent_request = settlement["request"]
        related = (settlement.get("result") or {})["related"]
        self._audit_best_effort(
            "authority_request_approved",
            {
                "request_id": persistent_request.request_id,
                "scope": scope,
                "principal_id": grant_principal,
                "permission_id": persistent_request.permission_id,
                "resource_hash": self._request_store.resource_hash(
                    persistent_request.resource
                ),
                **operator_audit,
            },
        )
        return {
            "success": True,
            "request_id": persistent_request.request_id,
            "approved": True,
            "scope": scope,
            "principal_id": grant_principal,
            "permission_id": persistent_request.permission_id,
            "config": grant_config,
            "related_approvals": related,
        }

    @staticmethod
    def _snapshot_capability_grant(manager: Any, principal_id: str) -> dict[str, Any] | None:
        get_grant = getattr(manager, "get_grant", None)
        grant = get_grant(principal_id) if callable(get_grant) else None
        if grant is None:
            return None
        return {
            "principal_id": grant.principal_id,
            "enabled": bool(grant.enabled),
            "granted_at": str(grant.granted_at or ""),
            "updated_at": str(grant.updated_at or ""),
            "permissions": {
                permission_id: {
                    "enabled": bool(permission.enabled),
                    "config": dict(permission.config or {}),
                }
                for permission_id, permission in dict(grant.permissions).items()
            },
        }

    @staticmethod
    def _restore_capability_grant(
        manager: Any,
        principal_id: str,
        snapshot: dict[str, Any] | None,
    ) -> None:
        if snapshot is None:
            delete_grant = getattr(manager, "delete_grant", None)
            if callable(delete_grant):
                delete_grant(principal_id)
            return

        from ..capability_grant_manager import CapabilityGrant, CapabilityPermissionGrant

        def restore() -> None:
            grants = getattr(manager, "_grants", None)
            if not isinstance(grants, dict):
                return
            grant = grants.get(principal_id)
            if grant is None:
                grant = CapabilityGrant(
                    principal_id=str(snapshot.get("principal_id") or principal_id),
                    enabled=bool(snapshot.get("enabled")),
                    granted_at=str(snapshot.get("granted_at") or ""),
                    updated_at=str(snapshot.get("updated_at") or ""),
                )
                grants[principal_id] = grant
            grant.principal_id = str(snapshot.get("principal_id") or principal_id)
            grant.enabled = bool(snapshot.get("enabled"))
            grant.granted_at = str(snapshot.get("granted_at") or grant.granted_at)
            grant.updated_at = str(snapshot.get("updated_at") or grant.updated_at)
            grant.permissions = {
                permission_id: CapabilityPermissionGrant(
                    enabled=bool(permission.get("enabled")),
                    config=dict(permission.get("config") or {}),
                )
                for permission_id, permission in dict(snapshot.get("permissions") or {}).items()
                if isinstance(permission, dict)
            }
            tampered = getattr(manager, "_tampered_principals", None)
            if isinstance(tampered, set):
                tampered.discard(principal_id)
            save_grant = getattr(manager, "_save_grant", None)
            if callable(save_grant):
                save_grant(grant)

        lock = getattr(manager, "_lock", None)
        if lock is None:
            restore()
            return
        with lock:
            restore()

    def _normalized_related_permissions(
        self,
        request: AuthorityRequest,
        related_permissions: list[str] | tuple[str, ...] | None,
    ) -> list[str]:
        bundled_permissions = {"model.invoke", "api_key.use", "network.egress"}
        if request.permission_id not in bundled_permissions:
            return []
        permissions: list[str] = []
        for permission_id in related_permissions or ():
            normalized = str(permission_id or "").strip()
            if normalized == request.permission_id:
                continue
            if normalized not in bundled_permissions:
                continue
            if normalized not in AUTHORITY_PERMISSION_IDS:
                continue
            if normalized not in permissions:
                permissions.append(normalized)
        return permissions

    @staticmethod
    def _resource_for_related_permission(resource: dict[str, Any], permission_id: str) -> dict[str, Any]:
        related = dict(resource or {})
        if permission_id == "model.invoke":
            related["kind"] = "model"
        elif permission_id == "api_key.use":
            related["kind"] = "api_key"
        elif permission_id == "network.egress":
            related["kind"] = "network"
        return related

    def _create_related_request(self, request: AuthorityRequest, permission_id: str) -> AuthorityRequest:
        resource = self._resource_for_related_permission(request.resource, permission_id)
        return self._request_store.create_request(
            principal_id=request.principal_id,
            permission_id=permission_id,
            resource=resource,
            reason=f"Bundled with {request.permission_id} approval",
            risk_level=self._risk_level(permission_id, resource),
            conversation_id=request.conversation_id,
            profile_id=request.profile_id,
            node_id=request.node_id,
            graph_id=request.graph_id,
        )

    def _approve_related_once(
        self,
        request: AuthorityRequest,
        *,
        related_permissions: list[str] | tuple[str, ...] | None,
        expires_in_seconds: int,
        operator_audit: dict[str, Any],
        issued_token_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        approvals: list[dict[str, Any]] = []
        for permission_id in self._normalized_related_permissions(request, related_permissions):
            related_request = self._create_related_request(request, permission_id)
            token = self._request_store.issue_one_shot(related_request, expires_in_seconds=expires_in_seconds)
            if issued_token_ids is not None:
                issued_token_ids.append(token["token_id"])
            self._request_store.set_request_status(related_request.request_id, "approved")
            self._request_store.audit(
                "authority_request_approved",
                {
                    "request_id": related_request.request_id,
                    "scope": "once",
                    "principal_id": related_request.principal_id,
                    "permission_id": permission_id,
                    "resource_hash": self._request_store.resource_hash(related_request.resource),
                    "bundled_with_request_id": request.request_id,
                    **operator_audit,
                },
            )
            approvals.append(
                {
                    "request_id": related_request.request_id,
                    "approved": True,
                    "scope": "once",
                    "token": token["token"],
                    "expires_at": token["expires_at"],
                    "permission_id": permission_id,
                    "resource": related_request.resource,
                }
            )
        return approvals

    def _approve_related_persistent(
        self,
        request: AuthorityRequest,
        *,
        grant_principal: str,
        scope: str,
        config: dict[str, Any] | None,
        related_permissions: list[str] | tuple[str, ...] | None,
        operator_audit: dict[str, Any],
    ) -> list[dict[str, Any]]:
        manager = self._capability_grant_manager
        if manager is None or not callable(getattr(manager, "grant_permission", None)):
            return []
        grant_items: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        for permission_id in self._normalized_related_permissions(request, related_permissions):
            resource = self._resource_for_related_permission(request.resource, permission_id)
            grant_config = self._grant_config_for_persistent_approval(resource, config)
            grant_items.append((permission_id, resource, grant_config))

        for permission_id, _, grant_config in grant_items:
            manager.grant_permission(grant_principal, permission_id, grant_config)

        approvals: list[dict[str, Any]] = []
        for permission_id, _, grant_config in grant_items:
            related_request = self._create_related_request(request, permission_id)
            self._request_store.set_request_status(related_request.request_id, "approved")
            self._request_store.audit(
                "authority_request_approved",
                {
                    "request_id": related_request.request_id,
                    "scope": scope,
                    "principal_id": grant_principal,
                    "permission_id": permission_id,
                    "resource_hash": self._request_store.resource_hash(related_request.resource),
                    "bundled_with_request_id": request.request_id,
                    **operator_audit,
                },
            )
            approvals.append(
                {
                    "request_id": related_request.request_id,
                    "approved": True,
                    "scope": scope,
                    "principal_id": grant_principal,
                    "permission_id": permission_id,
                    "config": grant_config,
                    "resource": related_request.resource,
                }
            )
        return approvals

    def _rollback_related_request_statuses(self, related_approvals: list[dict[str, Any]]) -> None:
        for item in related_approvals or []:
            request_id = str(item.get("request_id") or "").strip()
            if not request_id:
                continue
            request = self._request_store.get_request(request_id)
            if request is not None and request.status == "approved":
                self._request_store.set_request_status(request_id, "pending")

    def deny_request(
        self,
        request_id: str,
        *,
        reason: str = "",
        persist: bool = False,
        ui_operator: dict[str, Any] | None = None,
        debug_cli_operator: dict[str, Any] | None = None,
        expected_digest: str = "",
        actor_principal: Any = None,
        attestation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = self._request_store.get_request(request_id)
        if request is None:
            return {"success": False, "error": "Authority request not found", "status_code": 404}
        if not self._actor_can_access_request(request, actor_principal=actor_principal):
            return {"success": False, "error": "Authority request not found", "status_code": 404}
        if request.status != "pending":
            return {"success": False, "error": f"Authority request is {request.status}", "status_code": 409}
        if self._request_store.request_expired(request):
            self._request_store.set_request_status(request.request_id, "expired")
            return {"success": False, "error": "Authority request expired", "status_code": 409}
        mobile_approver = self._actor_mobile_approver(actor_principal)
        if mobile_approver and persist:
            return {
                "success": False,
                "error": "Mobile approver tokens may not create persistent denies",
                "status_code": 403,
            }
        if mobile_approver:
            if not self._mobile_actor_has_route_grant(actor_principal, "authority.request.deny"):
                return {"success": False, "error": "Mobile approver grant is not valid", "status_code": 403}
            attestation_result = verify_mobile_approval_attestation(
                request=request,
                actor_principal=actor_principal,
                decision="deny",
                scope="once",
                attestation=attestation,
                challenge_store=self._approval_challenge_store,
                device_key_registry=self._device_key_registry,
                request_store=self._request_store,
            )
            if not attestation_result.ok:
                self._request_store.audit(
                    "authority_mobile_attestation_rejected",
                    {
                        "request_id": request.request_id,
                        "reason": attestation_result.error,
                        **attestation_result.audit,
                    },
                )
                return {
                    "success": False,
                    "error": attestation_result.error,
                    "status_code": attestation_result.status_code,
                }
            operator_audit = dict(attestation_result.audit)
        elif debug_cli_operator is not None:
            if str(debug_cli_operator.get("decision") or "") != "deny":
                return {"success": False, "error": "debug operator decision mismatch", "status_code": 403}
            if persist:
                return {
                    "success": False,
                    "error": "Delegated debug denial cannot be persistent",
                    "status_code": 400,
                }
            operator_ok, operator_error, operator_audit = verify_authority_debug_operator(
                request, expected_digest, debug_cli_operator
            )
            if not operator_ok:
                self._request_store.audit(
                    "authority_debug_cli_operator_rejected",
                    {"request_id": request.request_id, "reason": operator_error},
                )
                return {"success": False, "error": operator_error, "status_code": 403}
        else:
            operator_ok, operator_error, operator_payload = verify_ui_operator(ui_operator, request_id=request.request_id)
            if not operator_ok:
                self._request_store.audit(
                    "authority_ui_operator_rejected",
                    {"request_id": request.request_id, "reason": operator_error},
                )
                return {"success": False, "error": operator_error, "status_code": 403}
            operator_audit = ui_operator_audit_record(operator_payload)

        def settle_deny(settled_request: AuthorityRequest) -> dict[str, Any]:
            deny_record = None
            if persist:
                deny_record = self._request_store.add_deny(
                    principal_id=settled_request.principal_id,
                    permission_id=settled_request.permission_id,
                    resource=settled_request.resource,
                    reason=reason or settled_request.reason,
                )
            return {"deny": deny_record}

        def rollback_deny(
            _settled_request: AuthorityRequest,
            result: dict[str, Any] | None,
        ) -> None:
            deny_record = (result or {}).get("deny")
            deny_id = str((deny_record or {}).get("deny_id") or "")
            if deny_id:
                self._request_store.remove_deny(
                    deny_id,
                    reason="deny_settlement_failed",
                )

        try:
            settlement = self._request_store.settle_pending_request(
                request.request_id,
                "denied",
                settle_deny,
                rollback_deny,
            )
        except Exception:
            return {
                "success": False,
                "error": "Authority denial failed",
                "status_code": 500,
                "reason": "deny_settlement_failed",
            }
        if not settlement.get("settled"):
            return self._settlement_failure_response(settlement)
        request = settlement["request"]
        deny_record = (settlement.get("result") or {}).get("deny")
        self._audit_best_effort(
            "authority_request_denied",
            {
                "request_id": request.request_id,
                "persist": bool(persist),
                "reason": reason,
                **operator_audit,
            },
        )
        return {
            "success": True,
            "request_id": request.request_id,
            "denied": True,
            "deny": deny_record,
        }

    def _audit_best_effort(self, action: str, details: dict[str, Any]) -> None:
        try:
            self._request_store.audit(action, details)
        except Exception:
            pass

    def list_requests(
        self,
        status: str = "all",
        *,
        profile_id: str | None = None,
        actor_principal: Any = None,
        debug_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        requests = [
            self._request_view(item)
            for item in self._request_store.list_requests(status)
            if self._actor_can_access_request(
                item,
                profile_id=profile_id,
                actor_principal=actor_principal,
            )
            and self._matches_debug_binding(item, debug_binding)
        ]
        return {"requests": requests, "pending": [item for item in requests if item.get("status") == "pending"], "count": len(requests)}

    def get_request(
        self,
        request_id: str,
        *,
        profile_id: str | None = None,
        actor_principal: Any = None,
        debug_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = self._request_store.get_request(request_id)
        if request is None:
            return {"success": False, "error": "Authority request not found", "status_code": 404}
        if not self._actor_can_access_request(
            request,
            profile_id=profile_id,
            actor_principal=actor_principal,
        ):
            return {"success": False, "error": "Authority request not found", "status_code": 404}
        if not self._matches_debug_binding(request, debug_binding):
            return {"success": False, "error": "Authority request not found", "status_code": 404}
        return {"success": True, "request": self._request_view(request)}

    @staticmethod
    def _matches_debug_binding(
        request: AuthorityRequest,
        debug_binding: dict[str, Any] | None,
    ) -> bool:
        if not isinstance(debug_binding, dict):
            return True
        expected = {
            "debug_session_id": str(debug_binding.get("debug_session_id") or ""),
            "lease_epoch": int(debug_binding.get("lease_epoch") or 0),
            "debug_run_id": str(debug_binding.get("debug_run_id") or ""),
            "workspace_identity_digest": str(
                debug_binding.get("workspace_identity_digest") or ""
            ),
            "pack_id": str(debug_binding.get("pack_id") or ""),
            "debug_profile_id": str(debug_binding.get("profile_id") or ""),
        }
        return bool(expected["debug_session_id"]) and all(
            str(getattr(request, key, "") or "") == str(value)
            for key, value in expected.items()
        )

    @classmethod
    def _actor_can_access_request(
        cls,
        request: AuthorityRequest,
        *,
        profile_id: str | None = None,
        actor_principal: Any = None,
    ) -> bool:
        if actor_principal is not None and cls._actor_core_role(actor_principal):
            return True
        expected_profile = str(profile_id or "").strip() or cls._actor_profile_id(actor_principal)
        if not expected_profile:
            return True
        target_profile = str(request.profile_id or "").strip()
        if not target_profile:
            target_profile = parse_principal_parts(request.principal_id).get("profile", "")
        return bool(target_profile and target_profile == expected_profile)

    @staticmethod
    def _actor_core_role(actor_principal: Any) -> bool:
        if isinstance(actor_principal, dict):
            return bool(actor_principal.get("core_role"))
        return bool(getattr(actor_principal, "core_role", False))

    @staticmethod
    def _actor_profile_id(actor_principal: Any) -> str:
        if actor_principal is None:
            return ""
        if isinstance(actor_principal, dict):
            if bool(actor_principal.get("core_role")):
                return ""
            return str(actor_principal.get("profile_id") or "").strip()
        if bool(getattr(actor_principal, "core_role", False)):
            return ""
        return str(getattr(actor_principal, "profile_id", "") or "").strip()

    @staticmethod
    def _actor_device_id(actor_principal: Any) -> str:
        if isinstance(actor_principal, dict):
            return str(actor_principal.get("device_id") or "").strip()
        return str(getattr(actor_principal, "device_id", "") or "").strip()

    @staticmethod
    def _actor_token_id(actor_principal: Any) -> str:
        if isinstance(actor_principal, dict):
            return str(actor_principal.get("token_id") or "").strip()
        return str(getattr(actor_principal, "token_id", "") or "").strip()

    @staticmethod
    def _actor_mobile_approver(actor_principal: Any) -> bool:
        if isinstance(actor_principal, dict):
            return str(actor_principal.get("role") or "").strip() == "mobile_approver"
        return str(getattr(actor_principal, "role", "") or "").strip() == "mobile_approver"

    @staticmethod
    def _actor_scopes(actor_principal: Any) -> set[str]:
        if isinstance(actor_principal, dict):
            values = actor_principal.get("scopes") or ()
        else:
            values = getattr(actor_principal, "scopes", ()) or ()
        if not isinstance(values, (list, tuple, set)):
            return set()
        return {str(value).strip() for value in values if str(value).strip()}

    def _mobile_actor_has_route_grant(self, actor_principal: Any, permission_id: str) -> bool:
        if permission_id in self._actor_scopes(actor_principal):
            return True
        manager = self._capability_grant_manager
        if manager is None:
            return False
        profile_id = self._actor_profile_id(actor_principal)
        if not profile_id:
            return False
        profile = f"profile:{profile_id}"
        principals = [profile]
        surface_id = "mobile-approver"
        device_id = self._actor_device_id(actor_principal)
        surface = f"{profile}__surface:{surface_id}"
        principals.append(surface)
        if device_id:
            principals.append(f"{surface}__device:{device_id}")
        configs: list[dict[str, Any]] = []
        for principal_id in principals:
            try:
                if callable(getattr(manager, "check_authority", None)):
                    check = manager.check_authority(principal_id, permission_id)
                elif callable(getattr(manager, "check", None)):
                    check = manager.check(principal_id, permission_id)
                else:
                    return False
            except Exception:
                return False
            if not getattr(check, "allowed", False):
                return False
            config = getattr(check, "config", None)
            configs.append(dict(config) if isinstance(config, dict) else {})
        try:
            meet_authority_configs(*configs)
        except AuthorityConfigError:
            return False
        return True

    def one_shot_approval_issued(
        self,
        *,
        request_id: str,
        permission_id: str,
        token: str,
        conversation_id: str | None = None,
        principal_id: str | None = None,
        resource: dict[str, Any] | None = None,
        include_consumed: bool = False,
    ) -> bool:
        request = self._request_store.get_request(request_id)
        if request is None:
            return False
        if request.permission_id != str(permission_id or "").strip():
            return False
        if conversation_id and request.conversation_id and request.conversation_id != conversation_id:
            return False
        expected_principal = str(principal_id or request.principal_id or "").strip() or None
        return self._request_store.one_shot_matches_request(
            request_id=request.request_id,
            principal_id=expected_principal,
            permission_id=request.permission_id,
            token=token,
            resource=resource,
            include_consumed=include_consumed,
        )

    def list_grants(self, principal_id: str = "", *, actor_principal: Any = None) -> dict[str, Any]:
        manager = self._capability_grant_manager
        if manager is None:
            return {"grants": {}, "count": 0}
        principal_id = str(principal_id or "").strip()
        actor_profile_id = self._actor_profile_id(actor_principal)
        if actor_principal is not None and not self._actor_core_role(actor_principal):
            profile_prefix = f"profile:{actor_profile_id}"
            if not actor_profile_id:
                return {"success": False, "error": "Forbidden", "status_code": 403}
            if not principal_id:
                principal_id = profile_prefix
            if principal_id != profile_prefix and not principal_id.startswith(f"{profile_prefix}__"):
                return {"success": False, "error": "Authority grants not found", "status_code": 404}
        if principal_id:
            grant = manager.get_grant(principal_id) if callable(getattr(manager, "get_grant", None)) else None
            return {
                "grants": {principal_id: grant.to_dict()} if grant is not None and hasattr(grant, "to_dict") else {},
                "count": 1 if grant is not None else 0,
                "principal_id": principal_id,
            }
        all_grants = manager.get_all_grants() if callable(getattr(manager, "get_all_grants", None)) else {}
        result = {pid: grant.to_dict() if hasattr(grant, "to_dict") else grant for pid, grant in dict(all_grants or {}).items()}
        return {"grants": result, "count": len(result)}

    def delete_grant(
        self,
        principal_id: str,
        permission_id: str,
        *,
        actor_principal: Any = None,
    ) -> dict[str, Any]:
        manager = self._capability_grant_manager
        if manager is None or not callable(getattr(manager, "revoke_permission", None)):
            return {"success": False, "error": "CapabilityGrantManager unavailable", "status_code": 500}
        principal_id = str(principal_id or "").strip()
        permission_id = str(permission_id or "").strip()
        if not principal_id or not permission_id:
            return {"success": False, "error": "principal_id and permission_id are required", "status_code": 400}
        actor_profile_id = self._actor_profile_id(actor_principal)
        if actor_principal is not None and not bool(getattr(actor_principal, "core_role", False)):
            if not actor_profile_id:
                return {"success": False, "error": "Forbidden", "status_code": 403}
            profile_prefix = f"profile:{actor_profile_id}"
            if principal_id != profile_prefix and not principal_id.startswith(f"{profile_prefix}__"):
                return {"success": False, "error": "Authority grants not found", "status_code": 404}
        revoked = bool(manager.revoke_permission(principal_id, permission_id))
        self._request_store.audit(
            "authority_grant_deleted",
            {
                "principal_id": principal_id,
                "permission_id": permission_id,
                "revoked": revoked,
                "actor_profile_id": actor_profile_id,
            },
        )
        return {"success": True, "principal_id": principal_id, "permission_id": permission_id, "revoked": revoked}

    def events(self, limit: int = 200, *, actor_principal: Any = None) -> dict[str, Any]:
        if actor_principal is not None and not self._actor_core_role(actor_principal):
            return {"success": False, "error": "Forbidden", "status_code": 403}
        return {"_sse": True, "events": self._request_store.list_events(limit)}

    def _matching_capability_grant(
        self,
        candidates: list[str],
        permission_id: str,
        resource: dict[str, Any],
    ) -> tuple[str, dict[str, Any]] | None:
        manager = self._capability_grant_manager
        if manager is None or not callable(getattr(manager, "get_grant", None)):
            return None
        profile_chain = self._profile_principal_chain(candidates[0] if candidates else "")
        if profile_chain:
            profile_match, _profile_has_grant = self._matching_profile_chain_grant(profile_chain, permission_id, resource)
            return profile_match
        for candidate in candidates:
            permission_state, config = self._permission_state_for_principal(candidate, permission_id)
            if permission_state == "disabled":
                return None
            if permission_state != "enabled":
                continue
            try:
                config = authority_constraints_from_config(config)
            except AuthorityConfigError:
                return None
            if self._resource_allowed(config, resource):
                return candidate, config
        return None

    def _matching_profile_chain_grant(
        self,
        profile_chain: list[str],
        permission_id: str,
        resource: dict[str, Any],
    ) -> tuple[tuple[str, dict[str, Any]] | None, bool]:
        configs: list[dict[str, Any]] = []
        matched_principal = ""
        for principal_id in profile_chain:
            permission_state, config = self._permission_state_for_principal(principal_id, permission_id)
            if permission_state == "disabled":
                return None, True
            if permission_state != "enabled":
                if self._profile_chain_segment_optional(principal_id):
                    continue
                return None, True
            configs.append(config)
            matched_principal = principal_id
        if not configs:
            return None, True
        try:
            effective_config = meet_authority_configs(*configs)
        except AuthorityConfigError:
            return None, True
        if self._resource_allowed(effective_config, resource):
            return (matched_principal, effective_config), True
        return None, True

    def _permission_config_for_principal(
        self,
        principal_id: str,
        permission_id: str,
    ) -> tuple[bool, dict[str, Any]]:
        permission_state, config = self._permission_state_for_principal(principal_id, permission_id)
        return permission_state == "enabled", config

    def _permission_state_for_principal(
        self,
        principal_id: str,
        permission_id: str,
    ) -> tuple[str, dict[str, Any]]:
        manager = self._capability_grant_manager
        if manager is None or not callable(getattr(manager, "get_grant", None)):
            return "missing", {}
        grant = manager.get_grant(principal_id)
        if grant is None:
            return "missing", {}
        if not getattr(grant, "enabled", False):
            return "disabled", {}
        permission = getattr(grant, "permissions", {}).get(permission_id)
        if permission is None:
            return "missing", {}
        if not getattr(permission, "enabled", False):
            return "disabled", {}
        return "enabled", dict(getattr(permission, "config", {}) or {})

    @staticmethod
    def _profile_principal_chain(principal_id: str) -> list[str]:
        principal_id = str(principal_id or "").strip()
        if not principal_id.startswith("profile:"):
            return []
        parts = [part for part in principal_id.split("__") if part]
        return ["__".join(parts[:index]) for index in range(1, len(parts) + 1)]

    @staticmethod
    def _profile_chain_segment_optional(principal_id: str) -> bool:
        segment = str(principal_id or "").split("__")[-1]
        key = segment.split(":", 1)[0] if ":" in segment else ""
        return key in {"graph", "node"}

    @staticmethod
    def _normalize_resource(resource: dict[str, Any]) -> dict[str, Any]:
        return sanitize_authority_resource(resource)

    @staticmethod
    def _resource_allowed(config: dict[str, Any], resource: dict[str, Any]) -> bool:
        for config_key, resource_keys in AuthorityService._config_resource_fields().items():
            if config_key not in config:
                continue
            allowed = set(AuthorityService._string_values(config.get(config_key)))
            if not allowed:
                return False
            resource_values = {
                value
                for resource_key in resource_keys
                if (value := str(resource.get(resource_key) or "").strip())
            }
            if not resource_values.intersection(allowed):
                return False
        if "ports" in config:
            allowed_ports = set(AuthorityService._port_values(config.get("ports")))
            if not allowed_ports:
                return False
            raw_port = resource.get("port")
            if not isinstance(raw_port, (str, int, float, bytes)):
                return False
            try:
                resource_port = int(raw_port)
            except (TypeError, ValueError):
                return False
            if resource_port not in allowed_ports:
                return False
        if "allow_stream" in config and resource.get("stream") and not bool(config.get("allow_stream")):
            return False
        if "max_input_tokens" in config and resource.get("input_tokens") is not None:
            raw_max_tokens = config.get("max_input_tokens")
            if not isinstance(raw_max_tokens, (str, int, float, bytes)):
                return False
            try:
                if int(resource.get("input_tokens") or 0) > int(raw_max_tokens):
                    return False
            except (TypeError, ValueError):
                return False
        return True

    @staticmethod
    def _string_values(value: Any) -> list[str]:
        values = value if isinstance(value, list) else [value]
        return [str(item).strip() for item in values if str(item or "").strip()]

    @staticmethod
    def _port_values(value: Any) -> list[int]:
        values = value if isinstance(value, list) else [value]
        ports: list[int] = []
        for item in values:
            try:
                ports.append(int(item))
            except (TypeError, ValueError):
                continue
        return ports

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    def _resource_always_allowed(permission_id: str, resource: dict[str, Any]) -> bool:
        if permission_id not in {"model.invoke", "api_key.use"}:
            return False
        provider_id = str(resource.get("provider_id") or "").strip()
        model_ref = str(resource.get("model_ref") or resource.get("model_id") or "").strip()
        if provider_id == "stub" or model_ref == "stub/default":
            return True
        if provider_id == "rumi":
            return True
        return False

    @staticmethod
    def _risk_level(permission_id: str, resource: dict[str, Any]) -> str:
        if permission_id.startswith("host."):
            definition = get_host_permission_definition(permission_id)
            if definition is not None:
                return definition.risk_level
            return "high"
        if permission_id in {"auth.token.issue", "auth.token.revoke", "authority.grant.manage"}:
            return "critical"
        if permission_id.endswith(".manage"):
            return "high"
        if permission_id == "network.egress" and resource.get("domain") == "*":
            return "high"
        if permission_id == "network.egress":
            return "medium"
        if permission_id == "secret.read":
            return "high"
        if permission_id == "model.invoke":
            provider_id = str(resource.get("provider_id") or "")
            return "low" if provider_id in {"stub", "rumi"} else "medium"
        return "medium" if permission_id in {"api_key.use", "file.write"} else "low"

    @staticmethod
    def _grant_config_from_resource(resource: dict[str, Any]) -> dict[str, Any]:
        return authority_config_from_resource(resource)

    @staticmethod
    def _grant_config_for_persistent_approval(
        resource: dict[str, Any],
        client_config: dict[str, Any] | None,
    ) -> dict[str, Any]:
        grant_config = AuthorityService._grant_config_from_resource(resource)
        if not isinstance(client_config, dict):
            return grant_config
        client_config = validate_authority_config(client_config)

        for key in AuthorityService._resource_config_keys():
            if key not in client_config or key not in grant_config:
                continue
            base_values = AuthorityService._string_values(grant_config.get(key))
            requested_values = set(AuthorityService._string_values(client_config.get(key)))
            grant_config[key] = [value for value in base_values if value in requested_values] if requested_values else []

        if "ports" in client_config and "ports" in grant_config:
            base_ports = AuthorityService._port_values(grant_config.get("ports"))
            requested_ports = set(AuthorityService._port_values(client_config.get("ports")))
            grant_config["ports"] = [port for port in base_ports if port in requested_ports] if requested_ports else []

        if "allow_stream" in client_config:
            if "allow_stream" in grant_config:
                grant_config["allow_stream"] = bool(grant_config.get("allow_stream")) and bool(client_config.get("allow_stream"))
            elif client_config.get("allow_stream") is False:
                grant_config["allow_stream"] = False

        requested_max_tokens = AuthorityService._positive_int(client_config.get("max_input_tokens"))
        if requested_max_tokens is not None:
            current_max_tokens = AuthorityService._positive_int(grant_config.get("max_input_tokens"))
            grant_config["max_input_tokens"] = (
                min(current_max_tokens, requested_max_tokens)
                if current_max_tokens is not None
                else requested_max_tokens
            )

        return grant_config

    @staticmethod
    def _resource_config_keys() -> tuple[str, ...]:
        return tuple(dict.fromkeys(config_key for _, config_key in RESOURCE_CONFIG_FIELDS))

    @staticmethod
    def _config_resource_fields() -> dict[str, tuple[str, ...]]:
        fields: dict[str, list[str]] = {}
        for resource_key, config_key in RESOURCE_CONFIG_FIELDS:
            fields.setdefault(config_key, []).append(resource_key)
        return {config_key: tuple(resource_keys) for config_key, resource_keys in fields.items()}

    @staticmethod
    def _principal_for_scope(request: AuthorityRequest, scope: str) -> str:
        if scope == "conversation" and request.conversation_id:
            return f"conversation:{request.conversation_id}"
        if scope == "profile":
            profile_id = request.profile_id or parse_principal_parts(request.principal_id).get("profile")
            return f"profile:{profile_id}" if profile_id else ""
        if scope == "node":
            parts = parse_principal_parts(request.principal_id)
            profile_id = request.profile_id or parts.get("profile")
            graph_id = request.graph_id or parts.get("graph")
            node_id = request.node_id or parts.get("node")
            return build_principal_id(profile_id=profile_id, graph_id=graph_id, node_id=node_id) if profile_id and node_id else ""
        return ""

    def _decision(
        self,
        allowed: bool,
        permission_id: str,
        principal_id: str,
        resource: dict[str, Any],
        reason: str,
        risk_level: str,
        *,
        request_id: str | None = None,
        approval_required: bool = False,
        grant_config: dict[str, Any] | None = None,
    ) -> AuthorityDecision:
        return AuthorityDecision(
            allowed=allowed,
            permission_id=permission_id,
            principal_id=principal_id,
            reason=reason,
            request_id=request_id,
            approval_required=approval_required,
            risk_level=risk_level,
            grant_config=dict(grant_config or {}),
            resource=dict(resource or {}),
        )

    def _request_view(self, request: AuthorityRequest) -> dict[str, Any]:
        data = request.to_dict()
        data["display_metadata"] = self._display_metadata(request)
        data["allowed_scopes"] = self._allowed_scopes(request)
        return data

    def _display_metadata(self, request: AuthorityRequest) -> dict[str, Any]:
        resource = dict(request.resource or {})
        provider_id = str(resource.get("provider_id") or "")
        api_id = str(resource.get("api_id") or "")
        model_id = str(resource.get("model_id") or resource.get("model_ref") or "")
        function_id = str(resource.get("function_id") or "")
        pack_id = str(resource.get("pack_id") or "")
        target_pack_id = str(resource.get("target_pack_id") or "")
        pack_request_id = str(resource.get("pack_request_id") or "")
        pack_request_mode = str(resource.get("mode") or "")
        app_name = str(resource.get("app_display_name") or "")
        provider_display_name = str(resource.get("provider_display_name") or provider_id or "")
        model_display_name = str(resource.get("model_display_name") or model_id or "")
        endpoint_url = str(resource.get("endpoint_url") or "")
        endpoint_host = str(resource.get("domain") or "")
        endpoint_path = str(resource.get("endpoint_path") or "")
        credential_label = str(resource.get("credential_label") or "")
        has_rich_provider_metadata = bool(
            app_name
            or resource.get("provider_display_name")
            or resource.get("model_display_name")
            or endpoint_url
            or credential_label
        )
        subject = " / ".join(item for item in (provider_id, api_id, model_id, function_id, pack_id) if item)
        title = subject or request.permission_id
        summary = request.reason or f"{request.permission_id} requires approval"
        host_definition = get_host_permission_definition(request.permission_id)
        permission_label = {
            "model.invoke": "Model/API",
            "api_key.use": "API key",
            "network.egress": "Network access",
            "pack.approve": "Pack approval",
        }.get(request.permission_id, host_definition.label if host_definition is not None else request.permission_id)
        access_summary = ""
        host_execution_summary: dict[str, Any] = {}
        typed_confirmation_required = self._typed_confirmation_required(request)
        confirmation_phrase = self._confirmation_phrase(request)
        if resource.get("kind") == "host_intent" or request.permission_id.startswith("host."):
            operation = str(resource.get("operation") or resource.get("host_operation") or request.permission_id)
            caller = " / ".join(
                item
                for item in (
                    resource.get("caller_pack_id") or resource.get("pack_id"),
                    resource.get("caller_function_id") or resource.get("function_id"),
                )
                if item
            )
            stream_label = "stream" if resource.get("stream_enabled") else "one-shot"
            title = f"Host操作 {operation} を許可しますか？"
            summary = f"{caller or request.principal_id} が {operation} ({stream_label}) を要求しています。"
            access_summary = f"{operation} / {stream_label}"
            host_execution_summary = _host_execution_display_summary(resource.get("args_summary"))
            if host_execution_summary:
                access_summary = _host_execution_access_summary(
                    operation=operation,
                    stream_label=stream_label,
                    summary=host_execution_summary,
                )
        if request.permission_id == "pack.approve" and resource.get("kind") == "defaultspack.pack_request":
            target_label = target_pack_id or pack_id or "pack"
            title = f"{target_label} のpack requestを承認しますか？"
            summary = (
                f"{target_label} への {pack_request_mode or 'pack'} request"
                f"{f' ({pack_request_id})' if pack_request_id else ''} を確認します。"
            )
            access_summary = pack_request_mode or "pack request"
        if has_rich_provider_metadata and request.permission_id in {"model.invoke", "api_key.use", "network.egress"}:
            app_label = app_name or "defaultspack"
            provider_label = provider_display_name or provider_id or "provider"
            provider_subject = (
                provider_label
                if provider_label.strip().lower().endswith("provider")
                else f"{provider_label} provider"
            )
            model_label = model_display_name or model_id or "model"
            endpoint_text = f"{endpoint_url} へのアクセス" if endpoint_url else "外部 API へのアクセス"
            credential_text = f"{credential_label} の使用" if credential_label else "API key の使用"
            title = f"{app_label} / {provider_subject} に {credential_text}と {endpoint_text}を許可しますか？"
            summary = (
                f"{app_label}: {provider_subject} を {model_label} との通信に使います。"
                f"{credential_text}と {endpoint_text}を含みます。"
            )
            access_summary = f"{credential_text} / {endpoint_text}"
        return {
            "title": title,
            "summary": summary,
            "permission_id": request.permission_id,
            "provider_id": provider_id or None,
            "api_id": api_id or None,
            "model_id": model_id or None,
            "function_id": function_id or None,
            "pack_id": pack_id or None,
            "target_pack_id": target_pack_id or None,
            "pack_request_id": pack_request_id or None,
            "pack_request_mode": pack_request_mode or None,
            "app_display_name": app_name or None,
            "provider_display_name": provider_display_name or None,
            "model_display_name": model_display_name or None,
            "endpoint_url": endpoint_url or None,
            "endpoint_host": endpoint_host or None,
            "endpoint_path": endpoint_path or None,
            "credential_label": credential_label or None,
            "permission_label": permission_label,
            "access_summary": access_summary or None,
            "host_execution_summary": host_execution_summary or None,
            "risk_level": request.risk_level,
            "typed_confirmation_required": typed_confirmation_required,
            "confirmation_phrase": confirmation_phrase or None,
            "audit_text": (
                "Approving records a signed local UI-operator action and grants only "
                "the requested resource constraints."
            ),
        }

    def _allowed_scopes(self, request: AuthorityRequest) -> list[str]:
        if self._typed_confirmation_required(request):
            return ["once"]
        scopes = ["once"]
        if request.conversation_id:
            scopes.append("conversation")
        if request.profile_id or parse_principal_parts(request.principal_id).get("profile"):
            scopes.append("profile")
        parts = parse_principal_parts(request.principal_id)
        if request.node_id or parts.get("node"):
            scopes.append("node")
        return scopes

    def _typed_confirmation_required(self, request: AuthorityRequest) -> bool:
        resource = dict(request.resource or {})
        definition = get_host_permission_definition(request.permission_id)
        return bool(
            request.risk_level == "critical"
            or request.permission_id == "host.process.exec_guarded"
            or resource.get("typed_confirmation_required")
            or (definition.typed_confirmation_required if definition is not None else False)
        )

    @staticmethod
    def _confirmation_phrase(request: AuthorityRequest) -> str:
        resource = dict(request.resource or {})
        return str(resource.get("confirmation_phrase") or resource.get("typed_confirmation_phrase") or "").strip()

    def _audit_check(self, action: str, principal_id: str, permission_id: str, resource: dict[str, Any]) -> None:
        self._request_store.audit(
            "authority_check_" + action,
            {
                "principal_id": principal_id,
                "permission_id": permission_id,
                "resource_hash": self._request_store.resource_hash(resource),
                "provider_id": resource.get("provider_id"),
                "api_id": resource.get("api_id"),
                "model_id": resource.get("model_id"),
            },
        )


def _host_execution_display_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    summary: dict[str, Any] = {}
    executable = _display_text(value.get("executable"))
    if executable:
        summary["executable"] = executable
    argument_count = _display_int(value.get("argument_count"))
    if argument_count is not None:
        summary["argument_count"] = argument_count
    cwd = _display_text(value.get("cwd"))
    if cwd:
        summary["cwd"] = cwd
    target_paths = _display_text_list(value.get("target_paths"))
    if target_paths:
        summary["target_paths"] = target_paths
    target_urls = _display_url_list(value.get("target_urls"))
    if target_urls:
        summary["target_urls"] = target_urls
    return summary


def _host_execution_access_summary(*, operation: str, stream_label: str, summary: dict[str, Any]) -> str:
    parts = [f"{operation} / {stream_label}"]
    executable = _display_text(summary.get("executable"))
    if executable:
        parts.append(f"exec: {executable}")
    argument_count = _display_int(summary.get("argument_count"))
    if argument_count is not None:
        parts.append(f"args: {argument_count}")
    cwd = _display_text(summary.get("cwd"))
    if cwd:
        parts.append(f"cwd: {cwd}")
    target_paths = _display_text_list(summary.get("target_paths"))
    if target_paths:
        parts.append("paths: " + ", ".join(target_paths))
    target_urls = _display_text_list(summary.get("target_urls"))
    if target_urls:
        parts.append("urls: " + ", ".join(target_urls))
    return " / ".join(parts)


def _display_text(value: Any, *, max_length: int = 160) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) > max_length:
        return text[: max(0, max_length - 14)] + "...(truncated)"
    return text


def _display_text_list(value: Any, *, max_items: int = 6) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _display_text(item)
        if text and text not in result:
            result.append(text)
        if len(result) >= max_items:
            break
    return result


def _display_url_list(value: Any, *, max_items: int = 6) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        url = _display_url(item)
        if url and url not in result:
            result.append(url)
        if len(result) >= max_items:
            break
    return result


def _display_url(value: Any) -> str:
    text = _display_text(value)
    candidate = text.split()[0] if text else ""
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return ""
    hostname = parsed.hostname or ""
    if not hostname:
        return ""
    if ":" in hostname and not (hostname.startswith("[") and hostname.endswith("]")):
        hostname = f"[{hostname}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{hostname}:{port}" if port is not None else hostname
    return _display_text(urlunsplit((scheme, netloc, parsed.path or "", "", "")))


def _display_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
