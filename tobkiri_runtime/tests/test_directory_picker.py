"""Picker adapters remain unavailable without trusted Host binding."""

from pathlib import Path

import pytest

from tobkiri_host.directory_picker import CapturedDirectoryPicker
from tobkiri_host.directory_selections import (
    DirectorySelections,
    DirectorySelectionScope,
)


def test_missing_port_and_client_authority_hints_fail_closed():
    picker = CapturedDirectoryPicker(None, DirectorySelections())
    scope = DirectorySelectionScope("p", "a", "d", 1, "owner", "session")
    with pytest.raises(ValueError):
        picker.acquire({"approved": True}, scope=scope, assert_current=lambda: None)
    with pytest.raises(NotImplementedError):
        picker.acquire({}, scope=scope, assert_current=lambda: None)


def test_stale_dialog_result_never_becomes_ticket(tmp_path):
    class Port:
        def pick_directory(self) -> Path:
            return tmp_path

    calls = []

    def current() -> None:
        calls.append(True)
        if len(calls) == 2:
            raise PermissionError("activation retired")

    store = DirectorySelections()
    picker = CapturedDirectoryPicker(Port(), store)
    scope = DirectorySelectionScope("p", "a", "d", 1, "owner", "session")
    with pytest.raises(PermissionError):
        picker.acquire({}, scope=scope, assert_current=current)
    assert store._entries == {}


@pytest.mark.parametrize("folder_count", [1, 2])
def test_native_picker_reply_binds_primary_to_first_private_ticket(
    tmp_path: Path, folder_count: int
) -> None:
    roots = [tmp_path / f"folder-{index}" for index in range(folder_count)]
    for root in roots:
        root.mkdir()

    class Port:
        def pick_directories(self) -> list[Path]:
            return roots

    store = DirectorySelections()
    picker = CapturedDirectoryPicker(Port(), store)
    scope = DirectorySelectionScope("p", "a", "d", 1, "owner", "session")
    reply = picker.acquire({}, scope=scope, assert_current=lambda: None)
    assert reply["cancelled"] is False
    assert reply["primary_selection_id"] == reply["selection_id"]
    assert reply["primary_selection_id"] == reply["selections"][0]["selection_id"]
    assert len(reply["selections"]) == folder_count
    assert set(reply) == {
        "cancelled",
        "selection_id",
        "display_name",
        "expires_in_ms",
        "selections",
        "primary_selection_id",
    }
    for selected, root in zip(reply["selections"], roots):
        assert selected["display_name"] == root.name
        assert selected["expires_in_ms"] > 0
        assert store.consume(selected["selection_id"], scope) == root


@pytest.mark.parametrize("error", [PermissionError, TimeoutError, NotImplementedError])
def test_adapter_errors_sanitized(error):
    class Port:
        def pick_directory(self):
            raise error("secret /private/path")

    picker = CapturedDirectoryPicker(Port(), DirectorySelections())
    with pytest.raises(error) as caught:
        picker.acquire(
            {},
            scope=DirectorySelectionScope("p", "a", "d", 1, "o", "s"),
            assert_current=lambda: None,
        )
    assert "secret" not in str(caught.value)


def test_reentrant_dialog_busy(tmp_path):
    kwargs = dict(
        scope=DirectorySelectionScope("p", "a", "d", 1, "o", "s"),
        assert_current=lambda: None,
    )

    class Port:
        def pick_directory(self):
            with pytest.raises(RuntimeError, match="busy"):
                picker.acquire({}, **kwargs)
            return tmp_path

    picker = CapturedDirectoryPicker(Port(), DirectorySelections())
    assert picker.acquire({}, **kwargs)["selection_id"]


def test_cancellation_does_not_issue_ticket_and_closed_picker_never_opens():
    calls = []

    class Port:
        def pick_directory(self):
            calls.append(True)
            return None

    store = DirectorySelections()
    picker = CapturedDirectoryPicker(Port(), store)
    kwargs = dict(
        scope=DirectorySelectionScope("p", "a", "d", 1, "o", "s"),
        assert_current=lambda: None,
    )
    assert picker.acquire({}, **kwargs) == {"cancelled": True, "selection_id": None}
    assert store._entries == {}
    picker.close()
    with pytest.raises(NotImplementedError):
        picker.acquire({}, **kwargs)
    assert len(calls) == 1
