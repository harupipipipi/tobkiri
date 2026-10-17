"""Keep signed Pack UI commands registered and authorized on the Host panel."""

from __future__ import annotations

import json
import re
import tomllib
import unittest
from pathlib import Path


LAUNCHER_ROOT = Path(__file__).resolve().parents[2]
TAURI_ROOT = LAUNCHER_ROOT / "src-tauri"
SIGNED_PACK_COMMANDS = (
    "signed_pack_admission_status",
    "admit_signed_pack_from_folder",
    "onboard_signed_pack_from_folder",
)


def _rust_tokens(source: str) -> list[str]:
    """Keep literals atomic and omit Rust comments, including nested blocks."""
    tokens: list[str] = []
    position = 0
    token_pattern = re.compile(
        r"(?:b)?'(?:\\(?:u\{[0-9a-fA-F_]+\}|x[0-9a-fA-F]{2}|.)|[^\\'\n])'"
        r"|[A-Za-z_][A-Za-z_0-9]*|::|\S"
    )
    raw_pattern = re.compile(r'(?:br|cr|r)(#*)"')
    while position < len(source):
        if source[position].isspace():
            position += 1
        elif source.startswith("//", position):
            end = source.find("\n", position)
            position = len(source) if end < 0 else end + 1
        elif source.startswith("/*", position):
            depth = 1
            position += 2
            while depth:
                if position >= len(source):
                    raise AssertionError("unterminated Rust block comment")
                if source.startswith("/*", position):
                    depth += 1
                    position += 2
                elif source.startswith("*/", position):
                    depth -= 1
                    position += 2
                else:
                    position += 1
        else:
            raw = raw_pattern.match(source, position)
            if raw:
                terminator = '"' + raw[1]
                end = source.find(terminator, raw.end())
                if end < 0:
                    raise AssertionError("unterminated Rust raw string")
                end += len(terminator)
            elif source[position] == '"':
                end = position + 1
                while end < len(source) and source[end] != '"':
                    end += 2 if source[end] == "\\" else 1
                if end >= len(source):
                    raise AssertionError("unterminated Rust string")
                end += 1
            else:
                match = token_pattern.match(source, position)
                assert match is not None
                end = match.end()
            tokens.append(source[position:end])
            position = end
    return tokens


def _group(tokens: list[str], start: int) -> tuple[list[str], int]:
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = [pairs[tokens[start]]]
    for position in range(start + 1, len(tokens)):
        token = tokens[position]
        if token in pairs:
            stack.append(pairs[token])
        elif token in pairs.values():
            if token != stack.pop():
                raise AssertionError("mismatched Rust delimiters")
            if not stack:
                return tokens[start + 1 : position], position + 1
    raise AssertionError("unterminated Rust group")


def _one_group_after(tokens: list[str], prefix: list[str]) -> list[str]:
    matches = [
        index + len(prefix) - 1
        for index in range(len(tokens) - len(prefix) + 1)
        if tokens[index : index + len(prefix)] == prefix
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected one Rust expression: {' '.join(prefix)}")
    return _group(tokens, matches[0])[0]


def _function(tokens: list[str], name: str) -> tuple[list[str], list[str]]:
    prefix = ["fn", name, "("]
    parameters = _one_group_after(tokens, prefix)
    start = next(
        index + len(prefix) - 1
        for index in range(len(tokens) - len(prefix) + 1)
        if tokens[index : index + len(prefix)] == prefix
    )
    _, end = _group(tokens, start)
    if tokens[end] != "{":
        raise AssertionError(f"expected a simple Rust function body for {name}")
    return parameters, _group(tokens, end)[0]


def _literal_commands(
    expression: list[str], source: list[str], seen: tuple[str, ...] = ()
) -> set[str]:
    if expression[:1] == ["&"]:
        expression = expression[1:]
    if len(expression) == 1 and re.fullmatch(
        r"[A-Za-z_][A-Za-z_0-9]*", expression[0]
    ):
        name = expression[0]
        if name in seen:
            raise AssertionError("recursive Rust command constant")
        declarations = [
            index
            for index in range(len(source) - 2)
            if source[index : index + 3] == ["const", name, ":"]
        ]
        if len(declarations) != 1:
            raise AssertionError(f"expected one command constant: {name}")
        start = source.index("=", declarations[0] + 3) + 1
        end = source.index(";", start)
        return _literal_commands(source[start:end], source, (*seen, name))
    if expression[:1] != ["["]:
        raise AssertionError("expected a literal Rust command slice")
    entries, end = _group(expression, 0)
    if end != len(expression):
        raise AssertionError("unexpected expression after Rust command slice")
    commands: set[str] = set()
    for index in range(0, len(entries), 2):
        match = re.fullmatch(r'"([a-z_]+)"', entries[index])
        if match is None or (index + 1 < len(entries) and entries[index + 1] != ","):
            raise AssertionError("expected comma-separated literal Rust commands")
        if match[1] in commands:
            raise AssertionError(f"duplicate Rust command: {match[1]}")
        commands.add(match[1])
    return commands


def _declared_legacy_commands(source: str) -> set[str]:
    tokens = _rust_tokens(source)
    parameters, manifest = _function(tokens, "build_tauri_manifest")
    if parameters[:2] != ["commands", ":"] or "," in parameters:
        raise AssertionError("expected the shared manifest command parameter")
    actual = _one_group_after(
        manifest,
        [
            "tauri_build", "::", "AppManifest", "::", "new", "(", ")",
            ".", "commands", "(",
        ],
    )
    if actual != ["commands"]:
        raise AssertionError("AppManifest must receive the supplied commands")
    parameters, legacy = _function(tokens, "build_legacy_tauri_manifest")
    if parameters:
        raise AssertionError("unexpected legacy manifest parameters")
    argument = _one_group_after(legacy, ["build_tauri_manifest", "("])
    return _literal_commands(argument, tokens)


def _registered_commands(source: str) -> set[str]:
    entries = _one_group_after(
        _rust_tokens(source),
        [".", "invoke_handler", "(", "tauri", "::", "generate_handler", "!", "["],
    )
    commands: set[str] = set()
    for entry in " ".join(entries).rstrip(" ,").split(","):
        path = entry.split()
        if (
            len(path) % 2 != 1
            or any(
                re.fullmatch(r"[a-z_][a-z_0-9]*", name) is None
                for name in path[::2]
            )
            or any(separator != "::" for separator in path[1::2])
        ):
            raise AssertionError("expected a Rust handler path")
        commands.add(path[-1])
    return commands


class SignedPackLauncherPermissionsTests(unittest.TestCase):
    def test_signed_pack_panel_commands_have_matching_native_permissions(self) -> None:
        build_script = (TAURI_ROOT / "build.rs").read_text(encoding="utf-8")
        launcher = (TAURI_ROOT / "src/legacy.rs").read_text(encoding="utf-8")
        panel = json.loads(
            (TAURI_ROOT / "capabilities/launcher-panel.json").read_text(
                encoding="utf-8"
            )
        )
        declared = _declared_legacy_commands(build_script)
        registered = _registered_commands(launcher)
        allowed = set(panel["permissions"])

        self.assertEqual(panel["windows"], ["main"])
        self.assertIn("http://127.0.0.1:8765/*", panel["remote"]["urls"])
        for command in SIGNED_PACK_COMMANDS:
            with self.subTest(command=command):
                permission = "allow-" + command.replace("_", "-")
                self.assertIn(command, declared)
                self.assertIn(command, registered)
                self.assertIn(permission, allowed)
                entries = tomllib.loads(
                    (TAURI_ROOT / "permissions/autogenerated" / f"{command}.toml")
                    .read_text(encoding="utf-8")
                )["permission"]
                self.assertEqual(entries[0]["identifier"], permission)
                self.assertEqual(entries[0]["commands"]["allow"], [command])
                self.assertEqual(
                    entries[1]["commands"]["deny"], [command]
                )

    def test_manifest_reader_follows_shared_helper_and_named_constants(self) -> None:
        helper = """
            fn build_tauri_manifest(commands: &'static [&'static str]) {
                tauri_build::AppManifest::new().commands(commands);
            }
        """
        for argument in ('&["real_command"]', "LEGACY_COMMANDS"):
            with self.subTest(argument=argument):
                source = helper + """
                    const CORE_COMMANDS: &[&str] = &["core_only"];
                    const LEGACY_COMMANDS: &[&str] = &["real_command"];
                    /* outer /* build_tauri_manifest(&["comment_only"]); */ */
                    fn build_legacy_tauri_manifest() {
                        let text = r#"build_tauri_manifest(&["raw_only"]);"#;
                        let text = "build_tauri_manifest(&[\\\"string_only\\\"]);";
                        // build_tauri_manifest(&["line_only"]);
                        build_tauri_manifest(ARGUMENT);
                    }
                """.replace("ARGUMENT", argument)
                self.assertEqual(_declared_legacy_commands(source), {"real_command"})
                call = f"build_tauri_manifest({argument});"
                for invalid in (
                    source.replace(".commands(commands)", ".commands(CORE_COMMANDS)"),
                    source.replace(call, ""),
                    source.replace(call, call + call),
                ):
                    with self.subTest(invalid=invalid):
                        with self.assertRaises(AssertionError):
                            _declared_legacy_commands(invalid)

    def test_handler_reader_ignores_comment_and_string_lookalikes(self) -> None:
        source = r'''
            // .invoke_handler(tauri::generate_handler![line_only,])
            /* .invoke_handler(tauri::generate_handler![comment_only,]) */
            let text = ".invoke_handler(tauri::generate_handler![string_only,])";
            let text = r#".invoke_handler(tauri::generate_handler![raw_only,])"#;
            builder.invoke_handler(tauri::generate_handler![module::real_command]);
        '''
        self.assertEqual(_registered_commands(source), {"real_command"})
        call = "builder.invoke_handler(tauri::generate_handler![module::real_command]);"
        for invalid in (
            source.replace("module::real_command", '"real_command"'),
            source.replace(call, ""),
            source.replace(call, call + call),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(AssertionError):
                    _registered_commands(invalid)


if __name__ == "__main__":
    unittest.main()
