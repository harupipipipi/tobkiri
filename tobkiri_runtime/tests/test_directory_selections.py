"""Directory picker tickets do not convey mount or filesystem authority."""

from dataclasses import replace
from pathlib import Path

import pytest

from tobkiri_host.directory_selections import (
    DirectorySelections,
    DirectorySelectionScope,
)


def scope() -> DirectorySelectionScope:
    return DirectorySelectionScope(
        "profile", "activation", "digest", 1, "principal", "session"
    )


def test_selection_is_private_scoped_and_one_use(tmp_path):
    store = DirectorySelections()
    selected = store.capture(tmp_path, scope())
    assert str(tmp_path) not in str(selected)
    token = selected["selection_id"]
    for field, value in (
        ("profile_id", "other"),
        ("activation_id", "other"),
        ("plan_digest", "other"),
        ("security_epoch", 2),
        ("presentation_owner_principal_id", "other"),
        ("presentation_owner_session_id", "other"),
    ):
        with pytest.raises(PermissionError):
            store.consume(token, replace(scope(), **{field: value}))
    assert store.consume(token, scope()) == tmp_path.resolve()
    with pytest.raises(PermissionError):
        store.consume(token, scope())


def test_expiry_retirement_and_capacity_fail_closed(tmp_path):
    now = [0.0]
    store = DirectorySelections(clock=lambda: now[0], capacity=1, ttl_seconds=1)
    token = store.capture(tmp_path, scope())["selection_id"]
    with pytest.raises(ValueError):
        store.capture(tmp_path, scope())
    now[0] = 1
    with pytest.raises(PermissionError):
        store.consume(token, scope())
    token = store.capture(tmp_path, scope())["selection_id"]
    store.close()
    with pytest.raises(PermissionError):
        store.consume(token, scope())
    with pytest.raises(PermissionError):
        store.capture(tmp_path, scope())


def test_symlink_is_not_a_picker_directory(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        DirectorySelections().capture(link, scope())


@pytest.mark.parametrize("replacement", ["directory", "symlink", "missing"])
def test_changed_root_invalidates_ticket(tmp_path, replacement):
    root = tmp_path / "selected"
    root.mkdir()
    store = DirectorySelections()
    token = store.capture(root, scope())["selection_id"]
    root.rename(tmp_path / "original")
    if replacement == "directory":
        root.mkdir()
    elif replacement == "symlink":
        root.symlink_to(tmp_path / "original", target_is_directory=True)
    with pytest.raises(PermissionError):
        store.consume(token, scope())
    with pytest.raises(PermissionError):
        store.consume(token, scope())


@pytest.mark.parametrize("ttl", [float("nan"), float("inf"), True, "60", 0])
def test_invalid_lifetime_rejected(ttl):
    with pytest.raises(ValueError):
        DirectorySelections(ttl_seconds=ttl)


@pytest.mark.parametrize("now", [float("nan"), float("inf"), True, "0"])
def test_invalid_clock_rejected(tmp_path, now):
    with pytest.raises(ValueError):
        DirectorySelections(clock=lambda: now).capture(tmp_path, scope())


@pytest.mark.parametrize("token", [None, [], {}, 123, ""])
def test_malformed_tokens_fail_closed(token):
    with pytest.raises(PermissionError):
        DirectorySelections().consume(token, scope())


def test_filesystem_root_selection_has_a_visible_label_without_exposing_its_path(
    tmp_path,
):
    root = Path(tmp_path.anchor)
    store = DirectorySelections()
    choice = store.capture(root, scope())
    assert choice["display_name"] == "Project folder"
    assert "path" not in choice
    captured_root, identity = store.consume_identity(choice["selection_id"], scope())
    assert captured_root == root.resolve(strict=True)
    current = captured_root.stat()
    assert identity == (current.st_dev, current.st_ino)
