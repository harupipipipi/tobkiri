"""Exact retained bundled roots preserve normal resolver/read boundaries."""

from pathlib import Path
import copy
import shutil
from typing import Any

import pytest

from core_runtime import admitted_pack_read_v4 as reads
from core_runtime import bundled_artifact_retention_v4 as retention
from core_runtime import external_pack_catalog_v4 as external
from core_runtime.authority.v4 import AuthorityStore
from core_runtime.pack_boundary import PackBoundaryError, resolve_pack_root
from tests.test_bundled_artifact_retention_v4 import (
    make_retention_fixture,
    retain_fixture,
)
from tests.test_external_pack_catalog_v4 import PACK_ID, _signed_external_pack
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_protocol.validation import validate_file

BUNDLED_ID = "rumi_provider_adapters_pack"
RetainedReadFixture = tuple[
    retention.BundledArtifactOrigin, retention.RetainedBundledArtifact, Path
]


@pytest.fixture
def retained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RetainedReadFixture:
    """Use normal active capture and real byte retention in a temporary owner."""
    fixture = make_retention_fixture(tmp_path, monkeypatch)
    origin = fixture.origin
    artifact = retain_fixture(fixture)
    # The resolver port supplies a valid compiled current revision with a
    # different digest. Retained bytes and their Authority origin remain real.
    current = resolve_pack_root("rumi_ai_gateway_pack")
    assert compile_pack_root(current).artifact.digest != artifact.artifact_digest
    monkeypatch.setattr(external, "resolve_pack_root", lambda *_args: current)
    monkeypatch.setattr(reads, "resolve_pack_root", lambda *_args: current)
    return origin, artifact, current


def test_common_resolvers_use_only_explicit_exact_retained_pin(
    retained: RetainedReadFixture,
) -> None:
    origin, artifact, current = retained
    assert external.resolve_admitted_pack_root(BUNDLED_ID, origin=origin) == current
    with pytest.raises(PackBoundaryError, match="revision is unavailable"):
        external.resolve_admitted_pack_root(
            BUNDLED_ID, artifact_digest=artifact.artifact_digest
        )
    assert (
        external.resolve_admitted_pack_root(
            BUNDLED_ID, artifact_digest=artifact.artifact_digest, origin=origin
        )
        == artifact.root
    )
    assert external.resolve_admitted_pack_roots(
        (BUNDLED_ID,),
        artifact_pins={BUNDLED_ID: artifact.artifact_digest},
        origin=origin,
    ) == {BUNDLED_ID: artifact.root}
    with pytest.raises(external.ExternalPackCatalogDenied):
        external.resolve_admitted_pack_root(
            BUNDLED_ID, artifact_digest="sha256:" + "f" * 64, origin=origin
        )


def test_retained_scope_fully_reverifies_and_closes(
    retained: RetainedReadFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, artifact, _current = retained
    verify = retention.verify_retained_bundled_artifact
    calls = []

    def counted(
        origin: retention.BundledArtifactOrigin, *, pack_id: str, artifact_digest: str
    ) -> Path:
        calls.append((pack_id, artifact_digest))
        return verify(origin, pack_id=pack_id, artifact_digest=artifact_digest)

    monkeypatch.setattr(retention, "verify_retained_bundled_artifact", counted)
    with reads.verified_pack_root_read(
        {BUNDLED_ID: artifact.artifact_digest}, origin=origin
    ) as reader:
        assert reader.resolve(BUNDLED_ID) == artifact.root
    assert calls == [(BUNDLED_ID, artifact.artifact_digest)] * 2
    with pytest.raises(external.ExternalPackCatalogDenied, match="closed"):
        reader.resolve(BUNDLED_ID)


@pytest.mark.parametrize("failure", ["missing", "symlink", "compiler"])
@pytest.mark.parametrize("scope", [False, True])
def test_retention_cannot_hide_current_boundary_or_compiler_failure(
    retained: RetainedReadFixture,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    scope: bool,
) -> None:
    origin, artifact, _current = retained

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("retention must not hide a current root verification failure")

    def denied(*_args: Any, **_kwargs: Any) -> Path:
        raise PackBoundaryError(f"current root {failure}")

    monkeypatch.setattr(retention, "verify_retained_bundled_artifact", forbidden)
    module = reads if scope else external
    if failure == "compiler":
        monkeypatch.setattr(module, "compile_pack_root", denied)
    else:
        monkeypatch.setattr(module, "resolve_pack_root", denied)
    with pytest.raises((PackBoundaryError, external.ExternalPackCatalogDenied)):
        if scope:
            with reads.verified_pack_root_read(
                {BUNDLED_ID: artifact.artifact_digest}, origin=origin
            ) as reader:
                reader.resolve(BUNDLED_ID)
        else:
            external.resolve_admitted_pack_root(
                BUNDLED_ID, artifact_digest=artifact.artifact_digest, origin=origin
            )


@pytest.mark.parametrize("scope", [False, True])
def test_retention_rejects_another_host_owner_before_authority_read(
    retained: RetainedReadFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: bool,
) -> None:
    origin, artifact, _current = retained
    other = tmp_path / "other-owner"
    other.mkdir(mode=0o700)
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(other))

    def forbidden(*_args: Any) -> None:
        pytest.fail("another owner's origin must not open its Authority store")

    monkeypatch.setattr(retention, "assert_retention_origin_current", forbidden)
    with pytest.raises(external.ExternalPackCatalogDenied, match="another Host owner"):
        if scope:
            with reads.verified_pack_root_read({}, origin=origin):
                pytest.fail("foreign origin must fail before scope entry")
        else:
            external.resolve_admitted_pack_root(
                BUNDLED_ID, artifact_digest=artifact.artifact_digest, origin=origin
            )


@pytest.mark.parametrize("change", ["epoch", "content", "root"])
def test_retained_scope_rejects_freshness_or_full_bytes_drift_and_closes(
    retained: RetainedReadFixture, change: str
) -> None:
    origin, artifact, _current = retained
    with pytest.raises((external.ExternalPackCatalogDenied, OSError, ValueError)):
        with reads.verified_pack_root_read(
            {BUNDLED_ID: artifact.artifact_digest}, origin=origin
        ) as reader:
            assert reader.resolve(BUNDLED_ID) == artifact.root
            if change == "epoch":
                with AuthorityStore(
                    origin.user_data_root / "authority/v4.sqlite3"
                ) as store:
                    store.advance_security_epoch("isolated retention read fence")
            elif change == "content":
                runtime = artifact.root / "runtime/streaming.py"
                runtime.chmod(0o600)
                runtime.write_bytes(runtime.read_bytes() + b"\n# drift\n")
            else:
                artifact.root.parent.chmod(0o700)
                moved = artifact.root.with_name("previous")
                artifact.root.rename(moved)
                shutil.copytree(moved, artifact.root)
    with pytest.raises(external.ExternalPackCatalogDenied, match="closed"):
        reader.resolve(BUNDLED_ID)


def test_signed_cas_priority_and_tamper_checks_remain_before_retention(
    retained: RetainedReadFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, _artifact, _current = retained
    source, trust = _signed_external_pack(tmp_path)
    admitted = external.admit_signed_external_pack(source, trust_store_path=trust)

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("signed CAS must never fall through to retained bundled bytes")

    monkeypatch.setattr(retention, "verify_retained_bundled_artifact", forbidden)
    pins = {PACK_ID: admitted["artifact_digest"]}
    root = external.resolve_admitted_pack_root(
        PACK_ID, artifact_digest=admitted["artifact_digest"], origin=origin
    )
    manifest = validate_file(root / "pack.v4.json", "pack")
    assert external.load_verified_pack_executable_catalog(
        PACK_ID, manifest, retained_bundled_origin=origin
    ) == external.load_admitted_external_executable_catalog(PACK_ID, manifest)
    with reads.verified_pack_root_read(pins, origin=origin) as reader:
        assert reader.resolve(PACK_ID) == root
    runtime = root / "runtime/echo.py"
    runtime.chmod(0o600)
    runtime.write_bytes(runtime.read_bytes() + b"\n# tampered\n")
    with pytest.raises(external.ExternalPackCatalogDenied):
        external.resolve_admitted_pack_root(
            BUNDLED_ID,
            artifact_digest=dict(origin.pack_pins)[BUNDLED_ID],
            origin=origin,
        )


def test_retained_executable_catalog_requires_exact_manifest_and_origin(
    retained: RetainedReadFixture,
) -> None:
    origin, artifact, _current = retained
    manifest = validate_file(artifact.root / "pack.v4.json", "pack")
    with pytest.raises(external.ExternalPackCatalogDenied, match="not admitted"):
        external.load_verified_pack_executable_catalog(BUNDLED_ID, manifest)
    assert external.load_verified_pack_executable_catalog(
        BUNDLED_ID, manifest, retained_bundled_origin=origin
    ) == validate_file(artifact.root / "executables.v4.json", "executable_catalog")
    changed = copy.deepcopy(manifest)
    changed["pack"]["version"] = "9.9.9"
    with pytest.raises(external.ExternalPackCatalogDenied, match="selected manifest"):
        external.load_verified_pack_executable_catalog(
            BUNDLED_ID, changed, retained_bundled_origin=origin
        )


def test_origin_does_not_make_current_bundled_executable_an_external_admission(
    retained: RetainedReadFixture,
) -> None:
    origin, _artifact, current = retained
    manifest = validate_file(current / "pack.v4.json", "pack")
    with pytest.raises(external.ExternalPackCatalogDenied, match="not retained"):
        external.load_verified_pack_executable_catalog(
            BUNDLED_ID, manifest, retained_bundled_origin=origin
        )


@pytest.mark.parametrize("change", ["epoch", "content"])
def test_retained_executable_catalog_rechecks_after_individual_reads(
    retained: RetainedReadFixture, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    origin, artifact, _current = retained
    manifest = validate_file(artifact.root / "pack.v4.json", "pack")
    read = external._read_regular_nofollow
    changed = False

    def racing_read(root: Path, relative: str) -> tuple[bytes, int]:
        nonlocal changed
        result = read(root, relative)
        if root == artifact.root and not changed:
            changed = True
            if change == "epoch":
                path = origin.user_data_root / "authority/v4.sqlite3"
                with AuthorityStore(path) as store:
                    store.advance_security_epoch("isolated executable catalog fence")
            else:
                target = artifact.root / "runtime/streaming.py"
                target.chmod(0o600)
                target.write_bytes(target.read_bytes() + b"\n# read race\n")
        return result

    monkeypatch.setattr(external, "_read_regular_nofollow", racing_read)
    with pytest.raises(external.ExternalPackCatalogDenied):
        external.load_verified_pack_executable_catalog(
            BUNDLED_ID, manifest, retained_bundled_origin=origin
        )
    assert changed
