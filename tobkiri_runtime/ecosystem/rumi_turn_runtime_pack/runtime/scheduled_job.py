"""Fresh captured Calendar job adapter with durable immutable source bindings."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Mapping

from core_runtime.global_contract_dispatch import GlobalContractClient
from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
)
from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
from ecosystem.rumi_turn_runtime_pack.runtime.saved import SAVED_CONTRACTS
from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnConflict
from tobkiri_protocol.canonical import canonical_digest, canonical_json

PACK = "rumi_turn_runtime_pack"
CONTRACT = "tobkiri.action.job.adapter.v2"
FUNCTION = "rumi_turn_runtime_pack.chat-saved-job-adapter"
OPERATION = FUNCTION
ACTION_ID = "chat.saved"
CONSUMED_CONTRACTS = SAVED_CONTRACTS | frozenset(
    {
        "tobkiri.resource.conversation.v1",
        "tobkiri.resource.chat.reference.v1",
        "tobkiri.resource.tool.definition.v1",
        "tobkiri.action.conversation.manage.v1",
        "tobkiri.resource.ai.model.profile.v1",
        "tobkiri.resource.workspace.v1",
    }
)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")


class ScheduledSourceLedger:
    """Persist original source objects and actual receipts, never authority."""

    def __init__(self, root: Path, profile_id: str) -> None:
        self.path = (
            root
            / "packs"
            / PACK
            / "profiles"
            / profile_id
            / "scheduled-chat-jobs.sqlite3"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs (job_key TEXT PRIMARY KEY, digest TEXT NOT NULL, receipt TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS destinations (job_key TEXT PRIMARY KEY, intent_digest TEXT NOT NULL, target_id TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS sources (job_key TEXT NOT NULL, turn_id TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY(job_key, turn_id))"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def admit(self, key: str, envelope: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Reserve immutable dispatch identity or return its saved actual receipt."""
        digest = canonical_digest(dict(envelope))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT digest, receipt FROM jobs WHERE job_key=?", (key,)
            ).fetchone()
            if old is not None:
                if old[0] != digest:
                    raise PermissionError("scheduled job envelope changed")
                return json.loads(old[1]) if old[1] is not None else None
            db.execute("INSERT INTO jobs(job_key,digest) VALUES (?,?)", (key, digest))
        return None

    def recover_source(self, key: str, turn_id: str) -> Mapping[str, Any] | None:
        """Recover the first source before reading a newer conversation revision."""
        with self._connect() as db:
            row = db.execute(
                "SELECT source FROM sources WHERE job_key=? AND turn_id=?",
                (key, turn_id),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def bind_source(self, key: str, source: Mapping[str, Any]) -> Mapping[str, Any]:
        """Commit source identity before saved dispatch; reject any retry rebind."""
        request = source.get("request", source)
        turn_id = request.get("turn_id") if isinstance(request, Mapping) else None
        if not isinstance(turn_id, str) or _ID.fullmatch(turn_id) is None:
            raise ValueError("scheduled source turn identity is invalid")
        encoded = canonical_json(dict(source))
        if len(encoded) > 128 * 1024:
            raise ValueError("scheduled source exceeds its bounded size")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute("SELECT 1 FROM jobs WHERE job_key=?", (key,)).fetchone()
                is None
            ):
                raise PermissionError("scheduled dispatch is not admitted")
            row = db.execute(
                "SELECT source FROM sources WHERE job_key=? AND turn_id=?",
                (key, turn_id),
            ).fetchone()
            if row is not None:
                if canonical_digest(json.loads(row[0])) != canonical_digest(
                    dict(source)
                ):
                    raise PermissionError("scheduled original source changed")
                return json.loads(row[0])
            db.execute(
                "INSERT INTO sources VALUES (?,?,?)",
                (key, turn_id, encoded.decode("utf-8")),
            )
        return json.loads(encoded)

    def has_source(self, key: str) -> bool:
        """Report only whether original saved work was durably bound."""
        with self._connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM sources WHERE job_key=? LIMIT 1", (key,)
                ).fetchone()
                is not None
            )

    def bind_destination(self, key: str, intent: Mapping[str, Any], target: str) -> str:
        """Bind a per-occurrence destination before any canonical owner mutation."""
        digest = canonical_digest(dict(intent))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT intent_digest,target_id FROM destinations WHERE job_key=?",
                (key,),
            ).fetchone()
            if row is not None:
                if row[0] != digest or row[1] != target:
                    raise PermissionError("scheduled destination intent changed")
                return row[1]
            db.execute("INSERT INTO destinations VALUES (?,?,?)", (key, digest, target))
        return target

    def finish(self, key: str, receipt: Mapping[str, Any]) -> Mapping[str, Any]:
        """Retain only an actual completed result; ambiguous work stays recoverable."""
        encoded = canonical_json(dict(receipt))
        if len(encoded) > 128 * 1024:
            raise ValueError("scheduled receipt exceeds its bounded size")
        with self._connect() as db:
            if (
                db.execute(
                    "UPDATE jobs SET receipt=? WHERE job_key=?",
                    (encoded.decode("utf-8"), key),
                ).rowcount
                != 1
            ):
                raise PermissionError("scheduled job admission is unavailable")
        return json.loads(encoded)


def prepare_calendar_destination(
    task: Mapping[str, Any],
    key: str,
    ledger: ScheduledSourceLedger,
    client: Any,
    profile_id: str,
    guard: Any,
    store: Any = None,
    schedule_id: str | None = None,
) -> dict[str, Any]:
    """Apply model intent and create one durable destination per occurrence."""
    if (
        not isinstance(task, Mapping)
        or set(task)
        - {
            "model",
            "chat_references",
            "tool_selection",
            "action_approval_mode",
            "workspace_id",
        }
        != {"message", "profile_id", "conversation_id"}
        or task["profile_id"] != profile_id
    ):
        raise ValueError("Calendar intent fields are invalid")
    target = task["conversation_id"]
    if target is not None and (
        not isinstance(target, str) or _ID.fullmatch(target) is None
    ):
        raise ValueError("Calendar destination identity is invalid")
    if (
        not isinstance(task["message"], str)
        or not task["message"].strip()
        or len(task["message"].encode("utf-8")) > 61440
    ):
        raise ValueError("Calendar message is invalid")
    model = task.get("model")
    if model is not None and (
        not isinstance(model, str) or not model or len(model) > 256
    ):
        raise ValueError("Calendar model intent is invalid")
    if target is None and model is None:
        raise ValueError("Calendar new destination requires a model")
    workspace = task.get("workspace_id")
    if workspace is not None and (
        not isinstance(workspace, str) or _ID.fullmatch(workspace) is None
    ):
        raise ValueError("Calendar workspace identity is invalid")
    mode = task.get("action_approval_mode", "ask")
    if mode not in {"ask", "agent", "full"}:
        raise ValueError("Calendar approval mode is invalid")
    if target is None and mode in {"agent", "full"} and workspace is None:
        raise PermissionError(
            "Calendar autonomous new destination requires a workspace"
        )
    if workspace is not None:
        guard()
        mount = client.invoke(
            "tobkiri.resource.workspace.v1",
            "rumi_workspace_mount_pack.workspace-resource",
            {
                "profile_id": profile_id,
                "operation": "binding",
                "workspace_id": workspace,
            },
        )
        record = mount.get("mount") if isinstance(mount, Mapping) else None
        binding = mount.get("binding") if isinstance(mount, Mapping) else None
        if (
            not isinstance(record, Mapping)
            or record.get("id") != workspace
            or not isinstance(binding, Mapping)
            or binding.get("workspace_id") != workspace
            or type(binding.get("mount_revision")) is not int
            or binding["mount_revision"] < 1
            or binding["mount_revision"] != record.get("mount_revision")
            or any(
                type(binding.get(key)) is not int or binding[key] < 0
                for key in ("root_st_dev", "root_st_ino")
            )
        ):
            raise PermissionError("Calendar registered workspace is unavailable")
    target = target or "calendar:" + canonical_digest([profile_id, key]).removeprefix(
        "sha256:"
    )
    # Pure saved ABI validation precedes owner writes, including reference/profile claims.
    from ecosystem.rumi_turn_runtime_pack.runtime.scheduled_saved import (
        normalize_scheduled_saved_task,
    )

    normalized = normalize_scheduled_saved_task(
        {
            key: value
            for key, value in task.items()
            if key not in {"model", "workspace_id"}
        }
        | {"conversation_id": target},
        profile_id=profile_id,
    )
    target = ledger.bind_destination(key, task, target)
    if ledger.has_source(key):
        return normalized
    if store is not None:
        recovery = store.recover_calendar_preparation(
            turn_id=_calendar_turn_id(profile_id, schedule_id, key),
            occurrence_key=key,
            source=dict(task),
        )
        if recovery is not None and recovery.get("status") in {"bound", "started"}:
            payload = recovery.get("payload")
            if not isinstance(payload, Mapping):
                raise RuntimeError("Calendar bound preparation payload is unavailable")
            ledger.bind_source(key, payload)
            return normalized
    refs = normalized.get("chat_references", [])
    if refs:
        guard()
        snapshot = client.invoke(
            "tobkiri.resource.chat.reference.v1",
            "rumi_conversation_store_pack.chat-reference-read",
            {
                "profile_id": profile_id,
                "operation": "resolve",
                "references": [{"kind": ref["kind"], "id": ref["id"]} for ref in refs],
            },
        )
        from tobkiri_protocol.saved_conversation import (
            validate_resolved_chat_references,
        )

        resolved_refs = validate_resolved_chat_references(snapshot, refs)
        if resolved_refs.get("profile_id") != profile_id or (
            resolved_refs.get("expires_at", 0) <= time.time_ns() // 1_000_000
        ):
            raise ValueError("Calendar reference snapshot is invalid or expired")
    if (
        model
        and hasattr(client, "providers")
        and client.providers("tobkiri.resource.ai.model.profile.v1")
    ):
        guard()
        resolved = client.invoke(
            "tobkiri.resource.ai.model.profile.v1",
            "rumi_model_registry_pack.model-profile-resource.generate",
            {"identifier": model},
        )
        if not isinstance(resolved, Mapping) or not isinstance(
            resolved.get("profile"), Mapping
        ):
            raise ValueError("Calendar model is unavailable")
    selection = normalized.get("tool_selection")
    if selection is not None:
        guard()
        client.invoke(
            "tobkiri.resource.tool.definition.v1",
            "rumi_tool_registry_pack.tool-definition-resource",
            {"profile_id": profile_id, "operation": "select", "selection": selection},
        )
    guard()
    resource = "tobkiri.resource.conversation.v1"
    read_op = "rumi_conversation_store_pack.conversation-resource"
    manage = "tobkiri.action.conversation.manage.v1"
    write_op = "rumi_conversation_store_pack.conversation-manage"
    if task["conversation_id"] is None:
        snapshot = client.invoke(
            resource, read_op, {"profile_id": profile_id, "operation": "list"}
        )
        conversations = (
            snapshot.get("conversations") if isinstance(snapshot, Mapping) else None
        )
        if (
            not isinstance(conversations, (list, tuple))
            or type(snapshot.get("revision")) is not int
        ):
            raise ValueError("Calendar conversation snapshot is invalid")
        record = next(
            (item for item in conversations if item.get("id") == target), None
        )
        if store is not None:
            _reserve_destination(
                store,
                schedule_id,
                key,
                task,
                target,
                record.get("conversation_revision", 0) if record else 0,
            )
        if record is None:
            guard()
            record = client.invoke(
                manage,
                write_op,
                {
                    "profile_id": profile_id,
                    "operation": "create",
                    "expected_revision": snapshot["revision"],
                    "conversation": {
                        "id": target,
                        "title": "Scheduled chat",
                        "model_reference": model or "",
                        "metadata": {
                            "calendar_occurrence": key,
                            **({"workspace_id": workspace} if workspace else {}),
                        },
                    },
                },
            )
    else:
        response = client.invoke(
            resource,
            read_op,
            {"profile_id": profile_id, "operation": "get", "conversation_id": target},
        )
        record = response.get("conversation") if isinstance(response, Mapping) else None
    if isinstance(record, Mapping) and isinstance(record.get("conversation"), Mapping):
        record = record["conversation"]
    if not isinstance(record, Mapping) or record.get("id") != target:
        raise ValueError("Calendar destination is unavailable")
    if workspace is not None:
        metadata = record.get("metadata")
        associated = (
            metadata.get("workspace_id") if isinstance(metadata, Mapping) else None
        )
        if associated != workspace:
            raise PermissionError("Calendar destination workspace differs from intent")
    if task["conversation_id"] is not None and store is not None:
        _reserve_destination(
            store, schedule_id, key, task, target, record["conversation_revision"]
        )
    if model and record.get("model_reference") != model:
        guard()
        record = client.invoke(
            manage,
            write_op,
            {
                "profile_id": profile_id,
                "operation": "update",
                "conversation_id": target,
                "expected_conversation_revision": record["conversation_revision"],
                "patch": {"model_reference": model},
            },
        )
        if isinstance(record, Mapping) and isinstance(
            record.get("conversation"), Mapping
        ):
            record = record["conversation"]
        if not isinstance(record, Mapping) or record.get("model_reference") != model:
            raise RuntimeError("Calendar model intent was not applied")
    return normalized


def _calendar_turn_id(profile: str, schedule: str, key: str) -> str:
    return "calendar:" + canonical_digest([profile, schedule, key]).removeprefix(
        "sha256:"
    )


def _reserve_destination(
    store: Any,
    schedule: str | None,
    key: str,
    task: Mapping[str, Any],
    target: str,
    revision: int,
) -> Mapping[str, Any]:
    if not schedule:
        raise ValueError("Calendar reservation requires schedule identity")
    return store.reserve_calendar_preparation(
        occurrence_key=key,
        source=dict(task),
        turn_id=_calendar_turn_id(task["profile_id"], schedule, key),
        conversation_id=target,
        conversation_revision=revision,
    )


def cancel_scheduled_task(
    *,
    store: Any,
    ledger: ScheduledSourceLedger,
    key: str,
    values: Mapping[str, Any],
    envelope: Mapping[str, Any],
    invocation: Any,
) -> Mapping[str, Any]:
    """Stop only exact owned work and persist cancellation after verified drain."""
    turn_id = _calendar_turn_id(values["profile_id"], values["schedule_id"], key)
    invocation.assert_current()
    released = store.release_calendar_preparation(
        turn_id=turn_id,
        occurrence_key=key,
        source=dict(values["payload"]),
    )
    record = store.get(turn_id)
    if released.get("status") == "released":
        if not isinstance(record, Mapping) or record.get("status") != "cancelled":
            raise RuntimeError("Calendar queued release receipt is unavailable")
        return ledger.finish(
            key,
            {
                "status": "cancelled",
                "idempotency_key": key,
                "turn_id": turn_id,
                "turn_revision": record["revision"],
            },
        )
    pending = {
        "status": "cancellation_pending",
        "idempotency_key": key,
        "turn_id": turn_id,
    }
    source = ledger.recover_source(key, turn_id)
    if source is None or not isinstance(record, Mapping):
        return pending
    if record.get("status") == "cancelled":
        return ledger.finish(
            key,
            {
                "status": "cancelled",
                "idempotency_key": key,
                "turn_id": turn_id,
                "turn_revision": record["revision"],
            },
        )
    if record.get("status") in {"completed", "failed"}:
        return {**pending, "status": "reconciliation_required"}

    def before_signal() -> None:
        invocation.assert_current()
        accepted = store.saved_input(source)
        current = store.get(turn_id)
        if (
            accepted is None
            or not isinstance(current, Mapping)
            or current.get("input_digest") != canonical_digest(accepted)
            or current.get("conversation_id") != source["request"]["conversation_id"]
        ):
            raise PermissionError("Calendar accepted source is unavailable or changed")
        # Owner transaction verifies this immutable saved turn and records intent.
        store.request_saved_cancellation(turn_id)
        invocation.assert_current()

    try:
        port = invocation.scheduled_job_cancellation
        observation = port.request(
            envelope, turn_id=turn_id, before_signal=before_signal
        )
    except (AttributeError, PermissionError, KeyError, TurnConflict):
        invocation.assert_current()
        return pending
    if not observation.wait_for_verified_drain(time.monotonic() + 10):
        invocation.assert_current()
        return pending
    invocation.assert_current()
    try:
        confirmed = store.confirm_saved_cancellation(turn_id)
    except (KeyError, TurnConflict):
        return {**pending, "status": "reconciliation_required"}
    if confirmed.get("status") != "cancelled":
        raise RuntimeError("Calendar cancellation owner ACK is unavailable")
    return ledger.finish(
        key,
        {
            "status": "cancelled",
            "idempotency_key": key,
            "turn_id": turn_id,
            "turn_revision": confirmed["revision"],
        },
    )


def _saved_execution_client(
    client: GlobalContractClient,
    profile_id: str,
) -> GlobalContractClient:
    """Narrow the same authenticated session without rebinding Host capture."""
    if (
        not isinstance(client, GlobalContractClient)
        or client.consumer_pack_id != PACK
        or client.session.profile_id != profile_id
        or not SAVED_CONTRACTS <= client.allowed_contract_ids
        or client.host_credential_transport is not None
    ):
        raise PermissionError("Calendar saved client owner differs from capture")
    return GlobalContractClient(
        session=client.session,
        allowed_contract_ids=SAVED_CONTRACTS,
        consumer_pack_id=PACK,
        host_credential_transport=None,
        host_local_model_transport=None,
    )


def execute_scheduled_task(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
    """Delegate to the canonical saved-task helper using this fresh invocation."""
    from ecosystem.rumi_turn_runtime_pack.runtime.scheduled_saved import (
        execute_scheduled_saved_task,
    )

    return execute_scheduled_saved_task(*args, **kwargs)


class ScheduledSavedJobFactory:
    """Capture one finite job adapter, never a retained timer HTTP session."""

    function_id = FUNCTION
    cancellation_group = "saved-turn"
    cancellation_role = "execute"

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Bind the selected Profile, root, implementation and exact job operation."""
        if (
            context.user_data_root is None
            or not context.profile_id
            or len(context.provider_bindings) != 1
        ):
            raise PermissionError("scheduled job capture is unavailable")
        binding = context.provider_bindings[0]
        if (
            binding.function.function_id != FUNCTION
            or binding.operation.contract_id != CONTRACT
            or binding.operation.operation_id != OPERATION
            or binding.operation.contract_version != "2.0.0"
        ):
            raise PermissionError("scheduled job binding changed")
        domain = context.domain_ids.get(
            (CONTRACT, OPERATION, binding.principal_ref.value)
        )
        if not domain:
            raise PermissionError("scheduled job domain is unavailable")

        def invoke(
            operation_id: str, payload: Mapping[str, Any], invocation: Any
        ) -> Mapping[str, Any]:
            invocation.assert_current()
            values = {
                key: value for key, value in payload.items() if key != "_session_id"
            }
            if (
                operation_id != OPERATION
                or values.get("profile_id") != context.profile_id
            ):
                raise PermissionError("scheduled job Profile differs from capture")
            operation = values.get("operation")
            if operation == "describe":
                if set(values) != {"profile_id", "operation"}:
                    raise ValueError("scheduled describe fields are invalid")
                return {"action_ids": [ACTION_ID]}
            if (
                set(values)
                != {
                    "profile_id",
                    "operation",
                    "action_id",
                    "payload",
                    "idempotency_key",
                    "schedule_id",
                    "lease_id",
                }
                or values["action_id"] != ACTION_ID
            ):
                raise ValueError("scheduled job fields are invalid")
            for field in ("idempotency_key", "schedule_id", "lease_id"):
                if (
                    not isinstance(values[field], str)
                    or _ID.fullmatch(values[field]) is None
                ):
                    raise ValueError("scheduled job identity is invalid")
            if not isinstance(values["payload"], Mapping):
                raise ValueError("scheduled task payload is invalid")
            parent = invocation.parent_invocation
            if parent is None or (
                parent.envelope.contract_id,
                parent.envelope.operation_id,
            ) != (
                "tobkiri.action.job.v1",
                "rumi_job_action_broker_pack.job-action-broker",
            ):
                raise PermissionError(
                    "scheduled job requires canonical broker ancestry"
                )
            parent.assert_current()
            ledger = ScheduledSourceLedger(
                Path(context.user_data_root), context.profile_id
            )
            key = values["idempotency_key"]
            envelope = {
                key: value for key, value in values.items() if key != "operation"
            }
            known = ledger.admit(key, envelope)
            if known is not None and known.get("status") in {
                "completed",
                "failed",
                "cancelled",
            }:
                return known
            if operation == "cancel":
                store = DurableTurnRuntime(
                    context.profile_id, user_data_root=context.user_data_root
                )
                return cancel_scheduled_task(
                    store=store,
                    ledger=ledger,
                    key=key,
                    values=values,
                    envelope=envelope,
                    invocation=invocation,
                )
            if operation == "status" and not ledger.has_source(key):
                return {"status": "reconciliation_required", "idempotency_key": key}
            if operation not in {"dispatch", "status"}:
                raise ValueError("scheduled job operation is invalid")
            client = invocation.contract_client(
                allowed_contract_ids=CONSUMED_CONTRACTS,
                consumer_pack_id=PACK,
                include_credentials=False,
            )
            saved_client = _saved_execution_client(client, context.profile_id)
            store = DurableTurnRuntime(
                context.profile_id, user_data_root=context.user_data_root
            )
            try:
                task = prepare_calendar_destination(
                    dict(values["payload"]),
                    key,
                    ledger,
                    client,
                    context.profile_id,
                    invocation.assert_current,
                    store,
                    values["schedule_id"],
                )
                result = execute_scheduled_task(
                    task,
                    values["schedule_id"],
                    key,
                    client=saved_client,
                    store=store,
                    guard=invocation.assert_current,
                    recover_source=lambda turn: ledger.recover_source(key, turn),
                    bind_source=lambda source: ledger.bind_source(
                        key,
                        store.bind_calendar_preparation(
                            occurrence_key=key,
                            source=dict(values["payload"]),
                            payload=source,
                        ),
                    ),
                    track_execution=invocation.cancellation.track,
                )
            except BaseException:
                store.release_calendar_preparation(
                    turn_id=_calendar_turn_id(
                        context.profile_id, values["schedule_id"], key
                    ),
                    occurrence_key=key,
                    source=dict(values["payload"]),
                )
                raise
            invocation.assert_current()
            if not isinstance(result, Mapping):
                raise RuntimeError("scheduled saved result is unavailable")
            return ledger.finish(key, result)

        return CapturedHostProviderV4(
            (
                HostProviderContributionV4(
                    contract_id=CONTRACT,
                    contract_version="2.0.0",
                    operation_id=OPERATION,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain,
                    invoke=invoke,
                ),
            ),
            lambda: None,
        )


HOST_PROVIDER_FACTORY = {FUNCTION: ScheduledSavedJobFactory()}
