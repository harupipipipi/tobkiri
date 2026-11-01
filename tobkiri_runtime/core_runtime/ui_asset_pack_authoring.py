"""Offline authoring of signed UI delivery inputs, without runtime authority."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Mapping

from .pack_authoring import PackAuthoringError, _build_pack

_PUBLIC_SUFFIXES = frozenset({
    ".html", ".js", ".css", ".png", ".svg", ".wasm", ".task", ".txt", ".md",
    ".woff", ".woff2", ".ttf", ".ico", ".webp", ".jpg", ".jpeg", ".gif",
})


def build_ui_asset_pack(
    target: Path,
    *,
    pack_id: str,
    version: str,
    display_name: str,
    assets: Mapping[str, bytes],
) -> Path:
    """Compile a new unsigned inert Normal Pack from captured public UI bytes.

    This does not sign, admit, select, or authorize a renderer. The trusted
    application must separately delegate its UI to an exact authorized signed
    artifact. Delegated JavaScript inherits the application's browser authority;
    an empty capability list does not isolate that JavaScript.
    """
    captured = dict(assets)
    if "ui/shell.html" not in captured:
        raise PackAuthoringError("UI asset Pack requires ui/shell.html")
    for name in captured:
        if not isinstance(name, str):
            raise PackAuthoringError("UI asset path must be text")
        path = PurePosixPath(name)
        if (
            not name.startswith("ui/")
            or any(part.startswith(".") for part in path.parts)
            or path.suffix.lower() not in _PUBLIC_SUFFIXES
        ):
            raise PackAuthoringError("UI asset Pack contains a non-public path")
    return _build_pack(
        target, pack_id=pack_id, version=version, display_name=display_name,
        contracts=(), functions=(), assets=captured, asset_only=True,
    )
