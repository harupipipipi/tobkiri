"""Cross-platform tests for Windows Git executable trust capture."""

from __future__ import annotations

import ctypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from core_runtime import executable_trust
from core_runtime.executable_trust import (
    ExecutableTrustError,
    WindowsPathSecurity,
)
from ecosystem.rumi_git_publish_pack.runtime import publish


class _AclSizeInformation(ctypes.Structure):
    """Cross-platform mirror of ACL_SIZE_INFORMATION for ctypes seam tests."""

    _fields_ = [
        ("AceCount", executable_trust.wintypes.DWORD),
        ("AclBytesInUse", executable_trust.wintypes.DWORD),
        ("AclBytesFree", executable_trust.wintypes.DWORD),
    ]


class _TrusteeW(ctypes.Structure):
    """Minimal TRUSTEE_W layout used only as a type argument in seam tests."""

    _fields_ = [("unused", ctypes.c_void_p)]


class _FakeAclApi:
    """Expose one ACE buffer through the tiny ACL API surface under test."""

    def __init__(self, ace: ctypes.Array[ctypes.c_char], *, sid_size: int) -> None:
        self.ace = ace
        self.sid_size = sid_size

    def GetAclInformation(
        self,
        _dacl: object,
        information: object,
        _size: int,
        _information_class: int,
    ) -> bool:
        target = ctypes.cast(
            information,
            ctypes.POINTER(_AclSizeInformation),
        ).contents
        target.AceCount = 1
        target.AclBytesInUse = len(self.ace)
        return True

    def GetAce(self, _dacl: object, _index: int, output: object) -> bool:
        target = ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))
        target.contents.value = ctypes.addressof(self.ace)
        return True

    def IsValidSid(self, _sid: object) -> bool:
        return True

    def GetLengthSid(self, _sid: object) -> int:
        return self.sid_size


def _trusted_windows_security(_path: Path) -> WindowsPathSecurity:
    """Return a locked-down Program Files-like ACL record for test injection."""

    return WindowsPathSecurity(
        owner_sid="S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
        caller_sid="S-1-5-21-1000-1000-1000-1001",
        caller_rights=0x001200A9,  # read + execute, no mutation authority
        descriptor_sha256="a" * 64,
        principal_rights=(
            ("world", 0),
            ("authenticated_users", 0),
            ("builtin_users", 0),
        ),
    )


def _windows_executable(tmp_path: Path) -> Path:
    """Create an executable-looking file whose POSIX mode is deliberately open."""

    executable = tmp_path / "Git" / "bin" / "git.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"MZ\0test-git")
    executable.chmod(0o777)
    return executable


@pytest.mark.skipif(executable_trust.os.name == "nt", reason="POSIX owner policy")
def test_posix_trust_rejects_caller_owned_executable_even_when_mode_is_0755(
    tmp_path: Path,
) -> None:
    """The owner can chmod and replace a read-only-looking executable."""

    executable = tmp_path / "git"
    executable.write_bytes(b"caller-owned-git")
    executable.chmod(0o755)

    with pytest.raises(ExecutableTrustError, match="untrusted principal"):
        executable_trust.capture_trusted_executable(executable)


@pytest.mark.skipif(executable_trust.os.name == "nt", reason="POSIX owner policy")
def test_posix_trust_accepts_root_owned_system_executable() -> None:
    """Packaged macOS/Linux system-tool paths remain usable."""

    executable = Path("/usr/bin/true")
    if not executable.is_file() or executable.stat().st_uid != 0:
        pytest.skip("root-owned /usr/bin/true is unavailable")

    resolved, identity = executable_trust.capture_trusted_executable(executable)

    assert resolved == executable.resolve(strict=True)
    assert identity["uid"] == "0"


@pytest.mark.skipif(executable_trust.os.name == "nt", reason="POSIX owner policy")
def test_posix_capture_to_invoke_rejects_executable_byte_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The identity binding still detects replacement after a trusted capture."""

    executable = tmp_path / "git"
    executable.write_bytes(b"captured-git")
    executable.chmod(0o755)
    # The owner policy is covered independently above.  Inject a trusted
    # ancestry here so this test isolates the capture-to-invoke identity check.
    monkeypatch.setattr(
        executable_trust,
        "_assert_posix_non_replaceable",
        lambda _path: None,
    )
    _resolved, identity = executable_trust.capture_trusted_executable(executable)

    executable.write_bytes(b"tampered-git")

    with pytest.raises(ExecutableTrustError, match="identity changed"):
        executable_trust.trusted_executable_path(
            executable,
            expected_identity=identity,
        )


def test_windows_trust_uses_dacl_not_posix_mode_bits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ACL-safe Windows executable is not rejected for a POSIX mode value."""

    executable = _windows_executable(tmp_path)
    monkeypatch.setattr(executable_trust, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(
        executable_trust,
        "_windows_path_security",
        _trusted_windows_security,
    )

    resolved, identity = executable_trust.capture_trusted_executable(executable)

    assert resolved == executable.resolve()
    assert identity["platform"] == "windows"
    assert identity["owner_sid"].startswith("S-1-5-80-")
    assert identity["security_descriptor_sha256"] == "a" * 64
    assert identity["mode"] != "0"


def test_windows_trust_rejects_broad_dacl_write_even_under_program_files_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Program Files-like path has no prefix-based trust exception."""

    executable = _windows_executable(tmp_path)
    monkeypatch.setattr(executable_trust, "_is_windows_platform", lambda: True)

    def writable_security(path: Path) -> WindowsPathSecurity:
        evidence = _trusted_windows_security(path)
        if path == executable.resolve():
            return WindowsPathSecurity(
                owner_sid=evidence.owner_sid,
                caller_sid=evidence.caller_sid,
                caller_rights=evidence.caller_rights,
                descriptor_sha256=evidence.descriptor_sha256,
                principal_rights=(
                    ("world", 0),
                    ("authenticated_users", 0),
                    ("builtin_users", 0x00000002),
                ),
            )
        return evidence

    monkeypatch.setattr(
        executable_trust,
        "_windows_path_security",
        writable_security,
    )

    with pytest.raises(ExecutableTrustError, match="writable"):
        executable_trust.capture_trusted_executable(executable)


def test_windows_trust_revalidates_capture_identity_before_invoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing executable bytes or its DACL evidence invalidates a capture."""

    executable = _windows_executable(tmp_path)
    state = {"descriptor": "a" * 64}
    monkeypatch.setattr(executable_trust, "_is_windows_platform", lambda: True)

    def changing_security(path: Path) -> WindowsPathSecurity:
        evidence = _trusted_windows_security(path)
        return WindowsPathSecurity(
            owner_sid=evidence.owner_sid,
            caller_sid=evidence.caller_sid,
            caller_rights=evidence.caller_rights,
            descriptor_sha256=state["descriptor"],
            principal_rights=evidence.principal_rights,
        )

    monkeypatch.setattr(executable_trust, "_windows_path_security", changing_security)
    _resolved, identity = executable_trust.capture_trusted_executable(executable)

    executable.write_bytes(b"MZ\0changed-git")
    with pytest.raises(ExecutableTrustError, match="identity changed"):
        executable_trust.trusted_executable_path(
            executable,
            expected_identity=identity,
        )

    executable.write_bytes(b"MZ\0test-git")
    _resolved, identity = executable_trust.capture_trusted_executable(executable)
    state["descriptor"] = "b" * 64
    with pytest.raises(ExecutableTrustError, match="identity changed"):
        executable_trust.trusted_executable_path(
            executable,
            expected_identity=identity,
        )


def test_git_provider_capture_degrades_untrusted_git_to_operation_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git absence cannot abort default Profile capture for unrelated features."""

    context = SimpleNamespace(
        profile_id="defaults",
        plan_digest="sha256:" + "a" * 64,
        security_epoch=1,
        state_root=tmp_path,
    )
    monkeypatch.setattr(
        publish,
        "_git_toolchain_identity",
        lambda: (_ for _ in ()).throw(ExecutableTrustError("untrusted")),
    )
    service = publish.GitPushProviderV4(context)
    invocation = SimpleNamespace(
        envelope=SimpleNamespace(
            context=SimpleNamespace(
                profile_id="defaults",
                plan_digest="sha256:" + "a" * 64,
                security_epoch=1,
            )
        )
    )

    with pytest.raises(PermissionError, match="GIT_EXECUTABLE_UNAVAILABLE"):
        service.invoke(publish.PREPARE_OPERATION, {}, invocation)


def test_windows_trust_rejects_ordinary_user_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows ownership itself confers WRITE_DAC, so user-owned Git is denied."""

    executable = _windows_executable(tmp_path)
    monkeypatch.setattr(executable_trust, "_is_windows_platform", lambda: True)

    def user_owned(path: Path) -> WindowsPathSecurity:
        evidence = _trusted_windows_security(path)
        if path == executable.resolve():
            return WindowsPathSecurity(
                owner_sid=evidence.caller_sid,
                caller_sid=evidence.caller_sid,
                caller_rights=0,
                descriptor_sha256=evidence.descriptor_sha256,
                principal_rights=evidence.principal_rights,
            )
        return evidence

    monkeypatch.setattr(executable_trust, "_windows_path_security", user_owned)

    with pytest.raises(ExecutableTrustError, match="trusted system principal"):
        executable_trust.capture_trusted_executable(executable)


def test_windows_trust_rejects_individual_caller_effective_write_ace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AccessCheck catches a mutating ACE granted to the actual Host caller."""

    executable = _windows_executable(tmp_path)
    monkeypatch.setattr(executable_trust, "_is_windows_platform", lambda: True)

    def caller_writable(path: Path) -> WindowsPathSecurity:
        evidence = _trusted_windows_security(path)
        if path == executable.resolve():
            return WindowsPathSecurity(
                owner_sid=evidence.owner_sid,
                caller_sid=evidence.caller_sid,
                caller_rights=0x00040000,  # WRITE_DAC
                descriptor_sha256=evidence.descriptor_sha256,
                principal_rights=evidence.principal_rights,
            )
        return evidence

    monkeypatch.setattr(
        executable_trust,
        "_windows_path_security",
        caller_writable,
    )

    with pytest.raises(ExecutableTrustError, match="Host caller"):
        executable_trust.capture_trusted_executable(executable)


def test_windows_trust_rejects_other_user_concrete_sid_write_ace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mutating ACE for another local user is denied, not missed by caller checks."""

    executable = _windows_executable(tmp_path)
    monkeypatch.setattr(executable_trust, "_is_windows_platform", lambda: True)

    def other_user_writable(path: Path) -> WindowsPathSecurity:
        evidence = _trusted_windows_security(path)
        if path == executable.resolve():
            return WindowsPathSecurity(
                owner_sid=evidence.owner_sid,
                caller_sid=evidence.caller_sid,
                caller_rights=evidence.caller_rights,
                descriptor_sha256=evidence.descriptor_sha256,
                principal_rights=evidence.principal_rights,
                dacl_principal_rights=(("S-1-5-21-2000-2000-2000-1002", 0x00000002),),
            )
        return evidence

    monkeypatch.setattr(
        executable_trust,
        "_windows_path_security",
        other_user_writable,
    )

    with pytest.raises(ExecutableTrustError, match="DACL grants mutation"):
        executable_trust.capture_trusted_executable(executable)


def test_windows_trust_rejects_custom_group_write_ace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A custom local/domain group cannot replace a protected executable child."""

    executable = _windows_executable(tmp_path)
    monkeypatch.setattr(executable_trust, "_is_windows_platform", lambda: True)

    def custom_group_parent_rights(path: Path) -> WindowsPathSecurity:
        evidence = _trusted_windows_security(path)
        if path == executable.resolve().parent:
            return WindowsPathSecurity(
                owner_sid=evidence.owner_sid,
                caller_sid=evidence.caller_sid,
                caller_rights=evidence.caller_rights,
                descriptor_sha256=evidence.descriptor_sha256,
                principal_rights=evidence.principal_rights,
                dacl_principal_rights=(("S-1-5-21-3000-3000-3000-2101", 0x00000040),),
            )
        return evidence

    monkeypatch.setattr(
        executable_trust,
        "_windows_path_security",
        custom_group_parent_rights,
    )

    with pytest.raises(ExecutableTrustError, match="DACL grants mutation"):
        executable_trust.capture_trusted_executable(executable)


def test_windows_trust_honors_effective_deny_for_other_principal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enumerated SID is safe only when ordered DACL evaluation grants no write."""

    executable = _windows_executable(tmp_path)
    monkeypatch.setattr(executable_trust, "_is_windows_platform", lambda: True)

    def denied_other_user(path: Path) -> WindowsPathSecurity:
        evidence = _trusted_windows_security(path)
        if path == executable.resolve():
            return WindowsPathSecurity(
                owner_sid=evidence.owner_sid,
                caller_sid=evidence.caller_sid,
                caller_rights=evidence.caller_rights,
                descriptor_sha256=evidence.descriptor_sha256,
                principal_rights=evidence.principal_rights,
                # GetEffectiveRightsFromAclW has applied the deny ACE.
                dacl_principal_rights=(("S-1-5-21-2000-2000-2000-1002", 0),),
            )
        return evidence

    monkeypatch.setattr(
        executable_trust,
        "_windows_path_security",
        denied_other_user,
    )

    resolved, _identity = executable_trust.capture_trusted_executable(executable)
    assert resolved == executable.resolve()


def test_windows_root_create_child_right_does_not_imply_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FILE_ADD_FILE on an ancestor cannot replace an existing protected child."""

    executable = _windows_executable(tmp_path)
    monkeypatch.setattr(executable_trust, "_is_windows_platform", lambda: True)

    def root_allows_creation(path: Path) -> WindowsPathSecurity:
        evidence = _trusted_windows_security(path)
        if path == path.parent:
            return WindowsPathSecurity(
                owner_sid=evidence.owner_sid,
                caller_sid=evidence.caller_sid,
                caller_rights=0x00000002,  # FILE_ADD_FILE on a directory
                descriptor_sha256=evidence.descriptor_sha256,
                principal_rights=(),
                dacl_principal_rights=(("S-1-5-11", 0x00000002),),
            )
        return evidence

    monkeypatch.setattr(
        executable_trust,
        "_windows_path_security",
        root_allows_creation,
    )

    resolved, _identity = executable_trust.capture_trusted_executable(executable)
    assert resolved == executable.resolve()


def test_windows_trust_rejects_parent_delete_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELETE_CHILD on a parent permits replacement despite a protected child DACL."""

    executable = _windows_executable(tmp_path)
    monkeypatch.setattr(executable_trust, "_is_windows_platform", lambda: True)

    def replaceable_parent(path: Path) -> WindowsPathSecurity:
        evidence = _trusted_windows_security(path)
        if path == executable.resolve().parent:
            return WindowsPathSecurity(
                owner_sid=evidence.owner_sid,
                caller_sid=evidence.caller_sid,
                caller_rights=0x00000040,
                descriptor_sha256=evidence.descriptor_sha256,
                principal_rights=evidence.principal_rights,
            )
        return evidence

    monkeypatch.setattr(
        executable_trust,
        "_windows_path_security",
        replaceable_parent,
    )

    with pytest.raises(ExecutableTrustError, match="Host caller"):
        executable_trust.capture_trusted_executable(executable)


def test_windows_trust_rejects_junction_or_other_reparse_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path resolution fails closed before traversing any Windows reparse point."""

    executable = _windows_executable(tmp_path)
    junction = executable.parent.parent
    monkeypatch.setattr(executable_trust, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(
        executable_trust,
        "_windows_file_attributes",
        lambda path: 0x00000400 if path == junction else 0,
    )

    with pytest.raises(ExecutableTrustError, match="reparse point"):
        executable_trust.capture_trusted_executable(executable)


def test_actual_permission_error_degrades_git_provider_without_aborting_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real filesystem PermissionError becomes Git-only unavailability."""

    context = SimpleNamespace(
        profile_id="defaults",
        plan_digest="sha256:" + "a" * 64,
        security_epoch=1,
        state_root=tmp_path,
    )
    monkeypatch.setattr(publish, "_ssh_executable", lambda: None)
    monkeypatch.setattr(publish, "_askpass_executable", lambda: None)
    monkeypatch.setattr(publish, "_git_executable", lambda: "/trusted/git.exe")
    monkeypatch.setattr(
        publish,
        "capture_trusted_executable",
        lambda _path: (_ for _ in ()).throw(PermissionError("ACL denied")),
    )

    service = publish.GitPushProviderV4(context)
    invocation = SimpleNamespace(
        envelope=SimpleNamespace(
            context=SimpleNamespace(
                profile_id="defaults",
                plan_digest="sha256:" + "a" * 64,
                security_epoch=1,
            )
        )
    )
    with pytest.raises(PermissionError, match="GIT_EXECUTABLE_UNAVAILABLE"):
        service.invoke(publish.PREPARE_OPERATION, {}, invocation)


def test_legacy_fast_forward_check_uses_trusted_hardened_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compatibility adapter never executes an inherited-PATH bare git."""

    calls: list[tuple[Path, list[str], int, bool]] = []

    def run_git(
        repository: Path,
        args: list[str],
        *,
        timeout: int,
        hardened: bool,
    ) -> SimpleNamespace:
        calls.append((repository, args, timeout, hardened))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(publish, "_run_git", run_git)
    publish._assert_non_force_fast_forward(
        tmp_path,
        {
            "force_with_lease": False,
            "expected_remote_oid": "1" * 40,
            "expected_source_oid": "2" * 40,
        },
    )

    assert calls == [
        (
            tmp_path,
            ["merge-base", "--is-ancestor", "1" * 40, "2" * 40],
            30,
            True,
        )
    ]


def test_malformed_inherit_only_access_ace_is_rejected_before_skip() -> None:
    """INHERIT_ONLY cannot bypass the declared ACE/SID bounds validation."""

    ace = ctypes.create_string_buffer(20)
    ace[0] = b"\x00"  # ACCESS_ALLOWED_ACE_TYPE
    ace[1] = b"\x08"  # INHERIT_ONLY_ACE
    ace[2:4] = (8).to_bytes(2, "little")  # No declared room for the SID.
    api = _FakeAclApi(ace, sid_size=12)

    with pytest.raises(ValueError, match="SID exceeds"):
        executable_trust._windows_dacl_principal_rights(
            api,
            object(),
            ctypes.c_void_p(1),
            _TrusteeW,
            _AclSizeInformation,
            executable_trust.wintypes.DWORD,
        )


def test_valid_inherit_only_access_ace_is_skipped_after_validation() -> None:
    """A structurally valid inherited-only grant is deferred to its child ACL."""

    ace = ctypes.create_string_buffer(20)
    ace[0] = b"\x00"  # ACCESS_ALLOWED_ACE_TYPE
    ace[1] = b"\x08"  # INHERIT_ONLY_ACE
    ace[2:4] = (20).to_bytes(2, "little")
    api = _FakeAclApi(ace, sid_size=12)

    rights = executable_trust._windows_dacl_principal_rights(
        api,
        object(),
        ctypes.c_void_p(1),
        _TrusteeW,
        _AclSizeInformation,
        executable_trust.wintypes.DWORD,
    )

    assert rights == ()
