from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from ._helpers import canonical_json


CURRENT_TEMPLATE_SCHEMA_VERSION = 1
_TemplateEnum = TypeVar("_TemplateEnum", bound=Enum)


class TemplatePieceKind(str, Enum):
    BACKEND_SERVICE = "backend_service"
    FUNCTION = "function"
    API_ROUTE = "api_route"
    SETTINGS_SECTION = "settings_section"
    SETTINGS_FIELD = "settings_field"
    FIELD_RENDERER = "field_renderer"
    FRONTEND_COMPONENT = "frontend_component"
    COMPOSER_COMMAND = "composer_command"
    COMPOSER_INPUT = "composer_input"
    COMPOSER_WIDGET = "composer_widget"
    STATUS_SURFACE = "status_surface"
    AI_INPUT = "ai_input"
    TOOL_POLICY = "tool_policy"
    SIDEBAR_ITEM = "sidebar_item"
    CHAT_RENDERER = "chat_renderer"
    SHELL_REGION = "shell_region"
    SHELL_RENDERER = "shell_renderer"
    CONTEXT_POLICY = "context_policy"
    EXTERNAL_IO_TEMPLATE = "external_io_template"
    PERMISSION = "permission"
    MIGRATION = "migration"
    TEST_CONTRACT = "test_contract"


class TemplateKind(str, Enum):
    PACK = "pack"
    RUNTIME = "runtime"
    BACKEND = "backend"
    FRONTEND = "frontend"
    INTEGRATION = "integration"
    COMPOSITE = "composite"


class TemplateStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"


class TemplateTrustLevel(str, Enum):
    BUILTIN = "builtin"
    LOCAL = "local"
    USER = "user"
    UNTRUSTED = "untrusted"


@dataclass
class TemplateDiagnostic:
    code: str
    message: str
    severity: str = "error"
    template_id: str | None = None
    piece_id: str | None = None
    path: str | None = None
    source_path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def fingerprint(self) -> tuple[str, str, str, str, str, str, str, str]:
        return (
            self.severity,
            self.code,
            self.template_id or "",
            self.piece_id or "",
            self.path or "",
            self.source_path or "",
            self.message,
            canonical_json(self.details),
        )


@dataclass
class TemplateCapabilitySpec:
    provides: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "TemplateCapabilitySpec":
        raw = raw or {}
        return cls(
            provides=_as_string_list(raw.get("provides")),
            requires=_as_string_list(raw.get("requires")),
            permissions=_as_string_list(raw.get("permissions")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provides": list(self.provides),
            "requires": list(self.requires),
            "permissions": list(self.permissions),
        }


@dataclass(frozen=True)
class TemplateDependencySpec:
    id: str
    version: str | None = None
    optional: bool = False

    @classmethod
    def from_value(cls, value: str | dict[str, Any]) -> "TemplateDependencySpec":
        if isinstance(value, str):
            return cls(id=value.strip())
        if isinstance(value, dict):
            return cls(
                id=str(value.get("id") or "").strip(),
                version=_optional_string(value.get("version")),
                optional=bool(value.get("optional")),
            )
        return cls(id="")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"id": self.id}
        if self.version is not None:
            result["version"] = self.version
        if self.optional:
            result["optional"] = True
        return result

    def to_value(self) -> str | dict[str, Any]:
        if self.version is None and not self.optional:
            return self.id
        return self.to_dict()


@dataclass
class TemplatePiece:
    id: str
    kind: TemplatePieceKind | str
    slot: str | None = None
    order: int | None = None
    insert_before: str | None = None
    insert_after: str | None = None
    entrypoint: str | None = None
    path: str | None = None
    handler: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TemplatePiece":
        known = {
            "id",
            "kind",
            "slot",
            "order",
            "insert_before",
            "insert_after",
            "entrypoint",
            "path",
            "handler",
        }
        return cls(
            id=str(raw.get("id", "")),
            kind=_enum_or_raw(TemplatePieceKind, raw.get("kind", "")),
            slot=_optional_string(raw.get("slot")),
            order=_optional_int(raw.get("order")),
            insert_before=_optional_string(raw.get("insert_before")),
            insert_after=_optional_string(raw.get("insert_after")),
            entrypoint=_optional_string(raw.get("entrypoint")),
            path=_optional_string(raw.get("path")),
            handler=_optional_string(raw.get("handler")),
            data={key: value for key, value in raw.items() if key not in known},
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"id": self.id, "kind": _enum_value(self.kind)}
        if self.slot is not None:
            result["slot"] = self.slot
        if self.order is not None:
            result["order"] = self.order
        if self.insert_before is not None:
            result["insert_before"] = self.insert_before
        if self.insert_after is not None:
            result["insert_after"] = self.insert_after
        if self.entrypoint is not None:
            result["entrypoint"] = self.entrypoint
        if self.path is not None:
            result["path"] = self.path
        if self.handler is not None:
            result["handler"] = self.handler
        result.update(self.data)
        return result


@dataclass
class RumiTemplate:
    id: str
    kind: TemplateKind | str
    version: str
    status: TemplateStatus | str
    schema_version: int = CURRENT_TEMPLATE_SCHEMA_VERSION
    pieces: list[TemplatePiece] = field(default_factory=list)
    trust_level: TemplateTrustLevel | str = TemplateTrustLevel.LOCAL
    extends: str | list[str] | None = None
    dependencies: list[TemplateDependencySpec] = field(default_factory=list)
    conflicts: list[TemplateDependencySpec] = field(default_factory=list)
    capabilities: TemplateCapabilitySpec = field(default_factory=TemplateCapabilitySpec)
    patches: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    source_path: Path | None = None
    declared_id: str | None = None

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any],
        *,
        source_path: str | Path | None = None,
        trust_level: TemplateTrustLevel | str | None = None,
        declared_id: str | None = None,
    ) -> "RumiTemplate":
        explicit_trust = (
            trust_level
            if trust_level is not None
            else raw.get("trust_level", TemplateTrustLevel.LOCAL)
        )
        metadata = (
            dict(raw.get("metadata", {})) if isinstance(raw.get("metadata", {}), dict) else {}
        )
        raw_id = str(raw.get("id", ""))
        if trust_level is not None and "trust_level" in raw:
            metadata["declared_trust_level"] = str(raw.get("trust_level"))
        return cls(
            id=raw_id.strip(),
            kind=_enum_or_raw(TemplateKind, raw.get("kind", "")),
            version=str(raw.get("version", "")),
            status=_enum_or_raw(TemplateStatus, raw.get("status", "")),
            schema_version=_optional_int(raw.get("schema_version"))
            or CURRENT_TEMPLATE_SCHEMA_VERSION,
            pieces=[
                TemplatePiece.from_dict(piece)
                for piece in raw.get("pieces", [])
                if isinstance(piece, dict)
            ],
            trust_level=_enum_or_raw(TemplateTrustLevel, explicit_trust),
            extends=_parse_extends(raw.get("extends")),
            dependencies=_as_dependency_specs(raw.get("dependencies")),
            conflicts=_as_dependency_specs(raw.get("conflicts")),
            capabilities=TemplateCapabilitySpec.from_dict(raw.get("capabilities")),
            patches=list(raw.get("patches", []))
            if isinstance(raw.get("patches", []), list)
            else [],
            metadata=metadata,
            source_path=Path(source_path) if source_path is not None else None,
            declared_id=declared_id
            if declared_id is not None
            else raw_id
            if raw_id != raw_id.strip()
            else None,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "schema_version": self.schema_version,
            "kind": _enum_value(self.kind),
            "version": self.version,
            "status": _enum_value(self.status),
            "trust_level": _enum_value(self.trust_level),
            "pieces": [piece.to_dict() for piece in self.pieces],
            "dependencies": [dependency.to_value() for dependency in self.dependencies],
            "conflicts": [conflict.to_value() for conflict in self.conflicts],
            "capabilities": self.capabilities.to_dict(),
            "patches": [dict(patch) for patch in self.patches],
            "metadata": dict(self.metadata),
        }
        if self.extends is not None:
            result["extends"] = (
                list(self.extends) if isinstance(self.extends, list) else self.extends
            )
        return result


@dataclass
class TemplateContext:
    roots: list[Path] = field(default_factory=list)
    defaultspack_root: Path | None = None
    trust_level: TemplateTrustLevel | str = TemplateTrustLevel.LOCAL


@dataclass
class ResolvedTemplate:
    template: RumiTemplate | None
    diagnostics: list[TemplateDiagnostic] = field(default_factory=list)
    ancestry: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.template is not None and not any(
            diagnostic.is_error for diagnostic in self.diagnostics
        )


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return []


def _as_dependency_specs(value: Any) -> list[TemplateDependencySpec]:
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        return []
    return [
        spec
        for spec in (
            TemplateDependencySpec.from_value(item)
            for item in values
            if isinstance(item, (str, dict))
        )
        if spec.id
    ]


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _enum_or_raw(enum_type: type[_TemplateEnum], value: Any) -> _TemplateEnum | str:
    raw = value.value if isinstance(value, Enum) else str(value)
    try:
        return enum_type(raw)
    except ValueError:
        return raw


def _enum_value(value: Enum | str) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def _parse_extends(value: Any) -> str | list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return str(value)
