"""Accessibility and isolation contracts for the Prompt Studio pack UI."""

from __future__ import annotations

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parent.parent
UI_ROOT = ROOT / "ecosystem" / "rumi_prompt_studio_pack" / "ui"


class _MarkupInventory(HTMLParser):
    """Collect the semantic attributes needed by the UI contract tests."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        """Record one start tag with normalized string attributes."""
        self.elements.append(
            (tag, {name: value or "" for name, value in attrs})
        )


def _inventory() -> _MarkupInventory:
    """Parse the shipped Prompt Studio HTML."""
    parser = _MarkupInventory()
    parser.feed((UI_ROOT / "index.html").read_text(encoding="utf-8"))
    return parser


def test_prompt_studio_fields_have_stable_label_and_description_links() -> None:
    """Every form field has a stable label and resolvable help/error IDs."""
    inventory = _inventory()
    elements_by_id = {
        attrs["id"]: (tag, attrs)
        for tag, attrs in inventory.elements
        if attrs.get("id")
    }
    labels = {
        attrs["for"]
        for tag, attrs in inventory.elements
        if tag == "label" and attrs.get("for")
    }

    for field_id in ("locale", "prompt-search", "prompt-id", "body"):
        assert field_id in elements_by_id
        assert field_id in labels

    for tag, attrs in inventory.elements:
        for relation in ("aria-describedby", "aria-labelledby", "aria-controls"):
            for target_id in attrs.get(relation, "").split():
                assert target_id in elements_by_id, (tag, attrs, relation, target_id)

    assert elements_by_id["prompt-id"][1]["aria-describedby"] == (
        "prompt-id-help field-error"
    )
    assert elements_by_id["body"][1]["aria-describedby"] == (
        "prompt-body-help field-error"
    )
    assert elements_by_id["error-region"][1]["role"] == "alert"
    assert elements_by_id["studio-status"][1]["role"] == "status"


def test_prompt_studio_declares_listbox_filters_and_complete_tab_pattern() -> None:
    """Selection, filtering, and inspector relationships follow APG shapes."""
    inventory = _inventory()
    elements_by_id = {
        attrs["id"]: (tag, attrs)
        for tag, attrs in inventory.elements
        if attrs.get("id")
    }
    assert elements_by_id["prompts"][1]["role"] == "listbox"

    filters = [
        attrs
        for _, attrs in inventory.elements
        if attrs.get("data-filter")
    ]
    assert {attrs["data-filter"] for attrs in filters} == {
        "all",
        "active",
        "editable",
        "readonly",
        "overrides",
    }
    assert all(attrs.get("aria-pressed") in {"true", "false"} for attrs in filters)

    tabs = [attrs for _, attrs in inventory.elements if attrs.get("role") == "tab"]
    panels = {
        attrs["id"]: attrs
        for _, attrs in inventory.elements
        if attrs.get("role") == "tabpanel"
    }
    assert {attrs["data-tab"] for attrs in tabs} == {
        "status",
        "result",
        "versions",
    }
    for tab in tabs:
        panel = panels[tab["aria-controls"]]
        assert panel["aria-labelledby"] == tab["id"]
        assert tab["aria-selected"] in {"true", "false"}
        assert tab["tabindex"] in {"0", "-1"}


def test_prompt_studio_rollback_dialog_is_explicit_and_version_aware() -> None:
    """The shipped dialog and client implement confirmation lifecycle states."""
    html = (UI_ROOT / "index.html").read_text(encoding="utf-8")
    script = (UI_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'id="rollback-dialog"' in html
    assert 'aria-labelledby="rollback-title"' in html
    assert 'aria-describedby="rollback-description"' in html
    assert 'id="rollback-status" role="status" aria-live="polite"' in html
    assert "dialog.showModal()" in script
    assert 'dialog.setAttribute("aria-busy", "true")' in script
    assert 'setAttribute("role", "alert")' in script
    assert 't("rollbackPending"' in script
    assert 't("rollbackSettled"' in script
    assert 't("rollbackFailed"' in script
    assert 'expected_body_hash: prompt?.body_hash || emptyHash' in script
    assert 't("rollbackAction", {' in script
    assert "rollbackReturnFocus" in script


def test_prompt_studio_client_drives_keyboard_focus_and_non_color_state() -> None:
    """Dynamic semantics cover keyboard operation, focus, and state text."""
    script = (UI_ROOT / "app.js").read_text(encoding="utf-8")

    for key in ("ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft", "Home", "End"):
        assert f'"{key}"' in script
    assert 'event.key === "Enter" || event.key === " "' in script
    assert 'setAttribute("aria-selected"' in script
    assert 'setAttribute("aria-pressed"' in script
    assert 'target.focus({ preventScroll: true })' in script
    assert 'byId("error-region").focus' in script
    assert 'byId("prompt-id").readOnly = !newDraft' in script
    assert 'setAttribute("aria-invalid"' in script

    for state_id in (
        "state-selection",
        "state-activation",
        "state-editing",
        "state-dirty",
        "state-tokenizer",
        "state-safety",
    ):
        assert f'byId("{state_id}").textContent' in script


def test_prompt_studio_ui_stays_localized_responsive_and_provider_free() -> None:
    """The isolated UI supports locale/zoom constraints without new authority."""
    script = (UI_ROOT / "app.js").read_text(encoding="utf-8")
    css = (UI_ROOT / "style.css").read_text(encoding="utf-8")

    assert "const messages = {" in script
    assert "en: {" in script
    assert "ja: {" in script
    assert "document.documentElement.lang = locale" in script
    assert "parent.postMessage" in script
    assert "fetch(" not in script
    assert "OPENAI_API_KEY" not in script
    assert "provider_invoked" not in script
    assert "tool_invoked" not in script

    assert "@media (max-width: 50rem)" in css
    assert "@media (max-width: 34rem)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "@media (forced-colors: active)" in css
    assert "overflow-wrap: anywhere" in css
    assert ":focus-visible" in css
    assert "min-height: 2.75rem" in css


def test_prompt_studio_javascript_is_syntactically_valid() -> None:
    """Reject malformed shipped JavaScript when Node is available."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    subprocess.run(
        [node, "--check", str(UI_ROOT / "app.js")],
        check=True,
        capture_output=True,
        text=True,
    )
