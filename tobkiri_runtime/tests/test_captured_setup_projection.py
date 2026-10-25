"""Original full Setup DATA projection over captured inputs, not GUI authority.

The ordinary clean packaged-source fixture remains mandatory. This compares
the original resolver, preview, confirmation and listing without executing a
Wasm worker, starting a Profile, approving anything or claiming native custody.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
    DefaultspackProfileRuntime,
)
from ecosystem.defaultspack.domain.runtime_v4.service import (
    BundledCatalog,
    resolve_default_profile,
)
from tests.conformance_support.packaged_profile import load_packaged_profile_catalog
from tobkiri_protocol import validation
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.captured_inputs import (
    CAPTURE_CHUNK_BYTES,
    CapturedFile,
    CapturedInputs,
)


def _resolve(catalog: BundledCatalog):
    """Supply recorded resolver facts, never Core approval or a Host capability."""
    references = {}
    for edge in catalog.profiles["defaults"]["requested_edges"]:
        key = "|".join(
            str(edge[field])
            for field in (
                "caller_function_id",
                "target_provider_id",
                "contract_id",
                "operation_id",
            )
        )
        references[key] = "authority-ref:" + canonical_digest(key).removeprefix("sha256:")
    return resolve_default_profile(
        catalog,
        "defaults",
        approved_artifact_digests={
            str(pack["pack"]["artifact_digest"]) for pack in catalog.packs.values()
        },
        authority_snapshot_digest="sha256:" + "9" * 64,
        authority_bindings=references,
        security_epoch=7,
    )


def _projections(catalog: BundledCatalog, resolved):
    """Use the complete original application as the behavioral oracle."""
    runtime = DefaultspackProfileRuntime()
    selected = replace(catalog, profiles={**catalog.profiles, "defaults": resolved.profile})
    confirmation = runtime.bootstrap_confirmation(
        resolved=resolved,
        profile_id="defaults",
        authority_snapshot_digest="sha256:" + "9" * 64,
        security_epoch=7,
    )
    return {
        "resolved": resolved,
        "preview": runtime.setup_profile_preview(selected),
        "confirmation": confirmation,
        "listings": [
            runtime.setup_listing(
                selected,
                confirmation,
                active=active,
                activation_denied=denied,
                denial_diagnostic=diagnostic,
            )
            for active, denied, diagnostic in (
                (False, False, None),
                (True, False, None),
                (False, True, "captured-fixture-denied"),
            )
        ],
    }


def _capture(catalog: BundledCatalog):
    """Capture only the three native-selected fixture inputs before UI evaluation."""
    assert catalog.artifact_root is not None
    files = []
    for prefix, root in (
        ("bundle", catalog.root),
        ("artifacts", catalog.artifact_root),
        ("schemas", validation.SCHEMA_DIR),
    ):
        for path in sorted(root.rglob("*")):
            assert not path.is_symlink()
            if path.is_file():
                files.append((prefix + "/" + path.relative_to(root).as_posix(), path.read_bytes()))
    immutable = tuple(files)

    def read(ordinal: int, offset: int, count: int) -> bytes:
        # Test double for a closed native capture binding. Product native
        # custody, ownership and quota enforcement need independent evidence.
        assert type(ordinal) is int and 0 <= ordinal < len(immutable)
        assert 0 < count <= CAPTURE_CHUNK_BYTES
        value = immutable[ordinal][1]
        assert 0 <= offset <= len(value) - count
        return value[offset : offset + count]

    return CapturedInputs((CapturedFile(name, len(raw)) for name, raw in immutable), read).root()


def test_complete_original_setup_data_matches_over_captured_inputs(monkeypatch):
    catalog = load_packaged_profile_catalog()
    original = _projections(catalog, _resolve(catalog))
    capture = _capture(catalog)
    captured_catalog = replace(
        catalog, root=capture / "bundle", artifact_root=capture / "artifacts"
    )

    def no_external_io(*args, **kwargs):
        pytest.fail("captured Setup projection attempted external I/O")

    validation.load_schema.cache_clear()
    validation._schema_store.cache_clear()
    try:
        with monkeypatch.context() as isolated:
            # A real UI worker owns its own module namespace and caches. This
            # test restores the legacy process globals and clears both caches.
            isolated.setattr(validation, "SCHEMA_DIR", capture / "schemas")
            isolated.setattr(Path, "read_bytes", no_external_io)
            for target in (
                "os.open",
                "builtins.open",
                "io.open",
                "subprocess.run",
                "sqlite3.connect",
                "urllib.request.urlopen",
                "socket.socket",
            ):
                isolated.setattr(target, no_external_io)
            actual = _projections(captured_catalog, _resolve(captured_catalog))
            assert actual == original
    finally:
        validation.load_schema.cache_clear()
        validation._schema_store.cache_clear()
