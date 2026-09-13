from __future__ import annotations

import importlib
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_TRUST_BUILTIN = "builtin"
_LIFECYCLE_ALIASES = {
    "local": "singleton",
    "local_secret_store": "singleton",
    "request": "request",
    "singleton": "singleton",
    "startup": "startup",
}
_DEFAULT_ALLOWED_PREFIXES = (
    "blocks.",
    "domain.",
    "ecosystem.defaultspack.domain.",
)


@dataclass(frozen=True)
class TemplateBackendServiceSpec:
    service_id: str
    entrypoint: str
    lifecycle: str
    dependencies: tuple[str, ...]
    template_id: str
    piece_id: str
    trust_level: str
    source_path: str


class TemplateBackendServiceRegistry:
    def __init__(
        self,
        *,
        defaultspack_root: str | Path,
        allowed_module_prefixes: tuple[str, ...] = _DEFAULT_ALLOWED_PREFIXES,
    ) -> None:
        self.defaultspack_root = Path(defaultspack_root).resolve()
        self.allowed_module_prefixes = allowed_module_prefixes
        self.specs: dict[str, TemplateBackendServiceSpec] = {}
        self.diagnostics: list[dict[str, Any]] = []
        self._singletons: dict[str, Any] = {}
        self._started: list[str] = []
        self._failed: set[str] = set()

    def load_from_catalog(self, catalog: dict[str, Any]) -> list[dict[str, Any]]:
        self.specs.clear()
        self.diagnostics.clear()
        self._singletons.clear()
        self._started.clear()
        self._failed.clear()
        items = catalog.get("backend_services") if isinstance(catalog, dict) else []
        if not isinstance(items, list):
            return self.diagnostics
        for item in items:
            if not isinstance(item, dict):
                continue
            spec = self._spec_from_item(item)
            if spec is None:
                continue
            if spec.service_id in self.specs:
                self.diagnostics.append(
                    _diagnostic(
                        "template.service.duplicate_service_id",
                        f"duplicate backend service id: {spec.service_id}",
                        spec,
                    )
                )
                continue
            self.specs[spec.service_id] = spec
        return list(self.diagnostics)

    def get(self, service_id: str) -> Any:
        spec = self.specs.get(service_id)
        if spec is None:
            return None
        if spec.lifecycle == "request":
            return self._instantiate(spec)
        if service_id not in self._singletons:
            service = self._instantiate(spec)
            if service is None:
                self._failed.add(service_id)
                return None
            self._singletons[service_id] = service
        return self._singletons[service_id]

    def start_all(self) -> list[dict[str, Any]]:
        diagnostics: list[dict[str, Any]] = []
        order, order_diagnostics = self._dependency_order()
        diagnostics.extend(order_diagnostics)
        blocked = {diag["service_id"] for diag in order_diagnostics if "service_id" in diag}
        for service_id in order:
            spec = self.specs[service_id]
            if service_id in blocked:
                self._failed.add(service_id)
                continue
            failed_dependencies = [dep for dep in spec.dependencies if dep in self._failed]
            if failed_dependencies:
                self._failed.add(service_id)
                diagnostics.append(
                    _diagnostic(
                        "template.service.dependency_failed",
                        f"backend service {service_id} blocked by failed dependencies",
                        spec,
                        details={"dependencies": failed_dependencies},
                    )
                )
                continue
            if spec.lifecycle == "request":
                continue
            service = self.get(service_id)
            if service is None:
                diagnostics.append(
                    _diagnostic(
                        "template.service.load_failed",
                        f"backend service {service_id} could not be loaded",
                        spec,
                    )
                )
                continue
            start = getattr(service, "start", None)
            if callable(start):
                try:
                    start()
                except Exception as exc:
                    self._failed.add(service_id)
                    diagnostics.append(
                        _diagnostic(
                            "template.service.start_failed",
                            f"backend service {service_id} failed to start: {exc}",
                            spec,
                        )
                    )
                    continue
            else:
                diagnostics.append(
                    _diagnostic(
                        "template.service.start_missing",
                        f"backend service {service_id} has no start() hook",
                        spec,
                        severity="warning",
                    )
                )
            self._started.append(service_id)
        self.diagnostics.extend(diagnostics)
        return diagnostics

    def stop_all(self) -> list[dict[str, Any]]:
        diagnostics: list[dict[str, Any]] = []
        for service_id in reversed(self._started):
            spec = self.specs[service_id]
            service = self._singletons.get(service_id)
            stop = getattr(service, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception as exc:
                    diagnostics.append(
                        _diagnostic(
                            "template.service.stop_failed",
                            f"backend service {service_id} failed to stop: {exc}",
                            spec,
                        )
                    )
            else:
                diagnostics.append(
                    _diagnostic(
                        "template.service.stop_missing",
                        f"backend service {service_id} has no stop() hook",
                        spec,
                        severity="warning",
                    )
                )
        self._started.clear()
        self.diagnostics.extend(diagnostics)
        return diagnostics

    def health(self) -> dict[str, Any]:
        services: dict[str, Any] = {}
        for service_id, spec in sorted(self.specs.items()):
            service = self._singletons.get(service_id)
            status = "failed" if service_id in self._failed else "ok"
            details: dict[str, Any] = {
                "status": status,
                "lifecycle": spec.lifecycle,
                "dependencies": list(spec.dependencies),
                "template_id": spec.template_id,
                "piece_id": spec.piece_id,
            }
            if service is None:
                details["loaded"] = False
            else:
                details["loaded"] = True
                hook = getattr(service, "health", None)
                if callable(hook):
                    try:
                        details["health"] = hook()
                    except Exception as exc:
                        details["status"] = "failed"
                        details["error"] = str(exc)
            services[service_id] = details
        return {
            "status": "ok" if not self._failed else "degraded",
            "services": services,
        }

    def _spec_from_item(self, item: dict[str, Any]) -> TemplateBackendServiceSpec | None:
        service_id = str(item.get("service_id") or item.get("id") or "").strip()
        entrypoint = str(item.get("entrypoint") or "").strip()
        trust_level = str(item.get("trust_level") or "").strip().lower()
        lifecycle = _LIFECYCLE_ALIASES.get(str(item.get("lifecycle") or "singleton").strip())
        spec = TemplateBackendServiceSpec(
            service_id=service_id,
            entrypoint=entrypoint,
            lifecycle=lifecycle or "",
            dependencies=_dependencies(item),
            template_id=str(item.get("template_id") or "").strip(),
            piece_id=str(item.get("piece_id") or "").strip(),
            trust_level=trust_level,
            source_path=str(item.get("_source") or item.get("source_path") or "").strip(),
        )
        if not service_id:
            self.diagnostics.append(
                _diagnostic("template.service.missing_id", "missing service_id", spec)
            )
            return None
        if trust_level != _TRUST_BUILTIN:
            self.diagnostics.append(
                _diagnostic(
                    "template.service.non_builtin_rejected",
                    "only builtin templates may register executable backend services",
                    spec,
                )
            )
            return None
        if not entrypoint:
            self.diagnostics.append(
                _diagnostic(
                    "template.service.missing_entrypoint", "missing service entrypoint", spec
                )
            )
            return None
        if lifecycle not in {"request", "singleton", "startup"}:
            self.diagnostics.append(
                _diagnostic(
                    "template.service.invalid_lifecycle",
                    f"unsupported service lifecycle: {item.get('lifecycle')}",
                    spec,
                )
            )
            return None
        module_name, _attr = self._split_entrypoint(entrypoint)
        if module_name is None or not module_name.startswith(self.allowed_module_prefixes):
            self.diagnostics.append(
                _diagnostic(
                    "template.service.entrypoint_not_allowlisted",
                    f"service entrypoint is not allowlisted: {entrypoint}",
                    spec,
                )
            )
            return None
        origin = _module_origin(module_name)
        if origin is None or not _is_relative_to(origin, self.defaultspack_root):
            self.diagnostics.append(
                _diagnostic(
                    "template.service.module_escape_rejected",
                    f"service module is outside defaultspack root: {entrypoint}",
                    spec,
                    details={"module_origin": str(origin) if origin is not None else None},
                )
            )
            return None
        return spec

    def _instantiate(self, spec: TemplateBackendServiceSpec) -> Any:
        module_name, attr_name = self._split_entrypoint(spec.entrypoint)
        if module_name is None:
            return None
        try:
            module = importlib.import_module(module_name)
        except Exception:
            return None
        target: Any = module
        if attr_name:
            for segment in attr_name.split("."):
                target = getattr(target, segment)
        if isinstance(target, type):
            return target()
        return target

    def _split_entrypoint(self, entrypoint: str) -> tuple[str | None, str]:
        if ":" in entrypoint:
            module_name, _, attr_name = entrypoint.partition(":")
            return module_name.strip() or None, attr_name.strip()
        parts = [part for part in entrypoint.split(".") if part]
        for index in range(len(parts), 0, -1):
            module_name = ".".join(parts[:index])
            if _module_origin(module_name) is not None:
                attr_name = ".".join(parts[index:])
                return module_name, attr_name
        return None, ""

    def _dependency_order(self) -> tuple[list[str], list[dict[str, Any]]]:
        order: list[str] = []
        temporary: set[str] = set()
        permanent: set[str] = set()
        diagnostics: list[dict[str, Any]] = []

        def visit(service_id: str, stack: list[str]) -> None:
            if service_id in permanent:
                return
            if service_id in temporary:
                cycle = [*stack, service_id]
                for member in sorted(set(cycle)):
                    spec = self.specs[member]
                    diagnostics.append(
                        _diagnostic(
                            "template.service.dependency_cycle",
                            "backend service dependency cycle detected",
                            spec,
                            details={"cycle": cycle},
                        )
                    )
                return
            temporary.add(service_id)
            spec = self.specs[service_id]
            for dependency in spec.dependencies:
                if dependency not in self.specs:
                    diagnostics.append(
                        _diagnostic(
                            "template.service.missing_dependency",
                            f"backend service dependency not found: {dependency}",
                            spec,
                            details={"dependency": dependency},
                        )
                    )
                    continue
                visit(dependency, [*stack, service_id])
            temporary.remove(service_id)
            permanent.add(service_id)
            order.append(service_id)

        for service_id in sorted(self.specs):
            visit(service_id, [])
        return order, diagnostics


def _dependencies(item: dict[str, Any]) -> tuple[str, ...]:
    value = item.get("dependencies")
    if not isinstance(value, list):
        value = item.get("depends_on")
    if not isinstance(value, list):
        return ()
    return tuple(sorted({str(entry).strip() for entry in value if str(entry or "").strip()}))


def _module_origin(module_name: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, AttributeError, ValueError):
        return None
    if spec is None or not isinstance(spec.origin, str) or spec.origin in ("built-in", "namespace"):
        return None
    return Path(spec.origin).resolve()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _diagnostic(
    code: str,
    message: str,
    spec: TemplateBackendServiceSpec,
    *,
    severity: str = "error",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "level": severity,
        "code": code,
        "message": message,
        "service_id": spec.service_id,
        "template_id": spec.template_id,
        "piece_id": spec.piece_id,
        "source_path": spec.source_path,
        "details": details or {},
    }
