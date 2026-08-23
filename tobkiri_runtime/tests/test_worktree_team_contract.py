from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from core_runtime.worktree_team_contract import (
    CONTRACT_VERSION,
    WorktreeContractError,
    WorktreeTeamLedger,
    normalize_task_request,
    worktree_task_preset,
)


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
EVIDENCE_SHA = "d" * 64


def request(task_id: str, *, file: str = "src/owned.py", harness: str = "native") -> dict:
    value = {
        "task_id": task_id,
        "parent_id": "run-parent",
        "pm_id": "pm-orion",
        "role": "implementation",
        "model_policy": {"class": "implementation", "reasoning": "high"},
        "starting": {
            "commit_sha": SHA_A,
            "tree_sha": SHA_B,
            "ordered_parents": [SHA_C],
            "clean": True,
        },
        "ownership": {
            "files": [file],
            "semantic_fields": [f"contract:{task_id}"],
            "collision_globs": ["docs/worktree-team/**"],
        },
        "dependencies": [],
        "required_predecessor_pass": [],
        "estimates": {"checkout_bytes": 1024, "output_bytes": 2048},
        "attempt_budgets": {"commit": 1, "build": 1, "package": 1, "gui": 1, "push": 1},
        "forbidden_capabilities": ["credential.read"],
        "forbidden_paths": ["private/**"],
        "required_gates": ["source", "tests", "review"],
        "required_evidence": [],
        "harness": {"kind": harness},
    }
    if harness == "external":
        value["harness"]["adapter_id"] = "external.codex-cli"
    return value


def pass_task(ledger: WorktreeTeamLedger, task_id: str) -> dict:
    for gate in ("source", "tests", "review"):
        ledger.record_gate(task_id, gate, "PASS", evidence_refs=[f"evidence:{gate}"])
    return ledger.complete(
        task_id,
        {
            "overall": "PASS",
            "output": {
                "commit_sha": SHA_B,
                "tree_sha": SHA_C,
                "ordered_parents": [SHA_A],
                "clean": True,
            },
            "changed_files": ["src/owned.py"],
            "changed_fields": [f"contract:{task_id}"],
            "commands": [{"argv": ["pytest", "-q"], "exit_code": 0}],
            "evidence": [{"kind": "tests", "sha256": EVIDENCE_SHA, "location": "artifacts/tests.json"}],
        },
    )


def test_normalized_manifest_is_exposed_and_schema_valid() -> None:
    manifest = normalize_task_request(request("task-schema", harness="external"))
    schema_path = Path(__file__).parents[1] / "tobkiri_protocol" / "schemas" / "worktree_team_task_v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(manifest)
    assert manifest["contract_version"] == CONTRACT_VERSION
    assert manifest["harness"] == {"kind": "external", "adapter_id": "external.codex-cli"}
    assert manifest["stop"] == {"first_material_blocker": True, "blind_retry": False}
    assert manifest["contract_digest"].startswith("sha256:")
    from tobkiri_protocol.validation import validate_document

    validate_document(manifest, "worktree_team_task")


def test_vendor_neutral_presets_cover_implementation_review_and_one_shot_work() -> None:
    implementation = worktree_task_preset("one_commit_implementation")
    review = worktree_task_preset("read_only_adversarial_review")
    one_shot = worktree_task_preset("one_shot_package_gui")
    assert implementation["attempt_budgets"]["commit"] == 1
    assert set(review["attempt_budgets"].values()) == {0}
    assert one_shot["attempt_budgets"]["package"] == one_shot["attempt_budgets"]["gui"] == 1
    assert "provider" not in json.dumps([implementation, review, one_shot]).lower()


def test_manifest_rejects_dirty_start_vendor_pin_and_credentials() -> None:
    dirty = request("task-dirty")
    dirty["starting"]["clean"] = False
    with pytest.raises(WorktreeContractError, match="clean state") as dirty_error:
        normalize_task_request(dirty)
    assert dirty_error.value.code == "DIRTY_START"

    vendor = request("task-vendor")
    vendor["model_policy"]["provider"] = "specific-vendor"
    with pytest.raises(WorktreeContractError) as vendor_error:
        normalize_task_request(vendor)
    assert vendor_error.value.code == "MODEL_VENDOR_PINNED"

    secret = request("task-secret")
    secret["api_key"] = "must-not-enter-ledger"
    with pytest.raises(WorktreeContractError) as secret_error:
        normalize_task_request(secret)
    assert secret_error.value.code == "SENSITIVE_INPUT_FORBIDDEN"


def test_concurrent_duplicate_claim_allows_exactly_one_admission(tmp_path: Path) -> None:
    ledger = WorktreeTeamLedger(str(tmp_path / "ledger.sqlite3"))

    def admit(task_id: str) -> str:
        return ledger.admit(request(task_id))["status"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(admit, ["task-one", "task-two"]))
    assert sorted(statuses) == ["admitted", "hold"]
    held = ledger.get("task-one" if ledger.get("task-one")["status"] == "hold" else "task-two")
    assert held["ownership_conflicts"]


def test_clean_handoff_releases_only_the_exact_ownership(tmp_path: Path) -> None:
    ledger = WorktreeTeamLedger(str(tmp_path / "ledger.sqlite3"))
    ledger.admit(request("task-owner"))
    held = ledger.admit(request("task-held"))
    assert held["status"] == "hold"
    with pytest.raises(WorktreeContractError) as error:
        ledger.release("task-owner", clean_boundary=False)
    assert error.value.code == "DIRTY_HANDOFF"

    ledger.release("task-owner", clean_boundary=True)
    assert ledger.admit(request("task-next"))["status"] == "admitted"


def test_predecessor_requires_exact_pass_handoff(tmp_path: Path) -> None:
    ledger = WorktreeTeamLedger(str(tmp_path / "ledger.sqlite3"))
    ledger.admit(request("task-before"))
    dependent = request("task-after", file="src/after.py")
    dependent["dependencies"] = ["task-before"]
    dependent["required_predecessor_pass"] = ["task-before"]
    with pytest.raises(WorktreeContractError) as error:
        ledger.admit(dependent)
    assert error.value.code == "PREDECESSOR_NOT_PASSED"

    pass_task(ledger, "task-before")
    ledger.release("task-before", clean_boundary=True)
    assert ledger.admit(dependent)["status"] == "admitted"


def test_first_product_blocker_marks_later_gates_unverified(tmp_path: Path) -> None:
    ledger = WorktreeTeamLedger(str(tmp_path / "ledger.sqlite3"))
    ledger.admit(request("task-product-fail"))
    record = ledger.record_gate(
        "task-product-fail", "source", "FAIL", blocker_class="product_source"
    )
    assert record["first_blocker"] == {"gate": "source", "class": "product_source"}
    assert record["gates"]["tests"]["outcome"] == "UNVERIFIED"
    assert record["gates"]["review"]["outcome"] == "UNVERIFIED"
    later = ledger.record_gate("task-product-fail", "tests", "PASS")
    assert later["gates"]["tests"]["outcome"] == "UNVERIFIED"


def test_harness_failure_remains_distinct_from_product_failure(tmp_path: Path) -> None:
    ledger = WorktreeTeamLedger(str(tmp_path / "ledger.sqlite3"))
    ledger.admit(request("task-harness-fail", harness="external"))
    record = ledger.record_gate(
        "task-harness-fail", "source", "FAIL", blocker_class="harness_environment"
    )
    assert record["first_blocker"]["class"] == "harness_environment"
    assert record["manifest"]["harness"]["kind"] == "external"


def test_one_shot_attempt_is_idempotent_and_cannot_be_blindly_retried(tmp_path: Path) -> None:
    ledger = WorktreeTeamLedger(str(tmp_path / "ledger.sqlite3"))
    ledger.admit(request("task-budget"))
    first = ledger.consume_operation("task-budget", "package", "pkg:sha256:one", result="indeterminate")
    replay = ledger.consume_operation("task-budget", "package", "pkg:sha256:one", result="completed")
    assert first["attempt"] == replay["attempt"] == 1
    assert replay["replayed"] is True
    assert replay["result"] == "indeterminate"
    with pytest.raises(WorktreeContractError) as error:
        ledger.consume_operation("task-budget", "package", "pkg:sha256:two")
    assert error.value.code == "ATTEMPT_BUDGET_EXHAUSTED"


def test_exact_pass_handoff_and_ordered_promotion(tmp_path: Path) -> None:
    ledger = WorktreeTeamLedger(str(tmp_path / "ledger.sqlite3"))
    ledger.admit(request("task-pass"))
    result = pass_task(ledger, "task-pass")
    packet = result["handoff"]
    assert packet["overall"] == "PASS"
    assert packet["commands"][0]["argv"] == ["pytest", "-q"]
    assert packet["input"]["commit_sha"] == SHA_A
    assert packet["attempts"]["build"] == {"consumed": 0, "remaining": 1}

    reviewed = ledger.promote("task-pass", "reviewed", exact_output_digest=packet["output_digest"])
    assert reviewed["promotion_state"] == "reviewed"
    with pytest.raises(WorktreeContractError) as error:
        ledger.promote("task-pass", "final", exact_output_digest=packet["output_digest"])
    assert error.value.code == "PROMOTION_ORDER_INVALID"
    stable = ledger.promote("task-pass", "stable", exact_output_digest=packet["output_digest"])
    final = ledger.promote("task-pass", "final", exact_output_digest=packet["output_digest"])
    assert stable["promotion_state"] == "stable"
    assert final["promotion_state"] == "final"


def test_rebase_or_semantic_output_change_invalidates_review(tmp_path: Path) -> None:
    ledger = WorktreeTeamLedger(str(tmp_path / "ledger.sqlite3"))
    ledger.admit(request("task-review"))
    complete = pass_task(ledger, "task-review")
    ledger.promote("task-review", "reviewed", exact_output_digest=complete["handoff"]["output_digest"])
    invalidated = ledger.invalidate_review(
        "task-review",
        {"commit_sha": SHA_C, "tree_sha": SHA_A, "ordered_parents": [SHA_B], "clean": True},
    )
    assert invalidated["promotion_state"] == "candidate"
    assert invalidated["status"] == "unverified"
    assert invalidated["handoff"]["overall"] == "UNVERIFIED"


def test_partial_or_dirty_completion_can_never_claim_pass(tmp_path: Path) -> None:
    ledger = WorktreeTeamLedger(str(tmp_path / "ledger.sqlite3"))
    ledger.admit(request("task-partial"))
    ledger.record_gate("task-partial", "source", "PASS")
    result = ledger.complete(
        "task-partial",
        {
            "overall": "PASS",
            "output": {
                "commit_sha": SHA_B,
                "tree_sha": SHA_C,
                "ordered_parents": [SHA_A],
                "clean": False,
            },
            "changed_files": ["src/owned.py"],
            "changed_fields": ["contract:task-partial"],
            "commands": [],
            "evidence": [],
        },
    )
    assert result["handoff"]["overall"] == "UNVERIFIED"
    assert result["handoff"]["gate_matrix"]["tests"]["outcome"] == "UNVERIFIED"


def test_changed_files_and_fields_cannot_expand_worker_ownership(tmp_path: Path) -> None:
    ledger = WorktreeTeamLedger(str(tmp_path / "ledger.sqlite3"))
    ledger.admit(request("task-scope"))
    with pytest.raises(WorktreeContractError) as error:
        ledger.complete(
            "task-scope",
            {
                "overall": "UNVERIFIED",
                "output": {
                    "commit_sha": SHA_B,
                    "tree_sha": SHA_C,
                    "ordered_parents": [SHA_A],
                    "clean": True,
                },
                "changed_files": ["src/not-owned.py"],
                "changed_fields": [],
                "commands": [],
                "evidence": [],
            },
        )
    assert error.value.code == "OWNERSHIP_EXPANDED"


def test_cancellation_is_terminal_and_preserves_unverified_matrix(tmp_path: Path) -> None:
    ledger = WorktreeTeamLedger(str(tmp_path / "ledger.sqlite3"))
    ledger.admit(request("task-cancel"))
    cancelled = ledger.cancel("task-cancel", reason="User cancelled the exact task")
    assert cancelled["status"] == "cancelled"
    assert {row["outcome"] for row in cancelled["gates"].values()} == {"UNVERIFIED"}
    archived = ledger.archive("task-cancel")
    assert archived["status"] == "archived"
    assert archived["ownership_released"] is True


def test_compaction_wake_resumes_from_same_durable_ledger(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    first = WorktreeTeamLedger(str(path))
    first.admit(request("task-resume"))
    first.record_gate("task-resume", "source", "PASS", evidence_refs=["source:sha"])
    wake = first.wake("task-resume")

    resumed = WorktreeTeamLedger(str(path))
    state = resumed.get("task-resume")
    assert wake["resume_ref"] == f"worktree-ledger:task-resume:{state['revision']}"
    assert state["gates"]["source"]["outcome"] == "PASS"
    assert resumed.events("task-resume")[-1]["event_type"] == "gate.recorded"
