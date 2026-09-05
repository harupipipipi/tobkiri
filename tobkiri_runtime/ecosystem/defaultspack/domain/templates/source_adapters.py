from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class TemplateSourceContribution:
    source_kind: str
    source_id: str
    source_path: str
    source_pack_id: str
    trust_level: str
    public_id: str
    bucket: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_catalog_item(self) -> dict[str, Any]:
        projected_id = _projected_id(self.source_kind, self.source_id, self.source_path)
        return {
            "id": projected_id,
            "projected_id": projected_id,
            "public_id": self.public_id,
            "bucket": self.bucket,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "source_path": self.source_path,
            "source_pack_id": self.source_pack_id,
            "trust_level": self.trust_level,
            "metadata": dict(self.metadata),
            "_source": self.source_path,
            "_metadata_only": True,
        }


@dataclass(frozen=True)
class TemplateSourceAdapterDiagnostic:
    code: str
    message: str
    source_kind: str
    source_path: str
    severity: str = "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.severity,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "source": self.source_path,
            "source_path": self.source_path,
            "details": {"source_kind": self.source_kind},
        }


@dataclass
class TemplateSourceAdapterResult:
    contributions: list[TemplateSourceContribution] = field(default_factory=list)
    diagnostics: list[TemplateSourceAdapterDiagnostic] = field(default_factory=list)


class TemplateSourceAdapter(Protocol):
    source_kind: str

    def discover(self, root: Path) -> TemplateSourceAdapterResult: ...


class DomainComponentTemplateAdapter:
    source_kind = "domain_component"

    def discover(self, root: Path) -> TemplateSourceAdapterResult:
        result = TemplateSourceAdapterResult()
        for path in sorted((root / "components").glob("*/manifest.json")):
            _append_manifest_contribution(
                result,
                source_kind=self.source_kind,
                root=root,
                path=path,
                bucket="component_manifests",
                id_keys=("id", "component_id", "name"),
            )
        for path in sorted((root / "domain").glob("**/manifest.json")):
            _append_manifest_contribution(
                result,
                source_kind=self.source_kind,
                root=root,
                path=path,
                bucket="domain_manifests",
                id_keys=("id", "component_id", "provider_id", "profile_id", "name"),
            )
        return result


class ExtensionManifestTemplateAdapter:
    source_kind = "extension_manifest"

    def discover(self, root: Path) -> TemplateSourceAdapterResult:
        result = TemplateSourceAdapterResult()
        for path in sorted((root / "extensions").glob("**/manifest.json")):
            _append_manifest_contribution(
                result,
                source_kind=self.source_kind,
                root=root,
                path=path,
                bucket="extension_manifests",
                id_keys=("id", "extension_id", "name"),
            )
        return result


class LegacyCommandTemplateAdapter:
    source_kind = "legacy_command"

    def discover(self, root: Path) -> TemplateSourceAdapterResult:
        result = TemplateSourceAdapterResult()
        paths = [root / "commands" / "default_commands.json"]
        paths.extend(sorted((root / "commands" / "manifests").glob("*.json")))
        paths.extend(sorted((root / "user_data" / "shared" / "commands").glob("*.json")))
        for path in paths:
            if not path.is_file():
                continue
            payload = _read_json(path, result, self.source_kind)
            if payload is None:
                continue
            commands = payload.get("commands") if isinstance(payload, dict) else payload
            if not isinstance(commands, list):
                result.diagnostics.append(
                    _diagnostic(
                        "template.source_adapter.invalid_command_manifest",
                        "legacy command manifest must be a list or object with commands",
                        self.source_kind,
                        path,
                    )
                )
                continue
            for index, command in enumerate(commands):
                if not isinstance(command, dict):
                    continue
                command_id = _first(command, "id", "command_id", "name") or f"command_{index}"
                trust = _trust_for_path(path, root)
                result.contributions.append(
                    TemplateSourceContribution(
                        source_kind=self.source_kind,
                        source_id=command_id,
                        source_path=str(path),
                        source_pack_id="defaultspack",
                        trust_level=trust,
                        public_id=f"legacy_command:{command_id}",
                        bucket="commands",
                        metadata={
                            "command": _safe_metadata(command),
                            "index": index,
                        },
                    )
                )
        return result


class ExternalIoTemplateAdapter:
    source_kind = "external_io_template"

    def discover(self, root: Path) -> TemplateSourceAdapterResult:
        result = TemplateSourceAdapterResult()
        for path in sorted((root / "external_io_templates").glob("*.template.*")):
            payload = _read_structured(path, result, self.source_kind)
            if payload is None:
                continue
            template_id = _first(payload, "id", "template_id", "name") or path.stem
            result.contributions.append(
                TemplateSourceContribution(
                    source_kind=self.source_kind,
                    source_id=template_id,
                    source_path=str(path),
                    source_pack_id="defaultspack",
                    trust_level=_trust_for_path(path, root),
                    public_id=f"external_io_template:{template_id}",
                    bucket="external_io_templates",
                    metadata=_safe_metadata(payload),
                )
            )
        return result


class FlowRouteTemplateAdapter:
    source_kind = "flow_route"

    def discover(self, root: Path) -> TemplateSourceAdapterResult:
        result = TemplateSourceAdapterResult()
        for path in sorted((root / "flows").glob("**/*.yaml")):
            payload = _read_structured(path, result, self.source_kind)
            if payload is None:
                continue
            flow_id = _first(payload, "flow_id", "id", "name") or path.stem
            routes = _flow_routes(payload)
            for index, route in enumerate(routes):
                method = str(route.get("method") or "GET").strip().upper()
                route_path = str(route.get("path") or "").strip()
                if not route_path:
                    continue
                source_id = f"{flow_id}:{method} {route_path}"
                result.contributions.append(
                    TemplateSourceContribution(
                        source_kind=self.source_kind,
                        source_id=source_id,
                        source_path=str(path),
                        source_pack_id="defaultspack",
                        trust_level=_trust_for_path(path, root),
                        public_id=f"flow_route:{method} {route_path}",
                        bucket="api_routes",
                        metadata={
                            "flow_id": flow_id,
                            "method": method,
                            "path": route_path,
                            "index": index,
                        },
                    )
                )
        return result


class SandboxRuntimeTemplateAdapter:
    source_kind = "sandbox_runtime_template"

    def discover(self, root: Path) -> TemplateSourceAdapterResult:
        result = TemplateSourceAdapterResult()
        templates_root = root.parent / "rumi_sandbox_runtime_pack" / "templates"
        if not templates_root.is_dir():
            return result
        for path in sorted(templates_root.glob("*/template.json")):
            payload = _read_json(path, result, self.source_kind)
            if payload is None:
                continue
            template_id = _first(payload, "id", "template_id", "name") or path.parent.name
            metadata = _safe_metadata(payload)
            metadata.setdefault("id", template_id)
            metadata.setdefault("status", "active")
            result.contributions.append(
                TemplateSourceContribution(
                    source_kind=self.source_kind,
                    source_id=template_id,
                    source_path=str(path),
                    source_pack_id="rumi_sandbox_runtime_pack",
                    trust_level="builtin",
                    public_id=f"sandbox_template:{template_id}",
                    bucket="sandbox_templates",
                    metadata=metadata,
                )
            )
        return result


def default_source_adapters() -> list[TemplateSourceAdapter]:
    return [
        DomainComponentTemplateAdapter(),
        ExtensionManifestTemplateAdapter(),
        LegacyCommandTemplateAdapter(),
        ExternalIoTemplateAdapter(),
        FlowRouteTemplateAdapter(),
        SandboxRuntimeTemplateAdapter(),
    ]


def discover_source_adapter_contributions(
    root: Path,
    *,
    adapters: list[TemplateSourceAdapter] | None = None,
) -> TemplateSourceAdapterResult:
    result = TemplateSourceAdapterResult()
    for adapter in default_source_adapters() if adapters is None else adapters:
        try:
            adapter_result = adapter.discover(root)
        except Exception as exc:
            result.diagnostics.append(
                TemplateSourceAdapterDiagnostic(
                    code="template.source_adapter.discovery_failed",
                    message=f"source adapter failed: {exc}",
                    source_kind=getattr(adapter, "source_kind", "unknown"),
                    source_path=str(root),
                )
            )
            continue
        result.contributions.extend(adapter_result.contributions)
        result.diagnostics.extend(adapter_result.diagnostics)
    result.contributions.sort(key=lambda item: (item.source_kind, item.source_id, item.source_path))
    result.diagnostics.sort(key=lambda item: (item.source_kind, item.source_path, item.code))
    return result


def _append_manifest_contribution(
    result: TemplateSourceAdapterResult,
    *,
    source_kind: str,
    root: Path,
    path: Path,
    bucket: str,
    id_keys: tuple[str, ...],
) -> None:
    payload = _read_json(path, result, source_kind)
    if payload is None:
        return
    source_id = _first(payload, *id_keys) or path.parent.name
    relative_parent = _relative_parent(path, root)
    result.contributions.append(
        TemplateSourceContribution(
            source_kind=source_kind,
            source_id=source_id,
            source_path=str(path),
            source_pack_id="defaultspack",
            trust_level=_trust_for_path(path, root),
            public_id=f"{source_kind}:{bucket}:{relative_parent}:{source_id}",
            bucket=bucket,
            metadata=_safe_metadata(payload),
        )
    )


def _read_json(
    path: Path,
    result: TemplateSourceAdapterResult,
    source_kind: str,
) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        result.diagnostics.append(
            _diagnostic(
                "template.source_adapter.invalid_json",
                f"failed to parse JSON source: {exc.msg}",
                source_kind,
                path,
            )
        )
    except OSError as exc:
        result.diagnostics.append(
            _diagnostic(
                "template.source_adapter.read_error",
                f"failed to read source: {exc}",
                source_kind,
                path,
            )
        )
    return None


def _read_structured(
    path: Path,
    result: TemplateSourceAdapterResult,
    source_kind: str,
) -> dict[str, Any] | None:
    if path.suffix == ".json":
        payload = _read_json(path, result, source_kind)
    else:
        try:
            import importlib

            yaml_module = importlib.import_module("yaml")
            safe_load = getattr(yaml_module, "safe_load", None)
            if not callable(safe_load):
                raise RuntimeError("PyYAML safe_load is unavailable")
            payload = safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            result.diagnostics.append(
                _diagnostic(
                    "template.source_adapter.invalid_yaml",
                    f"failed to parse YAML source: {exc}",
                    source_kind,
                    path,
                )
            )
            return None
    return payload if isinstance(payload, dict) else None


def _flow_routes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    transport = payload.get("transport")
    http = transport.get("http") if isinstance(transport, dict) else None
    routes = http.get("routes") if isinstance(http, dict) else None
    return (
        [route for route in routes if isinstance(route, dict)] if isinstance(routes, list) else []
    )


def _first(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _safe_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        key: value
        for key, value in payload.items()
        if key not in {"trust_level", "trusted", "approved"}
    }
    return json.loads(json.dumps(metadata, default=str))


def _trust_for_path(path: Path, root: Path) -> str:
    if _is_relative_to(path, root / "user_data"):
        return "user"
    if _is_relative_to(path, root):
        return "builtin"
    return "user"


def _diagnostic(
    code: str,
    message: str,
    source_kind: str,
    path: Path,
) -> TemplateSourceAdapterDiagnostic:
    return TemplateSourceAdapterDiagnostic(
        code=code,
        message=message,
        source_kind=source_kind,
        source_path=str(path),
    )


def _projected_id(source_kind: str, source_id: str, source_path: str) -> str:
    digest = hashlib.sha256(f"{source_kind}:{source_id}:{source_path}".encode("utf-8")).hexdigest()
    return f"{source_kind}:{source_id}:{digest[:12]}"


def _relative_parent(path: Path, root: Path) -> str:
    try:
        return str(path.parent.resolve().relative_to(root.resolve()))
    except (OSError, ValueError):
        return path.parent.name


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False
