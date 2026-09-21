import re
from pathlib import Path


RUNTIME_ROOT = Path(__file__).resolve().parent.parent
SETUP_UI = RUNTIME_ROOT / "core_runtime" / "core_pack" / "core_setup" / "web" / "index.html"
DEFAULT_PACK = RUNTIME_ROOT / "ecosystem" / "setup_pack" / "defaultspack" / "pack.json"


def setup_ui_source() -> str:
    """Return the initial setup page source."""
    return SETUP_UI.read_text(encoding="utf-8")


def test_setup_starts_with_a_single_dark_recommendation() -> None:
    """The first-run surface is a focused, dark one-column recommendation."""
    source = setup_ui_source()
    body_rules = re.search(r"body \{(?P<body>.*?)\n    \}", source, re.S)

    assert "color-scheme: dark;" in source
    assert "--bg: #0a0a0a;" in source
    assert "grid-template-columns: minmax(0, 720px);" in source
    assert "Tobkiri をはじめよう" in source
    assert "まずはこれだけで始められます" in source
    assert "このおすすめで始める →" in source


def test_setup_selects_defaultspack_instead_of_restoring_every_pack() -> None:
    """Existing setup selections must not turn the first screen into a pack dump."""
    source = setup_ui_source()

    assert 'pack.pack_id === "defaultspack"' in source
    assert "|| packs.find((pack) => pack.recommended)" in source
    assert "selectedPackIds.add(recommended.pack_id);" in source
    assert "renderRecommendedSelection(recommended);" in source
    assert "for (const pack of packs)" not in source[source.index("function renderPacks"):source.index("function renderRecommendedSelection")]
    assert "renderAdvancedSelection" not in source


def test_setup_hides_technical_status_until_an_action_needs_feedback() -> None:
    """Initial setup must not show migration, review, or debug internals."""
    source = setup_ui_source()

    assert 'id="status" class="status" role="status" aria-live="polite" hidden' in source
    assert "statusEl.hidden = !label;" in source
    assert "Advanced debug state" not in source
    assert "setup/migration/status" not in source
    assert "Review required before install" not in source


def test_recommended_install_posts_only_the_recommended_pack() -> None:
    """One click should install the selected recommendation instead of opening review."""
    source = setup_ui_source()

    assert 'getJson("/api/setup/packs/install"' in source
    assert "reviewed_pack_ids: selected" in source
    assert "review_revision: currentReviewRevision" in source
    assert "confirmed_privileged_pack_ids: []" in source
    assert 'setStatus("おすすめを入れています…"' in source
    assert 'setStatus("セットアップが完了しました"' in source
    assert "renderReview" not in source


def test_default_recommendation_does_not_grant_all_ok_on_install() -> None:
    """The one-click recommendation must not carry broad permission grants."""
    source = DEFAULT_PACK.read_text(encoding="utf-8")

    assert '"recommended": true' in source
    assert '"supports_all_ok": false' in source
