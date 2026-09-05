from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUMI_PKG = ROOT / "tobkiri_runtime"
if str(RUMI_PKG) not in sys.path:
    sys.path.insert(0, str(RUMI_PKG))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core_runtime.operating_profile import (  # noqa: E402
    OperatingProfilePlanStore,
    compile_operating_profile,
    get_builtin_operating_profiles,
    meet_level,
    meet_policy,
    policy_within,
    simulate_scenarios,
)
from core_runtime.operating_profile.constants import MUTATING_ACTION_IDS  # noqa: E402
from core_runtime.profile_workspace import ProfileWorkspaceManager  # noqa: E402


def test_compile_is_deterministic_and_builtin_presets_exist():
    answers = {
        "profile_id": "p1",
        "preset": "balanced_local",
        "occupation": "software_engineer",
        "actions": {"terminal": "ask", "external_send": "deny"},
    }

    first = compile_operating_profile(answers).to_dict()
    second = compile_operating_profile(dict(reversed(list(answers.items())))).to_dict()

    assert first == second
    builtins = get_builtin_operating_profiles()
    assert {"discussion_only", "balanced_local", "max_local_autonomy"} <= set(builtins)
    assert builtins["discussion_only"].policy.level_for("local_write").value == "deny"
    assert builtins["max_local_autonomy"].policy.level_for("external_send").value == "deny"


def test_lattice_meet_and_occupation_never_widens_permissions():
    assert meet_level("allow", "ask").value == "ask"
    met = meet_policy({"local_write": "allow", "external_send": "deny"}, {"local_write": "ask", "external_send": "allow"})
    assert met.level_for("local_write").value == "ask"
    assert met.level_for("external_send").value == "deny"

    discussion = compile_operating_profile(
        {"profile_id": "p2", "preset": "discussion_only", "occupation": "software_engineer"}
    )
    assert discussion.policy.level_for("local_write").value == "deny"

    child = compile_operating_profile({"profile_id": "p3", "preset": "max_local_autonomy", "occupation": "child"})
    assert child.policy.level_for("terminal").value == "deny"
    assert child.policy.level_for("external_send").value == "deny"


def test_explicit_answers_override_preset_defaults_but_not_ceilings():
    profile = compile_operating_profile(
        {
            "profile_id": "p2b",
            "preset": "balanced_local",
            "actions": {"local_write": "allow", "terminal": "allow"},
        },
        system_ceiling={"terminal": "ask"},
    )

    assert profile.policy.level_for("local_write").value == "allow"
    assert profile.policy.level_for("terminal").value == "ask"


def test_max_local_autonomy_does_not_allow_git_merge_by_default():
    profile = compile_operating_profile({"profile_id": "p2m", "preset": "max_local_autonomy"})

    assert profile.policy.level_for("git_push").value == "ask"
    assert profile.policy.level_for("git_merge").value == "ask"


def test_operating_profile_persists_multidimensional_model_and_split_git_policy():
    profile = compile_operating_profile(
        {
            "profile_id": "p2c",
            "preset": "max_local_autonomy",
            "use_cases": {"coding": True, "automation": True},
            "phase_autonomy": {"plan": "allow", "implement": "ask", "publish": "ask"},
            "responsibility_matrix": {
                "frontend": {"assistant": "implement", "user": "review"},
                "backend": {"assistant": "implement", "user": "review"},
            },
            "review_topology": {"required_for": ["git_push"], "reviewers": ["local_user"]},
            "privacy_policy": {"mode": "local_first", "redact_secrets": True},
            "memory_policy": {"mode": "project_summaries", "retention": "profile_scoped"},
            "skill_learning_policy": {"enabled": True, "review_required": True},
            "budget_policy": {"context_tokens": 12000, "max_parallel_actions": 2},
            "project_overrides": {"repo-a": {"terminal": "ask"}},
            "actions": {"git_commit": "allow", "git_push": "ask", "git_merge": "allow"},
        }
    )

    data = profile.to_dict()
    assert data["use_cases"] == {"automation": True, "coding": True}
    assert data["phase_autonomy"]["implement"] == "ask"
    assert data["responsibility_matrix"]["frontend"]["assistant"] == "implement"
    assert data["review_topology"]["required_for"] == ["git_push"]
    assert data["privacy_policy"]["redact_secrets"] is True
    assert data["memory_policy"]["mode"] == "project_summaries"
    assert data["skill_learning_policy"]["enabled"] is True
    assert data["budget_policy"]["context_tokens"] == 12000
    assert data["project_overrides"]["repo-a"]["terminal"] == "ask"
    assert profile.policy.level_for("git_write").value == "allow"
    assert profile.policy.level_for("git_commit").value == "allow"
    assert profile.policy.level_for("git_push").value == "ask"
    assert profile.policy.level_for("git_merge").value == "allow"


def test_malicious_pack_recommendation_cannot_widen_answers_or_system_ceiling():
    profile = compile_operating_profile(
        {
            "profile_id": "p4",
            "preset": "max_local_autonomy",
            "actions": {"external_send": "deny"},
        },
        pack_recommendations=[
            {
                "pack_id": "evil_pack",
                "actions": {
                    "external_send": "allow",
                    "terminal": "allow",
                    "local_write": "allow",
                    "__proto__": "allow",
                },
            },
            {"pack_id": "../escape", "actions": {"computer_control": "allow"}},
        ],
        system_ceiling={"terminal": "ask", "external_send": "deny"},
    )

    assert profile.policy.level_for("external_send").value == "deny"
    assert profile.policy.level_for("terminal").value == "ask"
    assert profile.policy.level_for("local_write").value == "allow"
    assert profile.recommended_packs == ["evil_pack"]
    assert any(
        diagnostic["code"] == "pack_contract.pack_id"
        for event in profile.provenance
        if event["source"] == "pack_contract"
        for diagnostic in event["detail"]["diagnostics"]
    )


def test_discussion_only_blocks_mutations_and_max_local_does_not_send_externally():
    discussion = compile_operating_profile({"profile_id": "p5", "preset": "discussion_only"})
    for action_id in sorted(MUTATING_ACTION_IDS):
        assert discussion.policy.level_for(action_id).value == "deny"
    assert discussion.policy.level_for("external_send").value == "deny"

    max_local = compile_operating_profile({"profile_id": "p6", "preset": "max_local_autonomy"})
    assert max_local.policy.level_for("local_write").value == "allow"
    assert max_local.policy.level_for("terminal").value == "allow"
    assert max_local.policy.level_for("external_send").value == "deny"


def test_child_profile_cannot_widen_parent():
    parent = compile_operating_profile({"profile_id": "parent", "preset": "discussion_only"})
    child = compile_operating_profile(
        {"profile_id": "child", "preset": "max_local_autonomy"},
        parent_profile=parent,
    )

    assert child.policy.level_for("local_write").value == "deny"
    assert child.policy.level_for("terminal").value == "deny"
    assert policy_within(child.policy, parent.policy)


def test_scenario_simulator_returns_coding_and_daily_scenarios():
    profile = compile_operating_profile({"profile_id": "p7", "preset": "balanced_local"})
    scenarios = {scenario.scenario_id: scenario.to_dict() for scenario in simulate_scenarios(profile)}

    assert {"coding", "daily"} <= set(scenarios)
    assert "terminal" in scenarios["coding"]["approval_required"]
    assert "external_send" in scenarios["daily"]["blocked"]


def test_signed_plan_apply_and_undo_persist_profile_scoped_files(tmp_path: Path):
    manager = ProfileWorkspaceManager(tmp_path)
    store = OperatingProfilePlanStore(manager)
    initial = compile_operating_profile({"profile_id": "p8", "preset": "discussion_only"})
    first_plan = store.create_plan("p8", initial, reason="initial")
    assert first_plan["signature"].startswith("hmac-sha256:")
    assert first_plan["expires_at"] > first_plan["issued_at"]
    assert first_plan["settings_revision"]
    assert first_plan["pack_digest"]
    assert len(first_plan["input_hash"]) == 64
    store.apply_plan(first_plan)

    target = compile_operating_profile({"profile_id": "p8", "preset": "max_local_autonomy"})
    second_plan = store.create_plan("p8", target, reason="raise local autonomy")
    tampered = dict(second_plan)
    tampered["reason"] = "tampered"
    with pytest.raises(ValueError):
        store.apply_plan(tampered)

    expired = store.create_plan("p8", target, reason="expired", expires_in_seconds=-1)
    with pytest.raises(ValueError, match="expired"):
        store.apply_plan(expired)

    stale_initial = compile_operating_profile({"profile_id": "p8_stale", "preset": "discussion_only"})
    store.apply_plan(store.create_plan("p8_stale", stale_initial, reason="stale initial"))
    stale_target = compile_operating_profile({"profile_id": "p8_stale", "preset": "max_local_autonomy"})
    stale_plan = store.create_plan("p8_stale", stale_target, reason="stale revision")
    stale_alternate = compile_operating_profile({"profile_id": "p8_stale", "preset": "balanced_local"})
    store.apply_plan(store.create_plan("p8_stale", stale_alternate, reason="intermediate"))
    with pytest.raises(ValueError, match="settings revision"):
        store.apply_plan(stale_plan)

    apply_result = store.apply_plan(second_plan)
    active_path = Path(apply_result["path"])
    profile_root = tmp_path / "workspaces" / "p8"
    assert active_path == profile_root / "operating_profile" / "active.json"
    assert active_path.is_file()

    reloaded = OperatingProfilePlanStore(ProfileWorkspaceManager(tmp_path))
    assert reloaded.load_active_profile("p8").preset_id == "max_local_autonomy"  # type: ignore[union-attr]

    undo_result = reloaded.undo_plan("p8", second_plan["plan_id"])
    assert Path(undo_result["path"]) == active_path
    restored = reloaded.load_active_profile("p8")
    assert restored is not None
    assert restored.preset_id == "discussion_only"
    assert (profile_root / "operating_profile" / "last_undo.json").is_file()
