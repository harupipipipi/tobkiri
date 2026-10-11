"""Fresh owned-file checks retain identity-field access and error semantics."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest

from core_runtime import secure_sqlite_path as paths


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative file tests")
def test_real_owned_metadata_identity_and_fresh_hazard_rejection(
    tmp_path: Path,
) -> None:
    """Every call checks current real metadata, links, symlinks and existence."""
    path = tmp_path / "owned.db"
    path.write_bytes(b"owned")
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fresh = paths.validate_owned_file_at(descriptor, path.name, required=True)
        assert fresh is not None
        assert paths._validate_regular(fresh) == paths.FileIdentity.from_stat(fresh)
        with paths.secure_parent(path) as parent:
            assert parent.validate_open(
                path.name, required=True
            ) == paths.FileIdentity.from_stat(fresh)
        symbolic = tmp_path / "symbolic.db"
        symbolic.symlink_to(path)
        with pytest.raises(paths.SecurePathError, match="file is not regular"):
            paths.validate_owned_file_at(descriptor, symbolic.name, required=True)
        linked = tmp_path / "linked.db"
        os.link(path, linked)
        with pytest.raises(paths.SecurePathError, match="exactly one link"):
            paths.validate_owned_file_at(descriptor, path.name, required=True)
        linked.unlink()
        assert (
            paths.validate_owned_file_at(descriptor, path.name, required=True)
            is not None
        )
        path.unlink()
        assert (
            paths.validate_owned_file_at(descriptor, path.name, required=False) is None
        )
        with pytest.raises(paths.SecurePathError, match="required file is unavailable"):
            paths.validate_owned_file_at(descriptor, path.name, required=True)
    finally:
        os.close(descriptor)


def _metadata_run(
    case: str, monkeypatch: pytest.MonkeyPatch, *, reference: bool
) -> tuple[Any, list[Any]]:
    """Compare original typed identity-return path against the fresh stat caller."""
    trace: list[Any] = []
    uid = os.getuid()
    mode_calls = 0
    uid_reads = 0

    class Mode:
        def __index__(self) -> int:
            nonlocal mode_calls
            mode_calls += 1
            trace.append(("index", mode_calls))
            if case.endswith("index_error") and mode_calls == 2:
                raise RuntimeError("original mode conversion")
            if case.endswith("stateful_mode") and mode_calls == 2:
                return stat.S_IFDIR | 0o700
            return stat.S_IFREG | 0o600

    class Metadata:
        def __getattr__(self, name: str) -> Any:
            nonlocal uid_reads
            trace.append(("field", name))
            if name == "st_file_attributes":
                if case == "reparse":
                    return getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                raise AttributeError(name)
            if name == "st_mode":
                return mode
            if name == "st_nlink":
                return 2 if case == "links" else 1
            if name == "st_uid":
                uid_reads += 1
                if case == "uid_error" and uid_reads == 2:
                    raise RuntimeError("original uid property")
                return uid + 1 if case == "owner" else uid
            if name == "st_dev":
                if case == "missing_dev":
                    raise AttributeError(name)
                return 3
            if name == "st_ino":
                if case == "missing_ino":
                    raise AttributeError(name)
                return 5
            raise AssertionError(name)

    mode = Mode()
    fresh: Any = Metadata()
    if case.startswith("stat_result"):
        fresh = os.stat_result((mode, 5, 3, 1, uid, 0, 0, 0, 0, 0))

    def original_stat(name: Any, **kwargs: Any) -> Any:
        assert name == "test.db" and kwargs == {"dir_fd": 41, "follow_symlinks": False}
        return fresh

    def original_uid() -> int:
        trace.append(("getuid",))
        return uid

    with monkeypatch.context() as patch:
        patch.setattr(paths.os, "stat", original_stat)
        patch.setattr(paths.os, "getuid", original_uid)
        patch.setattr(paths.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400, raising=False)
        try:
            if reference:
                result = paths._validate_regular(fresh)
                assert type(result) is paths.FileIdentity
            else:
                assert (
                    paths.validate_owned_file_at(41, "test.db", required=True) is fresh
                )
            outcome = ("returned",)
        except Exception as error:
            outcome = (type(error).__name__, str(error))
    return outcome, trace


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="current owner metadata checks")
@pytest.mark.parametrize(
    "case",
    [
        "normal",
        "missing_dev",
        "missing_ino",
        "uid_error",
        "index_error",
        "stateful_mode",
        "stat_result_mode",
        "stat_result_index_error",
        "stat_result_stateful_mode",
        "owner",
        "links",
        "reparse",
    ],
)
def test_identity_return_reference_preserves_extra_reads_and_failures(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Allocation-free path matches the unchanged identity-returning reference."""
    original = _metadata_run(case, monkeypatch, reference=True)
    fresh = _metadata_run(case, monkeypatch, reference=False)
    assert original == fresh
    if case in {"missing_dev", "missing_ino"}:
        assert fresh[0] == (
            "AttributeError",
            "st_dev" if case == "missing_dev" else "st_ino",
        )
    elif case in {"uid_error", "index_error", "stat_result_index_error"}:
        assert fresh[0] == (
            "RuntimeError",
            "original uid property"
            if case == "uid_error"
            else "original mode conversion",
        )
    elif case == "owner":
        assert fresh[0] == ("SecurePathError", "file is not owned by the current user")
    elif case == "links":
        assert fresh[0] == ("SecurePathError", "file does not have exactly one link")
    elif case == "reparse":
        assert fresh[0] == ("SecurePathError", "file is not regular")
    else:
        assert fresh[0] == ("returned",)
    if case == "normal":
        assert fresh[1] == [
            ("field", "st_file_attributes"),
            ("field", "st_mode"),
            ("index", 1),
            ("field", "st_nlink"),
            ("field", "st_uid"),
            ("getuid",),
            ("field", "st_dev"),
            ("field", "st_ino"),
            ("field", "st_uid"),
            ("field", "st_mode"),
            ("index", 2),
        ]


@pytest.mark.parametrize("required", [False, True])
def test_original_stat_permission_error_identity(
    monkeypatch: pytest.MonkeyPatch, required: bool
) -> None:
    """Permission errors propagate unchanged for either existence policy."""
    error = PermissionError("original OS refusal")
    calls: list[Any] = []

    def original_stat(name: Any, **kwargs: Any) -> Any:
        calls.append((name, kwargs))
        raise error

    monkeypatch.setattr(paths.os, "stat", original_stat)
    with pytest.raises(PermissionError) as caught:
        paths.validate_owned_file_at(41, "test.db", required=required)
    assert caught.value is error
    assert calls == [("test.db", {"dir_fd": 41, "follow_symlinks": False})]


def test_fresh_required_and_optional_missing_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only original missing-file handling translates required failure."""
    error = FileNotFoundError("original missing")
    calls = 0

    def original_stat(_name: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(paths.os, "stat", original_stat)
    assert paths.validate_owned_file_at(41, "test.db", required=False) is None
    with pytest.raises(
        paths.SecurePathError, match="required file is unavailable"
    ) as caught:
        paths.validate_owned_file_at(41, "test.db", required=True)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True
    assert calls == 2


def test_unused_identity_allocation_avoided_without_changing_reference_abi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh stat result avoids allocation; identity-return callers still allocate."""
    path = tmp_path / "owned.db"
    path.write_bytes(b"owned")
    fresh = path.stat()
    calls: list[Any] = []
    original = paths.FileIdentity.from_stat.__func__

    def counted(cls: Any, metadata: Any) -> Any:
        calls.append(metadata)
        return original(cls, metadata)

    monkeypatch.setattr(paths.FileIdentity, "from_stat", classmethod(counted))
    monkeypatch.setattr(paths.os, "stat", lambda *_args, **_kwargs: fresh)
    assert paths.validate_owned_file_at(41, "owned.db", required=True) is fresh
    assert calls == []
    assert type(paths._validate_regular(fresh)) is paths.FileIdentity
    assert calls == [fresh]
