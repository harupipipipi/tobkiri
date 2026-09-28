"""Exact Host Provider coverage for the file inspection Pack."""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ecosystem.rumi_file_inspect_pack.runtime import inspect
from tobkiri_protocol.canonical import canonical_digest


def _binding(operation_id: str) -> SimpleNamespace:
    principal = f"principal:{operation_id}"
    return SimpleNamespace(
        operation=SimpleNamespace(
            contract_id=inspect.CONTRACT_ID,
            contract_version=inspect.CONTRACT_VERSION,
            operation_id=operation_id,
        ),
        principal_ref=SimpleNamespace(value=principal),
        artifact=SimpleNamespace(digest="sha256:" + "a" * 64),
        function=SimpleNamespace(
            function_id=inspect.FUNCTION_ID,
            implementation_digest="sha256:" + "b" * 64,
        ),
    )


def _context(tmp_path: Path, bindings: tuple[SimpleNamespace, ...]) -> SimpleNamespace:
    return SimpleNamespace(
        profile_id="defaults",
        plan_digest="sha256:" + "c" * 64,
        security_epoch=4,
        activation={"activation_id": "activation-1"},
        state_root=tmp_path,
        provider_bindings=bindings,
        domain_ids={
            (
                binding.operation.contract_id,
                binding.operation.operation_id,
                binding.principal_ref.value,
            ): f"domain:{binding.operation.operation_id}"
            for binding in bindings
        },
    )


def _workspace_binding(root: Path) -> dict[str, object]:
    stat = root.stat()
    binding: dict[str, object] = {
        "workspace_id": "workspace-1",
        "access": "read_only",
        "mount_revision": 7,
        "canonical_root": str(root.resolve()),
        "root_st_dev": int(stat.st_dev),
        "root_st_ino": int(stat.st_ino),
    }
    binding["root_identity"] = hashlib.sha256(
        json.dumps(
            binding,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return binding


class _WorkspaceClient:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        self.calls.append((contract_id, operation_id, dict(payload)))
        assert contract_id == inspect.WORKSPACE
        assert operation_id == inspect.WORKSPACE_OPERATION
        if payload["operation"] == "list":
            return {"selected_workspace_id": "workspace-1"}
        return {
            "workspace_id": "workspace-1",
            "root_path": str(self.root),
            "mount_revision": 7,
        }


class _Invocation:
    def __init__(
        self,
        context: SimpleNamespace,
        binding: SimpleNamespace,
        payload: dict[str, object],
        client: _WorkspaceClient,
    ) -> None:
        self.client = client
        self.current_checks = 0
        self.envelope = SimpleNamespace(
            contract_id=inspect.CONTRACT_ID,
            contract_version=inspect.CONTRACT_VERSION,
            operation_id=binding.operation.operation_id,
            target_principal=binding.principal_ref,
            target_domain=SimpleNamespace(
                value=context.domain_ids[
                    (
                        inspect.CONTRACT_ID,
                        binding.operation.operation_id,
                        binding.principal_ref.value,
                    )
                ]
            ),
            payload=payload,
            context=SimpleNamespace(
                profile_id=context.profile_id,
                activation_id=context.activation["activation_id"],
                activation_digest=canonical_digest(dict(context.activation)),
                plan_digest=context.plan_digest,
                security_epoch=context.security_epoch,
            ),
        )

    def assert_current(self) -> None:
        self.current_checks += 1

    def contract_client(self, **kwargs: object) -> _WorkspaceClient:
        assert kwargs == {
            "allowed_contract_ids": frozenset({inspect.WORKSPACE}),
            "consumer_pack_id": inspect.PACK_ID,
            "include_credentials": False,
        }
        return self.client


def test_factory_captures_selected_operation_subset(tmp_path: Path) -> None:
    binding = _binding("rumi_file_inspect_pack.file-inspect")
    captured = inspect.HOST_PROVIDER_FACTORY.capture(
        _context(tmp_path, (binding,))
    )

    assert len(captured.contributions) == 1
    contribution = captured.contributions[0]
    assert contribution.operation_id == binding.operation.operation_id
    assert contribution.principal_id == binding.principal_ref.value
    captured.close()


def test_factory_dispatches_through_canonical_workspace_contract(
    tmp_path: Path,
) -> None:
    (tmp_path / "proof.txt").write_bytes(b"verified\n")
    binding = _binding("rumi_file_inspect_pack.file-inspect.for-media")
    context = _context(tmp_path, (binding,))
    captured = inspect.HOST_PROVIDER_FACTORY.capture(context)
    client = _WorkspaceClient(tmp_path)
    payload: dict[str, object] = {
        "name": "read",
        "profile_id": "defaults",
        "workspace_id": "workspace-1",
        "path": "proof.txt",
        "require_selected": True,
        "_workspace_binding": _workspace_binding(tmp_path),
    }
    invocation = _Invocation(context, binding, payload, client)

    result = captured.contributions[0].invoke(
        binding.operation.operation_id,
        payload,
        invocation,
    )

    assert result["content"] == "verified\n"
    assert [call[2]["operation"] for call in client.calls] == [
        "list",
        "get",
        "list",
        "get",
        "list",
    ]
    assert invocation.current_checks >= 6


def test_factory_rejects_changed_capture_or_invocation_before_workspace_access(
    tmp_path: Path,
) -> None:
    binding = _binding("rumi_file_inspect_pack.file-inspect")
    context = _context(tmp_path, (binding,))
    bad_binding = _binding("rumi_file_inspect_pack.file-inspect")
    bad_binding.function.function_id = "attacker.file-inspect"
    with pytest.raises(PermissionError, match="incomplete"):
        inspect.HOST_PROVIDER_FACTORY.capture(
            _context(tmp_path, (bad_binding,))
        )

    captured = inspect.HOST_PROVIDER_FACTORY.capture(context)
    client = _WorkspaceClient(tmp_path)
    payload: dict[str, object] = {
        "name": "list",
        "profile_id": "another-profile",
        "workspace_id": "workspace-1",
        "_workspace_binding": _workspace_binding(tmp_path),
    }
    invocation = _Invocation(context, binding, payload, client)
    with pytest.raises(PermissionError, match="profile changed"):
        captured.contributions[0].invoke(
            binding.operation.operation_id,
            payload,
            invocation,
        )
    assert client.calls == []


def _service_payload(root: Path, **values: object) -> dict[str, object]:
    return {
        "profile_id": "defaults",
        "workspace_id": "workspace-1",
        "require_selected": True,
        "_workspace_binding": _workspace_binding(root),
        **values,
    }


def _open_test_directory(path: Path) -> int:
    # CRT os.open does not accept directories on Windows. Use the same
    # no-follow native handle used by the provider under test.
    if os.name == "nt":
        return inspect._create_file_windows_no_follow(path)
    return os.open(path, os.O_RDONLY)


@pytest.mark.parametrize("name", ["stat", "list", "search"])
def test_path_operations_stay_bound_to_opened_root_during_swap_back_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    root = tmp_path / "workspace"
    parked = tmp_path / "parked"
    replacement = tmp_path / "replacement"
    root.mkdir()
    replacement.mkdir()
    (root / "proof.txt").write_text("authorized", encoding="utf-8")
    (replacement / "intruder.txt").write_text("unauthorized", encoding="utf-8")
    original_open = inspect._open_relative_fd
    race_attempted = False

    def swap_around_open(
        opened_root: Path,
        root_fd: int,
        relative: Path,
    ) -> int:
        nonlocal race_attempted
        if not race_attempted:
            race_attempted = True
            if os.name == "nt":
                # The native root handle deliberately denies delete sharing,
                # so the rename itself must fail while that handle is live.
                with pytest.raises(PermissionError):
                    root.rename(parked)
                return original_open(opened_root, root_fd, relative)
            root.rename(parked)
            replacement.rename(root)
            try:
                return original_open(opened_root, root_fd, relative)
            finally:
                root.rename(replacement)
                parked.rename(root)
        return original_open(opened_root, root_fd, relative)

    monkeypatch.setattr(inspect, "_open_relative_fd", swap_around_open)
    payload_values: dict[str, object]
    if name == "stat":
        payload_values = {"path": "proof.txt"}
    elif name == "search":
        payload_values = {"directory": ".", "pattern": "*.txt"}
    else:
        payload_values = {"directory": ".", "recursive": True}

    result = inspect.FileInspectService(_WorkspaceClient(root)).invoke(
        name,
        _service_payload(root, **payload_values),
    )

    assert race_attempted
    if name == "stat":
        assert result["size"] == len("authorized")
    elif name == "search":
        assert result["matches"] == ["proof.txt"]
    else:
        assert [item["path"] for item in result["items"]] == ["proof.txt"]


def test_read_rejects_component_replaced_with_symlink_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "proof.txt"
    target.write_text("authorized", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("unauthorized", encoding="utf-8")
    original_open = inspect._open_relative_fd
    swapped = False

    def swap_before_open(
        opened_root: Path,
        root_fd: int,
        relative: Path,
    ) -> int:
        nonlocal swapped
        if relative == Path("proof.txt") and not swapped:
            swapped = True
            target.unlink()
            target.symlink_to(outside)
        return original_open(opened_root, root_fd, relative)

    monkeypatch.setattr(inspect, "_open_relative_fd", swap_before_open)

    with pytest.raises(OSError):
        inspect.FileInspectService(_WorkspaceClient(root)).invoke(
            "read",
            _service_payload(root, path="proof.txt"),
        )
    assert swapped


def test_file_inspect_fails_closed_without_descriptor_relative_primitives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "proof.txt").write_text("authorized", encoding="utf-8")
    monkeypatch.setattr(inspect, "_is_windows_host", lambda: False)
    monkeypatch.setattr(inspect.os, "supports_dir_fd", set())

    with pytest.raises(PermissionError, match="descriptor-relative"):
        inspect._require_descriptor_support()


def _mock_windows_relative_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    final_path: str,
    reparse: bool = False,
) -> tuple[int, list[tuple[str, bool | None]]]:
    root_fd = _open_test_directory(tmp_path)
    opened: list[tuple[str, bool | None]] = []
    root_final = r"\\?\Volume{test}\workspace"

    def nt_open(
        _parent_fd: int,
        name: str,
        *,
        directory: bool | None,
    ) -> int:
        opened.append((name, directory))
        return os.open(tmp_path / name, os.O_RDONLY)

    def handle_path(descriptor: int) -> str:
        metadata = os.fstat(descriptor)
        target = (tmp_path / "proof.txt").stat()
        return final_path if metadata.st_ino == target.st_ino else root_final

    monkeypatch.setattr(inspect, "_is_windows_host", lambda: True)
    monkeypatch.setattr(inspect, "_nt_open_relative", nt_open)
    monkeypatch.setattr(inspect, "_windows_final_path", handle_path)
    monkeypatch.setattr(
        inspect,
        "_windows_handle_is_reparse",
        lambda descriptor: reparse
        and os.fstat(descriptor).st_ino == (tmp_path / "proof.txt").stat().st_ino,
    )
    return root_fd, opened


def test_windows_relative_open_uses_pinned_parent_and_normalized_final_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "proof.txt").write_text("verified", encoding="utf-8")
    expected = r"\\?\volume{test}\workspace\proof.txt"
    root_fd, opened = _mock_windows_relative_open(
        tmp_path,
        monkeypatch,
        final_path=expected,
    )
    try:
        descriptor = inspect._open_relative_fd(
            tmp_path,
            root_fd,
            Path("proof.txt"),
        )
        try:
            assert os.read(descriptor, 8) == b"verified"
        finally:
            os.close(descriptor)
    finally:
        os.close(root_fd)
    assert opened == [("proof.txt", None)]


@pytest.mark.parametrize(
    ("final_path", "reparse"),
    [
        (r"\\?\volume{other}\outside\proof.txt", False),
        (r"\\?\volume{test}\workspace\proof.txt", True),
    ],
)
def test_windows_relative_open_rejects_outside_or_reparse_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    final_path: str,
    reparse: bool,
) -> None:
    (tmp_path / "proof.txt").write_text("untrusted", encoding="utf-8")
    root_fd, _ = _mock_windows_relative_open(
        tmp_path,
        monkeypatch,
        final_path=final_path,
        reparse=reparse,
    )
    try:
        with pytest.raises(PermissionError, match="reparse point or changed"):
            inspect._open_relative_fd(tmp_path, root_fd, Path("proof.txt"))
    finally:
        os.close(root_fd)


@pytest.mark.parametrize(
    "value",
    [
        "file.txt:secret",
        r"parent\child.txt",
        "CON",
        "aux.txt",
        "trailing.",
        "trailing ",
        " leading.txt",
    ],
)
def test_windows_paths_reject_ads_separators_and_device_names(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setattr(inspect, "_is_windows_host", lambda: True)
    with pytest.raises(PermissionError, match="unsafe Windows"):
        inspect._safe_relative(value)


def test_windows_directory_listing_uses_verified_handle_enumerator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(inspect, "_is_windows_host", lambda: True)
    monkeypatch.setattr(
        inspect,
        "_windows_directory_names",
        lambda descriptor: [f"entry-{descriptor}"],
    )

    assert inspect._directory_names(42) == ["entry-42"]


def test_windows_root_open_rejects_identity_mismatch_and_closes_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    replacement = tmp_path / "replacement"
    root.mkdir()
    replacement.mkdir()
    opened: list[int] = []
    native_open_directory = inspect._create_file_windows_no_follow

    def open_replacement(_path: Path) -> int:
        descriptor = (
            native_open_directory(replacement)
            if os.name == "nt"
            else os.open(replacement, os.O_RDONLY)
        )
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(inspect, "_is_windows_host", lambda: True)
    monkeypatch.setattr(
        inspect,
        "_create_file_windows_no_follow",
        open_replacement,
    )
    monkeypatch.setattr(
        inspect,
        "_windows_handle_is_reparse",
        lambda _descriptor: False,
    )

    with pytest.raises(PermissionError, match="changed while opening"):
        inspect._open_windows_root(root)
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_windows_intermediate_reparse_is_rejected_before_final_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "proof.txt").write_text("untrusted", encoding="utf-8")
    root_fd = _open_test_directory(tmp_path)
    opened: list[str] = []
    root_final = r"\\?\volume{test}\workspace"

    def nt_open(
        _parent_fd: int,
        name: str,
        *,
        directory: bool | None,
    ) -> int:
        opened.append(name)
        target = folder if name == "folder" else folder / name
        if os.name == "nt" and target.is_dir():
            return _open_test_directory(target)
        return os.open(target, os.O_RDONLY)

    def handle_path(descriptor: int) -> str:
        metadata = os.fstat(descriptor)
        if metadata.st_ino == folder.stat().st_ino:
            return root_final + r"\folder"
        return root_final

    monkeypatch.setattr(inspect, "_is_windows_host", lambda: True)
    monkeypatch.setattr(inspect, "_nt_open_relative", nt_open)
    monkeypatch.setattr(inspect, "_windows_final_path", handle_path)
    monkeypatch.setattr(
        inspect,
        "_windows_handle_is_reparse",
        lambda descriptor: os.fstat(descriptor).st_ino == folder.stat().st_ino,
    )
    try:
        with pytest.raises(PermissionError, match="reparse point"):
            inspect._open_relative_fd(
                tmp_path,
                root_fd,
                Path("folder/proof.txt"),
            )
    finally:
        os.close(root_fd)
    assert opened == ["folder"]


def test_windows_missing_native_relative_open_fails_closed_and_closes_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_fd = _open_test_directory(tmp_path)
    duplicated: list[int] = []
    original_dup = os.dup

    def tracked_dup(descriptor: int) -> int:
        duplicate = original_dup(descriptor)
        duplicated.append(duplicate)
        return duplicate

    monkeypatch.setattr(inspect, "_is_windows_host", lambda: True)
    monkeypatch.setattr(inspect.os, "dup", tracked_dup)
    monkeypatch.setattr(
        inspect,
        "_windows_final_path",
        lambda _descriptor: r"\\?\volume{test}\workspace",
    )
    monkeypatch.setattr(
        inspect,
        "_nt_open_relative",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("NtCreateFile unavailable")
        ),
    )
    try:
        with pytest.raises(OSError, match="NtCreateFile unavailable"):
            inspect._open_relative_fd(
                tmp_path,
                root_fd,
                Path("proof.txt"),
            )
    finally:
        os.close(root_fd)
    with pytest.raises(OSError):
        os.fstat(duplicated[0])


def test_windows_tracked_only_reauthorizes_every_git_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = tmp_path / "proof.txt"
    proof.write_text("verified", encoding="utf-8")
    root_fd = _open_test_directory(tmp_path)
    checked: list[Path] = []

    def verified_open(_root: Path, _root_fd: int, relative: Path) -> int:
        checked.append(relative)
        if relative != Path("proof.txt"):
            raise FileNotFoundError(relative)
        return os.open(proof, os.O_RDONLY)

    monkeypatch.setattr(inspect, "_is_windows_host", lambda: True)
    monkeypatch.setattr(
        inspect.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout=b"proof.txt\0replacement-only.txt\0"
        ),
    )
    monkeypatch.setattr(inspect, "_open_relative_fd", verified_open)
    try:
        result = inspect._git_tracked_paths(
            tmp_path,
            root_fd,
            Path("."),
            recursive=True,
        )
    finally:
        os.close(root_fd)

    assert result == [Path("proof.txt")]
    assert checked == [Path("proof.txt"), Path("replacement-only.txt")]
