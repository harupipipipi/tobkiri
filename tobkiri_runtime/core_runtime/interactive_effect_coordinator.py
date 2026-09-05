"""Host coordinator for one approval-gated existing Provider invocation.

This module intentionally adds no executor, virtual machine, or Provider
implementation.  It selects signed prepare/execute edges, stores the Broker's
existing prepared snapshot through ``PendingEffectController``, and resumes it
through the same Broker after the Host-owned approval has been settled.
"""

from __future__ import annotations

import json
import re
import secrets
import shlex
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from core_runtime.authority.v4 import AuthorityScope
from tobkiri_host.broker import PreparedInvocation, RequestBroker
from tobkiri_host.interactive_effects import (
    PendingEffectController,
    PendingEffectStatus,
)
from tobkiri_host.models import InvocationFrame, OpaqueAuthorityRef, RequestContext
from tobkiri_host.ports import (
    InteractiveEffectOwnerQuery,
    InteractiveEffectPort,
    InteractiveEffectPrepareCommand,
    InteractiveEffectStatus,
)
from tobkiri_protocol.canonical import canonical_digest


class InteractiveEffectUnavailable(PermissionError):
    """Fail-closed public error for unavailable interactive future effects."""


INTERACTIVE_EFFECT_COORDINATOR_CONTRACT_ID = "tobkiri.service.interactive-effect.v1"
INTERACTIVE_EFFECT_COORDINATOR_OPERATION_ID = "interactive_effect.manage"


@dataclass(frozen=True)
class InteractiveEffectSpec:
    """One finite UI kind and its signed prepare/execute operations."""

    kind: str
    prepare_contract_id: str
    prepare_operation_id: str
    execute_contract_id: str
    execute_operation_id: str


INTERACTIVE_EFFECT_SPECS: Mapping[str, InteractiveEffectSpec] = {
    "shell_execute": InteractiveEffectSpec(
        kind="shell_execute",
        prepare_contract_id="tobkiri.service.shell.execute.v1",
        prepare_operation_id="rumi_shell_execute_pack.shell-prepare",
        execute_contract_id="tobkiri.service.shell.execute.v1",
        execute_operation_id="rumi_shell_execute_pack.shell-execute",
    ),
    "git_commit": InteractiveEffectSpec(
        kind="git_commit",
        prepare_contract_id="tobkiri.service.git.write.v1",
        prepare_operation_id="rumi_git_write_pack.git-commit-prepare",
        execute_contract_id="tobkiri.service.git.write.v1",
        execute_operation_id="rumi_git_write_pack.git-commit",
    ),
    "git_restore": InteractiveEffectSpec(
        kind="git_restore",
        prepare_contract_id="tobkiri.service.git.write.v1",
        prepare_operation_id="rumi_git_write_pack.git-restore-prepare",
        execute_contract_id="tobkiri.service.git.write.v1",
        execute_operation_id="rumi_git_write_pack.git-restore",
    ),
    "git_apply_patch": InteractiveEffectSpec(
        kind="git_apply_patch",
        prepare_contract_id="tobkiri.service.git.write.v1",
        prepare_operation_id="rumi_git_write_pack.git-apply-patch-prepare",
        execute_contract_id="tobkiri.service.git.write.v1",
        execute_operation_id="rumi_git_write_pack.git-apply-patch",
    ),
    "git_push": InteractiveEffectSpec(
        kind="git_push",
        prepare_contract_id="tobkiri.service.git.publish.v1",
        prepare_operation_id="rumi_git_publish_pack.git-push-prepare",
        execute_contract_id="tobkiri.service.git.publish.v1",
        execute_operation_id="rumi_git_publish_pack.git-push",
    ),
}


@dataclass(frozen=True)
class CapturedInteractiveEffectRoute:
    """Host-captured signed path for one finite interactive effect kind."""

    spec: InteractiveEffectSpec
    coordinator_principal: OpaqueAuthorityRef
    execute_target_principal: OpaqueAuthorityRef
    execute_ceiling: AuthorityScope


class HostInteractiveEffectService(InteractiveEffectPort):
    """Owner-bound coordinator over a finite captured Profile edge set."""

    _EXPIRY_SECONDS = 300.0
    _MAX_REQUEST_BYTES = 1024 * 1024
    _FORBIDDEN_AUTHORITY_FIELDS = frozenset(
        {
            "approved",
            "approval",
            "approval_id",
            "approval_token",
            "authority_receipt",
            "authority_token",
            "client_token",
            "domain",
            "domain_id",
            "grant",
            "grant_id",
            "backend",
            "backend_id",
            "principal",
            "principal_id",
            "provider",
            "provider_id",
            "publisher",
            "publisher_lineage",
            "presentation",
            "redacted_metadata",
            "receipt",
            "scope",
            "token",
            "target",
            "target_principal",
        }
    )

    def __init__(
        self,
        *,
        broker: RequestBroker,
        controller: PendingEffectController,
        routes: tuple[CapturedInteractiveEffectRoute, ...],
        context_for_execute: Callable[[CapturedInteractiveEffectRoute, RequestContext], RequestContext],
        assert_current_capture: Callable[[], None],
        profile_id: str,
        activation_id: str,
        plan_digest: str,
        security_epoch: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not routes:
            raise InteractiveEffectUnavailable("interactive effect is unavailable")
        route_map = {route.spec.kind: route for route in routes}
        if len(route_map) != len(routes):
            raise InteractiveEffectUnavailable("interactive effect is unavailable")
        coordinator_ids = {route.coordinator_principal.value for route in routes}
        if len(coordinator_ids) != 1:
            raise InteractiveEffectUnavailable("interactive effect is unavailable")
        for route in routes:
            expected = INTERACTIVE_EFFECT_SPECS.get(route.spec.kind)
            if expected != route.spec:
                raise InteractiveEffectUnavailable("interactive effect is unavailable")
        self._broker = broker
        self._controller = controller
        self._routes = route_map
        self._coordinator_principal = OpaqueAuthorityRef(next(iter(coordinator_ids)))
        self._context_for_execute = context_for_execute
        self._assert_current_capture = assert_current_capture
        self._profile_id = profile_id
        self._activation_id = activation_id
        self._plan_digest = plan_digest
        self._security_epoch = security_epoch
        self._clock = clock

    def prepare_interactive_effect(
        self,
        command: InteractiveEffectPrepareCommand,
    ) -> InteractiveEffectStatus:
        """Persist an execute-only Broker snapshot after a signed prepare edge."""

        try:
            self._assert_current_capture()
            self._validate_outer_context(command.context, command.coordinator_principal)
            self._validate_presentation_owner(
                command.presentation_owner_principal_id,
                command.presentation_owner_session_id,
            )
            route = self._route(command.effect_kind)
            request = _json_mapping(command.payload, self._MAX_REQUEST_BYTES)
            prepared_result = _json_mapping(
                command.prepared_result,
                self._MAX_REQUEST_BYTES,
            )
            _reject_authority_fields(request, self._FORBIDDEN_AUTHORITY_FIELDS)
            execute_payload = _execute_payload(route.spec, request, prepared_result)
            execute_context = self._context_for_execute(route, command.context)
            self._validate_execute_context(execute_context, route)
            prepared = self._broker.prepare(
                InvocationFrame(
                    contract_id=route.spec.execute_contract_id,
                    version_range=None,
                    operation_id=route.spec.execute_operation_id,
                    payload=execute_payload,
                ),
                execute_context,
            )
            if (
                prepared.binding.operation.contract_id
                != route.spec.execute_contract_id
                or prepared.binding.operation.operation_id
                != route.spec.execute_operation_id
                or prepared.binding.principal_ref != route.execute_target_principal
            ):
                raise InteractiveEffectUnavailable("interactive effect is unavailable")
            effect_scope = _effect_scope(
                route.execute_ceiling,
                request_digest=prepared.request_digest,
                invocation_owner_id=_invocation_owner_id(command.context),
                caller_session_id=execute_context.caller_session_id,
                plan_digest=execute_context.plan_digest,
            )
            pending = self._controller.prepare(
                prepared=prepared,
                context=execute_context,
                effect_scope=effect_scope.to_dict(),
                invocation_owner_id=effect_scope.dimensions["invocation_owner_id"][0],
                presentation_owner_principal_id=(
                    command.presentation_owner_principal_id
                ),
                presentation_owner_session_id=command.presentation_owner_session_id,
                # This is deliberately derived after Broker.prepare.  The UI
                # therefore describes the same frozen execute payload and
                # request digest which become the durable PendingEffect, not
                # the browser's optional presentation copy.
                presentation_metadata=_presentation_metadata(route.spec, prepared),
                expires_at=self._clock() + self._EXPIRY_SECONDS,
                typed_confirmation_phrase="EXECUTE",
            )
            return _port_status(pending)
        except InteractiveEffectUnavailable:
            raise
        except Exception as exc:
            raise InteractiveEffectUnavailable("interactive effect is unavailable") from exc

    def get_interactive_effect(
        self,
        query: InteractiveEffectOwnerQuery,
    ) -> InteractiveEffectStatus:
        """Return a redacted status only to its saved presentation owner."""

        try:
            self._assert_current_capture()
            self._validate_outer_context(query.context, query.coordinator_principal)
            self._validate_presentation_owner(
                query.presentation_owner_principal_id,
                query.presentation_owner_session_id,
            )
            return _port_status(
                self._controller.status_for_presentation(
                    effect_id=_effect_id(query.effect_id),
                    presentation_owner_principal_id=(
                        query.presentation_owner_principal_id
                    ),
                    presentation_owner_session_id=query.presentation_owner_session_id,
                )
            )
        except Exception as exc:
            raise InteractiveEffectUnavailable("interactive effect is unavailable") from exc

    def resume_interactive_effect(
        self,
        query: InteractiveEffectOwnerQuery,
    ) -> InteractiveEffectStatus:
        """Claim and resume an approved effect through the single existing Broker."""

        try:
            self._assert_current_capture()
            self._validate_outer_context(query.context, query.coordinator_principal)
            self._validate_presentation_owner(
                query.presentation_owner_principal_id,
                query.presentation_owner_session_id,
            )
            return _port_status(
                self._controller.resume_for_presentation(
                    effect_id=_effect_id(query.effect_id),
                    presentation_owner_principal_id=(
                        query.presentation_owner_principal_id
                    ),
                    presentation_owner_session_id=query.presentation_owner_session_id,
                    broker=self._broker,
                )
            )
        except Exception as exc:
            raise InteractiveEffectUnavailable("interactive effect is unavailable") from exc

    def cancel_interactive_effect(
        self,
        query: InteractiveEffectOwnerQuery,
    ) -> InteractiveEffectStatus:
        """Cancel an owned effect; post-dispatch cancellation is ambiguous."""

        try:
            self._assert_current_capture()
            self._validate_outer_context(query.context, query.coordinator_principal)
            self._validate_presentation_owner(
                query.presentation_owner_principal_id,
                query.presentation_owner_session_id,
            )
            return _port_status(
                self._controller.cancel_for_presentation(
                    effect_id=_effect_id(query.effect_id),
                    presentation_owner_principal_id=(
                        query.presentation_owner_principal_id
                    ),
                    presentation_owner_session_id=query.presentation_owner_session_id,
                )
            )
        except Exception as exc:
            raise InteractiveEffectUnavailable("interactive effect is unavailable") from exc

    def _route(self, effect_kind: str) -> CapturedInteractiveEffectRoute:
        if not isinstance(effect_kind, str):
            raise InteractiveEffectUnavailable("interactive effect is unavailable")
        route = self._routes.get(effect_kind)
        if route is None:
            raise InteractiveEffectUnavailable("interactive effect is unavailable")
        return route

    def _validate_outer_context(
        self,
        context: RequestContext,
        coordinator_principal: OpaqueAuthorityRef,
    ) -> None:
        if (
            not isinstance(context, RequestContext)
            or coordinator_principal != self._coordinator_principal
            or context.profile_id != self._profile_id
            or context.activation_id != self._activation_id
            or context.plan_digest != self._plan_digest
            or context.security_epoch != self._security_epoch
            or not context.caller_session_id
        ):
            raise InteractiveEffectUnavailable("interactive effect is unavailable")

    @staticmethod
    def _validate_presentation_owner(principal_id: str, session_id: str) -> None:
        """Reject missing Host-origin ownership without exposing its values."""

        if not principal_id or not session_id:
            raise InteractiveEffectUnavailable("interactive effect is unavailable")

    @staticmethod
    def _validate_execute_context(
        context: RequestContext,
        route: CapturedInteractiveEffectRoute,
    ) -> None:
        if (
            context.caller_principal != route.coordinator_principal
            or context.plan_digest == ""
            or context.delegation_chain
        ):
            raise InteractiveEffectUnavailable("interactive effect is unavailable")


def _execute_payload(
    spec: InteractiveEffectSpec,
    request: Mapping[str, Any],
    prepared_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Turn a Provider-produced prepare result into one fixed execute payload."""

    if spec.kind == "shell_execute":
        plan = prepared_result.get("redacted_plan")
        digest = prepared_result.get("plan_digest")
        if (
            prepared_result.get("executed") is not False
            or not isinstance(plan, Mapping)
            or not _is_digest(digest)
        ):
            raise InteractiveEffectUnavailable("interactive effect is unavailable")
        return {
            "redacted_plan": _json_mapping(plan, HostInteractiveEffectService._MAX_REQUEST_BYTES),
            "plan_digest": digest,
            "arguments": dict(request),
        }
    if spec.kind in {"git_commit", "git_restore"}:
        plan = _sealed_git_plan(prepared_result, spec.prepare_operation_id)
        return _git_execute_payload(plan)
    if spec.kind == "git_apply_patch":
        patch = request.get("patch")
        if not isinstance(patch, str):
            raise InteractiveEffectUnavailable("interactive effect is unavailable")
        return {
            **_git_execute_payload(
                _sealed_git_plan(prepared_result, spec.prepare_operation_id)
            ),
            "patch": patch,
        }
    if spec.kind == "git_push":
        plan = prepared_result.get("plan")
        digest = prepared_result.get("plan_digest")
        if not isinstance(plan, Mapping) or not _is_digest(digest):
            raise InteractiveEffectUnavailable("interactive effect is unavailable")
        frozen_plan = _json_mapping(plan, HostInteractiveEffectService._MAX_REQUEST_BYTES)
        if canonical_digest(frozen_plan) != digest:
            raise InteractiveEffectUnavailable("interactive effect is unavailable")
        return {"plan": frozen_plan, "plan_digest": digest}
    raise InteractiveEffectUnavailable("interactive effect is unavailable")


def _sealed_git_plan(
    prepared_result: Mapping[str, Any],
    expected_operation: str,
) -> dict[str, Any]:
    """Validate a Git prepare response before preserving it as execute input."""

    plan = _json_mapping(prepared_result, HostInteractiveEffectService._MAX_REQUEST_BYTES)
    digest = plan.get("plan_digest")
    operation_owner, separator, operation_name = expected_operation.rpartition(".")
    if (
        not separator
        or not operation_owner
        or not operation_name.endswith("-prepare")
    ):
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    expected_plan_operation = operation_name.removesuffix("-prepare")
    if (
        not _is_digest(digest)
        or plan.get("plan_version") != "tobkiri.git-write.plan.v4"
        or plan.get("operation") != expected_plan_operation
    ):
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    canonical = dict(plan)
    supplied_digest = str(canonical.pop("plan_digest"))
    if canonical_digest(canonical) != supplied_digest:
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    return plan


def _git_execute_payload(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Carry only the profile/workspace bindings required by Git execution."""

    profile_id = plan.get("profile_id")
    workspace_id = plan.get("workspace_id")
    if not isinstance(profile_id, str) or not isinstance(workspace_id, str):
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    return {
        "plan": dict(plan),
        "profile_id": profile_id,
        "workspace_id": workspace_id,
    }


def _effect_scope(
    ceiling: AuthorityScope,
    *,
    request_digest: str,
    invocation_owner_id: str,
    caller_session_id: str,
    plan_digest: str,
) -> AuthorityScope:
    """Add exact future-invocation dimensions without widening a signed ceiling."""

    dimensions = dict(ceiling.dimensions)
    dimensions.update(
        {
            "invocation_owner_id": (invocation_owner_id,),
            "caller_session_id": (caller_session_id,),
            "plan_digest": (plan_digest,),
        }
    )
    scope = AuthorityScope(
        capability=ceiling.capability,
        semantics_digest=ceiling.semantics_digest,
        dimensions=dimensions,
        quotas=dict(ceiling.quotas),
        exact_request_digest=request_digest,
        opaque=ceiling.opaque,
    )
    if not scope.is_subset_of(ceiling):
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    return scope


_MAX_PRESENTATION_ITEMS = 48
_MAX_PRESENTATION_TEXT = 1_600
_MAX_PRESENTATION_VALUE = 512
_REDACTED = "[REDACTED]"
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|authorization|credential|password|secret|token)"
    r"\b\s*(?:=|:)\s*(?:bearer\s+)?[^\s,;]+"
)
_URL_CREDENTIAL = re.compile(r"(://[^/\s:@]+:)[^@/\s]+@")
_OPAQUE_SECRET = re.compile(
    r"(?i)^(?:gh[pousr]_[a-z0-9_]+|sk-[a-z0-9_-]+|pk-[a-z0-9_-]+|"
    r"xox[baprs]-[a-z0-9-]+|akia[0-9a-z]{16})$"
)
_SECRET_ARGUMENT_NAMES = frozenset(
    {
        "api-key",
        "apikey",
        "authorization",
        "credential",
        "cookie",
        "header",
        "password",
        "secret",
        "token",
    }
)


def _presentation_metadata(
    spec: InteractiveEffectSpec,
    prepared: PreparedInvocation,
) -> Mapping[str, str]:
    """Build bounded, redacted approval copy from one Host-prepared effect.

    ``PreparedInvocation`` is the Host's immutable pre-dispatch snapshot.  Its
    request digest, normalized execute payload, and the eventual approval
    request all participate in the durable PendingEffect/approval bindings.
    This function intentionally never accepts browser presentation text.
    """

    if not _is_digest(prepared.request_digest):
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    payload = _json_mapping(
        prepared.normalized_payload,
        HostInteractiveEffectService._MAX_REQUEST_BYTES,
    )
    if spec.kind == "shell_execute":
        return _shell_presentation(payload)
    if spec.kind == "git_commit":
        return _git_commit_presentation(payload, spec)
    if spec.kind == "git_restore":
        return _git_restore_presentation(payload, spec)
    if spec.kind == "git_apply_patch":
        return _git_patch_presentation(payload, spec)
    if spec.kind == "git_push":
        return _git_push_presentation(payload)
    raise InteractiveEffectUnavailable("interactive effect is unavailable")


def _shell_presentation(payload: Mapping[str, Any]) -> Mapping[str, str]:
    """Describe only the safe command and working directory for a terminal run."""

    arguments = payload.get("arguments")
    redacted_plan = payload.get("redacted_plan")
    if (
        not isinstance(arguments, Mapping)
        or not isinstance(redacted_plan, Mapping)
        or not _is_digest(payload.get("plan_digest"))
    ):
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    argv = _terminal_argv(arguments.get("command"))
    cwd = _display_text(_required_text(arguments.get("cwd") or "."))
    return _presentation(
        action="Run local terminal command",
        summary="Run the prepared local terminal command.",
        detail=f"argv: {_display_argv(argv)}\ncwd: {cwd}",
    )


def _git_commit_presentation(
    payload: Mapping[str, Any],
    spec: InteractiveEffectSpec,
) -> Mapping[str, str]:
    """Describe a sealed commit message and its prepared repository scope."""

    plan = _prepared_git_plan(payload, spec)
    message = _display_text(_required_text(plan.get("message")))
    repository = _display_text(_required_text(plan.get("repository_root")))
    branch = _display_text(_required_text(plan.get("expected_head_ref")))
    index_tree = _display_git_object_id(plan.get("expected_index_tree"))
    return _presentation(
        action="Create Git commit",
        summary="Create the prepared local Git commit.",
        detail=(
            f"message: {message}\nrepository: {repository}\nbranch: {branch}\n"
            f"staged tree: {index_tree}"
        ),
    )


def _git_restore_presentation(
    payload: Mapping[str, Any],
    spec: InteractiveEffectSpec,
) -> Mapping[str, str]:
    """Describe the sealed working-tree restore paths and file-mode effects."""

    plan = _prepared_git_plan(payload, spec)
    paths = _prepared_paths(plan.get("paths"))
    source_tree = _display_text(_required_text(plan.get("source_tree")))
    modes = _restore_modes(plan.get("targets"))
    return _presentation(
        action="Restore Git paths",
        summary=f"Restore {len(paths)} prepared working-tree path(s).",
        detail=(
            f"paths: {_display_paths(paths)}\n"
            f"restore mode: working tree from prepared tree {source_tree}\n"
            f"file modes: {modes}"
        ),
    )


def _git_patch_presentation(
    payload: Mapping[str, Any],
    spec: InteractiveEffectSpec,
) -> Mapping[str, str]:
    """Describe the sealed patch only by digest and affected paths."""

    plan = _prepared_git_plan(payload, spec)
    paths = _prepared_paths(plan.get("paths"))
    patch_digest = _required_sha256_hex(plan.get("stdin_sha256"))
    return _presentation(
        action="Apply Git patch",
        summary=f"Apply a prepared patch to {len(paths)} path(s).",
        detail=(
            f"patch: sha256:{patch_digest}\n"
            f"paths: {_display_paths(paths)}"
        ),
    )


def _git_push_presentation(payload: Mapping[str, Any]) -> Mapping[str, str]:
    """Describe a sealed push without exposing its URL or credential routing."""

    plan = _prepared_push_plan(payload)
    remote = _display_text(_required_text(plan.get("remote_name")))
    remote_host = _display_text(_required_text(plan.get("remote_host")))
    destination_ref = _display_text(_required_text(plan.get("destination_ref")))
    lease = plan.get("force_with_lease")
    if not isinstance(lease, Mapping):
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    lease_mode = _display_text(_required_text(lease.get("mode")))
    lease_target = _display_text(_required_text(lease.get("argument")))
    allow_non_fast_forward = lease.get("allow_non_fast_forward")
    if not isinstance(allow_non_fast_forward, bool):
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    fast_forward_only = "no" if allow_non_fast_forward else "yes"
    destructive_notice = (
        "WARNING: non-fast-forward push may overwrite remote history."
        if allow_non_fast_forward
        else "non-fast-forward updates: not permitted"
    )
    return _presentation(
        action="Push Git branch",
        summary="Push the prepared Git branch to its configured remote.",
        detail=(
            f"remote: {remote} ({remote_host})\n"
            f"ref: {destination_ref}\n"
            f"lease target: {lease_target}\n"
            f"lease mode: {lease_mode}\n"
            f"fast-forward only: {fast_forward_only}\n"
            f"{destructive_notice}"
        ),
    )


def _presentation(*, action: str, summary: str, detail: str) -> Mapping[str, str]:
    """Return the finite display shape accepted by the Host approval record."""

    metadata = {
        "action": _display_text(action),
        "summary": _display_text(summary),
        "detail": _display_text(detail, max_length=_MAX_PRESENTATION_TEXT),
        "confirmation_phrase": "EXECUTE",
    }
    if any(not value for value in metadata.values()):
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    return metadata


def _prepared_git_plan(
    payload: Mapping[str, Any],
    spec: InteractiveEffectSpec,
) -> Mapping[str, Any]:
    """Revalidate the sealed Git plan carried by the exact execute snapshot."""

    plan = payload.get("plan")
    if not isinstance(plan, Mapping):
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    sealed = _sealed_git_plan(plan, spec.prepare_operation_id)
    profile_id = _required_text(payload.get("profile_id"))
    workspace_id = _required_text(payload.get("workspace_id"))
    if (
        profile_id != sealed.get("profile_id")
        or workspace_id != sealed.get("workspace_id")
    ):
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    return sealed


def _prepared_push_plan(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Revalidate the sealed push plan carried by the exact execute snapshot."""

    plan = payload.get("plan")
    digest = payload.get("plan_digest")
    if not isinstance(plan, Mapping) or not _is_digest(digest):
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    frozen = _json_mapping(plan, HostInteractiveEffectService._MAX_REQUEST_BYTES)
    if canonical_digest(frozen) != digest:
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    return frozen


def _terminal_argv(value: object) -> list[str]:
    """Normalize the V4 terminal command exactly as the Host provider does."""

    if isinstance(value, list):
        if not value or any(
            not isinstance(item, str) or "\x00" in item for item in value
        ):
            raise InteractiveEffectUnavailable("interactive effect is unavailable")
        return list(value)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    try:
        argv = shlex.split(value, posix=sys.platform != "win32")
    except ValueError as exc:
        raise InteractiveEffectUnavailable("interactive effect is unavailable") from exc
    if not argv or any("\x00" in item for item in argv):
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    return argv


def _prepared_paths(value: object) -> list[str]:
    """Validate a finite prepared path set without retaining patch bytes."""

    if (
        not isinstance(value, list)
        or not value
        or len(value) > HostInteractiveEffectService._MAX_REQUEST_BYTES
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    return list(value)


def _restore_modes(value: object) -> str:
    """Summarize only the bounded file mode classes in a restore plan."""

    if not isinstance(value, list) or not value:
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    modes: set[str] = set()
    for target in value:
        if not isinstance(target, Mapping):
            raise InteractiveEffectUnavailable("interactive effect is unavailable")
        mode = target.get("mode")
        if (
            not isinstance(mode, str)
            or mode not in {"", "100644", "100755", "120000"}
        ):
            raise InteractiveEffectUnavailable("interactive effect is unavailable")
        modes.add(mode or "deleted")
    return ", ".join(sorted(modes))


def _required_text(value: object) -> str:
    """Return one bounded display source string or fail closed."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    return value


def _required_sha256_hex(value: object) -> str:
    """Validate the raw SHA-256 representation used by Git patch plans."""

    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    return value


def _display_git_object_id(value: object) -> str:
    """Return one prepared Git object identity, never a mutable path or payload."""

    if (
        not isinstance(value, str)
        or len(value) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    return value


def _display_argv(argv: list[str]) -> str:
    """Return a bounded JSON argv projection while redacting secret values."""

    values: list[str] = []
    redact_next = False
    for index, item in enumerate(argv):
        if index >= _MAX_PRESENTATION_ITEMS:
            values.append(f"[{len(argv) - index} additional argv entries omitted]")
            break
        if redact_next:
            values.append(_REDACTED)
            redact_next = False
            continue
        values.append(_display_text(item, max_length=_MAX_PRESENTATION_VALUE))
        redact_next = _secret_argument_consumes_next(item)
    return _display_text(
        json.dumps(values, ensure_ascii=False),
        max_length=_MAX_PRESENTATION_TEXT,
    )


def _display_paths(paths: list[str]) -> str:
    """Return a finite path listing which cannot overflow approval metadata."""

    visible = [
        _display_text(path, max_length=_MAX_PRESENTATION_VALUE)
        for path in paths[:_MAX_PRESENTATION_ITEMS]
    ]
    if len(paths) > _MAX_PRESENTATION_ITEMS:
        visible.append(
            f"[{len(paths) - _MAX_PRESENTATION_ITEMS} additional paths omitted]"
        )
    return _display_text(
        ", ".join(visible),
        max_length=_MAX_PRESENTATION_TEXT,
    )


def _display_text(value: str, *, max_length: int = _MAX_PRESENTATION_VALUE) -> str:
    """Redact likely credential material before applying a hard display bound."""

    if max_length < 32:
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    redacted = _SECRET_ASSIGNMENT.sub(_REDACTED, value)
    redacted = _URL_CREDENTIAL.sub(r"\1" + _REDACTED + "@", redacted)
    if _OPAQUE_SECRET.fullmatch(redacted):
        redacted = _REDACTED
    if len(redacted) <= max_length:
        return redacted
    digest = canonical_digest(redacted).removeprefix("sha256:")
    return f"{redacted[: max_length - 96]}… [truncated sha256:{digest}]"


def _secret_argument_consumes_next(value: str) -> bool:
    """Recognize common CLI forms whose following argv item is credential data."""

    normalized = value.strip().lstrip("-").casefold()
    if "=" in normalized:
        return False
    return normalized in _SECRET_ARGUMENT_NAMES or value in {"-H", "-u"}


def _invocation_owner_id(context: RequestContext) -> str:
    """Create an immutable Host-originated owner identifier for one effect."""

    return "interactive-effect-owner." + canonical_digest(
        {
            "outer_request_id": context.request_id,
            "outer_trace_id": context.trace_id,
            "nonce": secrets.token_hex(16),
        }
    ).removeprefix("sha256:")


def _port_status(status: PendingEffectStatus) -> InteractiveEffectStatus:
    """Strip Host revision and all non-presentation state from a status view."""

    return InteractiveEffectStatus(
        effect_id=status.effect_id,
        approval_request_id=status.approval_request_id,
        state=status.state.value,
        expires_at=status.expires_at,
        redacted_metadata=dict(status.presentation_metadata),
    )


def _json_mapping(value: Mapping[str, Any], max_bytes: int) -> dict[str, Any]:
    """Deep-copy a bounded JSON object before it crosses a Host boundary."""

    if not isinstance(value, Mapping):
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    try:
        encoded = json.dumps(
            _mutable_json_value(value),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > max_bytes:
            raise ValueError("payload too large")
        parsed = json.loads(encoded.decode("utf-8"))
    except Exception as exc:
        raise InteractiveEffectUnavailable("interactive effect is unavailable") from exc
    if not isinstance(parsed, dict):
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    return parsed


def _mutable_json_value(value: object) -> object:
    """Thaw nested immutable Host snapshots without changing JSON values."""

    if isinstance(value, Mapping):
        return {key: _mutable_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mutable_json_value(item) for item in value]
    return value


def _reject_authority_fields(value: object, forbidden: frozenset[str]) -> None:
    """Reject client authority claims anywhere in an untrusted UI request."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or key.casefold() in forbidden:
                raise InteractiveEffectUnavailable("interactive effect is unavailable")
            _reject_authority_fields(item, forbidden)
    elif isinstance(value, list):
        for item in value:
            _reject_authority_fields(item, forbidden)


def _effect_id(value: str) -> str:
    """Validate an opaque pending-effect identifier without treating it as auth."""

    if not isinstance(value, str) or not value or len(value) > 255:
        raise InteractiveEffectUnavailable("interactive effect is unavailable")
    return value


def _is_digest(value: object) -> bool:
    """Recognize a canonical SHA-256 value without accepting aliases."""

    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


__all__ = [
    "CapturedInteractiveEffectRoute",
    "HostInteractiveEffectService",
    "INTERACTIVE_EFFECT_COORDINATOR_CONTRACT_ID",
    "INTERACTIVE_EFFECT_COORDINATOR_OPERATION_ID",
    "INTERACTIVE_EFFECT_SPECS",
    "InteractiveEffectSpec",
    "InteractiveEffectUnavailable",
]
