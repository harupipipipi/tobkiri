from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_STATE_RELATIVE_PATH = Path("user_data/shared/templates/template-state.json")
_SECRET_MARKERS = ("secret", "token", "password", "credential", "api_key", "apikey")
_ALLOWED_OPERATIONS = {
    "archive_setting",
    "delete_setting",
    "move_setting",
    "rename_external_io_config",
    "rename_setting",
    "set_default_if_missing",
}


@dataclass(frozen=True)
class TemplateLifecycleOperationResult:
    operation: str
    changed: bool = False
    skipped: bool = False
    message: str = ""
    path: str = ""


@dataclass
class TemplateLifecyclePlan:
    template_id: str
    action: str
    operations: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = True
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    results: list[TemplateLifecycleOperationResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(item.get("severity") == "error" for item in self.diagnostics)


class TemplateLifecycleStore:
    def __init__(
        self,
        defaultspack_root: str | Path,
        *,
        state_path: str | Path | None = None,
    ) -> None:
        self.root = Path(defaultspack_root).resolve()
        self.path = (
            Path(state_path).resolve()
            if state_path is not None
            else (self.root / _STATE_RELATIVE_PATH).resolve()
        )
        if not _is_relative_to(self.path, self.root):
            raise ValueError("template lifecycle state path must stay under defaultspack root")

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"templates": {}}
        except (OSError, json.JSONDecodeError):
            return {"templates": {}}
        if not isinstance(data, dict):
            return {"templates": {}}
        templates = data.get("templates")
        if not isinstance(templates, dict):
            data["templates"] = {}
        return data

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            backup = self.path.with_suffix(self.path.suffix + ".bak")
            try:
                backup.write_bytes(self.path.read_bytes())
            except OSError:
                pass
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
            _fsync_directory(self.path.parent)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise


def _fsync_directory(path: Path) -> None:
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        return
    directory_fd = os.open(path, directory_flag)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def plan_template_lifecycle(
    template: dict[str, Any],
    *,
    action: str,
    settings: dict[str, Any] | None = None,
    defaultspack_root: str | Path | None = None,
    source_generation: str = "",
) -> TemplateLifecyclePlan:
    del defaultspack_root, source_generation
    template_id = _template_id(template)
    operations = _operations_for_action(template, action)
    plan = TemplateLifecyclePlan(template_id=template_id, action=action, operations=operations)
    planned_settings = deepcopy(settings) if isinstance(settings, dict) else {}
    _execute_operations(plan, planned_settings, dry_run=True)
    return plan


def apply_template_lifecycle(
    template: dict[str, Any],
    *,
    action: str,
    settings: dict[str, Any],
    defaultspack_root: str | Path,
    source_generation: str = "",
    state_path: str | Path | None = None,
) -> TemplateLifecyclePlan:
    if not isinstance(settings, dict):
        raise TypeError("settings must be a mutable dictionary")
    template_id = _template_id(template)
    plan = TemplateLifecyclePlan(
        template_id=template_id,
        action=action,
        operations=_operations_for_action(template, action),
        dry_run=False,
    )
    working_settings = deepcopy(settings)
    _execute_operations(plan, working_settings, dry_run=False)
    if not plan.ok:
        return plan
    settings.clear()
    settings.update(working_settings)
    store = TemplateLifecycleStore(defaultspack_root, state_path=state_path)
    state = store.load()
    templates = state.setdefault("templates", {})
    previous = templates.get(template_id) if isinstance(templates.get(template_id), dict) else {}
    now = _now()
    templates[template_id] = {
        "template_id": template_id,
        "installed_version": str(template.get("version") or ""),
        "schema_version": int(template.get("schema_version") or 1),
        "source_generation": source_generation,
        "status": str(template.get("status") or ""),
        "installed_at": previous.get("installed_at") or now,
        "updated_at": now,
    }
    store.save(state)
    return plan


def _execute_operations(
    plan: TemplateLifecyclePlan,
    settings: dict[str, Any],
    *,
    dry_run: bool,
) -> None:
    for index, operation in enumerate(plan.operations):
        if not isinstance(operation, dict):
            plan.diagnostics.append(
                _diagnostic("template.lifecycle.invalid_operation", f"/{plan.action}/{index}")
            )
            return
        op_name = str(operation.get("op") or operation.get("type") or "").strip()
        path = f"/{plan.action}/{index}"
        if op_name not in _ALLOWED_OPERATIONS:
            plan.diagnostics.append(
                _diagnostic(
                    "template.lifecycle.unsupported_operation",
                    path,
                    f"unsupported lifecycle operation: {op_name}",
                )
            )
            return
        if _operation_touches_secret(operation):
            plan.diagnostics.append(
                _diagnostic(
                    "template.lifecycle.secret_operation_rejected",
                    path,
                    "lifecycle operations cannot read, copy, rename, or delete secret fields",
                )
            )
            return
        result = _apply_operation(op_name, operation, settings, dry_run=dry_run)
        plan.results.append(result)


def _apply_operation(
    op_name: str,
    operation: dict[str, Any],
    settings: dict[str, Any],
    *,
    dry_run: bool,
) -> TemplateLifecycleOperationResult:
    if op_name == "set_default_if_missing":
        key = _key(operation, "key", "setting", "path")
        value = deepcopy(operation.get("value"))
        if key in settings:
            return TemplateLifecycleOperationResult(op_name, skipped=True, path=key)
        if not dry_run:
            settings[key] = value
        return TemplateLifecycleOperationResult(op_name, changed=True, path=key)

    if op_name in {"rename_setting", "move_setting"}:
        source = _key(operation, "from", "source", "key")
        target = _key(operation, "to", "target")
        if source not in settings:
            return TemplateLifecycleOperationResult(op_name, skipped=True, path=source)
        if target in settings and settings[target] == settings[source]:
            if not dry_run:
                settings.pop(source, None)
            return TemplateLifecycleOperationResult(op_name, changed=True, path=target)
        if target in settings:
            return TemplateLifecycleOperationResult(
                op_name,
                skipped=True,
                message="target already exists",
                path=target,
            )
        if not dry_run:
            settings[target] = settings.pop(source)
        return TemplateLifecycleOperationResult(op_name, changed=True, path=target)

    if op_name == "archive_setting":
        source = _key(operation, "key", "from", "source")
        archive_key = _key(operation, "archive_key", "to", default=f"archived.{source}")
        if source not in settings:
            return TemplateLifecycleOperationResult(op_name, skipped=True, path=source)
        if not dry_run:
            archive = settings.setdefault("_archived_settings", {})
            if isinstance(archive, dict):
                archive[archive_key] = settings.pop(source)
        return TemplateLifecycleOperationResult(op_name, changed=True, path=source)

    if op_name == "delete_setting":
        key = _key(operation, "key", "from", "source")
        if key not in settings:
            return TemplateLifecycleOperationResult(op_name, skipped=True, path=key)
        if not dry_run:
            settings.pop(key, None)
        return TemplateLifecycleOperationResult(op_name, changed=True, path=key)

    if op_name == "rename_external_io_config":
        source = _key(operation, "from", "source", "key")
        target = _key(operation, "to", "target")
        external_io = settings.setdefault("external_io", {})
        if not isinstance(external_io, dict) or source not in external_io:
            return TemplateLifecycleOperationResult(op_name, skipped=True, path=source)
        if target in external_io:
            return TemplateLifecycleOperationResult(
                op_name,
                skipped=True,
                message="target already exists",
                path=target,
            )
        if not dry_run:
            external_io[target] = external_io.pop(source)
        return TemplateLifecycleOperationResult(op_name, changed=True, path=target)

    return TemplateLifecycleOperationResult(op_name, skipped=True)


def _operations_for_action(template: dict[str, Any], action: str) -> list[dict[str, Any]]:
    lifecycle = template.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return []
    raw = lifecycle.get(action)
    if isinstance(raw, dict) and action == "uninstall":
        raw = raw.get("operations", [])
    if not isinstance(raw, list):
        return []
    return [deepcopy(item) for item in raw if isinstance(item, dict)]


def _operation_touches_secret(operation: dict[str, Any]) -> bool:
    for key, value in operation.items():
        if key == "value":
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            text = str(item or "").lower()
            if any(marker in text for marker in _SECRET_MARKERS):
                return True
    return False


def _key(operation: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = operation.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def _template_id(template: dict[str, Any]) -> str:
    return str(template.get("id") or "").strip()


def _diagnostic(code: str, path: str, message: str | None = None) -> dict[str, Any]:
    return {
        "severity": "error",
        "level": "error",
        "code": code,
        "message": message or "invalid template lifecycle operation",
        "path": path,
    }


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
