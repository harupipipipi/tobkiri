"""Fixed alias normalization retains fresh trust and exact lexical behavior."""

from __future__ import annotations

from pathlib import Path
import stat
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from tobkiri_protocol import platform_paths as paths


def old_normalization(path: Path) -> Path:
    """Reference the original full function with the same current OS checks."""
    absolute = path.absolute()
    if paths.sys.platform != "darwin":
        return absolute
    aliases = (
        (Path("/var"), Path("/private/var")),
        (Path("/tmp"), Path("/private/tmp")),
    )
    for alias, canonical in aliases:
        if absolute != alias and alias not in absolute.parents:
            continue
        try:
            metadata = alias.lstat()
            link_target = Path(paths.os.readlink(alias))
            accepted_targets = {canonical, canonical.relative_to("/")}
            if (
                not stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != 0
                or link_target not in accepted_targets
            ):
                return absolute
        except OSError:
            return absolute
        return canonical / absolute.relative_to(alias)
    return absolute


def alias_trust(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Observe fresh alias reads without replacing filesystem resolution."""
    state: dict[str, Any] = {
        "uid": 0,
        "mode": stat.S_IFLNK | 0o777,
        "target": None,
        "error": None,
        "calls": [],
    }
    original = Path.lstat

    def lstat(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path not in {Path("/var"), Path("/tmp")}:
            return original(path, *args, **kwargs)
        state["calls"].append(("lstat", str(path)))
        if state["error"] == "lstat":
            raise OSError("unit lstat failure")
        return SimpleNamespace(st_uid=state["uid"], st_mode=state["mode"])

    def readlink(path: Path) -> str:
        state["calls"].append(("readlink", str(path)))
        if state["error"] == "readlink":
            raise OSError("unit readlink failure")
        return state["target"] or "/private" + str(path)

    monkeypatch.setattr(paths, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(paths, "os", SimpleNamespace(readlink=readlink))
    monkeypatch.setattr(Path, "lstat", lstat)
    return state


@pytest.mark.parametrize(
    "value",
    [
        "/var",
        "/tmp",
        "/var/a/b",
        "/tmp/a/b",
        "/private/var/a",
        "/variable",
        "/tmpfoo",
        "/VAR/a",
        "/tmp/../var/a",
        "/var/../other",
        "//var/a",
        "//tmp",
        "/tmp/日本語/é",
        "/var//a///b",
        "/",
        "relative/../tmp",
    ],
)
def test_complete_function_matches_original_lexical_cases(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Roots, anchors, segments and lexical dot-dot retain the old result."""
    alias_trust(monkeypatch)
    assert paths.canonical_platform_path(Path(value)) == old_normalization(Path(value))


@pytest.mark.parametrize(
    "change",
    ["uid", "nonlink", "target", "lstat", "readlink", "relative-target"],
)
def test_alias_trust_branches_match_original(
    change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All original fixed-alias trust and failure branches remain enforced."""
    state = alias_trust(monkeypatch)
    if change == "uid":
        state["uid"] = 123
    elif change == "nonlink":
        state["mode"] = stat.S_IFDIR | 0o755
    elif change == "target":
        state["target"] = "/caller-controlled"
    elif change == "relative-target":
        state["target"] = "private/tmp"
    else:
        state["error"] = change
    path = Path("/tmp/work")
    assert paths.canonical_platform_path(path) == old_normalization(path)
    if change != "relative-target":
        assert paths.canonical_platform_path(path) == path


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_non_darwin_returns_absolute_without_alias_reads(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Other platforms preserve the early return before parts or OS checks."""
    state = alias_trust(monkeypatch)
    monkeypatch.setattr(paths, "sys", SimpleNamespace(platform=platform))
    assert paths.canonical_platform_path(Path("/tmp/x")) == Path("/tmp/x")
    assert state["calls"] == []


def test_each_call_rechecks_alias_metadata_and_link_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trust changes between calls cannot reuse an earlier alias decision."""
    state = alias_trust(monkeypatch)
    path = Path("/tmp/work")
    assert paths.canonical_platform_path(path) == Path("/private/tmp/work")
    state["uid"] = 123
    assert paths.canonical_platform_path(path) == path
    state["uid"] = 0
    state["target"] = "/caller-controlled"
    assert paths.canonical_platform_path(path) == path
    assert state["calls"] == [
        ("lstat", "/tmp"),
        ("readlink", "/tmp"),
        ("lstat", "/tmp"),
        ("readlink", "/tmp"),
        ("lstat", "/tmp"),
        ("readlink", "/tmp"),
    ]


@pytest.mark.skipif(sys.platform != "darwin", reason="protected macOS aliases")
def test_real_alias_and_caller_symlink_remain_separate(tmp_path: Path) -> None:
    """Real fixed OS aliases normalize while caller symlinks remain unresolved."""
    assert paths.canonical_platform_path(Path("/tmp")) == Path("/private/tmp")
    assert paths.canonical_platform_path(Path("/var")) == Path("/private/var")
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "caller-link"
    link.symlink_to(target, target_is_directory=True)
    normalized = paths.canonical_platform_path(link)
    assert normalized.name == "caller-link"
    assert normalized != paths.canonical_platform_path(target)
    assert normalized.is_symlink()
