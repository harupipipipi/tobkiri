from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core_runtime.pack_trust import is_pack_trusted


TRUSTED_BUILTIN_PROMPT_PACK_IDS = {
    "defaultspack",
    "rumi_default_tools_pack",
    "rumi_operations_company_pack",
}


def _has_matching_pack_manifest(pack_root: Path, pack_id: str) -> bool:
    candidates = (
        pack_root / "pack.v4.json",
        pack_root / "v4" / "packs" / f"{pack_id}.pack.v4.json",
    )
    for manifest_path in candidates:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            declared = str((manifest.get("pack") or {}).get("id") or "").strip()
        except (OSError, UnicodeError, ValueError, TypeError):
            continue
        if declared == pack_id:
            return True
    return False


def _bundled_prompt_pack_root(pack_id: str) -> Path | None:
    if pack_id == "defaultspack":
        pack_root = Path(__file__).resolve().parents[2]
        return pack_root if _has_matching_pack_manifest(pack_root, pack_id) else None
    ecosystem_root = Path(__file__).resolve().parents[3]
    pack_root = ecosystem_root / pack_id
    return (
        pack_root
        if pack_root.is_dir() and _has_matching_pack_manifest(pack_root, pack_id)
        else None
    )


def _source_path_within_pack(source_path: str | Path | None, pack_root: Path | None) -> bool:
    if pack_root is None:
        return False
    if source_path in (None, ""):
        return True
    if not isinstance(source_path, (str, Path)):
        return False
    try:
        Path(source_path).resolve().relative_to(pack_root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _source_pack_root(source_path: str | Path, pack_id: str) -> Path | None:
    """Find the declared pack root that owns a prompt source file.

    Approved external packs may be installed outside the bundled ecosystem,
    so their root cannot be inferred from the defaultspack directory. The
    source path must still sit below a pack manifest that declares the same
    pack id; approval alone must not turn an arbitrary path into prompt text.
    """
    try:
        source = Path(source_path).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    start = source if source.is_dir() else source.parent
    normalized = str(pack_id or "").strip()
    for candidate in (start, *start.parents):
        manifest_path = candidate / "pack.v4.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        declared = str((manifest.get("pack") or {}).get("id") or "").strip()
        if declared == normalized and _source_path_within_pack(source, candidate):
            return candidate
    return None


def is_trusted_prompt_pack(pack_id: str, approval_manager: Any = None) -> tuple[bool, str | None]:
    normalized = str(pack_id or "").strip()
    if _selected_projection_root(normalized) is not None:
        return True, None
    if normalized in TRUSTED_BUILTIN_PROMPT_PACK_IDS and _bundled_prompt_pack_root(normalized) is not None:
        return True, None
    return is_pack_trusted(pack_id, approval_manager=approval_manager)


def prompt_pack_is_trusted(pack_id: str, approval_manager: Any = None) -> bool:
    trusted, _reason = is_trusted_prompt_pack(pack_id, approval_manager=approval_manager)
    return trusted


def prompt_pack_source_is_trusted(
    pack_id: str,
    source_path: str | Path | None = None,
    approval_manager: Any = None,
) -> bool:
    normalized = str(pack_id or "").strip()
    projection_root = _selected_projection_root(normalized)
    if projection_root is not None:
        return _source_path_within_pack(source_path, projection_root)
    if normalized in TRUSTED_BUILTIN_PROMPT_PACK_IDS:
        return _source_path_within_pack(source_path, _bundled_prompt_pack_root(normalized))
    trusted, _reason = is_pack_trusted(normalized, approval_manager=approval_manager)
    if not trusted:
        return False
    if source_path in (None, ""):
        return True
    if not isinstance(source_path, (str, Path)):
        return False
    return _source_pack_root(source_path, normalized) is not None


def _selected_projection_root(projection_id: str) -> Path | None:
    """Resolve trust only from the captured Profile's digest-bound selection."""

    from core_runtime.profile_content_projection import selected_projection_roots
    from core_runtime.resolved_profile_scope import effective_profile_projections

    for selected_id, root in selected_projection_roots(
        effective_profile_projections(), kind="profile_content"
    ):
        if selected_id == projection_id:
            return root
    return None
