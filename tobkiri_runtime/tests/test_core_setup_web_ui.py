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
    body_rules = re.search(r"body \{(?P<body>.*?)\n    \}", source, re.S)

    assert body_rules is not None
    assert "place-items: center" not in body_rules.group("body")
    assert "overflow: hidden;" not in body_rules.group("body")
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
    assert "詳細なデバッグ状態" in source
    assert 'document.createElement("details")' in source


def test_setup_offers_an_explicit_standard_choice_without_auto_selection() -> None:
    """The standard setup remains an explicit user choice, not an auto-selection."""
    source = setup_ui_source()
    assert 'const BASIC_PACK_IDS = ["defaultspack"];' in source
    assert "choice.dataset.basicPackIds" in source
    assert 'choice.setAttribute("aria-pressed", "false")' in source
    assert 'selectionState.textContent = "選択する"' in source
    basic_selection = re.search(
        r"function renderBasicSelection\(packs, recommendedProfile\) \{(?P<body>.*?)\n    \}",
        source,
        re.S,
    )
    assert basic_selection is not None
    assert 'document.createElement("input")' not in basic_selection.group("body")
    assert "selectedPackIds.clear();" in source
    assert "selectedPackIds.add(packs[0])" not in source
    assert 'summary.textContent = "詳細を表示して、packを個別に選ぶ"' in source
    assert "details.open = false;" in source
    assert "Defaults Profile" in source
    assert "recommended_default_profile" in source
    assert "PROFILE_CARDS" not in source
    assert "matchingPacks" not in source
    assert "matches.push(packs[0])" not in source
    assert "pack.recommended" not in source


def test_setup_cards_are_full_clickable_labels() -> None:
    """Recommended selection uses a clear card button; advanced packs use labels."""
    source = setup_ui_source()

    assert 'document.createElement("label")' in source
    assert 'choice.className = "profile-choice-main"' in source
    assert 'listEl.addEventListener("click", handleBasicSelection)' in source
    assert 'card.addEventListener("click", (event) =>' in source
    assert 'target.closest("[data-basic-pack-ids]") || target.closest("summary")' in source
    assert "choice.click();" in source
    assert 'label.className = "pack selectable-pack"' in source
    assert "cursor: pointer;" in source
    assert "min-height: 44px;" in source
    assert "dataset.selectPack" in source
    assert 'input.setAttribute("aria-label", setupPackName(pack) + " を追加")' in source
    assert '.pack:has(input:focus-visible)' in source


def test_recommended_pack_list_is_collapsed_by_default() -> None:
    """The 29-pack recommendation should not flood selection or review screens."""
    source = setup_ui_source()

    assert 'document.createElement("details")' in source
    assert 'preview.textContent = "含まれる pack: " + previewNames' in source
    assert 'const collapsedLabel = "すべて表示（" + names.length + "）"' in source
    assert 'summary.textContent = disclosure.open ? "一覧を閉じる" : collapsedLabel' in source
    assert 'list.className = "pack-name-list"' in source
    assert "profilePlan.appendChild(createPackDisclosure(" in source
    assert 'includedPacks.textContent = "含まれる pack: "' not in source
    assert '.pack.selectable-pack:hover' in source
    assert '.review-profile-plan {' in source
    assert 'if (pack && pack.pack_id === "defaultspack") return "Tobkiri"' in source
    assert "Tobkiriの標準機能とデータ移行を提供する" in source


def test_standard_and_advanced_choices_share_one_selection_model() -> None:
    """Standard and individual controls must not submit stale duplicate state."""
    source = setup_ui_source()

    assert "const selectedPackIds = new Set();" in source
    assert "function parseBasicPackIds(input)" in source
    assert "function handleSelectionChange(event)" in source
    assert "selectedPackIds.add(packId)" in source
    assert "selectedPackIds.delete(packId)" in source
    assert 'listEl.addEventListener("change", handleSelectionChange)' in source
    assert "return Array.from(selectedPackIds);" in source


def test_setup_page_has_a_back_action_and_uses_the_tobkiri_icon() -> None:
    source = setup_ui_source()

    assert 'id="back"' in source
    assert 'window.location.assign("/panel/setup")' in source
    assert 'src="/setup/assets/tobkiri-launcher-icon.png"' in source
    assert 'alt="Tobkiri"' in source
    assert 'get("color_mode")' in source
    assert '|| "dark"' in source
    assert "theme-standard" not in source
    assert "Standard setup" not in source


def test_install_action_sends_selected_ids_and_reports_feedback() -> None:
    """Install should post selected setup pack ids and expose pending/result feedback."""
    source = setup_ui_source()

    assert "const selected = selectedSetupPackIds();" in source
    assert "reviewed_pack_ids: selected" in source
    assert "review_revision: currentReviewRevision" in source
    assert "confirmed_privileged_pack_ids" in source
    assert "install_defaults_profile: installingDefaultsProfile" in source
    assert "confirmed_defaults_profile: installingDefaultsProfile" in source
    assert "reviewed_default_profile_pack_ids" in source
    assert 'getJson("/api/setup/packs/install"' in source
    assert 'setInstallProgress("pending")' in source
    assert 'setInstallProgress("success")' in source
    assert 'setInstallProgress("error")' in source
    assert 'id="install-progress"' in source
    assert 'id="install-selected" disabled' in source
    assert "do not immediately issue an unauthenticated refresh request here" in source
    assert "const refreshed = await load({ preserveStatus: true, redirect: false });" not in source
    assert 'aria-live="polite"' in source
    assert 'setStatus("setup pack をインストール中…"' in source
    assert 'setStatus("setup pack をインストールしました"' in source


def test_install_review_discloses_pack_risk_and_requires_privileged_confirmation() -> None:
    source = setup_ui_source()

    for field in ("source_path", "description", "risk_level", "required_permissions", "supports_all_ok", "depends_on", "conflicts_with", "version"):
        assert field in source
    review = re.search(
        r"function renderReview\(packs, recommendedProfile\) \{(?P<body>.*?)\n    \}",
        source,
        re.S,
    )

    assert review is not None
    assert "checkbox.type" not in review.group("body")
    assert "requiresPrivilegedConfirmation(pack)" in source
    assert 'item.addEventListener("click", toggleConfirmation)' in source
    assert 'item.setAttribute("role", "button")' in source
    assert "この項目のどこかをクリックして、権限の強い pack を明示的に確認してください。" not in source
    assert "確認済みです。もう一度クリックすると確認を取り消します。" not in source
    assert 'confirmation.textContent = confirmed' in source
    assert '"承認済み"' in source
    assert "インストール前の確認が必要です" in source
    assert "インストール内容の確認" in source
    assert 'profileTitle.textContent = "作成される " + recommendedProfile.name;' in source
    assert ":root.dark .pack:hover" not in source
    assert ".review-profile-plan:hover" in source
