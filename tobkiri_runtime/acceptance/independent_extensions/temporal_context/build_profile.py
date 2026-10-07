"""Reproducible new named source release using the public Profile producer."""
from __future__ import annotations

from pathlib import Path

from author_profile import render_tauri_intent
from core_runtime.profile_authoring import build_named_profile

RUNTIME_ROOT = Path(__file__).resolve().parents[3]


def build_profile(output: Path, pack_root: Path, *,
                  temporal_pack_id: str = "acceptance.temporal.context",
                  profile_id: str = "acceptance.temporal.named") -> Path:
    """Compile a new Tauri source release; do not activate it or build its Shell."""
    return build_named_profile(
        output, template_bundle=RUNTIME_ROOT / "ecosystem/defaultspack/v4",
        intent=render_tauri_intent(profile_id=profile_id,
                                   temporal_pack_id=temporal_pack_id),
        additional_pack_roots=(pack_root,),
    )
