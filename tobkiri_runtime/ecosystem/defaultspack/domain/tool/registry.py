import json
import os
import threading
from pathlib import Path
from typing import Any

from ..components.registry import DomainComponentRegistry, build_domain_component_roots
from ..extensions.runtime import get_extension_registry
from .security import (
    appears_write_or_execute_capable,
    is_trusted_pack_id,
    normalize_risk,
    source_pack_id_from_manifest,
    untrusted_tool_security_rejection,
    unsupported_execution_reason,
)
from .loading import normalize_tool_loading_mode
from .schema_adapter import list_or_empty, mapping_or_empty
from core_runtime.resolved_profile_scope import effective_pack_ids

_TOOL_SEARCH_METADATA_KEYS = {
    "aliases",
    "docs",
    "documentation",
    "help",
    "keywords",
    "skill_ids",
    "skills",
    "triggers",
}


class ToolRegistrationError(ValueError):
    """Raised when a Tool cannot enter the canonical registry snapshot."""


def _tool_source(tool_def: dict) -> str:
    metadata = mapping_or_empty(tool_def.get("metadata"))
    return str(
        tool_def.get("source_path")
        or metadata.get("manifest_path")
        or tool_def.get("source_pack_id")
        or metadata.get("source_pack_id")
        or "<unknown>"
    )


def _search_metadata_from_manifest(manifest: dict, config: dict) -> dict:
    metadata: dict[str, Any] = {}
    for container in (
        mapping_or_empty(manifest.get("metadata")),
        mapping_or_empty(config.get("metadata")),
    ):
        for key in _TOOL_SEARCH_METADATA_KEYS:
            if key in container:
                metadata[key] = container[key]
    for key in _TOOL_SEARCH_METADATA_KEYS:
        if key in manifest:
            metadata[key] = manifest[key]
        if key in config:
            metadata[key] = config[key]
    if manifest.get("schema_version") == "tobkiri.tool/v3":
        config_metadata = mapping_or_empty(config.get("metadata"))
        for key in ("schema_version", "effects", "security", "activity_ids"):
            if key in config_metadata:
                metadata[key] = config_metadata[key]
    return metadata


def _localized_text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    for locale in ("en", "ja"):
        text = str(value.get(locale) or "").strip()
        if text:
            return text
    return next(
        (
            str(item).strip()
            for _, item in sorted(value.items())
            if str(item).strip()
        ),
        "",
    )


def _v3_tool_config(manifest: dict) -> dict:
    """Project a strict Tool v3 manifest into the canonical registry shape."""

    discovery = mapping_or_empty(manifest.get("discovery"))
    contract = mapping_or_empty(manifest.get("contract"))
    risk = mapping_or_empty(manifest.get("risk"))
    approval = mapping_or_empty(manifest.get("approval"))
    requirements = mapping_or_empty(manifest.get("requirements"))
    raw_effects = manifest.get("effects")
    effects = [item for item in raw_effects if isinstance(item, dict)] if isinstance(raw_effects, list) else []
    effect_operations = {
        str(item.get("operation") or "").strip().casefold()
        for item in effects
    }
    effect_classes = {
        str(item.get("class") or "").strip().casefold()
        for item in effects
    }
    write_markers = {
        "write",
        "create",
        "update",
        "delete",
        "send",
        "publish",
        "execute",
        "control",
        "mutate",
    }
    write_action = bool(
        (effect_operations | effect_classes) & write_markers
        or any(bool(item.get("external")) for item in effects)
    )
    minimum = str(approval.get("minimum") or "auto").strip()
    default = str(approval.get("default") or "inherit").strip()
    activity_ids = [str(item) for item in discovery.get("activity_ids", []) or [] if str(item).strip()]
    aliases = [str(item) for item in discovery.get("aliases", []) or [] if str(item).strip()]
    keywords = [str(item) for item in discovery.get("keywords", []) or [] if str(item).strip()]
    return {
        "tool_id": str(manifest.get("id") or "").strip(),
        "name": str(manifest.get("id") or "").strip(),
        "display_name": _localized_text(manifest.get("display_name")),
        "summary": _localized_text(manifest.get("description")),
        "schema": {
            "parameters": mapping_or_empty(contract.get("input_schema")),
            **(
                {"returns": mapping_or_empty(contract.get("output_schema"))}
                if isinstance(contract.get("output_schema"), dict)
                else {}
            ),
        },
        "execution": mapping_or_empty(manifest.get("execution")),
        "risk": str(risk.get("level") or "medium"),
        "requires_approval": (
            minimum in {"confirm", "deny"}
            or default in {"confirm", "deny"}
        ),
        "approval_policy": minimum if minimum != "auto" else default,
        "write_action": write_action,
        "action_type": (
            sorted(effect_operations)[0]
            if effect_operations
            else ("write" if write_action else "read")
        ),
        "category": activity_ids[0] if activity_ids else "tool",
        "tool_category": activity_ids[0] if activity_ids else "tool",
        "tags": list(dict.fromkeys([*keywords, *activity_ids])),
        "aliases": aliases,
        "keywords": keywords,
        "activity_ids": activity_ids,
        "loading": (
            "lazy"
            if discovery.get("schema_loading") == "on_demand"
            else "always"
        ),
        "requires_model_capabilities": list(
            requirements.get("model_capabilities") or []
        ),
        "requires_runtime_capabilities": list(
            requirements.get("runtime_capabilities") or []
        ),
        "capability_requirements": {
            "connections": list(requirements.get("connections") or []),
            "env": list(requirements.get("env") or []),
        },
        "ui": mapping_or_empty(manifest.get("ui")),
        "metadata": {
            "aliases": aliases,
            "keywords": keywords,
            "activity_ids": activity_ids,
            "schema_version": "tobkiri.tool/v3",
            "effects": effects,
            "security": mapping_or_empty(manifest.get("security")),
        },
    }


class ToolRegistry:
    """ツール定義の登録・管理（シングルトン・インメモリ + 永続化）"""
    _instance: "ToolRegistry | None" = None
    _instance_lock = threading.Lock()
    _initialized: bool
    _initializing: bool
    _initializing_thread_id: int | None
    _initialization_condition: threading.Condition
    _tools: dict[str, dict[str, Any]]
    _diagnostics: list[dict[str, Any]]
    _mcp_servers: dict[str, Any]
    _lock: threading.Lock
    _tools_dir: str

    def __new__(cls) -> "ToolRegistry":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
                cls._instance._initializing = False
                cls._instance._initializing_thread_id = None
                cls._instance._initialization_condition = threading.Condition()
        return cls._instance

    def __init__(self):
        current_thread_id = threading.get_ident()
        with self._initialization_condition:
            while self._initializing and self._initializing_thread_id != current_thread_id:
                self._initialization_condition.wait()
            if self._initialized:
                return
            if self._initializing_thread_id == current_thread_id:
                return
            self._initializing = True
            self._initializing_thread_id = current_thread_id

        try:
            self._tools = {}
            self._diagnostics = []
            self._mcp_servers = {}
            self._lock = threading.Lock()
            self._tools_dir = self._resolve_tools_dir()
            self._load_pack_tools()
            self._load_dynamic_tools()
        except BaseException:
            with self._initialization_condition:
                self._initializing = False
                self._initializing_thread_id = None
                self._initialization_condition.notify_all()
            raise

        with self._initialization_condition:
            self._initialized = True
            self._initializing = False
            self._initializing_thread_id = None
            self._initialization_condition.notify_all()

    # ------------------------------------------------------------------
    # tools directory resolution
    # ------------------------------------------------------------------

    def _resolve_tools_dir(self):
        """user_data/shared/tools/ ディレクトリのパスを解決し、なければ作成する"""
        base = os.path.dirname(os.path.abspath(__file__))
        # domain/tool/ -> pack root -> user_data/shared/tools/
        pack_root = os.path.normpath(os.path.join(base, "..", ".."))
        tools_dir = os.path.join(pack_root, "user_data", "shared", "tools")
        os.makedirs(tools_dir, exist_ok=True)
        return tools_dir

    # ------------------------------------------------------------------
    # pack-provided tools
    # ------------------------------------------------------------------

    def _pack_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    def _ecosystem_dir(self) -> Path:
        return self._pack_root().parent

    def _installed_pack_roots(self) -> list[Path]:
        ecosystem_dir = self._ecosystem_dir()
        if not ecosystem_dir.is_dir():
            return [self._pack_root()]
        roots: list[Path] = []
        effective = effective_pack_ids()
        candidate_pack_ids = set(effective)
        for pack_id in sorted(candidate_pack_ids):
            path = ecosystem_dir / pack_id
            if (
                path.is_dir()
                and self._has_selected_pack_manifest(path, pack_id)
            ):
                roots.append(path)
        return roots

    @staticmethod
    def _has_selected_pack_manifest(pack_root: Path, pack_id: str) -> bool:
        """Accept only a finite selected Pack with a matching canonical manifest."""

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

    def _load_pack_tools(self):
        loaded = self._load_extension_tools()
        for pack_root in self._installed_pack_roots():
            loaded += self._load_tools_from_pack(pack_root)
        loaded += self._load_component_tools()
        loaded += self._load_first_party_memo_tools()
        self._apply_extension_skill_metadata()
        return loaded

    def _load_first_party_memo_tools(self) -> int:
        loaded = 0
        for manifest in _first_party_memo_tool_manifests():
            tool_def = self._tool_from_manifest(manifest, source_pack_id="defaultspack")
            if tool_def is None:
                continue
            metadata = dict(tool_def.get("metadata", {}))
            metadata["source"] = "pack"
            metadata["source_pack_id"] = "defaultspack"
            metadata["first_party"] = True
            tool_def["metadata"] = metadata
            tool_def["source_pack_id"] = "defaultspack"
            tool_def["trusted"] = True
            self.register(tool_def)
            loaded += 1
        return loaded

    def _load_tools_from_pack(self, pack_root: Path) -> int:
        loaded = 0
        pack_id = self._pack_id_from_root(pack_root)
        for manifest_path in sorted((pack_root / "tools").glob("*/manifest.json")):
            tool_def = self._tool_from_path_manifest(manifest_path, pack_root, pack_id)
            if tool_def is not None:
                if self._already_loaded_from_manifest(tool_def):
                    continue
                self.register(tool_def)
                loaded += 1
        for manifest_path in sorted((pack_root / "tools").glob("*/tool.json")):
            tool_def = self._tool_from_path_manifest(manifest_path, pack_root, pack_id)
            if tool_def is not None:
                if self._already_loaded_from_manifest(tool_def):
                    continue
                self.register(tool_def)
                loaded += 1
        for manifest_path in sorted((pack_root / "extensions" / "tools").glob("*/manifest.json")):
            tool_def = self._tool_from_path_manifest(manifest_path, pack_root, pack_id)
            if tool_def is not None:
                if self._already_loaded_from_manifest(tool_def):
                    continue
                self.register(tool_def)
                loaded += 1
        return loaded

    def _already_loaded_from_manifest(self, tool_def: dict) -> bool:
        """Return whether discovery already registered this exact manifest.

        Extension discovery and active-pack discovery can legitimately expose
        the same first-party manifest. Only that identical canonical source is
        idempotent; the registry still rejects the same Tool ID from a
        different file or pack.
        """

        tool_id = str(tool_def.get("tool_id") or "").strip()
        existing = self.get(tool_id) if tool_id else None
        if existing is None:
            return False

        existing_source = _tool_source(existing)
        incoming_source = _tool_source(tool_def)
        if not existing_source or not incoming_source:
            return False
        try:
            same_source = Path(existing_source).resolve() == Path(
                incoming_source
            ).resolve()
        except (OSError, RuntimeError):
            same_source = existing_source == incoming_source

        return same_source and (
            source_pack_id_from_manifest(existing)
            == source_pack_id_from_manifest(tool_def)
        )

    def _load_component_tools(self) -> int:
        loaded = 0
        registry = DomainComponentRegistry(build_domain_component_roots(self._pack_root()))
        for component in registry.list("tools"):
            manifest = component.as_dict()
            tool_manifest = self._tool_manifest_from_component(manifest)
            if tool_manifest is None:
                continue
            tool_def = self._tool_from_manifest(tool_manifest, source_pack_id=component.source_pack_id)
            if tool_def is None:
                continue
            if tool_def.get("tool_id") != component.id:
                continue
            existing = self.get(tool_def["tool_id"])
            if existing is not None:
                if not self._component_may_annotate_existing_tool(
                    existing,
                    component.source_pack_id,
                ):
                    continue
                metadata = dict(existing.get("metadata", {}))
                metadata["component_category"] = "tools"
                metadata["component_id"] = component.id
                metadata["component_manifest_path"] = manifest.get(
                    "source_path",
                    "",
                )
                annotated = dict(existing)
                annotated["metadata"] = metadata
                with self._lock:
                    self._tools[tool_def["tool_id"]] = annotated
                loaded += 1
                continue
            metadata = dict(tool_def.get("metadata", {}))
            metadata["source"] = "pack"
            metadata["source_pack_id"] = component.source_pack_id
            metadata["component_category"] = "tools"
            metadata["component_id"] = component.id
            metadata["component_manifest_path"] = manifest.get("source_path", "")
            tool_def["metadata"] = metadata
            tool_def["source_pack_id"] = component.source_pack_id
            self.register(tool_def)
            loaded += 1
        return loaded

    @staticmethod
    def _component_may_annotate_existing_tool(existing: dict, source_pack_id: str) -> bool:
        existing_pack_id = source_pack_id_from_manifest(existing)
        return bool(existing_pack_id and existing_pack_id == str(source_pack_id or "").strip())

    @staticmethod
    def _tool_manifest_from_component(component_manifest: dict):
        tool_manifest = component_manifest.get("tool_manifest")
        if isinstance(tool_manifest, dict):
            manifest = dict(tool_manifest)
            manifest.setdefault("source_path", component_manifest.get("source_path", ""))
            manifest.setdefault("source_pack_id", component_manifest.get("source_pack_id", ""))
            return manifest

        entrypoints = component_manifest.get("entrypoints")
        rel_path = entrypoints.get("tool_manifest") if isinstance(entrypoints, dict) else None
        if not isinstance(rel_path, str) or not rel_path.strip():
            return None
        source_path = component_manifest.get("source_path")
        if not isinstance(source_path, str) or not source_path:
            return None
        manifest_path = (Path(source_path).parent / rel_path).resolve()
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(manifest, dict):
            return None
        manifest["source_path"] = str(manifest_path)
        manifest.setdefault("source_pack_id", component_manifest.get("source_pack_id", ""))
        return manifest

    def _tool_from_path_manifest(self, manifest_path: Path, pack_root: Path, pack_id: str | None = None):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(manifest, dict):
            return None
        manifest["source_path"] = str(manifest_path)
        pack_id = pack_id or self._pack_id_from_root(pack_root)
        manifest["source_pack_id"] = pack_id
        tool_def = self._tool_from_manifest(manifest, source_pack_id=pack_id)
        if tool_def is None:
            return None
        metadata = dict(tool_def.get("metadata", {}))
        metadata["source_pack_id"] = pack_id
        metadata["source"] = "pack"
        tool_def["metadata"] = metadata
        tool_def["source_pack_id"] = pack_id
        return tool_def

    @staticmethod
    def _pack_id_from_root(pack_root: Path) -> str:
        candidates = (
            pack_root / "pack.v4.json",
            pack_root / "v4" / "packs" / f"{pack_root.name}.pack.v4.json",
        )
        for manifest_path in candidates:
            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                pack_id = str((raw.get("pack") or {}).get("id") or "").strip()
                if pack_id:
                    return pack_id
            except (OSError, UnicodeError, ValueError, TypeError):
                continue
        return pack_root.name

    def _load_extension_tools(self):
        """extension manifests から built-in tools を構築する。"""
        loaded = 0
        try:
            manifests = get_extension_registry().tools().list(
                enabled_only=True
            )
        except Exception:
            return 0

        for manifest in manifests:
            tool_def = self._tool_from_manifest(manifest, allow_legacy_compat=True)
            if tool_def is None:
                continue
            self.register(tool_def)
            loaded += 1
        return loaded

    def _apply_extension_skill_metadata(self):
        """skill manifests can annotate tools without becoming executable tools."""
        try:
            skills = get_extension_registry().skills().list(
                enabled_only=True
            )
        except Exception:
            return 0

        applied = 0
        for skill in skills:
            skill_id = str(skill.get("id", "")).strip()
            if not skill_id:
                continue
            applies_to = skill.get("applies_to_tools", [])
            if not isinstance(applies_to, list):
                applies_to = []
            triggers = skill.get("triggers", [])
            if not isinstance(triggers, list):
                triggers = []
            for tool_id in applies_to:
                tool_key = str(tool_id or "").strip()
                if not tool_key:
                    continue
                with self._lock:
                    tool_def = self._tools.get(tool_key)
                    if tool_def is None:
                        continue
                    skills_list = list(tool_def.get("skills", []) or [])
                    if skill_id not in skills_list:
                        skills_list.append(skill_id)
                    tool_def["skills"] = skills_list
                    metadata = dict(tool_def.get("metadata", {}))
                    metadata_skills = list(metadata.get("skills", []) or [])
                    if skill_id not in metadata_skills:
                        metadata_skills.append(skill_id)
                    metadata["skills"] = metadata_skills
                    if triggers:
                        metadata["skill_triggers"] = [
                            *list(metadata.get("skill_triggers", []) or []),
                            *(str(trigger) for trigger in triggers if str(trigger).strip()),
                        ]
                    tool_def["metadata"] = metadata
                    self._tools[tool_key] = tool_def
                applied += 1
        return applied

    @staticmethod
    def _tool_from_manifest(manifest, source_pack_id: str = "", allow_legacy_compat: bool = False):
        if manifest.get("schema_version") == "tobkiri.tool/v3":
            config = _v3_tool_config(manifest)
        else:
            config = mapping_or_empty(manifest.get("config"))
        tool_id = str(config.get("tool_id", manifest.get("id", ""))).strip()
        if not tool_id:
            return None
        ui = config.get("ui")
        if not isinstance(ui, dict):
            ui = manifest.get("ui")
        if not isinstance(ui, dict):
            ui = {}
        display_name = _localized_text(
            config.get("display_name")
            or manifest.get("display_name")
            or ""
        )
        execution = mapping_or_empty(config.get("execution"))
        handler = str(config.get("handler", "")).strip()
        write_action = bool(config.get("write_action", False))
        requires_approval = bool(config.get("requires_approval", False))
        action_type = str(config.get("action_type", "")).strip()
        approval_policy = str(config.get("approval_policy", "")).strip()
        raw_capability_grants = config.get("capability_grants", []) or []
        if isinstance(raw_capability_grants, list):
            capability_grants = [str(item) for item in raw_capability_grants if str(item).strip()]
        elif isinstance(raw_capability_grants, str) and raw_capability_grants.strip():
            capability_grants = [raw_capability_grants.strip()]
        else:
            capability_grants = []
        capability_requirements = mapping_or_empty(config.get("capability_requirements"))
        requires_model_capabilities = [
            str(item).strip()
            for item in list_or_empty(config.get("requires_model_capabilities"))
            if str(item or "").strip()
        ]
        requires_input_modalities = [
            str(item).strip()
            for item in list_or_empty(config.get("requires_input_modalities"))
            if str(item or "").strip()
        ]
        requires_runtime_capabilities = [
            str(item).strip()
            for item in list_or_empty(config.get("requires_runtime_capabilities"))
            if str(item or "").strip()
        ]
        attachment_policy = str(config.get("attachment_policy") or "").strip()
        supports_attachments = config.get("supports_attachments") if isinstance(config.get("supports_attachments"), bool) else None
        tags = list_or_empty(config.get("tags") or manifest.get("tags"))
        extra_metadata = _search_metadata_from_manifest(manifest, config)
        loading = normalize_tool_loading_mode(
            config.get("loading")
            or config.get("load_mode")
            or manifest.get("loading")
            or manifest.get("load_mode")
        )
        if not execution:
            execution = {"type": "local"}
        if handler and "handler" not in execution:
            execution["handler"] = handler
        pack_id = str(source_pack_id or "").strip() or source_pack_id_from_manifest(manifest)
        trusted = is_trusted_pack_id(pack_id)
        # Some older registry/UI paths built manifests without a pack identity.
        # Keep them visible for compatibility, but the executor still rejects the
        # untrusted legacy execution path via unsupported_execution_reason().
        legacy_compat = bool(allow_legacy_compat or not pack_id)
        raw_risk = config.get("risk", manifest.get("risk", ""))
        provisional: dict[str, Any] = {
            "tool_id": tool_id,
            "name": str(config.get("name", tool_id)),
            "display_name": display_name,
            "summary": _localized_text(
                config.get("summary") or manifest.get("description", "")
            ),
            "description": _localized_text(manifest.get("description", "")),
            "tags": tags,
            "schema": dict(
                config.get(
                    "schema",
                    {
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "required": [],
                        }
                    },
                )
            ),
            "execution": execution,
            "category": str(config.get("tool_category", config.get("category", ""))),
            "action_type": action_type,
            "approval_policy": approval_policy,
            "capability_grants": capability_grants,
            "capability_requirements": capability_requirements,
            "requires_model_capabilities": requires_model_capabilities,
            "requires_input_modalities": requires_input_modalities,
            "requires_runtime_capabilities": requires_runtime_capabilities,
            "attachment_policy": attachment_policy,
            "supports_attachments": supports_attachments,
            "write_action": write_action,
            "requires_approval": requires_approval,
            "metadata": {
                "source_pack_id": pack_id,
                "trusted": trusted,
                "category": str(config.get("tool_category", config.get("category", ""))),
                "action_type": action_type,
                "approval_policy": approval_policy,
                "capability_grants": capability_grants,
                "capability_requirements": capability_requirements,
                "requires_model_capabilities": requires_model_capabilities,
                "requires_input_modalities": requires_input_modalities,
                "requires_runtime_capabilities": requires_runtime_capabilities,
                "attachment_policy": attachment_policy,
                "supports_attachments": supports_attachments,
                "write_action": write_action,
                "requires_approval": requires_approval,
                "loading": loading,
            },
            "source_pack_id": pack_id,
            "trusted": trusted,
            "loading": loading,
        }
        risk, risk_was_unknown = normalize_risk(raw_risk, provisional, trusted)
        inferred_unsafe = appears_write_or_execute_capable(provisional)
        if inferred_unsafe and not trusted:
            risk = "high"
        requires_approval = bool(requires_approval or (not trusted and (risk == "high" or inferred_unsafe)))
        provisional["risk"] = risk
        provisional["requires_approval"] = requires_approval
        provisional["metadata"]["risk"] = risk
        provisional["metadata"]["requires_approval"] = requires_approval
        if risk_was_unknown:
            provisional["metadata"]["risk_defaulted"] = True
        rejection_reason = unsupported_execution_reason(provisional)
        untrusted_rejection = untrusted_tool_security_rejection(provisional)
        if rejection_reason is None and untrusted_rejection is not None:
            rejection_reason = untrusted_rejection
        if rejection_reason is not None and legacy_compat and str(execution.get("type") or "local").lower() in {
            "local",
            "handler",
            "dynamic",
            "prompt",
        }:
            risk = "high"
            requires_approval = True
            provisional["risk"] = risk
            provisional["requires_approval"] = requires_approval
            provisional["metadata"]["risk"] = risk
            provisional["metadata"]["requires_approval"] = requires_approval
            provisional["metadata"]["legacy_compat_unexecutable"] = True
            provisional["metadata"]["security_rejection"] = rejection_reason
        elif rejection_reason is not None:
            return None
        legacy_compat_metadata: dict[str, Any] = {}
        if provisional["metadata"].get("legacy_compat_unexecutable"):
            legacy_compat_metadata = {
                "legacy_compat_unexecutable": True,
                "security_rejection": provisional["metadata"].get("security_rejection", ""),
            }
        if risk == "high" and "danger" not in tags:
            tags.append("danger")
        return {
            "tool_id": tool_id,
            "name": str(config.get("name", tool_id)),
            "display_name": display_name,
            "summary": _localized_text(
                config.get("summary") or manifest.get("description", "")
            ),
            "description": _localized_text(manifest.get("description", "")),
            "tags": tags,
            "risk": risk,
            "schema": dict(
                config.get(
                    "schema",
                    {
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "required": [],
                        }
                    },
                )
            ),
            "execution": execution,
            "category": str(config.get("tool_category", config.get("category", ""))),
            "action_type": action_type,
            "approval_policy": approval_policy,
            "capability_grants": capability_grants,
            "capability_requirements": capability_requirements,
            "requires_model_capabilities": requires_model_capabilities,
            "requires_input_modalities": requires_input_modalities,
            "requires_runtime_capabilities": requires_runtime_capabilities,
            "attachment_policy": attachment_policy,
            "supports_attachments": supports_attachments,
            "write_action": write_action,
            "requires_approval": requires_approval,
            "loading": loading,
            "ui": dict(ui),
            "trusted": trusted,
            "source_pack_id": pack_id,
            "metadata": {
                "source": "extension",
                "manifest_path": manifest.get("source_path", ""),
                "source_pack_id": pack_id,
                "trusted": trusted,
                "category": str(config.get("tool_category", config.get("category", ""))),
                "action_type": action_type,
                "approval_policy": approval_policy,
                "capability_grants": capability_grants,
                "capability_requirements": capability_requirements,
                "requires_model_capabilities": requires_model_capabilities,
                "requires_input_modalities": requires_input_modalities,
                "requires_runtime_capabilities": requires_runtime_capabilities,
                "attachment_policy": attachment_policy,
                "supports_attachments": supports_attachments,
                "write_action": write_action,
                "requires_approval": requires_approval,
                "loading": loading,
                "risk": risk,
                **({"risk_defaulted": True} if risk_was_unknown else {}),
                **legacy_compat_metadata,
                **extra_metadata,
            },
        }

    # ------------------------------------------------------------------
    # dynamic tools — persistence
    # ------------------------------------------------------------------

    def _load_dynamic_tools(self):
        """起動時に user_data/shared/tools/ から動的ツール定義を読み込む"""
        if not os.path.isdir(self._tools_dir):
            return
        for fname in os.listdir(self._tools_dir):
            if not fname.endswith(".tool.json"):
                continue
            fpath = os.path.join(self._tools_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    tool_def = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            metadata = dict(tool_def.get("metadata", {}))
            metadata["source"] = "user"
            metadata["source_pack_id"] = "user_dynamic"
            metadata["trusted"] = False
            metadata["migration_required"] = True
            metadata["migration_reason"] = (
                "Dynamic Python handlers are no longer executable. "
                "Migrate this Tool to a reviewed pack, MCP server, or connector."
            )
            loading = normalize_tool_loading_mode(tool_def.get("loading") or metadata.get("loading"))
            metadata["loading"] = loading
            tool_def["metadata"] = metadata
            tool_def["source_pack_id"] = "user_dynamic"
            tool_def["trusted"] = False
            tool_def["loading"] = loading
            tool_def["enabled"] = False
            tool_def["availability"] = {
                "status": "unavailable",
                "reason": "migration_required",
            }
            tool_def["migration_required"] = True
            tool_def.pop("handler_code", None)
            if unsupported_execution_reason(tool_def) is not None:
                continue
            with self._lock:
                self._tools[tool_def["tool_id"]] = tool_def

    def _save_tool_json(self, tool_def):
        """ツール定義を JSON ファイルに保存する"""
        name = tool_def.get("name", tool_def.get("tool_id", "unknown"))
        fpath = os.path.join(self._tools_dir, name + ".tool.json")
        # handler_code はファイル分離するので JSON には含めない
        save_def = {k: v for k, v in tool_def.items() if k != "handler_code"}
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(save_def, f, ensure_ascii=False, indent=2)

    def _save_handler_code(self, name, code):
        """Reject persistence of retired executable Python source."""

        del name, code
        raise ValueError(
            "migration_required: dynamic Python handler source is not persisted"
        )

    def _delete_tool_files(self, name):
        """ツール定義ファイルと handler コードファイルを削除する"""
        json_path = os.path.join(self._tools_dir, name + ".tool.json")
        handler_path = os.path.join(self._tools_dir, name + ".handler.py")
        if os.path.isfile(json_path):
            os.remove(json_path)
        if os.path.isfile(handler_path):
            os.remove(handler_path)

    # ------------------------------------------------------------------
    # core CRUD
    # ------------------------------------------------------------------

    def register(self, tool_def):
        """ツール定義を登録（インメモリのみ、永続化なし）"""
        if not isinstance(tool_def, dict):
            raise ToolRegistrationError("tool definition must be an object")
        tool_id = str(tool_def.get("tool_id") or "").strip()
        if not tool_id:
            raise ToolRegistrationError("tool definition requires tool_id")
        with self._lock:
            existing = self._tools.get(tool_id)
            if existing is not None and existing != tool_def:
                diagnostic = {
                    "code": "tool_id_collision",
                    "tool_id": tool_id,
                    "existing_source": _tool_source(existing),
                    "incoming_source": _tool_source(tool_def),
                }
                self._diagnostics.append(diagnostic)
                raise ToolRegistrationError(
                    "tool_id collision: {} (existing={}, incoming={})".format(
                        tool_id,
                        diagnostic["existing_source"],
                        diagnostic["incoming_source"],
                    )
                )
            self._tools[tool_id] = tool_def

    def diagnostics(self):
        """Return canonical registry diagnostics without exposing live state."""

        with self._lock:
            return [dict(item) for item in self._diagnostics]

    def unregister(self, tool_name):
        """ツール定義を削除（インメモリのみ）"""
        with self._lock:
            self._tools.pop(tool_name, None)

    def get(self, tool_name):
        """ツール定義を取得"""
        with self._lock:
            return self._tools.get(tool_name)

    def list_tools(self, filter_dict=None):
        """登録済みツール一覧を返す"""
        with self._lock:
            tools = list(self._tools.values())
        if filter_dict and "tags" in filter_dict:
            required_tags = set(filter_dict["tags"])
            tools = [t for t in tools if required_tags & set(t.get("tags", []))]
        return tools

    def get_schema(self, tool_name):
        """ツールのスキーマを取得"""
        with self._lock:
            tool = self._tools.get(tool_name)
        if tool:
            return tool.get("schema", {})
        return None

    # ------------------------------------------------------------------
    # dynamic tool operations (with persistence)
    # ------------------------------------------------------------------

    def register_dynamic(self, tool_def, handler_code=None):
        """Reject executable Python while keeping the legacy API explicit."""

        del tool_def, handler_code
        raise ValueError(
            "migration_required: dynamic Python Tools are no longer supported; "
            "use a reviewed pack, MCP server, or connector"
        )

    def update_dynamic(self, tool_name, updates):
        """Reject edits to retired executable Python definitions."""

        del tool_name, updates
        raise ValueError(
            "migration_required: dynamic Python Tools are no longer supported; "
            "use a reviewed pack, MCP server, or connector"
        )

    def unregister_dynamic(self, tool_name):
        """
        動的ツールを削除し、ファイルも削除する。
        戻り値: 削除された tool_def、見つからなければ None
        """
        with self._lock:
            tool_def = self._tools.get(tool_name)
            if tool_def is None:
                return None
            exec_type = tool_def.get("execution", {}).get("type", "")
            if exec_type != "dynamic":
                return None
            self._tools.pop(tool_name, None)

        self._delete_tool_files(tool_name)
        return tool_def

    def export_tool(self, tool_name):
        """Export metadata without exposing retired executable source."""
        with self._lock:
            tool_def = self._tools.get(tool_name)
        if tool_def is None:
            return None
        export = {
            key: value
            for key, value in tool_def.items()
            if key != "handler_code"
        }
        execution = mapping_or_empty(export.get("execution"))
        if execution.get("type") == "dynamic":
            export["enabled"] = False
            export["migration_required"] = True
        return export

    # ------------------------------------------------------------------
    # MCP
    # ------------------------------------------------------------------

    def register_mcp_server(self, server_name, connection_info):
        """MCP サーバー接続情報を記録"""
        with self._lock:
            self._mcp_servers[server_name] = connection_info

    def list_mcp_servers(self):
        """MCP サーバー一覧"""
        with self._lock:
            return dict(self._mcp_servers)

    def unregister_mcp_server(self, server_name):
        """Remove runtime MCP connection metadata and the server's ephemeral tools."""
        normalized = str(server_name or "").strip()
        if not normalized:
            return []
        with self._lock:
            self._mcp_servers.pop(normalized, None)
            removed = []
            for tool_id, tool_def in list(self._tools.items()):
                execution = tool_def.get("execution") if isinstance(tool_def, dict) else {}
                if not isinstance(execution, dict) or execution.get("type") != "mcp":
                    continue
                if str(execution.get("server_name") or "") != normalized:
                    continue
                self._tools.pop(tool_id, None)
                removed.append(tool_id)
            return removed


def _first_party_memo_tool_manifests():
    base_properties = {
        "folder_id": {
            "type": "string",
            "description": "Memo folder id or slug. Defaults to personalization.",
        },
        "metadata": {
            "type": "object",
            "description": "Optional non-sensitive memo metadata.",
        },
    }
    return [
        {
            "id": "memo_folder_upsert",
            "category": "tool",
            "description": "Create or update a durable local memo folder.",
            "source_pack_id": "defaultspack",
            "config": {
                "tool_id": "memo_folder_upsert",
                "name": "memo_folder_upsert",
                "summary": "Create or update a Memory2 memo folder.",
                "tool_category": "memory",
                "action_type": "write",
                "write_action": True,
                "requires_approval": False,
                "tags": ["memory", "memo", "folder"],
                "execution": {"type": "handler", "handler": "blocks.memory.memo_folders:tool_upsert_folder"},
                "schema": {
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "folder_id": {"type": "string"},
                            "slug": {"type": "string"},
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "metadata": {"type": "object"},
                        },
                        "required": ["name"],
                    }
                },
            },
        },
        {
            "id": "memo_note_upsert",
            "category": "tool",
            "description": "Create or update a durable local memo note.",
            "source_pack_id": "defaultspack",
            "config": {
                "tool_id": "memo_note_upsert",
                "name": "memo_note_upsert",
                "summary": "Create or update a Memory2 memo note.",
                "tool_category": "memory",
                "action_type": "write",
                "write_action": True,
                "requires_approval": False,
                "tags": ["memory", "memo"],
                "execution": {"type": "handler", "handler": "blocks.memory.memo_notes:tool_upsert_note"},
                "schema": {
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **base_properties,
                            "note_id": {"type": "string", "description": "Existing note id to update."},
                            "title": {"type": "string", "description": "Short memo title."},
                            "content": {"type": "string", "description": "Memo note body."},
                            "source": {"type": "string", "description": "Optional source label."},
                        },
                        "required": ["content"],
                    }
                },
            },
        },
        {
            "id": "memo_search",
            "category": "tool",
            "description": "Search durable local memo notes.",
            "source_pack_id": "defaultspack",
            "config": {
                "tool_id": "memo_search",
                "name": "memo_search",
                "summary": "Search Memory2 memo notes.",
                "tool_category": "memory",
                "action_type": "read",
                "tags": ["memory", "memo", "search"],
                "execution": {"type": "handler", "handler": "blocks.memory.memo_notes:tool_search_notes"},
                "schema": {
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **base_properties,
                            "query": {"type": "string", "description": "Search query."},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                        },
                        "required": ["query"],
                    }
                },
            },
        },
        {
            "id": "memo_get",
            "category": "tool",
            "description": "Get a durable local memo note.",
            "source_pack_id": "defaultspack",
            "config": {
                "tool_id": "memo_get",
                "name": "memo_get",
                "summary": "Load a Memory2 memo note by id.",
                "tool_category": "memory",
                "action_type": "read",
                "tags": ["memory", "memo"],
                "execution": {"type": "handler", "handler": "blocks.memory.memo_notes:tool_get_note"},
                "schema": {
                    "parameters": {
                        "type": "object",
                        "properties": {"note_id": {"type": "string", "description": "Memo note id."}},
                        "required": ["note_id"],
                    }
                },
            },
        },
        {
            "id": "memo_list",
            "category": "tool",
            "description": "List durable local memo notes.",
            "source_pack_id": "defaultspack",
            "config": {
                "tool_id": "memo_list",
                "name": "memo_list",
                "summary": "List Memory2 memo notes.",
                "tool_category": "memory",
                "action_type": "read",
                "tags": ["memory", "memo"],
                "execution": {"type": "handler", "handler": "blocks.memory.memo_notes:tool_list_notes"},
                "schema": {
                    "parameters": {
                        "type": "object",
                        "properties": {**base_properties, "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
                        "required": [],
                    }
                },
            },
        },
        {
            "id": "memo_create_note",
            "category": "tool",
            "description": "Create a durable local memo note.",
            "source_pack_id": "defaultspack",
            "config": {
                "tool_id": "memo_create_note",
                "name": "memo_create_note",
                "summary": "Create a durable local memo note in Memory2.",
                "tool_category": "memory",
                "action_type": "write",
                "write_action": True,
                "requires_approval": False,
                "tags": ["memory", "memo"],
                "execution": {
                    "type": "handler",
                    "handler": "blocks.memory.memo_notes:tool_create_note",
                },
                "schema": {
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **base_properties,
                            "title": {"type": "string", "description": "Short memo title."},
                            "content": {"type": "string", "description": "Memo note body."},
                            "source": {"type": "string", "description": "Optional source label."},
                        },
                        "required": ["content"],
                    }
                },
            },
        },
        {
            "id": "memo_search_notes",
            "category": "tool",
            "description": "Search durable local memo notes.",
            "source_pack_id": "defaultspack",
            "config": {
                "tool_id": "memo_search_notes",
                "name": "memo_search_notes",
                "summary": "Search Memory2 memo notes.",
                "tool_category": "memory",
                "action_type": "read",
                "tags": ["memory", "memo", "search"],
                "execution": {
                    "type": "handler",
                    "handler": "blocks.memory.memo_notes:tool_search_notes",
                },
                "schema": {
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **base_properties,
                            "query": {"type": "string", "description": "Search query."},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                        },
                        "required": ["query"],
                    }
                },
            },
        },
        {
            "id": "memo_list_notes",
            "category": "tool",
            "description": "List durable local memo notes.",
            "source_pack_id": "defaultspack",
            "config": {
                "tool_id": "memo_list_notes",
                "name": "memo_list_notes",
                "summary": "List Memory2 memo notes.",
                "tool_category": "memory",
                "action_type": "read",
                "tags": ["memory", "memo"],
                "execution": {
                    "type": "handler",
                    "handler": "blocks.memory.memo_notes:tool_list_notes",
                },
                "schema": {
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **base_properties,
                            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                        },
                        "required": [],
                    }
                },
            },
        },
    ]
