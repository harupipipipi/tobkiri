"""Source contract checks for the whole native harness WIT surface.

These checks intentionally do not claim that a WIT parser or component build
accepted the contract. They pin its source-level exports and transport shapes.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/wasm"))
sys.path.insert(0, str(ROOT / "tools/python313-producer"))

import prepare_harness_native as prepare  # noqa: E402
from harness_native import source_package  # noqa: E402


WIT_PATH = ROOT / "tools/python313-producer/harness_native/native.wit"
WIT = WIT_PATH.read_text(encoding="utf-8")
NATIVE_BINDINGS_PATH = ROOT / (
    "tools/python313-producer/harness_native/deps/native-bindings/bindings.wit"
)
NATIVE_BINDINGS_WIT = NATIVE_BINDINGS_PATH.read_text(encoding="utf-8")


def _block(source: str, header: str) -> str:
    """Return a brace-balanced WIT declaration body for a simple header."""
    match = re.search(rf"(?m)^\s*{re.escape(header)}\s*\{{", source)
    if match is None:
        raise AssertionError(f"missing WIT block: {header}")
    start = match.end()
    depth = 1
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index]
    raise AssertionError(f"unterminated WIT block: {header}")


def _functions(body: str) -> dict[str, str]:
    """Extract one-line WIT function signatures in declaration order."""
    return {
        name: signature.rstrip("; \t\r\n")
        for name, signature in re.findall(
            r"(?m)^\s*([a-z][a-z0-9-]*):\s*(func\([^;\n]*;\s*$)", body,
        )
    }


class HarnessNativeWitTests(unittest.TestCase):
    """Pin original method scope and the declared whole-native ABI."""

    def test_wit_methods_cover_all_original_source_methods_without_scope_loss(self) -> None:
        expected_methods = (
            "bootstrap-profile-id",
            "setup-profile-preview",
            "confirmation",
            "bootstrap-confirmation",
            "profile-confirmation",
            "setup-listing",
            "setup-preview",
            "setup-activation-decision",
            "setup-activation-success",
            "setup-activation-failure",
            "retired-setup-response",
            "load-catalog",
            "catalog-with-packs",
            "catalog-with-profiles",
            "resolve-profile",
            "dynamic-profile-edges",
            "denied",
        )
        source_methods = tuple(
            "confirmation" if name == "_confirmation" else name.replace("_", "-")
            for name in prepare.METHODS
        )
        self.assertEqual(len(prepare.METHODS), 17)
        self.assertEqual(source_package._METHODS, prepare.METHODS)
        self.assertEqual(source_methods, expected_methods)

        runtime = _block(WIT, "interface runtime")
        functions = _functions(runtime)
        self.assertEqual(
            tuple(name for name in functions if name != "init"), expected_methods,
        )
        self.assertEqual(len(functions), 18)
        for name in expected_methods:
            self.assertEqual(
                functions[name], "func(payload: string) -> result<string, error>",
            )

        self.assertIn("closed frame containing scope, version, and typed graph refs", WIT)
        self.assertIn("Original Python signatures and bodies are assembled verbatim", WIT)

    def test_error_record_and_error_codes_have_fixed_shape_and_order(self) -> None:
        runtime = _block(WIT, "interface runtime")
        error_codes = _block(runtime, "enum error-code")
        codes = tuple(re.findall(r"(?m)^\s*([a-z][a-z0-9-]*),\s*$", error_codes))
        self.assertEqual(codes, (
            "not-initialized",
            "already-initialized",
            "invalid-frame",
            "invalid-reference",
            "unsupported-value",
            "method-raised",
        ))

        error_record = _block(runtime, "record error")
        fields = tuple(re.findall(
            r"(?m)^\s*([a-z][a-z0-9-]*):\s*([a-z][a-z0-9-]*),?\s*$",
            error_record,
        ))
        self.assertEqual(fields, (
            ("code", "error-code"),
            ("message", "string"),
            ("details", "string"),
        ))

    def test_init_and_transport_helpers_use_unit_or_string_results(self) -> None:
        runtime = _block(WIT, "interface runtime")
        self.assertEqual(
            _functions(runtime)["init"],
            "func(payload: string) -> result<_, error>",
        )

        transport = _block(WIT, "interface transport")
        functions = _functions(transport)
        string_helpers = (
            "import-values", "describe-value", "copy-data-dictionary",
        )
        unit_helpers = (
            "release-values", "append-data-item", "set-data-item",
        )
        self.assertEqual(tuple(functions), string_helpers + unit_helpers)
        for name in string_helpers:
            self.assertEqual(functions[name], "func(payload: string) -> result<string, error>")
        for name in unit_helpers:
            self.assertEqual(functions[name], "func(payload: string) -> result<_, error>")

    def test_world_has_the_exact_native_import_and_two_exports(self) -> None:
        self.assertIn("package tobkiri-ui:harness-native@1.0.0;", WIT)
        world = _block(WIT, "world harness-native")
        statements = tuple(
            line.strip() for line in world.splitlines()
            if line.strip() and not line.strip().startswith("///")
        )
        self.assertEqual(statements, (
            "import tobkiri-native:harness/bindings@1.0.0;",
            "export runtime;",
            "export transport;",
        ))
        imports = tuple(re.findall(r"(?m)^\s*import\s+([^;]+);", WIT))
        self.assertEqual(imports, ("tobkiri-native:harness/bindings@1.0.0",))
        self.assertNotRegex(world, r"\binclude\b")

        runtime = _block(WIT, "interface runtime")
        self.assertNotRegex(runtime, r"\bimport\b|\binclude\b")

    def test_dedicated_native_bindings_are_exact_and_document_the_effect_boundary(self) -> None:
        self.assertIn("package tobkiri-native:harness@1.0.0;", NATIVE_BINDINGS_WIT)
        bindings = _block(NATIVE_BINDINGS_WIT, "interface bindings")
        self.assertEqual(_functions(bindings), {
            "inventory": "func() -> string",
            "inspect": "func(payload: string) -> string",
        })

        for requirement in (
            "no filesystem",
            "stat, read, or prefetch",
            "context, account, Store, and invocation",
            "before runtime bootstrap",
            "Opaque IDs confer no authority",
            "checks every target selector",
            "reach point, owner, target, operation, phase,",
            "occurrence, and deadline",
            "explicit bounded DATA",
            "kernel resource and cancellation failures trap",
        ):
            self.assertIn(requirement, NATIVE_BINDINGS_WIT)

        declarations = WIT + NATIVE_BINDINGS_WIT
        interfaces = tuple(re.findall(
            r"(?m)^\s*interface\s+([a-z][a-z0-9-]*)\s*\{", declarations,
        ))
        self.assertEqual(interfaces, ("runtime", "transport", "bindings"))
        self.assertNotRegex(
            declarations,
            r"(?i)\b(?:core|gate|ordinary-pack|lifecycle)\b",
        )


if __name__ == "__main__":
    unittest.main()
