from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
WEBAPP_ROOT = DEFAULTSPACK_ROOT / "webapp"
COMPAT_ALIASES_PATH = DEFAULTSPACK_ROOT / "compat_aliases.yaml"
COMPAT_ALIAS_REFERENCE_PATH = ROOT / "docs" / "defaultspack-compat-alias-reference.md"


def _failures() -> list[str]:
    return []


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _route_specs():
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))
    from transport.registry import canonical_http_route_specs

    return canonical_http_route_specs(include_always_available=True)


def _template_function_manifests() -> dict[str, dict[str, Any]]:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))
    try:
        from domain.function_runtime.template_specs import template_function_manifests

        manifests = template_function_manifests(DEFAULTSPACK_ROOT)
    except Exception:
        return {}
    return manifests if isinstance(manifests, dict) else {}


def _physical_function_exists(function_id: str) -> bool:
    return (DEFAULTSPACK_ROOT / "functions" / function_id / "manifest.json").is_file()


def _physical_function_main_exists(function_id: str) -> bool:
    return (DEFAULTSPACK_ROOT / "functions" / function_id / "main.py").is_file()


def _template_function_entrypoint_exists(manifest: dict[str, Any]) -> bool:
    entrypoint = str(manifest.get("entrypoint") or "main.py:run")
    entrypoint_file = entrypoint.rsplit(":", 1)[0] if ":" in entrypoint else entrypoint
    candidate = (DEFAULTSPACK_ROOT / "domain" / "function_runtime" / entrypoint_file).resolve()
    try:
        candidate.relative_to((DEFAULTSPACK_ROOT / "domain" / "function_runtime").resolve())
    except ValueError:
        return False
    return candidate.is_file()


def _module_path(block_module: str) -> Path:
    if block_module.startswith("ecosystem."):
        return ROOT / Path(block_module.replace(".", "/") + ".py")
    return DEFAULTSPACK_ROOT / Path(block_module.replace(".", "/") + ".py")


def _extract_api_endpoints() -> set[str]:
    api_ts = WEBAPP_ROOT / "src" / "lib" / "api.ts"
    text = api_ts.read_text(encoding="utf-8")
    endpoints: set[str] = set()
    for match in re.finditer(r"([\"'`])(/api/[^\"'`]+)\1", text):
        endpoint = match.group(2)
        if "${" in endpoint:
            continue
        endpoint = re.sub(r"\$\{[^}]+\}", "{id}", endpoint)
        endpoint = endpoint.split("?", 1)[0]
        endpoints.add(endpoint)
    return endpoints


def _normalize_route(pattern: str) -> str:
    return re.sub(r"\{[^}]+\}", "{id}", pattern)


def _compat_aliases_config() -> dict[str, Any]:
    return _read_yaml(COMPAT_ALIASES_PATH)


def check_ecosystem_manifest(errors: list[str]) -> None:
    manifest = _read_json(DEFAULTSPACK_ROOT / "ecosystem.json")
    if manifest.get("pack_id") != "defaultspack":
        errors.append("ecosystem.json pack_id must be defaultspack")
    if manifest.get("pack_identity") != "rumi:ecosystem/defaultspack":
        errors.append(
            "ecosystem.json pack_identity must be rumi:ecosystem/defaultspack"
        )
    for route in manifest.get("api_routes", []):
        function_id = route.get("function_id")
        if not function_id:
            continue
        if not (DEFAULTSPACK_ROOT / "functions" / function_id / "manifest.json").is_file():
            errors.append(f"api route references missing function manifest: {function_id}")
        if not (DEFAULTSPACK_ROOT / "functions" / function_id / "main.py").is_file():
            errors.append(f"api route references missing function main.py: {function_id}")


def check_routes(errors: list[str]) -> None:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))
    from transport.registry import require_legacy_route_allowlisted

    template_manifests = _template_function_manifests()
    seen: set[tuple[str, str]] = set()
    for spec in _route_specs():
        method = str(spec.method or "").upper()
        pattern = str(spec.pattern or "")
        key = (method, pattern)
        if key in seen:
            errors.append(f"duplicate fallback route: {method} {pattern}")
        seen.add(key)

        function_id = str(getattr(spec, "function_id", "") or "").strip()
        flow_id = str(getattr(spec, "flow_id", "") or "").strip()
        handler_name = str(getattr(spec, "handler_name", "") or "").strip()
        legacy_block_module = str(
            getattr(spec, "legacy_block_module", "") or ""
        ).strip()

        if function_id:
            template_manifest = template_manifests.get(function_id)
            has_physical_manifest = _physical_function_exists(function_id)
            has_template_manifest = isinstance(template_manifest, dict)
            if not (has_physical_manifest or has_template_manifest):
                errors.append(
                    f"route references missing function manifest: {method} {pattern} -> {function_id}"
                )
            if has_template_manifest:
                if not _template_function_entrypoint_exists(template_manifest):
                    errors.append(
                        f"route references missing template function entrypoint: {method} {pattern} -> {function_id}"
                    )
            elif not _physical_function_main_exists(function_id):
                errors.append(
                    f"route references missing function main.py: {method} {pattern} -> {function_id}"
                )

        if legacy_block_module:
            if not _module_path(legacy_block_module).is_file():
                errors.append(
                    f"route references missing legacy block module: {method} {pattern} -> {legacy_block_module}"
                )
            try:
                require_legacy_route_allowlisted(spec)
            except Exception as exc:
                errors.append(str(exc))

        if not (function_id or flow_id or handler_name or legacy_block_module):
            errors.append(
                f"route has no executable target: {method} {pattern}"
            )


def check_frontend_route_parity(errors: list[str]) -> None:
    backend = {_normalize_route(str(spec.pattern or "")) for spec in _route_specs()}
    # Feature-gated mobile routes are omitted from the active registry until
    # enabled, but their frontend clients are still valid static declarations.
    from ecosystem.defaultspack.domain.mobile.contract import MOBILE_ROUTE_CONTRACTS

    backend.update(_normalize_route(route.pattern) for route in MOBILE_ROUTE_CONTRACTS)
    optional_prefixes = ("/api/agent/company/",)
    for endpoint in sorted(_extract_api_endpoints()):
        if endpoint in backend:
            continue
        if any(endpoint.startswith(prefix) for prefix in optional_prefixes):
            continue
        errors.append(f"frontend endpoint has no fallback route: {endpoint}")


def check_function_aliases(errors: list[str]) -> None:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))
    from domain.function_runtime.compat_aliases import compat_alias_metadata_errors

    alias_owner: dict[str, str] = {}
    compat_config = _compat_aliases_config()
    canonical_prefix = str(compat_config.get("canonical_prefix") or "defaultspack.")
    compat_prefixes = tuple(
        str(prefix)
        for prefix in (compat_config.get("compat_prefixes") or ["defaults."])
    )
    compat_aliases = compat_config.get("aliases") or {}

    for manifest_path in sorted((DEFAULTSPACK_ROOT / "functions").glob("*/manifest.json")):
        manifest = _read_json(manifest_path)
        function_id = manifest.get("function_id") or manifest.get("name")
        if function_id != manifest_path.parent.name:
            errors.append(f"function_id mismatch in {manifest_path}")
        aliases = [str(alias) for alias in (manifest.get("vocab_aliases") or [])]
        canonical_aliases = [
            alias for alias in aliases if alias.startswith(canonical_prefix)
        ]
        if not canonical_aliases:
            errors.append(f"missing {canonical_prefix}* alias for {function_id}")
        for alias in aliases:
            if alias in alias_owner:
                errors.append(
                    f"duplicate function alias {alias}: {alias_owner[alias]} and {function_id}"
                )
            alias_owner[alias] = str(function_id)
            if any(alias.startswith(prefix) for prefix in compat_prefixes):
                compat_meta = compat_aliases.get(alias)
                metadata_errors = compat_alias_metadata_errors(alias, compat_meta)
                if metadata_errors:
                    errors.extend(metadata_errors)
                    continue
                replacement = str(compat_meta.get("replacement") or "").strip()
                if replacement not in canonical_aliases:
                    errors.append(
                        f"compat alias replacement must point to a canonical alias on the same function: {alias} -> {replacement}"
                    )

    for alias in sorted(compat_aliases):
        if alias not in alias_owner:
            errors.append(f"compat alias allowlist entry is stale: {alias}")

    from domain.function_runtime.compat_aliases import render_compat_alias_reference

    expected_reference = render_compat_alias_reference()
    try:
        committed_reference = COMPAT_ALIAS_REFERENCE_PATH.read_text(encoding="utf-8")
    except OSError:
        committed_reference = ""
    if committed_reference != expected_reference:
        errors.append(
            "compat alias reference is stale; run "
            "python scripts/quality/render_defaultspack_compat_alias_docs.py"
        )


def check_local_first_defaults(errors: list[str]) -> None:
    critical_files = [
        DEFAULTSPACK_ROOT / "domain" / "ai_client" / "model_runtime_settings.py",
        DEFAULTSPACK_ROOT / "domain" / "chat" / "store.py",
        DEFAULTSPACK_ROOT / "domain" / "frontend" / "registry.py",
        WEBAPP_ROOT / "src" / "App.tsx",
    ]
    forbidden_defaults = [
        'DEFAULT_CHAT_MODEL = "openrouter/',
        'preferred_model ?? "openrouter/',
        '"default": "openrouter/',
        'model_api_routes": "openrouter/',
    ]
    for path in critical_files:
        text = path.read_text(encoding="utf-8")
        for needle in forbidden_defaults:
            if needle in text:
                errors.append(
                    "cloud model remains in local default path: "
                    f"{path.relative_to(ROOT)} contains {needle}"
                )


def check_sensitive_guard(errors: list[str]) -> None:
    http_text = (DEFAULTSPACK_ROOT / "transport" / "http.py").read_text(encoding="utf-8")
    if "require_local_guard(" not in http_text:
        errors.append(
            "transport/http.py does not call require_local_guard for sensitive coding paths"
        )
    approval_text = (
        DEFAULTSPACK_ROOT / "blocks" / "coding" / "_approval.py"
    ).read_text(encoding="utf-8")
    for needle in (
        "hash_arguments",
        "verify_execution_token",
        "create_approval_request",
    ):
        if needle not in approval_text:
            errors.append(f"coding approval helper missing {needle}")


def check_python_syntax(errors: list[str], paths: Iterable[Path]) -> None:
    for path in paths:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"syntax error in {path.relative_to(ROOT)}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true")
    parser.parse_args()
    errors = _failures()
    check_ecosystem_manifest(errors)
    check_routes(errors)
    check_frontend_route_parity(errors)
    check_function_aliases(errors)
    check_local_first_defaults(errors)
    check_sensitive_guard(errors)
    check_python_syntax(
        errors,
        [
            DEFAULTSPACK_ROOT / "domain" / "safety" / "approval.py",
            DEFAULTSPACK_ROOT / "domain" / "safety" / "audit.py",
            DEFAULTSPACK_ROOT / "domain" / "safety" / "local_guard.py",
            DEFAULTSPACK_ROOT / "blocks" / "coding" / "_approval.py",
        ],
    )
    if errors:
        print("defaultspack integrity scan failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("defaultspack integrity scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
