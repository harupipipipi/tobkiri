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
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input

from .model_profile_presentation import (
    MODEL_PROFILE_LIST_TARGET,
    MODEL_PROFILE_SAVE_TARGET,
    normalize_model_profile_save,
    present_model_profile_saved,
    present_model_profiles,
)
from .conversation_list_presentation import (
    CONVERSATION_LIST_TARGET,
    present_conversation_list,
)
from .conversation_create_presentation import (
    CONVERSATION_CREATE_TARGET,
    normalize_conversation_create,
    present_conversation_created,
)
from .conversation_record_presentation import (
    CONVERSATION_RECORD_TARGETS,
    normalize_conversation_record,
    present_conversation_record,
    present_conversation_deleted,
)


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
                isinstance(request_id, str) and str(uuid.UUID(request_id)) == request_id
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
            or body.get("profile_revision") != getattr(session, "profile_revision", None)
            or body.get("activation_id") != getattr(session, "activation_id", None)
            or body.get("plan_hash") != getattr(session, "plan_digest", None)
            or body.get("catalog_hash") != snapshot.catalog_hash
        ):
            return None
        nested = body.get("payload")
        if not isinstance(nested, Mapping) or any(not isinstance(key, str) for key in nested):
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
        """Bind model reads and media paths to the captured Profile."""

        if target.contribution_id == "defaults.providers.configure":
            phase = payload.get("phase")
            if phase == "prepare":
                if (
                    set(payload) != {"phase", "effect_kind", "request"}
                    or payload.get("effect_kind") != "provider_configure"
                    or not isinstance(payload.get("request"), Mapping)
                ):
                    raise ValueError("provider configuration request is invalid")
            elif phase not in {"status", "resume", "cancel"} or set(payload) != {
                "phase", "effect_id",
            }:
                raise ValueError("provider configuration phase is invalid")
            return dict(payload)

        if (
            target.contribution_id, target.contract_id, target.operation_id,
            target.provider_id, target.function_id,
        ) == MODEL_PROFILE_SAVE_TARGET:
            session.assert_current()
            return normalize_model_profile_save(payload)
        if (
            target.contribution_id, target.contract_id, target.operation_id,
            target.provider_id, target.function_id,
        ) == (
            "defaults.conversations.turn.read", "tobkiri.resource.turn.v1",
            "rumi_turn_runtime_pack.turn-resource",
            "rumi_turn_runtime_pack.turn-runtime.resource",
            "rumi_turn_runtime_pack.turn-runtime.resource",
        ):
            turn_id = payload.get("turn_id")
            if set(payload) != {"turn_id"} or not isinstance(turn_id, str) or (
                not turn_id or len(turn_id) > 256 or turn_id.strip() != turn_id
            ):
                raise ValueError("turn read requires one stable turn ID")
            session.assert_current()
            profile_id = str(getattr(session, "profile_id", ""))
            if not profile_id:
                raise ValueError("turn read requires a captured Profile")
            return {"profile_id": profile_id, "operation": "get", "turn_id": turn_id}

        if (
            target.contribution_id, target.contract_id, target.operation_id,
            target.provider_id, target.function_id,
        ) == (
            "defaults.conversations.send", "tobkiri.action.turn.saved.v1",
            "rumi_turn_runtime_pack.turn-saved",
            "rumi_turn_runtime_pack.turn-runtime.saved",
            "rumi_turn_runtime_pack.turn-runtime.saved",
        ):
            return validate_saved_conversation_input(payload)

        if (
            target.contribution_id,
            target.contract_id,
            target.operation_id,
            target.provider_id,
            target.function_id,
        ) == (
            "defaults.ui.preferences.write",
            "tobkiri.action.ui.preferences.v1",
            "tobkiri_ui_settings_pack.preferences-write",
            "tobkiri.ui.preferences.write",
            "tobkiri.ui.preferences.write",
        ):
            session.assert_current()
            profile_id = str(getattr(session, "profile_id", ""))
            if not profile_id or set(payload) != {"changes", "expected_revision"}:
                raise ValueError("preferences write requires captured identity")
            return {**dict(payload), "profile_id": profile_id}
        if (
            target.contribution_id,
            target.contract_id,
            target.operation_id,
            target.provider_id,
            target.function_id,
        ) in {
            (
                "defaults.ui.settings.read",
                "tobkiri.resource.ui.settings.v1",
                "tobkiri_ui_settings_pack.settings-read",
                "tobkiri.ui.settings.read",
                "tobkiri.ui.settings.read",
            ),
            (
                "defaults.ui.catalog.read",
                "tobkiri.resource.ui.settings.v1",
                "tobkiri_ui_settings_pack.catalog-read",
                "tobkiri.ui.catalog.read",
                "tobkiri.ui.catalog.read",
            ),
            (
                "defaults.commands.catalog.read",
                "tobkiri.resource.command.catalog.v1",
                "command.catalog.read",
                "rumi_command_protocol_pack.catalog.read",
                "rumi_command_protocol_pack.catalog.read",
            ),
        }:
            session.assert_current()
            profile_id = str(getattr(session, "profile_id", ""))
            if (
                not profile_id
                or set(payload)
                - ({"full"} if target.contribution_id == "defaults.ui.settings.read" else set())
                or ("full" in payload and payload["full"] not in {True, "true"})
            ):
                raise ValueError("settings read requires captured identity")
            return {"profile_id": profile_id}
        if (
            target.contribution_id,
            target.contract_id,
            target.operation_id,
            target.provider_id,
            target.function_id,
        ) in {MODEL_PROFILE_LIST_TARGET, CONVERSATION_LIST_TARGET}:
            session.assert_current()
            profile_id = str(getattr(session, "profile_id", ""))
            if not profile_id or payload:
                raise ValueError("owner listing requires captured identity")
            return {"profile_id": profile_id, "operation": "list"}
        if (
            target.contribution_id,
            target.contract_id,
            target.operation_id,
            target.provider_id,
            target.function_id,
        ) == CONVERSATION_CREATE_TARGET:
            session.assert_current()
            return normalize_conversation_create(
                payload, profile_id=str(getattr(session, "profile_id", "")),
            )
        record_action = CONVERSATION_RECORD_TARGETS.get((
            target.contribution_id, target.contract_id, target.operation_id,
            target.provider_id, target.function_id,
        ))
        if record_action is not None:
            session.assert_current()
            return normalize_conversation_record(
                record_action, payload, profile_id=str(getattr(session, "profile_id", "")),
            )
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

        if binding.presentation == "model_profile_list":
            return present_model_profiles(result)
        if binding.presentation == "model_profile_saved":
            return present_model_profile_saved(result)
        if binding.presentation == "conversation_list":
            return present_conversation_list(result)
        if binding.presentation == "conversation_created":
            return present_conversation_created(result)
        if binding.presentation == "conversation_record":
            return present_conversation_record(result)
        if binding.presentation == "conversation_deleted":
            return present_conversation_deleted(result)
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
                    and provider.get("activation_id") == getattr(session, "activation_id", None)
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
