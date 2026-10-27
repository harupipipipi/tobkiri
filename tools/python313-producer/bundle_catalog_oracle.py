"""Observe the complete genuine source bundle using the pinned original loader.

Only Linux CI imports the original runtime. This source DATA comparison is not
installed-artifact admission, filesystem custody, active Profile or GUI proof.
"""

import argparse
import base64
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys

ORACLE_COMMIT = "54e7da008997a3eb45876cbe18459dba9902cc9d"
FORMAT = "tobkiri.bundle-catalog-comparison/1"
MAX_BYTES = 4 * 1024 * 1024
SOURCE_HASHES = {
    "__init__.py": "fe5cc79898dfbc1154f11f01a498e32eb7566dfe527769069acd4dc88b63b424",
    "bundle_catalog.py": "b52df470ea9fb275bda11c2361469251aa0572f9a24df9147f59cad753676ecd",
    "validation.py": "91b382f8c2494da7368c56b6bd55a6200d29e1db797a76dd501f3fcdd6f5c9bb",
    "canonical.py": "9f068ab737e2c9c152348601d604c6c81a060edf207c483849a90ca97ccd434e",
    "errors.py": "2292de2b3fd27e3fe644979d7aee20a40e4b77b2366dd4c5ac3ad3a449acbddf",
    "ids.py": "833ff21e644c69e6693244d81331e5afc8f6ed5e15f8a1cdda866274c05eb15c",
}
LOCK_HASH = "3343eba47526007619eec75bb6d912e76d35f42509a54712d9a47409208cd7f0"
COLLECTIONS = ("packs", "bases", "shells", "profiles", "executable_catalogs")


def encoded(raw: bytes) -> str:
    """Serialize exact bytes without changing their original content."""
    return base64.b64encode(raw).decode("ascii")


def text(value: object) -> str:
    """Use the same finite UTF-8 serialization as the App adapter."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def run(source: Path, output: Path) -> None:
    """Require authenticated source before invoking its unchanged native loader."""
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("bundle catalog oracle is restricted to Linux GitHub Actions")
    if (
        not source.is_absolute()
        or source.resolve(strict=True) != source
        or not output.is_absolute()
        or output.suffix != ".json"
        or output.parent.resolve(strict=True) != output.parent
        or os.path.lexists(output)
        or source in output.parents
    ):
        raise ValueError("physical source and new external JSON output required")
    for name, expected in SOURCE_HASHES.items():
        path = source / "tobkiri_protocol" / name
        if (
            path.resolve(strict=True) != path
            or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise ValueError("pinned original source differs: " + name)
    if any(
        name == "tobkiri_protocol" or name.startswith("tobkiri_protocol.")
        for name in sys.modules
    ):
        raise ValueError("original protocol was imported before source checks")
    schemas = {}
    for path in sorted((source / "tobkiri_protocol/schemas").glob("*.schema.json")):
        if path.resolve(strict=True) != path or not path.is_file():
            raise ValueError("original schema resource is not a physical file")
        schemas[path.relative_to(source).as_posix()] = path.read_bytes()
    if not schemas:
        raise ValueError("original package schema resources are missing")
    root = source / "ecosystem/defaultspack/v4"
    lock = (root / "bundle.lock.json").read_bytes()
    if hashlib.sha256(lock).hexdigest() != LOCK_HASH:
        raise ValueError("complete original bundle lock changed")
    sys.path.insert(0, str(source))
    module = importlib.import_module("tobkiri_protocol.bundle_catalog")
    if (
        Path(module.__file__).resolve(strict=True)
        != source / "tobkiri_protocol/bundle_catalog.py"
    ):
        raise ValueError("original module escaped the pinned checkout")
    original = module.BundledCatalog.load(root)
    entries = json.loads(lock)["entries"]
    if len(entries) != 305:
        raise ValueError("full original inventory was narrowed or changed")
    files = {entry["path"]: (root / entry["path"]).read_bytes() for entry in entries}
    reads = []
    blobs = []
    ordinals = {}

    def reader(index: int, path: str, reread: bool) -> bytes:
        raw = files[path]
        # bytes dictionary equality, not a digest assertion, decides sharing.
        if raw not in ordinals:
            ordinals[raw] = len(blobs)
            blobs.append(encoded(raw))
        reads.append(
            {"index": index, "path": path, "reread": reread, "blob": ordinals[raw]}
        )
        return raw

    captured = module.BundledCatalog.load_from_reader(
        lock,
        entry_reader=reader,
        root=root,
        artifact_root_resolver=lambda: original.artifact_root,
    )
    expected = text({name: getattr(original, name) for name in COLLECTIONS})
    if text({name: getattr(captured, name) for name in COLLECTIONS}) != expected:
        raise ValueError("captured reads differ from the original native path loader")
    carrier = {
        "format": "tobkiri.bundle-defaults/2",
        "lock_base64": encoded(lock),
        "blobs": blobs,
        "reads": reads,
    }
    payload = text(carrier)
    source_identity = {
        "format": "tobkiri.bundle-source-bytes/1",
        "lock": {"bytes": len(lock), "sha256": hashlib.sha256(lock).hexdigest()},
        "reads": [
            {
                "index": row["index"],
                "path": row["path"],
                "reread": row["reread"],
                "bytes": len(files[row["path"]]),
                "sha256": hashlib.sha256(files[row["path"]]).hexdigest(),
            }
            for row in reads
        ],
    }
    if len(payload.encode()) > MAX_BYTES or len(expected.encode()) > MAX_BYTES:
        raise ValueError("complete original DATA exceeds the unchanged envelope")
    occurrence = next(i for i, row in enumerate(reads) if row["reread"])
    position = 0

    def changed_reread(index: int, path: str, reread: bool) -> bytes:
        nonlocal position
        row = reads[position]
        if (index, path, reread) != (row["index"], row["path"], row["reread"]):
            raise ValueError("original reread occurrence order changed")
        current = position
        position += 1
        return b"{}" if current == occurrence else files[path]

    try:
        module.BundledCatalog.load_from_reader(
            lock,
            entry_reader=changed_reread,
            root=root,
            artifact_root_resolver=lambda: original.artifact_root,
        )
    except module.BundleIntegrityError as error:
        reread_error = str(error)
    else:
        raise ValueError("original loader accepted different executable reread bytes")
    # Check every physical input again; this is still a CI source observation,
    # not a product capture/currentness receipt.
    if (
        (root / "bundle.lock.json").read_bytes() != lock
        or any((root / path).read_bytes() != raw for path, raw in files.items())
        or any((source / path).read_bytes() != raw for path, raw in schemas.items())
    ):
        raise ValueError("original bundle changed during observation")
    result = {
        "format": FORMAT,
        "oracle_commit": ORACLE_COMMIT,
        "payload": payload,
        "expected_sha256": hashlib.sha256(expected.encode()).hexdigest(),
        "expected_bytes": len(expected.encode()),
        "entries": len(entries),
        "read_occurrences": len(reads),
        "source_identity": source_identity,
        "source_identity_sha256": hashlib.sha256(
            json.dumps(
                source_identity,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "collections": {name: len(getattr(original, name)) for name in COLLECTIONS},
        "reread_mutation": {
            "occurrence": occurrence,
            "bytes_base64": encoded(b"{}"),
            "expected_error": reread_error,
        },
        "installed_artifact_or_gui_proof": False,
        "schema_resources": {
            name: {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            for name, raw in schemas.items()
        },
    }
    raw = (text(result) + "\n").encode()
    if len(raw) > MAX_BYTES:
        raise ValueError("original comparison input exceeds four MiB")
    with output.open("xb") as stream:
        stream.write(raw)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    run(**vars(parser.parse_args()))
