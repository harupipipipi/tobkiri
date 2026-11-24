"""Thin Host adapter for interactive approval-gated command operations.

The adapter is deliberately not an executor.  It owns only a durable mapping
from a command-protocol invocation id to the Host interactive-effect id and
uses the authenticated nested contract client to reach the single Host
coordinator operation.  Process, Git, approval, Grant, scope, and pending
effect lifecycle ownership all remain outside this Pack.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from tobkiri_host.broker import RequestEnvelope
from tobkiri_protocol.canonical import canonical_digest

_PACK_ID = "rumi_command_protocol_pack"
_FUNCTION_ID = "rumi_command_protocol_pack.high-risk-command.service"
_CONTRACT_ID = "tobkiri.service.command.high-risk.v1"
_OPERATION_ID = "high_risk_command.manage"
_COORDINATOR_CONTRACT_ID = "tobkiri.service.interactive-effect.v1"
_COORDINATOR_OPERATION_ID = "interactive_effect.manage"

_COMMAND_EFFECT_KINDS = {
    "terminal": "shell_execute",
    "commit": "git_commit",
    "push": "git_push",
    "patch": "git_apply_patch",
    "restore": "git_restore",
}
_MANAGE_PHASES = frozenset({"resume", "status", "cancel"})
_EFFECT_STATES = frozenset(
    {
        "prepared",
        "approval_pending",
        "approved",
        "claimed",
        "dispatched",
        "succeeded",
        "failed",
        "stale",
        "ambiguous",
        "cancelled",
        # These are local, conservative sentinels.  They never imply that an
        # effect was cancelled or executed; a coordinator result overwrites
        # them once it is known.
        "preparing",
        "resuming",
    }
)
_TERMINAL_OR_FENCED_STATES = frozenset(
    {"succeeded", "failed", "stale", "ambiguous", "cancelled"}
)
_RESUME_FENCED_STATES = _TERMINAL_OR_FENCED_STATES | frozenset(
    {"claimed", "dispatched"}
)
_PRUNABLE_TERMINAL_STATES = frozenset({"succeeded", "failed", "stale", "cancelled"})
_RESERVED_CLIENT_FIELDS = frozenset(
    {
        "approval",
        "approval_id",
        "approval_token",
        "approved",
        "authority_receipt",
        "authority_token",
        "backend",
        "backend_id",
        "effect_id",
        "grant",
        "grant_id",
        "plan",
        "principal",
        "principal_id",
        "profile_id",
        "provider",
        "provider_id",
        "receipt",
        "scope",
        "target_backend",
        "target_contract",
        "target_domain",
        "target_operation",
        "target_principal",
        "token",
    }
)
_MAX_INVOCATION_ID_LENGTH = 160
_MAX_ARGUMENT_BYTES = 512 * 1024
_MAX_PRESENTATION_BYTES = 1024
_MAX_METADATA_ENTRIES = 16
_MAX_METADATA_TEXT = 512
_MAX_STORED_ROWS = 4096
_INVOCATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


class HighRiskCommandUnavailable(PermissionError):
    """Fail-closed public error for an unavailable command adapter."""


class HighRiskCommandBusy(HighRiskCommandUnavailable):
    """Retryable fail-fast result while a nested Broker call is in flight."""

    retryable = True


@dataclass(frozen=True)
class _Owner:
    """Authenticated durable ownership tuple for one command invocation."""

    principal_id: str
    session_id: str
    profile_id: str


@dataclass(frozen=True)
class _StoredInvocation:
    """Secret-free adapter row reconstructed from the Host SQLite store."""

    owner: _Owner
    invocation_id: str
    request_fingerprint: str
    revision: int
    result: Mapping[str, Any]


class _InvocationStore:
    """WAL-backed Host-owned mapping without command payload persistence."""

    def __init__(self, state_root: Path) -> None:
        self._state_root = state_root.resolve(strict=True)
        if not self._state_root.is_dir():
            raise HighRiskCommandUnavailable("command adapter is unavailable")
        self._identity = _directory_identity(self._state_root)
        self._database = self._state_root / "high-risk-command-v4.sqlite3"
        self._lock = threading.RLock()
        self._closed = False
        self._initialize()

    def close(self) -> None:
        """Fence the capture-scoped state store permanently."""

        with self._lock:
            self._closed = True

    def reserve_prepare(
        self,
        owner: _Owner,
        invocation_id: str,
        request_fingerprint: str,
    ) -> tuple[bool, _StoredInvocation]:
        """Reserve exactly one prepare call or return an idempotent result."""

        with self._transaction() as connection:
            row = _select_row(connection, owner, invocation_id)
            if row is not None:
                stored = _row_to_stored(row)
                if stored.request_fingerprint != request_fingerprint:
                    raise HighRiskCommandUnavailable("command invocation is unavailable")
                return False, stored
            count = connection.execute(
                "SELECT COUNT(*) FROM command_invocations"
            ).fetchone()[0]
            if int(count) >= _MAX_STORED_ROWS:
                self._prune_terminal_rows(connection, int(count) - _MAX_STORED_ROWS + 1)
                count = connection.execute(
                    "SELECT COUNT(*) FROM command_invocations"
                ).fetchone()[0]
                if int(count) >= _MAX_STORED_ROWS:
                    raise HighRiskCommandUnavailable(
                        "command invocation capacity is unavailable"
                    )
            result = _local_result(invocation_id, "preparing")
            now = time.time()
            connection.execute(
                """
                INSERT INTO command_invocations (
                    owner_principal, owner_session, profile_id, invocation_id,
                    request_fingerprint, revision, safe_result_json, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner.principal_id,
                    owner.session_id,
                    owner.profile_id,
                    invocation_id,
                    request_fingerprint,
                    1,
                    _canonical_json(result),
                    now,
                    now,
                ),
            )
            return True, _StoredInvocation(
                owner=owner,
                invocation_id=invocation_id,
                request_fingerprint=request_fingerprint,
                revision=1,
                result=result,
            )

    def load(self, owner: _Owner, invocation_id: str) -> _StoredInvocation:
        """Load only the exact authenticated owner tuple."""

        with self._transaction() as connection:
            row = _select_row(connection, owner, invocation_id)
            if row is None:
                raise HighRiskCommandUnavailable("command invocation is unavailable")
            return _row_to_stored(row)

    def list_pending_for_owner(self, owner: _Owner) -> tuple[_StoredInvocation, ...]:
        """Return only nonterminal secret-free rows for one owner."""

        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT owner_principal, owner_session, profile_id, invocation_id,
                       request_fingerprint, revision, safe_result_json
                  FROM command_invocations
                 WHERE owner_principal = ? AND owner_session = ? AND profile_id = ?
                 ORDER BY updated_at DESC, invocation_id ASC
                 LIMIT ?
                """,
                (owner.principal_id, owner.session_id, owner.profile_id, _MAX_STORED_ROWS),
            ).fetchall()
            return tuple(
                item
                for item in (_row_to_stored(row) for row in rows)
                if item.result["state"] not in _TERMINAL_OR_FENCED_STATES
            )

    def replace_result(
        self,
        stored: _StoredInvocation,
        result: Mapping[str, Any],
    ) -> _StoredInvocation:
        """CAS-persist one secret-free coordinator projection."""

        validated = _validate_safe_result(result, stored.invocation_id)
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE command_invocations
                   SET revision = ?, safe_result_json = ?, updated_at = ?
                 WHERE owner_principal = ? AND owner_session = ? AND profile_id = ?
                   AND invocation_id = ? AND revision = ?
                """,
                (
                    stored.revision + 1,
                    _canonical_json(validated),
                    time.time(),
                    stored.owner.principal_id,
                    stored.owner.session_id,
                    stored.owner.profile_id,
                    stored.invocation_id,
                    stored.revision,
                ),
            )
            if cursor.rowcount != 1:
                raise HighRiskCommandUnavailable("command invocation is unavailable")
        return _StoredInvocation(
            owner=stored.owner,
            invocation_id=stored.invocation_id,
            request_fingerprint=stored.request_fingerprint,
            revision=stored.revision + 1,
            result=validated,
        )

    def _initialize(self) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS command_invocations (
                    owner_principal TEXT NOT NULL,
                    owner_session TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    invocation_id TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    safe_result_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (owner_principal, owner_session, profile_id, invocation_id)
                )
                """
            )

    @staticmethod
    def _prune_terminal_rows(connection: sqlite3.Connection, required: int) -> None:
        """Prune only oldest resolved rows; retain ambiguous future effects."""

        rows = connection.execute(
            """
            SELECT owner_principal, owner_session, profile_id, invocation_id,
                   request_fingerprint, revision, safe_result_json
              FROM command_invocations
             ORDER BY updated_at ASC, invocation_id ASC
             LIMIT ?
            """,
            (max(required * 4, 64),),
        ).fetchall()
        deleted = 0
        for row in rows:
            if deleted >= required:
                break
            stored = _row_to_stored(row)
            if stored.result["state"] not in _PRUNABLE_TERMINAL_STATES:
                continue
            cursor = connection.execute(
                """
                DELETE FROM command_invocations
                 WHERE owner_principal = ? AND owner_session = ? AND profile_id = ?
                   AND invocation_id = ? AND revision = ?
                """,
                (
                    stored.owner.principal_id,
                    stored.owner.session_id,
                    stored.owner.profile_id,
                    stored.invocation_id,
                    stored.revision,
                ),
            )
            deleted += int(cursor.rowcount)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS command_invocations_owner_updated
                    ON command_invocations (owner_principal, owner_session, profile_id,
                                            updated_at DESC)
                """
            )

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._assert_open()
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
                _fsync_sqlite(self._database, self._state_root)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        self._assert_open()
        if self._database.exists() and self._database.is_symlink():
            raise HighRiskCommandUnavailable("command adapter state is unavailable")
        connection = sqlite3.connect(
            str(self._database),
            timeout=5.0,
            isolation_level=None,
        )
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _assert_open(self) -> None:
        if self._closed or _directory_identity(self._state_root) != self._identity:
            raise HighRiskCommandUnavailable("command adapter is unavailable")


class HighRiskCommandAdapterV4:
    """One-operation Host adapter over the interactive-effect coordinator."""

    def __init__(
        self,
        *,
        capture: HostProviderCaptureContextV4,
        binding: object,
    ) -> None:
        self._profile_id = capture.profile_id
        self._plan_digest = capture.plan_digest
        self._security_epoch = capture.security_epoch
        self._activation_id = str(capture.activation.get("activation_id") or "")
        self._activation_digest = canonical_digest(dict(capture.activation))
        self._binding = binding
        self._store = _InvocationStore(capture.state_root)
        self._closed = False
        self._lock = threading.RLock()
        # The coordinator can synchronously enter the active Broker.  Limiting
        # mutating nested calls to one prevents all outer Broker workers from
        # waiting on inner work at the same time.  It is capture-scoped rather
        # than process-global, so unrelated Profiles remain isolated.
        self._nested_mutation = threading.BoundedSemaphore(1)
        if not self._activation_id:
            self.close()
            raise HighRiskCommandUnavailable("command adapter activation is unavailable")

    def close(self) -> None:
        """Fence all future calls after this captured Host provider closes."""

        with self._lock:
            self._closed = True
            self._store.close()

    def invoke(
        self,
        operation_id: str,
        payload: Mapping[str, Any],
        invocation: HostProviderInvocationContextV4,
    ) -> Mapping[str, Any]:
        """Manage an approval-gated command using only the Host coordinator."""

        envelope = self._authenticated_envelope(operation_id, invocation)
        if not isinstance(payload, Mapping):
            raise HighRiskCommandUnavailable("command payload is unavailable")
        _reject_reserved_fields(payload)
        phase = payload.get("phase")
        owner = _owner_from_envelope(envelope)
        if phase == "prepare":
            _require_exact_keys(
                payload,
                {"phase", "invocation_id", "command_ref", "arguments", "presentation"},
            )
            return self._prepare(owner, payload, invocation)
        if phase in _MANAGE_PHASES:
            _require_exact_keys(payload, {"phase", "invocation_id"})
            return self._manage(owner, str(phase), payload, invocation)
        if phase == "list_pending":
            _require_exact_keys(payload, {"phase"})
            return {
                "invocations": [
                    _client_projection(item.result)
                    for item in self._store.list_pending_for_owner(owner)
                ]
            }
        raise HighRiskCommandUnavailable("command phase is unavailable")

    def _prepare(
        self,
        owner: _Owner,
        payload: Mapping[str, Any],
        invocation: HostProviderInvocationContextV4,
    ) -> Mapping[str, Any]:
        with self._nested_mutation_gate():
            invocation_id = _invocation_id(payload.get("invocation_id"))
            command_ref = payload.get("command_ref")
            if (
                not isinstance(command_ref, str)
                or command_ref not in _COMMAND_EFFECT_KINDS
            ):
                raise HighRiskCommandUnavailable("command reference is unavailable")
            arguments = _bounded_json_mapping(payload.get("arguments"), _MAX_ARGUMENT_BYTES)
            presentation = _presentation_metadata(payload.get("presentation"))
            request_fingerprint = canonical_digest(
                {
                    "command_ref": command_ref,
                    "arguments": arguments,
                    "presentation": presentation,
                }
            )
            reserved, stored = self._store.reserve_prepare(
                owner,
                invocation_id,
                request_fingerprint,
            )
            if not reserved:
                return _client_projection(stored.result)
            try:
                result = self._coordinator_call(
                    invocation,
                    {
                        "phase": "prepare",
                        "effect_kind": _COMMAND_EFFECT_KINDS[command_ref],
                        "request": arguments,
                    },
                )
                return _client_projection(
                    self._store.replace_result(
                        stored,
                        _coordinator_result(result, invocation_id),
                    ).result
                )
            except Exception as exc:
                # A transport error can occur after the coordinator durably created
                # an effect.  Retaining an ambiguous local tombstone prevents an
                # unsafe duplicate prepare after a restart.
                try:
                    self._store.replace_result(
                        stored,
                        _local_result(invocation_id, "ambiguous"),
                    )
                except Exception:
                    pass
                if isinstance(exc, HighRiskCommandUnavailable):
                    raise
                raise HighRiskCommandUnavailable("command prepare is unavailable") from exc

    def _manage(
        self,
        owner: _Owner,
        phase: str,
        payload: Mapping[str, Any],
        invocation: HostProviderInvocationContextV4,
    ) -> Mapping[str, Any]:
        if phase == "resume":
            with self._nested_mutation_gate():
                invocation_id = _invocation_id(payload.get("invocation_id"))
                stored = self._store.load(owner, invocation_id)
                state = str(stored.result.get("state") or "")
                if state in _RESUME_FENCED_STATES:
                    return _client_projection(stored.result)
                if state == "preparing":
                    return _client_projection(stored.result)
                if state == "resuming":
                    return _client_projection(
                        self._resync_resuming(stored, invocation)
                    )
                effect_id = _stored_effect_id(stored)
                claiming = self._store.replace_result(
                    stored,
                    _with_state(stored.result, "resuming"),
                )
                try:
                    result = self._coordinator_call(
                        invocation,
                        {"phase": "resume", "effect_id": effect_id},
                    )
                    return _client_projection(
                        self._store.replace_result(
                            claiming,
                            _coordinator_result(result, invocation_id),
                        ).result
                    )
                except Exception as exc:
                    try:
                        self._store.replace_result(
                            claiming,
                            _with_state(claiming.result, "ambiguous"),
                        )
                    except Exception:
                        pass
                    if isinstance(exc, HighRiskCommandUnavailable):
                        raise
                    raise HighRiskCommandUnavailable("command resume is unavailable") from exc

        invocation_id = _invocation_id(payload.get("invocation_id"))
        stored = self._store.load(owner, invocation_id)
        state = str(stored.result.get("state") or "")
        if phase == "status":
            # Status also synchronously enters the Broker.  It may run in
            # parallel with list_pending, but must share the nested-call gate
            # with prepare/resume/cancel to avoid exhausting outer workers.
            if not self._nested_mutation.acquire(blocking=False):
                return _client_projection(stored.result)
            try:
                stored = self._store.load(owner, invocation_id)
                state = str(stored.result.get("state") or "")
                if state == "preparing":
                    return _client_projection(stored.result)
                if state == "resuming":
                    return _client_projection(self._resync_resuming(stored, invocation))
                result = self._coordinator_call(
                    invocation,
                    {"phase": "status", "effect_id": _stored_effect_id(stored)},
                )
                return _client_projection(
                    self._store.replace_result(
                        stored,
                        _coordinator_result(result, invocation_id),
                    ).result
                )
            finally:
                self._nested_mutation.release()

        with self._nested_mutation_gate():
            stored = self._store.load(owner, invocation_id)
            state = str(stored.result.get("state") or "")
            if state == "resuming":
                # A resumed command could have crossed the dispatch point
                # before a crash.  Synchronize instead of locally claiming it
                # cancelled; the coordinator returns ambiguous if dispatched.
                return _client_projection(self._resync_resuming(stored, invocation))
            if state == "preparing" or state in _TERMINAL_OR_FENCED_STATES:
                return _client_projection(stored.result)
            result = self._coordinator_call(
                invocation,
                {"phase": "cancel", "effect_id": _stored_effect_id(stored)},
            )
            return _client_projection(
                self._store.replace_result(
                    stored,
                    _coordinator_result(result, invocation_id),
                ).result
            )

    def _resync_resuming(
        self,
        stored: _StoredInvocation,
        invocation: HostProviderInvocationContextV4,
    ) -> Mapping[str, Any]:
        """Recover a restart-era local resume marker from Host authority state."""

        result = self._coordinator_call(
            invocation,
            {"phase": "status", "effect_id": _stored_effect_id(stored)},
        )
        return self._store.replace_result(
            stored,
            _coordinator_result(result, stored.invocation_id),
        ).result

    def _coordinator_call(
        self,
        invocation: HostProviderInvocationContextV4,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        client = invocation.contract_client(
            allowed_contract_ids=frozenset({_COORDINATOR_CONTRACT_ID}),
            consumer_pack_id=_PACK_ID,
        )
        result = client.invoke(
            _COORDINATOR_CONTRACT_ID,
            _COORDINATOR_OPERATION_ID,
            dict(payload),
        )
        if not isinstance(result, Mapping):
            raise HighRiskCommandUnavailable("interactive effect result is unavailable")
        return result

    def _authenticated_envelope(
        self,
        operation_id: str,
        invocation: HostProviderInvocationContextV4,
    ) -> RequestEnvelope:
        with self._lock:
            if self._closed:
                raise HighRiskCommandUnavailable("command adapter is closed")
        envelope = invocation.envelope
        binding = self._binding
        principal_id = str(getattr(getattr(binding, "principal_ref", None), "value", ""))
        context = getattr(envelope, "context", None)
        if (
            not isinstance(envelope, RequestEnvelope)
            or operation_id != _OPERATION_ID
            or envelope.contract_id != _CONTRACT_ID
            or envelope.operation_id != _OPERATION_ID
            or envelope.target_principal.value != principal_id
            or context.profile_id != self._profile_id
            or context.activation_id != self._activation_id
            or context.activation_digest != self._activation_digest
            or context.plan_digest != self._plan_digest
            or context.security_epoch != self._security_epoch
        ):
            raise HighRiskCommandUnavailable("command adapter capture binding changed")
        return envelope

    @contextlib.contextmanager
    def _nested_mutation_gate(self) -> Iterator[None]:
        """Fail fast instead of starving the Broker's outer worker pool."""

        if not self._nested_mutation.acquire(blocking=False):
            raise HighRiskCommandBusy("high-risk command adapter is busy")
        try:
            yield
        finally:
            self._nested_mutation.release()


class HighRiskCommandAdapterFactoryV4:
    """Capture exactly the one Host adapter Function and operation."""

    function_id = _FUNCTION_ID

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Create one contribution bound to verified Function identity."""

        bindings = tuple(context.provider_bindings)
        if (
            len(bindings) != 1
            or bindings[0].function.function_id != self.function_id
            or bindings[0].operation.contract_id != _CONTRACT_ID
            or bindings[0].operation.operation_id != _OPERATION_ID
        ):
            raise HighRiskCommandUnavailable("command adapter binding is unavailable")
        binding = bindings[0]
        key = (
            binding.operation.contract_id,
            binding.operation.operation_id,
            binding.principal_ref.value,
        )
        domain_id = context.domain_ids.get(key)
        if not domain_id:
            raise HighRiskCommandUnavailable("command adapter domain is unavailable")
        adapter = HighRiskCommandAdapterV4(capture=context, binding=binding)
        contribution = HostProviderContributionV4(
            contract_id=binding.operation.contract_id,
            contract_version=binding.operation.contract_version,
            operation_id=binding.operation.operation_id,
            principal_id=binding.principal_ref.value,
            artifact_digest=binding.artifact.digest,
            implementation_digest=binding.function.implementation_digest,
            domain_id=domain_id,
            invoke=adapter.invoke,
        )
        return CapturedHostProviderV4((contribution,), adapter.close)


def _owner_from_envelope(envelope: RequestEnvelope) -> _Owner:
    """Extract the only presentation owner identity from Host context."""

    context = envelope.context
    owner = str(getattr(context.caller_principal, "value", ""))
    session = str(getattr(context, "caller_session_id", ""))
    profile = str(getattr(context, "profile_id", ""))
    if not owner or not session or not profile:
        raise HighRiskCommandUnavailable("command owner is unavailable")
    return _Owner(owner, session, profile)


def _require_exact_keys(payload: Mapping[str, Any], expected: set[str]) -> None:
    """Reject extensions that could silently broaden a command authority path."""

    if set(payload) != expected:
        raise HighRiskCommandUnavailable("command payload fields are unavailable")


def _reject_reserved_fields(value: object) -> None:
    """Reject client-provided routing, authority, Grant, and receipt claims."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or key.casefold() in _RESERVED_CLIENT_FIELDS:
                raise HighRiskCommandUnavailable("client command authority is unavailable")
            _reject_reserved_fields(item)
    elif isinstance(value, list):
        for item in value:
            _reject_reserved_fields(item)


def _invocation_id(value: object) -> str:
    """Validate a bounded opaque invocation id without treating it as authority."""

    if not isinstance(value, str) or not _INVOCATION_ID.fullmatch(value):
        raise HighRiskCommandUnavailable("command invocation id is unavailable")
    return value


def _bounded_json_mapping(value: object, max_bytes: int) -> dict[str, Any]:
    """Deep-copy bounded JSON without retaining client-owned references."""

    if not isinstance(value, Mapping):
        raise HighRiskCommandUnavailable("command arguments are unavailable")
    try:
        encoded = json.dumps(
            dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(encoded) > max_bytes:
            raise ValueError("too large")
        parsed = json.loads(encoded.decode("utf-8"))
    except Exception as exc:
        raise HighRiskCommandUnavailable("command arguments are unavailable") from exc
    if not isinstance(parsed, dict):
        raise HighRiskCommandUnavailable("command arguments are unavailable")
    return parsed


def _presentation_metadata(value: object) -> dict[str, str]:
    """Validate bounded non-authoritative UI copy and deliberately discard it.

    The coordinator owns the actual approval wording.  This field remains in
    the request fingerprint so an idempotency key cannot be reused with a
    different user-visible intent, but it is never persisted or forwarded as
    authority-bearing data.
    """

    if not isinstance(value, Mapping) or set(value) - {"title", "summary"}:
        raise HighRiskCommandUnavailable("command presentation is unavailable")
    normalized: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise HighRiskCommandUnavailable("command presentation is unavailable")
        text = item.strip()
        if not text or len(text) > _MAX_METADATA_TEXT:
            raise HighRiskCommandUnavailable("command presentation is unavailable")
        normalized[key] = text
    if not normalized:
        raise HighRiskCommandUnavailable("command presentation is unavailable")
    if len(_canonical_json(normalized).encode("utf-8")) > _MAX_PRESENTATION_BYTES:
        raise HighRiskCommandUnavailable("command presentation is unavailable")
    return normalized


def _coordinator_result(value: Mapping[str, Any], invocation_id: str) -> dict[str, Any]:
    """Project the coordinator response to the only safe adapter result."""

    return _validate_safe_result(
        {
            "invocation_id": invocation_id,
            "effect_id": value.get("effect_id"),
            "approval_request_id": value.get("approval_request_id"),
            "state": value.get("state"),
            "expires_at": value.get("expires_at"),
            "redacted_metadata": value.get("redacted_metadata"),
        },
        invocation_id,
    )


def _local_result(invocation_id: str, state: str) -> dict[str, Any]:
    """Return a conservative no-effect local projection for an uncertain call."""

    return _validate_safe_result(
        {
            "invocation_id": invocation_id,
            "effect_id": None,
            "approval_request_id": None,
            "state": state,
            "expires_at": None,
            "redacted_metadata": {},
        },
        invocation_id,
    )


def _client_projection(result: Mapping[str, Any]) -> dict[str, Any]:
    """Remove the Host-only effect identifier from every Pack response."""

    validated = _validate_safe_result(
        result,
        str(result.get("invocation_id") or ""),
    )
    return {
        "invocation_id": validated["invocation_id"],
        "approval_request_id": validated["approval_request_id"],
        "state": validated["state"],
        "expires_at": validated["expires_at"],
        "redacted_metadata": dict(validated["redacted_metadata"]),
    }


def _with_state(result: Mapping[str, Any], state: str) -> dict[str, Any]:
    """Create a local conservative state without exposing any new data."""

    updated = dict(result)
    updated["state"] = state
    return _validate_safe_result(updated, str(updated.get("invocation_id") or ""))


def _validate_safe_result(value: Mapping[str, Any], invocation_id: str) -> dict[str, Any]:
    """Validate a strict secret-free status projection before SQLite storage."""

    if set(value) != {
        "invocation_id",
        "effect_id",
        "approval_request_id",
        "state",
        "expires_at",
        "redacted_metadata",
    } or value.get("invocation_id") != invocation_id:
        raise HighRiskCommandUnavailable("interactive effect result is unavailable")
    state = value.get("state")
    if not isinstance(state, str) or state not in _EFFECT_STATES:
        raise HighRiskCommandUnavailable("interactive effect state is unavailable")
    effect_id = _optional_identifier(value.get("effect_id"))
    approval_request_id = _optional_identifier(value.get("approval_request_id"))
    expires_at = value.get("expires_at")
    if expires_at is not None and (
        not isinstance(expires_at, (int, float))
        or isinstance(expires_at, bool)
        or not math.isfinite(float(expires_at))
        or float(expires_at) <= 0
    ):
        raise HighRiskCommandUnavailable("interactive effect expiry is unavailable")
    if state not in {"preparing", "ambiguous"} and (
        effect_id is None or approval_request_id is None or expires_at is None
    ):
        raise HighRiskCommandUnavailable("interactive effect identity is unavailable")
    metadata = value.get("redacted_metadata")
    if not isinstance(metadata, Mapping) or len(metadata) > _MAX_METADATA_ENTRIES:
        raise HighRiskCommandUnavailable("interactive effect metadata is unavailable")
    normalized_metadata: dict[str, str] = {}
    for key, item in metadata.items():
        if (
            not isinstance(key, str)
            or not isinstance(item, str)
            or not key
            or len(key) > 64
            or len(item) > _MAX_METADATA_TEXT
            or key.casefold() in _RESERVED_CLIENT_FIELDS
        ):
            raise HighRiskCommandUnavailable("interactive effect metadata is unavailable")
        normalized_metadata[key] = item
    return {
        "invocation_id": invocation_id,
        "effect_id": effect_id,
        "approval_request_id": approval_request_id,
        "state": state,
        # Public command results are journaled with strict I-JSON, which does
        # not permit floating-point values. Rounding up preserves the Host's
        # authoritative expiry without presenting an earlier deadline.
        "expires_at": (
            int(math.ceil(float(expires_at))) if expires_at is not None else None
        ),
        "redacted_metadata": normalized_metadata,
    }


def _optional_identifier(value: object) -> str | None:
    """Accept only bounded opaque Host identifiers in safe result projections."""

    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 255:
        raise HighRiskCommandUnavailable("interactive effect identifier is unavailable")
    return value


def _stored_effect_id(stored: _StoredInvocation) -> str:
    """Read an effect id only from Host-owned durable state."""

    effect_id = stored.result.get("effect_id")
    if not isinstance(effect_id, str) or not effect_id:
        raise HighRiskCommandUnavailable("interactive effect is unavailable")
    return effect_id


def _select_row(
    connection: sqlite3.Connection,
    owner: _Owner,
    invocation_id: str,
) -> sqlite3.Row | tuple[Any, ...] | None:
    """Select one durable row by its complete owner tuple."""

    return connection.execute(
        """
        SELECT owner_principal, owner_session, profile_id, invocation_id,
               request_fingerprint, revision, safe_result_json
          FROM command_invocations
         WHERE owner_principal = ? AND owner_session = ? AND profile_id = ?
           AND invocation_id = ?
        """,
        (owner.principal_id, owner.session_id, owner.profile_id, invocation_id),
    ).fetchone()


def _row_to_stored(row: sqlite3.Row | tuple[Any, ...]) -> _StoredInvocation:
    """Parse one SQLite row without accepting extra or secret result fields."""

    try:
        (
            owner_principal,
            owner_session,
            profile_id,
            invocation_id,
            fingerprint,
            revision,
            encoded_result,
        ) = row
        parsed = json.loads(str(encoded_result))
        if not isinstance(parsed, Mapping):
            raise TypeError("result")
        validated = _validate_safe_result(parsed, str(invocation_id))
        if (
            not isinstance(fingerprint, str)
            or not fingerprint.startswith("sha256:")
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise TypeError("row")
        return _StoredInvocation(
            owner=_Owner(str(owner_principal), str(owner_session), str(profile_id)),
            invocation_id=str(invocation_id),
            request_fingerprint=fingerprint,
            revision=revision,
            result=validated,
        )
    except Exception as exc:
        raise HighRiskCommandUnavailable("command adapter state is unavailable") from exc


def _canonical_json(value: Mapping[str, Any]) -> str:
    """Encode only already-validated JSON values deterministically."""

    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _directory_identity(path: Path) -> tuple[int, int]:
    """Return the stable device/inode identity of a Host state directory."""

    try:
        status = path.stat()
    except OSError as exc:
        raise HighRiskCommandUnavailable("command adapter state is unavailable") from exc
    if not path.is_dir():
        raise HighRiskCommandUnavailable("command adapter state is unavailable")
    return status.st_dev, status.st_ino


def _fsync_sqlite(database: Path, state_root: Path) -> None:
    """Durably flush SQLite database/WAL files and the containing directory."""

    for path in (database, Path(f"{database}-wal")):
        if path.exists():
            flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            if os.name == "nt":
                flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    if os.name == "nt":
        return
    descriptor = os.open(state_root, os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            # macOS may reject directory fsync while the database and WAL have
            # already been synchronously committed; reject every other error.
            if exc.errno not in {22, 45}:
                raise
    finally:
        os.close(descriptor)


HOST_PROVIDER_FACTORY = HighRiskCommandAdapterFactoryV4()


__all__ = [
    "HOST_PROVIDER_FACTORY",
    "HighRiskCommandAdapterFactoryV4",
    "HighRiskCommandAdapterV4",
    "HighRiskCommandBusy",
    "HighRiskCommandUnavailable",
]
