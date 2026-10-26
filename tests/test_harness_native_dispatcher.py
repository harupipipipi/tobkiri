"""Portable dispatcher protocol tests, not execution of original Host or Wasm."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "tools/python313-producer"),
    str(ROOT / "packs/local.ai-tool-bridge-ordered-python"),
    str(ROOT / "scripts/wasm"),
]

from harness_native.dispatcher import (  # noqa: E402
    AlreadyInitialized,
    Dispatcher,
    METHODS,
    MethodRaised,
    NotInitialized,
    OriginalRuntime,
    PREFIX,
)
from harness_native.value_graph import (  # noqa: E402
    InvalidFrame,
    InvalidReference,
    RecordType,
    Scope,
    UnsupportedValue,
)
from harness_native.native_path import NativePath, PathBridge  # noqa: E402
import ordered_json_v2 as wire  # noqa: E402
import prepare_harness_native as prepare  # noqa: E402


MISSING = object()


class PrivateError(Exception):
    def __str__(self) -> str:
        raise AssertionError("private exception must not be formatted")

    def __repr__(self) -> str:
        raise AssertionError("private exception must not be formatted")


class PortableRuntime:
    """Explicit test fixture for argument/return behavior, never a fake Host."""

    def __init__(self) -> None:
        self.calls = []
        self.error = PrivateError("private filename and original args")
        self.error.__cause__ = self.error

    def bootstrap_profile_id(self):
        return "fixture-id"

    def bundled_profile_root(self):
        self.calls.append("bundled_profile_root")
        return self.root_reference

    def host_resource_root(self):
        self.calls.append("host_resource_root")
        return self.root_reference

    def setup_preview(self, listing=MISSING, **kwargs):
        self.calls.append((listing, kwargs))
        return "omitted" if listing is MISSING else listing

    def _confirmation(self, first, *, second):
        self.calls.append((first, second))
        return [first, second]

    def denied(self, message):
        return self.error

    def resolve_profile(self):
        raise self.error

    def load_catalog(self):
        raise MemoryError("execution failure")

    def dynamic_profile_edges(self):
        raise LookupError("not in closed inventory")


def original(runtime=None):
    return OriginalRuntime(
        runtime or PortableRuntime(),
        (
            RecordType("type-error", TypeError),
            RecordType("private-error", PrivateError),
            RecordType("native-path", NativePath, native_binding=True),
        ),
        (TypeError, PrivateError),
    )


class DispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = PortableRuntime()
        self.scope = Scope(1, 2, 3, 4)
        self.dispatcher = Dispatcher(lambda _scope: original(self.runtime))
        self.dispatcher.init(
            wire.dumps({"format": PREFIX + "init.v1", "scope": self.scope.wire()})
        )
        self.registry = self.dispatcher._current().registry

    def frame(self, kind, **fields):
        return wire.dumps(
            {"format": PREFIX + kind + ".v1", "scope": self.scope.wire(), **fields}
        )

    def call(self, name, args=(), kwargs=()):
        result = self.dispatcher.call(
            name,
            self.frame(
                "call",
                args=[r.wire() for r in args],
                kwargs=[[k, v.wire()] for k, v in kwargs],
            ),
        )
        return self.registry.borrow(self.registry.reference(json.loads(result)["root"]))

    def test_invalid_init_reinit_and_store_separation_do_not_replace_state(self):
        count = []
        other = Dispatcher(lambda _scope: count.append(1) or original())
        with self.assertRaises(NotInitialized):
            other.import_values("{}")
        for scope in (
            [True, "2", "3", "4"],
            ["0", "2", "3", "4"],
            ["1", "2", "3", "10000000000000000"],
        ):
            with self.assertRaises(InvalidFrame):
                other.init(wire.dumps({"format": PREFIX + "init.v1", "scope": scope}))
        self.assertEqual(count, [])
        other.init(
            wire.dumps(
                {"format": PREFIX + "init.v1", "scope": Scope(1, 2, 7, 1).wire()}
            )
        )
        value = []
        ref = self.registry.retain(value)
        with self.assertRaises(InvalidReference):
            other._current().registry.reference(ref.wire())
        with self.assertRaises(AlreadyInitialized):
            self.dispatcher.init("invalid")
        self.assertIs(self.registry.borrow(ref), value)
        self.assertIs(self.dispatcher._current().runtime, self.runtime)

    def test_fixed_dispatch_preserves_actual_alias_and_omitted_vs_none(self):
        self.assertEqual(self.call("setup_preview"), "omitted")
        none = self.registry.retain(None)
        self.assertIsNone(self.call("setup_preview", [none]))
        self.call("setup_preview", kwargs=[("z", none), ("a", none)])
        self.assertEqual(list(self.runtime.calls[-1][1]), ["z", "a"])
        shared = []
        ref = self.registry.retain(shared)
        result = self.call("_confirmation", [ref], [("second", ref)])
        self.assertIs(result[0], shared)
        self.assertIs(result[1], shared)
        self.assertIs(self.runtime.calls[-1][0], self.runtime.calls[-1][1])
        self.assertEqual(METHODS, prepare.METHODS)

    def test_fixed_getters_return_the_same_opaque_native_reference(self):
        def unavailable(_payload):
            raise AssertionError("portable transport test must not inspect a target")

        bridge = PathBridge(self.scope, "posix", unavailable, os_flags={"O_RDONLY": 0})
        native = bridge.bind("fixture-root", "/not-disclosed-by-getter")
        self.runtime.root_reference = native
        refs = []
        for method in ("bundled_profile_root", "host_resource_root"):
            packet = json.loads(self.dispatcher.call(
                method, self.frame("call", args=[], kwargs=[]),
            ))
            reference = self.registry.reference(packet["root"])
            self.assertIs(self.registry.borrow(reference), native)
            refs.append(reference)
            view = json.loads(self.registry.describe(reference))
            self.assertEqual(view["nodes"][0]["fields"], [])
            self.assertTrue(view["nodes"][0]["opaque_native_binding"])
            self.assertNotIn("not-disclosed", json.dumps(view))
        self.assertEqual(refs[0], refs[1])
        self.assertEqual(self.runtime.calls,
                         ["bundled_profile_root", "host_resource_root"])
        for method in ("activation_store", "active_profile", "is_resolution_denied",
                       "defaultspack_host_resource_root", "__getattribute__"):
            with self.assertRaises(InvalidFrame):
                self.dispatcher.call(method, self.frame("call", args=[], kwargs=[]))

    def test_failed_bootstrap_does_not_commit_or_replace_initialization(self):
        attempts = []

        def bootstrap(_scope):
            attempts.append(1)
            if len(attempts) == 1:
                raise ImportError("original native facade is missing")
            return original()

        dispatcher = Dispatcher(bootstrap)
        with self.assertRaises(ImportError):
            dispatcher.init(self.frame("init"))
        with self.assertRaises(NotInitialized):
            dispatcher.import_values("{}")
        dispatcher.init(self.frame("init"))
        self.assertEqual(len(attempts), 2)
        self.assertIsNotNone(dispatcher._current())

    def test_init_observer_keeps_actual_factory_exception_and_bare_reraise(self):
        for cls in (ValueError, MemoryError, RecursionError, PrivateError):
            with self.subTest(kind=cls):
                failure = cls("original failure")
                seen = []

                def factory(_scope):
                    raise failure

                def observer(scope, phase, error):
                    seen.append((scope, phase, error))
                    raise RuntimeError("observer failure must not escape")

                dispatcher = Dispatcher(factory, init_failure_observer=observer)
                try:
                    dispatcher.init(self.frame("init"))
                except BaseException as error:
                    self.assertIs(error, failure)
                    names = []
                    trace = error.__traceback__
                    while trace is not None:
                        names.append(trace.tb_frame.f_code.co_name)
                        trace = trace.tb_next
                    self.assertEqual(names[-2:], ["init", "factory"])
                else:
                    self.fail("the original factory failure must be reraised")
                self.assertEqual(seen, [(self.scope, "original-factory", failure)])
                self.assertIs(dispatcher._init_failure, failure)
                self.assertIsNone(dispatcher._state)

    def test_registry_failure_is_observed_before_state_commit(self):
        seen = []
        failure = MemoryError("registry allocation failure")
        dispatcher = Dispatcher(
            lambda _scope: original(),
            init_failure_observer=lambda *values: seen.append(values),
        )
        with patch("harness_native.dispatcher.Registry", side_effect=failure):
            try:
                dispatcher.init(self.frame("init"))
            except BaseException as error:
                self.assertIs(error, failure)
            else:
                self.fail("registry allocation failure must escape unchanged")
        self.assertEqual(seen, [(self.scope, "transport-registry", failure)])
        self.assertIs(dispatcher._init_failure, failure)
        self.assertEqual(dispatcher._init_failure_phase, "transport-registry")
        with self.assertRaises(NotInitialized):
            dispatcher._current()

    def test_next_init_clears_old_failure_even_before_scope_validation(self):
        failure = ValueError("old generation")
        attempts = []

        def factory(scope):
            attempts.append(scope)
            if len(attempts) == 1:
                raise failure
            self.assertIsNone(dispatcher._init_failure)
            return original()

        dispatcher = Dispatcher(factory, init_failure_observer=lambda *_args: None)
        with self.assertRaises(ValueError):
            dispatcher.init(self.frame("init"))
        with self.assertRaises(InvalidFrame):
            dispatcher.init("invalid")
        self.assertIsNone(dispatcher._init_failure)
        self.assertIsNone(dispatcher._init_failure_scope)
        self.assertIsNone(dispatcher._init_failure_phase)
        next_scope = Scope(1, 2, 3, self.scope.generation + 1)
        dispatcher.init(wire.dumps({"format": PREFIX + "init.v1",
                                    "scope": next_scope.wire()}))
        self.assertEqual(dispatcher._current().registry.scope, next_scope)
        self.assertEqual(attempts, [self.scope, next_scope])

    def test_original_binder_type_errors_are_not_transport_errors(self):
        ref = self.registry.retain([])
        for args, kwargs in (
            ([], []),
            ([ref, ref], []),
            ([ref], [("first", ref)]),
            ([ref], [("unknown", ref)]),
        ):
            with self.subTest(args=len(args), kwargs=[k for k, _ in kwargs]):
                with self.assertRaises(MethodRaised) as raised:
                    self.call("_confirmation", args, kwargs)
                packet = json.loads(raised.exception.details)
                error = self.registry.borrow(self.registry.reference(packet["root"]))
                self.assertIs(type(error), TypeError)
        self.assertEqual(self.runtime.calls, [])

    def test_invalid_frames_validate_all_refs_before_original_call(self):
        ref = self.registry.retain([])
        good = {
            "format": PREFIX + "call.v1",
            "scope": self.scope.wire(),
            "args": [ref.wire()],
            "kwargs": [],
        }
        bad = [
            dict(good, export="load_catalog"),
            dict(good, scope=["1", "2", "8", "4"]),
            dict(good, kwargs=[["x", ref.wire()], ["x", ref.wire()]]),
            dict(good, kwargs=[["x", {**ref.wire(), "object": "ffff"}]]),
        ]
        for frame in bad:
            with self.assertRaises((InvalidFrame, InvalidReference)):
                self.dispatcher.call("setup_preview", wire.dumps(frame))
        with self.assertRaises(InvalidFrame):
            self.dispatcher.call("__getattribute__", wire.dumps(good))
        self.assertEqual(self.runtime.calls, [])

    def test_returned_and_raised_exception_keep_one_private_original_object(self):
        returned = self.call("denied", [self.registry.retain("unused")])
        self.assertIs(returned, self.runtime.error)
        with self.assertRaises(MethodRaised) as raised:
            self.call("resolve_profile")
        packet = json.loads(raised.exception.details)
        error = self.registry.borrow(self.registry.reference(packet["root"]))
        self.assertIs(error, returned)
        self.assertIs(error.__cause__, error)
        view = self.registry.describe(self.registry.reference(packet["root"]))
        self.assertNotIn("private filename", view)
        self.assertEqual(json.loads(view)["nodes"][0]["fields"], [])

    def test_resource_failures_escape_and_unknown_method_errors_stay_unsupported(self):
        with self.assertRaises(MemoryError):
            self.call("load_catalog")
        with self.assertRaises(UnsupportedValue):
            self.call("dynamic_profile_edges")
        invalid = OriginalRuntime(
            self.runtime, (RecordType("memory", MemoryError),), (MemoryError,)
        )
        with self.assertRaises(UnsupportedValue):
            d = Dispatcher(lambda _scope: invalid)
            d.init(self.frame("init"))
        invalid = OriginalRuntime(
            self.runtime,
            (RecordType("private", PrivateError, ("args",)),),
            (PrivateError,),
        )
        with self.assertRaises(UnsupportedValue):
            d = Dispatcher(lambda _scope: invalid)
            d.init(self.frame("init"))

    def test_import_copy_append_set_describe_release_share_original_objects(self):
        imported = json.loads(
            self.dispatcher.import_values(
                self.frame(
                    "value-input",
                    nodes=[{"kind": "dict", "items": []}],
                    root={"node": 0},
                )
            )
        )
        target = imported["root"]
        value = self.registry.retain([])
        key = self.registry.retain("items")
        self.dispatcher.set_data_item(
            self.frame("value-set", target=target, key=key.wire(), item=value.wire())
        )
        copied = json.loads(
            self.dispatcher.copy_data_dictionary(
                self.frame("value-copy-dictionary", source=target)
            )
        )
        original_value = self.registry.borrow(self.registry.reference(target))
        copy_value = self.registry.borrow(self.registry.reference(copied["root"]))
        self.assertIsNot(copy_value, original_value)
        self.assertIs(copy_value["items"], original_value["items"])
        appendable = self.registry.retain(original_value["items"], append=True)
        before = len(self.registry._objects)
        self.dispatcher.append_data_item(
            self.frame("value-append", target=appendable.wire(), item=key.wire())
        )
        self.assertEqual(copy_value["items"], ["items"])
        self.assertEqual(len(self.registry._objects), before)
        self.dispatcher.describe_value(self.frame("value-describe", root=target))
        self.dispatcher.release_values(
            self.frame("values-release", refs=[copied["root"]])
        )
        with self.assertRaises(InvalidReference):
            self.registry.reference(copied["root"])
        self.assertIs(self.registry.borrow(appendable), original_value["items"])

    def test_readonly_mutation_is_invalid_reference_and_real_dict_error_is_method_raised(
        self,
    ):
        readonly = self.registry.retain([])
        key = self.registry.retain("item")
        with self.assertRaises(InvalidReference):
            self.dispatcher.append_data_item(
                self.frame("value-append", target=readonly.wire(), item=key.wire())
            )
        target = self.registry.retain({}, set_items=True)
        with self.assertRaises(MethodRaised) as raised:
            self.dispatcher.set_data_item(
                self.frame(
                    "value-set",
                    target=target.wire(),
                    key=readonly.wire(),
                    item=key.wire(),
                )
            )
        error = self.registry.borrow(
            self.registry.reference(json.loads(raised.exception.details)["root"])
        )
        self.assertIs(type(error), TypeError)
        self.assertEqual(self.registry.borrow(target), {})

    def test_final_encoding_failure_rolls_back_new_holds(self):
        packet = self.frame(
            "value-input", nodes=[{"kind": "list", "items": []}], root={"node": 0}
        )
        call = self.frame("call", args=[], kwargs=[])
        with patch(
            "harness_native.dispatcher.wire.dumps",
            side_effect=MemoryError("encoder allocation"),
        ):
            with self.assertRaises(MemoryError):
                self.dispatcher.import_values(packet)
            with self.assertRaises(MemoryError):
                self.dispatcher.call("bootstrap_profile_id", call)
        self.assertEqual(self.registry._objects, {})
        self.assertEqual(self.registry._identity, {})
        self.assertEqual(self.registry._permissions, {})

    def test_description_explicitly_reports_only_new_holds(self):
        existing = []
        root = self.registry.retain([existing, {}])
        child = self.registry.retain(existing)
        view = json.loads(self.registry.describe(root))
        self.assertEqual(len(view["owned"]), 1)
        self.assertNotIn(format(root.object_id, "x"), view["owned"])
        self.assertNotIn(format(child.object_id, "x"), view["owned"])

    def test_thin_app_uses_fixed_methods_and_one_shared_dispatcher_without_importing_bindings(
        self,
    ):
        path = ROOT / "tools/python313-producer/harness_native/app.py"
        tree = ast.parse(path.read_text())
        classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
        runtime = [n for n in classes["Runtime"].body if isinstance(n, ast.FunctionDef)]
        self.assertEqual(
            [n.name for n in runtime],
            ["init", *["confirmation" if n == "_confirmation" else n for n in METHODS]],
        )
        for node, name in zip(runtime[1:], METHODS, strict=True):
            call = node.body[0].value
            self.assertEqual(call.args[1].value, name)
        assignments = [n for n in tree.body if isinstance(n, ast.Assign)]
        dispatcher_assignment, = [
            n for n in assignments
            if any(isinstance(t, ast.Name) and t.id == "_dispatcher" for t in n.targets)
        ]
        self.assertEqual(
            [(kw.arg, kw.value.id) for kw in dispatcher_assignment.value.keywords],
            [("init_failure_observer", "report_init_failure")],
        )
        invoke, = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                   and n.name == "_invoke"]
        self.assertEqual([ast.unparse(h.type) for h in invoke.body[0].handlers], [
            "NotInitialized", "AlreadyInitialized", "InvalidFrame",
            "InvalidReference", "UnsupportedValue", "MethodRaised",
        ])
        self.assertEqual(
            sum(
                any(
                    isinstance(t, ast.Name) and t.id == "_dispatcher" for t in n.targets
                )
                for n in assignments
            ),
            1,
        )
        self.assertFalse(
            any(
                n.name == "__init__"
                for cls in classes.values()
                for n in cls.body
                if isinstance(n, ast.FunctionDef)
            )
        )


if __name__ == "__main__":
    unittest.main()
