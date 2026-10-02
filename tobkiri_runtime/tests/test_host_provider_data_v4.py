"""Selected data capture uses Profile digests and never extends executable roots."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from core_runtime import host_provider_data_v4 as data_capture
from core_runtime.host_provider_backend_v4 import HostProviderDataRequestV4
from tests.test_declared_pack_data import PACK_ID, data_pack as data_pack
from tobkiri_host.errors import AuthorizationError, InvalidArtifactError


def _lock(digest: str, role: str = "pack") -> dict:
    return {"effective_set": [{"identity": PACK_ID, "artifact_digest": digest, "role": role}]}


def _factory(*requests: HostProviderDataRequestV4) -> SimpleNamespace:
    return SimpleNamespace(
        declared_pack_data=requests or (HostProviderDataRequestV4(PACK_ID, "tools/"),)
    )


def test_selected_admitted_data_is_pinned_and_cached(
    data_pack: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, digest = data_pack
    calls = []

    def resolve(pack_id: str, ecosystem_root: Path) -> Path:
        calls.append((pack_id, ecosystem_root))
        return root

    monkeypatch.setattr(data_capture, "resolve_admitted_pack_root", resolve)
    lock = _lock(digest)
    owner = data_capture.HostProviderDataCaptureV4(lock, root.parent)
    lock["effective_set"][0]["artifact_digest"] = "sha256:" + "0" * 64
    captured = owner.capture_for(_factory())
    assert captured[0].artifact_digest == digest
    assert len(captured[0].files) == 30
    (root / "tools/calculator/manifest.json").write_text("changed after capture")
    assert owner.capture_for(_factory())[0] is captured[0]
    assert calls == [(PACK_ID, root.parent)]
    assert all(not item.executable for item in captured[0].files)
    owner.assert_current()


@pytest.mark.parametrize("selected", [False, True])
def test_absent_or_non_pack_identity_never_resolves_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selected: bool,
) -> None:
    def reject(*_args: object) -> Path:
        pytest.fail("unselected data must not access the Pack catalog")

    monkeypatch.setattr(data_capture, "resolve_admitted_pack_root", reject)
    lock = _lock("sha256:" + "0" * 64, role="shell") if selected else {"effective_set": []}
    owner = data_capture.HostProviderDataCaptureV4(lock, tmp_path)
    assert owner.capture_for(_factory()) == ()
    assert owner.capture_for(SimpleNamespace()) == ()


def test_selected_digest_mismatch_is_not_an_empty_catalog(
    data_pack: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = data_pack
    monkeypatch.setattr(data_capture, "resolve_admitted_pack_root", lambda *_args: root)
    owner = data_capture.HostProviderDataCaptureV4(_lock("sha256:" + "0" * 64), root.parent)
    with pytest.raises(InvalidArtifactError, match="artifact identity mismatch"):
        owner.capture_for(_factory())


@pytest.mark.parametrize("replacement", ["directory", "symlink", "missing"])
def test_replaced_data_root_fences_both_cached_capture_and_invocation(
    data_pack: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    root, digest = data_pack
    monkeypatch.setattr(data_capture, "resolve_admitted_pack_root", lambda *_args: root)
    owner = data_capture.HostProviderDataCaptureV4(_lock(digest), root.parent)
    owner.capture_for(_factory())
    previous = root.with_name("previous")
    root.rename(previous)
    if replacement == "directory":
        root.mkdir()
    elif replacement == "symlink":
        root.symlink_to(previous, target_is_directory=True)
    with pytest.raises(AuthorizationError):
        owner.assert_current()
    with pytest.raises(AuthorizationError):
        owner.capture_for(_factory())


@pytest.mark.parametrize(
    "declaration",
    [
        None,
        [],
        (None,),
        (HostProviderDataRequestV4(PACK_ID, "tools/"),) * 2,
        tuple(HostProviderDataRequestV4(PACK_ID, f"data{n}/") for n in range(17)),
        (HostProviderDataRequestV4("../foreign", "tools/"),),
        (HostProviderDataRequestV4(PACK_ID, "./"),),
        (HostProviderDataRequestV4(PACK_ID, "tools//"),),
        (HostProviderDataRequestV4(PACK_ID, "tools/../"),),
        (HostProviderDataRequestV4(PACK_ID, "/tools/"),),
    ],
)
def test_static_declarations_are_finite_and_canonical(
    tmp_path: Path,
    declaration: object,
) -> None:
    owner = data_capture.HostProviderDataCaptureV4({"effective_set": []}, tmp_path)
    with pytest.raises(AuthorizationError):
        owner.capture_for(SimpleNamespace(declared_pack_data=declaration))
