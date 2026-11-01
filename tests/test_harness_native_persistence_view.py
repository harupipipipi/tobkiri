"""Real captured-loader/catalog bodies with a controlled portable IO fixture.

These tests do not qualify actual Wasm fuel, a physical Host owner, journal
cleanup, the original import environment, or original Secure class identity.
"""

from __future__ import annotations

import ast
import base64
import builtins
from contextlib import contextmanager
import hashlib
import importlib
from importlib.machinery import ModuleSpec
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts/wasm"),
                str(ROOT / "tools/python313-producer"),
                str(ROOT / "packs/local.ai-tool-bridge-ordered-python")]

# Genuine Host test dependencies, not substitutes in the captured namespace.
import jsonschema  # noqa: E402, F401
import packaging.specifiers  # noqa: E402, F401
import packaging.version  # noqa: E402, F401
import subprocess  # noqa: E402, F401
import plistlib  # noqa: E402, F401
from schema_probe.captured_loader import Finder, Loader, Tree, install  # noqa: E402
from harness_native import persistence_guard as guard  # noqa: E402
from harness_native import bootstrap  # noqa: E402
from harness_native.dispatcher import (  # noqa: E402
    AlreadyInitialized, Dispatcher, OriginalRuntime, PREFIX,
)
from harness_native.native_module_loader import ModulePathBindings  # noqa: E402
from harness_native.native_path import PathBridge  # noqa: E402
from harness_native.persistence_view import VIEW, SecurePersistenceError  # noqa: E402
from harness_native.source_package import assemble  # noqa: E402
from harness_native.value_graph import Registry, Scope, UnsupportedValue  # noqa: E402
import prepare_harness_native as prepare  # noqa: E402
import ordered_json_v2 as wire  # noqa: E402

SERVICE = "ecosystem.defaultspack.domain.runtime_v4.service"
SECURE = "tobkiri_protocol.secure_persistence"
ORIGIN = "/captured-catalog-persistence-fixture"
PREFIXES = ("core_runtime", "ecosystem", "tobkiri_protocol", "tobkiri_host")


def source_files():
    """Read the proof's fixed source inventory, without executing its modules."""
    paths = [guard.SERVICE, guard.COMPOSITION, guard.SECURE, guard.VIEW,
             "tools/python313-producer/harness_native/dispatcher.py",
             *(guard.RUNTIME + path for path in guard._PURE)]
    return {path: (ROOT / path).read_bytes() for path in paths}


class FixturePort:
    """Real reads of controlled fixture files; never a native authority proof."""

    def __init__(self, scope, paths):
        self.scope, self.paths, self.calls = scope, dict(paths), []
        self.retired = False

    def __call__(self, payload):
        if self.retired:
            raise UnsupportedValue("fixture IO retired")
        frame = wire.loads(payload)
        if (frame["scope"] != self.scope.wire()
                or frame["format"] != "io.tobkiri.native-catalog-request.v1"):
            raise AssertionError("not the active fixed catalog call")
        target = frame["target"]
        current = Path(self.paths[target["binding"]])
        for operation, argument in target["steps"]:
            if operation == "join":
                current /= argument
            elif operation == "parent":
                current = current.parent
            elif operation == "with-name":
                current = current.with_name(argument)
            elif operation == "with-suffix":
                current = current.with_suffix(argument)
            else:
                raise AssertionError("unexpected fixture path expression")
        operation = frame["operation"]
        self.calls.append((operation, current))
        if operation in {"root", "resolve"}:
            if operation == "resolve":
                current = current.resolve(strict=frame["args"][0])
            binding = "observed_" + str(len(self.calls))
            self.paths[binding] = str(current)
            value = {"kind": "path", "binding": binding,
                     "value": str(current), "steps": []}
        elif operation in {"is-symlink", "is-file", "is-dir"}:
            value = {"kind": "bool", "value": {
                "is-symlink": current.is_symlink,
                "is-file": current.is_file,
                "is-dir": current.is_dir,
            }[operation]()}
        elif operation == "read-bytes":
            value = {"kind": "bytes", "value": base64.b64encode(
                current.read_bytes()).decode("ascii")}
        else:
            raise AssertionError("unexpected native operation")
        return wire.dumps({"format": "io.tobkiri.native-inspection-response.v1",
                           "scope": self.scope.wire(), "result": value})


class ForeignLoader:
    """Self-consistent duck forgery, including the genuine source/hash bytes."""

    def __init__(self, finder):
        self.finder, self._source_tree = finder, finder.tree
        self.name, self.source = SERVICE, SERVICE.replace(".", "/") + ".py"

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        self.finder.executions += 1
        module.__file__ = self.finder.root + "/" + self.source
        exec(compile(self._source_tree.files[self.source], module.__file__, "exec"),
             vars(module))


class ForeignFinder:
    def __init__(self, files):
        self.tree, self.root, self.sealed = Tree(files), ORIGIN, True
        self.executions = 0

    def find_spec(self, fullname, path=None, target=None):
        if fullname != SERVICE:
            return None
        spec = ModuleSpec(fullname, ForeignLoader(self),
                          origin=self.root + "/" + SERVICE.replace(".", "/") + ".py")
        spec.has_location = True
        return spec


@contextmanager
def captured(package, root, *, with_capture=True, foreign=None):
    files = {name.removeprefix("source-package/tobkiri_runtime/"): raw
             for name, raw in package.items()
             if name.startswith("source-package/tobkiri_runtime/")}
    saved = {name: module for name, module in sys.modules.items()
             if name.split(".", 1)[0] in PREFIXES}
    for name in saved:
        del sys.modules[name]
    previous = sys.meta_path[:]
    scope = Scope(1, 2, 3, 4)
    index = {node.targets[0].id: ast.literal_eval(node.value)
             for node in ast.parse(package[
                 "source-package/harness_native_source_index.py"]).body}
    paths = {"module_" + str(i): str(ROOT / "tobkiri_runtime" / path)
             for i, (_name, path, _digest) in enumerate(index["MODULES"])}
    paths["catalog_root"] = str(root)
    port = FixturePort(scope, paths)
    bridge = PathBridge(scope, "posix", port, os_flags={"O_RDONLY": 0})
    anchors = {name: bridge.bind("module_" + str(i), paths["module_" + str(i)])
               for i, (name, _path, _digest) in enumerate(index["MODULES"])
               if name in index["PATH_MODULES"]}
    finder = install(files, {}, ORIGIN, ("ecosystem", "ecosystem.defaultspack"))
    finder.seal()
    if foreign is not None:
        if foreign == "duck":
            port.foreign = ForeignFinder(files)
        elif foreign == "same-class":
            port.foreign = Finder(files, {}, ORIGIN,
                                  ("ecosystem", "ecosystem.defaultspack"))
            port.foreign.seal()
        else:
            raise AssertionError("unknown test responder")
        sys.meta_path.insert(0, port.foreign)
    bindings = ModulePathBindings(
        bridge, anchors, _catalog_capture=(
            (finder, Finder, Loader, finder.tree) if with_capture else None),
    )
    bindings.install()
    try:
        yield finder, bridge, bindings, port
    finally:
        bindings.close()
        port.retired = True
        sys.meta_path[:] = previous
        for name in tuple(sys.modules):
            if name.split(".", 1)[0] in PREFIXES:
                del sys.modules[name]
        sys.modules.update(saved)


def write_bundle(root, case):
    root.mkdir()
    raw = (ROOT / "tobkiri_runtime/ecosystem/defaultspack/v4/"
           "defaults-basepack.base.v1.json").read_bytes()
    if case == "schema":
        raw = b'{}'
    (root / "entry.json").write_bytes(raw)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if case == "digest":
        digest = "sha256:" + "0" * 64
    lock = {"schema": "io.tobkiri.defaultspack-bundle-lock.v1", "entries": [
        {"path": "entry.json", "kind": "base", "digest": digest}]}
    (root / "bundle.lock.json").write_bytes(
        b'{' if case == "json" else json.dumps(lock).encode())
    if case == "missing-lock":
        (root / "bundle.lock.json").unlink()
    elif case == "symlink":
        actual = root.with_name("actual-bundle")
        root.rename(actual)
        root.symlink_to(actual, target_is_directory=True)


class CatalogPersistenceBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = assemble(prepare.captured())

    def test_actual_catalog_success_and_failure_paths_keep_owner_io_and_validation(self):
        # The strict Source proof covers ALL branches, including exception
        # matching. Runtime marker refusals alone cannot prove that property.
        receipt = guard.validate(source_files())
        self.assertEqual(receipt["catalog_success_and_failure_symbol_uses"], 0)
        manifest = json.loads(self.package["source-package/provenance.json"])
        self.assertEqual(manifest["catalog_host_dependency_replacement"], receipt)
        self.assertFalse(manifest["original_service_package"]["facade_modules_used_for_execution"])
        original_import = builtins.__import__

        def no_persistence_import(name, *args, **kwargs):
            if name in {SECURE, "secrets", "ctypes"}:
                raise AssertionError("unexpected Host persistence import")
            return original_import(name, *args, **kwargs)

        for case in ("success", "digest", "schema", "json", "missing-lock", "symlink"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "bundle"
                write_bundle(root, case)
                with captured(self.package, root) as (finder, bridge, bindings, port):
                    executions = []
                    original_execute = Loader.exec_module

                    def observe(owner, module):
                        if module.__name__ == SERVICE:
                            executions.append((owner, module))
                        return original_execute(owner, module)

                    with patch.object(Loader, "exec_module", observe), patch(
                        "builtins.__import__", no_persistence_import
                    ):
                        runtime = importlib.import_module(
                            "ecosystem.defaultspack.defaultspack.profile_runtime_composition"
                        )._DEFAULTSPACK_PROFILE_RUNTIME
                        load = lambda: bridge._load_catalog(
                            runtime.load_catalog, bridge.bind("catalog_root", str(root)))
                        if case == "success":
                            result = load()
                            self.assertIn("defaults-basepack", result.bases)
                        else:
                            error_type = importlib.import_module(
                                "tobkiri_protocol.bundle_catalog").BundleIntegrityError
                            with self.assertRaises(error_type) as raised:
                                load()
                            if case == "schema":
                                self.assertEqual(type(raised.exception.__cause__).__name__,
                                                 "SchemaValidationError")
                            elif case == "json":
                                self.assertIsNotNone(raised.exception.__cause__)
                    self.assertEqual(len(executions), 1)
                    owner, module = executions[0]
                    self.assertIs(module.__loader__, owner)
                    self.assertIs(module.__spec__.loader, owner)
                    self.assertIs(owner.finder, finder)
                    self.assertIs(owner._source_tree, finder.tree)
                    self.assertIs(owner._executed_module(), module)
                    self.assertEqual(finder.loaded.count(SERVICE), 1)
                    self.assertIs(module.BundledCatalog, sys.modules[
                        "tobkiri_protocol.bundle_catalog"].BundledCatalog)
                    self.assertIs(module.SecurePersistenceError, SecurePersistenceError)
                    self.assertNotIn(SECURE, sys.modules)
                    validation = sys.modules["tobkiri_protocol.validation"]
                    resources = validation.__loader__._tobkiri_schema_resources(validation)
                    self.assertIs(resources.tree, finder.tree)
                    self.assertTrue((resources / "base_definition_v4.schema.json").is_file())
                    self.assertIsNone(bridge._catalog_root)
                    operations = [operation for operation, _ in port.calls]
                    self.assertIn("is-symlink", operations)
                    if case != "symlink":
                        self.assertIn("read-bytes", operations)
                    if case in {"success", "digest", "schema"}:
                        self.assertIn("resolve", operations)
                        self.assertIn(("read-bytes", root / "entry.json"), port.calls)
                    before = len(port.calls)
                    bindings.close()
                    with self.assertRaises(UnsupportedValue):
                        module.SecureDirectory(root)
                    self.assertEqual(len(port.calls), before)

    def test_import_replacement_requires_active_catalog_and_the_actual_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            write_bundle(root, "success")
            with captured(self.package, root) as (finder, bridge, _, port):
                with self.assertRaises(UnsupportedValue):
                    importlib.import_module(SERVICE)
                self.assertNotIn(SERVICE, sys.modules)
                self.assertNotIn(SECURE, sys.modules)
                self.assertEqual(port.calls, [])

                def poison_then_import(_root):
                    original = Loader.exec_module

                    def poison(owner, module):
                        if module.__name__ == SERVICE:
                            owner._source_tree = Tree(dict(finder.tree.files))
                        return original(owner, module)

                    with patch.object(Loader, "exec_module", poison):
                        return importlib.import_module(SERVICE)

                with self.assertRaises(UnsupportedValue):
                    bridge._load_catalog(poison_then_import,
                                         bridge.bind("catalog_root", str(root)))
                self.assertIsNone(bridge._catalog_root)
                self.assertNotIn(SECURE, sys.modules)

    def test_fixed_view_rejects_fromlist_globals_spec_tree_and_finder_poison(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            write_bundle(root, "success")
            with captured(self.package, root) as (finder, bridge, _, _):
                def import_service(_root):
                    return importlib.import_module(SERVICE)

                module = bridge._load_catalog(import_service,
                    bridge.bind("catalog_root", str(root)))
                scoped = module.__builtins__["__import__"]
                exports = ("SecureDirectory", "SecurePersistenceError")

                def check(_root):
                    self.assertIs(scoped(SECURE, vars(module), None, exports, 0), VIEW)
                    for names in (("*",), ("SecureDirectory",),
                                  (*exports, "extra"), list(exports)):
                        with self.assertRaises(UnsupportedValue):
                            scoped(SECURE, vars(module), None, names, 0)
                    with self.assertRaises(UnsupportedValue):
                        scoped(SECURE, dict(vars(module)), None, exports, 0)
                    owner = module.__loader__
                    for target, name, replacement in (
                        (module, "__loader__", object()),
                        (module.__spec__, "loader", object()),
                        (module, "__file__", "/forged/source.py"),
                        (module.__spec__, "origin", "/forged/source.py"),
                        (owner, "finder", object()),
                        (finder, "tree", Tree(dict(finder.tree.files))),
                    ):
                        prior = getattr(target, name)
                        setattr(target, name, replacement)
                        try:
                            with self.assertRaises(UnsupportedValue):
                                scoped(SECURE, vars(module), None, exports, 0)
                        finally:
                            setattr(target, name, prior)
                    sys.meta_path.remove(finder)
                    try:
                        with self.assertRaises(UnsupportedValue):
                            scoped(SECURE, vars(module), None, exports, 0)
                    finally:
                        sys.meta_path.insert(1, finder)

                bridge._load_catalog(check, bridge.bind("catalog_root", str(root)))
                with self.assertRaises(UnsupportedValue):
                    scoped(SECURE, vars(module), None, exports, 0)
                self.assertNotIn(SECURE, sys.modules)

    def test_unavailable_marker_and_unreviewed_view_fields_never_succeed(self):
        directory = VIEW.SecureDirectory
        for use in (
            lambda: directory("/not-authority"),
            lambda: directory.open_lock,
            lambda: setattr(directory, "root", "/not-authority"),
            lambda: bool(directory),
            lambda: iter(directory),
            lambda: VIEW.unreviewed_export,
        ):
            with self.subTest(use=use), self.assertRaises(UnsupportedValue):
                use()
        self.assertEqual(SecurePersistenceError.__module__,
                         "harness_native.persistence_view")
        self.assertNotEqual(SecurePersistenceError.__module__, SECURE)
        registry = Registry(Scope(1, 2, 3, 4))
        for value in (directory, VIEW, SecurePersistenceError("unqualified")):
            with self.assertRaises(UnsupportedValue):
                registry.retain(value)

    def test_foreign_duck_and_same_class_responder_cannot_adopt_view(self):
        for kind in ("duck", "same-class"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "bundle"
                write_bundle(root, "success")
                with captured(self.package, root, foreign=kind) as (
                    finder, bridge, _, port
                ):
                    source = SERVICE.replace(".", "/") + ".py"
                    self.assertEqual(port.foreign.tree.files[source], finder.tree.files[source])
                    self.assertIsNot(port.foreign, finder)
                    with self.assertRaisesRegex(UnsupportedValue, "responder is not selected"):
                        bridge._load_catalog(lambda _: importlib.import_module(SERVICE),
                                             bridge.bind("catalog_root", str(root)))
                    self.assertNotIn(SECURE, sys.modules)
                    self.assertIsNone(bridge._catalog_root)
                    if kind == "duck":
                        self.assertEqual(port.foreign.executions, 0)
                    else:
                        self.assertNotIn(SERVICE, port.foreign.loaded)

    def test_missing_private_binding_keeps_path_only_and_refuses_view(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            write_bundle(root, "success")
            with captured(self.package, root, with_capture=False) as (
                _, bridge, _, _
            ):
                validation = importlib.import_module("tobkiri_protocol.validation")
                self.assertIs(validation.SCHEMA_DIR._bridge, bridge)
                with self.assertRaises(UnsupportedValue):
                    bridge._load_catalog(lambda _: importlib.import_module(SERVICE),
                                         bridge.bind("catalog_root", str(root)))
                self.assertNotIn(SECURE, sys.modules)


def binder(dispatcher):
    """Execute only the actual private app binder, without WIT fixture classes."""
    path = ROOT / "tools/python313-producer/harness_native/app.py"
    (node,) = [node for node in ast.parse(path.read_bytes()).body
               if isinstance(node, ast.FunctionDef) and node.name == "_bind_catalog_capture"]
    namespace = {"_dispatcher": dispatcher, "UnsupportedValue": UnsupportedValue,
                 "load_original_runtime": bootstrap.load_original_runtime}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[node.name]


class CatalogCaptureCompositionTests(unittest.TestCase):
    def test_entry_supplies_actual_install_and_same_imported_types_after_real_seal(self):
        path = ROOT / "tools/python313-producer/harness_native/entry_wrapper.py"
        tree = ast.parse(path.read_bytes())
        (fixed,) = [node for node in tree.body if isinstance(node, ast.ImportFrom)
                    and node.module == "harness_native_captured_loader"]
        self.assertEqual([node.name for node in fixed.names], ["Finder", "Loader", "install"])
        (installed,) = [node for node in tree.body if isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "_FINDER"
                                for t in node.targets)]
        self.assertEqual(installed.value.func.id, "install")
        calls = [(i, node.value) for i, node in enumerate(tree.body)
                 if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)]
        (seal,) = [i for i, call in calls if ast.unparse(call.func) == "_FINDER.seal"]
        ((bound, call),) = [(i, call) for i, call in calls
                            if ast.unparse(call.func) == "_bind_catalog_capture"]
        self.assertGreater(bound, seal)
        self.assertEqual([arg.id for arg in call.args], ["_FINDER", "Finder", "Loader"])

    def test_once_binding_preserves_dispatcher_observers_and_init_once_state(self):
        finder = install({"fixture.py": b""}, {}, "/capture-composition-fixture")
        previous = sys.meta_path[:]
        try:
            checkpoints, failures = [], []
            observer = lambda *args: failures.append(args)
            dispatcher = Dispatcher(bootstrap.load_original_runtime,
                                    init_checkpoint_observer=checkpoints.append,
                                    init_failure_observer=observer)
            bind = binder(dispatcher)
            with self.assertRaises(UnsupportedValue):
                bind(finder, Finder, Loader)
            finder.seal()
            bind(finder, Finder, Loader)
            selected = dispatcher._bootstrap
            self.assertIs(dispatcher._init_failure_observer, observer)
            self.assertIsNone(dispatcher._state)
            with self.assertRaises(UnsupportedValue):
                bind(finder, Finder, Loader)
            self.assertIs(dispatcher._bootstrap, selected)
            scope = Scope(1, 2, 3, 4)
            inputs = SimpleNamespace(modules=SimpleNamespace(install=lambda: None))
            original = OriginalRuntime(object(), (), ())
            with patch.object(bootstrap, "installed_inputs", return_value=inputs) as read:
                with patch.object(bootstrap, "_load_bound_runtime", return_value=original):
                    dispatcher.init(wire.dumps({"format": PREFIX + "init.v1",
                                                "scope": scope.wire()}))
            capture = read.call_args.kwargs["_catalog_capture"]
            self.assertIs(capture[0], finder)
            self.assertIs(capture[1], Finder)
            self.assertIs(capture[2], Loader)
            self.assertIs(capture[3], finder.tree)
            self.assertEqual(checkpoints, list(range(1, 8)))
            self.assertEqual(failures, [])
            with self.assertRaises(AlreadyInitialized):
                dispatcher.init("not another initialization")
        finally:
            sys.meta_path[:] = [f for f in previous if f is not finder]

    def test_import_failure_keeps_original_error_and_releases_only_binding_borrow(self):
        package = assemble(prepare.captured())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            write_bundle(root, "success")
            with captured(package, root) as (finder, bridge, installed, _):
                anchors = installed.anchors
                installed.close()
                modules = ModulePathBindings(bridge, anchors, _catalog_capture=(
                    finder, Finder, Loader, finder.tree))
                inputs = SimpleNamespace(modules=modules)
                failures = []
                dispatcher = Dispatcher(bootstrap.load_original_runtime,
                                        init_failure_observer=lambda *args: failures.append(args))
                bind = binder(dispatcher)
                bind(finder, Finder, Loader)
                selected = dispatcher._bootstrap
                original = ImportError("actual controlled import failure")
                with patch.object(bootstrap, "installed_inputs", return_value=inputs):
                    with patch.object(bootstrap, "_load_bound_runtime", side_effect=original):
                        with self.assertRaises(ImportError) as raised:
                            dispatcher.init(wire.dumps({"format": PREFIX + "init.v1",
                                                        "scope": Scope(1, 2, 3, 4).wire()}))
                self.assertIs(raised.exception, original)
                self.assertIs(dispatcher._init_failure, original)
                self.assertIs(dispatcher._bootstrap, selected)
                self.assertIsNone(dispatcher._state)
                self.assertIsNone(modules._catalog_capture)
                self.assertFalse(modules.installed)
                self.assertIn(finder, sys.meta_path)
                self.assertTrue(finder.sealed)
                self.assertIs(failures[0][2], original)
                with self.assertRaises(UnsupportedValue):
                    bind(finder, Finder, Loader)


class CatalogPersistenceSourceGuardTests(unittest.TestCase):
    def test_current_source_has_an_explicit_unqualified_replacement_receipt(self):
        receipt = guard.validate(source_files())
        self.assertEqual(receipt["scope"], "catalog-only")
        self.assertFalse(receipt["translated_exception_is_original_module_class"])
        self.assertFalse(receipt["host_persistence_connected"])
        self.assertFalse(receipt["runtime_execution_proven"])

    def test_success_failure_matching_class_and_reflection_changes_fail_closed(self):
        for code in (
            "SecureDirectory('/unexpected')",
            "isinstance(None, SecureDirectory)",
            "try:\n    raise OSError()\nexcept SecurePersistenceError:\n    pass",
            "class Unexpected(SecureDirectory):\n    pass",
            "globals()['SecurePersistenceError']",
        ):
            with self.subTest(code=code):
                files = source_files()
                files[guard.RUNTIME + "tobkiri_protocol/bundle_catalog.py"] += (
                    "\n" + code + "\n").encode()
                with self.assertRaisesRegex(ValueError, "success/failure Source closure"):
                    guard.validate(files)

    def test_service_symbol_placement_and_translated_class_drift_fail_closed(self):
        for path, extra in (
            (guard.SERVICE, b"\nSecureDirectory('/unexpected')\n"),
            (guard.SERVICE, b"\nclass Unexpected(SecurePersistenceError):\n    pass\n"),
        ):
            files = source_files()
            files[path] += extra
            with self.assertRaises(ValueError):
                guard.validate(files)
        files = source_files()
        files[guard.VIEW] = files[guard.VIEW].replace(
            b"class SecurePersistenceError(OSError):",
            b"class SecurePersistenceError(RuntimeError):")
        with self.assertRaisesRegex(ValueError, "private Host exception body"):
            guard.validate(files)

    def test_runtime_branch_and_method_inventory_cannot_inherit_receipt(self):
        files = source_files()
        files[guard.COMPOSITION] = files[guard.COMPOSITION].replace(
            b"return BundledCatalog.load(root)",
            b"return BundledCatalog.load(root) if root else None")
        with self.assertRaisesRegex(ValueError, "runtime Source closure"):
            guard.validate(files)
        files = source_files()
        path = "tools/python313-producer/harness_native/dispatcher.py"
        files[path] = files[path].replace(
            b'    "dynamic_profile_edges",',
            b'    "dynamic_profile_edges",\n    "activation_store",')
        with self.assertRaisesRegex(ValueError, "method inventory"):
            guard.validate(files)


if __name__ == "__main__":
    unittest.main()
