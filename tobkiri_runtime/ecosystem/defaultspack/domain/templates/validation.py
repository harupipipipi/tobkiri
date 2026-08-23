from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from .models import (
    CURRENT_TEMPLATE_SCHEMA_VERSION,
    RumiTemplate,
    TemplateDiagnostic,
    TemplateKind,
    TemplatePieceKind,
    TemplateStatus,
    TemplateTrustLevel,
)
from .security import assess_template_security


BUILTIN_SETTINGS_FIELD_RENDERERS = {
    "api_key_setup",
    "checkbox",
    "model_api_routes",
    "model_select",
    "number",
    "provider_select",
    "select",
    "text",
    "textarea",
    "toggle",
}
BUILTIN_SHELL_RENDERERS = {
    "activity_preview",
    "chat_header",
    "chat_messages",
    "composer",
    "history_board",
    "right_sidebar",
    "settings_modal",
    "title_bar",
}
BUILTIN_SHELL_REGIONS = {
    "activity_preview",
    "chat_header",
    "chat_messages",
    "composer",
    "history",
    "right_sidebar",
    "settings_modal",
    "title_bar",
}
STATUS_SURFACE_API_VERSION = "rumi.status_surface.v1"
STATUS_SURFACE_SLOTS = {
    "above_composer",
    "below_composer",
    "chat_header",
    "sidebar",
    "workspace_panel",
}
STATUS_SURFACE_CONTROL_KINDS = {
    "button",
    "toggle_button",
    "expand",
    "model_select",
    "provider_select",
    "thinking_select",
    "select",
    "menu",
}
STATUS_SURFACE_ACTION_CONTROL_KINDS = STATUS_SURFACE_CONTROL_KINDS - {"expand"}
TRUSTED_SHELL_RENDERER_MODULE_PREFIXES = (
    "/static/renderers/",
    "/static/assets/renderers/",
    "/static/user_renderers/",
)
TOOL_POLICY_BOOL_FIELDS = ("toggleable", "parallel_tool_calls")
TOOL_POLICY_LIST_FIELDS = (
    "default_enabled_tools",
    "default_disabled_tools",
    "selected_tools",
    "allowed_tools",
    "disabled_tools",
)
TOOL_POLICY_SHAPE_FIELDS = {
    *TOOL_POLICY_BOOL_FIELDS,
    *TOOL_POLICY_LIST_FIELDS,
    "tool_choice",
    "params",
}
TOOL_CHOICE_VALUES = {"auto", "none", "required"}
METADATA_ONLY_EXECUTABLE_REF_FIELDS = (
    "handler",
    "handler_ref",
    "entrypoint",
    "execution",
    "module",
    "qualified_name",
)


@dataclass
class TemplateValidationResult:
    template: RumiTemplate | None
    diagnostics: list[TemplateDiagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.template is not None and not any(
            diagnostic.is_error for diagnostic in self.diagnostics
        )


def parse_template(
    raw: dict[str, Any],
    *,
    source_path: str | None = None,
    trust_level: str | None = None,
    declared_id: str | None = None,
) -> TemplateValidationResult:
    if not isinstance(raw, dict):
        return TemplateValidationResult(
            None,
            [
                TemplateDiagnostic(
                    code="template.invalid_document",
                    message="template document must be a JSON object",
                    source_path=source_path,
                )
            ],
        )

    template = RumiTemplate.from_dict(
        raw, source_path=source_path, trust_level=trust_level, declared_id=declared_id
    )
    return TemplateValidationResult(template, validate_template(template, raw=raw))


def validate_template(
    template: RumiTemplate, *, raw: dict[str, Any] | None = None
) -> list[TemplateDiagnostic]:
    diagnostics: list[TemplateDiagnostic] = []
    diagnostics.extend(_validate_required(template, raw=raw))
    diagnostics.extend(_validate_schema_version(template, raw=raw))
    diagnostics.extend(_validate_template_version(template))
    diagnostics.extend(_validate_dependency_specs(template, raw=raw))
    diagnostics.extend(_validate_canonical_identity(template, raw=raw))
    diagnostics.extend(_validate_enums(template))
    diagnostics.extend(_validate_pieces(template, raw=raw))
    diagnostics.extend(_validate_references(template))
    diagnostics.extend(assess_template_security(template))
    return diagnostics


def _validate_schema_version(
    template: RumiTemplate, *, raw: dict[str, Any] | None
) -> list[TemplateDiagnostic]:
    diagnostics: list[TemplateDiagnostic] = []
    source_path = str(template.source_path) if template.source_path else None
    if raw is not None and "schema_version" not in raw:
        diagnostics.append(
            TemplateDiagnostic(
                code="template.schema_version.implicit_v1",
                message="missing schema_version is treated as legacy v1",
                severity="info",
                template_id=template.id or None,
                path="/schema_version",
                source_path=source_path,
            )
        )
        return diagnostics
    raw_value = raw.get("schema_version") if raw is not None else template.schema_version
    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        diagnostics.append(
            TemplateDiagnostic(
                code="template.schema_version.invalid",
                message="schema_version must be an integer",
                template_id=template.id or None,
                path="/schema_version",
                source_path=source_path,
            )
        )
        return diagnostics
    if raw_value > CURRENT_TEMPLATE_SCHEMA_VERSION:
        diagnostics.append(
            TemplateDiagnostic(
                code="template.schema_version.unsupported",
                message=(
                    f"template schema_version is newer than this defaultspack supports: {raw_value}"
                ),
                template_id=template.id or None,
                path="/schema_version",
                source_path=source_path,
            )
        )
    elif raw_value < 1:
        diagnostics.append(
            TemplateDiagnostic(
                code="template.schema_version.unsupported",
                message=f"template schema_version is unsupported: {raw_value}",
                template_id=template.id or None,
                path="/schema_version",
                source_path=source_path,
            )
        )
    return diagnostics


def _validate_template_version(template: RumiTemplate) -> list[TemplateDiagnostic]:
    try:
        Version(str(template.version))
    except InvalidVersion:
        return [
            TemplateDiagnostic(
                code="template.version.invalid",
                message=f"template version is invalid: {template.version}",
                template_id=template.id or None,
                path="/version",
                source_path=str(template.source_path) if template.source_path else None,
            )
        ]
    return []


def _validate_dependency_specs(
    template: RumiTemplate, *, raw: dict[str, Any] | None
) -> list[TemplateDiagnostic]:
    diagnostics: list[TemplateDiagnostic] = []
    source_path = str(template.source_path) if template.source_path else None
    raw = raw if isinstance(raw, dict) else {}
    for field_name, parsed_specs in (
        ("dependencies", template.dependencies),
        ("conflicts", template.conflicts),
    ):
        raw_value = raw.get(field_name)
        raw_items: list[Any]
        if raw_value is None:
            raw_items = []
        elif isinstance(raw_value, list):
            raw_items = raw_value
        elif isinstance(raw_value, (str, dict)):
            raw_items = [raw_value]
        else:
            diagnostics.append(
                TemplateDiagnostic(
                    code=f"template.{field_name}.invalid",
                    message=f"{field_name} must be a string, object, or list",
                    template_id=template.id or None,
                    path=f"/{field_name}",
                    source_path=source_path,
                )
            )
            raw_items = []
        for index, item in enumerate(raw_items):
            path = f"/{field_name}/{index}"
            if isinstance(item, str):
                if not item.strip():
                    diagnostics.append(
                        TemplateDiagnostic(
                            code=f"template.{field_name}.missing_id",
                            message=f"{field_name} entry must include a non-empty id",
                            template_id=template.id or None,
                            path=path,
                            source_path=source_path,
                        )
                    )
                continue
            if not isinstance(item, dict):
                diagnostics.append(
                    TemplateDiagnostic(
                        code=f"template.{field_name}.invalid_entry",
                        message=f"{field_name} entries must be strings or objects",
                        template_id=template.id or None,
                        path=path,
                        source_path=source_path,
                    )
                )
                continue
            if not str(item.get("id") or "").strip():
                diagnostics.append(
                    TemplateDiagnostic(
                        code=f"template.{field_name}.missing_id",
                        message=f"{field_name} entry must include a non-empty id",
                        template_id=template.id or None,
                        path=f"{path}/id",
                        source_path=source_path,
                    )
                )
            if "optional" in item and not isinstance(item.get("optional"), bool):
                diagnostics.append(
                    TemplateDiagnostic(
                        code=f"template.{field_name}.invalid_optional",
                        message=f"{field_name}.optional must be boolean",
                        template_id=template.id or None,
                        path=f"{path}/optional",
                        source_path=source_path,
                    )
                )
        for index, spec in enumerate(parsed_specs):
            if spec.version is None:
                continue
            try:
                SpecifierSet(str(spec.version))
            except InvalidSpecifier:
                diagnostics.append(
                    TemplateDiagnostic(
                        code="template.dependency.invalid_version_specifier",
                        message=f"invalid dependency version specifier: {spec.version}",
                        template_id=template.id or None,
                        path=f"/{field_name}/{index}/version",
                        source_path=source_path,
                    )
                )
    return diagnostics


def has_errors(diagnostics: list[TemplateDiagnostic]) -> bool:
    return any(diagnostic.is_error for diagnostic in diagnostics)


def _validate_required(
    template: RumiTemplate, *, raw: dict[str, Any] | None
) -> list[TemplateDiagnostic]:
    diagnostics: list[TemplateDiagnostic] = []
    required = {
        "id": template.id,
        "kind": template.kind,
        "version": template.version,
        "status": template.status,
    }
    for field_name, value in required.items():
        if value is None or value == "":
            diagnostics.append(
                TemplateDiagnostic(
                    code="template.missing_required",
                    message=f"{field_name} is required",
                    template_id=template.id or None,
                    path=f"/{field_name}",
                    source_path=str(template.source_path) if template.source_path else None,
                )
            )

    raw_pieces = raw.get("pieces") if raw is not None else None
    if raw is not None and "pieces" not in raw:
        diagnostics.append(
            TemplateDiagnostic(
                code="template.missing_required",
                message="pieces is required",
                template_id=template.id or None,
                path="/pieces",
                source_path=str(template.source_path) if template.source_path else None,
            )
        )
    elif raw_pieces is not None and not isinstance(raw_pieces, list):
        diagnostics.append(
            TemplateDiagnostic(
                code="template.invalid_pieces",
                message="pieces must be a list",
                template_id=template.id or None,
                path="/pieces",
                source_path=str(template.source_path) if template.source_path else None,
            )
        )
    return diagnostics


def _validate_canonical_identity(
    template: RumiTemplate, *, raw: dict[str, Any] | None
) -> list[TemplateDiagnostic]:
    template_id = str(template.id or "")
    if template_id != template_id.strip():
        return [
            TemplateDiagnostic(
                code="template.invalid_id",
                message="template id must not include leading or trailing whitespace",
                template_id=template_id.strip() or None,
                path="/id",
                source_path=str(template.source_path) if template.source_path else None,
            )
        ]

    declared_id = template.declared_id
    raw_id = raw.get("id") if raw is not None and "id" in raw else declared_id
    if not isinstance(raw_id, str):
        return []
    if raw_id == raw_id.strip():
        return []
    return [
        TemplateDiagnostic(
            code="template.invalid_id",
            message="template id must not include leading or trailing whitespace",
            template_id=template.id or None,
            path="/id",
            source_path=str(template.source_path) if template.source_path else None,
        )
    ]


def _validate_references(template: RumiTemplate) -> list[TemplateDiagnostic]:
    diagnostics: list[TemplateDiagnostic] = []
    trust_level = _value(template.trust_level)
    renderer_types: set[str] = set(BUILTIN_SETTINGS_FIELD_RENDERERS)
    shell_renderer_ids: set[str] = set(BUILTIN_SHELL_RENDERERS)
    shell_region_ids: set[str] = set(BUILTIN_SHELL_REGIONS)
    composer_input_ids: set[str] = set()
    context_policy_ids: set[str] = set()
    tool_policy_ids: set[str] = set()
    permission_ids: set[str] = set()
    command_ids: set[str] = set()
    action_ids: dict[str, str] = {}
    data_source_ids: dict[str, str] = {}

    for piece in template.pieces:
        kind = _value(piece.kind)
        if kind == TemplatePieceKind.SHELL_RENDERER.value:
            renderer_id = _piece_payload_id(piece, "renderer", "renderer_id", "shell_renderer_id")
            if renderer_id:
                shell_renderer_ids.add(renderer_id)
        elif kind == TemplatePieceKind.SHELL_REGION.value:
            region_id = _piece_payload_id(piece, "region", "region_id", "shell_region_id")
            if region_id:
                shell_region_ids.add(region_id)
        elif kind == TemplatePieceKind.COMPOSER_INPUT.value:
            for input_id in _payload_ids(_piece_payload(piece, "input"), piece, "input_id"):
                composer_input_ids.add(input_id)
        elif kind == TemplatePieceKind.COMPOSER_COMMAND.value:
            command = _piece_payload(piece, "command")
            command_ids.update(_payload_ids(command, piece, "command_id", "name"))
        elif kind == TemplatePieceKind.CONTEXT_POLICY.value:
            for policy_id in _payload_ids(
                _piece_payload(piece, "policy"), piece, "policy_id", "mode"
            ):
                context_policy_ids.add(policy_id)
        elif kind == TemplatePieceKind.TOOL_POLICY.value:
            for policy_id in _payload_ids(
                _tool_policy_payload(piece), piece, "policy_id", "tool_policy_id"
            ):
                tool_policy_ids.add(policy_id)

    for index, piece in enumerate(template.pieces):
        kind = _value(piece.kind)
        if trust_level != TemplateTrustLevel.BUILTIN.value:
            for field_name, value in (
                ("entrypoint", piece.entrypoint),
                ("handler_ref", piece.data.get("handler_ref")),
            ):
                if not str(value or "").strip():
                    continue
                diagnostics.append(
                    TemplateDiagnostic(
                        code="template.reference.non_builtin_handler_not_executable",
                        message=f"{field_name} is metadata only for non-builtin templates and is not executable by default",
                        severity="warning",
                        template_id=template.id,
                        piece_id=piece.id,
                        path=f"/pieces/{index}/{field_name}",
                        source_path=str(template.source_path) if template.source_path else None,
                    )
                )

        if kind == TemplatePieceKind.FIELD_RENDERER.value:
            field_types = _field_renderer_types(piece.data)
            if not field_types:
                diagnostics.append(
                    TemplateDiagnostic(
                        code="template.reference.field_renderer_missing_field_types",
                        message="field_renderer.field_types must declare at least one field type",
                        template_id=template.id,
                        piece_id=piece.id,
                        path=f"/pieces/{index}/field_types",
                        source_path=str(template.source_path) if template.source_path else None,
                    )
                )
            renderer_types.update(field_types)

        if kind == TemplatePieceKind.COMPOSER_COMMAND.value:
            diagnostics.extend(_validate_composer_command(template, piece, index))

        if kind == TemplatePieceKind.COMPOSER_INPUT.value:
            diagnostics.extend(
                _validate_composer_input(
                    template,
                    piece,
                    index,
                    shell_region_ids=shell_region_ids,
                    shell_renderer_ids=shell_renderer_ids,
                )
            )

        if kind == TemplatePieceKind.SHELL_RENDERER.value:
            diagnostics.extend(
                _validate_shell_renderer(template, piece, index, trust_level=trust_level)
            )

        if kind == TemplatePieceKind.SHELL_REGION.value:
            diagnostics.extend(
                _validate_shell_region(
                    template,
                    piece,
                    index,
                    shell_renderer_ids=shell_renderer_ids,
                )
            )

        if kind == TemplatePieceKind.CONTEXT_POLICY.value:
            diagnostics.extend(_validate_context_policy(template, piece, index))

        if kind == TemplatePieceKind.EXTERNAL_IO_TEMPLATE.value:
            diagnostics.extend(_validate_external_io_template(template, piece, index))

        if kind == TemplatePieceKind.TOOL_POLICY.value:
            diagnostics.extend(_validate_tool_policy(template, piece, index))

        if kind == TemplatePieceKind.AI_INPUT.value:
            diagnostics.extend(
                _validate_ai_input(
                    template,
                    piece,
                    index,
                    composer_input_ids=composer_input_ids,
                    context_policy_ids=context_policy_ids,
                    tool_policy_ids=tool_policy_ids,
                )
            )

        if kind == TemplatePieceKind.PERMISSION.value:
            permission_id = str(piece.data.get("permission_id") or piece.id or "").strip()
            if permission_id:
                permission_ids.add(permission_id)

        if _is_action_piece(kind, piece.data):
            _record_unique_id(
                diagnostics,
                action_ids,
                str(
                    piece.data.get("action_id") or piece.data.get("command_id") or piece.id or ""
                ).strip(),
                template=template,
                piece_id=piece.id,
                path=f"/pieces/{index}/action_id",
                code="template.reference.duplicate_action_id",
                label="action id",
            )

        if _is_data_source_piece(kind, piece.data):
            _record_unique_id(
                diagnostics,
                data_source_ids,
                str(
                    piece.data.get("data_source") or piece.data.get("source") or piece.id or ""
                ).strip(),
                template=template,
                piece_id=piece.id,
                path=f"/pieces/{index}/data_source",
                code="template.reference.duplicate_data_source_id",
                label="data source id",
            )

        route_metadata = piece.data.get("route_metadata")
        if kind == TemplatePieceKind.TEST_CONTRACT.value and isinstance(route_metadata, dict):
            method = str(route_metadata.get("method") or "").strip()
            path = str(route_metadata.get("path") or route_metadata.get("route_path") or "").strip()
            if not method or not path:
                diagnostics.append(
                    TemplateDiagnostic(
                        code="template.reference.route_metadata_missing_method_path",
                        message="test_contract.route_metadata must include method and path",
                        template_id=template.id,
                        piece_id=piece.id,
                        path=f"/pieces/{index}/route_metadata",
                        source_path=str(template.source_path) if template.source_path else None,
                    )
                )

    # Status controls execute only through the resolved Command Protocol.
    # Generic action metadata has no independent execution endpoint here.
    declared_status_actions = command_ids
    for index, piece in enumerate(template.pieces):
        if _value(piece.kind) != TemplatePieceKind.STATUS_SURFACE.value:
            continue
        diagnostics.extend(
            _validate_status_surface(
                template,
                piece,
                index,
                action_ids=declared_status_actions,
                data_source_ids=set(data_source_ids),
            )
        )

    for index, piece in enumerate(template.pieces):
        if _value(piece.kind) != TemplatePieceKind.SETTINGS_FIELD.value:
            continue
        field_type = str(piece.data.get("field_type") or piece.data.get("type") or "").strip()
        if field_type and field_type not in renderer_types:
            diagnostics.append(
                TemplateDiagnostic(
                    code="template.reference.settings_field_renderer_missing",
                    message=f"settings field type has no renderer: {field_type}",
                    template_id=template.id,
                    piece_id=piece.id,
                    path=f"/pieces/{index}/type",
                    source_path=str(template.source_path) if template.source_path else None,
                )
            )

    capability_permissions = set(template.capabilities.permissions)
    for permission_id in sorted(capability_permissions - permission_ids):
        diagnostics.append(
            TemplateDiagnostic(
                code="template.reference.permission_missing_piece",
                message=f"capability permission has no permission piece: {permission_id}",
                template_id=template.id,
                path="/capabilities/permissions",
                source_path=str(template.source_path) if template.source_path else None,
            )
        )
    if capability_permissions:
        for permission_id in sorted(permission_ids - capability_permissions):
            diagnostics.append(
                TemplateDiagnostic(
                    code="template.reference.permission_not_declared",
                    message=f"permission piece is not declared in capabilities.permissions: {permission_id}",
                    template_id=template.id,
                    path="/pieces/*/permission_id",
                    source_path=str(template.source_path) if template.source_path else None,
                )
            )
    return diagnostics


def _validate_enums(template: RumiTemplate) -> list[TemplateDiagnostic]:
    diagnostics: list[TemplateDiagnostic] = []
    if not isinstance(template.kind, TemplateKind):
        diagnostics.append(_invalid_enum(template, "kind", template.kind, TemplateKind))
    if not isinstance(template.status, TemplateStatus):
        diagnostics.append(_invalid_enum(template, "status", template.status, TemplateStatus))
    return diagnostics


def _validate_pieces(
    template: RumiTemplate, *, raw: dict[str, Any] | None
) -> list[TemplateDiagnostic]:
    diagnostics: list[TemplateDiagnostic] = []
    seen: set[str] = set()
    raw_pieces = (
        raw.get("pieces") if raw is not None and isinstance(raw.get("pieces"), list) else []
    )

    for index, piece in enumerate(template.pieces):
        if not piece.id:
            diagnostics.append(
                TemplateDiagnostic(
                    code="template.piece.missing_id",
                    message="piece id is required",
                    template_id=template.id,
                    piece_id=None,
                    path=f"/pieces/{index}/id",
                    source_path=str(template.source_path) if template.source_path else None,
                )
            )
        elif piece.id in seen:
            diagnostics.append(
                TemplateDiagnostic(
                    code="template.piece.duplicate_id",
                    message=f"duplicate piece id: {piece.id}",
                    severity="warning",
                    template_id=template.id,
                    piece_id=piece.id,
                    path=f"/pieces/{index}/id",
                    source_path=str(template.source_path) if template.source_path else None,
                )
            )
        seen.add(piece.id)

        if not isinstance(piece.kind, TemplatePieceKind):
            diagnostics.append(
                TemplateDiagnostic(
                    code="template.piece.invalid_kind",
                    message=f"unsupported piece kind: {piece.kind}",
                    template_id=template.id,
                    piece_id=piece.id or None,
                    path=f"/pieces/{index}/kind",
                    source_path=str(template.source_path) if template.source_path else None,
                )
            )

    if raw_pieces and len(raw_pieces) != len(template.pieces):
        diagnostics.append(
            TemplateDiagnostic(
                code="template.piece.invalid_item",
                message="all pieces must be JSON objects",
                template_id=template.id,
                path="/pieces",
                source_path=str(template.source_path) if template.source_path else None,
            )
        )
    return diagnostics


def _invalid_enum(
    template: RumiTemplate, field_name: str, value: object, enum_type: type[Enum]
) -> TemplateDiagnostic:
    allowed = ", ".join(item.value for item in enum_type)
    return TemplateDiagnostic(
        code=f"template.invalid_{field_name}",
        message=f"unsupported {field_name}: {value}; allowed: {allowed}",
        template_id=template.id or None,
        path=f"/{field_name}",
        source_path=str(template.source_path) if template.source_path else None,
    )


def _validate_composer_command(
    template: RumiTemplate,
    piece: Any,
    index: int,
) -> list[TemplateDiagnostic]:
    diagnostics: list[TemplateDiagnostic] = []
    command = _piece_payload(piece, "command")
    command_id = _payload_id(command, piece, "command_id")
    if not command_id:
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.command_missing_id_name",
                message="composer_command must include id or name",
                field="id",
                nested_key="command",
            )
        )

    execution = command.get("execution")
    if not isinstance(execution, dict) or not execution:
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.command_missing_execution",
                message="composer_command.execution must be a non-empty object",
                field="execution",
                nested_key="command",
            )
        )
        return diagnostics

    execution_type = str(execution.get("type") or "").strip()
    if not execution_type:
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.command_execution_missing_type",
                message="composer_command.execution.type is required",
                field="execution/type",
                nested_key="command",
            )
        )
    if (
        execution_type in {"pack_block", "rumi_function"}
        and not str(execution.get("qualified_name") or "").strip()
    ):
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.command_execution_missing_qualified_name",
                message=f"composer_command.execution.qualified_name is required for {execution_type}",
                field="execution/qualified_name",
                nested_key="command",
            )
        )
    return diagnostics


def _validate_status_surface(
    template: RumiTemplate,
    piece: Any,
    index: int,
    *,
    action_ids: set[str],
    data_source_ids: set[str],
) -> list[TemplateDiagnostic]:
    """Validate one feature-neutral status surface and its local bindings."""

    diagnostics = _validate_nested_object(template, piece, index, "surface")
    surface = _piece_payload(piece, "surface")
    surface_id = str(surface.get("surface_id") or surface.get("id") or piece.id).strip()
    if not _valid_status_surface_id(surface_id):
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.status_surface_invalid_id",
                message="status_surface id must be a bounded opaque registry id",
                field="id",
                nested_key="surface",
            )
        )

    api_version = str(surface.get("api_version") or STATUS_SURFACE_API_VERSION).strip()
    if api_version != STATUS_SURFACE_API_VERSION:
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.status_surface_unsupported_version",
                message=f"unsupported status_surface api_version: {api_version}",
                field="api_version",
                nested_key="surface",
            )
        )

    slot = str(surface.get("slot") or piece.slot or "above_composer").strip()
    if slot not in STATUS_SURFACE_SLOTS:
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.status_surface_invalid_slot",
                message=f"status_surface slot is not approved: {slot}",
                field="slot",
                nested_key="surface",
            )
        )

    data_source_id = str(surface.get("data_source") or surface.get("dataSource") or "").strip()
    if not _valid_status_surface_id(data_source_id):
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.status_surface_missing_data_source",
                message="status_surface must bind a registered pack-owned data source",
                field="data_source",
                nested_key="surface",
            )
        )
    elif data_source_id not in data_source_ids:
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.status_surface_unknown_data_source",
                message=(
                    f"status_surface references an unknown pack-owned data source: {data_source_id}"
                ),
                field="data_source",
                nested_key="surface",
            )
        )

    for field_name in (
        "title_path",
        "summary_path",
        "status_path",
        "severity_path",
        "timer_from_path",
        "count_path",
    ):
        if field_name in surface and not _valid_status_surface_path(surface[field_name]):
            diagnostics.append(
                _piece_diagnostic(
                    template,
                    piece,
                    index,
                    code="template.reference.status_surface_invalid_path",
                    message=f"status_surface has an invalid safe path: {field_name}",
                    field=field_name,
                    nested_key="surface",
                )
            )

    progress = surface.get("progress")
    if progress is not None:
        if not isinstance(progress, dict):
            diagnostics.append(
                _piece_diagnostic(
                    template,
                    piece,
                    index,
                    code="template.reference.status_surface_invalid_progress",
                    message="status_surface progress must be an object",
                    field="progress",
                    nested_key="surface",
                )
            )
        else:
            for field_name in ("current_path", "total_path", "label_path"):
                required_path_missing = (
                    field_name in {"current_path", "total_path"} and field_name not in progress
                )
                if required_path_missing or (
                    field_name in progress and not _valid_status_surface_path(progress[field_name])
                ):
                    diagnostics.append(
                        _piece_diagnostic(
                            template,
                            piece,
                            index,
                            code="template.reference.status_surface_invalid_path",
                            message=(
                                f"status_surface progress has an invalid safe path: {field_name}"
                            ),
                            field=f"progress/{field_name}",
                            nested_key="surface",
                        )
                    )

    visible_when = surface.get("visible_when")
    if visible_when is not None:
        if not isinstance(visible_when, dict):
            diagnostics.append(
                _piece_diagnostic(
                    template,
                    piece,
                    index,
                    code="template.reference.status_surface_invalid_visibility",
                    message="status_surface visible_when must be an object",
                    field="visible_when",
                    nested_key="surface",
                )
            )
        else:
            for path in visible_when:
                if not _valid_status_surface_path(path):
                    diagnostics.append(
                        _piece_diagnostic(
                            template,
                            piece,
                            index,
                            code="template.reference.status_surface_invalid_path",
                            message=("status_surface visible_when has an invalid safe path"),
                            field=f"visible_when/{path}",
                            nested_key="surface",
                        )
                    )

    details = surface.get("details")
    if details is not None:
        if not isinstance(details, list) or len(details) > 12:
            diagnostics.append(
                _piece_diagnostic(
                    template,
                    piece,
                    index,
                    code="template.reference.status_surface_invalid_details",
                    message="status_surface details must contain at most 12 entries",
                    field="details",
                    nested_key="surface",
                )
            )
        elif any(
            isinstance(detail, dict)
            and "path" in detail
            and not _valid_status_surface_path(detail["path"])
            for detail in details
        ):
            diagnostics.append(
                _piece_diagnostic(
                    template,
                    piece,
                    index,
                    code="template.reference.status_surface_invalid_path",
                    message="status_surface detail has an invalid safe path",
                    field="details",
                    nested_key="surface",
                )
            )

    controls = surface.get("controls", [])
    if not isinstance(controls, list) or len(controls) > 20:
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.status_surface_invalid_controls",
                message="status_surface controls must contain at most 20 entries",
                field="controls",
                nested_key="surface",
            )
        )
        return diagnostics

    seen_control_ids: set[str] = set()
    for control_index, control in enumerate(controls[:20]):
        if not isinstance(control, dict):
            diagnostics.append(
                _piece_diagnostic(
                    template,
                    piece,
                    index,
                    code="template.reference.status_surface_invalid_control",
                    message=f"status_surface control {control_index} must be an object",
                    field=f"controls/{control_index}",
                    nested_key="surface",
                )
            )
            continue
        kind = str(control.get("type") or "").strip()
        if kind not in STATUS_SURFACE_CONTROL_KINDS:
            diagnostics.append(
                _piece_diagnostic(
                    template,
                    piece,
                    index,
                    code="template.reference.status_surface_unknown_control",
                    message=f"unsupported status_surface control: {kind or 'missing'}",
                    field=f"controls/{control_index}/type",
                    nested_key="surface",
                )
            )
            continue
        configured_control_id = control.get("id")
        control_id = str(configured_control_id or f"{kind}_{control_index}").strip()
        if configured_control_id is not None and not _valid_status_surface_id(control_id):
            diagnostics.append(
                _piece_diagnostic(
                    template,
                    piece,
                    index,
                    code="template.reference.status_surface_invalid_control_id",
                    message="status_surface control id must be a bounded opaque registry id",
                    field=f"controls/{control_index}/id",
                    nested_key="surface",
                )
            )
        if control_id in seen_control_ids:
            diagnostics.append(
                _piece_diagnostic(
                    template,
                    piece,
                    index,
                    code="template.reference.status_surface_duplicate_control",
                    message=f"duplicate status_surface control id: {control_id}",
                    field=f"controls/{control_index}/id",
                    nested_key="surface",
                )
            )
        seen_control_ids.add(control_id)
        for path_field in ("value_path", "disabled_path", "options_path"):
            if path_field in control and not _valid_status_surface_path(control[path_field]):
                diagnostics.append(
                    _piece_diagnostic(
                        template,
                        piece,
                        index,
                        code="template.reference.status_surface_invalid_path",
                        message=(f"status_surface control has an invalid safe path: {path_field}"),
                        field=f"controls/{control_index}/{path_field}",
                        nested_key="surface",
                    )
                )
        if kind not in STATUS_SURFACE_ACTION_CONTROL_KINDS:
            continue
        action_id = str(control.get("action_id") or control.get("actionId") or "").strip()
        if action_id not in action_ids:
            diagnostics.append(
                _piece_diagnostic(
                    template,
                    piece,
                    index,
                    code="template.reference.status_surface_unknown_action",
                    message=(
                        "status_surface control references an unknown pack-owned action: "
                        f"{action_id or 'missing'}"
                    ),
                    field=f"controls/{control_index}/action_id",
                    nested_key="surface",
                )
            )
    return diagnostics


def _valid_status_surface_id(value: str) -> bool:
    if not value or len(value) > 128 or not value[0].isalnum():
        return False
    return all(character.isalnum() or character in "._:-" for character in value)


def _valid_status_surface_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 256:
        return False
    segments = value.split(".")
    if len(segments) > 12:
        return False
    blocked = {"__proto__", "constructor", "prototype"}
    return all(
        bool(segment)
        and segment not in blocked
        and len(segment) <= 64
        and (segment[0].isalpha() or segment[0] == "_")
        and all(character.isalnum() or character in "_-" for character in segment)
        for segment in segments
    )


def _validate_composer_input(
    template: RumiTemplate,
    piece: Any,
    index: int,
    *,
    shell_region_ids: set[str],
    shell_renderer_ids: set[str],
) -> list[TemplateDiagnostic]:
    data = _piece_payload(piece, "input")
    diagnostics: list[TemplateDiagnostic] = []
    region_id = _first_string(data, "region_id", "shell_region_id", "shell_region", "region")
    renderer_id = _first_string(data, "renderer_id", "shell_renderer_id", "renderer")
    if not region_id and not renderer_id:
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.composer_input_missing_reference",
                message="composer_input must reference a region_id, shell region, or renderer",
                field="region_id",
                nested_key="input",
            )
        )
    if region_id and region_id not in shell_region_ids:
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.composer_input_unknown_region",
                message=f"composer_input references unknown shell region: {region_id}",
                field="region_id",
                nested_key="input",
            )
        )
    if renderer_id and renderer_id not in shell_renderer_ids:
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.composer_input_unknown_renderer",
                message=f"composer_input references unknown shell renderer: {renderer_id}",
                field="renderer",
                nested_key="input",
            )
        )
    return diagnostics


def _validate_shell_renderer(
    template: RumiTemplate,
    piece: Any,
    index: int,
    *,
    trust_level: str,
) -> list[TemplateDiagnostic]:
    data = _piece_payload(piece, "renderer")
    diagnostics: list[TemplateDiagnostic] = []
    module = data.get("module")
    if module is None:
        return diagnostics
    if trust_level != TemplateTrustLevel.BUILTIN.value:
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.shell_renderer_module_requires_builtin",
                message="shell_renderer.module is only executable for builtin templates",
                field="module",
                nested_key="renderer",
            )
        )
    if not _is_trusted_renderer_module(module):
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.shell_renderer_untrusted_module",
                message="shell_renderer.module must be a trusted static renderer path",
                field="module",
                nested_key="renderer",
            )
        )
    if data.get("trust") != "local":
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.shell_renderer_missing_local_trust",
                message="shell_renderer.module requires trust='local'",
                field="trust",
                nested_key="renderer",
            )
        )
    return diagnostics


def _validate_shell_region(
    template: RumiTemplate,
    piece: Any,
    index: int,
    *,
    shell_renderer_ids: set[str],
) -> list[TemplateDiagnostic]:
    data = _piece_payload(piece, "region")
    renderer_id = str(
        data.get("renderer") or data.get("renderer_id") or data.get("shell_renderer_id") or ""
    ).strip()
    if not renderer_id or renderer_id in shell_renderer_ids:
        return []
    return [
        _piece_diagnostic(
            template,
            piece,
            index,
            code="template.reference.shell_region_unknown_renderer",
            message=f"shell_region references unknown shell renderer: {renderer_id}",
            field="renderer",
            nested_key="region",
        )
    ]


def _validate_context_policy(
    template: RumiTemplate,
    piece: Any,
    index: int,
) -> list[TemplateDiagnostic]:
    data = _piece_payload(piece, "policy")
    mode = str(data.get("mode") or data.get("policy_mode") or "").strip()
    if mode:
        return []
    return [
        _piece_diagnostic(
            template,
            piece,
            index,
            code="template.reference.context_policy_missing_mode",
            message="context_policy.mode is required",
            field="mode",
            nested_key="policy",
        )
    ]


def _validate_external_io_template(
    template: RumiTemplate,
    piece: Any,
    index: int,
) -> list[TemplateDiagnostic]:
    diagnostics: list[TemplateDiagnostic] = []
    diagnostics.extend(_validate_nested_object(template, piece, index, "template"))
    data = _piece_payload(piece, "template")
    template_id = str(data.get("template_id") or data.get("id") or data.get("name") or "").strip()
    direction = str(data.get("direction") or "").strip().lower()
    provider = str(data.get("provider") or "").strip()
    if not template_id:
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.external_io_template_missing_id",
                message="external_io_template must include id or template_id",
                field="id",
                nested_key="template",
            )
        )
    if direction not in {"input", "output"}:
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.external_io_template_invalid_direction",
                message="external_io_template.direction must be input or output",
                field="direction",
                nested_key="template",
            )
        )
    if not provider:
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.external_io_template_missing_provider",
                message="external_io_template.provider is required",
                field="provider",
                nested_key="template",
            )
        )
    return diagnostics


def _validate_tool_policy(
    template: RumiTemplate,
    piece: Any,
    index: int,
) -> list[TemplateDiagnostic]:
    diagnostics: list[TemplateDiagnostic] = []
    diagnostics.extend(_validate_nested_object(template, piece, index, "policy"))
    diagnostics.extend(_validate_nested_object(template, piece, index, "tool_policy"))

    data = _tool_policy_payload(piece)
    policy_id = _payload_id(data, piece, "policy_id", "tool_policy_id")
    if not policy_id:
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.tool_policy_missing_id",
                message="tool_policy must include an id or policy_id",
                field="id",
                nested_key=_tool_policy_nested_key(piece),
            )
        )

    if not any(field in data for field in TOOL_POLICY_SHAPE_FIELDS):
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.tool_policy_empty",
                message="tool_policy must declare at least one policy field",
                field="policy",
                nested_key=_tool_policy_nested_key(piece),
            )
        )

    for field_name in TOOL_POLICY_BOOL_FIELDS:
        if field_name in data and not isinstance(data.get(field_name), bool):
            diagnostics.append(
                _piece_diagnostic(
                    template,
                    piece,
                    index,
                    code="template.reference.tool_policy_invalid_boolean",
                    message=f"tool_policy.{field_name} must be boolean",
                    field=field_name,
                    nested_key=_tool_policy_nested_key(piece),
                )
            )

    for field_name in TOOL_POLICY_LIST_FIELDS:
        if field_name in data and not _is_string_list(data.get(field_name)):
            diagnostics.append(
                _piece_diagnostic(
                    template,
                    piece,
                    index,
                    code="template.reference.tool_policy_invalid_string_list",
                    message=f"tool_policy.{field_name} must be a list of non-empty strings",
                    field=field_name,
                    nested_key=_tool_policy_nested_key(piece),
                )
            )

    if "tool_choice" in data and not _valid_tool_choice(data.get("tool_choice")):
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.tool_policy_invalid_tool_choice",
                message="tool_policy.tool_choice must be auto, none, required, or a JSON object",
                field="tool_choice",
                nested_key=_tool_policy_nested_key(piece),
            )
        )

    if "params" in data and not isinstance(data.get("params"), dict):
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.tool_policy_invalid_params",
                message="tool_policy.params must be an object",
                field="params",
                nested_key=_tool_policy_nested_key(piece),
            )
        )

    diagnostics.extend(
        _diagnose_metadata_executable_refs(
            template,
            piece,
            index,
            data,
            nested_key=_tool_policy_nested_key(piece),
            code="template.reference.tool_policy_executable_ref",
            label="tool_policy",
        )
    )
    return diagnostics


def _validate_ai_input(
    template: RumiTemplate,
    piece: Any,
    index: int,
    *,
    composer_input_ids: set[str],
    context_policy_ids: set[str],
    tool_policy_ids: set[str],
) -> list[TemplateDiagnostic]:
    diagnostics: list[TemplateDiagnostic] = []
    diagnostics.extend(_validate_nested_object(template, piece, index, "input"))
    diagnostics.extend(_validate_nested_object(template, piece, index, "ai_input"))

    data = _ai_input_payload(piece)
    composer_refs = _reference_list(data, "composer_input", "composer_input_id")
    context_refs = _reference_list(data, "context_policy", "context_policy_id")
    tool_policy_refs = _reference_list(data, "tool_policy", "tool_policy_id")

    if not (composer_refs or context_refs or tool_policy_refs or "params" in data):
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.ai_input_missing_binding",
                message="ai_input must reference composer_input, context_policy, tool_policy, or params",
                field="input",
                nested_key=_ai_input_nested_key(piece),
            )
        )

    if "params" in data and not isinstance(data.get("params"), dict):
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code="template.reference.ai_input_invalid_params",
                message="ai_input.params must be an object",
                field="params",
                nested_key=_ai_input_nested_key(piece),
            )
        )

    diagnostics.extend(
        _diagnose_unknown_references(
            template,
            piece,
            index,
            refs=composer_refs,
            declared_ids=composer_input_ids,
            code="template.reference.ai_input_unknown_composer_input",
            label="composer_input",
            field="composer_input",
            nested_key=_ai_input_nested_key(piece),
        )
    )
    diagnostics.extend(
        _diagnose_unknown_references(
            template,
            piece,
            index,
            refs=context_refs,
            declared_ids=context_policy_ids,
            code="template.reference.ai_input_unknown_context_policy",
            label="context_policy",
            field="context_policy",
            nested_key=_ai_input_nested_key(piece),
        )
    )
    diagnostics.extend(
        _diagnose_unknown_references(
            template,
            piece,
            index,
            refs=tool_policy_refs,
            declared_ids=tool_policy_ids,
            code="template.reference.ai_input_unknown_tool_policy",
            label="tool_policy",
            field="tool_policy",
            nested_key=_ai_input_nested_key(piece),
        )
    )
    diagnostics.extend(
        _diagnose_metadata_executable_refs(
            template,
            piece,
            index,
            data,
            nested_key=_ai_input_nested_key(piece),
            code="template.reference.ai_input_executable_ref",
            label="ai_input",
        )
    )
    return diagnostics


def _field_renderer_types(data: dict[str, Any]) -> set[str]:
    raw = data.get("field_types")
    if isinstance(raw, list):
        return {str(value).strip() for value in raw if str(value or "").strip()}
    field_type = str(data.get("field_type") or data.get("type") or "").strip()
    return {field_type} if field_type else set()


def _is_action_piece(kind: str, data: dict[str, Any]) -> bool:
    return str(data.get("role") or "").strip() == "action" or (
        kind == TemplatePieceKind.FUNCTION.value
        and any(key in data for key in ("action", "action_id", "command_id"))
    )


def _is_data_source_piece(kind: str, data: dict[str, Any]) -> bool:
    return str(data.get("role") or "").strip() == "data_source" or (
        kind == TemplatePieceKind.FUNCTION.value
        and any(key in data for key in ("data_source", "source", "query"))
    )


def _record_unique_id(
    diagnostics: list[TemplateDiagnostic],
    seen: dict[str, str],
    item_id: str,
    *,
    template: RumiTemplate,
    piece_id: str,
    path: str,
    code: str,
    label: str,
) -> None:
    if not item_id:
        return
    if item_id in seen:
        diagnostics.append(
            TemplateDiagnostic(
                code=code,
                message=f"duplicate {label}: {item_id}",
                template_id=template.id,
                piece_id=piece_id,
                path=path,
                source_path=str(template.source_path) if template.source_path else None,
            )
        )
        return
    seen[item_id] = piece_id


def _piece_payload(piece: Any, nested_key: str) -> dict[str, Any]:
    nested = piece.data.get(nested_key)
    if isinstance(nested, dict):
        return dict(nested)
    return dict(piece.data)


def _ai_input_payload(piece: Any) -> dict[str, Any]:
    nested = piece.data.get("ai_input")
    if isinstance(nested, dict):
        return dict(nested)
    return _piece_payload(piece, "input")


def _tool_policy_payload(piece: Any) -> dict[str, Any]:
    nested = piece.data.get("tool_policy")
    if isinstance(nested, dict):
        return dict(nested)
    return _piece_payload(piece, "policy")


def _ai_input_nested_key(piece: Any) -> str:
    return "ai_input" if isinstance(piece.data.get("ai_input"), dict) else "input"


def _tool_policy_nested_key(piece: Any) -> str:
    return "tool_policy" if isinstance(piece.data.get("tool_policy"), dict) else "policy"


def _piece_payload_id(piece: Any, nested_key: str, *aliases: str) -> str:
    return _payload_id(_piece_payload(piece, nested_key), piece, *aliases)


def _payload_id(data: dict[str, Any], piece: Any, *aliases: str) -> str:
    for key in (*aliases, "id", "name"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return str(piece.id or "").strip()


def _payload_ids(data: dict[str, Any], piece: Any, *aliases: str) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for key in (*aliases, "id", "name"):
        value = str(data.get(key) or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ids.append(value)
    piece_id = str(piece.id or "").strip()
    if piece_id and piece_id not in seen:
        ids.append(piece_id)
    return ids


def _first_string(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return ""


def _reference_list(data: dict[str, Any], *keys: str) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for key in keys:
        value = data.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if not isinstance(item, str):
                continue
            ref = item.strip()
            if not ref or ref in seen:
                continue
            seen.add(ref)
            refs.append(ref)
    return refs


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def _valid_tool_choice(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip() in TOOL_CHOICE_VALUES
    return isinstance(value, dict)


def _validate_nested_object(
    template: RumiTemplate,
    piece: Any,
    index: int,
    nested_key: str,
) -> list[TemplateDiagnostic]:
    if nested_key not in piece.data or isinstance(piece.data.get(nested_key), dict):
        return []
    return [
        TemplateDiagnostic(
            code="template.reference.invalid_nested_payload",
            message=f"{nested_key} must be an object",
            template_id=template.id,
            piece_id=piece.id or None,
            path=f"/pieces/{index}/{nested_key}",
            source_path=str(template.source_path) if template.source_path else None,
        )
    ]


def _diagnose_unknown_references(
    template: RumiTemplate,
    piece: Any,
    index: int,
    *,
    refs: list[str],
    declared_ids: set[str],
    code: str,
    label: str,
    field: str,
    nested_key: str,
) -> list[TemplateDiagnostic]:
    diagnostics: list[TemplateDiagnostic] = []
    for ref in refs:
        if ref in declared_ids:
            continue
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code=code,
                message=f"ai_input references unknown {label}: {ref}",
                field=field,
                nested_key=nested_key,
            )
        )
    return diagnostics


def _diagnose_metadata_executable_refs(
    template: RumiTemplate,
    piece: Any,
    index: int,
    data: dict[str, Any],
    *,
    nested_key: str,
    code: str,
    label: str,
) -> list[TemplateDiagnostic]:
    diagnostics: list[TemplateDiagnostic] = []
    for field_name in METADATA_ONLY_EXECUTABLE_REF_FIELDS:
        if field_name not in data:
            continue
        diagnostics.append(
            _piece_diagnostic(
                template,
                piece,
                index,
                code=code,
                message=f"{label} is metadata/policy only and cannot declare executable {field_name}",
                field=field_name,
                nested_key=nested_key,
            )
        )
    return diagnostics


def _piece_diagnostic(
    template: RumiTemplate,
    piece: Any,
    index: int,
    *,
    code: str,
    message: str,
    field: str,
    nested_key: str,
) -> TemplateDiagnostic:
    return TemplateDiagnostic(
        code=code,
        message=message,
        template_id=template.id,
        piece_id=piece.id or None,
        path=_piece_path(piece, index, field, nested_key=nested_key),
        source_path=str(template.source_path) if template.source_path else None,
    )


def _piece_path(piece: Any, index: int, field: str, *, nested_key: str) -> str:
    if isinstance(piece.data.get(nested_key), dict):
        return f"/pieces/{index}/{nested_key}/{field}"
    return f"/pieces/{index}/{field}"


def _is_trusted_renderer_module(module: Any) -> bool:
    return isinstance(module, str) and module.startswith(TRUSTED_SHELL_RENDERER_MODULE_PREFIXES)


def _value(value: object) -> str:
    return value.value if hasattr(value, "value") else str(value)
