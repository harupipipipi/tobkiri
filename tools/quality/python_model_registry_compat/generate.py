"""Replay bounded Model Registry reads and Settings from pinned Python source."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
SOURCE_COMMIT = "6818f8ef7fae92685794acceccf71924d6eafd6b"
SCHEMA = "io.tobkiri.original-python-model-registry-read.v1"
REGISTRY = "ecosystem.rumi_model_registry_pack.runtime.registry"
SETTINGS = "ecosystem.defaultspack.defaultspack.model_profile_presentation"
SOURCE_PATHS = (
    "core_runtime/__init__.py",
    "core_runtime/paths.py",
    "core_runtime/profile_workspace.py",
    "core_runtime/runtime_locks.py",
    "ecosystem/defaultspack/defaultspack/__init__.py",
    "ecosystem/defaultspack/defaultspack/model_profile_presentation.py",
    "ecosystem/rumi_model_registry_pack/runtime/__init__.py",
    "ecosystem/rumi_model_registry_pack/runtime/registry.py",
    "tobkiri_protocol/__init__.py",
    "tobkiri_protocol/canonical.py",
    "tobkiri_protocol/durability.py",
    "tobkiri_protocol/errors.py",
    "tobkiri_protocol/ids.py",
    "tobkiri_protocol/platform_paths.py",
    "tobkiri_protocol/secure_persistence.py",
    "tobkiri_protocol/windows_private_file.py",
)
PROFILE = "prof_model_fixture"
STORE_VERSION = "rumi.model-registry.store.v1"


def _case(
    case_id: str,
    action: str,
    document: Any,
    **arguments: Any,
) -> dict[str, Any]:
    return {
        "id": case_id,
        "profile_id": PROFILE,
        "document": document,
        "action": action,
        **arguments,
    }


def _cases() -> list[dict[str, Any]]:
    saved = {
        "version": STORE_VERSION,
        "profile_id": PROFILE,
        "revision": 29,
        "profiles": {
            "z-disabled": {
                "model_profile_id": "z-disabled",
                "display_name": "Disabled",
                "model_id": "example/retired",
                "enabled": False,
                "metadata": {"provider_connection_id": "example-retired"},
            },
            "m-legacy": {
                "model_profile_id": "m-legacy",
                "display_name": "Legacy route",
                "model_id": "example/legacy",
                "enabled": True,
                "requirements": {
                    "preferred_provider_instance_id": "example-legacy",
                },
                "metadata": {"provider_connection_id": ""},
                "credential_handle": "credential:synthetic-legacy-fixture",
            },
            "a-current": {
                "model_profile_id": "a-current",
                "display_name": "現在のモデル",
                "model_id": "example/current",
                "enabled": True,
                "requirements": {"preferred_provider_instance_id": "ignored-legacy"},
                "metadata": {
                    "provider_connection_id": "example-provider",
                    "private_marker": "synthetic-settings-fixture",
                },
                "parameters": {
                    "max_tokens": 131072,
                    "temperature": 0.125,
                    "signed_zero": -0.0,
                    "nested": [None, False, {"counter": 9007199254740993}],
                },
                "credential_handle": "opaque:synthetic-settings-fixture",
                "record_revision": 3,
            },
        },
        "aliases": {
            "z-retired": "z-disabled",
            "old": "m-legacy",
            "chain": "old",
            "broken": "gone",
            "a-current": "m-legacy",
        },
        "migration": None,
    }
    small = {**saved, "profiles": {}, "aliases": {}}
    current = saved["profiles"]["a-current"]
    return [
        _case("missing-store-snapshot", "snapshot", None),
        _case("missing-store-get", "get", None, identifier="gone"),
        _case("missing-store-resolve", "resolve", None, identifier="gone"),
        _case("missing-store-settings", "settings", None),
        _case("unsorted-store-snapshot", "snapshot", saved),
        _case("get-saved-values", "get", saved, identifier=" a-current "),
        _case("get-alias-miss", "get", saved, identifier="old"),
        _case("get-disabled-record", "get", saved, identifier="z-disabled"),
        _case("resolve-exact", "resolve", saved, identifier="m-legacy"),
        _case("resolve-alias", "resolve", saved, identifier=" old "),
        _case("resolve-alias-shadows-exact", "resolve", saved, identifier="a-current"),
        _case("resolve-alias-only-once", "resolve", saved, identifier="chain"),
        _case("resolve-dangling-alias", "resolve", saved, identifier="broken"),
        _case("resolve-disabled-alias", "resolve", saved, identifier="z-retired"),
        _case("resolve-absent-profile", "resolve", saved, identifier="gone"),
        _case("settings-saved-projection", "settings", saved),
        _case("numeric-identifier", "get", {
            **small, "profiles": {"17": current},
        }, identifier=17),
        _case("invalid-identifier", "resolve", small, identifier="not valid"),
        _case("wrong-store-version", "snapshot", {**small, "version": "unknown"}),
        _case("wrong-store-profile", "snapshot", {**small, "profile_id": "other"}),
        _case("wrong-store-container", "snapshot", {**small, "aliases": []}),
        _case("wrong-store-root", "snapshot", []),
        _case("missing-store-revision", "snapshot", {
            key: value for key, value in small.items() if key != "revision"
        }),
        _case("settings-invalid-cap", "settings", {
            **small, "profiles": {"a-current": {
                **current, "parameters": {"max_tokens": 32.0},
            }},
        }),
        _case("settings-invalid-enabled", "settings", {
            **small, "profiles": {"a-current": {**current, "enabled": "true"}},
        }),
    ]


def _stage_sources(runtime_root: Path) -> list[dict[str, str]]:
    sources = []
    for relative in SOURCE_PATHS:
        path = f"tobkiri_runtime/{relative}"
        content = subprocess.check_output(
            ["git", "cat-file", "blob", f"{SOURCE_COMMIT}:{path}"],
            cwd=ROOT,
            env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
            timeout=5,
        )
        destination = runtime_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        sources.append({"path": path, "sha256": hashlib.sha256(content).hexdigest()})
    return sources


def _observe(
    case: dict[str, Any],
    user_data_root: Path,
    registry_type: type,
    present_profiles: Any,
) -> dict[str, Any]:
    registry = registry_type(case["profile_id"], user_data_root=user_data_root)
    document = case["document"]
    before = None
    if document is not None:
        before = _json_bytes(document)
        registry.path.parent.mkdir(parents=True)
        registry.path.write_bytes(before)
    action = case["action"]
    try:
        if action == "settings":
            result = present_profiles(registry.snapshot())
        elif action == "snapshot":
            result = registry.snapshot()
        else:
            result = getattr(registry, action)(case["identifier"])
        observation = {"result": result}
    except (KeyError, ValueError) as error:
        observation = {"error": {"class": type(error).__name__, "message": str(error)}}
    after = registry.path.read_bytes() if registry.path.is_file() else None
    if after != before:
        raise RuntimeError(f"source read changed store in {case['id']}")
    return {**case, **observation}


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    ).encode("utf-8")


def _block_network(event: str, _arguments: tuple[Any, ...]) -> None:
    if event.startswith("socket."):
        raise RuntimeError(f"unexpected network operation from source: {event}")


def main() -> None:
    """Print the reproducible original Python fixture to standard output."""
    argparse.ArgumentParser(description=__doc__).parse_args()
    sys.dont_write_bytecode = True
    sys.addaudithook(_block_network)
    with tempfile.TemporaryDirectory(prefix="tobkiri-model-registry-") as temporary:
        root = Path(temporary)
        runtime_root = root / "tobkiri_runtime"
        sources = _stage_sources(runtime_root)
        os.environ["RUMI_USER_DATA"] = str(root / "default-user-data")
        sys.path.insert(0, str(runtime_root))
        registry_type = importlib.import_module(REGISTRY).ModelRegistry
        present_profiles = importlib.import_module(SETTINGS).present_model_profiles
        cases = [
            _observe(case, root / case["id"], registry_type, present_profiles)
            for case in _cases()
        ]
    fixture = {
        "schema": SCHEMA,
        "source_commit": SOURCE_COMMIT,
        "sources": sources,
        "cases": cases,
    }
    sys.stdout.buffer.write(_json_bytes(fixture))


if __name__ == "__main__":
    main()
