"""The shared bundle validator consumes ordered DATA, while native loading stays native."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tests.conformance_support.packaged_profile import (
    load_packaged_profile_catalog,
    packaged_profile_bundle_root,
)
from tobkiri_protocol import bundle_catalog
from tobkiri_protocol.bundle_catalog import BundleIntegrityError, BundledCatalog

pytestmark = pytest.mark.contract


@pytest.fixture(scope="module")
def packaged_capture() -> dict[str, Any]:
    """Record the genuine clean packaged fixture's original native reads."""

    root = packaged_profile_bundle_root()
    reads: list[tuple[Path, bytes]] = []
    original_read_bytes = Path.read_bytes

    def record_read_bytes(path: Path) -> bytes:
        raw = original_read_bytes(path)
        if path == root / "bundle.lock.json" or root in path.parents:
            reads.append((path, raw))
        return raw

    patch = pytest.MonkeyPatch()
    patch.setattr(Path, "read_bytes", record_read_bytes)
    try:
        native_catalog = load_packaged_profile_catalog()
    finally:
        patch.undo()

    lock_bytes = reads[0][1]
    lock = bundle_catalog.strict_loads(lock_bytes)
    assert isinstance(lock, dict)
    entries = lock["entries"]
    assert isinstance(entries, list) and entries
    expected_calls = [
        (index, entry["path"], False)
        for index, entry in enumerate(entries)
    ]
    expected_calls.extend(
        (index, entry["path"], True)
        for index, entry in enumerate(entries)
        if entry["kind"] == "executable_catalog"
    )
    return {
        "root": root,
        "native": native_catalog,
        "lock_bytes": lock_bytes,
        "entries": entries,
        "reads": tuple(reads),
        "expected_calls": tuple(expected_calls),
        "entry_bytes": tuple(raw for _, raw in reads[1 : 1 + len(entries)]),
    }


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _different_digest(value: str) -> str:
    last = "0" if value[-1] != "0" else "1"
    return value[:-1] + last


def _lock_bytes(entries: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        {"schema": "io.tobkiri.defaultspack-bundle-lock.v1", "entries": entries},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _captured_reader(
    entries: list[dict[str, Any]],
    entry_bytes: list[bytes],
    calls: list[tuple[int, str, bool]],
):
    """Build a reader keyed only by lock-entry occurrence, never by path."""

    def read(index: int, original_relative: str, reread: bool) -> bytes:
        assert original_relative == entries[index]["path"]
        calls.append((index, original_relative, reread))
        return entry_bytes[index]

    return read


def _executable_pair(
    capture: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[bytes], int]:
    """Return an authentic executable catalog and its authentic Pack manifest."""

    entries = copy.deepcopy(capture["entries"])
    entry_bytes = list(capture["entry_bytes"])
    executable_index = next(
        index
        for index, entry in enumerate(entries)
        if entry["kind"] == "executable_catalog"
    )
    executable = bundle_catalog.strict_loads(entry_bytes[executable_index])
    pack_id = executable["pack_id"]
    pack_index = next(
        index
        for index, entry in enumerate(entries)
        if entry["kind"] == "pack"
        and bundle_catalog.strict_loads(entry_bytes[index])["pack"]["id"] == pack_id
    )
    selected = sorted((executable_index, pack_index))
    selected_entries = [entries[index] for index in selected]
    selected_bytes = [entry_bytes[index] for index in selected]
    selected_executable_index = selected.index(executable_index)
    return selected_entries, selected_bytes, selected_executable_index


def test_native_and_data_reader_catalogs_match_and_preserve_read_occurrences(
    packaged_capture: dict[str, Any],
) -> None:
    native = packaged_capture["native"]
    entries = packaged_capture["entries"]
    reads = packaged_capture["reads"]
    expected_calls = packaged_capture["expected_calls"]

    expected_paths = [native.root / "bundle.lock.json"]
    expected_paths.extend(native.root / relative for _, relative, _ in expected_calls)
    assert [path for path, _ in reads] == expected_paths
    assert len(reads) == len(expected_calls) + 1

    calls: list[tuple[int, str, bool]] = []
    read_position = 0

    def read_recorded(index: int, original_relative: str, reread: bool) -> bytes:
        nonlocal read_position
        occurrence = (index, original_relative, reread)
        assert occurrence == expected_calls[read_position]
        calls.append(occurrence)
        read_position += 1
        return reads[read_position][1]

    data_catalog = BundledCatalog.load_from_reader(
        reads[0][1],
        entry_reader=read_recorded,
        root=native.root,
        artifact_root_resolver=lambda: native.artifact_root,
    )

    assert type(data_catalog) is BundledCatalog
    assert type(native) is BundledCatalog
    assert calls == list(expected_calls)
    assert read_position == len(reads) - 1
    assert data_catalog.root == native.root
    assert data_catalog.artifact_root == native.artifact_root
    assert data_catalog.packs == native.packs
    assert data_catalog.bases == native.bases
    assert data_catalog.shells == native.shells
    assert data_catalog.profiles == native.profiles
    assert data_catalog.executable_catalogs == native.executable_catalogs
    assert all(entry["path"] for entry in entries)


def test_successful_data_load_retains_non_filesystem_presentation_views(
    packaged_capture: dict[str, Any],
) -> None:
    source_index = next(
        index
        for index, entry in enumerate(packaged_capture["entries"])
        if entry["kind"] == "pack"
    )
    source_entry = copy.deepcopy(packaged_capture["entries"][source_index])
    raw = packaged_capture["entry_bytes"][source_index]
    entries = [source_entry]
    calls: list[tuple[int, str, bool]] = []

    class PresentationRoot:
        def __fspath__(self) -> str:
            raise AssertionError("DATA validation must not convert root to an OS path")

    root_view = PresentationRoot()
    artifact_root_view = object()
    resolver_calls: list[bool] = []

    def resolve_artifact_root() -> object:
        resolver_calls.append(True)
        return artifact_root_view

    catalog = BundledCatalog.load_from_reader(
        _lock_bytes(entries),
        entry_reader=_captured_reader(entries, [raw], calls),
        root=root_view,
        artifact_root_resolver=resolve_artifact_root,
    )

    assert type(catalog) is BundledCatalog
    assert catalog.root is root_view
    assert catalog.artifact_root is artifact_root_view
    assert resolver_calls == [True]
    assert calls == [(0, source_entry["path"], False)]


def test_captured_catalog_load_and_complete_original_setup_need_no_os_io(
    packaged_capture: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Include catalog parsing in the unchanged original full Setup comparison."""
    from tests.test_captured_setup_projection import _capture, _projections, _resolve
    from tobkiri_protocol import validation

    native = packaged_capture["native"]
    expected = _projections(native, _resolve(native))
    capture = _capture(native)
    # The native side selects each occurrence before entering isolated DATA
    # evaluation. The reader never rediscovers a host or captured path from
    # caller-provided path text. These are test input bindings, not OS custody.
    selected = tuple(
        (
            entry["path"],
            capture / "bundle" / (native.root / entry["path"])
            .resolve(strict=True).relative_to(native.root),
        )
        for entry in packaged_capture["entries"]
    )
    calls: list[tuple[int, str, bool]] = []

    def read(index: int, relative: str, reread: bool) -> bytes:
        original_relative, path = selected[index]
        assert relative == original_relative
        calls.append((index, relative, reread))
        return path.read_bytes()

    def no_external_io(*args: Any, **kwargs: Any) -> None:
        pytest.fail("captured catalog or Setup computation attempted external I/O")

    validation.load_schema.cache_clear()
    validation._schema_store.cache_clear()
    # Empty validation caches ensure schemas are actually read from capture.
    bundle_catalog._validated_document.cache_clear()
    bundle_catalog._clear_large_validation_cache()
    try:
        with monkeypatch.context() as isolated:
            isolated.setattr(validation, "SCHEMA_DIR", capture / "schemas")
            isolated.setattr(Path, "read_bytes", no_external_io)
            for target in (
                "os.open", "builtins.open", "io.open", "subprocess.run",
                "sqlite3.connect", "urllib.request.urlopen", "socket.socket",
            ):
                isolated.setattr(target, no_external_io)
            catalog = BundledCatalog.load_from_reader(
                (capture / "bundle" / "bundle.lock.json").read_bytes(),
                entry_reader=read,
                root=capture / "bundle",
                artifact_root_resolver=lambda: capture / "artifacts",
            )
            actual = _projections(catalog, _resolve(catalog))
            assert actual == expected
            assert calls == list(packaged_capture["expected_calls"])
    finally:
        validation.load_schema.cache_clear()
        validation._schema_store.cache_clear()
        bundle_catalog._validated_document.cache_clear()
        bundle_catalog._clear_large_validation_cache()


def test_reader_keeps_aliased_entry_occurrences_distinct(
    packaged_capture: dict[str, Any],
) -> None:
    source_entry = next(
        entry for entry in packaged_capture["entries"] if entry["kind"] == "pack"
    )
    source_index = packaged_capture["entries"].index(source_entry)
    raw = packaged_capture["entry_bytes"][source_index]
    relative = source_entry["path"]
    alias = str(Path("packs") / ".." / relative)
    entries = [
        {"path": relative, "kind": "pack", "digest": _digest(raw)},
        {"path": alias, "kind": "pack", "digest": _digest(raw)},
    ]
    entry_bytes = [raw, raw]
    calls: list[tuple[int, str, bool]] = []
    root_view = object()

    with pytest.raises(BundleIntegrityError, match="duplicate or missing pack identity"):
        BundledCatalog.load_from_reader(
            _lock_bytes(entries),
            entry_reader=_captured_reader(entries, entry_bytes, calls),
            root=root_view,
            artifact_root_resolver=lambda: pytest.fail("resolution must be deferred"),
        )

    assert calls == [(0, relative, False), (1, alias, False)]


def test_earlier_digest_failure_precedes_later_reader_error_and_artifact_resolution(
    packaged_capture: dict[str, Any],
) -> None:
    entries = copy.deepcopy(packaged_capture["entries"][:2])
    entry_bytes = list(packaged_capture["entry_bytes"][:2])
    entries[0]["digest"] = _different_digest(entries[0]["digest"])
    calls: list[tuple[int, str, bool]] = []
    resolver_calls: list[bool] = []

    def read(index: int, original_relative: str, reread: bool) -> bytes:
        calls.append((index, original_relative, reread))
        if index == 1:
            raise OSError("later ordinal read failed")
        return entry_bytes[index]

    def resolve_artifact_root() -> None:
        resolver_calls.append(True)
        return None

    with pytest.raises(BundleIntegrityError, match="bundle artifact digest changed"):
        BundledCatalog.load_from_reader(
            _lock_bytes(entries),
            entry_reader=read,
            root=object(),
            artifact_root_resolver=resolve_artifact_root,
        )

    assert calls == [(0, entries[0]["path"], False)]
    assert resolver_calls == []


def test_schema_validation_still_runs_after_the_entry_digest_check(
    packaged_capture: dict[str, Any],
) -> None:
    entries = copy.deepcopy(packaged_capture["entries"])
    entry_bytes = list(packaged_capture["entry_bytes"])
    entry_bytes[0] = b"{}"
    entries[0]["digest"] = _digest(entry_bytes[0])
    calls: list[tuple[int, str, bool]] = []

    with pytest.raises(BundleIntegrityError, match=f"invalid {entries[0]['kind']} document"):
        BundledCatalog.load_from_reader(
            _lock_bytes(entries),
            entry_reader=_captured_reader(entries, entry_bytes, calls),
            root=object(),
            artifact_root_resolver=lambda: pytest.fail("resolution must be deferred"),
        )

    assert calls == [(0, entries[0]["path"], False)]


def test_base_or_shell_revision_validation_is_retained(
    packaged_capture: dict[str, Any],
) -> None:
    entries = copy.deepcopy(packaged_capture["entries"])
    entry_bytes = list(packaged_capture["entry_bytes"])
    index = next(
        position
        for position, entry in enumerate(entries)
        if entry["kind"] in {"base", "shell"}
    )
    document = bundle_catalog.strict_loads(entry_bytes[index])
    document["definition_revision"] = _different_digest(document["definition_revision"])
    entry_bytes[index] = json.dumps(
        document, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    entries[index]["digest"] = _digest(entry_bytes[index])
    calls: list[tuple[int, str, bool]] = []

    with pytest.raises(BundleIntegrityError, match="definition revision is stale or tampered"):
        BundledCatalog.load_from_reader(
            _lock_bytes(entries),
            entry_reader=_captured_reader(entries, entry_bytes, calls),
            root=object(),
            artifact_root_resolver=lambda: pytest.fail("resolution must be deferred"),
        )

    assert calls == [
        (position, entries[position]["path"], False)
        for position in range(index + 1)
    ]


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("source_identity", "executable catalog source identity is stale"),
        ("catalog_digest", "executable catalog digest is stale"),
        ("artifact_pin", "Pack executable catalog artifact pin is stale"),
    ],
)
def test_executable_catalog_identity_digest_and_manifest_pin_checks_are_retained(
    packaged_capture: dict[str, Any],
    corruption: str,
    message: str,
) -> None:
    entries, entry_bytes, executable_index = _executable_pair(packaged_capture)
    executable = bundle_catalog.strict_loads(entry_bytes[executable_index])

    if corruption == "source_identity":
        executable["source_identity"] = _different_digest(executable["source_identity"])
    elif corruption == "catalog_digest":
        executable["catalog_digest"] = _different_digest(executable["catalog_digest"])
    else:
        executable["materialization_catalog_digest"] = _different_digest(
            executable["materialization_catalog_digest"]
        )
        executable["catalog_digest"] = bundle_catalog.canonical_digest(
            {key: value for key, value in executable.items() if key != "catalog_digest"}
        )

    entry_bytes[executable_index] = json.dumps(
        executable, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    entries[executable_index]["digest"] = _digest(entry_bytes[executable_index])
    calls: list[tuple[int, str, bool]] = []

    with pytest.raises(BundleIntegrityError, match=message):
        BundledCatalog.load_from_reader(
            _lock_bytes(entries),
            entry_reader=_captured_reader(entries, entry_bytes, calls),
            root=object(),
            artifact_root_resolver=lambda: pytest.fail("resolution must be deferred"),
        )

    if corruption == "artifact_pin":
        assert calls[-1] == (executable_index, entries[executable_index]["path"], True)
    else:
        assert all(not reread for _, _, reread in calls)
