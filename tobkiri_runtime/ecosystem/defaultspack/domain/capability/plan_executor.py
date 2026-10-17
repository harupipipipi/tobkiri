"""Single Capability Plan execution boundary (fail-closed).

Every execution entry point (the public capability API, the retired tool
invoke compatibility route, and the pack function tool dispatcher) must pass
through this module.  The executor validates the detached canonical plan,
cross-checks the persisted approval record, consumes the single-use claim,
and only then runs the invocation through the gated ``ToolExecutor``.  Any
failure after the claim is recorded as ``outcome_unknown`` and can never be
retried against the same grant.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import time
from typing import Any

from core_runtime.capability_plan import (
    CapabilityPlanValidationError,
    validate_capability_plan,
)
from domain.capability.models import stable_revision
from domain.capability.repository import (
    CapabilityPlanAlreadyExecuted,
    CapabilityRepository,
    StaleCapabilityPlan,
    _canonical_invocation,
    _digest,
    _redact_for_storage,
)

_OWNER_FIELDS = ("principal_id", "workspace_id", "conversation_id", "profile_id")

_PRE_EFFECT_REJECTION_MARKERS = (
    "rejected_by_policy",
    "rejected_by_security",
    "approval_required",
    "adaptive_policy",
)
_PRE_EFFECT_ERROR_TYPES = {
    "capability_executor_unbound",
    "capability_plan_required",
    "capability_plan_invalid",
    "capability_plan_owner_mismatch",
    "legacy_tool_alias",
    "pack_not_approved",
    "registry_revision_missing",
    "tool_not_attached",
    "tool_schema_revision_mismatch",
    "workspace_binding_invalid",
    "workspace_binding_missing",
    "workspace_binding_mismatch",
}


class CapabilityPlanExecutionFailed(RuntimeError):
    """A claimed execution did not finish successfully.

    ``status`` is the terminal execution status recorded on the plan
    (``failed_pre_effect`` or ``outcome_unknown``) and ``result`` carries the
    gated tool result that produced the failure, when available.
    """

    def __init__(
        self,
        message: str,
        *,
        status: str,
        result: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.result = result

    @property
    def error_code(self) -> str:
        """Map the failure to a stable public error code."""

        if self.status == "outcome_unknown":
            return "OUTCOME_UNKNOWN"
        if isinstance(self.result, Mapping):
            error_type = str(self.result.get("error_type") or "").strip()
            if error_type:
                return error_type.upper()
            if self.result.get("approval_required"):
                return "APPROVAL_REQUIRED"
        return "TOOL_REJECTED"


def execute(
    plan: Mapping[str, Any],
    approval: Mapping[str, Any],
    invocation: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
    *,
    owner: Mapping[str, Any] | None = None,
    repository: CapabilityRepository | None = None,
    tool_executor: Any | None = None,
) -> dict[str, Any]:
    """Consume one approved Capability Plan and run its exact invocation.

    The claim is consumed before any tool dispatch begins; a later failure is
    journaled as ``outcome_unknown`` (or ``failed_pre_effect`` when the gated
    executor explicitly rejected the call before any effect) and raised as
    ``CapabilityPlanExecutionFailed`` so callers can never silently retry.

    Returns the completed repository record on success.  Raises
    ``KeyError``/``PermissionError``/``StaleCapabilityPlan``/
    ``CapabilityPlanAlreadyExecuted``/``CapabilityPlanValidationError`` for
    pre-claim failures and ``CapabilityPlanExecutionFailed`` after the claim.
    """

    repo = repository if repository is not None else CapabilityRepository()
    owner_scope = _normalize_owner(owner)
    supplied_id = (
        str(plan.get("plan_id") or "").strip()
        if isinstance(plan, Mapping)
        else ""
    )
    record = repo.get_plan(supplied_id, owner=owner_scope, require_owner=True)
    if record is None:
        # Preserve pre-claim failure semantics: malformed plans surface the
        # canonical validation error, unknown well-formed plans surface
        # KeyError/NOT_FOUND.
        validate_capability_plan(plan)
        raise KeyError(supplied_id)
    stored_plan = record.get("plan")
    canonical_plan = _execution_plan(plan, stored_plan)
    plan_id = str(canonical_plan.get("plan_id") or "").strip()
    stored_approval = record.get("approval")
    if not isinstance(stored_approval, dict):
        raise PermissionError("Capability Plan is not approved")
    if _json_safe(dict(approval)) != stored_approval:
        raise StaleCapabilityPlan("Capability Plan approval record changed")
    if stored_approval.get("plan_digest") != record.get("plan_digest"):
        raise StaleCapabilityPlan("Capability Plan digest changed")
    if stored_approval.get("generation") != record.get("generation"):
        raise StaleCapabilityPlan("Capability Plan generation changed")
    safe_invocation = _canonical_invocation(invocation)
    if stored_approval.get("invocation_digest") != stable_revision(
        safe_invocation
    ):
        raise StaleCapabilityPlan("approved invocation changed")

    repo.claim_execution(
        plan_id,
        {"at": _utc_timestamp(), "status": "running"},
        owner=owner_scope,
        invocation=safe_invocation,
    )

    # Everything after the durable claim lives inside one guarded region: any
    # escape (context setup, executor construction, dispatch, nested executor
    # failure types, or the final completion write) must journal
    # outcome_unknown instead of stranding the record in 'executing'.
    journaled = False
    try:
        tool_id = str(safe_invocation.get("tool_id") or "").strip()
        arguments = safe_invocation.get("arguments")
        executor = (
            tool_executor if tool_executor is not None else _tool_executor()
        )
        execution_context = dict(context or {})
        # Inject the persisted (storage-redacted) record form, never the
        # secret-bearing caller original, so secret-shaped values cannot
        # reach tool context.  ``capability_plan_digest`` re-binds the record
        # for the dispatch-time authority gate (see _is_record_bound_plan).
        execution_context["capability_plan"] = dict(stored_plan)
        execution_context["capability_plan_digest"] = str(
            record.get("plan_digest") or ""
        )
        execution_context["capability_plan_owner"] = dict(owner_scope)
        for field in _OWNER_FIELDS:
            execution_context[field] = owner_scope[field]
        result = executor.execute(
            tool_id,
            arguments if isinstance(arguments, dict) else {},
            execution_context,
        )
        if not isinstance(result, dict) or result.get("is_error"):
            status = (
                "failed_pre_effect"
                if _is_pre_effect_rejection(result)
                else "outcome_unknown"
            )
            _complete(
                repo,
                plan_id,
                owner_scope,
                {
                    "at": _utc_timestamp(),
                    "status": status,
                    "result": _json_safe(result),
                },
            )
            journaled = True
            raise CapabilityPlanExecutionFailed(
                _failure_message(result),
                status=status,
                result=result,
            )
        return repo.complete_execution(
            plan_id,
            {
                "at": _utc_timestamp(),
                "status": "succeeded",
                "result": _json_safe(result),
            },
            owner=owner_scope,
        )
    except BaseException as exc:
        if not journaled:
            _complete(
                repo,
                plan_id,
                owner_scope,
                {
                    "at": _utc_timestamp(),
                    "status": "outcome_unknown",
                    "error": str(exc),
                },
            )
        if isinstance(exc, CapabilityPlanExecutionFailed):
            raise
        if isinstance(exc, Exception):
            raise CapabilityPlanExecutionFailed(
                "Capability Plan execution outcome is unknown; "
                "automatic retry is forbidden",
                status="outcome_unknown",
            ) from exc
        raise


def validate_tool_plan_authority(
    tool_name: Any,
    tool_def: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
    *,
    require_plan: bool = False,
) -> dict[str, Any] | None:
    """Fail closed unless a canonical plan attaches this exact Tool.

    The public Capability API owns persisted approval and owner binding.  The
    executor still validates the detached plan at the last non-core boundary,
    so a direct adapter call cannot use a legacy alias or an unsigned plan to
    reach a reviewed pack function.  Returns a tool-result rejection payload
    or ``None`` when the plan authorizes this tool.
    """

    if not isinstance(context, Mapping):
        if not require_plan:
            return None
        return _required_plan_rejection()
    plan = context.get("capability_plan")
    if not isinstance(plan, dict):
        if not require_plan:
            return None
        return _required_plan_rejection()
    try:
        plan = validate_capability_plan(plan)
    except (CapabilityPlanValidationError, TypeError, ValueError):
        if not _is_record_bound_plan(plan, context):
            return {
                "result": "CapabilityPlan authority is invalid",
                "is_error": True,
                "widget": None,
                "error_type": "capability_plan_invalid",
            }
        plan = dict(plan)
    tools = plan.get("tools")
    if not isinstance(tools, dict):
        return {
            "result": "CapabilityPlan Tool authority is invalid",
            "is_error": True,
            "widget": None,
            "error_type": "capability_plan_invalid",
        }
    attached = {
        str(item).strip()
        for item in tools.get("attached") or []
        if str(item or "").strip()
    }
    definition = tool_def if isinstance(tool_def, Mapping) else {}
    canonical_name = str(
        definition.get("tool_id") or definition.get("name") or tool_name or ""
    ).strip()
    requested_name = str(tool_name or "").strip()
    if requested_name != canonical_name:
        return {
            "result": "Legacy Tool aliases cannot authorize execution",
            "is_error": True,
            "widget": None,
            "error_type": "legacy_tool_alias",
        }
    if canonical_name not in attached:
        return {
            "result": "Tool is not attached by the active CapabilityPlan",
            "is_error": True,
            "widget": None,
            "error_type": "tool_not_attached",
        }
    schema_hashes = tools.get("schema_hashes")
    expected_hash = (
        str(schema_hashes.get(canonical_name) or "").strip()
        if isinstance(schema_hashes, dict)
        else ""
    )
    schema = definition.get("schema")
    if not isinstance(schema, dict):
        contract = definition.get("contract")
        schema = (
            contract.get("input_schema")
            if isinstance(contract, dict)
            and isinstance(contract.get("input_schema"), dict)
            else {}
        )
    actual_hash = hashlib.sha256(
        json.dumps(
            schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    if not expected_hash or expected_hash != actual_hash:
        return {
            "result": "Tool schema does not match the active CapabilityPlan",
            "is_error": True,
            "widget": None,
            "error_type": "tool_schema_revision_mismatch",
        }
    plan_owner = plan.get("owner") or plan.get("authority_owner")
    if plan_owner is not None:
        if not isinstance(plan_owner, dict):
            return {
                "result": "CapabilityPlan owner binding is invalid",
                "is_error": True,
                "widget": None,
                "error_type": "capability_plan_owner_mismatch",
            }
        context_owner = context.get("capability_plan_owner")
        if not isinstance(context_owner, dict):
            context_owner = context
        for field in _OWNER_FIELDS:
            expected = str(plan_owner.get(field) or "").strip()
            actual = str(context_owner.get(field) or "").strip()
            if expected and actual != expected:
                return {
                    "result": "CapabilityPlan owner does not match "
                    "execution scope",
                    "is_error": True,
                    "widget": None,
                    "error_type": "capability_plan_owner_mismatch",
                }
    return None


def _execution_plan(
    plan: Mapping[str, Any],
    stored_plan: Any,
) -> dict[str, Any]:
    """Resolve the authoritative plan form for this execution.

    Callers may present either the original signed plan or the persisted
    storage-redacted record (``put_plan`` stores ``_redact_for_storage(plan)``,
    so secret-shaped keys and oversized strings never survive storage).  The
    original is bound by its embedded digest and must redact to the stored
    record; the stored echo is authoritative only when it is byte-identical to
    the persisted record, because its digest field no longer recomputes over
    the redacted payload.
    """

    try:
        canonical_plan = validate_capability_plan(plan)
    except CapabilityPlanValidationError:
        if (
            isinstance(plan, Mapping)
            and isinstance(stored_plan, dict)
            and _json_safe(dict(plan)) == stored_plan
        ):
            return dict(stored_plan)
        raise
    if not isinstance(stored_plan, dict) or stored_plan != _redact_for_storage(
        canonical_plan
    ):
        raise StaleCapabilityPlan("Capability Plan payload changed")
    return canonical_plan


def _is_record_bound_plan(plan: Any, context: Mapping[str, Any]) -> bool:
    """Bind a storage-redacted stored record to its persisted digest.

    The canonical executor injects ``capability_plan_digest`` (the
    repository's ``plan_digest`` column, computed over the stored payload at
    ``put_plan`` time) after it has verified the record.  A digest match is a
    SHA-256 preimage check: the presented mapping must be exactly the
    persisted record whose secrets were redacted at storage time.  The key is
    populated only by the in-process executor — transport contexts are built
    server-side — so callers cannot mint a matching digest for a record they
    did not receive through the repository.
    """

    digest = context.get("capability_plan_digest")
    return (
        isinstance(plan, dict)
        and isinstance(digest, str)
        and bool(digest)
        and _digest(_json_safe(dict(plan))) == digest
    )


def _required_plan_rejection() -> dict[str, Any]:
    """Return the uniform rejection for a missing canonical plan."""

    return {
        "result": "CapabilityPlan is required for tool execution",
        "is_error": True,
        "widget": None,
        "error_type": "capability_plan_required",
    }


def _normalize_owner(owner: Mapping[str, Any] | None) -> dict[str, str]:
    """Bind execution to the approved four-field authority scope."""

    source = owner if isinstance(owner, Mapping) else {}
    return {
        "principal_id": str(
            source.get("principal_id") or source.get("user_id") or "local-user"
        ),
        "workspace_id": str(source.get("workspace_id") or "local-workspace"),
        "conversation_id": str(
            source.get("conversation_id") or "local-conversation"
        ),
        "profile_id": str(source.get("profile_id") or "default"),
    }


def _tool_executor() -> Any:
    """Resolve the gated tool executor lazily to avoid an import cycle."""

    from domain.tool.executor import ToolExecutor

    return ToolExecutor()


def _complete(
    repo: CapabilityRepository,
    plan_id: str,
    owner_scope: dict[str, str],
    execution: dict[str, Any],
) -> None:
    """Finalize a claimed execution; completion failure is also unknown.

    Best-effort by design: every write error (stale plan, consumed claim,
    backend failure) is swallowed because the claim is already consumed and
    the caller surface is always outcome_unknown.  Retrying the write must
    never reopen replay or mask the original failure.
    """

    try:
        repo.complete_execution(plan_id, execution, owner=owner_scope)
    except Exception:
        pass


def _is_pre_effect_rejection(result: Any) -> bool:
    """Return True when the gated executor rejected before any effect."""

    if not isinstance(result, dict):
        return False
    if any(result.get(marker) for marker in _PRE_EFFECT_REJECTION_MARKERS):
        return True
    return str(result.get("error_type") or "") in _PRE_EFFECT_ERROR_TYPES


def _failure_message(result: Any) -> str:
    """Describe a failed gated execution without leaking internals."""

    if isinstance(result, dict):
        message = str(result.get("result") or "").strip()
        if message:
            return message
    return "Capability Plan execution was rejected"


def _json_safe(value: Any) -> Any:
    """Round-trip a result through JSON so stored records stay canonical."""

    try:
        return json.loads(
            json.dumps(value, ensure_ascii=False, default=str)
        )
    except (TypeError, ValueError):
        return str(value)


def _utc_timestamp() -> str:
    """Return an ISO-8601 UTC timestamp for execution journal entries."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


__all__ = [
    "CapabilityPlanExecutionFailed",
    "CapabilityPlanAlreadyExecuted",
    "StaleCapabilityPlan",
    "execute",
    "validate_tool_plan_authority",
]
