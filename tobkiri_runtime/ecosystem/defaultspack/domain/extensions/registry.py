from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .categories import DEFAULT_CATEGORY_SPECS
from .discovery import DiscoveryIssue, discover_extensions
from .manifest import ManifestValidationError


class ExtensionRegistry:
    def __init__(
        self,
        extensions_root: Path | str | Iterable[Path | str],
        *,
        strict: bool = False,
    ) -> None:
        if isinstance(extensions_root, (str, Path)):
            self._roots = [Path(extensions_root)]
        else:
            self._roots = [Path(root) for root in extensions_root]
        self._root = self._roots[0] if self._roots else Path(".")
        self._strict = strict
        self._items: Dict[str, Dict[str, Dict[str, Any]]] = {
            category: {} for category in DEFAULT_CATEGORY_SPECS.keys()
        }
        self._issues: List[DiscoveryIssue] = []
        self.reload()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def issues(self) -> List[DiscoveryIssue]:
        return list(self._issues)

    def reload(self) -> "ExtensionRegistry":
        self._items = {category: {} for category in DEFAULT_CATEGORY_SPECS.keys()}
        self._issues = []
        for root in self._roots:
            result = discover_extensions(
                root,
                categories=DEFAULT_CATEGORY_SPECS.keys(),
                strict=self._strict,
            )
            for item in result.extensions:
                self._items[item.category][item.extension_id] = dict(item.manifest)
            self._issues.extend(result.issues)
        self._validate_cross_references()
        return self

    def _validate_cross_references(self) -> None:
        provider_ids = set(self._items.get("llm_provider", {}).keys())
        models = self._items.get("llm_model", {})
        for model_id, model in list(models.items()):
            provider_id = str(model.get("provider_id", "")).strip()
            if provider_id and provider_id in provider_ids:
                continue
            issue = DiscoveryIssue(
                path=str(model.get("source_path") or model_id),
                category="llm_model",
                message=f"llm_model provider_id is not registered: {provider_id or '<missing>'}",
            )
            if self._strict:
                raise ManifestValidationError(issue.message)
            self._issues.append(issue)
            models.pop(model_id, None)

    def categories(self) -> List[str]:
        return list(DEFAULT_CATEGORY_SPECS.keys())

    def get(self, category: str, extension_id: str) -> Optional[Dict[str, Any]]:
        return self._items.get(category, {}).get(extension_id)

    def list(
        self,
        category: str,
        *,
        enabled_only: bool = True,
    ) -> List[Dict[str, Any]]:
        items = list(self._items.get(category, {}).values())
        if enabled_only:
            items = [item for item in items if bool(item.get("enabled", True))]
        items.sort(key=lambda m: (int(m.get("priority", 100)), m.get("id", "")))
        return [dict(item) for item in items]

    def llm(self) -> "LLMRegistry":
        return LLMRegistry(self)

    def prompts(self) -> "PromptRegistry":
        return PromptRegistry(self)

    def tools(self) -> "ToolExtensionRegistry":
        return ToolExtensionRegistry(self)

    def skills(self) -> "SkillExtensionRegistry":
        return SkillExtensionRegistry(self)

    def activities(self) -> "ActivityExtensionRegistry":
        return ActivityExtensionRegistry(self)

    def chat_modes(self) -> "ChatModeRegistry":
        return ChatModeRegistry(self)

    def agent_modes(self) -> "AgentModeRegistry":
        return AgentModeRegistry(self)

    def knowledge_backends(self) -> "KnowledgeBackendRegistry":
        return KnowledgeBackendRegistry(self)

    def transports(self) -> "TransportRegistry":
        return TransportRegistry(self)

    def ui_surfaces(self) -> "UISurfaceRegistry":
        return UISurfaceRegistry(self)

    def policies(self) -> "PolicyRegistry":
        return PolicyRegistry(self)


class LLMRegistry:
    def __init__(self, registry: ExtensionRegistry) -> None:
        self._registry = registry

    def providers(self, *, enabled_only: bool = True) -> List[Dict[str, Any]]:
        return self._registry.list("llm_provider", enabled_only=enabled_only)

    def models(
        self,
        *,
        provider_id: str = "",
        enabled_only: bool = True,
    ) -> List[Dict[str, Any]]:
        explicit_models = self._registry.list("llm_model", enabled_only=enabled_only)
        manifests_by_id: Dict[str, Dict[str, Any]] = {}

        for model in explicit_models:
            full_id = str(model.get("id", "")).strip()
            if not full_id:
                continue
            if provider_id and model.get("provider_id") != provider_id:
                continue
            manifests_by_id[full_id] = dict(model)

        for provider in self.providers(enabled_only=enabled_only):
            current_provider_id = str(provider.get("id", "")).strip()
            if not current_provider_id:
                continue
            if provider_id and current_provider_id != provider_id:
                continue
            for synthetic_model in self._synthetic_models_for_provider(provider):
                full_id = str(synthetic_model.get("id", "")).strip()
                if full_id and full_id not in manifests_by_id:
                    manifests_by_id[full_id] = synthetic_model

        models = list(manifests_by_id.values())
        models.sort(key=lambda m: (int(m.get("priority", 100)), m.get("id", "")))
        return models

    @staticmethod
    def _synthetic_models_for_provider(provider_manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
        provider_id = str(provider_manifest.get("id", "")).strip()
        if not provider_id:
            return []

        synthetic: List[Dict[str, Any]] = []
        declared_models = provider_manifest.get("models", [])
        if isinstance(declared_models, list):
            for item in declared_models:
                if not isinstance(item, dict):
                    continue
                model_id = str(item.get("model_id") or item.get("id") or "").strip()
                if not model_id:
                    continue
                if "/" in model_id:
                    _, model_id = model_id.split("/", 1)
                synthetic.append(
                    {
                        "id": f"{provider_id}/{model_id}",
                        "category": "llm_model",
                        "provider_id": provider_id,
                        "model_id": model_id,
                        "display_name": str(item.get("display_name", model_id)),
                        "type": str(item.get("type", "chat")),
                        "defaults": dict(item.get("defaults", {})),
                        "priority": int(item.get("priority", provider_manifest.get("priority", 100))),
                        "metadata": dict(item.get("metadata", {})),
                        "enabled": bool(item.get("enabled", provider_manifest.get("enabled", True))),
                    }
                )

        default_model = str(provider_manifest.get("default_model", "")).strip()
        default_model_for = provider_manifest.get("default_model_for", {}) or {}
        if not isinstance(default_model_for, dict):
            default_model_for = {}

        if default_model:
            defaults = {"chat": True}
            for use_case, candidate_id in default_model_for.items():
                if str(candidate_id).strip() == default_model:
                    defaults[str(use_case)] = True
            synthetic.append(
                {
                    "id": f"{provider_id}/{default_model}",
                    "category": "llm_model",
                    "provider_id": provider_id,
                    "model_id": default_model,
                    "display_name": default_model,
                    "type": "chat",
                    "defaults": defaults,
                    "priority": int(provider_manifest.get("priority", 100)),
                    "enabled": bool(provider_manifest.get("enabled", True)),
                }
            )

        for use_case, candidate_id in default_model_for.items():
            model_id = str(candidate_id).strip()
            if not model_id:
                continue
            found = next(
                (
                    item
                    for item in synthetic
                    if item.get("provider_id") == provider_id and item.get("model_id") == model_id
                ),
                None,
            )
            if found is None:
                found = {
                    "id": f"{provider_id}/{model_id}",
                    "category": "llm_model",
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "display_name": model_id,
                    "type": "embedding" if use_case == "embedding" else "chat",
                    "defaults": {},
                    "priority": int(provider_manifest.get("priority", 100)),
                    "enabled": bool(provider_manifest.get("enabled", True)),
                }
                synthetic.append(found)
            defaults = dict(found.get("defaults", {}))
            defaults[str(use_case)] = True
            found["defaults"] = defaults
            if use_case == "embedding":
                found["type"] = "embedding"

        deduped: Dict[str, Dict[str, Any]] = {}
        for item in synthetic:
            full_id = str(item.get("id", "")).strip()
            if full_id and full_id not in deduped:
                deduped[full_id] = item
        return list(deduped.values())

    def best_model(
        self,
        provider_id: str,
        *,
        use_case: str = "chat",
    ) -> Optional[Dict[str, Any]]:
        candidates = self.models(provider_id=provider_id, enabled_only=True)
        if not candidates:
            return None
        provider_manifest = self._registry.get("llm_provider", provider_id) or {}
        default_model_for = provider_manifest.get("default_model_for", {}) or {}
        if not isinstance(default_model_for, dict):
            default_model_for = {}
        preferred_ref = str(default_model_for.get(use_case) or "").strip()
        if not preferred_ref and use_case == "chat":
            preferred_ref = str(provider_manifest.get("default_model") or "").strip()

        def _matches_preferred(model: Dict[str, Any]) -> bool:
            if not preferred_ref:
                return False
            model_id = str(model.get("model_id") or "").strip()
            full_id = str(model.get("id") or "").strip()
            qualified = f"{provider_id}/{preferred_ref}"
            return preferred_ref in {model_id, full_id} or qualified == full_id

        def _score(model: Dict[str, Any]) -> tuple[int, int, int, int]:
            defaults = model.get("defaults", {}) or {}
            manifest_default_hit = int(_matches_preferred(model))
            exact_hit = int(bool(defaults.get(use_case, False)))
            chat_fallback_hit = int(
                use_case != "chat" and bool(defaults.get("chat", False))
            )
            priority = int(model.get("priority", 100))
            return manifest_default_hit, exact_hit, chat_fallback_hit, -priority

        candidates.sort(key=_score, reverse=True)
        return candidates[0]


class PromptRegistry:
    def __init__(self, registry: ExtensionRegistry) -> None:
        self._registry = registry

    def list(self, *, enabled_only: bool = True) -> List[Dict[str, Any]]:
        return self._registry.list("prompt", enabled_only=enabled_only)


class ToolExtensionRegistry:
    def __init__(self, registry: ExtensionRegistry) -> None:
        self._registry = registry

    def list(self, *, enabled_only: bool = True) -> List[Dict[str, Any]]:
        return self._registry.list("tool", enabled_only=enabled_only)


class SkillExtensionRegistry:
    def __init__(self, registry: ExtensionRegistry) -> None:
        self._registry = registry

    def list(self, *, enabled_only: bool = True) -> List[Dict[str, Any]]:
        return self._registry.list("skill", enabled_only=enabled_only)


class ActivityExtensionRegistry:
    """Read validated Activity manifests from an extension registry."""

    def __init__(self, registry: ExtensionRegistry) -> None:
        self._registry = registry

    def list(self, *, enabled_only: bool = True) -> List[Dict[str, Any]]:
        """Return Activity manifests ordered by extension priority."""

        return self._registry.list("activity", enabled_only=enabled_only)

    def get(self, activity_id: str) -> Optional[Dict[str, Any]]:
        """Return one Activity manifest by stable identifier."""

        return self._registry.get("activity", activity_id)


class ChatModeRegistry:
    def __init__(self, registry: ExtensionRegistry) -> None:
        self._registry = registry

    def list(self, *, enabled_only: bool = True) -> List[Dict[str, Any]]:
        return self._registry.list("chat_mode", enabled_only=enabled_only)

    def get(self, mode_id: str) -> Optional[Dict[str, Any]]:
        return self._registry.get("chat_mode", mode_id)


class AgentModeRegistry:
    def __init__(self, registry: ExtensionRegistry) -> None:
        self._registry = registry

    def list(self, *, enabled_only: bool = True) -> List[Dict[str, Any]]:
        return self._registry.list("agent_mode", enabled_only=enabled_only)

    def get(self, mode_id: str) -> Optional[Dict[str, Any]]:
        return self._registry.get("agent_mode", mode_id)


class KnowledgeBackendRegistry:
    def __init__(self, registry: ExtensionRegistry) -> None:
        self._registry = registry

    def list(self, *, enabled_only: bool = True) -> List[Dict[str, Any]]:
        return self._registry.list("knowledge_backend", enabled_only=enabled_only)


class TransportRegistry:
    def __init__(self, registry: ExtensionRegistry) -> None:
        self._registry = registry

    def list(self, *, enabled_only: bool = True) -> List[Dict[str, Any]]:
        return self._registry.list("transport", enabled_only=enabled_only)

    def get(self, transport_id: str) -> Optional[Dict[str, Any]]:
        return self._registry.get("transport", transport_id)


class UISurfaceRegistry:
    def __init__(self, registry: ExtensionRegistry) -> None:
        self._registry = registry

    def list(self, *, enabled_only: bool = True) -> List[Dict[str, Any]]:
        return self._registry.list("ui_surface", enabled_only=enabled_only)


class PolicyRegistry:
    def __init__(self, registry: ExtensionRegistry) -> None:
        self._registry = registry

    def list(self, *, enabled_only: bool = True) -> List[Dict[str, Any]]:
        return self._registry.list("policy", enabled_only=enabled_only)
