import re
from pathlib import Path


SETUP_WEB = (
    Path(__file__).resolve().parent.parent
    / "core_runtime"
    / "core_pack"
    / "core_setup"
    / "web"
)
SETUP_UI = SETUP_WEB / "index.html"
SETUP_LOCALES = SETUP_WEB / "locales.js"


def setup_ui_source() -> str:
    """Return the standalone setup page source."""

    return SETUP_UI.read_text(encoding="utf-8")


def setup_locale_source() -> str:
    """Return the dedicated setup locale catalog source."""

    return SETUP_LOCALES.read_text(encoding="utf-8")


def test_setup_uses_only_the_canonical_defaults_v4_transaction() -> None:
    """The page must not restore legacy selection or a second authority path."""

    source = setup_ui_source()
    request = re.search(
        r'body: JSON\.stringify\(\{(?P<body>.*?)\n\s*\}\),',
        source,
        re.S,
    )

    assert request is not None
    request_body = request.group("body")
    assert "setup_api_version: SETUP_API_VERSION" in request_body
    assert "operation_id: SETUP_OPERATION_ID" in request_body
    assert "confirmed: true" in request_body
    assert (
        "confirmation: currentPayload.recommended_default_profile.confirmation"
        in request_body
    )
    assert "setup_pack_ids" not in source
    assert "reviewed_pack_ids" not in source
    assert "confirmed_privileged_pack_ids" not in source
    assert "install_defaults_profile" not in source
    assert "review_revision" not in source
    assert 'getJson("/api/setup/migration/status")' not in source
    assert source.count('getJson("/api/setup/packs"') == 1
    assert source.count('getJson("/api/setup/packs/install"') == 1
    assert (
        'result?.setup_api_version !== SETUP_API_VERSION || result.state !== "active"'
        in source
    )


def test_server_confirmation_is_opaque_and_never_rendered_or_redigested() -> None:
    """ProfileLock, ResolvedPlan, Authority, and PackVM evidence stays opaque."""

    source = setup_ui_source()

    assert source.count("recommended_default_profile.confirmation") == 1
    assert ".confirmation." not in source
    assert "confirmation_digest" not in source
    assert "plan_digest" not in source
    assert "authority_snapshot_digest" not in source
    assert "JSON.stringify(currentPayload" not in source
    assert "crypto.subtle" not in source


def test_setup_locale_catalog_is_complete_and_sets_document_metadata() -> None:
    """Every referenced string exists in both QA-enabled LTR catalogs."""

    source = setup_ui_source()
    locales = setup_locale_source()
    catalog = re.search(
        r"const messages = \{\s*ja: \{(?P<ja>.*?)\n\s*\},\s*en: \{(?P<en>.*?)\n\s*\},\s*\};",
        locales,
        re.S,
    )

    assert catalog is not None
    key_pattern = re.compile(r"^\s{6}([A-Za-z][A-Za-z0-9]+):", re.M)
    japanese_keys = set(key_pattern.findall(catalog.group("ja")))
    english_keys = set(key_pattern.findall(catalog.group("en")))
    referenced = set(re.findall(r'tr\("([A-Za-z][A-Za-z0-9]+)"', source))
    referenced.update(
        re.findall(
            r'data-i18n(?:-aria-label|-alt)?="([A-Za-z][A-Za-z0-9]+)"',
            source,
        )
    )

    assert japanese_keys == english_keys
    assert referenced <= japanese_keys
    assert '<html lang="ja" dir="ltr">' in source
    assert 'document.documentElement.lang = currentLocale;' in source
    assert 'document.documentElement.dir = "ltr";' in source
    assert 'id="locale"' in source
    assert set(re.findall(r"^\s{4}(ja|en): \{", locales, re.M)) == {"ja", "en"}


def test_setup_models_all_states_with_one_atomic_live_region() -> None:
    """State announcements are atomic, urgent only on error, and busy-aware."""

    source = setup_ui_source()

    assert 'id="status-live"' in source
    assert 'role="status" aria-live="polite" aria-atomic="true" tabindex="-1"' in source
    assert (
        'statusLiveEl.setAttribute("role", tone === "error" ? "alert" : "status")'
        in source
    )
    assert "statusLiveEl.replaceChildren(fragment)" in source
    assert "statusLiveEl.innerHTML" not in source
    assert 'setStatus(tr("selectionChangedTitle")' in source
    assert "announceSelection();" in source
    assert 'document.body.setAttribute("aria-busy", "true")' in source
    assert 'document.body.setAttribute("aria-busy", "false")' in source
    assert 'installButton.setAttribute("aria-busy", String(installInProgress))' in source
    for state in (
        "loading",
        "review_required",
        "active",
        "activation_denied",
        "installing",
        "redirecting",
    ):
        assert f'"{state}"' in source


def test_profile_and_included_packs_are_separate_concise_groups() -> None:
    """Only the exact Profile is selectable; included Packs are informational."""

    source = setup_ui_source()

    assert 'card.setAttribute("role", "group")' in source
    assert 'card.setAttribute("aria-label", tr("profileGroupLabel"))' in source
    assert 'details.setAttribute("role", "group")' in source
    assert 'details.setAttribute("aria-label", tr("individualGroupLabel"))' in source
    assert 'choice.setAttribute("aria-pressed", String(profileSelected))' in source
    assert 'tr("selectionProfileSummary", { count })' in source
    assert 'selectionSummaryEl.setAttribute("aria-label", selectionSummaryEl.textContent)' in source
    assert 'document.createElement("input")' not in source
    assert 'input.type = "checkbox"' not in source
    assert "pack.pack_id" in source
    assert 'technical.className = "technical-id"' in source


def test_setup_redacts_backend_errors_and_copies_only_allowlisted_details() -> None:
    """Backend payloads and diagnostics cannot flow directly into copy or status."""

    source = setup_ui_source()

    assert "class SetupRequestError extends Error" in source
    assert "setupErrorCode(payload)" in source
    assert "sanitizeDebugPayload(payload)" in source
    assert 'navigator.clipboard.writeText(safeText)' in source
    assert "payload.denial_diagnostic" not in source
    assert re.search(r"payload\.error(?!_)", source) is None
    assert "envelope.error" not in source
    assert "safe.state = payload.state" in source
    assert "safe.status = payload.status" in source
    assert "safe.code = payload.code" in source
    assert "safe.pack_count = payload.pack_count" in source
    assert "safe.confirmation" not in source
    for forbidden in (
        "token",
        "secret",
        "password",
        "credential",
        "api[_-]?key",
        "cookie",
        "bearer",
    ):
        assert forbidden in source


def test_errors_map_to_actions_and_focus_the_summary() -> None:
    """Validation, request, denial, and stale-review failures are actionable."""

    source = setup_ui_source()

    assert "function mapSetupError(error)" in source
    for status in ("401", "403", "409", "410", "400", "422", "500"):
        assert status in source
    assert "if (options.focus) window.requestAnimationFrame(() => statusLiveEl.focus())" in source
    assert source.count("{ focus: true }") >= 3
    assert 'refreshButton.textContent = tr("retry")' in source
    assert 'currentPayload?.state !== "review_required"' in source


def test_pending_success_and_redirect_labels_are_accurate() -> None:
    """The activation button and redirect expose their current state."""

    source = setup_ui_source()

    assert 'state === "pending" ? "installPending"' in source
    assert 'state === "success" ? "installSuccess"' in source
    assert 'setStatus(tr("installingTitle")' in source
    assert 'setStatus(tr("redirectingTitle")' in source
    assert "window.location.assign(returnTo)" in source
    assert "safeReturnTo(" in source
    assert 'url.origin === window.location.origin && panelPath' in source


def test_setup_layout_covers_motion_zoom_and_small_viewports() -> None:
    """The page keeps natural scroll and reflow under accessibility sizing."""

    source = setup_ui_source()
    body = re.search(r"body \{(?P<body>.*?)\n    \}", source, re.S)

    assert body is not None
    assert "overflow: hidden" not in body.group("body")
    assert "min-height: 100dvh" in body.group("body")
    assert "overflow-x: clip" in body.group("body")
    assert "grid-template-columns: minmax(0, 1.35fr) minmax(260px, 0.65fr)" in source
    assert "@media (max-width: 820px)" in source
    assert "@media (max-width: 520px)" in source
    assert "@media (max-height: 560px)" in source
    assert "@media (prefers-reduced-motion: reduce)" in source
    assert "animation-duration: 0.01ms !important" in source
    assert ".selection-footer { align-items: stretch; flex-direction: column; }" in source


def test_setup_avoids_unsafe_dom_replacement_and_raw_technical_primary_copy() -> None:
    """Dynamic UI uses DOM APIs and keeps raw identifiers secondary."""

    source = setup_ui_source()

    assert ".innerHTML" not in source
    assert "insertAdjacentHTML" not in source
    assert "document.write" not in source
    assert "rootEl.replaceChildren()" in source
    assert "statusLiveEl.replaceChildren(fragment)" in source
    assert "displayPackName(pack)" in source
    assert "String(pack?.display_name" in source
    assert "pack?.display_name || pack?.pack_id" not in source
