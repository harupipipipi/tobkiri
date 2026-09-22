from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SHELL_ROOT = REPO_ROOT / "ecosystem" / "defaultspack" / "ui"
SHELL_APP = SHELL_ROOT / "shell-app.js"
COMPOSER_WIDGETS = REPO_ROOT / "ecosystem" / "defaultspack" / "webapp" / "src" / "lib" / "composerWidgets.ts"


def _shipped_javascript() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(SHELL_ROOT.glob("shell-*.js"))
    )


def test_shipped_composer_bundle_rehydrates_catalog_actions():
    bundle = _shipped_javascript()
    source = COMPOSER_WIDGETS.read_text(encoding="utf-8")

    assert "trustedComposerActionForWidget" in bundle
    assert "composer_catalog_drop" in bundle
    assert "sourceItemId" in bundle

    trusted_action_fn = source.split("export function trustedComposerActionForWidget", 1)[1].split(
        "(trustedComposerActionForWidget",
        1,
    )[0]
    assert "item.ui?.composer_action" in trusted_action_fn
    assert "widget.action" not in trusted_action_fn

    # Regression guard for the stale bundle vulnerability: the shipped composer
    # must not execute a dropped widget's serialized action directly.  Keep the
    # minified-string sentinel, but assert the source-level security invariant so
    # harmless bundle reshaping does not break CI.
    assert "Yu=u=>{const b=u.action" not in bundle


def test_shipped_composer_bundle_keeps_endpoint_allowlist():
    bundle = _shipped_javascript()
    source = COMPOSER_WIDGETS.read_text(encoding="utf-8")

    assert "api/coding/git/status" in bundle
    assert "call_endpoint" in bundle
    assert "requires_approval" in bundle
    assert "COMPOSER_ENDPOINT_ACTION_ALLOWLIST" in source
    assert "action.type === \"call_endpoint\"" in source
    assert "&& !action.requires_approval" in source
    assert "&& isSafeLocalEndpoint(action.endpoint)" in source
    assert "&& COMPOSER_ENDPOINT_ACTION_ALLOWLIST.has(composerEndpointActionKey(action))" in source


def test_shipped_shell_bundle_loads_split_chunks_under_static_mount():
    bundle = SHELL_APP.read_text(encoding="utf-8")

    assert 'from"./shell-' not in bundle
    assert 'import("./shell-' not in bundle
    assert 'from"/static/shell-' in bundle or 'import("/static/shell-' in bundle
