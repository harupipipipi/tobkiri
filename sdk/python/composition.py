"""Produce a typed outer component with a private Python implementation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Mapping, Sequence

COMPOSER_VERSION = "0.12.0"
COMPOSER_RELEASE_HASHES = {
    "aarch64-apple-darwin":
        "891af0e6d591481daed3a7c52a3dc20fd34cc326e6e955d4c8fd85c4b1835e3d",
    "x86_64-apple-darwin":
        "805ff57132b81dfcd1f25632e5928eac1d5a1e1a32dead48e73f108571da4e17",
    "aarch64-unknown-linux-musl":
        "80fe01c4cbf1e59526069d46f51a8a396be6f8df09ecc060d949181a317c08f5",
    "x86_64-unknown-linux-musl":
        "7cd490edbd452221ef89e6750fc43eacc9b71b1242095eb577cb9a3bf0d3beda",
    "x86_64-pc-windows-gnu":
        "33d92a2a73db1f8de69538038e594a14cc8f9e6feb8138bf29fdaf2598d425d9",
}
_INTERFACE = re.compile(
    r"[a-z][a-z0-9-]*:[a-z][a-z0-9-]*/[a-z][a-z0-9-]*"
    r"@[0-9]+\.[0-9]+\.[0-9]+"
)
_IDENTIFIER = re.compile(r"[a-z][a-z0-9-]*")
_PACKAGE = re.compile(
    r"\bpackage\s+([a-z][a-z0-9-]*):([a-z][a-z0-9-]*)"
    r"@([0-9]+\.[0-9]+\.[0-9]+)\s*;"
)
_INCLUDED_WORLD = re.compile(
    r"([a-z][a-z0-9-]*:[a-z][a-z0-9-]*)/([a-z][a-z0-9-]*)"
    r"@([0-9]+\.[0-9]+\.[0-9]+)"
)
MAX_WIT_BYTES = 8 * 1024 * 1024
MAX_WIT_TOTAL_BYTES = 32 * 1024 * 1024
MAX_WIT_FILES = 256


def _without_comments(text: str) -> str:
    """Remove WIT comments while preserving quoted strings and source offsets."""
    result = list(text)
    index = 0
    in_string = False
    escaped = False
    block_depth = 0
    while index < len(text):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if block_depth:
            if text.startswith("/*", index):
                block_depth += 1
                result[index] = result[index + 1] = " "
                index += 2
            elif text.startswith("*/", index):
                block_depth -= 1
                result[index] = result[index + 1] = " "
                index += 2
            else:
                if char != "\n":
                    result[index] = " "
                index += 1
            continue
        if char == '"':
            in_string = True
            index += 1
            continue
        if text.startswith("/*", index):
            block_depth = 1
            result[index] = result[index + 1] = " "
            index += 2
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            if end < 0:
                end = len(text)
            for position in range(index, end):
                result[position] = " "
            index = end
            continue
        index += 1
    if block_depth:
        raise ValueError("WIT input has an unterminated block comment")
    if in_string:
        raise ValueError("WIT input has an unterminated string")
    return "".join(result)


def _blank_strings(text: str) -> str:
    """Mask quoted WIT text before matching declarations with regular expressions."""
    result = list(text)
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        if in_string:
            if char != "\n":
                result[index] = " "
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            result[index] = " "
            in_string = True
        index += 1
    if in_string:
        raise ValueError("WIT input has an unterminated string")
    return "".join(result)


def _matching_brace(text: str, opening: int) -> int:
    """Return a WIT block's matching brace, ignoring comments and strings."""
    depth = 0
    in_string = False
    escaped = False
    index = opening
    while index < len(text):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise ValueError("WIT world has an unterminated body")


def _world_statements(body: str) -> list[str]:
    """Split the direct world body into semicolon-terminated declarations."""
    statements: list[str] = []
    start = 0
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(body):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                raise ValueError("WIT world has an invalid body")
        elif char == ";" and depth == 0:
            statement = body[start:index].strip()
            if statement:
                statements.append(statement)
            start = index + 1
    if depth != 0 or in_string:
        raise ValueError("WIT world has an invalid body")
    if body[start:].strip():
        raise ValueError("WIT world declaration is missing a semicolon")
    for statement in statements:
        if statement.startswith("@"):
            raise ValueError("annotated WIT world items are unsupported")
        if not re.match(r"(?:export|import|include|use|type)\b", statement):
            raise ValueError("unsupported WIT world item")
    return statements


def _direct_world_exports(body: str) -> list[str]:
    """Read direct exports; reject syntax this producer cannot name exactly."""
    return [statement[len("export"):].strip()
            for statement in _world_statements(body)
            if re.match(r"export\b", statement)]


def _included_worlds(body: str) -> list[tuple[str, str, str]]:
    includes = []
    for statement in _world_statements(body):
        if not re.match(r"include\b", statement):
            continue
        target = statement[len("include"):].strip()
        match = _INCLUDED_WORLD.fullmatch(target)
        if not match:
            raise ValueError("WIT included worlds must be fully qualified and unrenamed")
        includes.append((match.group(1), match.group(2), match.group(3)))
    return includes


def world_export_names_from_sources(sources: Sequence[tuple[str, bytes | str]], world: str,
                                   *, root_source: str) -> list[str]:
    """Resolve exports, including exact versioned worlds included from WIT deps."""
    if not _IDENTIFIER.fullmatch(world):
        raise ValueError("WIT world name must be a simple identifier")
    if not sources or len(sources) > MAX_WIT_FILES:
        raise ValueError("Python build WIT source set is empty or too large")

    packages: dict[tuple[str, str], dict[str, object]] = {}
    total_bytes = 0
    root_package: tuple[str, str] | None = None
    seen_paths: set[str] = set()
    for path, content in sources:
        if path in seen_paths:
            raise ValueError("Python build WIT source paths must be unique")
        seen_paths.add(path)
        if isinstance(content, bytes):
            total_bytes += len(content)
            if len(content) > MAX_WIT_BYTES:
                raise ValueError("WIT input exceeds the Python build byte limit")
            try:
                source = content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("WIT input must be UTF-8") from error
        elif isinstance(content, str):
            encoded = content.encode("utf-8")
            total_bytes += len(encoded)
            if len(encoded) > MAX_WIT_BYTES:
                raise ValueError("WIT input exceeds the Python build byte limit")
            source = content
        else:
            raise ValueError("WIT source content must be bytes or text")
        if total_bytes > MAX_WIT_TOTAL_BYTES:
            raise ValueError("WIT source set exceeds its total byte limit")
        source = _without_comments(source)
        declaration_text = _blank_strings(source)
        declarations = list(_PACKAGE.finditer(declaration_text))
        if len(declarations) != 1:
            raise ValueError("each Python build WIT source must declare exactly one versioned package")
        package = declarations[0]
        package_name = f"{package.group(1)}:{package.group(2)}"
        package_version = package.group(3)
        package_key = (package_name, package_version)
        if path == root_source:
            root_package = package_key
        model = packages.setdefault(package_key, {"interfaces": set(), "worlds": {}})
        interfaces = model["interfaces"]
        assert isinstance(interfaces, set)
        declared_interfaces = re.findall(
            r"\binterface\s+([a-z][a-z0-9-]*)\s*\{", declaration_text)
        if (len(declared_interfaces) != len(set(declared_interfaces))
                or interfaces.intersection(declared_interfaces)):
            raise ValueError("Python build WIT package has a duplicate interface declaration")
        interfaces.update(declared_interfaces)
        worlds = model["worlds"]
        assert isinstance(worlds, dict)
        for match in re.finditer(r"\bworld\s+([a-z][a-z0-9-]*)\s*\{",
                                 declaration_text):
            name = match.group(1)
            opening = match.end() - 1
            closing = _matching_brace(source, opening)
            if name in worlds:
                raise ValueError("Python build WIT package has a duplicate world declaration")
            body = source[opening + 1:closing]
            worlds[name] = {
                "exports": _direct_world_exports(body),
                "includes": _included_worlds(body),
            }

    if root_package is None:
        raise ValueError("selected WIT world file is missing from captured sources")

    resolving: set[tuple[str, str, str]] = set()

    def resolve(package_key: tuple[str, str], name: str) -> list[str]:
        key = (package_key[0], package_key[1], name)
        if key in resolving:
            raise ValueError("WIT world include cycle detected")
        package_model = packages.get(package_key)
        if package_model is None:
            raise ValueError("WIT included package is not among the captured sources")
        worlds = package_model["worlds"]
        assert isinstance(worlds, dict)
        world_model = worlds.get(name)
        if world_model is None:
            raise ValueError("included or selected WIT world is not declared")
        resolving.add(key)
        resolved: list[str] = []
        includes = world_model["includes"]
        exports = world_model["exports"]
        assert isinstance(includes, list) and isinstance(exports, list)
        for package_name, included_name, package_version in includes:
            resolved.extend(resolve((package_name, package_version), included_name))
        interfaces = package_model["interfaces"]
        assert isinstance(interfaces, set)
        for declaration in exports:
            interface = re.fullmatch(r"([a-z][a-z0-9-]*)", declaration)
            if interface:
                interface_name = interface.group(1)
                if interface_name not in interfaces:
                    raise ValueError(f"WIT world export {interface_name!r} is not a local interface")
                resolved.append(f"{package_key[0]}/{interface_name}@{package_key[1]}")
                continue
            function = re.match(r"([a-z][a-z0-9-]*)\s*:\s*func\b", declaration)
            if function:
                function_name = function.group(1)
                if function_name == "exports":
                    raise ValueError("WIT cannot export the compiler's private initializer name")
                resolved.append(function_name)
                continue
            raise ValueError("WIT world exports must be named functions or local interfaces")
        resolving.remove(key)
        if len(resolved) != len(set(resolved)):
            raise ValueError("WIT world include closure has duplicate component exports")
        return resolved

    names = resolve(root_package, world)
    if not names:
        raise ValueError("WIT world must have nonempty, unique component exports")
    return sorted(names)


def world_export_names(wit_text: str, world: str) -> list[str]:
    """Resolve exact exports in a single captured WIT document."""
    if not isinstance(wit_text, str):
        raise ValueError("WIT input must be text")
    return world_export_names_from_sources([("root.wit", wit_text)], world,
                                          root_source="root.wit")


def is_wit_package_source(relative: PurePosixPath) -> bool:
    """Select text WIT files in a package and its nonrecursive deps directory."""
    return (relative.suffix == ".wit"
            and (len(relative.parts) == 1
                 or (relative.parts[0] == "deps"
                     and len(relative.parts) in (2, 3))))


def world_export_names_from_path(wit: Path, world: str) -> tuple[list[str], str]:
    """Read the selected WIT package and captured dependency files."""
    if wit.is_symlink():
        raise ValueError("WIT input must not be a symlink")
    if wit.is_dir():
        root = wit
        root_file = None
    elif wit.is_file():
        root = wit.parent
        root_file = wit
    else:
        raise ValueError("WIT input must be a regular file or directory")
    deps = root / "deps"
    if deps.is_symlink():
        raise ValueError("WIT dependency directory must not be a symlink")
    files = list(root.glob("*.wit"))
    if deps.is_dir():
        for dependency in sorted(deps.iterdir()):
            if dependency.is_symlink():
                raise ValueError("WIT dependency paths must not be symlinks")
            if dependency.is_dir():
                files.extend(dependency.glob("*.wit"))
            elif dependency.suffix == ".wit":
                files.append(dependency)
            elif dependency.suffix in {".wasm", ".wat"}:
                raise ValueError("encoded WIT dependencies are unsupported")
    files = sorted(files)
    if not files or len(files) > MAX_WIT_FILES:
        raise ValueError("the Python build WIT source set is empty or too large")
    sources: list[tuple[str, bytes]] = []
    total = 0
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError("WIT inputs must be regular files, not symlinks")
        size = path.stat().st_size
        if size > MAX_WIT_BYTES:
            raise ValueError("WIT input exceeds the Python build byte limit")
        content = path.read_bytes()
        if len(content) > MAX_WIT_BYTES:
            raise ValueError("WIT input exceeds the Python build byte limit")
        total += len(content)
        if total > MAX_WIT_TOTAL_BYTES:
            raise ValueError("WIT source set exceeds its total byte limit")
        relative = path.relative_to(root).as_posix()
        if not is_wit_package_source(PurePosixPath(relative)):
            raise ValueError("WIT input is outside the package dependency layout")
        sources.append((relative, content))
    if root_file is not None:
        root_source = root_file.relative_to(root).as_posix()
    else:
        world_declaration = re.compile(rf"\bworld\s+{re.escape(world)}\s*\{{")
        candidates = []
        for source_path, content in sources:
            if len(PurePosixPath(source_path).parts) != 1:
                continue
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("WIT input must be UTF-8") from error
            if world_declaration.search(_blank_strings(_without_comments(text))):
                candidates.append(source_path)
        if len(candidates) != 1:
            raise ValueError("WIT directory must contain exactly one selected world")
        root_source = candidates[0]
    content = next(data for path, data in sources if path == root_source)
    names = world_export_names_from_sources(sources, world, root_source=root_source)
    return names, hashlib.sha256(content).hexdigest()


def composition_source(exports: Sequence[str]) -> str:
    """Select reviewed exports without exposing the private inner instance."""
    if not exports or len(set(exports)) != len(exports):
        raise ValueError("outer exports must be explicit, nonempty and unique")
    for name in exports:
        if (not isinstance(name, str) or name == "exports"
                or not (_INTERFACE.fullmatch(name) or _IDENTIFIER.fullmatch(name))):
            raise ValueError("outer export must be a WIT interface or named world function")
    lines = ["package local:sdk-python-outer;",
             "let implementation = new local:python-inner { ... };"]
    lines.extend(f"export implementation[{json.dumps(name)}];" for name in exports)
    return "\n".join(lines) + "\n"


def _composition_commands(composer: Path, inner: Path, stage: Path, wit_directory: Path,
                          world: str) -> list[list[str]]:
    """Build both WAC commands against the same staged WIT package directory."""
    source_path = stage / "outer.wac"
    output = stage / "outer.wasm"
    return [
        [str(composer), "compose", "--dep", f"local:python-inner={inner}",
         "--deps-dir", str(stage / "no-ambient-dependencies"),
         "--output", str(output), str(source_path)],
        [str(composer), "targets", str(output), "--wit", str(wit_directory),
         "--world", world],
    ]


def compose(composer: Path, inner: Path, stage: Path, wit: Path, world: str,
            exports: Sequence[str], environment: Mapping[str, str]) -> dict:
    """Embed an unchanged inner artifact through a pinned, validating composer."""
    expected, wit_hash = world_export_names_from_path(wit, world)
    if sorted(exports) != expected:
        raise ValueError("outer exports differ from the exact WIT world export names")
    if any(path.name.casefold() == "no-ambient-dependencies"
           for path in stage.iterdir()):
        raise ValueError("composition dependency directory must remain absent")
    if composer.is_symlink() or not composer.is_file():
        raise ValueError("composer must be a regular, explicitly selected file")
    tool_hash = hashlib.sha256(composer.read_bytes()).hexdigest()
    if tool_hash not in COMPOSER_RELEASE_HASHES.values():
        raise ValueError("composer differs from the pinned official WAC release")
    version = subprocess.run([str(composer), "--version"], env=dict(environment),
                             check=True, capture_output=True, text=True,
                             timeout=30).stdout.strip()
    if version != f"wac-cli {COMPOSER_VERSION}":
        raise ValueError("composer must be WAC 0.12.0")
    source = composition_source(exports)
    source_path = stage / "outer.wac"
    source_path.write_text(source, encoding="utf-8")
    output = stage / "outer.wasm"
    for command in _composition_commands(composer, inner, stage, wit, world):
        subprocess.run(command, cwd=stage, env=dict(environment), check=True,
                       timeout=180)
    if hashlib.sha256(composer.read_bytes()).hexdigest() != tool_hash:
        raise ValueError("composer changed while composing the component")
    return {"tool": "wac-cli", "version": COMPOSER_VERSION,
            "tool_sha256": tool_hash,
            "wit_sha256": wit_hash,
            "composition_source": source,
            "composition_source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "flags": {"define_components": True, "validate": True,
                      "ambient_dependency_directory": False,
                      "typed_target_validation": True}}
