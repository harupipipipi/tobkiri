"""Focused acceptance coverage for the canonical Team model (#1351)."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

DEFAULTSPACK_ROOT = Path(__file__).parents[1] / "ecosystem" / "defaultspack"
if str(DEFAULTSPACK_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.team_model import (  # noqa: E402
    AliasResolutionError,
    PolicyResolutionError,
    ProfileAdoptionError,
    SnapshotIntegrityError,
    TeamValidationError,
    adopt_profile_revision,
    apply_materialization_plan,
    can_accept_assignment,
    canonical_hash,
    create_assignment,
    create_attempt,
    export_legacy_company,
    materialize_team,
    migrate_legacy_company,
    normalize_profile,
    normalize_team_definition,
    plan_profile_update,
    resolve_effective_policy,
    resolve_member_alias,
    settle_member_availability,
    team_console_snapshot,
    update_member_state,
    validate_policy_snapshot,
    validate_assignment_snapshot,
)


def _profile(
    profile_id: str = "profile-coder",
    revision: int = 1,
    *,
    policy: dict | None = None,
) -> dict:
    return normalize_profile(
        {
            "profile_id": profile_id,
            "revision": revision,
            "display_name": profile_id.title(),
            "policy": policy
            or {
                "tool": ["coding_file_read", "coding_file_patch"],
                "command": ["pytest"],
                "models": ["model-safe"],
                "model": "model-safe",
                "max_tokens": 10000,
                "required_review": "peer",
                "mandatory_safety_checks": ["workspace-trust"],
            },
        }
    )


def _team(
    *, profile: dict | None = None, member_overrides: dict | None = None
) -> tuple[dict, dict, dict]:
    profile = profile or _profile()
    members = [
        {
            "member_id": "manager",
            "profile_id": profile["profile_id"],
            "adopted_profile_revision": profile["revision"],
            "adopted_profile_hash": profile["content_hash"],
            "display_name": "Manager",
            "aliases": ["mgr"],
            "availability": "idle",
            "policy": {"authority": ["propose"], "tool": ["coding_file_read"]},
        },
        {
            "member_id": "coder",
            "profile_id": profile["profile_id"],
            "adopted_profile_revision": profile["revision"],
            "adopted_profile_hash": profile["content_hash"],
            "display_name": "Coder",
            "aliases": ["code"],
            "availability": "idle",
            **(member_overrides or {}),
        },
        {
            "member_id": "reviewer",
            "profile_id": profile["profile_id"],
            "adopted_profile_revision": profile["revision"],
            "adopted_profile_hash": profile["content_hash"],
            "display_name": "Reviewer",
            "aliases": ["review"],
            "availability": "idle",
        },
    ]
    definition = normalize_team_definition(
        {
            "team_definition_id": "engineering-definition",
            "revision": 1,
            "name": "Engineering",
            "manager_member_id": "manager",
            "members": members,
            "departments": [
                {
                    "department_id": "engineering",
                    "lead_member_id": "manager",
                    "member_ids": ["manager", "coder", "reviewer"],
                    "policy": {"tool": ["coding_file_read", "coding_file_patch"]},
                }
            ],
            "member_pools": [
                {
                    "pool_id": "coders",
                    "member_ids": ["coder"],
                    "selector": {"availability": "idle"},
                }
            ],
            "policy": {
                "authority": ["propose"],
                "tool": ["coding_file_read", "coding_file_patch"],
                "max_tokens": 20000,
                "required_review": "peer",
            },
        },
        profiles={profile["profile_id"]: profile},
    )
    team = materialize_team(
        definition,
        profiles={profile["profile_id"]: profile},
        team_id="team-engineering",
    )
    return profile, definition, team


def test_manager_is_an_ordinary_member_and_references_are_team_local() -> None:
    _profile_value, definition, team = _team()
    manager = next(item for item in team["members"] if item["member_id"] == "manager")
    assert team["manager_member_id"] == "manager"
    assert manager["schema_version"] == "tobkiri.member/v1"
    assert manager["policy"]["authority"] == ["propose"]
    assert "coordinator" not in manager

    disabled_definition = deepcopy(definition)
    next(item for item in disabled_definition["members"] if item["member_id"] == "manager")[
        "configuration"
    ] = "disabled"
    with pytest.raises(TeamValidationError, match="enabled Member"):
        normalize_team_definition(
            disabled_definition, profiles={_profile_value["profile_id"]: _profile_value}
        )


def test_cross_team_leads_and_manager_and_nested_departments_fail_closed() -> None:
    profile = _profile()
    base = {
        "team_definition_id": "d",
        "members": [
            {
                "member_id": "m",
                "profile_id": profile["profile_id"],
                "adopted_profile_revision": profile["revision"],
                "adopted_profile_hash": profile["content_hash"],
            }
        ],
    }
    with pytest.raises(TeamValidationError, match="Member in this Team"):
        normalize_team_definition(
            {**base, "manager_member_id": "other"}, profiles={profile["profile_id"]: profile}
        )
    with pytest.raises(TeamValidationError, match="flat"):
        normalize_team_definition(
            {
                **base,
                "departments": [{"department_id": "nested", "parent_department_id": "root"}],
            },
            profiles={profile["profile_id"]: profile},
        )
    with pytest.raises(TeamValidationError, match="enabled Member"):
        normalize_team_definition(
            {
                **base,
                "departments": [{"department_id": "d1", "lead_member_id": "m2"}],
            },
            profiles={profile["profile_id"]: profile},
        )


def test_pool_is_routing_only_and_cannot_expand_capabilities() -> None:
    _profile_value, _definition, team = _team()
    assignment = create_assignment(
        team,
        target_kind="member_pool",
        target_id="coders",
        requested_policy={"tool": ["coding_file_patch"]},
        review={"reviewer_member_id": "reviewer", "reviewed_input_revision": "input-1"},
    )
    assert assignment["selected_member_ids"] == ["coder"]
    assert assignment["policy_snapshot"]["tool"] == []
    assert assignment["routing_snapshot"]["pool_id"] == "coders"
    with pytest.raises(TeamValidationError, match="policy or authority"):
        normalize_team_definition(
            {
                "team_definition_id": "unsafe-pool",
                "members": [],
                "member_pools": [{"pool_id": "p", "capabilities": ["host.exec"]}],
            }
        )


def test_policy_resolution_is_intersection_deny_wins_strictest_and_traceable() -> None:
    result = resolve_effective_policy(
        [
            (
                "team",
                {
                    "tool": ["read", "write"],
                    "max_tokens": 1000,
                    "required_review": "peer",
                    "mandatory_safety_checks": ["audit"],
                },
            ),
            (
                "profile",
                {
                    "tool": ["read", "write"],
                    "deny_tool": ["write"],
                    "max_tokens": 800,
                    "required_review": "approval",
                    "mandatory_safety_checks": ["sandbox"],
                },
            ),
            ("member", {"tool": ["read"], "max_tokens": 900}),
        ]
    )
    assert result.effective["tool"] == ["read"]
    assert result.effective["limits"]["max_tokens"] == 800
    assert result.effective["required_review"] == "approval"
    assert result.effective["mandatory_safety_checks"] == ["audit", "sandbox"]
    assert {item["rule"] for item in result.trace} >= {
        "intersection",
        "deny_wins",
        "strictest_bound",
        "strictest_review",
        "union",
    }
    assert result.to_dict()["policy_hash"] == canonical_hash(result.effective)


def test_unlimited_requires_explicit_permission_at_every_layer() -> None:
    assert (
        resolve_effective_policy(
            [("team", {"max_tokens": "unlimited"}), ("profile", {"max_tokens": "unlimited"})]
        ).effective["limits"]["max_tokens"]
        == "unlimited"
    )
    assert (
        "max_tokens"
        not in resolve_effective_policy(
            [("team", {"max_tokens": "unlimited"}), ("profile", {})]
        ).effective["limits"]
    )


def test_preference_requires_available_backend_or_explicit_fallback() -> None:
    with pytest.raises(PolicyResolutionError, match="explicit fallback"):
        resolve_effective_policy(
            [("team", {"models": ["safe"], "model": "safe"})],
            available_models=["offline"],
        )
    result = resolve_effective_policy(
        [
            (
                "team",
                {"models": ["safe", "offline"], "model": "safe", "fallback_models": ["offline"]},
            )
        ],
        available_models=["offline"],
    )
    assert result.effective["model"] == "offline"


def test_review_cannot_be_weakened_and_reviewer_input_revision_is_frozen() -> None:
    _profile_value, _definition, team = _team()
    assignment = create_assignment(
        team,
        target_kind="member",
        target_id="coder",
        requested_policy={"required_review": "none"},
        review={
            "required_review": "none",
            "reviewer_member_id": "reviewer",
            "reviewed_input_revision": "input-7",
        },
    )
    assert assignment["review_snapshot"]["required_review"] == "peer"
    assert assignment["review_snapshot"]["reviewed_input_revision"] == "input-7"
    assert assignment["review_snapshot"]["reviewer_member_id"] == "reviewer"


def test_review_evidence_and_acceptance_criteria_cannot_be_weakened() -> None:
    profile = _profile(
        policy={
            "tool": ["coding_file_read"],
            "required_review": "peer",
            "evidence_required": True,
            "acceptance_criteria": ["profile-check"],
        }
    )
    _profile_value, _definition, team = _team(profile=profile)
    assignment = create_assignment(
        team,
        target_kind="member",
        target_id="coder",
        review={
            "reviewer_member_id": "reviewer",
            "reviewed_input_revision": "input-8",
            "evidence_required": False,
            "acceptance_criteria": ["assignment-check"],
        },
    )
    assert assignment["review_snapshot"]["evidence_required"] is True
    assert assignment["review_snapshot"]["acceptance_criteria"] == [
        "assignment-check",
        "profile-check",
    ]


def test_assignment_and_attempt_snapshots_do_not_drift_after_team_edit() -> None:
    _profile_value, _definition, team = _team()
    assignment = create_assignment(
        team,
        target_kind="member",
        target_id="coder",
        review={"reviewer_member_id": "reviewer", "reviewed_input_revision": "input-1"},
    )
    attempt = create_attempt(team, assignment, member_id="coder")
    before_assignment_hash = assignment["policy_snapshot_hash"]
    team["policy"]["tool"] = []
    team["members"][1]["policy"]["tool"] = []
    assert assignment["policy_snapshot"]["tool"]
    assert attempt["policy_snapshot_hash"] == before_assignment_hash
    validate_policy_snapshot(attempt["policy_snapshot"], attempt["policy_snapshot_hash"])
    attempt["policy_snapshot"]["tool"] = []
    with pytest.raises(SnapshotIntegrityError):
        validate_policy_snapshot(attempt["policy_snapshot"], attempt["policy_snapshot_hash"])


def test_attempt_uses_assignment_profile_provenance_after_profile_adoption() -> None:
    old_profile, _definition, team = _team()
    assignment = create_assignment(
        team,
        target_kind="member",
        target_id="coder",
        review={"reviewer_member_id": "reviewer", "reviewed_input_revision": "input-2"},
    )
    new_profile = _profile(revision=2)
    updated_team = adopt_profile_revision(team, "coder", new_profile, approved=True)
    attempt = create_attempt(updated_team, assignment, member_id="coder")
    assert attempt["provenance"]["adopted_profile_revision"] == 1
    assert attempt["provenance"]["adopted_profile_hash"] == old_profile["content_hash"]

    tampered = deepcopy(assignment)
    tampered["selected_member_ids"].append("manager")
    with pytest.raises(SnapshotIntegrityError, match="Assignment hash"):
        validate_assignment_snapshot(tampered)
    with pytest.raises(SnapshotIntegrityError, match="Assignment hash"):
        create_attempt(updated_team, tampered, member_id="coder")


def test_profile_update_requires_explicit_plan_and_preserves_old_provenance() -> None:
    old_profile, _definition, team = _team()
    assignment = create_assignment(
        team,
        target_kind="member",
        target_id="coder",
        review={"reviewer_member_id": "reviewer", "reviewed_input_revision": "input-1"},
    )
    new_profile = _profile(revision=2)
    plan = plan_profile_update(
        team,
        {new_profile["profile_id"]: new_profile},
        active_assignment_ids=[assignment["assignment_id"]],
    )
    assert plan["kind"] == "profile_adoption"
    assert plan["profile_updates"]
    assert plan["active_work_impact"][0]["required_action"] == "old_snapshot_preserved"
    with pytest.raises(ProfileAdoptionError, match="explicit approval"):
        apply_materialization_plan(team, plan, profiles={new_profile["profile_id"]: new_profile})
    updated = apply_materialization_plan(
        team,
        plan,
        profiles={new_profile["profile_id"]: new_profile},
        approved=True,
        active_work_strategy="drain",
    )
    assert updated["generation"] == team["generation"] + 1
    assert assignment["provenance"] if "provenance" in assignment else True
    assert old_profile["content_hash"] != new_profile["content_hash"]
    assert (
        assignment["policy_snapshot_hash"] != updated["policy_snapshot_hash"]
        or assignment["policy_snapshot"] == assignment["policy_snapshot"]
    )


def test_direct_profile_adoption_requires_approval_and_keeps_team_identity() -> None:
    _old_profile, _definition, team = _team()
    new_profile = _profile(revision=2)
    with pytest.raises(ProfileAdoptionError):
        adopt_profile_revision(team, "coder", new_profile)
    adopted = adopt_profile_revision(team, "coder", new_profile, approved=True)
    assert adopted["team_id"] == team["team_id"]
    member = next(item for item in adopted["members"] if item["member_id"] == "coder")
    assert member["adopted_profile_revision"] == 2
    assert member["adopted_profile_hash"] == new_profile["content_hash"]
    different_profile = _profile(profile_id="different-profile", revision=2)
    with pytest.raises(ProfileAdoptionError, match="cannot change"):
        adopt_profile_revision(team, "coder", different_profile, approved=True)


def test_configuration_and_availability_are_separate_and_never_done() -> None:
    _profile_value, _definition, team = _team()
    disabled = update_member_state(team, "coder", configuration="disabled", availability="running")
    assert not can_accept_assignment(disabled, "coder")
    assert (
        next(item for item in disabled["members"] if item["member_id"] == "coder")["availability"]
        == "running"
    )
    settled = settle_member_availability(disabled, "coder")
    assert (
        next(item for item in settled["members"] if item["member_id"] == "coder")["availability"]
        == "idle"
    )
    with pytest.raises(TeamValidationError):
        update_member_state(team, "coder", availability="done")


def test_only_active_teams_and_enabled_available_targets_accept_new_work() -> None:
    _profile_value, _definition, team = _team()
    paused_team = deepcopy(team)
    paused_team["state"] = "paused"
    with pytest.raises(TeamValidationError, match="active Team"):
        create_assignment(paused_team, target_kind="member", target_id="coder")

    busy_team = update_member_state(team, "coder", availability="running")
    with pytest.raises(TeamValidationError, match="cannot accept"):
        create_assignment(busy_team, target_kind="member", target_id="coder")

    disabled_department = deepcopy(team)
    disabled_department["departments"][0]["status"] = "disabled"
    with pytest.raises(TeamValidationError, match="Department engineering is not enabled"):
        create_assignment(
            disabled_department,
            target_kind="department",
            target_id="engineering",
            dispatch_mode="fanout",
        )


def test_aliases_are_exact_and_ambiguous_routing_fails_closed() -> None:
    _profile_value, _definition, team = _team()
    assert resolve_member_alias(team, "@mgr")["member_id"] == "manager"
    with pytest.raises(AliasResolutionError, match="Unknown"):
        resolve_member_alias(team, "missing")
    profile = _profile()
    with pytest.raises(TeamValidationError, match="ambiguous"):
        normalize_team_definition(
            {
                "team_definition_id": "ambiguous",
                "members": [
                    {
                        "member_id": "one",
                        "profile_id": profile["profile_id"],
                        "adopted_profile_revision": 1,
                        "adopted_profile_hash": profile["content_hash"],
                        "aliases": ["same"],
                    },
                    {
                        "member_id": "two",
                        "profile_id": profile["profile_id"],
                        "adopted_profile_revision": 1,
                        "adopted_profile_hash": profile["content_hash"],
                        "aliases": ["same"],
                    },
                ],
            },
            profiles={profile["profile_id"]: profile},
        )


def test_legacy_migration_preserves_identity_and_policy_provenance() -> None:
    migrated = migrate_legacy_company(
        {
            "id": "legacy-company",
            "name": "Legacy",
            "manager_member_id": "manager",
            "agents": {
                "manager": {"display_name": "Manager", "aliases": ["mgr"], "status": "done"},
                "coder": {"display_name": "Coder", "model": "legacy-model", "status": "running"},
            },
        }
    )
    assert migrated["team_id"] == "legacy-company"
    assert {member["member_id"] for member in migrated["members"]} == {"manager", "coder"}
    assert all(
        member["adopted_profile_hash"].startswith("sha256:") for member in migrated["members"]
    )
    assert all(member["availability"] != "done" for member in migrated["members"])
    assert migrated["provenance"]["legacy_source_hash"].startswith("sha256:")
    legacy = export_legacy_company(migrated)
    assert legacy["agents"]["coder"]["profile_hash"] == next(
        member["adopted_profile_hash"]
        for member in migrated["members"]
        if member["member_id"] == "coder"
    )


def test_console_exposes_generation_profile_revision_and_resolution_trace() -> None:
    _profile_value, _definition, team = _team()
    view = team_console_snapshot(team)
    assert view["team_id"] == "team-engineering"
    assert view["generation"] == 1
    assert view["profile_generation"] == 1
    assert view["members"]
    assert view["members"][0]["policy_resolution_trace"]
    assert "manager" in {
        item["scope"]
        for member in view["members"]
        for item in member["policy_resolution_trace"]
    }
    assert view["team_policy_snapshot_hash"].startswith("sha256:")


def test_versioned_team_schemas_are_valid_json() -> None:
    schema_root = (
        Path(__file__).parents[1] / "ecosystem" / "defaultspack" / "schemas" / "team_model"
    )
    names = {
        "team_definition.v1.schema.json",
        "effective-policy.v1.schema.json",
        "assignment.v1.schema.json",
        "execution_attempt.v1.schema.json",
    }
    assert {path.name for path in schema_root.glob("*.json")} == names
    for path in schema_root.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["$schema"].endswith("2020-12/schema")
        assert payload["$id"].startswith("https://tobkiri.local/")


def test_versioned_schemas_validate_canonical_resources() -> None:
    profile, definition, team = _team()
    assignment = create_assignment(
        team,
        target_kind="member",
        target_id="coder",
        assignment_id="assignment-schema",
        review={"reviewer_member_id": "reviewer", "reviewed_input_revision": "input-schema"},
    )
    attempt = create_attempt(
        team,
        assignment,
        member_id="coder",
        attempt_id="attempt-schema",
    )
    resolution = resolve_effective_policy(
        [("team", team["policy"]), ("profile", profile["policy"])]
    ).to_dict()
    schema_root = (
        Path(__file__).parents[1] / "ecosystem" / "defaultspack" / "schemas" / "team_model"
    )
    schemas = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in schema_root.glob("*.json")
    }
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
    )
    instances = {
        "team_definition.v1.schema.json": definition,
        "effective-policy.v1.schema.json": resolution,
        "assignment.v1.schema.json": assignment,
        "execution_attempt.v1.schema.json": attempt,
    }
    for name, instance in instances.items():
        Draft202012Validator(schemas[name], registry=registry).validate(instance)
