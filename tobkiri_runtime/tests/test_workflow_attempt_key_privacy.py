"""Private attempt keys never rely on Windows synthetic POSIX mode bits."""

from __future__ import annotations

from contextlib import contextmanager
import os
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

import core_runtime.hmac_key_manager as hmac_keys
import core_runtime.private_file as privacy
from core_runtime.workflow_v4.attempt_store import WorkflowAttemptStoreV4
from core_runtime.workflow_v4.models import WorkflowDenied
import tobkiri_protocol.secure_persistence as persistence
from tobkiri_protocol.secure_persistence import SecureDirectory, SecurePersistenceError


def test_attempt_key_reload_preserves_private_journal(tmp_path):
    path = tmp_path / "attempts.bin"
    first = WorkflowAttemptStoreV4(path)
    first.create_host_pending_effect("pending", {"state": "completed", "private": "retained"})
    key = path.with_suffix(".bin.key").read_bytes()
    second = WorkflowAttemptStoreV4(path)
    assert second.get_host_pending_effect("pending") == (1, {"state": "completed", "private": "retained"})
    assert path.with_suffix(".bin.key").read_bytes() == key


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner-only mode contract")
def test_existing_broad_key_fails_before_read_without_silent_repair(tmp_path, monkeypatch):
    path = tmp_path / "attempts.bin"
    WorkflowAttemptStoreV4(path)
    key_path = path.with_suffix(".bin.key")
    original = key_path.read_bytes()
    key_path.chmod(0o644)
    key_inode = key_path.stat().st_ino
    real_read = os.read

    def read(descriptor, size):
        assert os.fstat(descriptor).st_ino != key_inode, "unsafe key was read"
        return real_read(descriptor, size)

    monkeypatch.setattr(persistence.os, "read", read)
    with pytest.raises(WorkflowDenied, match="private"):
        WorkflowAttemptStoreV4(path)
    assert key_path.stat().st_mode & 0o777 == 0o644
    assert key_path.read_bytes() == original


def test_missing_key_for_existing_journal_does_not_rotate(tmp_path):
    path = tmp_path / "attempts.bin"
    WorkflowAttemptStoreV4(path)
    ciphertext = path.read_bytes()
    key_path = path.with_suffix(".bin.key")
    key_path.unlink()
    with pytest.raises(WorkflowDenied, match="missing"):
        WorkflowAttemptStoreV4(path)
    assert not key_path.exists()
    assert path.read_bytes() == ciphertext


def test_invalid_private_key_is_not_replaced(tmp_path):
    path = tmp_path / "attempts.bin"
    WorkflowAttemptStoreV4(path)
    key_path = path.with_suffix(".bin.key")
    key_path.write_bytes(b"invalid")
    with pytest.raises(WorkflowDenied, match="valid"):
        WorkflowAttemptStoreV4(path)
    assert key_path.read_bytes() == b"invalid"


def test_windows_verifier_uses_acl_despite_synthetic_broad_mode(tmp_path, monkeypatch):
    path = tmp_path / "key"
    calls = []
    monkeypatch.setattr(privacy, "os", SimpleNamespace(
        name="nt", fstat=lambda fd: SimpleNamespace(st_mode=0o100666, st_nlink=1)
    ))
    monkeypatch.setattr(privacy, "_verify_windows_signing_key_acl", calls.append)
    privacy.verify_private_file(path, 77)
    assert calls == [path]


@pytest.mark.parametrize("message", ["ACL unreadable", "ACL has extra principals"])
def test_windows_acl_failure_is_never_repaired(tmp_path, monkeypatch, message):
    path = tmp_path / "key"
    monkeypatch.setattr(privacy, "os", SimpleNamespace(
        name="nt", fstat=lambda fd: SimpleNamespace(st_mode=0o100666, st_nlink=1)
    ))
    def rejected(target):
        raise hmac_keys.SigningKeyError(message)
    monkeypatch.setattr(privacy, "_verify_windows_signing_key_acl", rejected)
    monkeypatch.setattr(hmac_keys, "_secure_windows_signing_key", lambda _: pytest.fail("repaired"))
    with pytest.raises(hmac_keys.SigningKeyError, match=message):
        privacy.verify_private_file(path, 77)


def test_acl_verifier_enumerates_inherited_rules_too():
    script = hmac_keys._WINDOWS_SIGNING_KEY_ACL_VALIDATION
    assert "$true, $true, [System.Security.Principal.SecurityIdentifier]" in script
    assert "$rules.Count -ne 1" in script
    assert "$rule.IsInherited" in script
    assert "$ownerSid -ne $sid.Value" in script
    assert "$rule.IdentityReference.Value -ne $sid.Value" in script
    assert "$rule.FileSystemRights -ne $fullControl" in script
    assert "$rule.AccessControlType -ne $allow" in script
    assert "-not $verified.AreAccessRulesProtected" in script


def test_private_creation_callback_precedes_secret_write_and_failure_cleans(tmp_path):
    store = SecureDirectory(tmp_path / "root")
    def rejected(path, descriptor):
        assert os.fstat(descriptor).st_size == 0
        raise hmac_keys.SigningKeyError("ACL unavailable")
    with pytest.raises(hmac_keys.SigningKeyError, match="ACL unavailable"):
        store.write_bytes_atomic(
            "key", b"secret", prepare_new_file=rejected, create_only=True
        )
    assert list(store.root.iterdir()) == []


def test_private_read_verifies_same_descriptor_before_and_after(tmp_path, monkeypatch):
    store = SecureDirectory(tmp_path / "root")
    store.write_bytes_atomic("key", b"secret")
    events = []
    real_read = os.read
    def verify(path, descriptor):
        assert path == store.root / "key"
        events.append(("verify", descriptor))
    def read(descriptor, size):
        events.append(("read", descriptor))
        return real_read(descriptor, size)
    monkeypatch.setattr(persistence.os, "read", read)
    assert store.read_bytes_bounded("key", max_bytes=6, verify_open_file=verify) == b"secret"
    assert events[0][0] == events[-1][0] == "verify"
    assert len({descriptor for _, descriptor in events}) == 1


def test_second_privacy_verification_failure_does_not_return_secret(tmp_path):
    store = SecureDirectory(tmp_path / "root")
    store.write_bytes_atomic("key", b"secret")
    calls = 0
    def verify(path, descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise hmac_keys.SigningKeyError("ACL changed")
    with pytest.raises(hmac_keys.SigningKeyError, match="ACL changed"):
        store.read_bytes_bounded("key", max_bytes=6, verify_open_file=verify)


def test_create_only_never_overwrites_existing_entry(tmp_path):
    store = SecureDirectory(tmp_path / "root")
    store.write_bytes_atomic("key", b"existing")
    with pytest.raises(FileExistsError):
        store.write_bytes_atomic("key", b"replacement", create_only=True)
    assert store.read_bytes("key") == b"existing"


def test_create_only_publication_rejects_late_concurrent_winner(tmp_path):
    store = SecureDirectory(tmp_path / "root")
    def race():
        (store.root / "key").write_bytes(b"winner")
    with pytest.raises(FileExistsError):
        store.write_bytes_atomic("key", b"loser", create_only=True, before_publish=race)
    assert store.read_bytes("key") == b"winner"
    assert sorted(p.name for p in store.root.iterdir()) == ["key"]


def test_windows_private_temporary_uses_atomic_private_creation(tmp_path, monkeypatch):
    store = SecureDirectory(tmp_path / "root")
    pinned = False
    @contextmanager
    def parent(relative, *, create):
        nonlocal pinned
        pinned = True
        try:
            yield store.root, relative
        finally:
            pinned = False
    monkeypatch.setattr(store, "_windows_parent", parent)
    monkeypatch.setattr(store, "_windows_stat_entry", lambda *args, **kwargs: None)
    opened = []
    def open_file(path, flags, *, disposition, private):
        assert disposition == persistence._CREATE_NEW
        assert private
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        opened.append(descriptor)
        return descriptor, (1, 2)
    monkeypatch.setattr(persistence, "_windows_open_file_descriptor", open_file)
    def prepare(path, descriptor):
        assert pinned
        assert descriptor == opened[0]
        assert os.fstat(descriptor).st_size == 0
        raise hmac_keys.SigningKeyError("mock hardening failure")
    with pytest.raises(hmac_keys.SigningKeyError, match="mock hardening"):
        store._write_bytes_windows("key", b"secret", prepare_new_file=prepare)
    assert list(store.root.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL and share-mode proof")
def test_windows_private_file_is_owner_only_before_any_secret_write(tmp_path):
    store = SecureDirectory(tmp_path / "root")
    observed = []
    def prepare(path, descriptor):
        assert os.fstat(descriptor).st_size == 0
        with pytest.raises(OSError):
            path.read_bytes()
        with pytest.raises(OSError):
            path.rename(path.with_name("displaced"))
        with pytest.raises(OSError):
            store.root.rename(store.root.with_name("displaced-root"))
        privacy.prepare_private_file(path, descriptor)
        observed.append(path)
    store.write_bytes_atomic(
        "key", b"secret", prepare_new_file=prepare, create_only=True
    )
    assert observed
    assert store.read_bytes_bounded(
        "key", max_bytes=6, verify_open_file=privacy.verify_private_file
    ) == b"secret"


@pytest.mark.skipif(os.name != "nt", reason="native Windows inherited ACL rejection")
def test_windows_existing_inherited_key_is_rejected_unchanged(tmp_path):
    path = tmp_path / "attempts.bin"
    key_path = path.with_suffix(".bin.key")
    key_path.write_bytes(Fernet.generate_key())
    before = key_path.read_bytes()
    with pytest.raises(WorkflowDenied, match="private"):
        WorkflowAttemptStoreV4(path)
    assert key_path.read_bytes() == before
    assert not path.exists()
    with pytest.raises(hmac_keys.SigningKeyError):
        hmac_keys._verify_windows_signing_key_acl(key_path)


@pytest.mark.parametrize("changed_id", [False, True])
def test_windows_read_retains_parent_and_file_during_both_acl_checks(
    tmp_path, monkeypatch, changed_id
):
    store = SecureDirectory(tmp_path / "root")
    store.write_bytes_atomic("key", b"secret")
    pinned = False
    events = []
    @contextmanager
    def parent(relative, *, create):
        nonlocal pinned
        pinned = True
        try:
            yield store.root, relative
        finally:
            pinned = False
    monkeypatch.setattr(store, "_windows_parent", parent)
    def open_file(path, flags):
        return os.open(path, flags), (1, 2)
    monkeypatch.setattr(persistence, "_windows_open_file_descriptor", open_file)
    monkeypatch.setattr(persistence, "_windows_descriptor_file_id", lambda fd: (1, 2))
    def stat_entry(parent, name, *, required):
        return persistence._fingerprint((parent / name).stat()), (1, 3 if changed_id else 2)
    monkeypatch.setattr(store, "_windows_stat_entry", stat_entry)
    def verify(path, descriptor):
        assert pinned
        assert path == store.root / "key"
        assert os.fstat(descriptor).st_size == 6
        events.append(descriptor)
    if changed_id:
        with pytest.raises(SecurePersistenceError, match="changed during read"):
            store._read_bytes_windows("key", max_bytes=6, verify_open_file=verify)
        assert len(events) == 1
    else:
        assert store._read_bytes_windows(
            "key", max_bytes=6, verify_open_file=verify
        ) == b"secret"
        assert len(events) == 2 and events[0] == events[1]
    assert not pinned


@pytest.mark.parametrize("private, expected_share", [(False, 3), (True, 0)])
def test_native_open_adapter_enforces_requested_data_sharing(
    tmp_path, monkeypatch, private, expected_share
):
    import sys
    import ctypes
    from tobkiri_protocol.windows_private_file import SecurityAttributes
    attributes = SecurityAttributes()
    @contextmanager
    def security_attributes():
        yield attributes
    monkeypatch.setattr(persistence, "owner_only_security_attributes", security_attributes)
    shares = []
    def create_file(path, access, share, security, disposition, flags, template):
        shares.append(share)
        if private:
            assert ctypes.addressof(security._obj) == ctypes.addressof(attributes)
        else:
            assert security is None
        assert flags & persistence._FILE_FLAG_OPEN_REPARSE_POINT
        return 123
    def get_information(handle, pointer):
        pointer._obj.number_of_links = 1
        pointer._obj.volume_serial_number = 4
        pointer._obj.file_index_low = 5
        return 1
    native = SimpleNamespace(
        CreateFileW=create_file,
        GetFileInformationByHandle=get_information,
        CloseHandle=lambda handle: pytest.fail("unexpected CloseHandle"),
    )
    monkeypatch.setattr(persistence, "_windows_kernel32", lambda: native)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(
        open_osfhandle=lambda handle, flags: 77
    ))
    descriptor, identity = persistence._windows_open_file_descriptor(
        tmp_path / "key", os.O_WRONLY,
        disposition=persistence._CREATE_NEW, private=private,
    )
    assert descriptor == 77
    assert identity == (4, 5)
    assert shares == [expected_share]


@pytest.mark.skipif(os.name != "nt", reason="native Windows extra-principal ACL rejection")
def test_windows_broadened_existing_key_fails_without_repair(tmp_path):
    import base64
    import subprocess
    path = tmp_path / "attempts.bin"
    WorkflowAttemptStoreV4(path)
    key_path = path.with_suffix(".bin.key")
    key_bytes = key_path.read_bytes()
    environment = os.environ.copy()
    environment["TOBKIRI_TEST_ACL_PATH"] = base64.b64encode(
        str(key_path).encode("utf-8")
    ).decode("ascii")
    script = r'''
$ErrorActionPreference = 'Stop'
$target = [Text.Encoding]::UTF8.GetString(
  [Convert]::FromBase64String($env:TOBKIRI_TEST_ACL_PATH))
$file = [IO.FileInfo]::new($target)
$acl = $file.GetAccessControl()
$sid = [Security.Principal.SecurityIdentifier]::new('S-1-1-0')
$rule = [Security.AccessControl.FileSystemAccessRule]::new(
  $sid, [Security.AccessControl.FileSystemRights]::Read,
  [Security.AccessControl.AccessControlType]::Allow)
[void]$acl.AddAccessRule($rule)
$file.SetAccessControl($acl)
'''
    subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True, capture_output=True, env=environment, timeout=60,
    )
    with pytest.raises(WorkflowDenied, match="private"):
        WorkflowAttemptStoreV4(path)
    assert key_path.read_bytes() == key_bytes
    with pytest.raises(hmac_keys.SigningKeyError):
        hmac_keys._verify_windows_signing_key_acl(key_path)
