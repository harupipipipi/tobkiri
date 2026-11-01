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


class FactoryDeniedFixture(Exception):
    """Portable ABI fixture only; the original factory has independent CI."""

    def __str__(self):
        raise AssertionError("factory observations must not format exceptions")

    def __repr__(self):
        raise AssertionError("factory observations must not format exceptions")

    def __getattribute__(self, name):
        if name in {"args", "__cause__", "__context__", "__suppress_context__", "__class__"}:
            raise AssertionError("factory observations must use builtin descriptors")
        return super().__getattribute__(name)


class FactoryRuntimeFixture:
    def __init__(self):
        self.last = None
        self.reuse = False
        self.failure = None

    def denied(self, message):
        if self.failure is not None:
            raise self.failure
        if not self.reuse:
            self.last = FactoryDeniedFixture(message)
        return self.last

    def resolve_profile(self):
        raise self.last

    def setup_preview(self):
        return [self.last]


class IdentityRuntimeFixture:
    """Controlled raw getter inputs, not the original Pack implementation."""

    def __init__(self):
        self.value = "fixture-id"
        self.failure = None

    def bootstrap_profile_id(self):
        if self.failure is not None:
            raise self.failure
        return self.value

    def setup_preview(self):
        return self.value if self.failure is None else self.failure


class IdentityObservationTransportTests(unittest.TestCase):
    """Top-level/provenance ABI; original validator and native proof are separate."""

    def setUp(self):
        self.runtime = IdentityRuntimeFixture()
        self.scope = Scope(1, 2, 3, 4)
        self.errors = (ValueError, OSError, FileNotFoundError, TypeError,
                       RuntimeError, KeyError, FactoryDeniedFixture)
        self.dispatcher = Dispatcher(lambda _scope: OriginalRuntime(
            self.runtime,
            tuple(RecordType(kind, cls) for kind, cls in zip((
                "value-error", "os-error", "file-not-found-error", "type-error",
                "runtime-error", "key-error", "profile-resolution-denied"), self.errors)),
            self.errors,
        ))
        self.dispatcher.init(self.frame("init"))
        self.registry = self.dispatcher._current().registry

    def frame(self, kind, **fields):
        return wire.dumps({"format": PREFIX + kind + ".v1",
                           "scope": self.scope.wire(), **fields})

    def packet(self):
        call = self.frame("call", args=[], kwargs=[])
        if self.runtime.failure is None:
            return json.loads(self.dispatcher.call("bootstrap_profile_id", call))
        with self.assertRaises(MethodRaised) as caught:
            self.dispatcher.call("bootstrap_profile_id", call)
        return json.loads(caught.exception.details)

    def view(self, packet):
        return json.loads(self.dispatcher.describe_value(
            self.frame("bootstrap-identity-describe", root=packet["root"])))

    def test_actual_string_codepoints_preserve_surrogates_and_129_length_branch(self):
        for value in ("", "a", "e\u0301", "İΣ", "\ud800x\udfff", "𐀀" * 128, "x" * 129,
                      "secret contents" * 1000):
            self.runtime.value = value
            packet = self.packet()
            view = self.view(packet)
            self.assertEqual(view, {
                "format": PREFIX + "bootstrap-identity-view.v1",
                "scope": self.scope.wire(), "root": packet["root"],
                "origin": "getter-returned", "kind": "str",
                "registered_builtin_kind": "str", "exception": None, "owned": [],
                "string": {"length": min(len(value), 129),
                           "codepoints": list(map(ord, value)) if len(value) <= 128 else []},
            })

    def test_non_strings_disclose_only_top_level_type_without_visiting_contents(self):
        private = object()
        for value, kind in ((None, "none"), (False, "bool"), (10**500, "int"),
                            (float("nan"), "float"), ([private], "list"),
                            ((private,), "tuple"), ({private: private}, "dict"),
                            ({private}, "set"), (frozenset({private}), "frozenset"),
                            (FactoryDeniedFixture(private), "record")):
            self.runtime.value = value
            view = self.view(self.packet())
            self.assertEqual(view["registered_builtin_kind"], kind)
            self.assertIsNone(view["string"])
            self.assertIsNone(view["exception"])
            self.assertEqual(view["owned"], [])

    def test_only_actual_getter_origin_and_live_same_scope_are_observable(self):
        ref = self.registry.retain("not a getter call")
        with self.assertRaises(InvalidReference):
            self.view({"root": ref.wire()})
        packet = self.packet()
        # Returning the same actual object through another export invalidates
        # its getter origin, even though its issued registry hold remains live.
        self.dispatcher.call("setup_preview", self.frame("call", args=[], kwargs=[]))
        with self.assertRaises(InvalidReference):
            self.view(packet)
        packet = self.packet()
        foreign = {**packet["root"], "scope": ["1", "2", "9", "4"]}
        with self.assertRaises(InvalidReference):
            self.view({"root": foreign})
        self.dispatcher.release_values(self.frame("values-release", refs=[packet["root"]]))
        self.assertNotIn(int(packet["root"]["object"], 16), self.registry._bootstrap_identity)
        with self.assertRaises(InvalidReference):
            self.view(packet)
        self.packet()
        self.registry.retire()
        self.assertEqual(self.registry._bootstrap_identity, {})

    def test_actual_exception_class_catch_and_opaque_intrinsic_edge_identity(self):
        for error, catch in ((ValueError("value\ud800"), "value-error"),
                             (TypeError("type"), "none"), (KeyError("key"), "none"),
                             (FactoryDeniedFixture("denied"), "none"),
                             (FileNotFoundError(2, "missing", "private/path"), "os-error")):
            edge = RuntimeError("opaque edge is never walked")
            edge.args = (object(),)
            edge.__cause__ = error
            BaseException.__cause__.__set__(error, edge)
            BaseException.__context__.__set__(error, edge)
            self.runtime.failure = error
            packet = self.packet()
            view = self.view(packet)
            fields = view["exception"]
            self.assertEqual(fields["recovery_catch"], catch)
            self.assertEqual(fields["cause"], fields["context"])
            self.assertEqual(view["owned"], [fields["cause"]["object"]])
            self.assertIs(self.registry.borrow(self.registry.reference(fields["cause"])), edge)
            self.assertNotIn("nodes", view)
            if catch == "os-error":
                self.assertEqual(fields["os_fields"]["errno"], {"kind": "int", "value": "2"})
                self.assertEqual(fields["os_fields"]["filename"]["value"]["codepoints"],
                                 list(map(ord, "private/path")))
            else:
                self.assertIsNone(fields["os_fields"])

    def test_outside_inventory_or_finite_diagnostics_are_explicit_abi_gaps(self):
        for value in (b"bytes", object()):
            self.runtime.value = value
            with self.assertRaises(UnsupportedValue):
                self.packet()
        class StringSubclass(str):
            pass

        runtime = IdentityRuntimeFixture()
        runtime.value = StringSubclass("same contents are not an exact str observation")
        dispatcher = Dispatcher(lambda _scope: OriginalRuntime(
            runtime, (RecordType("str-subclass", StringSubclass),), ()))
        dispatcher.init(self.frame("init"))
        packet = json.loads(dispatcher.call("bootstrap_profile_id", self.frame(
            "call", args=[], kwargs=[])))
        with self.assertRaises(UnsupportedValue):
            dispatcher.describe_value(self.frame("bootstrap-identity-describe", root=packet["root"]))
        for args in (([],), ("x" * 4097,), (1 << 4096,), tuple(range(9))):
            self.runtime.failure = ValueError(*args)
            packet = self.packet()
            with self.assertRaises(UnsupportedValue):
                self.view(packet)
        self.runtime.failure = ValueError("root")
        self.runtime.failure.__cause__ = PrivateError("not registered")
        packet = self.packet()
        before = set(self.registry._objects)
        with self.assertRaises(UnsupportedValue):
            self.view(packet)
        self.assertEqual(set(self.registry._objects), before)

    def test_rollback_reused_error_and_fake_kind_cannot_manufacture_classification(self):
        self.runtime.failure = ValueError("actual")
        packet = self.packet()
        self.dispatcher.call("setup_preview", self.frame("call", args=[], kwargs=[]))
        with self.assertRaises(InvalidReference):
            self.view(packet)
        self.runtime.failure = None
        self.runtime.value = "new failing publication"
        payload = self.frame("call", args=[], kwargs=[])
        before = set(self.registry._objects)
        with patch("harness_native.dispatcher.wire.dumps", side_effect=MemoryError):
            with self.assertRaises(MemoryError):
                self.dispatcher.call("bootstrap_profile_id", payload)
        self.assertEqual(set(self.registry._objects), before)
        self.runtime.failure = ValueError("new intrinsic edge")
        self.runtime.failure.__cause__ = RuntimeError("must not leak a new hold")
        packet = self.packet()
        request = self.frame("bootstrap-identity-describe", root=packet["root"])
        before = set(self.registry._objects)
        with patch("harness_native.value_graph.wire.dumps", side_effect=MemoryError):
            with self.assertRaises(MemoryError):
                self.dispatcher.describe_value(request)
        self.assertEqual(set(self.registry._objects), before)

        class FailingMarker(dict):
            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                raise MemoryError

        self.registry._bootstrap_identity = FailingMarker(self.registry._bootstrap_identity)
        self.runtime.failure = None
        self.runtime.value = "new marker allocation failure"
        before = set(self.registry._objects)
        with self.assertRaises(MemoryError):
            self.packet()
        self.assertEqual(set(self.registry._objects), before)
        # A trusted test inventory with a deliberately false tag cannot change
        # the actual class test. Native validation separately rejects the lie.
        runtime = IdentityRuntimeFixture()
        runtime.failure = RuntimeError("not a ValueError")
        dispatcher = Dispatcher(lambda _scope: OriginalRuntime(
            runtime, (RecordType("value-error", RuntimeError),), (RuntimeError,)))
        dispatcher.init(self.frame("init"))
        with self.assertRaises(MethodRaised) as caught:
            dispatcher.call("bootstrap_profile_id", payload)
        view = json.loads(dispatcher.describe_value(self.frame(
            "bootstrap-identity-describe", root=json.loads(caught.exception.details)["root"])))
        self.assertEqual(view["exception"]["recovery_catch"], "none")


class DeniedFactoryTransportTests(unittest.TestCase):
    """Finite provenance/rollback ABI, not original factory or native proof."""

    def setUp(self):
        self.runtime = FactoryRuntimeFixture()
        self.scope = Scope(1, 2, 3, 4)
        self.dispatcher = Dispatcher(lambda _scope: OriginalRuntime(
            self.runtime,
            (RecordType("profile-resolution-denied", FactoryDeniedFixture),
             RecordType("type-error", TypeError), RecordType("runtime-error", RuntimeError)),
            (FactoryDeniedFixture, TypeError, RuntimeError),
        ))
        self.dispatcher.init(self.frame("init"))
        self.registry = self.dispatcher._current().registry

    def frame(self, kind, **fields):
        return wire.dumps({"format": PREFIX + kind + ".v1",
                           "scope": self.scope.wire(), **fields})

    def call_frame(self, message):
        imported = json.loads(self.dispatcher.import_values(self.frame(
            "value-input", nodes=[{"kind": "str", "value": message}], root={"node": 0})))
        return self.frame("call", args=[imported["root"]], kwargs=[])

    def call(self, message):
        return json.loads(self.dispatcher.call("denied", self.call_frame(message)))

    def view(self, packet):
        return json.loads(self.dispatcher.describe_value(
            self.frame("value-describe", root=packet["root"])))

    def test_fresh_factory_values_have_only_the_fixed_builtin_observation(self):
        previous = set()
        for message in ("", "plain", "λ 日本語", 'quote" and \\ slash', "line\n\x00"):
            packet = self.call(message)
            self.assertNotIn(packet["root"]["object"], previous)
            previous.add(packet["root"]["object"])
            self.assertEqual(packet["owned"], [packet["root"]["object"]])
            view = self.view(packet)
            self.assertEqual(view["owned"], [])
            self.assertEqual(view["nodes"], [{
                "ref": packet["root"], "kind": "profile-resolution-denied",
                "opaque_native_binding": False, "fields": [],
                "factory_denied": {"args": [message], "cause": None,
                                   "context": None, "suppress_context": False},
            }])

    def test_other_errors_nested_views_reuse_and_raised_values_stay_opaque(self):
        existing = self.registry.retain(FactoryDeniedFixture("private"))
        self.assertNotIn("factory_denied", json.loads(self.registry.describe(existing))["nodes"][0])
        packet = self.call("fresh")
        nested = json.loads(self.dispatcher.call("setup_preview", self.frame("call", args=[], kwargs=[])))
        self.assertTrue(all("factory_denied" not in row for row in self.view(nested)["nodes"]))
        self.runtime.reuse = True
        reused = self.call("fresh")
        self.assertEqual(reused["root"], packet["root"])
        self.assertEqual(reused["owned"], [])
        self.assertNotIn("factory_denied", self.view(reused)["nodes"][0])
        self.runtime.reuse = False
        packet = self.call("raised later")
        with self.assertRaises(MethodRaised) as caught:
            self.dispatcher.call("resolve_profile", self.frame("call", args=[], kwargs=[]))
        raised = json.loads(caught.exception.details)
        self.assertEqual(raised["root"], packet["root"])
        self.assertNotIn("factory_denied", self.view(raised)["nodes"][0])

    def test_mutation_foreign_scope_release_retire_and_fake_kind_do_not_disclose(self):
        packet = self.call("before mutation")
        reference = self.registry.reference(packet["root"])
        BaseException.args.__set__(self.registry.borrow(reference), ("secret",))
        self.assertNotIn("factory_denied", self.view(packet)["nodes"][0])
        packet = self.call("release")
        foreign = {**packet["root"], "scope": ["1", "2", "9", "4"]}
        with self.assertRaises(InvalidReference):
            self.dispatcher.describe_value(self.frame("value-describe", root=foreign))
        self.dispatcher.release_values(self.frame("values-release", refs=[packet["root"]]))
        self.assertNotIn(int(packet["root"]["object"], 16), self.registry._denied)
        with self.assertRaises(InvalidReference):
            self.view(packet)
        with self.assertRaises((InvalidFrame, UnsupportedValue)):
            self.dispatcher.import_values(self.frame("value-input", nodes=[{
                "kind": "profile-resolution-denied", "args": ["fake"]}], root={"node": 0}))
        self.call("retire")
        self.registry.retire()
        self.assertEqual(self.registry._denied, {})
        with self.assertRaises(InvalidReference):
            self.view(packet)

    def test_provenance_allocation_and_output_failures_roll_back_fresh_rows(self):
        class FailInsertion(dict):
            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                raise MemoryError("injected provenance allocation failure")

        for insertion in (False, True):
            with self.subTest(insertion=insertion):
                frame = self.call_frame("rollback")
                before = (dict(self.registry._objects), dict(self.registry._identity),
                          dict(self.registry._permissions), dict(self.registry._denied))
                if insertion:
                    self.registry._denied = FailInsertion(self.registry._denied)
                    with self.assertRaises(MemoryError):
                        self.dispatcher.call("denied", frame)
                    self.registry._denied = dict(self.registry._denied)
                else:
                    with patch("harness_native.dispatcher.wire.dumps", side_effect=MemoryError("output")):
                        with self.assertRaises(MemoryError):
                            self.dispatcher.call("denied", frame)
                self.assertEqual((self.registry._objects, self.registry._identity,
                                  self.registry._permissions, self.registry._denied), before)

    def test_signature_typeerror_and_mock_faults_are_not_returned_factory_values(self):
        with self.assertRaises(MethodRaised) as caught:
            self.dispatcher.call("denied", self.frame("call", args=[], kwargs=[]))
        packet = json.loads(caught.exception.details)
        error = self.registry.borrow(self.registry.reference(packet["root"]))
        self.assertIs(type(error), TypeError)
        self.assertNotIn("factory_denied", self.view(packet)["nodes"][0])
        # These injected ABI faults are separate from original-factory CI.
        for error, expected in ((RuntimeError("fixture"), MethodRaised),
                                (ImportError("fixture"), UnsupportedValue),
                                (MemoryError("fixture"), MemoryError),
                                (RecursionError("fixture"), RecursionError)):
            self.runtime.failure = error
            with self.subTest(kind=type(error)), self.assertRaises(expected):
                self.call("not returned")


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

    def test_checkpoints_preserve_actual_init_order_and_original_failure(self):
        events = []

        def factory(_scope):
            events.append("factory")
            return original()

        dispatcher = Dispatcher(factory, init_checkpoint_observer=events.append)
        dispatcher.init(self.frame("init"))
        self.assertEqual(events, [1, 2, 3, 4, "factory", 5, 6, 7])
        with self.assertRaises(AlreadyInitialized):
            dispatcher.init(self.frame("init"))
        self.assertEqual(events, [1, 2, 3, 4, "factory", 5, 6, 7])

        error = RuntimeError("actual factory failure")

        def failing(_scope):
            raise error

        events.clear()
        dispatcher = Dispatcher(failing, init_checkpoint_observer=events.append)
        with self.assertRaises(RuntimeError) as caught:
            dispatcher.init(self.frame("init"))
        self.assertIs(caught.exception, error)
        self.assertEqual(events, [1, 2, 3, 4])
        self.assertIsNone(dispatcher._state)

    def test_checkpoint_errors_do_not_change_frames_or_factory_outcome(self):
        seen = []

        def observer(sequence):
            seen.append(sequence)
            raise RuntimeError("ordinary observer failure")

        dispatcher = Dispatcher(lambda _scope: original(),
                                init_checkpoint_observer=observer)
        dispatcher.init(self.frame("init"))
        self.assertEqual(seen, list(range(1, 8)))
        self.assertIsNotNone(dispatcher._state)
        seen.clear()
        dispatcher = Dispatcher(lambda _scope: original(),
                                init_checkpoint_observer=observer)
        with self.assertRaises(InvalidFrame):
            dispatcher.init("invalid JSON")
        self.assertEqual(seen, [1])
        self.assertIsNone(dispatcher._state)

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
            [("init_failure_observer", "report_init_failure"),
             ("init_checkpoint_observer", "report_init_checkpoint")],
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
