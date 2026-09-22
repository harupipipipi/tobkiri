"""Canonical ordering and path domains for the Defaultspack v4 bundle lock."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any, TypeVar


_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ENTRY_KEYS = frozenset({"path", "kind", "digest"})

# Pack documents and executable catalogs share one ``packs/`` namespace.  They
# therefore sort together by path, preserving the order emitted by the
# canonical generator when sidecars are interleaved with Pack manifests.
_KIND_GROUP = {
    "pack": 0,
    "executable_catalog": 0,
    "base": 1,
    "shell": 2,
    "profile": 3,
}
_PATH_SUFFIX = {
    "pack": ("packs/", ".pack.v4.json"),
    "executable_catalog": ("packs/", ".executables.v4.json"),
    "base": ("", ".base.v1.json"),
    "shell": ("", ".shell.v1.json"),
    "profile": ("", ".profile.v4.json"),
}

Entry = TypeVar("Entry", bound=Mapping[str, Any])


def defaultspack_bundle_entry_key(entry: Mapping[str, Any]) -> tuple[int, str]:
    """Return the canonical sort key after validating one lock entry.

    The path suffix and namespace are part of the v4 lock contract.  Keeping
    this validation beside the sort key prevents a verifier from accepting a
    path that happens to sort correctly but belongs to another document kind.
    """

    if set(entry) != _ENTRY_KEYS:
        raise ValueError("bundle lock entry must contain path, kind, and digest")
    kind = entry.get("kind")
    path = entry.get("path")
    digest = entry.get("digest")
    if not isinstance(kind, str) or kind not in _KIND_GROUP:
        raise ValueError(f"unsupported v4 bundle kind: {kind!r}")
    if not isinstance(path, str) or not path:
        raise ValueError("bundle lock path must be a non-empty string")
    normalized = PurePosixPath(path)
    if (
        normalized.is_absolute()
        or normalized.as_posix() != path
        or any(part in {"", ".", ".."} for part in normalized.parts)
    ):
        raise ValueError(f"bundle lock path is unsafe or not canonical: {path!r}")
    prefix, suffix = _PATH_SUFFIX[kind]
    if prefix and not path.startswith(prefix):
        raise ValueError(f"{kind} bundle path is outside {prefix}: {path!r}")
    if not prefix and path.startswith("packs/"):
        raise ValueError(f"{kind} bundle path is inside packs/: {path!r}")
    if not path.endswith(suffix):
        raise ValueError(f"{kind} bundle path has an unexpected suffix: {path!r}")
    if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
        raise ValueError(f"bundle lock digest is not canonical: {path!r}")
    return (_KIND_GROUP[kind], path)


def canonical_defaultspack_bundle_entries(
    entries: Sequence[Entry],
) -> list[Entry]:
    """Validate and return the deterministic v4 bundle lock entry order."""

    result = list(entries)
    paths: list[str] = []
    for entry in result:
        defaultspack_bundle_entry_key(entry)
        paths.append(str(entry["path"]))
    if len(set(paths)) != len(paths):
        raise ValueError("bundle lock paths must be unique")
    return sorted(result, key=defaultspack_bundle_entry_key)
