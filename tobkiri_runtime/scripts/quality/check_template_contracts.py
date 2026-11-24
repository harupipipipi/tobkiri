#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.templates.contracts import run_template_contracts  # noqa: E402
from domain.templates.projectors import build_template_catalog  # noqa: E402


EXPECTED_MANAGED_TEMPLATE_IDS = {
    "pack.safe",
    "pack.networked",
    "coding.python",
    "coding.node",
    "coding.rust",
    "desktop.ubuntu",
    "desktop.browser",
    "desktop.coding",
    "desktop.linux_native",
    "tool.ephemeral",
}

REQUIRED_MANAGED_CONTRACT_FLAGS = {
    "no_backend_entrypoint",
    "no_provider_command",
    "no_arbitrary_host_mounts",
    "no_privileged_mode",
    "no_ambient_host_environment",
    "public_exec_requires_argv",
}

PROHIBITED_MANAGED_KEYS = {
    "backend_entrypoint",
    "backend_module",
    "handler_code",
    "host_path",
    "mount",
    "mounts",
    "privileged",
    "provider_command",
    "shell",
}


def _managed_templates_dir() -> Path:
    return ROOT / "ecosystem" / "rumi_sandbox_runtime_pack" / "templates"


def _load_managed_templates(templates_dir: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    errors: list[str] = []
    templates: dict[str, dict[str, Any]] = {}
    for path in sorted(templates_dir.glob("*/template.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: cannot load JSON: {exc}")
            continue
        template_id = str(payload.get("id") or "")
        if template_id in templates:
            errors.append(f"{path}: duplicate template id {template_id!r}")
            continue
        templates[template_id] = payload
    return templates, errors


def _walk_json(value: Any, path: str = "$") -> list[tuple[str, Any]]:
    items = [(path, value)]
    if isinstance(value, dict):
        for key, child in value.items():
            items.extend(_walk_json(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            items.extend(_walk_json(child, f"{path}[{index}]"))
    return items


def _validate_managed_template(template_id: str, template: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    prefix = template_id or "<missing-id>"
    expected_source_pack_id = _managed_templates_dir().parent.name

    if template.get("schema_version") != 1:
        errors.append(f"{prefix}: schema_version must be 1")
    if template.get("kind") != "rumi.sandbox.template":
        errors.append(f"{prefix}: kind must be rumi.sandbox.template")
    if template.get("id") != template_id:
        errors.append(f"{prefix}: id field mismatch")
    if template.get("source_pack_id") != expected_source_pack_id:
        errors.append(
            f"{prefix}: source_pack_id must be {expected_source_pack_id}"
        )

    if "trust" in template:
        errors.append(f"{prefix}: trust must be projected by the loader, not self-declared in template JSON")

    contract = template.get("contract") if isinstance(template.get("contract"), dict) else {}
    for flag in sorted(REQUIRED_MANAGED_CONTRACT_FLAGS):
        if contract.get(flag) is not True:
            errors.append(f"{prefix}: contract.{flag} must be true")

    policy = template.get("policy") if isinstance(template.get("policy"), dict) else {}
    filesystem = policy.get("filesystem") if isinstance(policy.get("filesystem"), dict) else {}
    if filesystem.get("host_mounts") != []:
        errors.append(f"{prefix}: policy.filesystem.host_mounts must be empty")
    if filesystem.get("arbitrary_host_mounts") is not False:
        errors.append(f"{prefix}: arbitrary host mounts must be false")

    secrets = policy.get("secrets") if isinstance(policy.get("secrets"), dict) else {}
    if secrets.get("ambient") is not False:
        errors.append(f"{prefix}: ambient secrets must be false")
    if secrets.get("mounts") != []:
        errors.append(f"{prefix}: template must not declare secret mounts")

    overrides = template.get("user_overrides") if isinstance(template.get("user_overrides"), dict) else {}
    if overrides.get("narrow_only") is not True:
        errors.append(f"{prefix}: user_overrides.narrow_only must be true")

    allowed_operations = template.get("allowed_operations")
    if not isinstance(allowed_operations, list) or not allowed_operations:
        errors.append(f"{prefix}: allowed_operations must be a non-empty list")
    else:
        for operation in allowed_operations:
            operation_text = str(operation)
            if "command" in operation_text and "argv" not in operation_text:
                errors.append(f"{prefix}: allowed operation {operation_text!r} is not argv-safe")

    network = policy.get("network") if isinstance(policy.get("network"), dict) else {}
    if template_id in {"pack.safe", "tool.ephemeral"}:
        if network.get("mode") != "off":
            errors.append(f"{prefix}: network must be off")
        if network.get("allowlist") != []:
            errors.append(f"{prefix}: network allowlist must be empty")

    desktop = policy.get("desktop") if isinstance(policy.get("desktop"), dict) else {}
    if template_id.startswith("desktop."):
        if desktop.get("enabled") is not True:
            errors.append(f"{prefix}: desktop template must enable desktop")
        if desktop.get("visible_host_window") is not False:
            errors.append(f"{prefix}: desktop must not open a host-visible window")
        if contract.get("human_control_requires_lease") is not True:
            errors.append(f"{prefix}: desktop human control must require a lease")
    elif desktop.get("enabled") is not False:
        errors.append(f"{prefix}: non-desktop template must disable desktop")

    runtime = template.get("runtime") if isinstance(template.get("runtime"), dict) else {}
    if "provider_requirements" not in runtime:
        errors.append(f"{prefix}: runtime.provider_requirements is required")
    if "capabilities" not in runtime:
        errors.append(f"{prefix}: runtime.capabilities is required")

    for json_path, value in _walk_json(template):
        if not isinstance(value, dict):
            continue
        for key in value:
            if key in PROHIBITED_MANAGED_KEYS:
                if json_path == "$.policy.filesystem" and key == "mounts":
                    continue
                if json_path == "$.policy.secrets" and key == "mounts":
                    continue
                errors.append(f"{prefix}: prohibited key {key!r} at {json_path}")

    return errors


def _run_managed_sandbox_template_contracts() -> dict[str, Any]:
    templates_dir = _managed_templates_dir()
    templates, errors = _load_managed_templates(templates_dir)

    found_ids = set(templates)
    for template_id in sorted(EXPECTED_MANAGED_TEMPLATE_IDS - found_ids):
        errors.append(f"missing template {template_id}")
    for template_id in sorted(found_ids - EXPECTED_MANAGED_TEMPLATE_IDS):
        errors.append(f"unexpected template {template_id}")

    for template_id in sorted(EXPECTED_MANAGED_TEMPLATE_IDS & found_ids):
        expected_path = templates_dir / template_id / "template.json"
        if not expected_path.is_file():
            errors.append(f"{template_id}: expected path {expected_path}")
        errors.extend(_validate_managed_template(template_id, templates[template_id]))

    if templates.get("desktop.browser", {}).get("extends") != "desktop.ubuntu":
        errors.append("desktop.browser: extends must be desktop.ubuntu")

    return {
        "passed": not errors,
        "template_count": len(found_ids),
        "templates_dir": str(templates_dir),
        "errors": errors,
    }


def main() -> int:
    catalog = build_template_catalog(defaultspack_root=DEFAULTSPACK_ROOT)
    result = run_template_contracts(catalog, defaultspack_root=DEFAULTSPACK_ROOT)
    defaultspack_summary = result.to_dict()
    managed_summary = _run_managed_sandbox_template_contracts()
    summary = {
        "passed": result.passed and managed_summary["passed"],
        "defaultspack": defaultspack_summary,
        "managed_sandbox": managed_summary,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
