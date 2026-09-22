"""Regressions for checked-in complete-v4 migration evidence freshness."""

from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.quality import scan_complete_v4_migration as scanner


CURRENT = "a" * 40
PARENT = "b" * 40
PR_HEAD = "c" * 40
PR_PARENT = "d" * 40
BASE = "e" * 40
UNRELATED = "f" * 40


def _evidence(sha: str = PARENT) -> dict[str, object]:
    return {
        "schema": "io.tobkiri.quality.complete-v4-migration-evidence.v1",
        "source": {"observed_head_sha": sha, "test_file": "gate.py"},
        "counts": {"production_pack_directories": 143},
        "findings": {"legacy": []},
    }


@pytest.mark.parametrize("tracked_sha", [CURRENT, PARENT])
def test_push_accepts_current_or_immediate_parent(tracked_sha: str) -> None:
    """A normal push accepts only its checkout or self-reference parent."""

    assert scanner.provenance_errors(
        tracked_sha=tracked_sha,
        event_name="push",
        current_sha=CURRENT,
        current_parents=(PARENT,),
    ) == []


@pytest.mark.parametrize("tracked_sha", [PR_HEAD, PR_PARENT])
def test_pr_merge_accepts_head_or_head_immediate_parent(tracked_sha: str) -> None:
    """A synthetic merge follows the PR lineage, never its base parent."""

    assert scanner.provenance_errors(
        tracked_sha=tracked_sha,
        event_name="pull_request",
        current_sha=CURRENT,
        current_parents=(BASE, PR_HEAD),
        pr_head_sha=PR_HEAD,
        pr_head_parents=(PR_PARENT,),
    ) == []


@pytest.mark.parametrize("tracked_sha", [BASE, UNRELATED])
def test_pr_merge_rejects_base_unrelated_and_stale_sha(tracked_sha: str) -> None:
    """Neither the synthetic base nor another valid commit is provenance."""

    errors = scanner.provenance_errors(
        tracked_sha=tracked_sha,
        event_name="pull_request",
        current_sha=CURRENT,
        current_parents=(BASE, PR_HEAD),
        pr_head_sha=PR_HEAD,
        pr_head_parents=(PR_PARENT,),
    )
    assert errors and "stale for pull_request" in errors[0]


@pytest.mark.parametrize(
    ("event_name", "pr_head_sha", "expected"),
    [
        ("", "", "unsupported or missing CI event name"),
        ("pull_request", "", "pull_request head SHA is missing or malformed"),
        ("pull_request", "not-a-sha", "pull_request head SHA is missing or malformed"),
    ],
)
def test_missing_or_malformed_workflow_inputs_fail_closed(
    event_name: str,
    pr_head_sha: str,
    expected: str,
) -> None:
    errors = scanner.provenance_errors(
        tracked_sha=PR_PARENT,
        event_name=event_name,
        current_sha=CURRENT,
        current_parents=(BASE, PR_HEAD),
        pr_head_sha=pr_head_sha,
        pr_head_parents=(PR_PARENT,),
    )
    assert errors and expected in errors[0]


def test_pr_head_must_be_direct_synthetic_checkout_parent() -> None:
    errors = scanner.provenance_errors(
        tracked_sha=PR_PARENT,
        event_name="pull_request",
        current_sha=CURRENT,
        current_parents=(BASE,),
        pr_head_sha=PR_HEAD,
        pr_head_parents=(PR_PARENT,),
    )
    assert errors == ["pull_request head SHA is not a direct checkout parent"]


def test_semantic_drift_fails_even_with_valid_push_provenance(monkeypatch) -> None:
    """Provenance can never mask count, finding, or inventory drift."""

    monkeypatch.setattr(scanner, "_git_sha", lambda *_args: CURRENT)
    monkeypatch.setattr(
        scanner,
        "_git_parents",
        lambda revision: (PARENT,) if revision == CURRENT else (),
    )
    tracked = _evidence(CURRENT)
    observed = deepcopy(tracked)
    observed["counts"]["production_pack_directories"] = 144  # type: ignore[index]
    assert scanner.evidence_drift(
        tracked,
        observed,
        event_name="push",
    ) == ["tracked evidence differs from the current semantic scan"]


def test_semantic_drift_fails_even_with_valid_pr_provenance(monkeypatch) -> None:
    """A verified synthetic merge topology cannot mask semantic drift."""

    monkeypatch.setattr(scanner, "_git_sha", lambda *_args: CURRENT)
    monkeypatch.setattr(
        scanner,
        "_git_parents",
        lambda revision: (BASE, PR_HEAD) if revision == CURRENT else (PR_PARENT,),
    )
    tracked = _evidence(PR_PARENT)
    observed = deepcopy(tracked)
    observed["findings"]["legacy"] = [{"path": "old.py"}]  # type: ignore[index]
    assert scanner.evidence_drift(
        tracked,
        observed,
        event_name="pull_request",
        pr_head_sha=PR_HEAD,
    ) == ["tracked evidence differs from the current semantic scan"]
