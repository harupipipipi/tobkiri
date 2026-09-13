"""Non-authoritative provenance for the legacy Profile compatibility projection.

The v4-named Profile remains a compatibility artifact while Profile intent and
lock artifacts are split.  Its provenance must describe the generator inputs
without becoming an activation or release authority.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.provenance import repository_tree_digest, sha256_file

PROVENANCE_SCHEMA = "io.tobkiri.provenance.v1"
_GENERATOR_RULE = "compatibility-projection-generator-bytes"
_INPUT_RULE = "compatibility-projection-input-bytes"


def compatibility_profile_payload(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Return the Profile content whose identity excludes its provenance shell."""

    return {key: value for key, value in profile.items() if key != "provenance"}


def compatibility_profile_provenance(
    *,
    root: Path,
    profile: Mapping[str, Any],
    source_path: str,
    generator: str,
    generator_version: str,
    generator_path: Path,
    input_paths: Iterable[Path] = (),
) -> dict[str, Any]:
    """Build v1 provenance for a non-authoritative compatibility projection.

    ``repository_tree`` deliberately uses the protocol's sorted
    path/digest-pair algorithm over the explicit generator closure.  The
    semantic Profile payload is bound separately by ``source_digest`` so the
    output does not become a recursive provenance input.
    """

    repository_root = root.resolve(strict=True)
    paths = _repository_inputs(repository_root, (generator_path, *input_paths))
    source_digest = canonical_digest(compatibility_profile_payload(profile))
    generator_resolved = generator_path.resolve(strict=True)
    evidence = [
        {
            "path": path.relative_to(repository_root).as_posix(),
            "rule_id": _GENERATOR_RULE if path == generator_resolved else _INPUT_RULE,
            "digest": sha256_file(path),
        }
        for path in paths
    ]
    return {
        "schema": PROVENANCE_SCHEMA,
        "source_kind": "generated",
        "source_path": source_path,
        "source_digest": source_digest,
        "repository_commit": "working-tree",
        "repository_tree": repository_tree_digest(repository_root, paths),
        "generator": generator,
        "generator_version": generator_version,
        "normative": False,
        "evidence": evidence,
    }


def validate_compatibility_profile(profile: Mapping[str, Any]) -> None:
    """Reject a compatibility Profile that attempts to gain normative authority."""

    provenance = profile.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError("compatibility Profile provenance must be an object")
    if provenance.get("schema") != PROVENANCE_SCHEMA:
        raise ValueError("compatibility Profile must use provenance v1")
    if (
        provenance.get("repository_commit") == "working-tree"
        and provenance.get("normative") is True
    ):
        raise ValueError(
            "compatibility Profile cannot claim normative provenance while "
            "sourced from a working tree"
        )
    if (
        profile.get("state") == "needs_resolution"
        and provenance.get("normative") is True
    ):
        raise ValueError(
            "compatibility Profile cannot claim normative provenance while unresolved"
        )
    if provenance.get("normative") is not False:
        raise ValueError("compatibility Profile provenance must be non-authoritative")
    if provenance.get("source_digest") != canonical_digest(
        compatibility_profile_payload(profile)
    ):
        raise ValueError("compatibility Profile provenance source digest is stale")


def _repository_inputs(root: Path, paths: Iterable[Path]) -> list[Path]:
    """Return a unique sorted list of real closure files inside ``root``."""

    selected: dict[str, Path] = {}
    for path in paths:
        resolved = path.resolve(strict=True)
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError(
                f"compatibility provenance input escapes repository root: {path}"
            ) from exc
        if not resolved.is_file():
            raise ValueError(f"compatibility provenance input is not a file: {path}")
        selected[relative] = resolved
    if not selected:
        raise ValueError("compatibility provenance requires generator inputs")
    return [selected[relative] for relative in sorted(selected)]
