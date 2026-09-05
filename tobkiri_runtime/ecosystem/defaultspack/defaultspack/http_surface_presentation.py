"""Defaultspack-specific HTTP capability payload and UI projection rules."""

from __future__ import annotations

import time
import uuid
from pathlib import PurePosixPath
from typing import Mapping

from core_runtime.global_contracts.http_contract_dispatch import (
    HTTPCapabilitySnapshot,
    HTTPContractBinding,
    HTTPContractTarget,
)
from core_runtime.pack_api_server import (
    ApplicationHTTPContractRequest,
    CapabilitySnapshotReader,
    DispatchSession,
    WorkspaceBindingResolver,
)
from tobkiri_protocol.canonical import canonical_digest


_CONVERSATION_TARGET = (
    "defaults.conversation.complete",
    "conversation.turn.v1",
    "complete",
    "defaultspack.conversation",
    "defaultspack.conversation",
)
_CAPABILITY_REQUEST_FIELDS = frozenset(
    {
        "request_id",
        "expires_at",
        "profile_id",
        "profile_revision",
        "activation_id",
        "plan_hash",
        "catalog_hash",
        "contribution_id",
        "owner_pack_id",
        "contract_id",
        "payload",
    }
)


class DefaultspackHTTPPresentation:
    """Interpret Defaultspack UI capability contracts after Host validation."""

    def decode_request(
        self,
        binding: HTTPContractBinding,
        *,
        body: Mapping[str, object],
        query: Mapping[str, object],
        session: DispatchSession,
        snapshot: HTTPCapabilitySnapshot,
    ) -> ApplicationHTTPContractRequest | None:
        """Verify the application capability envelope and select its target."""

        if binding.path != "/api/ui/capability/invoke":
            return None
        if set(body) != _CAPABILITY_REQUEST_FIELDS:
            return None
        request_id = body.get("request_id")
        expires_at = body.get("expires_at")
        try:
            request_id_valid = (
                isinstance(request_id, str)
                and str(uuid.UUID(request_id)) == request_id
            )
        except ValueError:
            request_id_valid = False
        now = time.time()
        expiry_valid = (
            isinstance(expires_at, (float, int))
            and not isinstance(expires_at, bool)
            and now < float(expires_at) <= now + 60
        )
        if not request_id_valid or not expiry_valid:
            return None
        try:
            session.assert_current()
        except Exception:
            return None
        if (
            body.get("profile_id") != getattr(session, "profile_id", None)
            or body.get("profile_revision")
            != getattr(session, "profile_revision", None)
            or body.get("activation_id") != getattr(session, "activation_id", None)
            or body.get("plan_hash") != getattr(session, "plan_digest", None)
            or body.get("catalog_hash") != snapshot.catalog_hash
        ):
            return None
        nested = body.get("payload")
        if not isinstance(nested, Mapping) or any(
            not isinstance(key, str) for key in nested
        ):
            return None
        target = next(
            (
                candidate
                for candidate in snapshot.targets
                if candidate.contribution_id == body.get("contribution_id")
                and candidate.contract_id == body.get("contract_id")
                and candidate.owner_pack_id == body.get("owner_pack_id")
            ),
            None,
        )
        if target is None:
            return None
        return ApplicationHTTPContractRequest(
            target=target,
            payload={**dict(query), **dict(nested)},
        )

    def normalize_payload(
        self,
        target: HTTPContractTarget,
        payload: Mapping[str, object],
        *,
        session: DispatchSession,
        workspace_binding_resolver: WorkspaceBindingResolver | None,
    ) -> Mapping[str, object]:
        """Bind Defaultspack media targets to their selected workspace only."""

        if not target.contribution_id.startswith("pack."):
            return dict(payload)
        if target.contract_id != "tobkiri.service.media.inspect.v1":
            raise ValueError("dynamic Pack operation is not an approved media contract")
        if payload.get("name") not in {
            "document.parse",
            "image.inspect",
            "audio.inspect",
            "recording.inspect",
        }:
            raise ValueError("media inspection operation is not selected")
        raw_path = payload.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
            raise ValueError("a workspace-relative path is required")
        if "\\" in raw_path:
            raise PermissionError("backslash paths are not accepted")
        relative = PurePosixPath(raw_path.strip())
        if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
            raise PermissionError("a workspace-relative path is required")
        if workspace_binding_resolver is None:
            raise RuntimeError("Host workspace binding resolver is unavailable")
        profile_id = str(getattr(session, "profile_id", ""))
        binding = dict(workspace_binding_resolver(profile_id))
        normalized = dict(payload)
        normalized["path"] = relative.as_posix()
        normalized["profile_id"] = profile_id
        normalized["workspace_id"] = binding["workspace_id"]
        normalized["require_selected"] = True
        normalized["_workspace_binding"] = binding
        return normalized

    def requires_operation_ready(self, target: HTTPContractTarget) -> bool:
        """Leave the declarative conversation route bindable while unavailable.

        Its Broker invocation still performs the Host-owned Plan, activation,
        grant, and backend readiness checks.  This only permits the desktop
        to render a capture-verified route with its unavailable state.
        """

        return not _is_conversation(target)

    def present_result(
        self,
        binding: HTTPContractBinding,
        result: Mapping[str, object],
        *,
        session: DispatchSession | None,
        routes: Mapping[tuple[str, str], HTTPContractBinding],
        capability_snapshot: CapabilitySnapshotReader,
    ) -> Mapping[str, object]:
        """Attach Defaultspack UI contributions to the committed catalog result."""

        if binding.presentation != "dynamic_pack_catalog":
            return dict(result)
        capability_binding = routes.get(("POST", "/api/ui/capability/invoke"))
        snapshot = (
            capability_snapshot(capability_binding, catalog=result)
            if capability_binding is not None
            else HTTPCapabilitySnapshot(
                catalog_hash=canonical_digest({"contributions": []}), targets=()
            )
        )
        return {
            **dict(result),
            "dynamic_host": {
                "version": "rumi.ui.contribution.v1",
                "profile_id": str(getattr(session, "profile_id", "")),
                "profile_revision": str(getattr(session, "profile_revision", "")),
                "activation_id": str(getattr(session, "activation_id", "")),
                "plan_hash": str(getattr(session, "plan_digest", "")),
                "contributions": [
                    _contribution(target, index, session)
                    for index, target in enumerate(snapshot.targets)
                ],
                "diagnostics": _diagnostics(result, session),
                "quarantined_pack_ids": [],
                "catalog_hash": snapshot.catalog_hash,
            },
        }


def _is_conversation(target: HTTPContractTarget) -> bool:
    return (
        target.contribution_id,
        target.contract_id,
        target.operation_id,
        target.provider_id,
        target.function_id,
    ) == _CONVERSATION_TARGET


def _contribution(
    target: HTTPContractTarget,
    priority: int,
    session: DispatchSession | None,
) -> dict[str, object]:
    conversation = _is_conversation(target)
    profile_id = str(getattr(session, "profile_id", ""))
    profile_revision = str(getattr(session, "profile_revision", ""))
    activation_id = str(getattr(session, "activation_id", ""))
    plan_digest = str(getattr(session, "plan_digest", ""))
    contribution: dict[str, object] = {
        "contribution_id": target.contribution_id,
        "kind": "route" if conversation else "action",
        "mode": "declarative" if conversation else "same_origin_builtin",
        "label": "Tobkiri Conversation" if conversation else target.operation_id,
        "priority": priority,
        "owner_pack_id": target.owner_pack_id,
        "owner_pack_hash": target.artifact_digest or plan_digest,
        "build_identity": target.function_id,
        "resolved_profile_id": profile_id,
        "resolved_profile_revision": profile_revision,
        "resolved_activation_id": activation_id,
        "resolved_plan_hash": plan_digest,
        "descriptor_hash": canonical_digest(
            {
                "contribution_id": target.contribution_id,
                "operation_id": target.operation_id,
            }
        ),
        "route": "/chat" if conversation else "/packs",
        "action_contract": target.contract_id,
        "operation_id": target.operation_id,
        "provider_id": target.provider_id,
        "function_id": target.function_id,
        "localization": {},
        "accessibility": {
            "name": "Tobkiri Conversation" if conversation else target.operation_id,
            "keyboard": True,
        },
    }
    if conversation:
        contribution["view"] = {
            "type": "conversation_v4",
            "title": "Tobkiri Conversation",
            "body": "Start a conversation with your active Tobkiri Profile.",
        }
    return contribution


def _diagnostics(
    catalog: Mapping[str, object],
    session: DispatchSession | None,
) -> list[dict[str, str]]:
    packs = catalog.get("packs")
    if session is None or not isinstance(packs, list):
        return []
    diagnostics: list[dict[str, str]] = []
    for pack in packs:
        if not isinstance(pack, Mapping) or pack.get("enabled") is not True:
            continue
        pack_id = str(pack.get("pack_id") or "")
        operations = pack.get("operations")
        if not isinstance(operations, list):
            continue
        for operation in operations:
            if not isinstance(operation, Mapping) or operation.get("invokable") is not True:
                continue
            contract_id = str(operation.get("contract_id") or "")
            operation_id = str(operation.get("operation_id") or "")
            provider_id = str(operation.get("provider_id") or "")
            for provider in session.provider_metadata(contract_id):
                if (
                    provider.get("provider_id") == provider_id
                    and provider.get("operation_id") == operation_id
                    and provider.get("profile_id") == getattr(session, "profile_id", None)
                    and provider.get("profile_revision")
                    == getattr(session, "profile_revision", None)
                    and provider.get("activation_id")
                    == getattr(session, "activation_id", None)
                    and provider.get("plan_digest") == getattr(session, "plan_digest", None)
                    and provider.get("backend_unavailable_reason")
                ):
                    diagnostics.append(
                        {
                            "code": "production_backend_unavailable",
                            "severity": "error",
                            "owner_pack_id": pack_id,
                            "contribution_id": f"pack.{pack_id}.{operation_id}",
                            "message": str(provider["backend_unavailable_reason"]),
                        }
                    )
    return diagnostics


__all__ = ["DefaultspackHTTPPresentation"]
