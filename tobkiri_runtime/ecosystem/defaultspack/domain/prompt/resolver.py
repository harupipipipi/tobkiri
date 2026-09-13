from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional

from ..capability.catalog import CapabilityCatalog
from ..extensions.runtime import get_extension_registry, get_extensions_root
from .component_prompts import component_prompt_manifests, component_prompt_text
from .trust import prompt_pack_is_trusted, prompt_pack_source_is_trusted


_PROMPT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class PromptResolver:
    """Manifest-driven prompt resolver (tool/provider independent)."""

    def __init__(self) -> None:
        self._registry = get_extension_registry()
        self._extensions_root = get_extensions_root()
        self._capability_catalog = CapabilityCatalog(pack_root=self._extensions_root.parent)

    def get_manifest(self, prompt_id: str) -> Optional[Dict[str, Any]]:
        for manifest in (
            self._registry.get("prompt", prompt_id),
            component_prompt_manifests().get(prompt_id),
        ):
            if manifest is None:
                continue
            source_pack_id = str(
                manifest.get("source_pack_id")
                or manifest.get("_source_pack_id")
                or self._extensions_root.parent.name
            ).strip()
            if _manifest_prompt_is_trusted(manifest, source_pack_id):
                return manifest
        for manifest in self._capability_catalog.prompts():
            if str(manifest.get("id") or "").strip() != str(prompt_id or "").strip():
                continue
            source_pack_id = str(manifest.get("source_pack_id") or manifest.get("_source_pack_id") or "").strip()
            if _manifest_prompt_is_trusted(manifest, source_pack_id):
                return manifest
        return None

    def resolve_prompt(self, prompt_id: str, *, source_pack_id: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
        requested_source_pack_id = str(source_pack_id or "").strip()
        extension_manifest = self._registry.get("prompt", prompt_id)
        if extension_manifest is not None:
            actual_source_pack_id = str(
                extension_manifest.get("source_pack_id")
                or extension_manifest.get("_source_pack_id")
                or self._extensions_root.parent.name
            ).strip() or None
            if (
                _matches_source_pack(actual_source_pack_id, requested_source_pack_id)
                and _manifest_prompt_is_trusted(
                    extension_manifest,
                    actual_source_pack_id or "",
                )
            ):
                config = extension_manifest.get("config", {}) or {}
                template_file = str(config.get("template_file", "prompt.md"))
                path = _extension_prompt_path(
                    self._extensions_root,
                    prompt_id,
                    template_file,
                )
                if path is not None and path.is_file():
                    try:
                        return path.read_text(encoding="utf-8"), actual_source_pack_id
                    except (OSError, UnicodeDecodeError):
                        pass
        component_manifest = component_prompt_manifests().get(prompt_id)
        if component_manifest is not None:
            actual_source_pack_id = str(component_manifest.get("source_pack_id") or "").strip() or None
            if (
                _matches_source_pack(actual_source_pack_id, requested_source_pack_id)
                and _manifest_prompt_is_trusted(
                    component_manifest,
                    actual_source_pack_id or "",
                )
            ):
                component = component_prompt_text(prompt_id)
                if component is not None:
                    return component, actual_source_pack_id
        for pack_prompt in self._capability_catalog.prompts():
            if str(pack_prompt.get("id") or "").strip() != str(prompt_id or "").strip():
                continue
            candidate_source_pack_id = str(pack_prompt.get("source_pack_id") or "").strip()
            if requested_source_pack_id and candidate_source_pack_id != requested_source_pack_id:
                continue
            actual_source_pack_id = candidate_source_pack_id or source_pack_id or None
            if not prompt_pack_is_trusted(actual_source_pack_id or ""):
                continue
            text = self._capability_catalog.prompt_text(prompt_id, source_pack_id=actual_source_pack_id)
            if text is not None:
                return text, actual_source_pack_id
        return None, None

    def resolve_prompt_text(self, prompt_id: str, *, source_pack_id: Optional[str] = None) -> Optional[str]:
        content, _ = self.resolve_prompt(prompt_id, source_pack_id=source_pack_id)
        return content

    def render(
        self,
        prompt_id: str,
        variables: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        template = self.resolve_prompt_text(prompt_id)
        if template is None:
            return None
        values = dict(variables or {})
        rendered = template
        for key, value in values.items():
            rendered = rendered.replace("{{" + str(key) + "}}", str(value))
            rendered = rendered.replace("{{ " + str(key) + " }}", str(value))
        return rendered


def _matches_source_pack(actual_source_pack_id: str | None, requested_source_pack_id: str) -> bool:
    return not requested_source_pack_id or str(actual_source_pack_id or "").strip() == requested_source_pack_id


def _manifest_prompt_is_trusted(
    manifest: Dict[str, Any],
    source_pack_id: str,
) -> bool:
    """Trust a manifest and, when declared, the path that supplied it."""
    if not prompt_pack_is_trusted(source_pack_id):
        return False
    source_path = manifest.get("source_path")
    if isinstance(source_path, (str, Path)) and str(source_path).strip():
        return prompt_pack_source_is_trusted(source_pack_id, source_path)
    return True


def _extension_prompt_path(
    extensions_root: Path,
    prompt_id: str,
    template_file: str,
) -> Path | None:
    """Resolve an extension prompt body without allowing path traversal."""
    normalized_id = str(prompt_id or "").strip()
    if not normalized_id or _PROMPT_ID_RE.fullmatch(normalized_id) is None:
        return None
    relative_file = Path(str(template_file or "prompt.md").strip() or "prompt.md")
    if relative_file.is_absolute():
        return None
    try:
        prompt_dir = (Path(extensions_root) / "prompts" / normalized_id).resolve()
        candidate = (prompt_dir / relative_file).resolve()
        candidate.relative_to(prompt_dir)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate
