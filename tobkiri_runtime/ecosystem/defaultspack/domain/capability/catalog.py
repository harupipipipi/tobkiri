from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from core_runtime.profile_content_projection import selected_projection_roots
from core_runtime.resolved_profile_scope import (
    effective_pack_ids,
    effective_profile_projections,
)


class CapabilityCatalog:
    """Loads defaultspack's local-first service manifests."""

    def __init__(self, pack_root: Optional[Path] = None) -> None:
        self.pack_root = Path(pack_root) if pack_root is not None else Path(__file__).resolve().parents[2]

    def _load_yaml_dir(self, directory_name: str, suffix: str) -> List[Dict[str, Any]]:
        return self._load_yaml_dir_from_roots(directory_name, suffix, self._catalog_roots())

    def _catalog_sources(self) -> List[Tuple[str, Path, str]]:
        ecosystem_dir = self._ecosystem_root()
        sources: List[Tuple[str, Path, str]] = []
        effective = effective_pack_ids()
        if ecosystem_dir.is_dir():
            for pack_id in sorted(effective):
                path = ecosystem_dir / pack_id
                try:
                    is_pack_root = path.is_dir() and self._pack_id(path) == pack_id
                except OSError:
                    continue
                if is_pack_root:
                    sources.append((pack_id, path, "pack"))
        pack_id = self._pack_id(self.pack_root)
        if pack_id in effective and all(path != self.pack_root for _, path, _ in sources):
            sources.insert(0, (pack_id, self.pack_root, "pack"))
        for projection_id, root in selected_projection_roots(
            effective_profile_projections(), kind="profile_content"
        ):
            sources.append((projection_id, root, "profile_projection"))
        return sources

    def _catalog_roots(self) -> List[Path]:
        return [root for _source_id, root, _kind in self._catalog_sources()]

    def _ecosystem_root(self) -> Path:
        if self._pack_id(self.pack_root) and self.pack_root.parent.name == "ecosystem":
            return self.pack_root.parent
        return Path(__file__).resolve().parents[3]

    @staticmethod
    def _pack_id(pack_root: Path) -> str:
        try:
            raw = json.loads((pack_root / "pack.v4.json").read_text(encoding="utf-8"))
            pack_id = str((raw.get("pack") or {}).get("id") or "").strip()
            if pack_id:
                return pack_id
        except Exception:
            pass
        return ""

    def _load_yaml_dir_from_roots(
        self,
        directory_name: str,
        suffix: str,
        roots: List[Path],
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        source_by_root = {
            root: (source_id, source_kind)
            for source_id, root, source_kind in self._catalog_sources()
        }
        for root in roots:
            source_id, source_kind = source_by_root.get(
                root, (self._pack_id(root), "pack")
            )
            items.extend(
                self._load_yaml_dir_from_root(
                    root,
                    directory_name,
                    suffix,
                    source_id=source_id,
                    source_kind=source_kind,
                )
            )
        return items

    def _load_yaml_dir_from_root(
        self,
        pack_root: Path,
        directory_name: str,
        suffix: str,
        *,
        source_id: str,
        source_kind: str,
    ) -> List[Dict[str, Any]]:
        directory = pack_root / directory_name
        if not directory.is_dir():
            return []
        items: List[Dict[str, Any]] = []
        for path in sorted(directory.glob("*" + suffix)):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception as exc:
                data = {"id": path.stem, "error": str(exc)}
            if isinstance(data, dict):
                data.setdefault("id", path.name.replace(suffix, ""))
                data["source_authority_id"] = source_id
                data["source_authority_kind"] = source_kind
                if source_kind == "pack":
                    data["source_pack_id"] = source_id
                    data["_source_pack_id"] = source_id
                else:
                    data["source_projection_id"] = source_id
                try:
                    data["_source_path"] = str(path.relative_to(pack_root))
                except ValueError:
                    data["_source_path"] = str(path)
                items.append(data)
        return items

    def capabilities(self, local_only: Any = None, risk_level: Optional[str] = None) -> List[Dict[str, Any]]:
        items = self._load_yaml_dir("capabilities", ".capability.yaml")
        if local_only is not None:
            expected = local_only
            if isinstance(expected, str):
                expected = expected.lower() in {"1", "true", "yes"}
            items = [item for item in items if item.get("local_only") == expected]
        if risk_level:
            items = [item for item in items if item.get("risk_level") == risk_level]
        return items

    def capability(self, capability_id: str) -> Optional[Dict[str, Any]]:
        for item in self.capabilities():
            if item.get("id") == capability_id or item.get("capability_id") == capability_id:
                return item
        return None

    def profiles(self) -> List[Dict[str, Any]]:
        return [self._normalize_profile(item) for item in self._load_yaml_dir("profiles", ".profile.yaml")]

    @staticmethod
    def _normalize_profile(item: Dict[str, Any]) -> Dict[str, Any]:
        profile = dict(item)
        nested = profile.get("profile")
        if isinstance(nested, dict):
            profile = {**profile, **nested}
            profile.pop("profile", None)
        profile_id = str(profile.get("profile_id") or profile.get("id") or "").strip()
        if profile_id:
            profile["profile_id"] = profile_id
            profile.setdefault("id", profile_id)
        return profile

    def presets(self) -> List[Dict[str, Any]]:
        return self._load_yaml_dir("presets", ".preset.yaml")

    def schemas(self) -> List[Dict[str, Any]]:
        return self._load_yaml_dir("schemas", ".schema.yaml")

    def examples(self) -> List[Dict[str, Any]]:
        return self._load_yaml_dir("examples", ".example.yaml")

    def prompts(self) -> List[Dict[str, Any]]:
        prompts: List[Dict[str, Any]] = []
        for source_id, pack_root, source_kind in self._catalog_sources():
            prompt_dir = pack_root / "prompts"
            if not prompt_dir.is_dir():
                continue
            for path in sorted(prompt_dir.glob("*.system.md")):
                text = path.read_text(encoding="utf-8")
                prompts.append(
                    {
                        "id": path.name.replace(".system.md", ""),
                        "name": path.stem.replace(".system", ""),
                        "content_ref": str(path.relative_to(pack_root)),
                        "preview": text.strip().splitlines()[0] if text.strip() else "",
                        "source_authority_id": source_id,
                        "source_authority_kind": source_kind,
                        **(
                            {"source_pack_id": source_id, "_source_pack_id": source_id}
                            if source_kind == "pack"
                            else {"source_projection_id": source_id}
                        ),
                    }
                )
        return prompts

    def prompt(self, prompt_id: str, source_pack_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        target_id = str(prompt_id or "").strip()
        target_pack = str(source_pack_id or "").strip()
        if not target_id:
            return None
        for prompt in self.prompts():
            if str(prompt.get("id") or "").strip() != target_id:
                continue
            if target_pack and str(
                prompt.get("source_authority_id") or ""
            ).strip() != target_pack:
                continue
            return prompt
        return None

    def prompt_text(self, prompt_id: str, source_pack_id: Optional[str] = None) -> Optional[str]:
        prompt = self.prompt(prompt_id, source_pack_id=source_pack_id)
        if not isinstance(prompt, dict):
            return None
        content_ref = str(prompt.get("content_ref") or "").strip()
        source_id = str(
            prompt.get("source_authority_id") or source_pack_id or ""
        ).strip()
        if not content_ref:
            return None
        for candidate_id, pack_root, _source_kind in self._catalog_sources():
            if source_id and candidate_id != source_id:
                continue
            path = pack_root / content_ref
            if path.is_file():
                return path.read_text(encoding="utf-8")
        return None

    def feature_catalog(self) -> Dict[str, Any]:
        path = self.pack_root / "docs" / "ai_agent_services_feature_catalog.md"
        return {
            "content_ref": str(path.relative_to(self.pack_root)),
            "exists": path.is_file(),
        }

    def profile(self, profile_id: str) -> Optional[Dict[str, Any]]:
        for profile in self.profiles():
            if profile.get("profile_id") == profile_id or profile.get("id") == profile_id:
                return profile
        return None

    def manifest(self) -> Dict[str, Any]:
        capabilities = self.capabilities()
        profiles = self.profiles()
        presets = self.presets()
        return {
            "service_id": "defaultspack.ai_agent_service",
            "version": "rumi.defaultspack.agent_service.v1",
            "local_first": True,
            "core_requires_api_key": False,
            "default_profile": "defaultspack.local_agent",
            "counts": {
                "capabilities": len(capabilities),
                "profiles": len(profiles),
                "presets": len(presets),
                "schemas": len(self.schemas()),
                "prompts": len(self.prompts()),
                "examples": len(self.examples()),
            },
            "capabilities": capabilities,
            "profiles": profiles,
            "presets": presets,
            "feature_catalog": self.feature_catalog(),
            "policy": {
                "network_default": "deny",
                "write_actions_require_approval": True,
                "delete_actions_require_approval": True,
                "terminal_actions_require_approval": True,
                "git_push_requires_approval": True,
                "secrets_redacted": True,
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.manifest(), ensure_ascii=False, indent=2)
