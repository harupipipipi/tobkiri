"""Regressions for checked-in complete-v4 migration evidence freshness."""

from __future__ import annotations

from copy import deepcopy

from scripts.quality import scan_complete_v4_migration as scanner


CURRENT = "a" * 40
PARENT = "b" * 40
PR_HEAD = "c" * 40
PR_PARENT = "d" * 40


def _evidence(sha: str = PARENT) -> dict[str, object]:
    evidence = {
        "schema": "io.tobkiri.quality.complete-v4-migration-evidence.v1",
        "source": {
            "freshness_basis": "deterministic-semantic-recomputation",
            "observed_head_sha": sha,
            "semantic_digest": "",
            "test_file": "gate.py",
        },
        "counts": {"production_pack_directories": 143},
        "findings": {"legacy": []},
    }
    evidence["source"]["semantic_digest"] = scanner._semantic_digest(evidence)  # type: ignore[index]
    return evidence


def _gate_evidence(status: str) -> dict[str, object]:
    """Return the minimum gate shape used by Phase 0 enforcement helpers."""

    return {
        "gate": {"status": status},
        "counts": {"gates": {"migration_evidence": 1 if status == "RED" else 0}},
        "findings": {
            "migration_evidence": (
                [{"unverified_count": 139}] if status == "RED" else []
            )
        },
    }


def test_informational_head_does_not_create_recursive_freshness_requirement() -> None:
    """Different valid commit labels cannot override identical recomputed truth."""

    tracked = _evidence(PARENT)
    observed = _evidence(CURRENT)

    assert scanner.evidence_drift(tracked, observed, event_name="push") == []


def test_malformed_informational_head_still_fails_schema_validation() -> None:
    tracked = _evidence("not-a-sha")
    observed = _evidence(CURRENT)

    assert scanner.evidence_drift(tracked, observed, event_name="push") == [
        "tracked informational HEAD is malformed"
    ]


def test_semantic_drift_fails_even_with_valid_push_provenance() -> None:
    """Provenance can never mask count, finding, or inventory drift."""

    tracked = _evidence(CURRENT)
    observed = deepcopy(tracked)
    observed["counts"]["production_pack_directories"] = 144  # type: ignore[index]
    observed["source"]["semantic_digest"] = scanner._semantic_digest(observed)  # type: ignore[index]
    assert scanner.evidence_drift(
        tracked,
        observed,
        event_name="push",
    ) == ["tracked evidence differs from the current semantic scan"]


def test_semantic_drift_fails_even_with_valid_pr_provenance() -> None:
    """A verified synthetic merge topology cannot mask semantic drift."""

    tracked = _evidence(PR_PARENT)
    observed = deepcopy(tracked)
    observed["findings"]["legacy"] = [{"path": "old.py"}]  # type: ignore[index]
    observed["source"]["semantic_digest"] = scanner._semantic_digest(observed)  # type: ignore[index]
    assert scanner.evidence_drift(
        tracked,
        observed,
        event_name="pull_request",
        pr_head_sha=PR_HEAD,
    ) == ["tracked evidence differs from the current semantic scan"]


def test_tampered_semantic_digest_fails_closed() -> None:
    tracked = _evidence(PARENT)
    observed = _evidence(CURRENT)
    tracked["source"]["semantic_digest"] = "sha256:" + "0" * 64  # type: ignore[index]

    assert scanner.evidence_drift(tracked, observed, event_name="push") == [
        "tracked evidence semantic digest is invalid"
    ]


def test_phase_zero_freshness_gate_rejects_stale_evidence() -> None:
    """Freshness-only mode must still fail closed on semantic evidence drift."""

    evidence = _gate_evidence("RED")
    drift = ["tracked evidence differs from the current semantic scan"]

    assert scanner._exit_code(evidence, drift, freshness_only=True) == 1


def test_phase_zero_fresh_red_is_reported_and_passes_freshness_gate() -> None:
    """A genuine RED remains visible while Phase 0 enforces only freshness."""

    evidence = _gate_evidence("RED")

    assert scanner._exit_code(evidence, [], freshness_only=True) == 0
    summary = scanner._summary_markdown(evidence, [], freshness_only=True)
    assert "Semantic migration status: **RED** (report-only)" in summary
    assert "without claiming migration completion" in summary
    assert "139 Pack release proof(s) missing" in summary


def test_phase_zero_fresh_green_passes() -> None:
    """Fresh GREEN evidence passes the Phase 0 freshness gate."""

    evidence = _gate_evidence("GREEN")

    assert scanner._exit_code(evidence, [], freshness_only=True) == 0
    assert "Semantic migration status: **GREEN** (report-only)" in (
        scanner._summary_markdown(evidence, [], freshness_only=True)
    )


def test_default_mode_keeps_semantic_red_fail_closed() -> None:
    """The opt-in Phase 0 split must not weaken the scanner's default mode."""

    assert scanner._exit_code(
        _gate_evidence("RED"), [], freshness_only=False
    ) == 1
