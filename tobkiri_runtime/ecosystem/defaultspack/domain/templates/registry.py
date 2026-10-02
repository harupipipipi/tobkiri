from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import List

from .discovery import TemplateRoot, discover_templates
from .models import RumiTemplate, TemplateDiagnostic, TemplateKind, TemplateStatus
from .validation import validate_template


class TemplateRegistry:
    def __init__(self) -> None:
        self._templates: dict[str, RumiTemplate] = {}
        self._diagnostics: dict[str, list[TemplateDiagnostic]] = defaultdict(list)

    def register(
        self, template: RumiTemplate, *, validate: bool = True
    ) -> list[TemplateDiagnostic]:
        diagnostics: list[TemplateDiagnostic] = []
        template_id = str(template.id or "").strip()
        if template_id != str(template.id or ""):
            diagnostics.append(
                TemplateDiagnostic(
                    code="template.registry.noncanonical_template_id",
                    message="template id must not include leading or trailing whitespace",
                    template_id=template_id or None,
                    source_path=str(template.source_path) if template.source_path else None,
                    path="/id",
                )
            )
            self._diagnostics[template_id or str(template.id or "")].extend(diagnostics)
            return diagnostics

        if template_id in self._templates:
            existing = self._templates[template_id]
            diagnostics.append(
                TemplateDiagnostic(
                    code="template.registry.duplicate_template",
                    message=(
                        f"duplicate template id rejected: {template_id}; "
                        f"existing source={existing.source_path}, duplicate source={template.source_path}"
                    ),
                    template_id=template_id,
                    source_path=str(template.source_path) if template.source_path else None,
                )
            )
            self._diagnostics[template_id].extend(diagnostics)
            return diagnostics
        if validate:
            diagnostics.extend(validate_template(template))
        self._templates[template_id] = template
        self._diagnostics[template_id].extend(diagnostics)
        return diagnostics

    def list(
        self,
        *,
        kind: TemplateKind | str | None = None,
        status: TemplateStatus | str | None = None,
    ) -> list[RumiTemplate]:
        templates = list(self._templates.values())
        if kind is not None:
            kind_value = kind.value if isinstance(kind, TemplateKind) else str(kind)
            templates = [template for template in templates if _value(template.kind) == kind_value]
        if status is not None:
            status_value = status.value if isinstance(status, TemplateStatus) else str(status)
            templates = [
                template for template in templates if _value(template.status) == status_value
            ]
        return sorted(templates, key=lambda template: template.id)

    def get(self, template_id: str) -> RumiTemplate | None:
        return self._templates.get(template_id)

    def diagnostics(self, template_id: str | None = None) -> List[TemplateDiagnostic]:
        if template_id is not None:
            return list(self._diagnostics.get(template_id, []))
        diagnostics: list[TemplateDiagnostic] = []
        for template_diagnostics in self._diagnostics.values():
            diagnostics.extend(template_diagnostics)
        return diagnostics

    def clear(self) -> None:
        self._templates.clear()
        self._diagnostics.clear()


def build_template_registry(
    roots: list[str | Path | TemplateRoot] | None = None,
    *,
    defaultspack_root: str | None = None,
) -> tuple[TemplateRegistry, list[TemplateDiagnostic]]:
    registry = TemplateRegistry()
    discovered = discover_templates(roots, defaultspack_root=defaultspack_root)
    diagnostics = list(discovered.diagnostics)
    for template in discovered.templates:
        diagnostics.extend(registry.register(template, validate=False))
    return registry, diagnostics


def _value(value: object) -> str:
    return value.value if hasattr(value, "value") else str(value)
