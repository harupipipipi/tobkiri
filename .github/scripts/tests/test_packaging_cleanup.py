"""Deterministic coverage for scoped Windows packaging cleanup."""

from __future__ import annotations

import ctypes
import errno
import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from typing import Any, TypedDict

import pytest


def _load_cleanup_module() -> ModuleType:
    helper_path = (
        Path(__file__).resolve().parents[3]
        / "tobkiri_runtime/scripts/packaging_cleanup.py"
    )
    spec = importlib.util.spec_from_file_location(
        "packaging_cleanup_test_module", helper_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load cleanup helper: {helper_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cleanup = _load_cleanup_module()


class _WindowsLockError(PermissionError):
    """Permission error with a deterministic Windows error number."""

    def __init__(self, winerror: int) -> None:
        super().__init__(errno.EACCES, "simulated Windows cleanup lock")
        self._simulated_winerror = winerror

    @property
    def winerror(self) -> int:
        return self._simulated_winerror


def _windows_error(winerror: int) -> PermissionError:
    """Build a Windows-shaped access error on every test platform."""

    return _WindowsLockError(winerror)


def _make_owned_directory(tmp_path: Path) -> Path:
    target = tmp_path / "owned-transaction"
    target.mkdir()
    (target / "tobkiri-shell.exe").write_bytes(b"shell")
    return target


def _fake_stat(
    mode: int,
    *,
    device: int = 1,
    inode: int = 9001,
    file_attributes: int | None = 0,
) -> SimpleNamespace:
    """Create a platform-neutral lstat result for link/reparse simulations."""

    return SimpleNamespace(
        st_mode=mode,
        st_dev=device,
        st_ino=inode,
        st_file_attributes=file_attributes,
    )


def _patch_lstat_component(
    monkeypatch: pytest.MonkeyPatch,
    component: Path,
    result: SimpleNamespace,
) -> None:
    """Make one component appear unsafe while retaining real path structure."""

    original_lstat = cleanup._lstat_no_follow

    def lstat(path: Path):
        if Path(path) == component:
            return result
        return original_lstat(path)

    monkeypatch.setattr(cleanup, "_lstat_no_follow", lstat)


class _FakeHandleRecord(TypedDict):
    """State held by one disposable native-handle simulation."""

    path: Path
    volume_serial: int
    file_index: int
    attributes: int
    share_mode: int


class _FakeWindowsApi:
    """Disposable-fixture simulation of the native handle operations."""

    def __init__(
        self,
        *,
        open_failures: int = 0,
        identity_failures: set[int] | None = None,
        persistent_identity_failures: set[int] | None = None,
        close_failures: set[int] | None = None,
        persistent_close_failures: set[int] | None = None,
    ) -> None:
        self._next_handle = 100
        self.open_failures = open_failures
        self.identity_failures = set(identity_failures or ())
        self.persistent_identity_failures = set(persistent_identity_failures or ())
        self.close_failures = set(close_failures or ())
        self.persistent_close_failures = set(persistent_close_failures or ())
        self.handles: dict[int, _FakeHandleRecord] = {}
        self.open_share_modes: list[int] = []
        self.rename_calls: list[tuple[int, int, str]] = []
        self.close_attempts: list[int] = []
        self.identity_attempts: list[int] = []

    @staticmethod
    def _path_identity(path: Path, *, directory: bool) -> Any:
        """Model GetFileInformationByHandle for one no-follow path open."""

        result = cleanup._lstat_no_follow(path)
        attributes = int(getattr(result, "st_file_attributes", 0) or 0)
        if directory:
            attributes |= cleanup._WINDOWS_FILE_ATTRIBUTE_DIRECTORY
        return cleanup._WindowsFileIdentity(
            volume_serial=int(result.st_dev),
            file_index=int(result.st_ino),
            file_attributes=attributes,
        )

    def open(
        self,
        path: Path,
        *,
        directory: bool,
        share_mode: int = cleanup._WINDOWS_HANDLE_SHARE_MODE,
    ) -> int:
        if path.name == "owned.bin" and self.open_failures:
            self.open_failures -= 1
            raise _windows_error(32)
        if share_mode & cleanup._WINDOWS_FILE_SHARE_DELETE:
            raise AssertionError("simulation received FILE_SHARE_DELETE")
        self.open_share_modes.append(share_mode)
        identity = self._path_identity(path, directory=directory)
        handle = self._next_handle
        self._next_handle += 1
        self.handles[handle] = {
            "path": Path(path),
            "volume_serial": identity.volume_serial,
            "file_index": identity.file_index,
            "attributes": identity.file_attributes,
            "share_mode": share_mode,
        }
        return handle

    def identity(self, handle: int) -> Any:
        self.identity_attempts.append(handle)
        if handle in self.persistent_identity_failures:
            raise OSError(errno.EIO, "simulated identity failure")
        if handle in self.identity_failures:
            self.identity_failures.remove(handle)
            raise OSError(errno.EIO, "simulated identity failure")
        record = self.handles[handle]
        return cleanup._WindowsFileIdentity(
            volume_serial=int(record["volume_serial"]),
            file_index=int(record["file_index"]),
            file_attributes=int(record["attributes"]),
        )

    def path_identity(self, path: Path, *, directory: bool) -> Any:
        return self._path_identity(path, directory=directory)

    def rename_same_parent(
        self,
        handle: int,
        parent_handle: int,
        _parent_path: Path,
        name: str,
    ) -> None:
        target = Path(self.handles[handle]["path"])
        parent = Path(self.handles[parent_handle]["path"])
        quarantine = parent / name
        self.rename_calls.append((handle, parent_handle, name))
        target.rename(quarantine)
        self.handles[handle]["path"] = quarantine

    def mark_delete(self, handle: int) -> None:
        path = Path(self.handles[handle]["path"])
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()

    def close(self, handle: int) -> None:
        self.close_attempts.append(handle)
        if handle in self.persistent_close_failures:
            raise _windows_error(5)
        if handle in self.close_failures:
            self.close_failures.remove(handle)
            raise _windows_error(5)
        self.handles.pop(handle)

    def attempt_rename(self, source: Path, destination: Path) -> None:
        """Simulate a competing delete/rename open while handles are held."""

        source = Path(source)
        for record in self.handles.values():
            held_path = Path(record["path"])
            try:
                held_path.relative_to(source)
            except ValueError:
                continue
            if not record["share_mode"] & cleanup._WINDOWS_FILE_SHARE_DELETE:
                raise _windows_error(32)
        source.rename(destination)


def _use_fake_windows_native_api(
    monkeypatch: pytest.MonkeyPatch,
    fake_api: _FakeWindowsApi,
) -> None:
    """Enable the native path against the disposable fake API only."""

    monkeypatch.setattr(cleanup, "_IS_WINDOWS", True)
    monkeypatch.setattr(cleanup, "_REAL_WINDOWS", True)
    monkeypatch.setattr(cleanup, "_WINDOWS_API", fake_api)


def test_fake_windows_path_probe_matches_bound_handle_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fake models one native volume/file/attribute identity domain."""

    target = tmp_path / "owned.bin"
    target.write_bytes(b"owned")
    file_attributes = 0x20  # FILE_ATTRIBUTE_ARCHIVE on Windows fixtures.
    _patch_lstat_component(
        monkeypatch,
        target,
        _fake_stat(
            stat.S_IFREG,
            device=17,
            inode=23,
            file_attributes=file_attributes,
        ),
    )
    fake_api = _FakeWindowsApi()

    handle = fake_api.open(target, directory=False)
    bound = fake_api.identity(handle)
    probed = fake_api.path_identity(target, directory=False)

    assert bound == probed
    assert bound.volume_serial == 17
    assert bound.file_index == 23
    assert bound.file_attributes == file_attributes
    fake_api.close(handle)
    assert fake_api.handles == {}


def test_transient_windows_lock_retries_then_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recognized sharing violation is retried and then removed."""

    target = _make_owned_directory(tmp_path)
    failures = [_windows_error(32)]
    original_remove = cleanup._remove_once
    calls = 0

    def remove_with_transient_lock(path: Path, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if failures:
            raise failures.pop(0)
        original_remove(path, **kwargs)

    monkeypatch.setattr(cleanup, "_IS_WINDOWS", True)
    monkeypatch.setattr(cleanup, "_remove_once", remove_with_transient_lock)
    sleeps: list[float] = []

    cleanup.remove_owned_path(
        target,
        owner_root=tmp_path,
        operation="test transient cleanup",
        sleep=sleeps.append,
    )

    assert calls == 2
    assert sleeps == [0.1]
    assert not target.exists()
    assert not list(tmp_path.glob(".tobkiri-cleanup-*"))


def test_windows_native_file_cleanup_uses_bound_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native simulation renames and deletes the originally bound file."""

    scope = tmp_path / "scope"
    scope.mkdir()
    target = scope / "owned.bin"
    target.write_bytes(b"owned")
    fake_api = _FakeWindowsApi()
    _use_fake_windows_native_api(monkeypatch, fake_api)

    cleanup.remove_owned_path(
        target,
        owner_root=scope,
        operation="test native file cleanup",
    )

    assert not target.exists()
    assert len(fake_api.rename_calls) == 1
    assert not list(scope.glob(".tobkiri-cleanup-*"))


def test_windows_rename_info_uses_absolute_same_parent_utf16(tmp_path: Path) -> None:
    """The Win32 rename buffer uses an absolute held-parent sibling path."""

    calls: list[tuple[int, int, bytes]] = []
    api = cleanup._WindowsApi.__new__(cleanup._WindowsApi)

    def set_file_information(
        handle: Any,
        information_class: int,
        information: Any,
        buffer_size: int,
    ) -> bool:
        address = ctypes.cast(information, ctypes.c_void_p).value
        assert address is not None
        assert address % ctypes.alignment(cleanup._WindowsFileRenameInfo) == 0
        calls.append(
            (
                int(getattr(handle, "value", handle)),
                information_class,
                ctypes.string_at(address, buffer_size),
            )
        )
        return True

    api._set_file_information = set_file_information
    name = ".tobkiri-cleanup-native"
    api.rename_same_parent(41, 42, tmp_path, name)

    assert len(calls) == 1
    handle, information_class, raw = calls[0]
    assert handle == 41
    assert information_class == cleanup._WINDOWS_FILE_RENAME_INFO_CLASS
    assert len(raw) % ctypes.alignment(cleanup._WindowsFileRenameInfo) == 0
    information = cleanup._WindowsFileRenameInfo.from_buffer_copy(raw)
    assert information.Flags == 0
    assert information.RootDirectory is None
    encoded_name = cleanup._windows_absolute_path(tmp_path / name).encode(
        "utf-16-le"
    )
    assert information.FileNameLength == len(encoded_name)
    offset = cleanup._WindowsFileRenameInfo.FileName.offset
    assert raw[offset : offset + len(encoded_name)] == encoded_name
    assert raw[offset + len(encoded_name) : offset + len(encoded_name) + 2] == b"\0\0"


def test_windows_rename_info_accepts_unicode_long_same_parent_name(
    tmp_path: Path,
) -> None:
    """A long Unicode basename remains an absolute held-parent sibling."""

    calls: list[bytes] = []
    api = cleanup._WindowsApi.__new__(cleanup._WindowsApi)

    def set_file_information(
        _handle: Any,
        _information_class: int,
        information: Any,
        buffer_size: int,
    ) -> bool:
        address = ctypes.cast(information, ctypes.c_void_p).value
        assert address is not None
        calls.append(ctypes.string_at(address, buffer_size))
        return True

    api._set_file_information = set_file_information
    name = ".tobkiri-" + "安全" * 140
    api.rename_same_parent(41, 42, tmp_path, name)

    information = cleanup._WindowsFileRenameInfo.from_buffer_copy(calls[0])
    assert information.Flags == 0
    assert information.RootDirectory is None
    encoded_name = cleanup._windows_absolute_path(tmp_path / name).encode(
        "utf-16-le"
    )
    assert len(encoded_name) > 260
    offset = cleanup._WindowsFileRenameInfo.FileName.offset
    assert information.FileNameLength == len(encoded_name)
    assert calls[0][offset : offset + len(encoded_name)] == encoded_name


def test_windows_quarantine_collision_retries_in_same_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A colliding quarantine basename never redirects to another volume."""

    scope = tmp_path / "checkout"
    scope.mkdir()
    target = scope / "owned.bin"
    target.write_bytes(b"owned")
    (scope / ".tobkiri-cleanup-first").write_bytes(b"collision")
    values = iter(("first", "second"))
    monkeypatch.setattr(
        cleanup.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex=next(values)),
    )

    quarantine = cleanup._new_quarantine_path(
        target,
        operation="test same-parent quarantine collision",
    )

    assert quarantine.parent == target.parent
    assert quarantine.name == ".tobkiri-cleanup-second"
    assert "/" not in quarantine.name
    assert "\\" not in quarantine.name


def test_windows_quarantine_rejects_cross_volume_bound_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source/parent volume mismatch fails before native rename."""

    scope = tmp_path / "scope"
    scope.mkdir()
    target = scope / "owned.bin"
    target.write_bytes(b"owned")
    fake_api = _FakeWindowsApi()
    original_identity = fake_api.identity
    original_path_identity = fake_api.path_identity

    def cross_volume_identity(handle: int) -> Any:
        identity = original_identity(handle)
        if Path(fake_api.handles[handle]["path"]) == target:
            return cleanup._WindowsFileIdentity(
                volume_serial=identity.volume_serial + 1,
                file_index=identity.file_index,
                file_attributes=identity.file_attributes,
            )
        return identity

    def cross_volume_path_identity(path: Path, *, directory: bool) -> Any:
        identity = original_path_identity(path, directory=directory)
        if path == target:
            return cleanup._WindowsFileIdentity(
                volume_serial=identity.volume_serial + 1,
                file_index=identity.file_index,
                file_attributes=identity.file_attributes,
            )
        return identity

    fake_api.identity = cross_volume_identity  # type: ignore[assignment]
    fake_api.path_identity = cross_volume_path_identity  # type: ignore[assignment]
    _use_fake_windows_native_api(monkeypatch, fake_api)

    with pytest.raises(cleanup.PackagingCleanupError, match="different volumes"):
        cleanup.remove_owned_path(
            target,
            owner_root=scope,
            operation="test cross-volume quarantine rejection",
        )

    assert target.exists()
    assert fake_api.rename_calls == []


@pytest.mark.parametrize("name", ["", ".", "..", "nested/name", "nested\\name", "x\0y"])
def test_windows_rename_info_rejects_non_simple_names(
    name: str, tmp_path: Path
) -> None:
    """A quarantine rename cannot smuggle a path into FILE_RENAME_INFO."""

    api = cleanup._WindowsApi.__new__(cleanup._WindowsApi)
    api._set_file_information = lambda *_args: True

    with pytest.raises(ValueError, match="simple filename"):
        api.rename_same_parent(41, 42, tmp_path, name)


def test_windows_rename_info_rejects_relative_parent() -> None:
    """A relative parent cannot redirect an absolute quarantine rename."""

    api = cleanup._WindowsApi.__new__(cleanup._WindowsApi)
    api._set_file_information = lambda *_args: True

    with pytest.raises(ValueError, match="absolute parent"):
        api.rename_same_parent(41, 42, Path("scope"), "name")


def test_windows_directory_open_requests_traverse_and_read_attributes() -> None:
    """Held parent directories request every right used by the trust walk."""

    accesses: list[int] = []
    api = cleanup._WindowsApi.__new__(cleanup._WindowsApi)

    def create_file(
        _path: str,
        access: int,
        _share_mode: int,
        _security: object,
        _creation: int,
        _flags: int,
        _template: object,
    ) -> int:
        accesses.append(access)
        return 123

    api._create_file = create_file
    assert api.open(Path("held-parent"), directory=True) == 123
    assert len(accesses) == 1
    assert accesses[0] & cleanup._WINDOWS_FILE_LIST_DIRECTORY
    assert accesses[0] & cleanup._WINDOWS_FILE_TRAVERSE
    assert accesses[0] & cleanup._WINDOWS_FILE_READ_ATTRIBUTES


def test_windows_path_probe_is_nofollow_identity_only_and_closes() -> None:
    """Path remap probes are compatible with held no-delete-sharing handles."""

    calls: list[tuple[int, int, int]] = []
    closed: list[int] = []
    api = cleanup._WindowsApi.__new__(cleanup._WindowsApi)
    expected = cleanup._WindowsFileIdentity(7, 11, 0)

    def create_file(
        _path: str,
        access: int,
        share_mode: int,
        _security: object,
        _creation: int,
        flags: int,
        _template: object,
    ) -> int:
        calls.append((access, share_mode, flags))
        return 321

    api._create_file = create_file
    api.identity = lambda handle: expected if handle == 321 else None
    api.close = closed.append

    actual = api.path_identity(Path("C:/scope/owned.bin"), directory=False)

    assert actual == expected
    assert calls == [
        (
            cleanup._WINDOWS_FILE_READ_ATTRIBUTES,
            cleanup._WINDOWS_FILE_SHARE_READ
            | cleanup._WINDOWS_FILE_SHARE_WRITE
            | cleanup._WINDOWS_FILE_SHARE_DELETE,
            cleanup._WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
        )
    ]
    assert closed == [321]


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows handles")
def test_windows_native_quarantine_rename_accepts_packager_output(
    tmp_path: Path,
) -> None:
    """Native SetFileInformationByHandle accepts the quarantine rename ABI."""

    scope = tmp_path / "scope"
    scope.mkdir()
    target = scope / "owned.bin"
    target.write_bytes(b"owned")
    setattr(cleanup, "_WINDOWS_API", None)

    cleanup.remove_owned_path(
        target,
        owner_root=scope,
        operation="test native quarantine ABI",
    )

    assert not target.exists()
    assert not list(scope.glob(".tobkiri-cleanup-*"))


def test_windows_bound_chain_excludes_delete_sharing_and_blocks_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every held chain handle blocks competing component moves."""

    scope = tmp_path / "scope"
    nested = scope / "nested"
    target = nested / "owned.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"owned")
    fake_api = _FakeWindowsApi()
    _use_fake_windows_native_api(monkeypatch, fake_api)

    binding = cleanup._bind_owned_path(
        target,
        scope,
        operation="test no-delete-sharing binding",
    )
    try:
        expected_share_mode = (
            cleanup._WINDOWS_FILE_SHARE_READ | cleanup._WINDOWS_FILE_SHARE_WRITE
        )
        assert fake_api.open_share_modes
        assert all(
            share_mode == expected_share_mode
            and not share_mode & cleanup._WINDOWS_FILE_SHARE_DELETE
            for share_mode in fake_api.open_share_modes
        )
        assert binding.windows_state is not None
        assert len(binding.windows_state.ancestor_handles) == 2
        assert binding.windows_state.target_handle is not None

        for source, name in (
            (scope, "scope-moved"),
            (nested, "nested-moved"),
            (target, "target-moved.bin"),
        ):
            with pytest.raises(_WindowsLockError):
                fake_api.attempt_rename(source, tmp_path / name)
            assert source.exists()
    finally:
        binding.close()

    moved_nested = tmp_path / "nested-moved-after-close"
    fake_api.attempt_rename(nested, moved_nested)
    assert not nested.exists()
    assert (moved_nested / "owned.bin").exists()


def test_windows_native_binding_retries_transient_handle_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient target-open sharing violation uses bounded binding retry."""

    scope = tmp_path / "scope"
    scope.mkdir()
    target = scope / "owned.bin"
    target.write_bytes(b"owned")
    fake_api = _FakeWindowsApi(open_failures=1)
    _use_fake_windows_native_api(monkeypatch, fake_api)
    sleeps: list[float] = []

    cleanup.remove_owned_path(
        target,
        owner_root=scope,
        operation="test native binding retry",
        sleep=sleeps.append,
    )

    assert sleeps == [0.1]
    assert not target.exists()
    assert fake_api.handles == {}


def test_windows_partial_binding_one_shot_close_reaches_zero_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partial construction retries a transient close and releases all handles."""

    scope = tmp_path / "scope"
    nested = scope / "nested"
    target = nested / "owned.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"owned")
    fake_api = _FakeWindowsApi(open_failures=1, close_failures={101})
    _use_fake_windows_native_api(monkeypatch, fake_api)

    cleanup.remove_owned_path(
        target,
        owner_root=scope,
        operation="test partial one-shot close",
    )

    assert fake_api.handles == {}
    assert not target.exists()


def test_windows_partial_binding_persistent_close_retains_handle_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partial construction never retries after persistent close ownership loss."""

    scope = tmp_path / "scope"
    nested = scope / "nested"
    target = nested / "owned.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"owned")
    fake_api = _FakeWindowsApi(open_failures=1, persistent_close_failures={101})
    _use_fake_windows_native_api(monkeypatch, fake_api)

    with pytest.raises(cleanup.PackagingCleanupError, match="retained handles"):
        cleanup.remove_owned_path(
            target,
            owner_root=scope,
            operation="test partial persistent close",
        )

    assert set(fake_api.handles) == {101}
    assert target.exists()


def test_windows_ancestor_identity_failure_closes_new_handle_once_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ancestor is owned before identity failure and one-shot close drains it."""

    scope = tmp_path / "scope"
    nested = scope / "nested"
    target = nested / "owned.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"owned")
    fake_api = _FakeWindowsApi(identity_failures={100}, close_failures={100})
    _use_fake_windows_native_api(monkeypatch, fake_api)

    with pytest.raises(cleanup.PackagingCleanupError) as raised:
        cleanup.remove_owned_path(
            target,
            owner_root=scope,
            operation="test ancestor identity one-shot close",
        )

    assert isinstance(raised.value.__cause__, OSError)
    assert "simulated identity failure" in str(raised.value.__cause__)
    assert fake_api.close_attempts == [100, 100]
    assert fake_api.handles == {}
    assert target.exists()


def test_windows_ancestor_identity_failure_retains_persistent_close_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ancestor identity failure reports a persistent close residue."""

    scope = tmp_path / "scope"
    nested = scope / "nested"
    target = nested / "owned.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"owned")
    fake_api = _FakeWindowsApi(
        identity_failures={100},
        persistent_close_failures={100},
    )
    _use_fake_windows_native_api(monkeypatch, fake_api)

    with pytest.raises(
        cleanup.PackagingCleanupError, match="retained handles"
    ) as raised:
        cleanup.remove_owned_path(
            target,
            owner_root=scope,
            operation="test ancestor identity persistent close",
        )

    assert isinstance(raised.value.__cause__, OSError)
    assert "simulated identity failure" in str(raised.value.__cause__)
    assert fake_api.close_attempts == [100, 100]
    assert set(fake_api.handles) == {100}
    assert [record.handle for record in raised.value.unclosed_windows] == [100]
    assert [record.handle for record in raised.value.cleanup_unclosed_windows] == [100]
    assert target.exists()


def test_windows_native_tree_cleanup_uses_child_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native simulation recursively deletes children by identity-bound handle."""

    scope = tmp_path / "scope"
    target = scope / "owned"
    (target / "nested").mkdir(parents=True)
    (target / "file.txt").write_text("owned", encoding="utf-8")
    (target / "nested" / "child.txt").write_text("owned", encoding="utf-8")
    fake_api = _FakeWindowsApi()
    _use_fake_windows_native_api(monkeypatch, fake_api)

    cleanup.remove_owned_path(
        target,
        owner_root=scope,
        operation="test native tree cleanup",
    )

    assert not target.exists()
    assert not list(scope.glob(".tobkiri-cleanup-*"))
    assert fake_api.handles == {}


def test_windows_target_one_shot_close_failure_retries_to_zero_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target close failure is retried after identity validation."""

    scope = tmp_path / "scope"
    scope.mkdir()
    target = scope / "owned.bin"
    target.write_bytes(b"owned")
    fake_api = _FakeWindowsApi(close_failures={101})
    _use_fake_windows_native_api(monkeypatch, fake_api)

    cleanup.remove_owned_path(
        target,
        owner_root=scope,
        operation="test target close failure",
    )

    assert fake_api.close_attempts == [101, 101, 100]
    assert fake_api.handles == {}
    assert not target.exists()


def test_windows_ancestor_one_shot_close_failure_retries_to_zero_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ancestor close failure is retried without losing handle ownership."""

    scope = tmp_path / "scope"
    nested = scope / "nested"
    target = nested / "owned.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"owned")
    fake_api = _FakeWindowsApi(close_failures={100})
    _use_fake_windows_native_api(monkeypatch, fake_api)

    cleanup.remove_owned_path(
        target,
        owner_root=scope,
        operation="test ancestor close failure",
    )

    assert fake_api.close_attempts == [102, 101, 100, 100]
    assert fake_api.handles == {}
    assert not target.exists()


def test_windows_recursive_child_one_shot_close_retries_to_zero_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recursive child close failure is retried before parent deletion."""

    scope = tmp_path / "scope"
    target = scope / "owned"
    target.mkdir(parents=True)
    (target / "child.txt").write_text("owned", encoding="utf-8")
    fake_api = _FakeWindowsApi(close_failures={102})
    _use_fake_windows_native_api(monkeypatch, fake_api)

    cleanup.remove_owned_path(
        target,
        owner_root=scope,
        operation="test recursive child close failure",
    )

    assert fake_api.close_attempts == [102, 102, 101, 100]
    assert fake_api.handles == {}
    assert not target.exists()


def test_windows_persistent_target_close_retains_handle_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persistent target close failure retains the handle and fails closed."""

    scope = tmp_path / "scope"
    scope.mkdir()
    target = scope / "owned.bin"
    target.write_bytes(b"owned")
    fake_api = _FakeWindowsApi(persistent_close_failures={101})
    _use_fake_windows_native_api(monkeypatch, fake_api)

    with pytest.raises(
        cleanup.PackagingCleanupError, match="retained handles"
    ) as raised:
        cleanup.remove_owned_path(
            target,
            owner_root=scope,
            operation="test persistent target close failure",
        )

    assert fake_api.close_attempts == [101, 101, 100]
    assert set(fake_api.handles) == {101}
    assert raised.value.unclosed_windows[0].handle == 101
    assert not target.exists()


def test_windows_persistent_ancestor_close_retains_handle_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persistent ancestor close failure retains that ancestor ownership."""

    scope = tmp_path / "scope"
    nested = scope / "nested"
    target = nested / "owned.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"owned")
    fake_api = _FakeWindowsApi(persistent_close_failures={100})
    _use_fake_windows_native_api(monkeypatch, fake_api)

    with pytest.raises(cleanup.PackagingCleanupError, match="retained handles"):
        cleanup.remove_owned_path(
            target,
            owner_root=scope,
            operation="test persistent ancestor close failure",
        )

    assert fake_api.close_attempts == [102, 101, 100, 100]
    assert set(fake_api.handles) == {100}
    assert target.exists() is False


def test_windows_close_retry_rejects_reused_handle_identity(
    tmp_path: Path,
) -> None:
    """A failed close is never retried against a reused handle number."""

    target = tmp_path / "owned.bin"
    target.write_bytes(b"owned")
    fake_api = _FakeWindowsApi(close_failures={100})
    handle = fake_api.open(target, directory=False)
    identity = fake_api.identity(handle)

    def changed_identity(_handle: int) -> Any:
        return cleanup._WindowsFileIdentity(
            volume_serial=identity.volume_serial,
            file_index=identity.file_index + 1,
            file_attributes=identity.file_attributes,
        )

    fake_api.identity = changed_identity  # type: ignore[assignment]
    report = cleanup._close_windows_handle(
        fake_api,
        cleanup._WindowsHandleRecord(target, handle, identity),
    )

    assert len(report.errors) == 2
    assert [handle for handle in fake_api.close_attempts] == [100]
    assert [record.handle for record in report.unclosed] == [100]
    assert set(fake_api.handles) == {100}


def test_windows_persistent_recursive_child_close_retains_residue_and_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persistent child close failure leaves safe quarantine residue and handle."""

    scope = tmp_path / "scope"
    target = scope / "owned"
    target.mkdir(parents=True)
    (target / "child.txt").write_text("owned", encoding="utf-8")
    fake_api = _FakeWindowsApi(persistent_close_failures={102})
    _use_fake_windows_native_api(monkeypatch, fake_api)

    with pytest.raises(cleanup.PackagingCleanupError, match="retained handles"):
        cleanup.remove_owned_path(
            target,
            owner_root=scope,
            operation="test persistent recursive child close failure",
        )

    assert fake_api.close_attempts == [102, 102, 101, 100]
    assert set(fake_api.handles) == {102}
    quarantine = next(scope.glob(".tobkiri-cleanup-*"))
    assert quarantine.is_dir()


def test_windows_recursive_child_identity_failure_one_shot_close_preserves_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A one-shot child close failure does not leak after identity failure."""

    scope = tmp_path / "scope"
    target = scope / "owned"
    target.mkdir(parents=True)
    (target / "child.txt").write_text("owned", encoding="utf-8")
    fake_api = _FakeWindowsApi(identity_failures={102}, close_failures={102})
    _use_fake_windows_native_api(monkeypatch, fake_api)

    with pytest.raises(cleanup.PackagingCleanupError) as raised:
        cleanup.remove_owned_path(
            target,
            owner_root=scope,
            operation="test child identity one-shot close",
        )

    assert isinstance(raised.value.__cause__, OSError)
    assert "simulated identity failure" in str(raised.value.__cause__)
    assert fake_api.close_attempts == [102, 102, 101, 100]
    assert fake_api.handles == {}
    quarantine = next(scope.glob(".tobkiri-cleanup-*"))
    assert quarantine.is_dir()


def test_windows_recursive_child_identity_failure_reports_persistent_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child identity failure propagates retained close metadata and cause."""

    scope = tmp_path / "scope"
    target = scope / "owned"
    target.mkdir(parents=True)
    (target / "child.txt").write_text("owned", encoding="utf-8")
    fake_api = _FakeWindowsApi(
        identity_failures={102},
        persistent_close_failures={102},
    )
    _use_fake_windows_native_api(monkeypatch, fake_api)

    with pytest.raises(
        cleanup.PackagingCleanupError, match="retained handles"
    ) as raised:
        cleanup.remove_owned_path(
            target,
            owner_root=scope,
            operation="test child identity persistent close",
        )

    assert isinstance(raised.value.__cause__, OSError)
    assert "simulated identity failure" in str(raised.value.__cause__)
    assert fake_api.close_attempts == [102, 102, 101, 100]
    assert set(fake_api.handles) == {102}
    assert [record.handle for record in raised.value.unclosed_windows] == [102]
    assert [record.handle for record in raised.value.cleanup_unclosed_windows] == [102]
    cause = raised.value.__cause__
    assert cause is not None
    assert [
        record.handle for record in getattr(cause, "cleanup_unclosed_windows", ())
    ] == [102]
    quarantine = next(scope.glob(".tobkiri-cleanup-*"))
    assert quarantine.is_dir()


def test_close_failure_is_not_allowed_to_mask_primary_cleanup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The original cleanup error remains primary while close failure is noted."""

    scope = tmp_path / "scope"
    scope.mkdir()
    target = scope / "owned.bin"
    target.write_bytes(b"owned")
    fake_api = _FakeWindowsApi(persistent_close_failures={101})
    _use_fake_windows_native_api(monkeypatch, fake_api)

    def fail_remove(_path: Path, **_kwargs: object) -> None:
        raise OSError(errno.EIO, "primary cleanup failure")

    monkeypatch.setattr(cleanup, "_remove_once", fail_remove)

    with pytest.raises(cleanup.PackagingCleanupError) as raised:
        cleanup.remove_owned_path(
            target,
            owner_root=scope,
            operation="test primary plus close failure",
        )

    assert raised.value.diagnostic.reason == "non-retryable cleanup error"
    assert "failed to close" in str(raised.value)
    assert fake_api.close_attempts == [101, 101, 100]
    assert set(fake_api.handles) == {101}
    assert target.exists()


def test_windows_component_swap_at_mutation_boundary_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A boundary ancestor swap cannot redirect native quarantine or deletion."""

    scope = tmp_path / "scope"
    nested = scope / "nested"
    target = nested / "owned"
    target.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("must remain", encoding="utf-8")
    fake_api = _FakeWindowsApi()
    _use_fake_windows_native_api(monkeypatch, fake_api)
    hook_calls: list[Path] = []

    def replace_ancestor(path: Path) -> None:
        hook_calls.append(path)
        nested.rename(outside / "moved-nested")
        nested.mkdir()

    monkeypatch.setattr(
        cleanup,
        "_BEFORE_WINDOWS_QUARANTINE_MUTATION",
        replace_ancestor,
    )

    with pytest.raises(cleanup.PackagingCleanupError, match="identity changed"):
        cleanup.remove_owned_path(
            target,
            owner_root=scope,
            operation="test native boundary swap",
        )

    assert hook_calls == [target]
    assert fake_api.rename_calls == []
    assert (outside / "moved-nested" / "owned").exists()
    assert sentinel.read_text(encoding="utf-8") == "must remain"
    assert not list(scope.glob(".tobkiri-cleanup-*"))


def test_symlinked_ancestor_is_rejected_without_following(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scoped-looking path cannot traverse a symlinked ancestor."""

    scope = tmp_path / "scope"
    nested = scope / "nested"
    target = nested / "owned"
    target.mkdir(parents=True)
    _patch_lstat_component(monkeypatch, nested, _fake_stat(stat.S_IFLNK))

    with pytest.raises(cleanup.PackagingCleanupError, match="symlink or reparse"):
        cleanup.remove_owned_path(
            target,
            owner_root=scope,
            operation="test symlink ancestor cleanup",
        )

    assert target.exists()


@pytest.mark.skipif(cleanup._IS_WINDOWS, reason="POSIX descriptor contract")
def test_recursive_cleanup_does_not_call_shutil_rmtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python 3.10 cleanup uses the local fd walker, not rmtree(dir_fd=...)."""

    target = tmp_path / "owned" / "nested"
    (target / "deeper").mkdir(parents=True)
    (target / "deeper" / "payload.bin").write_bytes(b"payload")

    def forbidden_rmtree(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"shutil.rmtree must not be called: {args!r} {kwargs!r}")

    monkeypatch.setattr(cleanup.shutil, "rmtree", forbidden_rmtree)
    cleanup.remove_owned_path(
        target,
        owner_root=tmp_path / "owned",
        operation="test Python 3.10 descriptor cleanup",
    )

    assert not target.exists()


@pytest.mark.skipif(cleanup._IS_WINDOWS, reason="POSIX sealed-tree contract")
def test_manifest_bound_read_only_tree_is_unsealed_and_removed(tmp_path: Path) -> None:
    """Only a complete host-owned manifest tree may be unsealed for reset."""

    owner = tmp_path / "owned"
    target = owner / "tree"
    bundle = target / "bundle"
    bundle.mkdir(parents=True)
    (bundle / "global_contract_types.py").write_text("sealed\n", encoding="utf-8")
    (target / "runtime-resource-manifest.v1.json").write_text(
        "manifest\n", encoding="utf-8"
    )
    (bundle / "global_contract_types.py").chmod(0o444)
    bundle.chmod(0o555)
    target.chmod(0o555)
    expected = {
        "runtime-resource-manifest.v1.json": False,
        "bundle/global_contract_types.py": False,
    }

    cleanup.remove_owned_path(
        target,
        owner_root=owner,
        operation="test sealed tree reset",
        expected_tree=expected,
        unseal_read_only=True,
    )

    assert not target.exists()


@pytest.mark.skipif(cleanup._IS_WINDOWS, reason="POSIX sealed-tree contract")
@pytest.mark.parametrize("case", ("missing", "extra", "symlink", "hardlink"))
def test_manifest_bound_read_only_tree_rejects_drift(
    tmp_path: Path, case: str
) -> None:
    """Manifest, link, and exact-tree drift fail before unseal or quarantine."""

    owner = tmp_path / "owned"
    target = owner / "tree"
    bundle = target / "bundle"
    bundle.mkdir(parents=True)
    payload = bundle / "global_contract_types.py"
    payload.write_text("sealed\n", encoding="utf-8")
    manifest = target / "runtime-resource-manifest.v1.json"
    manifest.write_text("manifest\n", encoding="utf-8")
    expected = {
        "runtime-resource-manifest.v1.json": False,
        "bundle/global_contract_types.py": False,
    }
    external = tmp_path / "external-victim"
    external.write_text("must remain\n", encoding="utf-8")

    if case == "missing":
        payload.unlink()
    elif case == "extra":
        (target / "unlisted-extra").write_text("extra\n", encoding="utf-8")
    else:
        bundle.chmod(0o755)
        payload.unlink()
        if case == "symlink":
            payload.symlink_to(external)
        else:
            os.link(external, payload)
    if not bundle.is_symlink():
        bundle.chmod(0o555)
    if case == "extra":
        (target / "unlisted-extra").chmod(0o444)
    target.chmod(0o555)

    with pytest.raises(cleanup.PackagingCleanupError, match="sealed"):
        cleanup.remove_owned_path(
            target,
            owner_root=owner,
            operation="test sealed tree drift",
            expected_tree=expected,
            unseal_read_only=True,
        )

    assert target.exists()
    assert external.read_text(encoding="utf-8") == "must remain\n"
    assert not list(owner.glob(".tobkiri-cleanup-*"))
    for item in sorted(
        tmp_path.rglob("*"), key=lambda value: len(value.parts), reverse=True
    ):
        if item.is_symlink():
            continue
        try:
            item.chmod(0o755 if item.is_dir() else 0o644)
        except OSError:
            pass


@pytest.mark.skipif(cleanup._IS_WINDOWS, reason="POSIX sealed-tree contract")
def test_manifest_bound_read_only_tree_rejects_foreign_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree owned by another UID is never made writable for cleanup."""

    owner = tmp_path / "owned"
    target = owner / "tree"
    (target / "bundle").mkdir(parents=True)
    (target / "bundle/global_contract_types.py").write_text(
        "sealed\n", encoding="utf-8"
    )
    (target / "runtime-resource-manifest.v1.json").write_text(
        "manifest\n", encoding="utf-8"
    )
    (target / "bundle").chmod(0o555)
    target.chmod(0o555)
    monkeypatch.setattr(cleanup, "_posix_owner", lambda _result: os.geteuid() + 1)

    with pytest.raises(cleanup.PackagingCleanupError, match="host-owned"):
        cleanup.remove_owned_path(
            target,
            owner_root=owner,
            operation="test foreign sealed tree",
            expected_tree={
                "runtime-resource-manifest.v1.json": False,
                "bundle/global_contract_types.py": False,
            },
            unseal_read_only=True,
        )

    assert target.exists()
    target.chmod(0o755)
    (target / "bundle").chmod(0o755)


@pytest.mark.skipif(cleanup._IS_WINDOWS, reason="POSIX sealed-tree contract")
def test_manifest_bound_read_only_tree_rejects_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target identity swap at quarantine is rejected after unseal."""

    owner = tmp_path / "owned"
    target = owner / "tree"
    bundle = target / "bundle"
    bundle.mkdir(parents=True)
    (bundle / "global_contract_types.py").write_text("sealed\n", encoding="utf-8")
    (target / "runtime-resource-manifest.v1.json").write_text(
        "manifest\n", encoding="utf-8"
    )
    bundle.chmod(0o555)
    target.chmod(0o555)
    original = owner / "tree-original"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "victim").write_text("must remain\n", encoding="utf-8")
    swapped = False

    def replace_target(path: Path) -> None:
        nonlocal swapped
        if path == target and not swapped:
            swapped = True
            target.rename(original)
            replacement.rename(target)

    monkeypatch.setattr(cleanup, "_BEFORE_POSIX_MUTATION", replace_target)
    with pytest.raises(cleanup.PackagingCleanupError, match="identity"):
        cleanup.remove_owned_path(
            target,
            owner_root=owner,
            operation="test sealed target swap",
            expected_tree={
                "runtime-resource-manifest.v1.json": False,
                "bundle/global_contract_types.py": False,
            },
            unseal_read_only=True,
        )

    assert swapped
    assert (target / "victim").read_text(encoding="utf-8") == "must remain\n"
    assert (original / "bundle/global_contract_types.py").exists()
    for item in sorted(
        tmp_path.rglob("*"), key=lambda value: len(value.parts), reverse=True
    ):
        if item.is_symlink():
            continue
        try:
            item.chmod(0o755 if item.is_dir() else 0o644)
        except OSError:
            pass


@pytest.mark.skipif(cleanup._IS_WINDOWS, reason="POSIX descriptor contract")
def test_recursive_cleanup_rejects_hardlink_and_preserves_external_victim(
    tmp_path: Path,
) -> None:
    """A tree entry linked to an external inode is never unlinked."""

    owner = tmp_path / "owned"
    target = owner / "tree"
    target.mkdir(parents=True)
    victim = tmp_path / "external-victim.bin"
    victim.write_bytes(b"preserve")
    os.link(victim, target / "linked.bin")

    with pytest.raises(cleanup.PackagingCleanupError, match="hard-linked"):
        cleanup.remove_owned_path(
            target,
            owner_root=owner,
            operation="test hardlink cleanup",
        )

    assert victim.read_bytes() == b"preserve"
    quarantine = next(owner.glob(".tobkiri-cleanup-*"))
    assert (quarantine / "linked.bin").exists()


@pytest.mark.skipif(cleanup._IS_WINDOWS, reason="POSIX descriptor contract")
def test_recursive_cleanup_rejects_nested_symlink_without_touching_victim(
    tmp_path: Path,
) -> None:
    """A nested symlink is residue, never a traversal or unlink target."""

    owner = tmp_path / "owned"
    target = owner / "tree"
    target.mkdir(parents=True)
    victim = tmp_path / "external-victim.bin"
    victim.write_bytes(b"preserve")
    (target / "linked.bin").symlink_to(victim)

    with pytest.raises(cleanup.PackagingCleanupError, match="symlink"):
        cleanup.remove_owned_path(
            target,
            owner_root=owner,
            operation="test nested symlink cleanup",
        )

    assert victim.read_bytes() == b"preserve"
    quarantine = next(owner.glob(".tobkiri-cleanup-*"))
    assert (quarantine / "linked.bin").is_symlink()


@pytest.mark.skipif(cleanup._IS_WINDOWS, reason="POSIX descriptor contract")
def test_missing_descriptor_support_has_no_pathname_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unsupported dirfd platforms fail closed before touching the target."""

    owner = tmp_path / "owned"
    target = owner / "tree"
    target.mkdir(parents=True)
    (target / "payload.bin").write_bytes(b"preserve")
    monkeypatch.setattr(cleanup, "_open_parent_directories", lambda *args, **kwargs: ())

    with pytest.raises(
        cleanup.PackagingCleanupError, match="pathname fallback is forbidden"
    ):
        cleanup.remove_owned_path(
            target,
            owner_root=owner,
            operation="test unavailable descriptor cleanup",
        )

    assert (target / "payload.bin").read_bytes() == b"preserve"


@pytest.mark.skipif(cleanup._IS_WINDOWS, reason="POSIX descriptor contract")
def test_file_replacement_at_unlink_boundary_fails_without_deleting_victim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A final-component rename swap is detected after the file is opened."""

    owner = tmp_path / "owned"
    target = owner / "tree"
    target.mkdir(parents=True)
    payload = target / "payload.bin"
    payload.write_bytes(b"owned")
    original = target / "payload-original.bin"
    victim = tmp_path / "external-victim.bin"
    victim.write_bytes(b"external")
    swapped = False

    def replace_before_unlink(path: Path) -> None:
        nonlocal swapped
        if path == payload and not swapped:
            swapped = True
            quarantine = next(owner.glob(".tobkiri-cleanup-*"))
            actual_payload = quarantine / payload.name
            actual_payload.rename(quarantine / original.name)
            victim.rename(actual_payload)

    monkeypatch.setattr(cleanup, "_BEFORE_POSIX_MUTATION", replace_before_unlink)
    with pytest.raises(cleanup.PackagingCleanupError, match="identity changed"):
        cleanup.remove_owned_path(
            target,
            owner_root=owner,
            operation="test file replacement cleanup",
        )

    assert swapped
    quarantine = next(owner.glob(".tobkiri-cleanup-*"))
    assert (quarantine / payload.name).read_bytes() == b"external"
    assert (quarantine / original.name).read_bytes() == b"owned"


@pytest.mark.skipif(cleanup._IS_WINDOWS, reason="POSIX descriptor contract")
def test_target_swap_before_quarantine_never_deletes_external_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The atomic quarantine step validates the exact object it will delete."""

    owner = tmp_path / "owned"
    target = owner / "tree"
    target.mkdir(parents=True)
    (target / "owned.bin").write_bytes(b"owned")
    original = owner / "tree-original"
    external = tmp_path / "external"
    external.mkdir()
    (external / "victim.bin").write_bytes(b"preserve")
    swapped = False

    def replace_target(path: Path) -> None:
        nonlocal swapped
        if path == target and not swapped:
            swapped = True
            target.rename(original)
            external.rename(target)

    monkeypatch.setattr(cleanup, "_BEFORE_POSIX_MUTATION", replace_target)
    with pytest.raises(cleanup.PackagingCleanupError, match="identity changed"):
        cleanup.remove_owned_path(
            target,
            owner_root=owner,
            operation="test target swap before quarantine",
        )

    assert swapped
    assert (target / "victim.bin").read_bytes() == b"preserve"
    assert (original / "owned.bin").read_bytes() == b"owned"


@pytest.mark.skipif(cleanup._IS_WINDOWS, reason="POSIX descriptor contract")
def test_ancestor_swap_during_fd_walk_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Held dirfds never redirect deletion through a substituted ancestor."""

    owner = tmp_path / "owned"
    ancestor = owner / "ancestor"
    target = ancestor / "tree"
    target.mkdir(parents=True)
    payload = target / "payload.bin"
    payload.write_bytes(b"owned")
    held_ancestor = owner / "ancestor-held"
    external = tmp_path / "external"
    external.mkdir()
    victim = external / "victim.bin"
    victim.write_bytes(b"preserve")
    swapped = False

    def replace_ancestor(path: Path) -> None:
        nonlocal swapped
        if path == payload and not swapped:
            swapped = True
            ancestor.rename(held_ancestor)
            ancestor.symlink_to(external, target_is_directory=True)

    monkeypatch.setattr(cleanup, "_BEFORE_POSIX_MUTATION", replace_ancestor)
    with pytest.raises(cleanup.PackagingCleanupError, match="symlink or reparse"):
        cleanup.remove_owned_path(
            target,
            owner_root=owner,
            operation="test ancestor swap during descriptor cleanup",
        )

    assert swapped
    assert victim.read_bytes() == b"preserve"
    quarantine = next(held_ancestor.glob(".tobkiri-cleanup-*"))
    assert (quarantine / "payload.bin").read_bytes() == b"owned"


@pytest.mark.skipif(cleanup._IS_WINDOWS, reason="POSIX descriptor contract")
def test_nested_device_substitution_during_fd_walk_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The descriptor walker refuses an entry whose device leaves the owner."""

    owner = tmp_path / "owned"
    target = owner / "tree"
    nested = target / "mounted"
    nested.mkdir(parents=True)
    original_lstat_at = cleanup._lstat_at

    def changed_device(parent_fd: int, name: str) -> Any:
        result = original_lstat_at(parent_fd, name)
        if name != "mounted":
            return result
        return SimpleNamespace(
            st_dev=result.st_dev + 1,
            st_ino=result.st_ino,
            st_mode=result.st_mode,
            st_nlink=result.st_nlink,
        )

    monkeypatch.setattr(cleanup, "_lstat_at", changed_device)
    with pytest.raises(cleanup.PackagingCleanupError, match="mount/device"):
        cleanup.remove_owned_path(
            target,
            owner_root=owner,
            operation="test nested device substitution",
        )

    quarantine = next(owner.glob(".tobkiri-cleanup-*"))
    assert (quarantine / nested.name).exists()


@pytest.mark.skipif(cleanup._IS_WINDOWS, reason="POSIX descriptor contract")
def test_same_device_different_mount_identity_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-device mount substitution is rejected by descriptor mount ID."""

    owner = tmp_path / "owned"
    target = owner / "tree"
    target.mkdir(parents=True)
    (target / "payload.bin").write_bytes(b"owned")
    original_mount_identity = cleanup._posix_mount_identity
    calls = 0

    def changed_mount_identity(fd: int) -> tuple[str, int]:
        nonlocal calls
        calls += 1
        identity = original_mount_identity(fd)
        if calls == 2:
            return (identity[0], identity[1] + 1)
        return identity

    monkeypatch.setattr(cleanup, "_posix_mount_identity", changed_mount_identity)
    with pytest.raises(cleanup.PackagingCleanupError, match="mount boundary"):
        cleanup.remove_owned_path(
            target,
            owner_root=owner,
            operation="test same-device mount identity substitution",
        )

    assert calls >= 2
    quarantine = next(owner.glob(".tobkiri-cleanup-*"))
    assert (quarantine / "payload.bin").read_bytes() == b"owned"


@pytest.mark.skipif(cleanup._IS_WINDOWS, reason="POSIX descriptor contract")
def test_mount_identity_unavailable_fails_closed_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing platform mount identity never permits pathname cleanup."""

    owner = tmp_path / "owned"
    target = owner / "tree"
    target.mkdir(parents=True)
    (target / "payload.bin").write_bytes(b"preserve")

    def unavailable(_fd: int) -> tuple[str, int]:
        raise OSError(errno.ENOTSUP, "mount identity unavailable")

    monkeypatch.setattr(cleanup, "_posix_mount_identity", unavailable)
    with pytest.raises(
        cleanup.PackagingCleanupError, match="owned root mount identity"
    ):
        cleanup.remove_owned_path(
            target,
            owner_root=owner,
            operation="test unavailable mount identity",
        )

    assert (target / "payload.bin").read_bytes() == b"preserve"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux fdinfo contract")
def test_linux_mount_identity_reads_held_descriptor_mount_id(
    tmp_path: Path,
) -> None:
    """Linux mount identity comes from fdinfo for the held descriptor."""

    descriptor = os.open(tmp_path, os.O_RDONLY)
    try:
        kind, mount_id = cleanup._posix_mount_identity(descriptor)
    finally:
        os.close(descriptor)

    assert kind == "linux-mnt-id"
    assert mount_id > 0


@pytest.mark.parametrize(
    "lines",
    [
        [],
        ["mnt_id: 7\n", "mnt_id: 8\n"],
        ["mnt_id: not-a-number\n"],
        ["mnt_id: 0\n"],
    ],
)
def test_linux_mount_id_parser_rejects_missing_duplicate_or_malformed(
    lines: list[str],
) -> None:
    """fdinfo parsing is strict and never invents a mount identity."""

    with pytest.raises(ValueError):
        cleanup._parse_linux_mount_id(lines)


def test_linux_fdinfo_rejects_deterministic_original_fd_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reused original FD is detected by before/after identity checks."""

    class _FdInfo:
        def __enter__(self) -> "_FdInfo":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self):
            return iter(["mnt_id: 42\n"])

    identities = [
        _fake_stat(stat.S_IFDIR, inode=1),
        _fake_stat(stat.S_IFDIR, inode=1),
        _fake_stat(stat.S_IFDIR, inode=1),
        _fake_stat(stat.S_IFDIR, inode=2),
    ]
    close_calls: list[int] = []
    inheritable_calls: list[tuple[int, bool]] = []

    monkeypatch.setattr(cleanup.os, "dup", lambda _fd: 11)
    monkeypatch.setattr(
        cleanup.os,
        "set_inheritable",
        lambda fd, inheritable: inheritable_calls.append((fd, inheritable)),
    )
    monkeypatch.setattr(cleanup.os, "fstat", lambda _fd: identities.pop(0))
    monkeypatch.setattr(cleanup.os, "close", lambda fd: close_calls.append(fd))
    monkeypatch.setattr(
        cleanup,
        "open",
        lambda *_args, **_kwargs: _FdInfo(),
        raising=False,
    )

    with pytest.raises(OSError, match="original POSIX FD identity changed"):
        cleanup._linux_fdinfo_mount_identity(10)

    assert inheritable_calls == [(11, False)]
    assert close_calls == [11]


def test_linux_fdinfo_duplicate_close_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate FD close failure is surfaced after identity validation."""

    class _FdInfo:
        def __enter__(self) -> "_FdInfo":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self):
            return iter(["mnt_id: 42\n"])

    identity = _fake_stat(stat.S_IFDIR, inode=1)
    close_calls: list[int] = []
    monkeypatch.setattr(cleanup.os, "dup", lambda _fd: 11)
    monkeypatch.setattr(cleanup.os, "set_inheritable", lambda *_args: None)
    monkeypatch.setattr(cleanup.os, "fstat", lambda _fd: identity)

    def fail_close(fd: int) -> None:
        close_calls.append(fd)
        raise OSError(errno.EIO, "duplicate close failed")

    monkeypatch.setattr(cleanup.os, "close", fail_close)
    monkeypatch.setattr(
        cleanup,
        "open",
        lambda *_args, **_kwargs: _FdInfo(),
        raising=False,
    )

    with pytest.raises(OSError, match="duplicate close failed"):
        cleanup._linux_fdinfo_mount_identity(10)

    assert close_calls == [11]


def test_final_symlink_is_rejected_even_when_contained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A final symlink is not treated as an owned file to unlink."""

    scope = tmp_path / "scope"
    scope.mkdir()
    target = scope / "owned"
    target.write_bytes(b"sentinel")
    _patch_lstat_component(monkeypatch, target, _fake_stat(stat.S_IFLNK))

    with pytest.raises(cleanup.PackagingCleanupError, match="symlink or reparse"):
        cleanup.remove_owned_path(
            target,
            owner_root=scope,
            operation="test final symlink cleanup",
        )

    assert target.exists()


def test_nested_reparse_or_junction_abstraction_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows junction/reparse metadata is rejected without Windows APIs."""

    scope = tmp_path / "scope"
    nested = scope / "nested"
    target = nested / "owned"
    target.mkdir(parents=True)
    _patch_lstat_component(
        monkeypatch,
        nested,
        _fake_stat(
            stat.S_IFDIR,
            file_attributes=cleanup._FILE_ATTRIBUTE_REPARSE_POINT,
        ),
    )

    with pytest.raises(cleanup.PackagingCleanupError, match="symlink or reparse"):
        cleanup.remove_owned_path(
            target,
            owner_root=scope,
            operation="test nested reparse cleanup",
        )

    assert target.exists()


def test_mount_device_substitution_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A component on another device cannot be treated as owned output."""

    scope = tmp_path / "scope"
    nested = scope / "nested"
    target = nested / "owned"
    target.mkdir(parents=True)
    root_device = cleanup._lstat_no_follow(scope).st_dev
    _patch_lstat_component(
        monkeypatch,
        nested,
        _fake_stat(stat.S_IFDIR, device=root_device + 1),
    )

    with pytest.raises(
        cleanup.PackagingCleanupError, match="mount/device substitution"
    ):
        cleanup.remove_owned_path(
            target,
            owner_root=scope,
            operation="test mount substitution cleanup",
        )

    assert target.exists()


def test_ancestor_replacement_between_attempts_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient first failure cannot permit a replaced ancestor on retry."""

    scope = tmp_path / "scope"
    nested = scope / "nested"
    target = nested / "owned"
    target.mkdir(parents=True)
    external = tmp_path / "external-victim"
    external.mkdir()
    victim = external / "sentinel.txt"
    victim.write_text("preserve", encoding="utf-8")
    fake_api = _FakeWindowsApi()
    _use_fake_windows_native_api(monkeypatch, fake_api)
    calls = 0
    original_remove = cleanup._remove_once

    def replace_ancestor_then_lock(path: Path, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            original_nested = scope / "nested-original"
            nested.rename(original_nested)
            nested.mkdir()
            external.rename(target)
            raise _windows_error(32)
        original_remove(path, **kwargs)

    monkeypatch.setattr(cleanup, "_remove_once", replace_ancestor_then_lock)

    with pytest.raises(cleanup.PackagingCleanupError, match="identity changed"):
        cleanup.remove_owned_path(
            target,
            owner_root=scope,
            operation="test ancestor replacement cleanup",
            sleep=lambda _delay: None,
        )

    assert calls == 1
    assert (scope / "nested-original" / "owned").exists()
    assert nested.exists()
    assert (target / "sentinel.txt").read_text(encoding="utf-8") == "preserve"
    assert fake_api.rename_calls == []


def test_windows_retry_rejects_ancestor_that_becomes_reparse_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A junction/reparse substitution between retries is never traversed."""

    scope = tmp_path / "scope"
    nested = scope / "nested"
    target = nested / "owned.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"owned")
    victim = tmp_path / "external-victim.txt"
    victim.write_text("preserve", encoding="utf-8")
    fake_api = _FakeWindowsApi()
    _use_fake_windows_native_api(monkeypatch, fake_api)
    original_path_identity = fake_api.path_identity
    reparse = False

    def path_identity(path: Path, *, directory: bool) -> Any:
        identity = original_path_identity(path, directory=directory)
        if reparse and path == nested:
            return cleanup._WindowsFileIdentity(
                volume_serial=identity.volume_serial,
                file_index=identity.file_index,
                file_attributes=(
                    identity.file_attributes | cleanup._FILE_ATTRIBUTE_REPARSE_POINT
                ),
            )
        return identity

    def lock_then_reparse(_path: Path, **_kwargs: object) -> None:
        nonlocal reparse
        reparse = True
        raise _windows_error(32)

    fake_api.path_identity = path_identity  # type: ignore[assignment]
    monkeypatch.setattr(cleanup, "_remove_once", lock_then_reparse)

    with pytest.raises(cleanup.PackagingCleanupError, match="identity changed"):
        cleanup.remove_owned_path(
            target,
            owner_root=scope,
            operation="test retry reparse substitution",
            sleep=lambda _delay: None,
        )

    assert target.read_bytes() == b"owned"
    assert victim.read_text(encoding="utf-8") == "preserve"
    assert fake_api.rename_calls == []


def test_persistent_windows_lock_fails_closed_after_bounded_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistent sharing violation remains visible and is never masked."""

    target = _make_owned_directory(tmp_path)

    def remove_with_persistent_lock(path: Path, **kwargs: object) -> None:
        raise _windows_error(5)

    monkeypatch.setattr(cleanup, "_IS_WINDOWS", True)
    monkeypatch.setattr(cleanup, "_remove_once", remove_with_persistent_lock)
    sleeps: list[float] = []

    with pytest.raises(cleanup.PackagingCleanupError) as raised:
        cleanup.remove_owned_path(
            target,
            owner_root=tmp_path,
            operation="test persistent cleanup",
            sleep=sleeps.append,
        )

    diagnostic = raised.value.diagnostic
    assert diagnostic.attempts == 3
    assert diagnostic.transient is True
    assert diagnostic.exhausted is True
    assert diagnostic.winerror == 5
    assert len(sleeps) == 2
    assert target.exists()


def test_non_lock_error_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrelated I/O error fails immediately, even on Windows."""

    target = _make_owned_directory(tmp_path)

    def remove_with_non_lock_error(path: Path, **kwargs: object) -> None:
        raise OSError(errno.EIO, "simulated media error")

    monkeypatch.setattr(cleanup, "_IS_WINDOWS", True)
    monkeypatch.setattr(cleanup, "_remove_once", remove_with_non_lock_error)
    sleeps: list[float] = []

    with pytest.raises(cleanup.PackagingCleanupError) as raised:
        cleanup.remove_owned_path(
            target,
            owner_root=tmp_path,
            operation="test non-lock cleanup",
            sleep=sleeps.append,
        )

    assert raised.value.diagnostic.attempts == 1
    assert raised.value.diagnostic.transient is False
    assert sleeps == []
    assert target.exists()


class _LiveChild:
    """Minimal process double that remains alive at cleanup time."""

    stdin = None
    stdout = None
    stderr = None

    def poll(self) -> None:
        return None

    def wait(self, *, timeout: float) -> None:
        raise subprocess.TimeoutExpired(["locked-child"], timeout)


class _ClosableStream:
    """Platform-neutral stream double for process-handle cleanup tests."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _ExitedChild:
    """Minimal exited process double with closable streams."""

    def __init__(self) -> None:
        self.stdin = _ClosableStream()
        self.stdout = _ClosableStream()
        self.stderr = _ClosableStream()

    def poll(self) -> int:
        return 0

    def wait(self, *, timeout: float | None = None) -> int:
        return 0


def test_run_process_waits_and_closes_process_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The packaging subprocess wrapper closes handles before returning."""

    child = _ExitedChild()
    exited = False

    class _ProcessContext:
        def __enter__(self) -> _ExitedChild:
            return child

        def __exit__(self, *args: object) -> None:
            nonlocal exited
            exited = True

    monkeypatch.setattr(
        cleanup.subprocess,
        "Popen",
        lambda command, cwd: _ProcessContext(),
    )

    cleanup.run_process_and_wait(["packager-child"], cwd=tmp_path)

    assert exited
    assert child.stdin.closed
    assert child.stdout.closed
    assert child.stderr.closed


def test_live_child_refuses_cleanup_before_deletion(tmp_path: Path) -> None:
    """Cleanup refuses to touch a path while its child process is alive."""

    target = _make_owned_directory(tmp_path)

    with pytest.raises(cleanup.PackagingCleanupError) as raised:
        cleanup.remove_owned_path(
            target,
            owner_root=tmp_path,
            operation="test live child cleanup",
            child=_LiveChild(),
        )

    diagnostic = raised.value.diagnostic
    assert diagnostic.child_alive is True
    assert diagnostic.attempts == 0
    assert target.exists()


def test_exited_child_streams_close_before_cleanup(tmp_path: Path) -> None:
    """Exited child streams are closed before the owned path is removed."""

    target = _make_owned_directory(tmp_path)
    child = _ExitedChild()

    cleanup.remove_owned_path(
        target,
        owner_root=tmp_path,
        operation="test exited child cleanup",
        child=child,
    )

    assert child.stdin.closed
    assert child.stdout.closed
    assert child.stderr.closed
    assert not target.exists()


def test_cleanup_is_idempotent_across_restart(tmp_path: Path) -> None:
    """Repeated cleanup and a later staging restart remain scoped and safe."""

    target = _make_owned_directory(tmp_path)
    cleanup.remove_owned_path(
        target,
        owner_root=tmp_path,
        operation="test first cleanup",
    )
    cleanup.remove_owned_path(
        target,
        owner_root=tmp_path,
        operation="test idempotent cleanup",
    )

    target.mkdir()
    (target / "restart.marker").write_text("restart", encoding="utf-8")
    cleanup.remove_owned_path(
        target,
        owner_root=tmp_path,
        operation="test restart cleanup",
    )

    assert not target.exists()


def test_cleanup_rejects_scope_root_and_outside_paths(tmp_path: Path) -> None:
    """Scope validation prevents root or traversal deletion."""

    owner_root = tmp_path / "owned"
    owner_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(cleanup.PackagingCleanupError):
        cleanup.remove_owned_path(
            owner_root,
            owner_root=owner_root,
            operation="test scope root cleanup",
        )
    with pytest.raises(cleanup.PackagingCleanupError):
        cleanup.remove_owned_path(
            tmp_path / "owned" / ".." / "outside",
            owner_root=owner_root,
            operation="test outside cleanup",
        )

    assert owner_root.exists()
    assert outside.exists()
