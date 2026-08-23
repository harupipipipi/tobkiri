"""Versioned Team organization records and deterministic policy resolution.

This module is the contract-level part of the Team model introduced by issue
#1351.  It intentionally has no network, model, provider, or tool execution
side effects.  The coordinator and the state-store packs own those concerns;
this module validates their inputs and produces immutable, hash-bound
assignment/attempt snapshots.

The public functions accept and return JSON-compatible dictionaries.  Keeping
the boundary JSON-shaped makes the same rules usable by the Team Console,
import/export code, and a future transactional Team store without creating a
second persistence format.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


TEAM_DEFINITION_SCHEMA_VERSION = "tobkiri.team-definition/v1"
TEAM_SCHEMA_VERSION = "tobkiri.team/v1"
DEPARTMENT_SCHEMA_VERSION = "tobkiri.department/v1"
MEMBER_POOL_SCHEMA_VERSION = "tobkiri.member-pool/v1"
POLICY_SCHEMA_VERSION = "tobkiri.policy/v1"
EFFECTIVE_POLICY_SCHEMA_VERSION = "tobkiri.effective-policy/v1"
ASSIGNMENT_SCHEMA_VERSION = "tobkiri.assignment/v1"
ATTEMPT_SCHEMA_VERSION = "tobkiri.execution-attempt/v1"

CONFIGURATION_STATES = frozenset({"enabled", "disabled", "archived"})
AVAILABILITY_STATES = frozenset({"offline", "idle", "assigned", "running", "blocked", "paused"})
TEAM_STATES = frozenset({"active", "paused", "archived"})
DEPARTMENT_STATES = frozenset({"enabled", "disabled", "archived"})
POOL_STATES = frozenset({"enabled", "disabled", "archived"})
ASSIGNMENT_STATES = frozenset(
    {
        "planned",
        "queued",
        "assigned",
        "running",
        "waiting_review",
        "completed",
        "failed",
        "cancelled",
    }
)
ATTEMPT_STATES = frozenset(
    {"created", "queued", "running", "waiting_review", "completed", "failed", "cancelled"}
)
TARGET_KINDS = frozenset({"member", "department", "member_pool"})
DISPATCH_MODES = frozenset({"select_one", "fanout", "quorum", "pipeline"})
REVIEW_LEVELS = ("none", "self", "peer", "approval", "dual_control")
ASSURANCE_LEVELS = ("none", "standard", "high", "critical")

# These fields are security boundaries.  A list is an allow-list and is
# intersected at every layer; a corresponding deny-list removes entries even
# if an earlier layer allowed them.  ``capabilities`` is included explicitly
# because selectors/pools must never be able to add one.
_SECURITY_SET_FIELDS = (
    "host",
    "profile",
    "workspace",
    "authority",
    "context",
    "history",
    "memory",
    "network",
    "secret",
    "tool",
    "command",
    "capabilities",
)
_SECURITY_BOOL_FIELDS = ("share_history", "share_workspace", "memory_read", "memory_write")
_LIMIT_FIELDS = (
    "max_concurrency",
    "max_cost",
    "max_tokens",
    "max_time_seconds",
    "max_resource",
)
_PREFERENCE_FIELDS = ("model", "harness", "routing_hint")
_POLICY_ALIASES = {
    "host_access": "host",
    "profile_access": "profile",
    "workspace_access": "workspace",
    "authority_access": "authority",
    "context_access": "context",
    "history_access": "history",
    "memory_access": "memory",
    "network_access": "network",
    "secret_access": "secret",
    "tool_access": "tool",
    "command_access": "command",
    "capability_access": "capabilities",
    "allowed_models": "models",
    "allowed_harnesses": "harnesses",
    "preferred_model": "model",
    "preferred_harness": "harness",
    "max_time": "max_time_seconds",
    "max_duration_seconds": "max_time_seconds",
    "safety_checks": "mandatory_safety_checks",
    "review": "required_review",
    "assurance": "required_assurance",
    "required_acceptance_criteria": "acceptance_criteria",
    "tools": "tool",
    "commands": "command",
    "secrets": "secret",
    "networks": "network",
    "workspaces": "workspace",
    "allow_host": "host",
    "allow_profile": "profile",
    "allow_workspace": "workspace",
    "allow_authority": "authority",
    "allow_context": "context",
    "allow_history": "history",
    "allow_memory": "memory",
    "allow_network": "network",
    "allow_secret": "secret",
    "allow_tool": "tool",
    "allow_command": "command",
    "allow_capabilities": "capabilities",
    "allow_tools": "tool",
    "allow_commands": "command",
    "allow_secrets": "secret",
    "allow_networks": "network",
    "allow_workspaces": "workspace",
    "deny_tools": "deny_tool",
    "deny_commands": "deny_command",
    "deny_secrets": "deny_secret",
    "deny_networks": "deny_network",
    "deny_workspaces": "deny_workspace",
    "allow_models": "models",
    "allow_harnesses": "harnesses",
}
_POLICY_ALLOWED_SCALARS = {
    "models",
    "harnesses",
    "required_review",
    "required_assurance",
    "mandatory_safety_checks",
    "fallback_models",
    "fallback_harnesses",
    "fallbacks",
    "metadata",
}
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HASH_PATTERN = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")
_UNLIMITED = "unlimited"


class TeamModelError(ValueError):
    """Base error for malformed or unsafe Team model input."""


class TeamValidationError(TeamModelError):
    """Raised when a Team Definition is not complete or internally valid."""

    def __init__(self, message: str, diagnostics: Sequence[Mapping[str, Any]] | None = None):
        super().__init__(message)
        self.diagnostics = [dict(item) for item in (diagnostics or [])]


class PolicyResolutionError(TeamModelError):
    """Raised when policy layers cannot be safely resolved."""

    def __init__(self, message: str, diagnostics: Sequence[Mapping[str, Any]] | None = None):
        super().__init__(message)
        self.diagnostics = [dict(item) for item in (diagnostics or [])]


class AliasResolutionError(TeamValidationError):
    """Raised when an alias is unknown or does not identify one Member."""


class ProfileAdoptionError(TeamModelError):
    """Raised when a Profile revision is changed without explicit adoption."""


class SnapshotIntegrityError(TeamModelError):
    """Raised when an immutable policy snapshot no longer matches its hash."""


@dataclass(frozen=True)
class PolicyResolution:
    """A complete effective policy and machine-readable resolution trace."""

    effective: dict[str, Any]
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    layers: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible policy resolution resource."""

        return {
            "schema_version": EFFECTIVE_POLICY_SCHEMA_VERSION,
            "effective": _copy(self.effective),
            "trace": [_copy(item) for item in self.trace],
            "layers": list(self.layers),
            "policy_hash": canonical_hash(self.effective),
        }


def canonical_hash(value: Any) -> str:
    """Hash JSON-compatible values using one stable canonical encoding."""

    encoded = json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _canonicalize(value: Any) -> Any:
    """Convert mappings/sets into deterministic JSON-compatible values."""

    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda entry: str(entry[0]))
        }
    if isinstance(value, (set, frozenset)):
        return sorted((_canonicalize(item) for item in value), key=lambda item: str(item))
    if isinstance(value, (tuple, list)):
        return [_canonicalize(item) for item in value]
    return value


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def _required_id(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not _ID_PATTERN.fullmatch(text):
        raise TeamValidationError(f"{field_name} must be a stable non-empty identifier")
    return text


def _optional_id(value: Any, field_name: str) -> str | None:
    if value in (None, ""):
        return None
    return _required_id(value, field_name)


def _string_list(value: Any, *, field_name: str, max_items: int = 1000) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values: Iterable[Any] = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = (
            sorted(value, key=lambda item: str(item))
            if isinstance(value, (set, frozenset))
            else value
        )
    else:
        raise TeamValidationError(f"{field_name} must be a string list")
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > 255:
            raise TeamValidationError(f"{field_name} contains an overlong value")
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) > max_items:
            raise TeamValidationError(f"{field_name} exceeds its collection limit")
    return result


def _hash_matches(value: Any) -> str:
    text = str(value or "").strip()
    if not _HASH_PATTERN.fullmatch(text):
        raise TeamValidationError("adopted_profile_hash must be a sha256 digest")
    digest = text.split(":", 1)[-1].lower()
    return "sha256:" + digest


def _profile_content(profile: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): _copy(value)
        for key, value in profile.items()
        if key not in {"content_hash", "profile_hash", "adopted_profile_hash"}
    }


def normalize_profile(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize an Agent Profile revision without mutating its source."""

    if not isinstance(raw, Mapping):
        raise TeamValidationError("Profile must be an object")
    profile = _copy(dict(raw))
    profile_id = _required_id(profile.get("profile_id") or profile.get("id"), "profile_id")
    revision = profile.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise TeamValidationError(f"Profile {profile_id} revision must be a positive integer")
    profile["schema_version"] = str(profile.get("schema_version") or "tobkiri.agent-profile/v1")
    profile["profile_id"] = profile_id
    profile["revision"] = revision
    profile["policy"] = normalize_policy(profile.get("policy"), scope="profile")["policy"]
    profile["display_name"] = str(profile.get("display_name") or profile_id).strip()[:200]
    profile["enabled"] = bool(profile.get("enabled", True))
    computed_hash = canonical_hash(_profile_content(profile))
    supplied = profile.get("content_hash") or profile.get("profile_hash")
    if supplied not in (None, "") and _hash_matches(supplied) != computed_hash:
        raise TeamValidationError(f"Profile {profile_id} content_hash does not match content")
    profile["content_hash"] = computed_hash
    profile.pop("profile_hash", None)
    return profile


def _canonical_policy_key(key: str) -> str:
    clean = str(key or "").strip()
    return _POLICY_ALIASES.get(clean, clean)


def _as_set(value: Any, field_name: str) -> set[str]:
    return {item.casefold() for item in _string_list(value, field_name=field_name)}


def normalize_policy(raw: Mapping[str, Any] | None, *, scope: str = "policy") -> dict[str, Any]:
    """Normalize one typed policy layer and reject unknown broadening fields.

    The returned object keeps allow and deny lists separate.  The resolver is
    the only function that combines layers, so callers cannot accidentally
    apply last-write-wins semantics to a capability field.
    """

    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise TeamValidationError(f"{scope} policy must be an object")
    policy: dict[str, Any] = {"schema_version": POLICY_SCHEMA_VERSION, "scope": str(scope)}
    unknown: list[str] = []
    for raw_key, raw_value in raw.items():
        key = _canonical_policy_key(str(raw_key))
        # Normalized policy resources are valid inputs to the normalizer too;
        # their bookkeeping fields do not participate in policy semantics.
        if key in {"schema_version", "scope"}:
            continue
        if key in _SECURITY_SET_FIELDS:
            policy[key] = sorted(_as_set(raw_value, f"{scope}.{raw_key}"))
            continue
        if key.startswith("deny_") and key.removeprefix("deny_") in _SECURITY_SET_FIELDS:
            base = key.removeprefix("deny_")
            policy[key] = sorted(_as_set(raw_value, f"{scope}.{raw_key}"))
            # Keep an explicit empty deny-list so the trace can distinguish an
            # omitted deny field from one deliberately set to empty.
            policy.setdefault(base, policy.get(base, []))
            continue
        if key in _SECURITY_BOOL_FIELDS:
            if not isinstance(raw_value, bool):
                raise TeamValidationError(f"{scope}.{raw_key} must be boolean")
            policy[key] = raw_value
            continue
        if key in _LIMIT_FIELDS:
            policy[key] = _normalize_limit(raw_value, f"{scope}.{raw_key}")
            continue
        if key in _PREFERENCE_FIELDS:
            if raw_value in (None, ""):
                continue
            if not isinstance(raw_value, str):
                raise TeamValidationError(f"{scope}.{raw_key} must be a string")
            policy[key] = raw_value.strip()
            continue
        if key in {"models", "harnesses", "mandatory_safety_checks"}:
            policy[key] = sorted(_as_set(raw_value, f"{scope}.{raw_key}"))
            continue
        if key == "acceptance_criteria":
            policy[key] = sorted(_as_set(raw_value, f"{scope}.{raw_key}"))
            continue
        if key == "evidence_required":
            if not isinstance(raw_value, bool):
                raise TeamValidationError(f"{scope}.{raw_key} must be boolean")
            policy[key] = raw_value
            continue
        if key in {"deny_models", "deny_harnesses"}:
            policy[key] = sorted(_as_set(raw_value, f"{scope}.{raw_key}"))
            continue
        if key in {"fallback_models", "fallback_harnesses", "fallbacks"}:
            policy[key] = _string_list(raw_value, field_name=f"{scope}.{raw_key}")
            continue
        if key == "required_review":
            value = str(raw_value or "none").strip().casefold()
            if value not in REVIEW_LEVELS:
                raise TeamValidationError(f"{scope}.required_review is invalid")
            policy[key] = value
            continue
        if key == "required_assurance":
            value = str(raw_value or "none").strip().casefold()
            if value not in ASSURANCE_LEVELS:
                raise TeamValidationError(f"{scope}.required_assurance is invalid")
            policy[key] = value
            continue
        if key == "metadata":
            if not isinstance(raw_value, Mapping):
                raise TeamValidationError(f"{scope}.metadata must be an object")
            policy[key] = _copy(dict(raw_value))
            continue
        # ``allow_*`` and ``*_allow`` unknown keys are especially dangerous:
        # accepting them would create an untraced capability escape hatch.
        unknown.append(str(raw_key))
    if unknown:
        raise TeamValidationError(
            f"{scope} policy contains unsupported fields: {', '.join(sorted(unknown))}"
        )
    return {"policy": policy}


def _normalize_limit(value: Any, field_name: str) -> int | float | str:
    if isinstance(value, bool):
        raise TeamValidationError(f"{field_name} must be a non-negative number or unlimited")
    if isinstance(value, str) and value.strip().casefold() == _UNLIMITED:
        return _UNLIMITED
    if not isinstance(value, (int, float)) or value < 0:
        raise TeamValidationError(f"{field_name} must be a non-negative number or unlimited")
    return value


def _profile_record(
    profiles: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    profile_id: str,
) -> dict[str, Any] | None:
    if profiles is None:
        return None
    if isinstance(profiles, Mapping):
        raw = profiles.get(profile_id)
        if raw is None:
            return None
    else:
        matches = [
            item
            for item in profiles
            if isinstance(item, Mapping)
            and str(item.get("profile_id") or item.get("id")) == profile_id
        ]
        if not matches:
            return None
        raw = matches[0]
    return normalize_profile(raw)


def _normalize_member(raw: Mapping[str, Any], *, profiles: Any = None) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise TeamValidationError("Member must be an object")
    member = _copy(dict(raw))
    member_id = _required_id(member.get("member_id") or member.get("id"), "member_id")
    profile_id = _required_id(
        member.get("profile_id") or member.get("agent_profile_id"), "profile_id"
    )
    revision = member.get("adopted_profile_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise TeamValidationError(f"Member {member_id} must adopt a positive Profile revision")
    profile_hash = _hash_matches(
        member.get("adopted_profile_hash")
        or member.get("profile_hash")
        or member.get("content_hash")
    )
    profile = _profile_record(profiles, profile_id)
    if profiles is not None and profile is None:
        raise TeamValidationError(f"Member {member_id} references missing Profile {profile_id}")
    if profile is not None:
        if profile["revision"] != revision or profile["content_hash"] != profile_hash:
            raise TeamValidationError(
                f"Member {member_id} Profile adoption does not match {profile_id} revision/hash"
            )
    configuration = str(
        member.get("configuration") or member.get("configuration_state") or "enabled"
    ).casefold()
    availability = str(member.get("availability") or "offline").casefold()
    if configuration not in CONFIGURATION_STATES:
        raise TeamValidationError(f"Member {member_id} configuration state is invalid")
    if availability not in AVAILABILITY_STATES:
        raise TeamValidationError(f"Member {member_id} availability state is invalid")
    if configuration == "archived" and availability in {"assigned", "running"}:
        raise TeamValidationError(f"Archived Member {member_id} cannot be assigned or running")
    raw_aliases = member.get("aliases")
    if isinstance(raw_aliases, (list, tuple, set, frozenset)):
        alias_keys = [str(value or "").strip().lstrip("@").casefold() for value in raw_aliases]
        alias_keys = [value for value in alias_keys if value]
        if len(alias_keys) != len(set(alias_keys)):
            raise TeamValidationError(f"Member {member_id} aliases must be unique")
    aliases = _string_list(raw_aliases, field_name=f"member {member_id}.aliases")
    aliases = [alias.lstrip("@").strip() for alias in aliases if alias.lstrip("@").strip()]
    overrides = member.get("overrides") if isinstance(member.get("overrides"), Mapping) else {}
    policy_source = member.get("policy")
    if policy_source is None:
        policy_source = overrides.get("policy") if isinstance(overrides, Mapping) else {}
    normalized_policy = normalize_policy(policy_source, scope=f"member:{member_id}")["policy"]
    primary_department_id = _optional_id(
        member.get("primary_department_id") or member.get("department_id"),
        f"member {member_id}.primary_department_id",
    )
    result = {
        "schema_version": "tobkiri.member/v1",
        "member_id": member_id,
        "profile_id": profile_id,
        "adopted_profile_revision": revision,
        "adopted_profile_hash": profile_hash,
        "primary_department_id": primary_department_id,
        "configuration": configuration,
        "availability": availability,
        "aliases": aliases,
        "policy": normalized_policy,
        "overrides": {key: _copy(value) for key, value in overrides.items() if key != "policy"},
        "display_name": str(member.get("display_name") or member_id).strip()[:200],
        "labels": _string_list(member.get("labels"), field_name=f"member {member_id}.labels"),
        "metadata": _copy(
            member.get("metadata") if isinstance(member.get("metadata"), Mapping) else {}
        ),
    }
    if profile is not None:
        # Keep the adopted revision's policy with the runtime record.  A source
        # Profile edit can therefore never mutate a running Team implicitly.
        result["adopted_profile_policy"] = _copy(profile["policy"])
        result["adopted_profile_display_name"] = profile["display_name"]
    else:
        result["adopted_profile_policy"] = {}
    return result


def _normalize_department(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise TeamValidationError("Department must be an object")
    department_id = _required_id(raw.get("department_id") or raw.get("id"), "department_id")
    if raw.get("parent_department_id") not in (None, "") or raw.get("parent_id") not in (None, ""):
        raise TeamValidationError("v1 Departments are flat and cannot be nested")
    status = str(raw.get("status") or "enabled").casefold()
    if status not in DEPARTMENT_STATES:
        raise TeamValidationError(f"Department {department_id} status is invalid")
    lead = _optional_id(raw.get("lead_member_id"), f"department {department_id}.lead_member_id")
    member_ids = _string_list(
        raw.get("member_ids") if raw.get("member_ids") is not None else raw.get("members"),
        field_name=f"department {department_id}.member_ids",
    )
    policy = normalize_policy(raw.get("policy"), scope=f"department:{department_id}")["policy"]
    return {
        "schema_version": DEPARTMENT_SCHEMA_VERSION,
        "department_id": department_id,
        "name": str(raw.get("name") or department_id).strip()[:200],
        "status": status,
        "lead_member_id": lead,
        "member_ids": member_ids,
        "policy": policy,
        "metadata": _copy(raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}),
    }


def _normalize_pool(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise TeamValidationError("Member Pool must be an object")
    pool_id = _required_id(raw.get("pool_id") or raw.get("id"), "pool_id")
    status = str(raw.get("status") or "enabled").casefold()
    if status not in POOL_STATES:
        raise TeamValidationError(f"Member Pool {pool_id} status is invalid")
    static_member_ids = _string_list(
        raw.get("member_ids") if raw.get("member_ids") is not None else raw.get("members"),
        field_name=f"pool {pool_id}.member_ids",
    )
    selector = raw.get("selector")
    if selector is None:
        selector = {}
    if not isinstance(selector, Mapping):
        raise TeamValidationError(f"Member Pool {pool_id}.selector must be an object")
    selector = _copy(dict(selector))
    for key in selector:
        if key not in {"department_id", "configuration", "availability", "labels", "member_ids"}:
            raise TeamValidationError(
                f"Member Pool {pool_id}.selector contains unsupported field {key}"
            )
    forbidden = {
        "policy",
        "capabilities",
        "permissions",
        "authority",
        "context",
        "workspace_access",
        "tool_access",
        "command_access",
        "network_access",
        "secret_access",
    }
    if forbidden.intersection(raw):
        raise TeamValidationError(f"Member Pool {pool_id} cannot grant policy or authority")
    return {
        "schema_version": MEMBER_POOL_SCHEMA_VERSION,
        "pool_id": pool_id,
        "name": str(raw.get("name") or pool_id).strip()[:200],
        "status": status,
        "member_ids": static_member_ids,
        "selector": selector,
        "routing_only": True,
        "metadata": _copy(raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}),
    }


def normalize_team_definition(
    raw: Mapping[str, Any],
    *,
    profiles: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate and normalize one complete reusable Team Definition.

    Validation is performed before any runtime object is created.  References
    are exact, aliases are unique in Team scope, and no partial materialization
    is returned on failure.
    """

    if not isinstance(raw, Mapping):
        raise TeamValidationError("Team Definition must be an object")
    source = _copy(dict(raw))
    definition_id = _required_id(
        source.get("team_definition_id") or source.get("definition_id") or source.get("id"),
        "team_definition_id",
    )
    revision = source.get("revision", 1)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise TeamValidationError("Team Definition revision must be a positive integer")
    members_raw = source.get("members")
    if not isinstance(members_raw, (list, tuple)):
        raise TeamValidationError("Team Definition members must be a list")
    members = [_normalize_member(value, profiles=profiles) for value in members_raw]
    member_ids = [member["member_id"] for member in members]
    if len(set(member_ids)) != len(member_ids):
        raise TeamValidationError("Member IDs must be unique within a Team Definition")
    member_by_id = {member["member_id"]: member for member in members}
    aliases: dict[str, str] = {}
    for member in members:
        for alias in [member["member_id"], *member["aliases"]]:
            key = alias.casefold()
            previous = aliases.get(key)
            if previous is not None and previous != member["member_id"]:
                raise TeamValidationError(f"Alias {alias!r} is ambiguous between Members")
            aliases[key] = member["member_id"]
    departments_raw = source.get("departments") or []
    if not isinstance(departments_raw, (list, tuple)):
        raise TeamValidationError("Team Definition departments must be a list")
    departments = [_normalize_department(value) for value in departments_raw]
    department_ids = [department["department_id"] for department in departments]
    if len(set(department_ids)) != len(department_ids):
        raise TeamValidationError("Department IDs must be unique and non-nested")
    department_by_id = {department["department_id"]: department for department in departments}
    for member in members:
        department_id = member["primary_department_id"]
        if department_id and department_id not in department_by_id:
            raise TeamValidationError(
                f"Member {member['member_id']} references missing Department {department_id}"
            )
    for department in departments:
        for member_id in department["member_ids"]:
            if member_id not in member_by_id:
                raise TeamValidationError(
                    f"Department {department['department_id']} references missing Member {member_id}"
                )
        lead_id = department["lead_member_id"]
        if lead_id:
            lead = member_by_id.get(lead_id)
            if lead is None or lead["configuration"] != "enabled":
                raise TeamValidationError(
                    f"Department {department['department_id']} Lead must be an enabled Member in this Team"
                )
    manager_id = _optional_id(source.get("manager_member_id"), "manager_member_id")
    if manager_id:
        manager = member_by_id.get(manager_id)
        if manager is None:
            raise TeamValidationError("manager_member_id must reference a Member in this Team")
        if manager["configuration"] != "enabled":
            raise TeamValidationError(
                "manager_member_id must reference an enabled Member in this Team"
            )
    pools_raw = source.get("member_pools")
    if pools_raw is None:
        pools_raw = source.get("pools") or []
    if not isinstance(pools_raw, (list, tuple)):
        raise TeamValidationError("Member Pools must be a list")
    pools = [_normalize_pool(value) for value in pools_raw]
    pool_ids = [pool["pool_id"] for pool in pools]
    if len(set(pool_ids)) != len(pool_ids):
        raise TeamValidationError("Member Pool IDs must be unique")
    for pool in pools:
        selected_ids = [
            *pool["member_ids"],
            *_string_list(
                pool["selector"].get("member_ids"), field_name="pool selector.member_ids"
            ),
        ]
        for member_id in selected_ids:
            if member_id not in member_by_id:
                raise TeamValidationError(
                    f"Member Pool {pool['pool_id']} references missing Member {member_id}"
                )
        selector_department = pool["selector"].get("department_id")
        if selector_department and selector_department not in department_by_id:
            raise TeamValidationError(
                f"Member Pool {pool['pool_id']} references missing Department {selector_department}"
            )
    policy = normalize_policy(source.get("policy"), scope=f"team-definition:{definition_id}")[
        "policy"
    ]
    result = {
        "schema_version": TEAM_DEFINITION_SCHEMA_VERSION,
        "team_definition_id": definition_id,
        "revision": revision,
        "name": str(source.get("name") or definition_id).strip()[:200],
        "description": str(source.get("description") or "").strip()[:4000],
        "manager_member_id": manager_id,
        "members": members,
        "departments": departments,
        "member_pools": pools,
        "policy": policy,
        "metadata": _copy(
            source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {}
        ),
        "aliases": aliases,
    }
    result["definition_hash"] = canonical_hash(
        {key: value for key, value in result.items() if key not in {"definition_hash", "aliases"}}
    )
    return result


def validate_team_definition(
    raw: Mapping[str, Any],
    *,
    profiles: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compatibility alias for :func:`normalize_team_definition`."""

    return normalize_team_definition(raw, profiles=profiles)


def resolve_effective_policy(
    layers: Sequence[Mapping[str, Any] | tuple[str, Mapping[str, Any]]],
    *,
    available_models: Iterable[str] | None = None,
    available_harnesses: Iterable[str] | None = None,
) -> PolicyResolution:
    """Resolve policy layers with intersection, deny-wins, and strict bounds.

    ``layers`` must be ordered from broadest to most specific.  A tuple may
    provide an explicit scope name; otherwise the layer's ``scope`` field is
    used.  The trace records every field decision and contains no raw prompt,
    token, secret, or credential material.
    """

    normalized_layers: list[tuple[str, dict[str, Any]]] = []
    for index, raw_layer in enumerate(layers):
        if isinstance(raw_layer, tuple) and len(raw_layer) == 2:
            scope, raw_policy = raw_layer
        else:
            scope = (
                raw_layer.get("scope", f"layer_{index}")
                if isinstance(raw_layer, Mapping)
                else f"layer_{index}"
            )
            raw_policy = raw_layer
        if not isinstance(raw_policy, Mapping):
            raise PolicyResolutionError(f"Policy layer {scope} must be an object")
        normalized_layers.append(
            (str(scope), normalize_policy(raw_policy, scope=str(scope))["policy"])
        )
    effective_sets: dict[str, set[str] | None] = {
        field_name: None for field_name in _SECURITY_SET_FIELDS
    }
    effective_bools: dict[str, bool | None] = {
        field_name: None for field_name in _SECURITY_BOOL_FIELDS
    }
    effective_limits: dict[str, int | float | str] = {}
    explicit_unlimited: dict[str, int] = {field_name: 0 for field_name in _LIMIT_FIELDS}
    effective_reviews = "none"
    effective_assurance = "none"
    effective_checks: set[str] = set()
    effective_acceptance_criteria: set[str] = set()
    evidence_required = False
    allowed_models: set[str] | None = None
    allowed_harnesses: set[str] | None = None
    preferences: dict[str, str] = {}
    trace: list[dict[str, Any]] = []
    scope_names: list[str] = []

    def add_trace(
        field_name: str, scope: str, rule: str, before: Any, after: Any, **details: Any
    ) -> None:
        trace.append(
            {
                "field": field_name,
                "scope": scope,
                "rule": rule,
                "before": _copy(before),
                "after": _copy(after),
                **{key: _copy(value) for key, value in details.items()},
            }
        )

    for scope, policy in normalized_layers:
        scope_names.append(scope)
        for field_name in _SECURITY_SET_FIELDS:
            if field_name in policy:
                current_set = effective_sets[field_name]
                set_before = None if current_set is None else sorted(current_set)
                incoming_set = set(policy[field_name])
                effective_sets[field_name] = (
                    incoming_set if current_set is None else current_set & incoming_set
                )
                add_trace(
                    field_name,
                    scope,
                    "intersection",
                    set_before,
                    sorted(effective_sets[field_name] or []),
                    incoming=sorted(incoming_set),
                )
            deny_key = "deny_" + field_name
            if deny_key in policy:
                current_set = effective_sets[field_name]
                set_before = None if current_set is None else sorted(current_set)
                denied = set(policy[deny_key])
                if current_set is not None:
                    effective_sets[field_name] = current_set - denied
                add_trace(
                    field_name,
                    scope,
                    "deny_wins",
                    set_before,
                    None
                    if effective_sets[field_name] is None
                    else sorted(effective_sets[field_name] or []),
                    denied=sorted(denied),
                )
        for field_name in _SECURITY_BOOL_FIELDS:
            if field_name not in policy:
                continue
            bool_before = effective_bools[field_name]
            bool_value = bool(policy[field_name])
            effective_bools[field_name] = (
                bool_value if bool_before is None else bool_before and bool_value
            )
            add_trace(
                field_name,
                scope,
                "deny_wins",
                bool_before,
                effective_bools[field_name],
            )
        for field_name in _LIMIT_FIELDS:
            if field_name not in policy:
                continue
            limit_before = effective_limits.get(field_name)
            incoming_limit: int | float | str = policy[field_name]
            effective_limits[field_name] = _strictest_limit(limit_before, incoming_limit)
            if incoming_limit == _UNLIMITED:
                explicit_unlimited[field_name] += 1
            add_trace(
                field_name,
                scope,
                "strictest_bound",
                limit_before,
                effective_limits[field_name],
            )
        if "required_review" in policy:
            review_before = effective_reviews
            incoming_review = str(policy["required_review"])
            effective_reviews = _strictest_level(effective_reviews, incoming_review, REVIEW_LEVELS)
            add_trace(
                "required_review",
                scope,
                "strictest_review",
                review_before,
                effective_reviews,
            )
        if "required_assurance" in policy:
            assurance_before = effective_assurance
            incoming_assurance = str(policy["required_assurance"])
            effective_assurance = _strictest_level(
                effective_assurance, incoming_assurance, ASSURANCE_LEVELS
            )
            add_trace(
                "required_assurance",
                scope,
                "strictest_assurance",
                assurance_before,
                effective_assurance,
            )
        if "mandatory_safety_checks" in policy:
            checks_before = sorted(effective_checks)
            effective_checks.update(policy["mandatory_safety_checks"])
            add_trace(
                "mandatory_safety_checks",
                scope,
                "union",
                checks_before,
                sorted(effective_checks),
                added=sorted(set(policy["mandatory_safety_checks"])),
            )
        if "acceptance_criteria" in policy:
            criteria_before = sorted(effective_acceptance_criteria)
            effective_acceptance_criteria.update(policy["acceptance_criteria"])
            add_trace(
                "acceptance_criteria",
                scope,
                "union",
                criteria_before,
                sorted(effective_acceptance_criteria),
                added=sorted(set(policy["acceptance_criteria"])),
            )
        if "evidence_required" in policy:
            evidence_before = evidence_required
            evidence_required = evidence_required or bool(policy["evidence_required"])
            add_trace(
                "evidence_required",
                scope,
                "strictest_requirement",
                evidence_before,
                evidence_required,
            )
        if "models" in policy:
            models_before = None if allowed_models is None else sorted(allowed_models)
            incoming_models = set(policy["models"])
            allowed_models = (
                incoming_models if allowed_models is None else allowed_models & incoming_models
            )
            add_trace(
                "models",
                scope,
                "intersection",
                models_before,
                sorted(allowed_models),
                incoming=sorted(incoming_models),
            )
        if "deny_models" in policy:
            models_before = None if allowed_models is None else sorted(allowed_models)
            denied_models = set(policy["deny_models"])
            if allowed_models is not None:
                allowed_models -= denied_models
            add_trace(
                "models",
                scope,
                "deny_wins",
                models_before,
                None if allowed_models is None else sorted(allowed_models),
                denied=sorted(denied_models),
            )
        if "harnesses" in policy:
            harnesses_before = (
                None if allowed_harnesses is None else sorted(allowed_harnesses)
            )
            incoming_harnesses = set(policy["harnesses"])
            allowed_harnesses = (
                incoming_harnesses
                if allowed_harnesses is None
                else allowed_harnesses & incoming_harnesses
            )
            add_trace(
                "harnesses",
                scope,
                "intersection",
                harnesses_before,
                sorted(allowed_harnesses),
                incoming=sorted(incoming_harnesses),
            )
        if "deny_harnesses" in policy:
            harnesses_before = (
                None if allowed_harnesses is None else sorted(allowed_harnesses)
            )
            denied_harnesses = set(policy["deny_harnesses"])
            if allowed_harnesses is not None:
                allowed_harnesses -= denied_harnesses
            add_trace(
                "harnesses",
                scope,
                "deny_wins",
                harnesses_before,
                None if allowed_harnesses is None else sorted(allowed_harnesses),
                denied=sorted(denied_harnesses),
            )
        for field_name in _PREFERENCE_FIELDS:
            if field_name not in policy:
                continue
            candidate = str(policy[field_name])
            preference_before = preferences.get(field_name)
            if field_name == "model":
                allowed = allowed_models
                available = (
                    {str(item).casefold() for item in available_models}
                    if available_models is not None
                    else None
                )
                fallback = [*policy.get("fallback_models", []), *policy.get("fallbacks", [])]
            elif field_name == "harness":
                allowed = allowed_harnesses
                available = (
                    {str(item).casefold() for item in available_harnesses}
                    if available_harnesses is not None
                    else None
                )
                fallback = [*policy.get("fallback_harnesses", []), *policy.get("fallbacks", [])]
            else:
                allowed = None
                available = None
                fallback = list(policy.get("fallbacks", []))
            if allowed is not None and candidate.casefold() not in allowed:
                candidate = _choose_fallback(field_name, fallback, allowed, available, scope)
            elif available is not None and candidate.casefold() not in available:
                candidate = _choose_fallback(field_name, fallback, allowed, available, scope)
            preferences[field_name] = candidate
            add_trace(
                field_name,
                scope,
                "most_specific_valid_preference",
                preference_before,
                candidate,
            )
    if allowed_models is not None and not allowed_models:
        raise PolicyResolutionError("Policy layers have no common allowed model", trace)
    if allowed_harnesses is not None and not allowed_harnesses:
        raise PolicyResolutionError("Policy layers have no common allowed harness", trace)
    # A selected preference must remain within the final capability set.  This
    # catches a narrower later layer that arrived after the preference layer.
    for field_name, allowed in (("model", allowed_models), ("harness", allowed_harnesses)):
        preference_value = preferences.get(field_name)
        if (
            preference_value
            and allowed is not None
            and preference_value.casefold() not in allowed
        ):
            raise PolicyResolutionError(
                f"Preferred {field_name} is outside the effective allow-list", trace
            )
    # ``unlimited`` is a positive capability.  An omitted higher ceiling is
    # not an explicit permit, so retain an unspecified bounded value rather
    # than accidentally turning one layer's preference into unlimited access.
    if normalized_layers:
        for field_name, limit_value in list(effective_limits.items()):
            if limit_value == _UNLIMITED and explicit_unlimited[field_name] != len(
                normalized_layers
            ):
                add_trace(
                    field_name,
                    "resolver",
                    "unlimited_requires_explicit_all_layers",
                    limit_value,
                    None,
                )
                effective_limits.pop(field_name, None)
    effective: dict[str, Any] = {"schema_version": EFFECTIVE_POLICY_SCHEMA_VERSION}
    for field_name in _SECURITY_SET_FIELDS:
        effective[field_name] = sorted(effective_sets[field_name] or [])
    for field_name in _SECURITY_BOOL_FIELDS:
        effective[field_name] = bool(effective_bools[field_name])
    effective["limits"] = {key: value for key, value in sorted(effective_limits.items())}
    for field_name, value in effective_limits.items():
        effective[field_name] = value
    effective["required_review"] = effective_reviews
    effective["required_assurance"] = effective_assurance
    effective["mandatory_safety_checks"] = sorted(effective_checks)
    effective["acceptance_criteria"] = sorted(effective_acceptance_criteria)
    effective["evidence_required"] = evidence_required
    effective["allowed_models"] = sorted(allowed_models) if allowed_models is not None else []
    effective["allowed_harnesses"] = (
        sorted(allowed_harnesses) if allowed_harnesses is not None else []
    )
    effective.update(preferences)
    return PolicyResolution(effective=effective, trace=tuple(trace), layers=tuple(scope_names))


def _strictest_limit(
    before: int | float | str | None, incoming: int | float | str
) -> int | float | str:
    if before is None:
        return incoming
    if before == _UNLIMITED:
        return incoming
    if incoming == _UNLIMITED:
        return before
    return min(before, incoming)


def _strictest_level(before: str, incoming: str, levels: Sequence[str]) -> str:
    return levels[max(levels.index(before), levels.index(incoming))]


def _choose_fallback(
    field_name: str,
    fallback: Sequence[str],
    allowed: set[str] | None,
    available: set[str] | None,
    scope: str,
) -> str:
    for value in fallback:
        candidate = str(value).strip()
        if not candidate:
            continue
        normalized = candidate.casefold()
        if allowed is not None and normalized not in allowed:
            continue
        if available is not None and normalized not in available:
            continue
        return candidate
    reason = "unavailable" if available is not None else "outside effective allow-list"
    raise PolicyResolutionError(
        f"Explicit {field_name} backend in {scope} is {reason}; configure an explicit fallback"
    )


def materialize_team(
    definition: Mapping[str, Any],
    *,
    profiles: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    team_id: str | None = None,
    generation: int = 1,
    actor_id: str = "system",
    now: str | None = None,
) -> dict[str, Any]:
    """Create one persistent Team runtime record atomically in memory.

    All references and Profile revisions are validated before constructing the
    result.  A caller can persist this returned record in the canonical Team
    state owner; no mutation happens on the input Definition or Profiles.
    """

    normalized = normalize_team_definition(definition, profiles=profiles)
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise TeamValidationError("Team generation must be a positive integer")
    runtime_id = _required_id(team_id or _new_id("team_"), "team_id")
    timestamp = now or _now()
    team_policy = resolve_effective_policy([("team", normalized["policy"])])
    members = _copy(normalized["members"])
    runtime = {
        "schema_version": TEAM_SCHEMA_VERSION,
        "team_id": runtime_id,
        "team_definition_id": normalized["team_definition_id"],
        "team_definition_revision": normalized["revision"],
        "team_definition_hash": normalized["definition_hash"],
        "generation": generation,
        "state": "active",
        "name": normalized["name"],
        "description": normalized["description"],
        "manager_member_id": normalized["manager_member_id"],
        "members": members,
        "departments": _copy(normalized["departments"]),
        "member_pools": _copy(normalized["member_pools"]),
        "policy": _copy(normalized["policy"]),
        "policy_snapshot": _copy(team_policy.effective),
        "policy_resolution_trace": [_copy(item) for item in team_policy.trace],
        "policy_snapshot_hash": canonical_hash(team_policy.effective),
        "assignments": {},
        "attempts": {},
        "created_at": timestamp,
        "updated_at": timestamp,
        "profile_generation": max(
            (member["adopted_profile_revision"] for member in members),
            default=0,
        ),
        "provenance": {
            "materialized_by": str(actor_id or "system"),
            "materialized_at": timestamp,
            "team_definition_hash": normalized["definition_hash"],
        },
        "metadata": _copy(normalized["metadata"]),
    }
    return runtime


def _member_map(team: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw_values = team.get("members")
    values = raw_values if isinstance(raw_values, list) else []
    return {
        str(value.get("member_id")): dict(value)
        for value in values
        if isinstance(value, Mapping)
    }


def _department_map(team: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw_values = team.get("departments")
    values = raw_values if isinstance(raw_values, list) else []
    return {
        str(value.get("department_id")): dict(value)
        for value in values
        if isinstance(value, Mapping)
    }


def _pool_map(team: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    values = (
        team.get("member_pools")
        if isinstance(team.get("member_pools"), list)
        else team.get("pools")
    )
    values = values if isinstance(values, list) else []
    return {
        str(value.get("pool_id")): dict(value)
        for value in values
        if isinstance(value, Mapping)
    }


def resolve_member_alias(team: Mapping[str, Any], alias: str) -> dict[str, Any]:
    """Resolve one exact Team-local alias, failing closed on ambiguity."""

    requested = str(alias or "").strip().lstrip("@").casefold()
    if not requested:
        raise AliasResolutionError("Member alias is empty")
    matches: list[dict[str, Any]] = []
    for member in _member_map(team).values():
        keys = {str(member.get("member_id") or "").casefold()}
        keys.update(str(value).casefold() for value in member.get("aliases") or [])
        if requested in keys:
            matches.append(member)
    if len(matches) != 1:
        if not matches:
            raise AliasResolutionError(f"Unknown Member alias: {alias}")
        raise AliasResolutionError(f"Ambiguous Member alias: {alias}")
    return _copy(matches[0])


def _pool_candidates(team: Mapping[str, Any], pool: Mapping[str, Any]) -> list[dict[str, Any]]:
    members = _member_map(team)
    static_ids = [
        *pool.get("member_ids", []),
        *((pool.get("selector") or {}).get("member_ids") or []),
    ]
    if static_ids:
        candidates = [members[item] for item in static_ids if item in members]
    else:
        candidates = list(members.values())
    raw_selector = pool.get("selector")
    selector = dict(raw_selector) if isinstance(raw_selector, Mapping) else {}
    department_id = selector.get("department_id")
    configuration = selector.get("configuration")
    availability = selector.get("availability")
    labels = {
        str(item).casefold()
        for item in _string_list(selector.get("labels"), field_name="pool selector.labels")
    }
    result = []
    for member in candidates:
        if department_id and member.get("primary_department_id") != department_id:
            continue
        if configuration and member.get("configuration") != str(configuration).casefold():
            continue
        if availability and member.get("availability") != str(availability).casefold():
            continue
        member_labels = {str(item).casefold() for item in member.get("labels") or []}
        if labels and not labels.issubset(member_labels):
            continue
        if member.get("configuration") != "enabled":
            continue
        result.append(member)
    return sorted(result, key=lambda item: str(item.get("member_id")))


def _target_members(
    team: Mapping[str, Any], target_kind: str, target_id: str
) -> list[dict[str, Any]]:
    members = _member_map(team)
    if target_kind == "member":
        member = members.get(target_id)
        if member is None:
            raise TeamValidationError(f"Unknown target Member {target_id}")
        if not can_accept_assignment(team, target_id):
            raise TeamValidationError(f"Member {target_id} cannot accept a new Assignment")
        return [_copy(member)]
    if target_kind == "department":
        department = _department_map(team).get(target_id)
        if department is None:
            raise TeamValidationError(f"Unknown target Department {target_id}")
        if department.get("status") != "enabled":
            raise TeamValidationError(f"Department {target_id} is not enabled")
        return sorted(
            [
                members[item]
                for item in department.get("member_ids", [])
                if item in members and can_accept_assignment(team, item)
            ],
            key=lambda item: str(item.get("member_id")),
        )
    pool = _pool_map(team).get(target_id)
    if pool is None:
        raise TeamValidationError(f"Unknown target Member Pool {target_id}")
    if pool.get("status") != "enabled":
        raise TeamValidationError(f"Member Pool {target_id} is not enabled")
    return _pool_candidates(team, pool)


def _manager_policy(team: Mapping[str, Any]) -> Mapping[str, Any]:
    manager_id = str(team.get("manager_member_id") or "")
    manager = _member_map(team).get(manager_id)
    if not manager:
        return {}
    policy = manager.get("policy")
    return dict(policy) if isinstance(policy, Mapping) else {}


def _department_policy(team: Mapping[str, Any], member: Mapping[str, Any]) -> Mapping[str, Any]:
    department_id = str(member.get("primary_department_id") or "")
    department = _department_map(team).get(department_id)
    policy = department.get("policy") if isinstance(department, Mapping) else None
    return dict(policy) if isinstance(policy, Mapping) else {}


def _profile_policy(member: Mapping[str, Any]) -> Mapping[str, Any]:
    policy = member.get("adopted_profile_policy")
    return policy if isinstance(policy, Mapping) else {}


def _review_snapshot(
    team: Mapping[str, Any],
    policy: Mapping[str, Any],
    requested: Mapping[str, Any] | None,
) -> dict[str, Any]:
    requested = requested if isinstance(requested, Mapping) else {}
    requested_level = str(requested.get("required_review") or "none").casefold()
    policy_level = str(policy.get("required_review") or "none").casefold()
    if requested_level not in REVIEW_LEVELS or policy_level not in REVIEW_LEVELS:
        raise TeamValidationError("Assignment required_review is invalid")
    # An Assignment can add review requirements, never remove a Team/Profile
    # gate that was already required by a broader layer.
    level = _strictest_level(policy_level, requested_level, REVIEW_LEVELS)
    reviewer_id = _optional_id(requested.get("reviewer_member_id"), "reviewer_member_id")
    if level != "none":
        if not reviewer_id:
            raise TeamValidationError("Required review must freeze reviewer_member_id")
        reviewer = _member_map(team).get(reviewer_id)
        if reviewer is None or reviewer.get("configuration") != "enabled":
            raise TeamValidationError("Reviewer must be an enabled Member in the same Team")
    input_revision = str(
        requested.get("reviewed_input_revision") or requested.get("input_revision") or ""
    ).strip()
    if level != "none" and not input_revision:
        raise TeamValidationError("Required review must freeze reviewed_input_revision")
    return {
        "required_review": level,
        "reviewer_member_id": reviewer_id,
        "reviewed_input_revision": input_revision or None,
        "evidence_required": bool(
            policy.get("evidence_required") or requested.get("evidence_required") or level != "none"
        ),
        "acceptance_criteria": sorted(
            {
                *(
                    str(value)
                    for value in policy.get("acceptance_criteria", [])
                    if str(value).strip()
                ),
                *_string_list(
                    requested.get("acceptance_criteria"),
                    field_name="review.acceptance_criteria",
                ),
            }
        ),
    }


def create_assignment(
    team: Mapping[str, Any],
    *,
    target_kind: str,
    target_id: str,
    dispatch_mode: str = "select_one",
    requested_policy: Mapping[str, Any] | None = None,
    review: Mapping[str, Any] | None = None,
    assignment_id: str | None = None,
    input_snapshot_hash: str | None = None,
    available_models: Iterable[str] | None = None,
    available_harnesses: Iterable[str] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Resolve a target and freeze one immutable Assignment policy snapshot."""

    if str(team.get("schema_version") or "") != TEAM_SCHEMA_VERSION:
        raise TeamValidationError("Assignment requires a canonical Team runtime record")
    if str(team.get("state") or "") != "active":
        raise TeamValidationError("Only an active Team can create a new Assignment")
    target_kind = str(target_kind or "").casefold()
    if target_kind not in TARGET_KINDS:
        raise TeamValidationError("Assignment target_kind is invalid")
    dispatch_mode = str(dispatch_mode or "select_one").casefold()
    if dispatch_mode not in DISPATCH_MODES:
        raise TeamValidationError("Assignment dispatch_mode is invalid")
    target_id = _required_id(target_id, "target_id")
    selected = _target_members(team, target_kind, target_id)
    if not selected:
        raise TeamValidationError(f"Target {target_kind}:{target_id} has no enabled Members")
    if dispatch_mode == "select_one":
        selected = selected[:1]
    elif dispatch_mode == "quorum" and not isinstance(review, Mapping):
        # Quorum still needs an aggregation rule, which is represented in the
        # review/assignment options rather than inferred from pool membership.
        raise TeamValidationError("quorum Assignment requires explicit review/aggregation policy")
    requested_policy = dict(requested_policy) if isinstance(requested_policy, Mapping) else {}
    team_policy = team.get("policy")
    target_layers: list[Mapping[str, Any] | tuple[str, Mapping[str, Any]]] = [
        ("team", dict(team_policy) if isinstance(team_policy, Mapping) else {}),
        ("manager", _manager_policy(team)),
    ]
    # Department, Profile, and Member policy are resolved per selected Member.
    # Fan-out Assignments carry one common snapshot only when all selected
    # Members resolve to the same effective policy; otherwise each Member gets
    # a deterministic child Assignment at the coordinator boundary.
    member_resolutions = []
    for member in selected:
        member_policy = member.get("policy")
        layers: list[Mapping[str, Any] | tuple[str, Mapping[str, Any]]] = [
            *target_layers,
            ("department", _department_policy(team, member)),
            ("profile", _profile_policy(member)),
            (
                f"member:{member['member_id']}",
                dict(member_policy) if isinstance(member_policy, Mapping) else {},
            ),
            ("assignment", requested_policy),
        ]
        member_resolutions.append(
            resolve_effective_policy(
                layers,
                available_models=available_models,
                available_harnesses=available_harnesses,
            )
        )
    first = member_resolutions[0]
    for other in member_resolutions[1:]:
        if other.effective != first.effective:
            raise TeamValidationError(
                "Fan-out target Members resolve different policies; create one Assignment per Member"
            )
    review_snapshot = _review_snapshot(team, first.effective, review)
    timestamp = now or _now()
    assignment = {
        "schema_version": ASSIGNMENT_SCHEMA_VERSION,
        "assignment_id": _required_id(assignment_id or _new_id("assignment_"), "assignment_id"),
        "team_id": str(team["team_id"]),
        "team_generation": int(team.get("generation") or 0),
        "target_kind": target_kind,
        "target_id": target_id,
        "dispatch_mode": dispatch_mode,
        "selected_member_ids": [str(member["member_id"]) for member in selected],
        "routing_reason": str((review or {}).get("routing_reason") or "explicit_target")[:1000]
        if isinstance(review, Mapping)
        else "explicit_target",
        "routing_snapshot": {
            "pool_id": target_id if target_kind == "member_pool" else None,
            "selected_member_ids": [str(member["member_id"]) for member in selected],
            "member_configuration": {
                str(member["member_id"]): member.get("configuration") for member in selected
            },
        },
        "member_profile_snapshots": {
            str(member["member_id"]): {
                "profile_id": str(member["profile_id"]),
                "adopted_profile_revision": int(member["adopted_profile_revision"]),
                "adopted_profile_hash": str(member["adopted_profile_hash"]),
            }
            for member in selected
        },
        "policy_snapshot": _copy(first.effective),
        "policy_resolution_trace": [_copy(item) for item in first.trace],
        "policy_snapshot_hash": canonical_hash(first.effective),
        "review_snapshot": review_snapshot,
        "input_snapshot_hash": str(input_snapshot_hash or "").strip() or None,
        "state": "queued",
        "revision": 1,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    assignment["assignment_hash"] = canonical_hash(
        {
            key: value
            for key, value in assignment.items()
            if key not in {"assignment_hash", "created_at", "updated_at"}
        }
    )
    return assignment


def create_attempt(
    team: Mapping[str, Any],
    assignment: Mapping[str, Any],
    *,
    member_id: str | None = None,
    attempt_number: int = 1,
    attempt_id: str | None = None,
    input_snapshot_hash: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Create one concrete Attempt with the Assignment policy frozen again."""

    if str(assignment.get("schema_version") or "") != ASSIGNMENT_SCHEMA_VERSION:
        raise TeamValidationError("Execution Attempt requires a canonical Assignment")
    if str(assignment.get("team_id") or "") != str(team.get("team_id") or ""):
        raise TeamValidationError("Assignment belongs to a different Team")
    validate_assignment_snapshot(assignment)
    if (
        isinstance(attempt_number, bool)
        or not isinstance(attempt_number, int)
        or attempt_number < 1
    ):
        raise TeamValidationError("attempt_number must be a positive integer")
    selected = [str(value) for value in assignment.get("selected_member_ids") or []]
    chosen = str(member_id or (selected[0] if selected else "")).strip()
    if chosen not in selected:
        raise TeamValidationError("Attempt member_id must be one selected by the Assignment")
    profile_snapshots = assignment.get("member_profile_snapshots")
    profile_snapshot = (
        profile_snapshots.get(chosen) if isinstance(profile_snapshots, Mapping) else None
    )
    if not isinstance(profile_snapshot, Mapping):
        raise SnapshotIntegrityError("Assignment is missing frozen Member Profile provenance")
    validate_policy_snapshot(
        assignment.get("policy_snapshot"), assignment.get("policy_snapshot_hash")
    )
    timestamp = now or _now()
    attempt = {
        "schema_version": ATTEMPT_SCHEMA_VERSION,
        "execution_attempt_id": _required_id(
            attempt_id or _new_id("attempt_"), "execution_attempt_id"
        ),
        "assignment_id": str(assignment["assignment_id"]),
        "team_id": str(team["team_id"]),
        "team_generation": int(assignment.get("team_generation") or 0),
        "member_id": chosen,
        "attempt_number": attempt_number,
        "status": "created",
        "input_snapshot_hash": str(
            input_snapshot_hash or assignment.get("input_snapshot_hash") or ""
        ).strip()
        or None,
        "policy_snapshot": _copy(assignment["policy_snapshot"]),
        "policy_resolution_trace": _copy(assignment.get("policy_resolution_trace") or []),
        "policy_snapshot_hash": str(assignment["policy_snapshot_hash"]),
        "review_snapshot": _copy(assignment.get("review_snapshot") or {}),
        "provenance": {
            "assignment_hash": str(assignment.get("assignment_hash") or ""),
            "profile_id": str(profile_snapshot.get("profile_id") or ""),
            "adopted_profile_revision": int(profile_snapshot.get("adopted_profile_revision") or 0),
            "adopted_profile_hash": str(profile_snapshot.get("adopted_profile_hash") or ""),
        },
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    attempt["attempt_hash"] = canonical_hash(
        {
            key: value
            for key, value in attempt.items()
            if key not in {"attempt_hash", "created_at", "updated_at"}
        }
    )
    return attempt


def validate_policy_snapshot(snapshot: Any, expected_hash: Any) -> None:
    """Verify that a policy snapshot has not drifted since it was frozen."""

    if not isinstance(snapshot, Mapping):
        raise SnapshotIntegrityError("Policy snapshot is missing")
    actual = canonical_hash(snapshot)
    expected = str(expected_hash or "")
    if actual != expected:
        raise SnapshotIntegrityError("Policy snapshot hash does not match frozen provenance")


def validate_assignment_snapshot(assignment: Mapping[str, Any]) -> None:
    """Verify all hash-bound Assignment fields before creating an Attempt."""

    if not isinstance(assignment, Mapping):
        raise SnapshotIntegrityError("Assignment snapshot is missing")
    expected = str(assignment.get("assignment_hash") or "")
    actual = canonical_hash(
        {
            key: value
            for key, value in assignment.items()
            if key not in {"assignment_hash", "created_at", "updated_at"}
        }
    )
    if actual != expected:
        raise SnapshotIntegrityError("Assignment hash does not match frozen provenance")


def can_accept_assignment(team: Mapping[str, Any], member_id: str) -> bool:
    """Return whether a Member may receive a new Assignment right now."""

    member = _member_map(team).get(str(member_id))
    if member is None:
        return False
    # ``disabled`` blocks new work but does not invalidate active Attempts.
    return member.get("configuration") == "enabled" and member.get("availability") == "idle"


def update_member_state(
    team: Mapping[str, Any],
    member_id: str,
    *,
    configuration: str | None = None,
    availability: str | None = None,
) -> dict[str, Any]:
    """Return a Team copy with one valid configuration/availability update."""

    updated = _copy(dict(team))
    members = _member_map(updated)
    member = members.get(str(member_id))
    if member is None:
        raise TeamValidationError(f"Unknown Member {member_id}")
    next_configuration = str(configuration or member.get("configuration") or "enabled").casefold()
    next_availability = str(availability or member.get("availability") or "offline").casefold()
    if next_configuration not in CONFIGURATION_STATES:
        raise TeamValidationError("Member configuration state is invalid")
    if next_availability not in AVAILABILITY_STATES:
        raise TeamValidationError("Member availability state is invalid")
    if next_configuration == "archived" and next_availability in {"assigned", "running"}:
        raise TeamValidationError("Archived Member cannot be assigned or running")
    for item in updated.get("members", []):
        if isinstance(item, dict) and item.get("member_id") == str(member_id):
            item["configuration"] = next_configuration
            item["availability"] = next_availability
            break
    updated["updated_at"] = _now()
    return updated


def settle_member_availability(
    team: Mapping[str, Any], member_id: str, *, blocked: bool = False
) -> dict[str, Any]:
    """Settle a Member after an Attempt without inventing a ``done`` state."""

    member = _member_map(team).get(str(member_id))
    if member is None:
        raise TeamValidationError(f"Unknown Member {member_id}")
    target = "blocked" if blocked else "idle"
    return update_member_state(team, str(member_id), availability=target)


def materialization_plan(
    current_team: Mapping[str, Any],
    definition: Mapping[str, Any],
    *,
    profiles: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    active_assignment_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build an explicit, reviewable Definition-to-Team materialization plan."""

    normalized = normalize_team_definition(definition, profiles=profiles)
    current_members = _member_map(current_team)
    next_members = {item["member_id"]: item for item in normalized["members"]}
    changes: list[dict[str, Any]] = []
    for member_id in sorted(set(current_members) | set(next_members)):
        old = current_members.get(member_id)
        new = next_members.get(member_id)
        if old is None:
            changes.append({"kind": "member_added", "member_id": member_id})
        elif new is None:
            changes.append({"kind": "member_removed", "member_id": member_id})
        elif old.get("adopted_profile_hash") != new.get("adopted_profile_hash") or old.get(
            "adopted_profile_revision"
        ) != new.get("adopted_profile_revision"):
            changes.append(
                {
                    "kind": "profile_changed",
                    "member_id": member_id,
                    "from": {
                        "revision": old.get("adopted_profile_revision"),
                        "hash": old.get("adopted_profile_hash"),
                    },
                    "to": {
                        "revision": new.get("adopted_profile_revision"),
                        "hash": new.get("adopted_profile_hash"),
                    },
                }
            )
        elif old != new:
            changes.append({"kind": "member_changed", "member_id": member_id})
    old_departments: dict[str, Mapping[str, Any]] = {
        str(item.get("department_id")): item
        for item in current_team.get("departments", [])
        if isinstance(item, Mapping)
    }
    next_departments = {item["department_id"]: item for item in normalized["departments"]}
    for department_id in sorted(set(old_departments) | set(next_departments)):
        if department_id not in old_departments:
            changes.append({"kind": "department_added", "department_id": department_id})
        elif department_id not in next_departments:
            changes.append({"kind": "department_removed", "department_id": department_id})
        elif old_departments[department_id] != next_departments[department_id]:
            changes.append({"kind": "department_changed", "department_id": department_id})
    old_pools = _pool_map(current_team)
    next_pools = {item["pool_id"]: item for item in normalized["member_pools"]}
    for pool_id in sorted(set(old_pools) | set(next_pools)):
        if pool_id not in old_pools:
            changes.append({"kind": "pool_added", "pool_id": pool_id})
        elif pool_id not in next_pools:
            changes.append({"kind": "pool_removed", "pool_id": pool_id})
        elif old_pools[pool_id] != next_pools[pool_id]:
            changes.append({"kind": "pool_changed", "pool_id": pool_id})
    active = sorted({str(value) for value in (active_assignment_ids or []) if str(value).strip()})
    changed_members = {
        item.get("member_id")
        for item in changes
        if item.get("kind") in {"member_removed", "profile_changed", "member_changed"}
    }
    impact = [
        {"assignment_id": assignment_id, "required_action": "drain_reassign_or_cancel"}
        for assignment_id in active
        if changed_members
    ]
    plan = {
        "schema_version": "tobkiri.team-materialization-plan/v1",
        "team_id": str(current_team.get("team_id") or ""),
        "from_generation": int(current_team.get("generation") or 0),
        "to_generation": int(current_team.get("generation") or 0) + 1,
        "from_definition_hash": str(current_team.get("team_definition_hash") or ""),
        "to_definition_hash": normalized["definition_hash"],
        "changes": changes,
        "active_work_impact": impact,
        "required_approval": bool(changes or impact),
        "approved": False,
        "definition": normalized,
    }
    plan["plan_hash"] = canonical_hash(
        {key: value for key, value in plan.items() if key not in {"plan_hash", "approved"}}
    )
    return plan


def apply_materialization_plan(
    current_team: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    profiles: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    approved: bool = False,
    active_work_strategy: str | None = None,
    actor_id: str = "system",
    now: str | None = None,
) -> dict[str, Any]:
    """Apply a materialization plan only after explicit approval/active-work handling."""

    if str(plan.get("schema_version") or "") != "tobkiri.team-materialization-plan/v1":
        raise ProfileAdoptionError("Invalid Team materialization plan")
    expected_hash = canonical_hash(
        {key: value for key, value in plan.items() if key not in {"plan_hash", "approved"}}
    )
    if expected_hash != str(plan.get("plan_hash") or ""):
        raise ProfileAdoptionError("Materialization plan has been modified")
    if plan.get("required_approval") and not approved:
        raise ProfileAdoptionError("Materialization plan requires explicit approval")
    impact = (
        plan.get("active_work_impact") if isinstance(plan.get("active_work_impact"), list) else []
    )
    if impact and active_work_strategy not in {"drain", "reassign", "cancel"}:
        raise ProfileAdoptionError(
            "Active work requires an explicit drain, reassign, or cancel strategy"
        )
    definition = plan.get("definition")
    if not isinstance(definition, Mapping):
        raise ProfileAdoptionError("Materialization plan is missing its validated Definition")
    result = materialize_team(
        definition,
        profiles=profiles,
        team_id=str(current_team.get("team_id") or ""),
        generation=int(plan.get("to_generation") or 0),
        actor_id=actor_id,
        now=now,
    )
    # Materialization changes organizational configuration, not the durable
    # history of work.  Existing Assignments/Attempts retain their original
    # profile and policy provenance until the explicit drain/reassign/cancel
    # strategy is completed by the Team Coordinator.
    result["assignments"] = _copy(current_team.get("assignments") or {})
    result["attempts"] = _copy(current_team.get("attempts") or {})
    result["materialized_from_generation"] = int(current_team.get("generation") or 0)
    result["materialization_plan_hash"] = str(plan["plan_hash"])
    result["active_work_strategy"] = active_work_strategy
    return result


def plan_profile_update(
    team: Mapping[str, Any],
    profiles: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    active_assignment_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Plan explicit adoption of newer Profile revisions for a Team."""

    definition = {
        "team_definition_id": team.get("team_definition_id"),
        "revision": int(team.get("team_definition_revision") or 1),
        "name": team.get("name"),
        "description": team.get("description"),
        "manager_member_id": team.get("manager_member_id"),
        "members": _copy(team.get("members") or []),
        "departments": _copy(team.get("departments") or []),
        "member_pools": _copy(team.get("member_pools") or []),
        "policy": _copy(team.get("policy") or {}),
        "metadata": _copy(team.get("metadata") or {}),
    }
    normalized_profiles = {
        profile["profile_id"]: profile
        for profile in (
            _profile_record(profiles, str(member.get("profile_id")))
            for member in team.get("members", [])
            if isinstance(member, Mapping)
        )
        if profile is not None
    }
    for member in definition["members"]:
        profile = _profile_record(profiles, str(member.get("profile_id") or ""))
        if profile is None:
            raise ProfileAdoptionError(f"Updated Profile {member.get('profile_id')} is missing")
        # Preserve the current adoption in the Definition until the plan is
        # explicitly applied; this makes the diff readable and approval-bound.
        member["adopted_profile_revision"] = profile["revision"]
        member["adopted_profile_hash"] = profile["content_hash"]
    plan = materialization_plan(
        team,
        definition,
        profiles=normalized_profiles or profiles,
        active_assignment_ids=active_assignment_ids,
    )
    plan["kind"] = "profile_adoption"
    plan["profile_updates"] = [
        change for change in plan["changes"] if change.get("kind") == "profile_changed"
    ]
    next_definition = plan["definition"]
    next_team = {
        **_copy(dict(team)),
        "members": _copy(next_definition["members"]),
        "departments": _copy(next_definition["departments"]),
        "member_pools": _copy(next_definition["member_pools"]),
        "manager_member_id": next_definition["manager_member_id"],
        "policy": _copy(next_definition["policy"]),
    }
    current_members = _member_map(team)
    next_members = _member_map(next_team)
    policy_diff = []
    for update in plan["profile_updates"]:
        member_id = str(update["member_id"])
        current_member = current_members[member_id]
        next_member = next_members[member_id]
        before = resolve_effective_policy(
            [
                ("team", team.get("policy") or {}),
                ("manager", _manager_policy(team)),
                ("department", _department_policy(team, current_member)),
                ("profile", _profile_policy(current_member)),
                (f"member:{member_id}", current_member.get("policy") or {}),
            ]
        )
        after = resolve_effective_policy(
            [
                ("team", next_team.get("policy") or {}),
                ("manager", _manager_policy(next_team)),
                ("department", _department_policy(next_team, next_member)),
                ("profile", _profile_policy(next_member)),
                (f"member:{member_id}", next_member.get("policy") or {}),
            ]
        )
        policy_diff.append(
            {
                "member_id": member_id,
                "profile_id": next_member["profile_id"],
                "from_policy_hash": canonical_hash(before.effective),
                "to_policy_hash": canonical_hash(after.effective),
                "from_effective_policy": _copy(before.effective),
                "to_effective_policy": _copy(after.effective),
                "changed": before.effective != after.effective,
            }
        )
    plan["policy_diff"] = policy_diff
    plan["active_work_impact"] = [
        {"assignment_id": str(value), "required_action": "old_snapshot_preserved"}
        for value in (active_assignment_ids or [])
    ]
    plan["required_approval"] = bool(plan["profile_updates"])
    plan["plan_hash"] = canonical_hash(
        {key: value for key, value in plan.items() if key not in {"plan_hash", "approved"}}
    )
    return plan


def adopt_profile_revision(
    team: Mapping[str, Any],
    member_id: str,
    profile: Mapping[str, Any],
    *,
    approved: bool = False,
    actor_id: str = "system",
    now: str | None = None,
) -> dict[str, Any]:
    """Explicitly materialize one Profile revision onto a Member."""

    if not approved:
        raise ProfileAdoptionError("Profile adoption requires explicit approval/materialization")
    normalized_profile = normalize_profile(profile)
    result = _copy(dict(team))
    target = _member_map(result).get(str(member_id))
    if target is None:
        raise ProfileAdoptionError(f"Unknown Member {member_id}")
    if str(target.get("profile_id") or "") != normalized_profile["profile_id"]:
        raise ProfileAdoptionError("Profile revision adoption cannot change a Member's profile_id")
    for member in result.get("members", []):
        if isinstance(member, dict) and member.get("member_id") == str(member_id):
            member["adopted_profile_revision"] = normalized_profile["revision"]
            member["adopted_profile_hash"] = normalized_profile["content_hash"]
            member["adopted_profile_policy"] = _copy(normalized_profile["policy"])
            member["adopted_profile_display_name"] = normalized_profile["display_name"]
            break
    result["generation"] = int(result.get("generation") or 0) + 1
    result["profile_generation"] = max(
        int(item.get("adopted_profile_revision") or 0)
        for item in result.get("members", [])
        if isinstance(item, Mapping)
    )
    result["updated_at"] = now or _now()
    result["provenance"] = {
        **(
            _copy(result.get("provenance")) if isinstance(result.get("provenance"), Mapping) else {}
        ),
        "last_profile_adoption": {
            "member_id": str(member_id),
            "profile_id": normalized_profile["profile_id"],
            "revision": normalized_profile["revision"],
            "hash": normalized_profile["content_hash"],
            "actor_id": str(actor_id or "system"),
            "at": result["updated_at"],
        },
    }
    return result


def team_console_snapshot(team: Mapping[str, Any]) -> dict[str, Any]:
    """Build a read-only Team Console projection with effective policy details."""

    members = _member_map(team)
    output_members: list[dict[str, Any]] = []
    for member in sorted(members.values(), key=lambda value: str(value.get("member_id"))):
        team_policy = team.get("policy")
        member_policy = member.get("policy")
        resolution = resolve_effective_policy(
            [
                ("team", dict(team_policy) if isinstance(team_policy, Mapping) else {}),
                ("manager", _manager_policy(team)),
                ("department", _department_policy(team, member)),
                ("profile", _profile_policy(member)),
                (
                    f"member:{member['member_id']}",
                    dict(member_policy) if isinstance(member_policy, Mapping) else {},
                ),
            ]
        )
        output_members.append(
            {
                "member_id": member["member_id"],
                "display_name": member["display_name"],
                "configuration": member["configuration"],
                "availability": member["availability"],
                "profile_id": member["profile_id"],
                "adopted_profile_revision": member["adopted_profile_revision"],
                "adopted_profile_hash": member["adopted_profile_hash"],
                "effective_policy": _copy(resolution.effective),
                "policy_snapshot_hash": canonical_hash(resolution.effective),
                "policy_resolution_trace": [_copy(item) for item in resolution.trace],
            }
        )
    return {
        "schema_version": "tobkiri.team-console/v1",
        "team_id": str(team.get("team_id") or ""),
        "team_definition_id": str(team.get("team_definition_id") or ""),
        "generation": int(team.get("generation") or 0),
        "profile_generation": int(team.get("profile_generation") or 0),
        "state": str(team.get("state") or ""),
        "manager_member_id": team.get("manager_member_id"),
        "members": output_members,
        "departments": _copy(team.get("departments") or []),
        "member_pools": _copy(team.get("member_pools") or []),
        "effective_team_policy": _copy(team.get("policy_snapshot") or {}),
        "team_policy_snapshot_hash": str(team.get("policy_snapshot_hash") or ""),
        "team_policy_resolution_trace": _copy(team.get("policy_resolution_trace") or []),
    }


def export_team(team: Mapping[str, Any]) -> dict[str, Any]:
    """Export a Team without exposing mutable references or hidden authority."""

    result = _copy(dict(team))
    result["export_schema_version"] = "tobkiri.team-export/v1"
    result["exported_at"] = _now()
    result["policy_provenance"] = {
        "team_policy_snapshot_hash": str(team.get("policy_snapshot_hash") or ""),
        "member_profile_adoptions": [
            {
                "member_id": member.get("member_id"),
                "profile_id": member.get("profile_id"),
                "revision": member.get("adopted_profile_revision"),
                "hash": member.get("adopted_profile_hash"),
            }
            for member in team.get("members", [])
            if isinstance(member, Mapping)
        ],
    }
    return result


def export_legacy_company(team: Mapping[str, Any]) -> dict[str, Any]:
    """Project canonical Team state to a compatibility-safe Company shape."""

    members = _member_map(team)
    agents = {}
    for member_id, member in members.items():
        agents[member_id] = {
            "id": member_id,
            "agent_id": member_id,
            "display_name": member.get("display_name") or member_id,
            "aliases": list(member.get("aliases") or []),
            "agent_profile_id": member.get("profile_id"),
            "profile_revision": member.get("adopted_profile_revision"),
            "profile_hash": member.get("adopted_profile_hash"),
            "enabled": member.get("configuration") == "enabled",
            "status": member.get("availability") or "offline",
            "metadata": {
                "team_member_id": member_id,
                "policy_provenance": {"profile_hash": member.get("adopted_profile_hash")},
            },
        }
    return {
        "id": str(team.get("team_id") or ""),
        "name": str(team.get("name") or "Team"),
        "description": str(team.get("description") or ""),
        "status": str(team.get("state") or "active"),
        "manager_member_id": team.get("manager_member_id"),
        "agents": agents,
        "departments": _copy(team.get("departments") or []),
        "member_pools": _copy(team.get("member_pools") or []),
        "settings": {
            "team_generation": team.get("generation"),
            "policy_snapshot_hash": team.get("policy_snapshot_hash"),
        },
        "metadata": {
            "source": "tobkiri.team/v1",
            "team_definition_hash": team.get("team_definition_hash"),
        },
    }


def migrate_legacy_company(
    legacy: Mapping[str, Any],
    *,
    profiles: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    team_definition_id: str | None = None,
) -> dict[str, Any]:
    """Import a legacy Company record while preserving stable IDs/provenance.

    Legacy records did not carry Profile revision hashes.  Such members are
    assigned a deterministic ``legacy:<digest>`` Profile reference and marked
    with migration provenance; callers may then replace it through an explicit
    Profile adoption plan.  No legacy role is treated as Manager authority.
    """

    if not isinstance(legacy, Mapping):
        raise TeamValidationError("Legacy Company state must be an object")
    company_id = _required_id(legacy.get("id") or legacy.get("company_id"), "legacy company id")
    raw_members = (
        legacy.get("members")
        if isinstance(legacy.get("members"), Mapping)
        else legacy.get("agents")
    )
    if isinstance(raw_members, Mapping):
        entries = []
        for member_id, value in raw_members.items():
            item = dict(value) if isinstance(value, Mapping) else {}
            item.setdefault("id", member_id)
            entries.append(item)
    elif isinstance(raw_members, list):
        entries = [dict(item) for item in raw_members if isinstance(item, Mapping)]
    else:
        entries = []
    migrated_members: list[dict[str, Any]] = []
    migration_profile_cache: dict[str, dict[str, Any]] = {}
    for entry in entries:
        member_id = _required_id(
            entry.get("member_id") or entry.get("agent_id") or entry.get("id"), "legacy member id"
        )
        profile_id = str(
            entry.get("profile_id")
            or entry.get("agent_profile_id")
            or entry.get("model")
            or "legacy-default"
        ).strip()
        if not _ID_PATTERN.fullmatch(profile_id):
            profile_id = (
                "legacy-profile-" + hashlib.sha256(profile_id.encode("utf-8")).hexdigest()[:24]
            )
        profile = _profile_record(profiles, profile_id)
        if profile is None:
            legacy_profile = {
                "profile_id": profile_id,
                "revision": 1,
                "display_name": profile_id,
                "policy": {},
                "metadata": {"migration_source": "legacy-company", "legacy_company_id": company_id},
            }
            profile = normalize_profile(legacy_profile)
        migration_profile_cache[profile_id] = profile
        migrated_members.append(
            {
                "member_id": member_id,
                "profile_id": profile["profile_id"],
                "adopted_profile_revision": profile["revision"],
                "adopted_profile_hash": profile["content_hash"],
                "display_name": entry.get("display_name") or entry.get("agent_name") or member_id,
                "aliases": entry.get("aliases") or entry.get("mentions") or [],
                "configuration": "enabled" if bool(entry.get("enabled", True)) else "disabled",
                "availability": _legacy_availability(
                    entry.get("availability") or entry.get("status")
                ),
                "policy": entry.get("policy") or {},
                "metadata": {"migration_source": "legacy-company", "legacy_member_id": member_id},
            }
        )
    requested_manager = str(legacy.get("manager_member_id") or "").strip()
    migrated_ids = {str(item["member_id"]): item for item in migrated_members}
    manager_id = (
        requested_manager
        if requested_manager in migrated_ids
        and migrated_ids[requested_manager]["configuration"] == "enabled"
        else None
    )
    definition = {
        "team_definition_id": team_definition_id or f"legacy:{company_id}",
        "revision": 1,
        "name": legacy.get("name") or company_id,
        "description": legacy.get("description") or "Migrated legacy Company",
        "manager_member_id": manager_id,
        "members": migrated_members,
        "departments": legacy.get("departments") or [],
        "member_pools": legacy.get("member_pools") or legacy.get("pools") or [],
        "policy": legacy.get("policy") or {},
        "metadata": {
            "migration_source": "legacy-company",
            "legacy_company_id": company_id,
            "legacy_source_hash": canonical_hash(legacy),
        },
    }
    normalized = normalize_team_definition(definition, profiles=migration_profile_cache)
    team = materialize_team(
        normalized,
        profiles=migration_profile_cache,
        team_id=company_id,
        actor_id="legacy-migration",
    )
    team["provenance"]["legacy_company_id"] = company_id
    team["provenance"]["legacy_source_hash"] = canonical_hash(legacy)
    if requested_manager and manager_id is None:
        team["provenance"]["legacy_manager_reference"] = {
            "requested": requested_manager,
            "preserved": False,
            "reason": "missing_or_disabled_member",
        }
    return team


def _legacy_availability(value: Any) -> str:
    """Map legacy agent status values to the finite Team availability enum."""

    status = str(value or "offline").strip().casefold()
    if status in AVAILABILITY_STATES:
        return status
    # Legacy Company records sometimes used ``done``/``completed`` for an
    # Agent.  Members are never work items, so a settled Agent is simply idle.
    if status in {"done", "completed", "complete", "finished", "success"}:
        return "idle"
    if status in {"error", "failed", "stale"}:
        return "blocked"
    return "offline"
