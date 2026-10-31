"""Fresh original publication-prelude fixtures; execution is CI-only.

This observes the actual original recovery body until it would construct the
verified pointer store. A test-only sentinel stops that not-yet-ported suffix.
NoJournal executes the two original recovery calls before the catalog boundary.
This is neither full recovery nor the original Setup review/listing execution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
from types import SimpleNamespace

from original_authority_reference import ORIGINAL_HEAD, error_graph, write_json
from original_setup_review_reference import check_original


FORMAT = "io.tobkiri.original-publication-prelude-fixtures.v1"
CASES = (
    "absent", "profiles-file", "profiles-without-intent", "valid", "valid-predecessor",
    "intent-disappears-after-lock", "intent-directory", "intent-dangling-symlink",
    "intent-symlink", "profiles-symlink", "profiles-broad-mode",
    "json-empty", "json-invalid-utf8", "json-duplicate", "json-float", "json-large-integer",
    "key-missing", "key-before-envelope-shape", "envelope-shape", "payload-schema-before-key",
    "key-empty", "key-short", "key-invalid-utf8", "key-broad-mode", "key-directory",
    "key-symlink", "key-dangling-symlink", "key-unicode-short", "key-unicode-valid",
    "key-unicode-whitespace", "hmac-wrong", "hmac-uppercase", "hmac-nonascii", "hmac-empty",
    "predecessor-shape", "predecessor-nonmapping", "candidate-not-validated-here",
)
SOURCES = (
    "core_runtime/bootstrap/profile_capture.py", "core_runtime/bootstrap/profile_publication.py",
    "core_runtime/active_profile_store_v4.py", "core_runtime/hmac_key_manager.py",
    "tobkiri_protocol/canonical.py", "tobkiri_protocol/secure_persistence.py",
)


class PointerBoundary(BaseException):
    """An explicit observation boundary, never a projected original error."""


def filesystem(root: Path) -> dict:
    """Record only fixed fixture effects, never key contents or executable data."""
    if not root.exists():
        return {"exists": False, "files": []}
    rows, pending = [], [root]
    while pending:
        path = pending.pop()
        if len(rows) >= 128:
            raise ValueError("publication fixture observation exceeds file bound")
        info = path.lstat()
        kind = ("directory" if stat.S_ISDIR(info.st_mode) else
                "file" if stat.S_ISREG(info.st_mode) else
                "symlink" if stat.S_ISLNK(info.st_mode) else "other")
        row = {"path": "." if path == root else path.relative_to(root).as_posix(),
               "kind": kind, "mode": stat.S_IMODE(info.st_mode)}
        if kind == "symlink":
            row["target"] = os.readlink(path)
        elif kind == "file":
            if info.st_size > 4 * 1024 * 1024:
                raise ValueError("publication fixture file exceeds observation bound")
            row["bytes"] = info.st_size
            if path.name not in {".publication-key", "original-key"}:
                row["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif kind == "directory":
            pending.extend(sorted(path.iterdir(), reverse=True))
        rows.append(row)
    return {"exists": True, "files": rows}


def seed(name: str, root: Path, journal_type, pointer_type, protocol, signing) -> None:
    """Build inputs with the real original writer; mutate only the named boundary."""
    if name == "absent":
        return
    root.mkdir(mode=0o700)
    if name == "profiles-file":
        (root / "profiles").write_bytes(b"not a directory\n")
        return
    profiles = root / "profiles"
    profiles.mkdir(mode=0o700)
    if name == "profiles-without-intent":
        return
    resolved = SimpleNamespace(
        profile={"profile_id": "defaults"},
        plan={"profile_revision": "sha256:" + "1" * 64,
              "plan_digest": "sha256:" + "2" * 64,
              "profile_definition_digest": "sha256:" + "3" * 64, "security_epoch": 1},
        lock={"lock_digest": "sha256:" + "4" * 64},
    )
    journal = journal_type(root)
    journal.begin(predecessor=None, resolved=resolved,
                  activation_id="activation:publication-fixture", confirmation={"fixture": True})
    intent, key = journal.path, profiles / ".publication-key"
    if name == "intent-directory":
        intent.unlink(); intent.mkdir()
    elif name in {"intent-dangling-symlink", "intent-symlink"}:
        intent.rename(profiles / "original-intent")
        intent.symlink_to("missing-intent" if "dangling" in name else "original-intent")
    elif name == "profiles-symlink":
        profiles.rename(root / "original-profiles"); profiles.symlink_to("original-profiles")
    elif name == "profiles-broad-mode":
        profiles.chmod(0o777)
    elif name.startswith("json-"):
        contents = {
            "json-empty": b"", "json-invalid-utf8": b"\xff",
            "json-duplicate": b'{"a":1,"a":2}', "json-float": b'{"a":1.5}',
            "json-large-integer": b'{"a":9007199254740992}',
        }
        intent.write_bytes(contents[name])
    elif name in {"key-missing", "key-before-envelope-shape"}:
        key.unlink()
        if name == "key-before-envelope-shape":
            intent.write_bytes(b"null")
    elif name == "envelope-shape":
        intent.write_bytes(b"null")
    elif name in {"key-empty", "key-short", "key-invalid-utf8", "key-unicode-short"}:
        key.write_bytes({"key-empty": b"", "key-short": b"a" * 31,
                         "key-invalid-utf8": b"\xff",
                         "key-unicode-short": ("\u754c" * 31).encode()}[name])
    elif name == "key-broad-mode":
        key.chmod(0o644)
    elif name == "key-directory":
        key.unlink(); key.mkdir()
    elif name in {"key-symlink", "key-dangling-symlink"}:
        key.rename(profiles / "original-key")
        key.symlink_to("missing-key" if "dangling" in name else "original-key")
    else:
        envelope = protocol.strict_loads(intent.read_bytes())
        if name == "valid-predecessor":
            predecessor = pointer_type(
                profile_id="defaults", profile_revision="sha256:" + "5" * 64,
                activation_id="activation:publication-before", plan_digest="sha256:" + "6" * 64,
                lock_digest="sha256:" + "7" * 64,
                activation_snapshot_path="workspaces/defaults/activation/activations/publication-before.json",
                activation_snapshot_digest="sha256:" + "8" * 64,
                catalog_revision=None, generation=1, updated_at=1000,
            )
            envelope["payload"]["predecessor"] = predecessor.to_dict()
        elif name == "payload-schema-before-key":
            envelope["payload"]["schema"] = "unsupported"
            key.write_bytes(b"weak")
        elif name == "predecessor-shape":
            envelope["payload"]["predecessor"] = {}
        elif name == "predecessor-nonmapping":
            envelope["payload"]["predecessor"] = 42
        elif name == "candidate-not-validated-here":
            envelope["payload"]["candidate"] = [1, True, None]
            envelope["payload"]["confirmation_digest"] = None
        elif name == "key-unicode-valid":
            key.write_bytes(("\u754c" * 32).encode())
        elif name == "key-unicode-whitespace":
            key.write_bytes(("\u2003\x1c" + "a" * 64 + "\x1f\u2003").encode())
        # The short-circuit schema fixture deliberately has an unreadable key;
        # its signature is irrelevant before key loading is reached.
        if name != "payload-schema-before-key":
            envelope["hmac"] = signing.compute_data_hmac(
                signing.load_signing_key(key), envelope["payload"])
        if name.startswith("hmac-"):
            envelope["hmac"] = {"hmac-wrong": "0" * 64,
                                "hmac-uppercase": envelope["hmac"].upper(),
                                "hmac-nonascii": "\u00e9", "hmac-empty": ""}[name]
        intent.write_bytes(protocol.canonical_json(envelope) + b"\n")


def observe(root: Path, name: str, capture, journal_type) -> dict:
    """Run real original recovery IO, stopping before the unimplemented suffix."""
    original_pointer, original_read = capture.ActiveProfileStore, journal_type.read
    previous = {key: os.environ.get(key) for key in ("TOBKIRI_USER_DATA", "RUMI_USER_DATA")}

    def stop_pointer(*args, **kwargs):
        raise PointerBoundary

    def disappearing_read(journal):
        journal.path.unlink()
        return original_read(journal)

    try:
        capture.ActiveProfileStore = stop_pointer
        if name == "intent-disappears-after-lock":
            journal_type.read = disappearing_read
        for key in previous:
            os.environ[key] = str(root)
        for discard in (True, False):
            capture._recover_bootstrap_publication(discard_superseded=discard)
        return {"outcome": "integration-boundary", "stage": "catalog-not-bound"}
    except PointerBoundary:
        return {"outcome": "integration-boundary", "stage": "verified-pointer-not-bound"}
    except BaseException as error:
        return {"outcome": "error", "error": error_graph(error, root)}
    finally:
        capture.ActiveProfileStore, journal_type.read = original_pointer, original_read
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def prepare(original: Path, destination: Path) -> None:
    """Keep DB/key fixtures only in the CI temporary directory, never artifacts."""
    check_original(original)
    runtime = original / "tobkiri_runtime"
    sys.path.insert(0, str(runtime))
    from core_runtime.bootstrap import profile_capture as capture
    from core_runtime.bootstrap.profile_publication import ProfilePublicationJournal
    from core_runtime.active_profile_store_v4 import ActiveProfilePointer
    from core_runtime import hmac_key_manager as signing
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import install_defaultspack_profile_runtime
    from tobkiri_protocol import canonical as protocol

    install_defaultspack_profile_runtime()
    if Path(capture.__file__).resolve() != (runtime / SOURCES[0]).resolve():
        raise ValueError("original recovery source was substituted")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    reports = []
    for name in CASES:
        directory = destination / name
        directory.mkdir(mode=0o700)
        seed_root = directory / "seed"
        seed(name, seed_root, ProfilePublicationJournal, ActiveProfilePointer, protocol, signing)
        for target in ("reference", "native"):
            if seed_root.exists():
                shutil.copytree(seed_root, directory / target, symlinks=True)
        reference, native = directory / "reference", directory / "native"
        result = observe(reference, name, capture, ProfilePublicationJournal)
        reports.append({"name": name, "user_data_dir": str(native),
                        "after_lock_remove_intent": name == "intent-disappears-after-lock",
                        "expected": result, "reference_state": filesystem(reference)})
    metadata = {"format": FORMAT, "original_head": ORIGINAL_HEAD, "python": "3.13.16",
                "sources": {name: hashlib.sha256((runtime / name).read_bytes()).hexdigest()
                            for name in SOURCES}, "cases": reports,
                "native_comparison": False, "qualification": False, "production_enabled": False,
                "scope": "original publication prelude only; pointer construction and later recovery excluded",
                "pointer_boundary_injected": True, "reference_checks_passed": False}
    write_json(destination / "fixtures.json", metadata)
    summary = {key: value for key, value in metadata.items() if key != "cases"}
    summary["cases"] = [{"name": row["name"], "observation": row["expected"],
                         "reference_state": row["reference_state"]} for row in reports]
    write_json(destination / "summary.json", summary)
    if len(reports) != len(CASES) or len({row["name"] for row in reports}) != len(CASES):
        raise AssertionError("exact original publication case inventory required")
    catalog = {"absent", "profiles-file", "profiles-without-intent", "intent-disappears-after-lock"}
    pointer = {"valid", "valid-predecessor", "profiles-broad-mode", "key-unicode-valid",
               "key-unicode-whitespace", "candidate-not-validated-here"}
    for row in reports:
        expected = ("catalog-not-bound" if row["name"] in catalog else
                    "verified-pointer-not-bound" if row["name"] in pointer else None)
        if expected is None:
            if row["expected"]["outcome"] != "error":
                raise AssertionError((row["name"], "original typed failure required"))
        elif row["expected"] != {"outcome": "integration-boundary", "stage": expected}:
            raise AssertionError((row["name"], "original IO reachpoint differs", row["expected"]))
        if any(path["path"].split("/")[0] == "authority"
               for path in row["reference_state"]["files"]):
            raise AssertionError("prelude observation crossed the old Authority constructor")
    summary["reference_checks_passed"] = True
    metadata["reference_checks_passed"] = True
    write_json(destination / "fixtures.json", metadata)
    write_json(destination / "summary.json", summary)
    print(json.dumps({"original_cases": len(reports), "native_comparison": False,
                      "qualification": False, "production_enabled": False}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.original_root.resolve(strict=True), args.output.resolve())


if __name__ == "__main__":
    main()
