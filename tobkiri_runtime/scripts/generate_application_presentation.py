"""Stage Defaultspack-owned pure presentation code for isolated PackVM reads.

This is packaging, not an execution or authority adapter. The generated file
contains no Host imports, user-data lookup, network calls or provider discovery.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "ecosystem" / "defaultspack"
OUTPUT = PACK / "runtime" / "application_presentation.py"
SOURCES = (
    "domain/frontend_settings_catalog.py",
    "domain/frontend_command_catalog.py",
    "defaultspack/builtin_ui_catalog.v1.json",
    "commands/default_commands.json",
)

ENTRYPOINT = '''
def tobkiri_packvm_invoke(operation_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Read only this application's sealed presentation definitions.

    Profile/session metadata is transport context, never a path or authority.
    Model options are display data; they cannot select an execution provider.
    Returned command metadata does not authorize any command execution.
    """
    if operation_id != "defaultspack.presentation.read":
        raise ValueError("application presentation operation is invalid")
    if not isinstance(payload, Mapping) or set(payload) - {"kind", "model_options", "profile_id", "_session_id"}:
        raise ValueError("application presentation fields are invalid")
    if len(json.dumps(dict(payload), allow_nan=False).encode("utf-8")) > 64 * 1024:
        raise ValueError("application presentation input exceeds limit")
    kind = payload.get("kind")
    if kind == "ui":
        models = payload.get("model_options", [])
        if not isinstance(models, list) or len(models) > 256:
            raise ValueError("model display options are invalid")
        for item in models:
            if not isinstance(item, dict) or set(item) != {"value", "label"}:
                raise ValueError("model display option fields are invalid")
            if any(not isinstance(value, str) or not value or len(value) > 4096 for value in item.values()):
                raise ValueError("model display option value is invalid")
        inputs = SettingsCatalogInputs(
            input_templates=[], output_templates=[], input_profile_options=[],
            output_profile_options=[], model_options=models,
            model_route_options=models, api_key_status=[],
        )
        return {
            "sections": SettingsSections().build([], [], template_catalog={}, inputs=inputs),
            "definitions": deepcopy(_BUILTIN_UI),
        }
    if kind == "commands" and "model_options" not in payload:
        reader = CommandCatalogProjection()
        diagnostics = reader._identity_collisions(_COMMANDS)
        commands = [reader._resolve_command(item, diagnostics, _GENERATION) for item in _COMMANDS]
        for command in commands:
            command["availability"] = {
                "status": "unavailable", "reason_code": "canonical_binding_missing",
                "reason": "This command's canonical execution binding is not connected.",
            }
        return {"commands": commands, "diagnostics": diagnostics, "pack_generation": _GENERATION}
    raise ValueError("application presentation kind is invalid")
'''


def _portable_module(source: str) -> tuple[list[str], str]:
    """Hoist fixed stdlib imports without duplicating module-level bindings."""
    lines = source.splitlines(keepends=True)
    removed: set[int] = set()
    imports: list[str] = []
    for index, node in enumerate(ast.parse(source).body):
        is_import = isinstance(node, (ast.Import, ast.ImportFrom))
        is_docstring = (
            index == 0 and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        )
        if not is_import and not is_docstring:
            continue
        assert node.end_lineno is not None
        span = range(node.lineno - 1, node.end_lineno)
        removed.update(span)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and node.level:
                raise ValueError("presentation source imported a relative dependency")
            names = [node.module] if isinstance(node, ast.ImportFrom) else [alias.name for alias in node.names]
            if names == ["__future__"]:
                continue
            if any(name not in {"copy", "dataclasses", "typing", "re", "unicodedata"} for name in names):
                raise ValueError("presentation source imported a non-portable dependency")
            imports.append("".join(lines[position] for position in span).strip())
    return imports, "".join(line for index, line in enumerate(lines) if index not in removed)


def render() -> str:
    """Compile fixed owner inputs into one deterministic standalone module."""
    inputs = {name: (PACK / name).read_bytes() for name in SOURCES}
    digest = hashlib.sha256()
    for name, content in inputs.items():
        digest.update(name.encode("utf-8") + b"\0" + content)
    imports = ["import json", "from collections.abc import Mapping"]
    bodies = []
    for name in SOURCES[:2]:
        module_imports, body = _portable_module(inputs[name].decode("utf-8"))
        imports.extend(module_imports)
        bodies.append(body)
    parts = [
        '# Generated by scripts/generate_application_presentation.py; do not edit.\n',
        '"""Sealed Defaultspack presentation; no Host state or execution authority."""\n',
        "\n".join(dict.fromkeys(imports)) + "\n",
        *bodies,
    ]
    for name, constant in zip(SOURCES[2:], ("_BUILTIN_UI", "_COMMANDS")):
        value = json.loads(inputs[name])
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        parts.append(f"{constant} = json.loads({encoded!r})\n")
    generation = max(1, int.from_bytes(digest.digest()[:4], "big"))
    parts.append(f"_GENERATION = {generation}\n")
    parts.append(ENTRYPOINT)
    return "\n".join(parts)


def main() -> None:
    """Generate or check the portable presentation artifact."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = render()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != expected:
            raise SystemExit("application presentation artifact is stale")
    else:
        OUTPUT.write_text(expected, encoding="utf-8")


if __name__ == "__main__":
    main()
