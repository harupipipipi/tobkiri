"""Warm metadata schema successes never replace live materialization guards."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import shutil
import sys
from typing import Any

import pytest

from core_runtime import host_provider_hooks_v4
from tests.test_artifact_materialization import _copied_binding
from tests.test_job_broker_provider_import import sealed_binding
import tobkiri_host.artifact_materialization as materialization
from tobkiri_host._metadata_validation_cache import _MetadataValidationCache
from tobkiri_host.contracts import ResolvedOperationBinding
from tobkiri_host.errors import InvalidArtifactError
from tobkiri_protocol.errors import SchemaValidationError


_METADATA_KINDS = {
    "pack.v4.json": "pack",
    "contracts.v4.json": "pack_contract_catalog",
    "artifact-index.v4.json": "pack_artifact_index",
    "executables.v4.json": "executable_catalog",
}
_COLD_CALLS = Counter(_METADATA_KINDS.values())


@pytest.fixture
def validation_calls(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    """Isolate successes and count the real validator without changing identity."""
    calls: Counter[str] = Counter()
    original = materialization.validate_document

    def tracked(raw: bytes, kind: str, **kwargs: Any) -> dict[str, Any]:
        calls[kind] += 1
        assert kwargs.get("reject_authority_fields", True) is True
        return original(raw, kind, **kwargs)

    monkeypatch.setattr(materialization, "validate_document", tracked)
    monkeypatch.setattr(
        materialization, "_METADATA_VALIDATION_CACHE", _MetadataValidationCache()
    )
    return calls


def _warm_capture(
    root: Path,
    binding: ResolvedOperationBinding,
    calls: Counter[str],
) -> materialization.MaterializedPackArtifact:
    first = materialization.capture_materialized_artifact(root, binding)
    assert calls == _COLD_CALLS
    assert materialization.capture_materialized_artifact(root, binding) == first
    assert calls == _COLD_CALLS, "all four exact-byte schema successes must be warm"
    return first


@pytest.fixture
def warm_capture(
    tmp_path: Path,
    validation_calls: Counter[str],
) -> tuple[Path, ResolvedOperationBinding]:
    """Warm through real captures of the existing digest-pinned fixture."""
    root, binding = _copied_binding(tmp_path)
    _warm_capture(root, binding, validation_calls)
    return root, binding


def _rewrite_same_stat(path: Path, old: bytes, new: bytes) -> None:
    before = path.stat()
    original = path.read_bytes()
    assert old in original and len(old) == len(new)
    changed = original.replace(old, new, 1)
    assert changed != original
    path.write_bytes(changed)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )


def test_warm_capture_rereads_every_file_through_secure_reader(
    warm_capture: tuple[Path, ResolvedOperationBinding],
    validation_calls: Counter[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only schema work is skipped; the original bounded reader still runs."""
    root, binding = warm_capture
    original = materialization._pack_reader
    reads: Counter[str] = Counter()

    @contextmanager
    def tracked_reader(*args: Any, **kwargs: Any) -> Iterator[Any]:
        with original(*args, **kwargs) as (read_regular, identity):
            def tracked(relative: str) -> tuple[bytes, int]:
                reads[relative] += 1
                return read_regular(relative)

            yield tracked, identity

    monkeypatch.setattr(materialization, "_pack_reader", tracked_reader)
    captured = materialization.capture_materialized_artifact(root, binding)
    assert reads == Counter(item.path for item in captured.files)
    assert validation_calls == _COLD_CALLS


@pytest.mark.parametrize(
    "relative, old, new",
    [
        ("runtime/echo.py", b"return", b"Return"),
        ("pack.v4.json", b"Minimal Echo Pack", b"Changed Echo Pack"),
    ],
)
def test_warm_capture_rejects_same_stat_runtime_and_metadata_digest_changes(
    warm_capture: tuple[Path, ResolvedOperationBinding],
    relative: str,
    old: bytes,
    new: bytes,
) -> None:
    """Even schema-valid metadata and unchanged stat identity are rehashed."""
    root, binding = warm_capture
    _rewrite_same_stat(root / relative, old, new)
    with pytest.raises(InvalidArtifactError, match="digest"):
        materialization.capture_materialized_artifact(root, binding)


@pytest.mark.parametrize("relative", tuple(_METADATA_KINDS))
def test_warm_capture_revalidates_changed_bytes_in_each_metadata_document(
    warm_capture: tuple[Path, ResolvedOperationBinding],
    validation_calls: Counter[str],
    relative: str,
) -> None:
    """Each metadata callsite must miss its success cache on changed bytes."""
    root, binding = warm_capture
    _rewrite_same_stat(root / relative, b'.v4"', b'.v0"')
    with pytest.raises(SchemaValidationError):
        materialization.capture_materialized_artifact(root, binding)
    assert validation_calls == _COLD_CALLS + Counter({_METADATA_KINDS[relative]: 1})


@pytest.mark.parametrize(
    "field", ["artifact", "implementation", "variant", "operation"]
)
def test_warm_capture_still_checks_exact_binding(
    warm_capture: tuple[Path, ResolvedOperationBinding],
    validation_calls: Counter[str],
    field: str,
) -> None:
    """A valid document does not establish a different caller's binding."""
    root, binding = warm_capture
    if field == "artifact":
        changed = replace(
            binding, artifact=replace(binding.artifact, digest="sha256:" + "0" * 64)
        )
        reason = "metadata identity mismatch"
    elif field == "implementation":
        changed = replace(
            binding,
            function=replace(
                binding.function, implementation_digest="sha256:" + "0" * 64
            ),
        )
        reason = "executable binding"
    elif field == "variant":
        changed = replace(
            binding, variant=replace(binding.variant, variant_id="other.variant")
        )
        reason = "executable binding"
    else:
        changed = replace(
            binding, operation=replace(binding.operation, operation_id="other")
        )
        reason = "executable binding"
    with pytest.raises(InvalidArtifactError, match=reason):
        materialization.capture_materialized_artifact(root, changed)
    assert validation_calls == _COLD_CALLS


@pytest.mark.parametrize("relative", ["runtime/echo.py", "pack.v4.json"])
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_warm_capture_rejects_identical_bytes_behind_links(
    warm_capture: tuple[Path, ResolvedOperationBinding],
    tmp_path: Path,
    relative: str,
    link_kind: str,
) -> None:
    """A schema success never authorizes a linked metadata or runtime file."""
    root, binding = warm_capture
    target = root / relative
    outside = tmp_path / "outside"
    target.rename(outside)
    if link_kind == "symlink":
        target.symlink_to(outside)
    else:
        os.link(outside, target)
    with pytest.raises(InvalidArtifactError, match="unavailable|regular file"):
        materialization.capture_materialized_artifact(root, binding)


def test_warm_capture_rejects_identical_pack_root_swapped_during_read(
    warm_capture: tuple[Path, ResolvedOperationBinding],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validation_calls: Counter[str],
) -> None:
    """Root continuity must be checked after all four schema cache hits."""
    root, binding = warm_capture
    replacement = tmp_path / "replacement"
    shutil.copytree(root, replacement)
    original = materialization._pack_reader
    swapped = False

    @contextmanager
    def swapping_reader(*args: Any, **kwargs: Any) -> Iterator[Any]:
        with original(*args, **kwargs) as (read_regular, identity):
            def swap_after_read(relative: str) -> tuple[bytes, int]:
                nonlocal swapped
                result = read_regular(relative)
                if not swapped:
                    swapped = True
                    root.rename(tmp_path / "original")
                    replacement.rename(root)
                return result

            yield swap_after_read, identity

    monkeypatch.setattr(materialization, "_pack_reader", swapping_reader)
    with pytest.raises(InvalidArtifactError, match="root changed"):
        materialization.capture_materialized_artifact(root, binding)
    assert swapped
    assert validation_calls == _COLD_CALLS


@pytest.mark.parametrize("phase", ["before_import", "after_import"])
def test_warm_metadata_preserves_both_host_provider_import_captures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validation_calls: Counter[str],
    phase: str,
) -> None:
    """The actual Host loader rejects mutations around an otherwise valid import."""
    root, binding = sealed_binding(tmp_path)
    captured = _warm_capture(root, binding, validation_calls)
    implementation = root / captured.implementation_path
    original_load = host_provider_hooks_v4.CapturedProviderModules.load
    modules = host_provider_hooks_v4.CapturedProviderModules.for_capture(
        root, captured,
        artifact_digest=binding.artifact.digest,
        pack_id=binding.artifact.pack_id,
    )
    imported = False

    def mutate() -> None:
        implementation.write_bytes(implementation.read_bytes() + b"\n# changed\n")

    def tracked_load(captured_modules: Any) -> Any:
        nonlocal imported
        assert phase == "after_import", "changed bytes must fail before import"
        module = original_load(captured_modules)
        imported = True
        mutate()
        return module

    monkeypatch.setattr(
        host_provider_hooks_v4.CapturedProviderModules, "load", tracked_load
    )
    if phase == "before_import":
        mutate()
    prefix = modules.namespace
    previous_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == prefix or name.startswith(prefix + ".")
    }
    try:
        with pytest.raises(InvalidArtifactError, match="digest"):
            host_provider_hooks_v4.load_host_provider_factory(root, binding)
        assert imported is (phase == "after_import")
        assert validation_calls == _COLD_CALLS
        assert {
            name: module
            for name, module in sys.modules.items()
            if name == prefix or name.startswith(prefix + ".")
        } == previous_modules
    finally:
        for name in tuple(sys.modules):
            if name == prefix or name.startswith(prefix + "."):
                sys.modules.pop(name, None)
        sys.modules.update(previous_modules)
