"""Invoke the minimal ordinary Command Protocol surface.

This provider deliberately owns only ``/help``.  Approval-gated terminal,
git, and file mutations remain isolated in ``high_risk_adapter.py``.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from tobkiri_protocol.canonical import canonical_digest

FUNCTION_ID = "rumi_command_protocol_pack.command.invoke"
CONTRACT_ID = "tobkiri.action.command.invoke.v1"
OPERATION_ID = "command.invoke"
_COMMAND_REF = "defaultspack:help"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_ALLOWED_FIELDS = frozenset(
    {
        "profile_id",
        "command_ref",
        "args",
        "invocation_id",
        "mode",
        "conversation_id",
        "catalog_revision",
        "idempotency_key",
        "client_sequence",
        "_session_id",
    }
)


def _identity(value: object, field: str) -> str:
    text = value if isinstance(value, str) else ""
    if not _IDENTIFIER.fullmatch(text):
        raise ValueError(f"{field} is invalid")
    return text


def _owner_identity(value: object, field: str) -> str:
    text = value if isinstance(value, str) else ""
    if (
        not text
        or len(text) > 512
        or text.strip() != text
        or any(ord(character) < 0x20 for character in text)
    ):
        raise PermissionError(f"{field} is invalid")
    return text


def _public_result(invocation_id: str) -> dict[str, Any]:
    return {
        "api_version": "tobkiri.commands/v1",
        "operation_id": invocation_id,
        "status": "succeeded",
        "command_ref": _COMMAND_REF,
        "state_changes": [],
        "legacy_result": {
            "command": {
                "id": "help",
                "name": "help",
                "label": "Help",
            },
            "executed": False,
            "requires_approval": False,
            "action": "open_command_help",
            "args": {},
        },
    }


class _InvocationJournal:
    """Store replay-safe ordinary command receipts outside Pack source."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def invoke_once(
        self,
        *,
        profile_id: str,
        owner_principal_id: str,
        owner_session_id: str,
        invocation_id: str,
        fingerprint: str,
        result: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self._path.parent, 0o700)
        except OSError:
            pass
        result_json = json.dumps(
            dict(result), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        connection = sqlite3.connect(self._path, timeout=5.0)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS command_invocations (
                    profile_id TEXT NOT NULL,
                    owner_principal_id TEXT NOT NULL,
                    owner_session_id TEXT NOT NULL,
                    invocation_id TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    PRIMARY KEY (
                        profile_id, owner_principal_id,
                        owner_session_id, invocation_id
                    )
                )
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT request_fingerprint, result_json
                FROM command_invocations
                WHERE profile_id = ? AND owner_principal_id = ?
                  AND owner_session_id = ? AND invocation_id = ?
                """,
                (
                    profile_id,
                    owner_principal_id,
                    owner_session_id,
                    invocation_id,
                ),
            ).fetchone()
            if row is not None:
                if row[0] != fingerprint:
                    raise PermissionError(
                        "command invocation ID was reused with a different request"
                    )
                stored = json.loads(row[1])
                if not isinstance(stored, dict):
                    raise RuntimeError("stored command invocation result is invalid")
                connection.commit()
                return stored
            connection.execute(
                """
                INSERT INTO command_invocations (
                    profile_id, owner_principal_id, owner_session_id,
                    invocation_id, request_fingerprint, result_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    owner_principal_id,
                    owner_session_id,
                    invocation_id,
                    fingerprint,
                    result_json,
                ),
            )
            connection.commit()
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass
            return dict(result)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class CommandInvokeHostFactoryV4:
    """Capture the ordinary command invoker for one exact Profile."""

    function_id = FUNCTION_ID

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Capture without creating the invocation journal."""
        if (
            context.user_data_root is None
            or not context.profile_id
            or len(context.provider_bindings) != 1
        ):
            raise PermissionError("command invocation capture is incomplete")
        binding = context.provider_bindings[0]
        operation = binding.operation
        if (
            binding.function.function_id != FUNCTION_ID
            or operation.contract_id != CONTRACT_ID
            or operation.operation_id != OPERATION_ID
            or operation.contract_version != "1.0.0"
        ):
            raise PermissionError("command invocation binding is invalid")
        domain_id = context.domain_ids.get(
            (CONTRACT_ID, OPERATION_ID, binding.principal_ref.value)
        )
        if domain_id is None:
            raise PermissionError("command invocation domain is unavailable")
        journal = _InvocationJournal(
            context.user_data_root
            / "defaultspack"
            / "shared"
            / "command_protocol"
            / "ordinary_invocations.sqlite3"
        )

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            invocation.assert_current()
            if operation_id != OPERATION_ID or set(payload) - _ALLOWED_FIELDS:
                raise PermissionError("command invocation request is invalid")
            if payload.get("profile_id") != context.profile_id:
                raise PermissionError("command invocation Profile is invalid")
            if payload.get("command_ref") != _COMMAND_REF or payload.get("args") != {}:
                raise PermissionError("ordinary command is not owned by this provider")
            if payload.get("mode") not in {"chat", "coding", "agent"}:
                raise ValueError("command mode is invalid")
            conversation_id = payload.get("conversation_id")
            if conversation_id is not None and (
                not isinstance(conversation_id, str)
                or not conversation_id
                or len(conversation_id) > 256
            ):
                raise ValueError("conversation_id is invalid")
            client_sequence = payload.get("client_sequence")
            if client_sequence is not None and (
                type(client_sequence) is not int or client_sequence < 0
            ):
                raise ValueError("client_sequence is invalid")
            invocation_id = _identity(payload.get("invocation_id"), "invocation_id")
            idempotency_key = payload.get("idempotency_key", invocation_id)
            if _identity(idempotency_key, "idempotency_key") != invocation_id:
                raise ValueError("idempotency_key must equal invocation_id")
            principal_id = _owner_identity(
                invocation.presentation_owner_principal_id,
                "presentation owner principal",
            )
            session_id = _owner_identity(
                invocation.presentation_owner_session_id,
                "presentation owner session",
            )
            request = {
                key: value
                for key, value in payload.items()
                if key != "_session_id"
            }
            fingerprint = canonical_digest(
                {
                    "profile_id": context.profile_id,
                    "owner_principal_id": principal_id,
                    "owner_session_id": session_id,
                    "request": request,
                }
            )
            result = _public_result(invocation_id)
            if client_sequence is not None:
                result["client_sequence"] = client_sequence
            return journal.invoke_once(
                profile_id=context.profile_id,
                owner_principal_id=principal_id,
                owner_session_id=session_id,
                invocation_id=invocation_id,
                fingerprint=fingerprint,
                result=result,
            )

        return CapturedHostProviderV4(
            (
                HostProviderContributionV4(
                    contract_id=CONTRACT_ID,
                    contract_version=operation.contract_version,
                    operation_id=OPERATION_ID,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain_id,
                    invoke=invoke,
                ),
            ),
            lambda: None,
        )


HOST_PROVIDER_FACTORY = {FUNCTION_ID: CommandInvokeHostFactoryV4()}
