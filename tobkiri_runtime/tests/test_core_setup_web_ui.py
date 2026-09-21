import re
from pathlib import Path


SETUP_UI = (
    Path(__file__).resolve().parent.parent
    / "core_runtime"
    / "core_pack"
    / "core_setup"
    / "web"
    / "index.html"
)


def setup_ui_source() -> str:
    """Return the setup-pack landing page source."""
    return SETUP_UI.read_text(encoding="utf-8")


def test_setup_pack_landing_avoids_centered_clipping_layout() -> None:
    """Initial desktop view should top-align and allow natural vertical scroll."""
    source = setup_ui_source()

    assert "place-items: center" not in source
    assert "overflow: hidden;" not in source
    assert "margin: 0 auto;" in source
    assert "grid-template-columns: minmax(0, 1.35fr) minmax(260px, 0.65fr)" in source
    assert "min-width: 0;" in source
    assert "overflow-x: clip;" in source
    assert "@media (max-width: 820px)" in source


def test_setup_state_hides_raw_json_behind_debug_disclosure() -> None:
    """The default status panel should render compact copy, not a raw JSON dump."""
    source = setup_ui_source()
    set_status = re.search(
        r"function setStatus\(label, payload, rows, tone = \"neutral\"\) \{(?P<body>.*?)\n    \}",
        source,
        re.S,
    )

    assert set_status is not None
    assert "JSON.stringify(payload, null, 2)" not in set_status.group("body")
    assert "renderInstallSummary(packs, migration)" in source
    assert "Advanced debug state" in source
    assert 'document.createElement("details")' in source


def test_setup_never_infers_or_auto_selects_recommendations() -> None:
    """Selection must be explicit and independent of names, order, and recommended flags."""
    source = setup_ui_source()
    assert "PROFILE_CARDS" not in source
    assert "matchingPacks" not in source
    assert "matches.push(packs[0])" not in source
    assert "pack.recommended" not in source
    assert 'summary.textContent = "Choose setup packs individually (no automatic recommendation)"' in source


def test_setup_cards_are_full_clickable_labels() -> None:
    """Selection cards should use label-owned checkboxes for large hit targets."""
    source = setup_ui_source()

    assert 'document.createElement("label")' in source
    assert 'label.className = "pack"' in source
    assert "cursor: pointer;" in source
    assert "min-height: 44px;" in source
    assert "dataset.selectPack" in source
    assert 'input.setAttribute("aria-label", "Include " + (pack.display_name || pack.pack_id || "pack"))' in source
    assert '.pack:has(input:focus-visible)' in source


def test_group_and_advanced_choices_share_one_selection_model() -> None:
    """Group and individual controls must not submit stale duplicate state."""
    source = setup_ui_source()

    assert "const selectedPackIds = new Set();" in source
    assert "function handleSelectionChange(event)" in source
    assert "selectedPackIds.add(packId)" in source
    assert "selectedPackIds.delete(packId)" in source
    assert 'listEl.addEventListener("change", handleSelectionChange)' in source
    assert "return Array.from(selectedPackIds);" in source


def test_install_action_sends_selected_ids_and_reports_feedback() -> None:
    """Install should post selected setup pack ids and expose pending/result feedback."""
    source = setup_ui_source()

    assert "const selected = selectedSetupPackIds();" in source
    assert "reviewed_pack_ids: selected" in source
    assert "review_revision: currentReviewRevision" in source
    assert "confirmed_privileged_pack_ids" in source
    assert 'getJson("/api/setup/packs/install"' in source
    assert 'setInstallProgress("pending")' in source
    assert 'setInstallProgress("success")' in source
    assert 'setInstallProgress("error")' in source
    assert 'id="install-progress"' in source
    assert 'id="install-selected" disabled' in source
    assert "const refreshed = await load({ preserveStatus: true, redirect: false });" in source
    assert "if (!refreshed) return;" in source
    assert 'aria-live="polite"' in source
    assert 'setStatus("Installing setup packs…"' in source
    assert 'setStatus("Setup packs installed"' in source


def test_install_review_discloses_pack_risk_and_requires_privileged_confirmation() -> None:
    source = setup_ui_source()

    for field in ("source_path", "description", "risk_level", "required_permissions", "supports_all_ok", "depends_on", "conflicts_with", "version"):
        assert field in source
    assert "I explicitly confirm this privileged pack" in source
    assert "Review required before install" in source
