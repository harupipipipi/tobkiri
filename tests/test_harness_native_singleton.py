"""Original pure declaration/registration tests, not SDK imports or Wasm proof.

Only the unchanged class and registration definitions are executed in isolated
namespaces. Native services, runtime methods and the original module preambles
are never imported or executed by these lightweight tests.
"""
from __future__ import annotations

import ast
from pathlib import Path
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
PORT = ROOT / "tobkiri_runtime/core_runtime/profile_runtime_port.py"
COMPOSITION = ROOT / "tobkiri_runtime/ecosystem/defaultspack/defaultspack/profile_runtime_composition.py"


def declarations(path: Path, names: set[str]) -> list[ast.stmt]:
    """Select complete named declarations, retaining their real source spans."""
    nodes = [node for node in ast.parse(path.read_text()).body
             if getattr(node, "name", None) in names]
    if {node.name for node in nodes} != names:
        raise AssertionError("original declaration inventory changed")
    return nodes


def execute(nodes: list[ast.stmt], namespace: dict, path: Path) -> None:
    """Avoid eager annotation evaluation without executing any source imports."""
    future = ast.ImportFrom(module="__future__", names=[
        ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[]))
    exec(compile(module, str(path), "exec"), namespace)


def original_registration() -> tuple[dict, dict]:
    """Use actual source bodies and an ordinary lock, with no Host or Profile."""
    port = {"__name__": "core_runtime.profile_runtime_port",
            "_LOCK": threading.RLock(), "_PROFILE_RUNTIME": None}
    execute(declarations(PORT, {
        "ProfileRuntimeUnavailable", "ProfileRuntimeAlreadyConfigured",
        "register_profile_runtime", "require_profile_runtime",
    }), port, PORT)
    nodes = declarations(COMPOSITION, {
        "DefaultspackProfileRuntime", "install_defaultspack_profile_runtime",
    })
    cls, = [node for node in nodes if isinstance(node, ast.ClassDef)]
    if cls.bases or cls.keywords or cls.decorator_list:
        raise AssertionError("original runtime gained class construction effects")
    for method in cls.body:
        if not isinstance(method, ast.FunctionDef):
            if not (isinstance(method, ast.Expr) and isinstance(method.value, ast.Constant)):
                raise AssertionError("original runtime gained class-level effects")
            continue
        if method.name in {"__init__", "__new__"} or method.decorator_list:
            raise AssertionError("original runtime gained construction effects")
        for value in [*method.args.defaults, *method.args.kw_defaults]:
            if value is not None:
                ast.literal_eval(value)
    singleton, = [node for node in ast.parse(COMPOSITION.read_text()).body
                  if isinstance(node, ast.Assign) and any(
                      isinstance(target, ast.Name)
                      and target.id == "_DEFAULTSPACK_PROFILE_RUNTIME"
                      for target in node.targets)]
    if not (isinstance(singleton.value, ast.Call)
            and isinstance(singleton.value.func, ast.Name)
            and singleton.value.func.id == "DefaultspackProfileRuntime"
            and not singleton.value.args and not singleton.value.keywords):
        raise AssertionError("original singleton construction changed")
    install, = [node for node in nodes if isinstance(node, ast.FunctionDef)]
    composition = {
        "__name__": "ecosystem.defaultspack.defaultspack.profile_runtime_composition",
        "register_profile_runtime": port["register_profile_runtime"],
    }
    execute([cls, singleton, install], composition, COMPOSITION)
    return port, composition


class OriginalSingletonTests(unittest.TestCase):
    def test_candidate_borrows_the_original_singleton_without_moving_installation(self):
        path = ROOT / "tools/python313-producer/harness_native/bootstrap.py"
        load, = declarations(path, {"_load_bound_runtime"})
        imports = [node for node in load.body if isinstance(node, ast.ImportFrom)]
        original, = [node for node in imports
                     if node.module == "ecosystem.defaultspack.defaultspack.profile_runtime_composition"]
        self.assertIn("_DEFAULTSPACK_PROFILE_RUNTIME", [n.name for n in original.names])
        result, = [node for node in load.body if isinstance(node, ast.Return)]
        self.assertEqual(result.value.args[0].id, "_DEFAULTSPACK_PROFILE_RUNTIME")
        calls = [n.func.id for n in ast.walk(load)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        self.assertNotIn("DefaultspackProfileRuntime", calls)
        self.assertNotIn("install_defaultspack_profile_runtime", calls)

    def test_original_installation_preserves_identity_and_unregistered_failure(self):
        port, composition = original_registration()
        runtime = composition["_DEFAULTSPACK_PROFILE_RUNTIME"]
        with self.assertRaises(port["ProfileRuntimeUnavailable"]):
            port["require_profile_runtime"]()
        for _ in range(2):
            self.assertIs(composition["install_defaultspack_profile_runtime"](), runtime)
            self.assertIs(port["require_profile_runtime"](), runtime)
        with self.assertRaises(port["ProfileRuntimeAlreadyConfigured"]):
            port["register_profile_runtime"](object())
        self.assertIs(port["require_profile_runtime"](), runtime)

    def test_original_installation_cannot_replace_an_existing_other_object(self):
        port, composition = original_registration()
        existing = object()
        port["register_profile_runtime"](existing)
        with self.assertRaises(port["ProfileRuntimeAlreadyConfigured"]):
            composition["install_defaultspack_profile_runtime"]()
        self.assertIs(port["require_profile_runtime"](), existing)


if __name__ == "__main__":
    unittest.main()
