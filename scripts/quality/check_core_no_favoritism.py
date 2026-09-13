#!/usr/bin/env python3
"""Reject selected application ownership in ``core_runtime`` assets.

This structural gate has no baseline or exception file for the Python patterns
it recognizes. It scans Python and JSON configuration material but is not
presented as repository-wide proof.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Iterable


FORBIDDEN_FILENAME_PARTS = (
    "ai_input",
    "chat_session",
    "defaultspack",
    "frontend",
    "prompt_builder",
    "runtime_surface",
    "supervisor_dashboard",
)
FORBIDDEN_IMPORT_PREFIXES = (
    "domain",
    "ecosystem.defaultspack",
)
FORBIDDEN_ROUTE_SEGMENTS = frozenset(
    {
        "ai",
        "ai_input",
        "chat",
        "defaultspack",
        "frontend",
        "prompt",
        "supervisor",
        "tool",
        "ui",
    }
)
FORBIDDEN_CONTRACT_RE = re.compile(
    r"(?:^|[.:/_-])(?:ai[_-]?input|chat|defaultspack|frontend|prompt|"
    r"runtime[_-]?surface|supervisor)"
    r"(?:$|[.:/ _-])",
    re.IGNORECASE,
)
CONTRACT_SHAPE_RE = re.compile(r"^(?:io\.tobkiri|rumi|tobkiri|defaultspack)[.:]")
PACK_SETUP_OPERATION_RE = re.compile(r"(?:^|[.:/_-])defaults\.activate(?:$|[.:/_-])")
PACK_SETUP_ACTION_RE = re.compile(r"(?:^|[.:/_-])install_defaults_profile(?:$|[.:/_-])")
NAMED_PACK_RE = re.compile(
    r"(?:^|[.:/_-])(?:defaultspack|rumi_default_tools_pack)(?:$|[.:/_-])",
    re.IGNORECASE,
)
PACK_ENV_RE = re.compile(r"^RUMI_(?:DEFAULTSPACK|DEFAULT_TOOLS)_", re.IGNORECASE)

# These owners are intentionally excluded until their separately scheduled
# authority/profile migrations land. Keeping this list exact makes new core
# favoritism fail the scan rather than silently inheriting a broad exemption.
LITERAL_ALLOWLIST: dict[str, str] = {
    "tobkiri_runtime/core_runtime/legacy_profile_successor_v4.py": (
        "Profile migration owns the historical successor compatibility text."
    ),
}


@dataclass(frozen=True, order=True)
class Violation:
    """One stable No Favoritism violation."""

    path: str
    line: int
    rule: str
    detail: str


def _module_forbidden(module: str) -> bool:
    normalized = module.strip().lstrip(".")
    return any(
        normalized == prefix or normalized.startswith(f"{prefix}.")
        for prefix in FORBIDDEN_IMPORT_PREFIXES
    )


def _string_fragments(node: ast.AST, aliases: dict[str, set[str]]) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name):
        return set(aliases.get(node.id, set()))
    fragments: set[str] = set()
    for child in ast.iter_child_nodes(node):
        fragments.update(_string_fragments(child, aliases))
    return fragments


def _aliases(tree: ast.AST) -> dict[str, set[str]]:
    aliases: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        value: ast.AST | None = None
        targets: Iterable[ast.expr] = ()
        if isinstance(node, ast.Assign):
            value = node.value
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            value = node.value
            targets = (node.target,)
        if value is None:
            continue
        fragments = _string_fragments(value, aliases)
        if not fragments:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                aliases[target.id] = fragments
    return aliases


def _sensitive_call_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Resolve ordinary aliases for dynamic imports and ``sys.path`` calls."""

    import_calls = {"__import__", "importlib.import_module"}
    sys_modules = {"sys"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                local_name = item.asname or item.name
                if item.name == "importlib":
                    import_calls.add(f"{local_name}.import_module")
                elif item.name == "sys":
                    sys_modules.add(local_name)
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for item in node.names:
                if item.name == "import_module":
                    import_calls.add(item.asname or item.name)
    sys_path_calls = {
        f"{module}.path.{method}"
        for module in sys_modules
        for method in ("append", "extend", "insert")
    }
    return import_calls, sys_path_calls


def _looks_pack_local(fragments: set[str], rendered: str = "") -> bool:
    lowered = {value.lower() for value in fragments}
    combined = " ".join(sorted(lowered)) + " " + rendered.lower()
    return (
        "ecosystem.defaultspack" in combined
        or "ecosystem/defaultspack" in combined
        or "_defaultspack" in combined
        or ("defaultspack" in lowered and "domain" in lowered)
    )


def _literal_violation(value: str) -> str | None:
    normalized = value.strip()
    if PACK_SETUP_OPERATION_RE.search(normalized):
        return "application setup operation identifier"
    if PACK_SETUP_ACTION_RE.search(normalized):
        return "application setup action identifier"
    if "frontend_contract_map" in normalized.lower():
        return "frontend contract-map literal"
    if PACK_ENV_RE.match(normalized):
        return "named Pack environment literal"
    if normalized == "/chat" or normalized.startswith("/chat/"):
        return "application chat route literal"
    if NAMED_PACK_RE.search(normalized):
        return "named Pack identifier, allowlist, or path literal"
    if normalized.startswith("/api/"):
        segments = {
            segment.lower().replace("-", "_")
            for segment in normalized.split("?")[0].split("/")
            if segment
        }
        matched = sorted(segments & FORBIDDEN_ROUTE_SEGMENTS)
        if matched:
            return f"application API route segment: {', '.join(matched)}"
    if CONTRACT_SHAPE_RE.match(normalized) and FORBIDDEN_CONTRACT_RE.search(normalized):
        return "application contract identifier"
    return None


def _named_pack_literal_violation(value: str) -> str | None:
    normalized = value.strip()
    if PACK_ENV_RE.match(normalized):
        return "named Pack environment literal"
    if normalized == "/chat" or normalized.startswith("/chat/"):
        return "application chat route literal"
    if NAMED_PACK_RE.search(normalized):
        return "named Pack identifier, allowlist, or path literal"
    return None


def _add_literal_violation(
    violations: set[Violation],
    *,
    relative: str,
    line: int,
    value: str,
    named_pack_only: bool = False,
) -> None:
    if relative in LITERAL_ALLOWLIST:
        return
    detail = (
        _named_pack_literal_violation(value)
        if named_pack_only
        else _literal_violation(value)
    )
    if detail is not None:
        violations.add(
            Violation(relative, line, "forbidden_application_literal", detail)
        )


def _json_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_strings(item)


def _json_line(source: str, value: str) -> int:
    offset = source.find(value)
    return source[:offset].count("\n") + 1 if offset >= 0 else 1


def scan_core(repo_root: Path) -> list[Violation]:
    """Return recognized Python structural violations below core_runtime."""

    core_root = repo_root / "tobkiri_runtime" / "core_runtime"
    if not core_root.is_dir():
        return [
            Violation(
                "tobkiri_runtime/core_runtime",
                1,
                "core_root_missing",
                "required core_runtime directory is unavailable",
            )
        ]
    violations: set[Violation] = set()
    for path in sorted(core_root.rglob("*.py")):
        relative = path.relative_to(repo_root).as_posix()
        lowered_name = path.name.lower()
        for marker in FORBIDDEN_FILENAME_PARTS:
            if marker in lowered_name:
                violations.add(Violation(relative, 1, "forbidden_core_filename", marker))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as error:
            violations.add(Violation(relative, 1, "core_source_unreadable", type(error).__name__))
            continue
        aliases = _aliases(tree)
        import_calls, sys_path_calls = _sensitive_call_names(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    if _module_forbidden(item.name):
                        violations.add(
                            Violation(relative, node.lineno, "forbidden_pack_import", item.name)
                        )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if _module_forbidden(module):
                    violations.add(
                        Violation(relative, node.lineno, "forbidden_pack_import", module)
                    )
            elif isinstance(node, ast.Call):
                rendered = ast.unparse(node.func)
                if rendered in import_calls and node.args:
                    fragments = _string_fragments(node.args[0], aliases)
                    if any(_module_forbidden(value) for value in fragments) or _looks_pack_local(
                        fragments, ast.unparse(node.args[0])
                    ):
                        violations.add(
                            Violation(
                                relative,
                                node.lineno,
                                "forbidden_dynamic_pack_import",
                                ast.unparse(node.args[0]),
                            )
                        )
                if rendered in sys_path_calls:
                    fragments: set[str] = set()
                    for argument in node.args:
                        fragments.update(_string_fragments(argument, aliases))
                    if _looks_pack_local(fragments, ast.unparse(node)):
                        violations.add(
                            Violation(
                                relative,
                                node.lineno,
                                "forbidden_pack_sys_path",
                                ast.unparse(node),
                            )
                        )
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                _add_literal_violation(
                    violations,
                    relative=relative,
                    line=node.lineno,
                    value=node.value,
                )
    for path in sorted(core_root.rglob("*.json")):
        relative = path.relative_to(repo_root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            parsed = json.loads(source)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            violations.add(
                Violation(relative, 1, "core_json_unreadable", type(error).__name__)
            )
            continue
        for value in _json_strings(parsed):
            _add_literal_violation(
                violations,
                relative=relative,
                line=_json_line(source, value),
                value=value,
                named_pack_only=True,
            )
    return sorted(violations)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    violations = scan_core(args.root.resolve())
    if violations:
        for item in violations:
            print(f"{item.path}:{item.line}: {item.rule}: {item.detail}")
        print(
            "core No Favoritism structural check failed: "
            f"{len(violations)} violation(s)"
        )
        return 1
    print("core No Favoritism structural check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
