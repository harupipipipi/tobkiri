"""Portable dispatcher protocol tests, not execution of original Host or Wasm."""

from __future__ import annotations

import ast
import base64
import hashlib
import json
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
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
    _CatalogDiagnostic,
)
from harness_native.value_graph import (  # noqa: E402
    InvalidFrame,
    InvalidReference,
    RecordType,
    Registry,
    Scope,
    UnsupportedValue,
)
from harness_native.native_path import NativePath, PathBridge, _UnboundPlatformArtifacts  # noqa: E402
import ordered_json_v2 as wire  # noqa: E402
import prepare_harness_native as prepare  # noqa: E402


MISSING = object()


class FixedManifestParseTests(unittest.TestCase):
    """Actual stdlib parser/finite provenance, not native IO or Wasm proof."""

    def setUp(self):
        errors = (("json-decode-error", json.JSONDecodeError),
                  ("stop-iteration", StopIteration),
                  ("unicode-decode-error", UnicodeDecodeError))
        self.dispatcher = Dispatcher(lambda _scope: OriginalRuntime(
            object(), tuple(RecordType(kind, cls) for kind, cls in errors),
            tuple(cls for _, cls in errors)))
        self.scope = ["1", "2", "3", "4"]
        self.dispatcher.init(wire.dumps({"format": PREFIX + "init.v1", "scope": self.scope}))

    def parse(self, raw):
        return self.dispatcher.describe_value(wire.dumps({
            "format": PREFIX + "catalog-manifest-parse.v1", "scope": self.scope,
            "bytes": base64.b64encode(raw).decode("ascii")}))

    def observed_error(self, raw):
        with self.assertRaises(MethodRaised) as caught:
            self.parse(raw)
        packet = json.loads(caught.exception.details)
        registry = self.dispatcher._current().registry
        actual = registry.borrow(registry.reference(packet["root"]))
        view = json.loads(self.dispatcher.describe_value(wire.dumps({
            "format": PREFIX + "bootstrap-call-describe.v1", "scope": self.scope,
            "root": packet["root"]})))
        return packet, actual, view

    def test_read_text_newlines_permissive_values_and_origin_are_preserved(self):
        packet = json.loads(self.parse(b'{\r\n"x": 1, "x": 2, "nan": NaN, "s": "\\ud800"}'))
        registry = self.dispatcher._current().registry
        value = registry.borrow(registry.reference(packet["root"]))
        self.assertEqual(value["x"], 2)
        self.assertNotEqual(value["nan"], value["nan"])
        self.assertEqual(value["s"], "\ud800")
        with self.assertRaises(InvalidReference):
            registry.describe_bootstrap_identity(registry.reference(packet["root"]))
        view = json.loads(registry.describe_bootstrap_call(registry.reference(packet["root"])))
        self.assertEqual(view["origin"], "manifest-returned")
        self.dispatcher.release_values(wire.dumps({
            "format": PREFIX + "values-release.v1", "scope": self.scope,
            "refs": [packet["root"]]}))
        with self.assertRaises(InvalidReference):
            registry.describe_bootstrap_call(registry.reference(packet["root"]))

    def test_invalid_json_keeps_actual_normalized_doc_and_parser_positions(self):
        _, actual, view = self.observed_error(b'{\r\n"bad": }')
        self.assertIs(type(actual), json.JSONDecodeError)
        fields = view["exception"]["json_fields"]
        self.assertEqual(fields["doc"]["codepoints"], list(map(ord, actual.doc)))
        self.assertEqual(actual.doc, '{\n"bad": }')
        self.assertEqual((fields["pos"], fields["lineno"], fields["colno"]),
                         (actual.pos, actual.lineno, actual.colno))
        self.assertEqual(view["origin"], "manifest-raised")

    def test_utf8_bom_is_not_the_bytes_overload_of_json_loads(self):
        _, actual, view = self.observed_error(b'\xef\xbb\xbf{}')
        self.assertEqual(actual.pos, 0)
        self.assertEqual(actual.doc, "\ufeff{}")
        self.assertEqual(view["exception"]["json_fields"]["doc"]["codepoints"], [65279, 123, 125])

    def test_invalid_utf8_keeps_the_actual_decoder_bytes_and_fields(self):
        _, actual, view = self.observed_error(b'\xff')
        self.assertIs(type(actual), UnicodeDecodeError)
        fields = view["exception"]["unicode_fields"]
        self.assertEqual(fields["object"], {"kind": "bytes", "value": "/w=="})
        self.assertEqual((fields["start"], fields["end"]), (actual.start, actual.end))
        self.assertEqual(fields["encoding"]["codepoints"], list(map(ord, actual.encoding)))
        self.assertEqual(fields["reason"]["codepoints"], list(map(ord, actual.reason)))
        formatted = UnicodeDecodeError.__str__(actual)
        self.assertEqual(fields["formatted_message"], {
            "length": len(formatted), "codepoints": list(map(ord, formatted))})
        self.assertEqual(view["exception"]["recovery_catch"], "value-error")

    def test_decoder_formatting_is_limited_to_exact_manifest_origin_and_fields(self):
        _, actual, _ = self.observed_error(b'\xff')
        registry = self.dispatcher._current().registry
        reference = registry.retain(actual)
        registry._bootstrap_identity[reference.object_id] = "getter-raised"
        # The existing getter observer cannot expose decoder-specific bytes or
        # formatter DATA merely because the value is the same builtin class.
        with self.assertRaises(UnsupportedValue):
            registry.describe_bootstrap_identity(reference)
        registry._bootstrap_identity[reference.object_id] = "manifest-raised"
        actual.start = -1
        with self.assertRaisesRegex(UnsupportedValue, "decoder fields"):
            registry.describe_bootstrap_call(reference)
        long_message = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "x" * 4096)
        long_reference = registry.retain(long_message)
        registry._bootstrap_identity[long_reference.object_id] = "manifest-raised"
        with self.assertRaisesRegex(UnsupportedValue, "decoder message"):
            registry.describe_bootstrap_call(long_reference)

        class DecoderSubclass(UnicodeDecodeError):
            def __str__(self):
                raise AssertionError("subclass formatter must not run")

        subclass = DecoderSubclass("utf-8", b"\xff", 0, 1, "invalid start byte")
        with self.assertRaises(UnsupportedValue):
            registry.retain(subclass)

    def test_decoder_message_allocation_failure_is_fatal_and_rolls_back_edges(self):
        with self.assertRaises(MethodRaised) as caught:
            self.parse(b'\xff')
        packet = json.loads(caught.exception.details)
        registry = self.dispatcher._current().registry
        reference = registry.reference(packet["root"])
        actual = registry.borrow(reference)
        actual.__context__ = StopIteration(0)
        before = set(registry._objects)
        formatted = UnicodeDecodeError.__str__(actual)
        original = Registry._identity_string
        fatal = MemoryError("controlled observer allocation failure")

        def exhausted(value, limit):
            if value == formatted:
                raise fatal
            return original(value, limit)

        with patch.object(Registry, "_identity_string", staticmethod(exhausted)):
            with self.assertRaises(MemoryError) as failure:
                registry.describe_bootstrap_call(reference)
        self.assertIs(failure.exception, fatal)
        self.assertEqual(set(registry._objects), before)
        self.assertIs(registry.borrow(reference), actual)


class CatalogDiagnosticTests(unittest.TestCase):
    """Controlled wrapper ABI only, not actual original loader/Wasm proof."""

    def setUp(self):
        # Execute the EXACT two thin adapter functions with WIT result stubs.
        # No original module, file IO import or Native permission is supplied.
        class AbiErr(Exception):
            def __init__(self, value):
                super().__init__()
                self.value = value

        codes = ("NOT_INITIALIZED", "ALREADY_INITIALIZED", "INVALID_FRAME",
                 "INVALID_REFERENCE", "UNSUPPORTED_VALUE", "METHOD_RAISED")
        self.namespace = {
            "Err": AbiErr, "Error": lambda *args: SimpleNamespace(
                code=args[0], message=args[1], details=args[2]),
            "ErrorCode": SimpleNamespace(**{name: name for name in codes}),
            "NotInitialized": NotInitialized, "AlreadyInitialized": AlreadyInitialized,
            "InvalidFrame": InvalidFrame, "InvalidReference": InvalidReference,
            "UnsupportedValue": UnsupportedValue, "MethodRaised": MethodRaised,
            "_CatalogDiagnostic": _CatalogDiagnostic,
        }
        tree = ast.parse((ROOT / "tools/python313-producer/harness_native/app.py").read_text())
        selected = ast.Module(body=[node for node in tree.body
                                   if isinstance(node, ast.FunctionDef)
                                   and node.name in {"_invoke", "_invoke_catalog"}],
                              type_ignores=[])
        exec(compile(selected, "actual-catalog-thin-wrapper-functions", "exec"), self.namespace)
        self.abi_error = AbiErr

    def setup_dispatcher(self, *, errors=(("value-error", ValueError),), codec_error=None):
        class Runtime:
            failure = None
            value = "actual controlled return"

            def load_catalog(self, root):
                if self.failure is not None:
                    raise self.failure
                return self.value

        self.runtime = Runtime()
        self.scope = Scope(1, 2, 3, 4)
        self.dispatcher = Dispatcher(lambda _scope: OriginalRuntime(
            self.runtime, (RecordType("native-path", NativePath, native_binding=True),
                           *(RecordType(kind, cls) for kind, cls in errors)),
            tuple(cls for _, cls in errors), _catalog_codec_error=codec_error))
        self.dispatcher.init(wire.dumps({"format": PREFIX + "init.v1",
                                        "scope": self.scope.wire()}))
        registry = self.dispatcher._current().registry
        root = registry.retain("controlled argument DATA")
        self.payload = wire.dumps({"format": PREFIX + "call.v1", "scope": self.scope.wire(),
                                   "args": [root.wire()], "kwargs": []})
        self.observed = []

        def diagnostic():
            value = _CatalogDiagnostic()
            self.observed.append(value)
            return value

        self.namespace["_CatalogDiagnostic"] = diagnostic

    def call_catalog(self):
        return self.namespace["_invoke_catalog"](
            self.dispatcher.call, "load_catalog", self.payload)

    def test_known_unknown_and_nonexact_bridge_args_are_only_fixed_tags(self):
        class Subclass(UnsupportedValue):
            def __str__(self):
                raise AssertionError("must not format diagnostic exception")

            def __repr__(self):
                raise AssertionError("must not render diagnostic exception")

        for error, expected in (
            (UnsupportedValue("invalid native inspection result"), "result-frame"),
            (UnsupportedValue("private path/value must never be echoed"), "unknown"),
            (UnsupportedValue("invalid native inspection result", "extra"), "unknown"),
            (Subclass("invalid native inspection result"), "unknown"),
        ):
            diagnostic = _CatalogDiagnostic()
            diagnostic.phase = "original-call"
            details = diagnostic.details(error)
            self.assertLessEqual(len(details), _CatalogDiagnostic.MAX_BYTES)
            self.assertEqual(json.loads(details), {
                "format": "io.tobkiri.native-catalog-refusal-ci.v1",
                "phase": "original-call", "reason": expected})
            self.assertNotIn("private", details)
            self.assertEqual((diagnostic.phase, diagnostic.reason), (None, None))

    def test_original_classification_and_return_retaining_have_distinct_phases(self):
        self.setup_dispatcher()
        for error, expected in (
            (UnsupportedValue("invalid native inspection result"), "result-frame"),
            (UnsupportedValue("unreviewed private reason"), "unknown"),
            (LookupError("outside inventory must not be disclosed"), "outside-fixed-method-inventory"),
        ):
            self.runtime.failure = error
            with self.assertRaises(UnsupportedValue):
                self.dispatcher.call("load_catalog", self.payload)
            with self.assertRaises(self.abi_error) as caught:
                self.call_catalog()
            self.assertEqual(caught.exception.value.code, "UNSUPPORTED_VALUE")
            self.assertEqual(caught.exception.value.message, "original DATA is not represented")
            self.assertEqual(json.loads(caught.exception.value.details)["phase"], "original-call")
            self.assertEqual(json.loads(caught.exception.value.details)["reason"], expected)
            self.assertEqual((self.observed[-1].phase, self.observed[-1].reason), (None, None))
        self.runtime.failure = None
        self.runtime.value = object()
        with self.assertRaises(self.abi_error) as caught:
            self.call_catalog()
        self.assertEqual(json.loads(caught.exception.value.details)["phase"], "result-retain")
        self.assertEqual(json.loads(caught.exception.value.details)["reason"], "unrepresented-result-type")

    def test_normal_methodraised_and_fatal_calls_clear_without_changing_classification(self):
        self.setup_dispatcher()
        packet = json.loads(self.call_catalog())
        registry = self.dispatcher._current().registry
        self.assertIs(registry.borrow(registry.reference(packet["root"])), self.runtime.value)
        self.assertEqual((self.observed[-1].phase, self.observed[-1].reason), (None, None))
        self.assertIsNone(self.observed[-1].outside_builtin)
        self.runtime.failure = ValueError("actual domain object")
        with self.assertRaises(self.abi_error) as caught:
            self.call_catalog()
        self.assertEqual(caught.exception.value.code, "METHOD_RAISED")
        packet = json.loads(caught.exception.value.details)
        self.assertIs(registry.borrow(registry.reference(packet["root"])), self.runtime.failure)
        self.assertEqual((self.observed[-1].phase, self.observed[-1].reason), (None, None))
        self.assertIsNone(self.observed[-1].outside_builtin)
        for error in (MemoryError("fatal"), RecursionError("fatal"), KeyboardInterrupt()):
            self.runtime.failure = error
            with self.assertRaises(type(error)):
                self.call_catalog()
            self.assertEqual((self.observed[-1].phase, self.observed[-1].reason), (None, None))
            self.assertIsNone(self.observed[-1].outside_builtin)

    def test_outside_inventory_tags_only_fixed_exact_builtins_and_never_their_args(self):
        self.setup_dispatcher()

        class Secret:
            def __str__(self):
                raise AssertionError("must not format original argument")

            def __repr__(self):
                raise AssertionError("must not render original argument")

        cases = []
        for builtin, tag in (
            (TypeError, "type-error"), (AttributeError, "attribute-error"),
            (NameError, "name-error"), (ImportError, "import-error"),
            (NotImplementedError, "not-implemented-error"),
            (AssertionError, "assertion-error"),
            (ModuleNotFoundError, "module-not-found-error"),
            (KeyError, "key-error"), (IndexError, "index-error"),
            (StopIteration, "stop-iteration"), (OSError, "os-error"),
            (FileNotFoundError, "file-not-found-error"),
            (PermissionError, "permission-error"),
        ):
            class Subclass(builtin):
                def __getattribute__(self, name):
                    if name == "args":
                        raise AssertionError("must not inspect original subclass args")
                    return super().__getattribute__(name)

            cases.extend(((builtin(Secret()), tag), (Subclass(Secret()), "unknown")))
        cases.append((LookupError(Secret()), "unknown"))
        for error, expected in cases:
            with self.subTest(expected=expected):
                self.runtime.failure = error
                with self.assertRaises(UnsupportedValue):
                    self.dispatcher.call("load_catalog", self.payload)
                with self.assertRaises(self.abi_error) as caught:
                    self.call_catalog()
                value = caught.exception.value
                self.assertEqual((value.code, value.message),
                                 ("UNSUPPORTED_VALUE", "original DATA is not represented"))
                self.assertLessEqual(len(value.details), _CatalogDiagnostic.MAX_BYTES)
                self.assertEqual(json.loads(value.details), {
                    "format": "io.tobkiri.native-catalog-refusal-ci.v1",
                    "phase": "original-call", "reason": "outside-fixed-method-inventory",
                    "outside_builtin": expected,
                })
                self.assertEqual((self.observed[-1].phase, self.observed[-1].reason,
                                  self.observed[-1].outside_builtin), (None, None, None))

        # Exact types which ARE in the fixed inventory keep MethodRaised.
        self.runtime.failure = ValueError("actual domain object")
        with self.assertRaises(self.abi_error) as caught:
            self.call_catalog()
        self.assertEqual(caught.exception.value.code, "METHOD_RAISED")
        self.assertEqual((self.observed[-1].phase, self.observed[-1].reason,
                          self.observed[-1].outside_builtin), (None, None, None))

    def test_codec_diagnostic_requires_trusted_exact_binding_without_changing_priority(self):
        class CodecError(Exception):
            def __getattribute__(self, name):
                if name == "args":
                    raise AssertionError("must not inspect original codec args")
                return super().__getattribute__(name)

            def __str__(self):
                raise AssertionError("must not format original codec error")

            def __repr__(self):
                raise AssertionError("must not render original codec error")

        class Subclass(CodecError):
            pass

        class OtherError(Exception):
            pass

        for binding in (None, CodecError):
            self.setup_dispatcher(codec_error=binding)
            self.assertIs(self.dispatcher._current()._catalog_codec_error, binding)
            for error, tag in ((CodecError(), "native-exception-codec" if binding else "unknown"),
                               (Subclass(), "unknown"), (OtherError(), "unknown")):
                self.runtime.failure = error
                with self.assertRaises(UnsupportedValue):
                    self.dispatcher.call("load_catalog", self.payload)
                with self.assertRaises(self.abi_error) as caught:
                    self.call_catalog()
                value = caught.exception.value
                self.assertEqual((value.code, value.message),
                                 ("UNSUPPORTED_VALUE", "original DATA is not represented"))
                self.assertEqual(json.loads(value.details)["outside_builtin"], tag)
                self.assertLessEqual(len(value.details), _CatalogDiagnostic.MAX_BYTES)
                self.assertEqual((self.observed[-1].phase, self.observed[-1].reason,
                                  self.observed[-1].outside_builtin), (None, None, None))
        # A registered exact exception still takes the ORIGINAL MethodRaised
        # path even when a private diagnostic binding names that same type.
        self.setup_dispatcher(errors=(("codec-fixture", CodecError),), codec_error=CodecError)
        error = CodecError()
        self.runtime.failure = error
        with self.assertRaises(self.abi_error) as caught:
            self.call_catalog()
        self.assertEqual(caught.exception.value.code, "METHOD_RAISED")
        packet = json.loads(caught.exception.value.details)
        registry = self.dispatcher._current().registry
        self.assertIs(registry.borrow(registry.reference(packet["root"])), error)
        self.assertEqual((self.observed[-1].phase, self.observed[-1].reason,
                          self.observed[-1].outside_builtin), (None, None, None))
        for invalid in ("UnsupportedNativeException", object()):
            dispatcher = Dispatcher(lambda _scope: OriginalRuntime(
                object(), (), (), _catalog_codec_error=invalid))
            with self.assertRaises(UnsupportedValue):
                dispatcher.init(wire.dumps({"format": PREFIX + "init.v1",
                                            "scope": self.scope.wire()}))

    def test_bootstrap_binds_existing_codec_class_only_as_private_diagnostic_reference(self):
        tree = ast.parse((ROOT / "tools/python313-producer/harness_native/bootstrap.py").read_text())
        (load,) = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name == "_load_bound_runtime"]
        (normal_import,) = [node for node in load.body if isinstance(node, ast.ImportFrom)
                            and node.module == "exception_graph"]
        self.assertEqual(normal_import.level, 1)
        self.assertEqual([(item.name, item.asname) for item in normal_import.names],
                         [("_FIXED_EXCEPTION_CLASSES", None), ("UnsupportedNativeException", None)])
        (returned,) = [node.value for node in load.body if isinstance(node, ast.Return)]
        self.assertIsInstance(returned, ast.Call)
        self.assertEqual(returned.func.id, "OriginalRuntime")
        self.assertEqual([(keyword.arg, keyword.value.id) for keyword in returned.keywords],
                         [("_catalog_codec_error", "UnsupportedNativeException")])
        for assignment in (node for node in load.body if isinstance(node, ast.Assign)):
            if any(isinstance(target, ast.Name) and target.id in {"errors", "records"}
                   for target in assignment.targets):
                self.assertFalse(any(isinstance(node, ast.Name)
                                     and node.id == "UnsupportedNativeException"
                                     for node in ast.walk(assignment.value)))
        self.assertEqual(ast.dump(returned.args[2], include_attributes=False),
                         ast.dump(ast.parse("tuple(cls for _, cls in errors)", mode="eval").body,
                                  include_attributes=False))

    def test_diagnostic_encoding_failure_remains_fatal_and_clears_copy_data(self):
        diagnostic = _CatalogDiagnostic()
        self.namespace["_CatalogDiagnostic"] = lambda: diagnostic

        def refusal(_method, _payload, *, _catalog_diagnostic):
            _catalog_diagnostic.phase = "original-call"
            raise UnsupportedValue("invalid native inspection result")

        with patch("harness_native.dispatcher.wire.dumps", side_effect=MemoryError("encode fault")):
            with self.assertRaises(MemoryError):
                self.namespace["_invoke_catalog"](refusal, "load_catalog", "DATA")
        self.assertEqual((diagnostic.phase, diagnostic.reason), (None, None))


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


class ResolverSourceLookupTransportTests(unittest.TestCase):
    """Fixed resolver observations, live Python lookup and issued provenance."""

    class Catalog:
        def __init__(self) -> None:
            self.profiles = {}
            self.packs = {}
            self.root = None
            self.artifact_root = None

    class Runtime(IdentityRuntimeFixture):
        def __init__(self) -> None:
            super().__init__()
            self.dynamic_calls: list[tuple[object, str, tuple[str, ...]]] = []
            self.dynamic_result: object = ()

        def load_catalog(self, _root: NativePath) -> object:
            if type(self.catalog) is ResolverSourceLookupTransportTests.Catalog:
                self.catalog.root = _root
            return self.catalog

        def dynamic_profile_edges(
            self, catalog: object, profile_id: str, pack_ids: tuple[str, ...],
        ) -> object:
            self.dynamic_calls.append((catalog, profile_id, pack_ids))
            catalog.profiles.get(profile_id)
            return self.dynamic_result

    def setUp(self) -> None:
        self.scope = Scope(1, 2, 3, 4)
        self.runtime = self.Runtime()
        self.runtime.catalog = self.Catalog()
        self.dispatcher = Dispatcher(lambda _scope: OriginalRuntime(
            self.runtime,
            (RecordType("native-path", NativePath, native_binding=True),
             RecordType("bundled-catalog", self.Catalog, ("profiles",)),
             RecordType("key-error", KeyError), RecordType("type-error", TypeError)),
            (KeyError, TypeError),
        ))
        self.dispatcher.init(self.frame("init"))
        self.registry = self.dispatcher._current().registry
        self.bridge = PathBridge(
            self.scope, "posix", self.inspect_catalog_root, os_flags={"O_RDONLY": 0},
        )
        self.root = self.registry.retain(self.bridge.bind("fixture-root", "/fixture"))

    def frame(self, kind: str, **fields: object) -> str:
        return wire.dumps({"format": PREFIX + kind + ".v1",
                           "scope": self.scope.wire(), **fields})

    def inspect_catalog_root(self, payload: str) -> str:
        request = wire.loads(payload)
        self.assertEqual(request["operation"], "root")
        return wire.dumps({
            "format": PREFIX + "inspection-response.v1",
            "scope": self.scope.wire(),
            "result": {"kind": "path", "binding": "fixture-root", "value": "/fixture"},
        })

    def load(self) -> dict:
        return wire.loads(self.dispatcher.call("load_catalog", self.frame(
            "call", args=[self.root.wire()], kwargs=[],
        )))["root"]

    def getter(self, value: object) -> dict:
        self.runtime.value = value
        return wire.loads(self.dispatcher.call("bootstrap_profile_id", self.frame(
            "call", args=[], kwargs=[],
        )))["root"]

    def lookup(self, catalog: dict, profile_id: dict, **fields: object) -> dict:
        return wire.loads(self.dispatcher.describe_value(self.frame(
            "resolver-source-lookup", catalog=catalog, profile_id=profile_id, **fields,
        )))

    def view(self, packet: dict) -> dict:
        return wire.loads(self.dispatcher.describe_value(self.frame(
            "bootstrap-call-describe", root=packet["root"],
        )))

    def static_edges(self, source_profile: dict, **fields: object) -> dict:
        return wire.loads(self.dispatcher.describe_value(self.frame(
            "resolver-static-edges", source_profile=source_profile, **fields,
        )))

    def dynamic_edges(
        self, catalog: dict, profile_id: dict, source_profile: dict, **fields: object,
    ) -> dict:
        return wire.loads(self.dispatcher.describe_value(self.frame(
            "resolver-dynamic-edges", catalog=catalog, profile_id=profile_id,
            source_profile=source_profile, **fields,
        )))

    def verified_artifacts(self, catalog: dict, profile_id: dict, source: dict) -> dict:
        return wire.loads(self.dispatcher.describe_value(self.frame(
            "resolver-verified-artifacts", catalog=catalog, profile_id=profile_id,
            source_profile=source,
        )))

    def source(self, profile: dict) -> tuple[dict, dict, dict]:
        self.runtime.catalog.profiles["fixture-id"] = profile
        catalog = self.load()
        self.getter("first-id")
        profile_id = self.getter("fixture-id")
        return catalog, profile_id, self.lookup(catalog, profile_id)["root"]

    def test_uses_second_exact_input_and_preserves_live_aliases(self) -> None:
        first_value = "".join(["fixture", "-id"])
        second_value = "".join(["fixture", "-id"])
        self.assertIsNot(first_value, second_value)
        profile = {"requested_edges": [object()]}
        keys = []

        class Profiles(dict):
            def get(self, key: str) -> object:
                keys.append(key)
                return super().get(key)

        profiles = Profiles({first_value: profile})
        self.runtime.catalog.profiles = profiles
        catalog = self.load()
        first = self.getter(first_value)
        with self.assertRaises(InvalidReference):
            self.lookup(catalog, first)
        second = self.getter(second_value)
        with self.assertRaises(InvalidReference):
            self.lookup(catalog, first)
        self.assertEqual(keys, [])
        profile_ref = self.registry.retain(profile)
        packet = self.lookup(catalog, second)
        self.assertEqual(packet["root"], profile_ref.wire())
        self.assertEqual(packet["owned"], [])
        self.assertIs(keys[0], second_value)
        view = self.view(packet)
        self.assertEqual(view["origin"], "resolver-source-lookup-returned")
        self.assertEqual(view["kind"], "dict")
        self.assertNotIn("nodes", view)
        del profiles[first_value]
        missing = self.lookup(catalog, second)
        self.assertIsNone(
            self.registry.borrow(self.registry.reference(missing["root"])),
        )
        self.assertEqual(self.view(missing)["kind"], "none")
        self.assertEqual(len(keys), 2)

    def test_same_getter_object_reuses_reference_and_rejects_other_refs(self) -> None:
        catalog = self.load()
        profile_id = self.getter("fixture-id")
        self.assertEqual(self.getter("fixture-id"), profile_id)
        alien_catalog = self.registry.retain(self.Catalog()).wire()
        alien_id = self.registry.retain("".join(["fixture", "-id"])).wire()
        for ref in (alien_id, {**profile_id, "scope": ["1", "2", "9", "4"]}):
            with self.assertRaises(InvalidReference):
                self.lookup(catalog, ref)
        with self.assertRaises(InvalidReference):
            self.lookup(alien_catalog, profile_id)
        with self.assertRaises(InvalidFrame):
            self.lookup(catalog, profile_id, attribute="profiles")
        self.assertEqual(self.view(self.lookup(catalog, profile_id))["kind"], "none")
        self.dispatcher.release_values(self.frame("values-release", refs=[profile_id]))
        replacement = self.getter("fixture-id")
        self.assertNotEqual(replacement, profile_id)
        with self.assertRaises(InvalidReference):
            self.lookup(catalog, replacement)

    def test_catalog_return_with_wrong_type_is_not_a_lookup_input(self) -> None:
        self.runtime.catalog = {"profiles": {}}
        catalog = self.load()
        self.getter("first-id")
        profile_id = self.getter("fixture-id")
        with self.assertRaises(InvalidReference):
            self.lookup(catalog, profile_id)

    def test_failed_second_getter_cannot_be_substituted_by_a_later_call(self) -> None:
        catalog = self.load()
        self.getter("first-id")
        self.runtime.failure = KeyError("second getter failed")
        with self.assertRaises(MethodRaised):
            self.getter("failed-id")
        self.runtime.failure = None
        profile_id = self.getter("later-id")
        with self.assertRaises(InvalidReference):
            self.lookup(catalog, profile_id)

    def test_second_getter_must_return_exact_string(self) -> None:
        catalog = self.load()
        self.getter("first-id")
        profile_id = self.getter(12)
        with self.assertRaises(InvalidReference):
            self.lookup(catalog, profile_id)

    def test_retirement_revokes_both_inputs(self) -> None:
        catalog = self.load()
        self.getter("first-id")
        profile_id = self.getter("fixture-id")
        self.registry.retire()
        with self.assertRaises(InvalidReference):
            self.lookup(catalog, profile_id)

    def test_preserves_exact_original_method_exception(self) -> None:
        original = KeyError("controlled failure")

        class FailingProfiles(dict):
            def get(self, key: str) -> object:
                raise original

        self.runtime.catalog.profiles = FailingProfiles()
        catalog = self.load()
        self.getter("first-id")
        profile_id = self.getter("fixture-id")
        with self.assertRaises(MethodRaised) as raised:
            self.lookup(catalog, profile_id)
        packet = wire.loads(raised.exception.details)
        ref = self.registry.reference(packet["root"])
        self.assertIs(self.registry.borrow(ref), original)
        view = self.view(packet)
        self.assertEqual(view["origin"], "resolver-source-lookup-raised")
        self.assertEqual(view["kind"], "key-error")
        with self.assertRaises(InvalidReference):
            self.registry.describe_bootstrap_identity(ref)

    def test_static_edges_keep_row_order_duplicates_and_real_aliases(self) -> None:
        edge = {"operation_id": "read", "contract_id": "contract.v1",
                "target_provider_id": "provider", "caller_function_id": "caller",
                "untouched": object()}
        second = {**edge, "operation_id": "write"}
        edges = [second, edge, second]
        profile = {"requested_edges": edges, "untouched": object()}
        alias = self.registry.retain(profile).wire()
        alien = self.registry.retain(dict(profile)).wire()
        with self.assertRaises(InvalidReference):
            self.static_edges(alias)
        catalog, profile_id, source = self.source(profile)
        self.assertEqual(alias, source)
        before = dict(self.registry._objects)
        with self.assertRaises(InvalidReference):
            self.static_edges(alien)
        with self.assertRaises(InvalidReference):
            self.static_edges({**source, "scope": ["1", "2", "9", "4"]})
        with self.assertRaises(InvalidFrame):
            self.static_edges(source, field="untouched")
        self.assertEqual(self.static_edges(source), {
            "format": PREFIX + "resolver-static-edges-result.v1",
            "scope": self.scope.wire(), "source_profile": alias,
            "rows": [["caller", "provider", "contract.v1", operation]
                     for operation in ("write", "read", "write")],
        })
        self.assertEqual(set(self.registry._objects), set(before))
        for number, original in before.items():
            self.assertIs(self.registry._objects[number], original)
        self.assertIs(self.runtime.catalog.profiles["fixture-id"], profile)
        self.assertIs(profile["requested_edges"], edges)
        self.assertIs(edges[0], second)
        self.assertIs(self.registry.borrow(self.registry.reference(catalog)),
                      self.runtime.catalog)
        self.assertIs(self.registry.borrow(self.registry.reference(profile_id)),
                      self.runtime.value)

    def test_static_edges_reject_replaced_and_released_sources(self) -> None:
        profile = {"requested_edges": []}
        catalog, profile_id, source = self.source(profile)
        self.assertEqual(self.static_edges(source)["rows"], [])
        replacement_profile = {"requested_edges": []}
        self.runtime.catalog.profiles["fixture-id"] = replacement_profile
        replacement = self.lookup(catalog, profile_id)["root"]
        with self.assertRaises(InvalidReference):
            self.static_edges(source)
        self.assertEqual(self.static_edges(replacement)["rows"], [])
        self.dispatcher.release_values(self.frame("values-release", refs=[replacement]))
        with self.assertRaises(InvalidReference):
            self.static_edges(replacement)
        reissued = self.registry.retain(replacement_profile).wire()
        self.assertNotEqual(reissued, replacement)
        with self.assertRaises(InvalidReference):
            self.static_edges(reissued)
        latest = self.lookup(catalog, profile_id)["root"]
        self.assertEqual(latest, reissued)
        self.dispatcher.release_values(self.frame("values-release", refs=[profile_id]))
        with self.assertRaises(InvalidReference):
            self.static_edges(latest)

    def test_static_edges_enforce_the_transport_utf8_byte_limit(self) -> None:
        edge = {"caller_function_id": "é\"\\\n", "target_provider_id": "provider",
                "contract_id": "contract.v1", "operation_id": "read"}
        _, _, source = self.source({"requested_edges": [edge, edge]})
        request = self.frame("resolver-static-edges", source_profile=source)
        response = self.dispatcher.describe_value(request)
        expected = [[*edge.values()], [*edge.values()]]
        self.assertEqual(wire.loads(response)["rows"], expected)
        size = len(response.encode("utf-8"))
        with patch.object(wire, "MAX_BYTES", size):
            self.assertEqual(self.dispatcher.describe_value(request), response)
        with patch.object(wire, "MAX_BYTES", size - 1):
            with self.assertRaises(UnsupportedValue):
                self.dispatcher.describe_value(request)

    def test_throwing_replacement_revokes_static_source(self) -> None:
        catalog, profile_id, source = self.source({"requested_edges": []})

        class FailingProfiles(dict):
            def get(self, key: str) -> object:
                raise KeyError(key)

        self.runtime.catalog.profiles = FailingProfiles()
        with self.assertRaises(MethodRaised):
            self.lookup(catalog, profile_id)
        with self.assertRaises(InvalidReference):
            self.static_edges(source)

    def test_static_edges_keep_actual_key_and_type_failures(self) -> None:
        profile = {}
        _, _, source = self.source(profile)
        for field, edges, expected in (
            (None, None, KeyError),
            ("requested_edges", [{}], KeyError),
            ("requested_edges", None, TypeError),
            ("requested_edges", [None], TypeError),
        ):
            with self.subTest(field=field, edges=edges):
                if field is not None:
                    profile[field] = edges
                with self.assertRaises(MethodRaised) as raised:
                    self.static_edges(source)
                packet = wire.loads(raised.exception.details)
                root = self.registry.reference(packet["root"])
                original = self.registry.borrow(root)
                self.assertIs(type(original), expected)
                if expected is KeyError:
                    self.assertEqual(original.args, (
                        "requested_edges" if field is None else "caller_function_id",
                    ))
                self.assertNotIn(str(original), str(raised.exception))
                self.assertEqual(self.view(packet)["origin"], "resolver-static-edges-raised")
                with self.assertRaises(InvalidReference):
                    self.registry.describe_bootstrap_identity(root)

    def test_static_edges_refuse_outside_abi_without_hooks(self) -> None:
        class ForeignList(list):
            def __iter__(self):
                raise AssertionError("foreign list iteration")

        class ForeignDict(dict):
            def __getitem__(self, key):
                raise AssertionError("foreign row indexing")

        class ForeignString(str):
            def __str__(self):
                raise AssertionError("foreign field coercion")

        profile = {"requested_edges": []}
        _, _, source = self.source(profile)
        for edges in (ForeignList(), [ForeignDict()],
                      [{"caller_function_id": ForeignString("caller")}],
                      [{"caller_function_id": 42}]):
            with self.subTest(value_type=type(edges)):
                profile["requested_edges"] = edges
                with self.assertRaises(UnsupportedValue):
                    self.static_edges(source)

    def test_dynamic_edges_call_live_second_lookup_once_after_static_rows(self) -> None:
        keys = []

        class Profiles(dict):
            def get(self, key: str) -> object:
                value = super().get(key)
                keys.append((key, value))
                return value

        profile = {"requested_edges": []}
        catalog_obj = self.runtime.catalog
        catalog_obj.profiles = Profiles({"fixture-id": profile})
        catalog = self.load()
        first_value = "".join(["fixture", "-id"])
        second_value = "".join(["fixture-", "id"])
        self.assertIsNot(first_value, second_value)
        self.getter(first_value)
        profile_id = self.getter(second_value)
        source = self.lookup(catalog, profile_id)["root"]
        with self.assertRaises(InvalidReference):
            self.dynamic_edges(catalog, profile_id, source)
        self.assertEqual(self.runtime.dynamic_calls, [])
        self.assertEqual(self.static_edges(source)["rows"], [])
        replacement = {"requested_edges": []}
        catalog_obj.profiles[second_value] = replacement
        self.assertEqual(self.dynamic_edges(catalog, profile_id, source), {
            "format": PREFIX + "resolver-dynamic-edges-result.v1",
            "scope": self.scope.wire(), "catalog": catalog,
            "profile_id": profile_id, "source_profile": source, "rows": [],
        })
        self.assertEqual(len(keys), 2)
        self.assertIs(keys[0][0], second_value)
        self.assertIs(keys[0][1], profile)
        self.assertIs(keys[1][0], second_value)
        self.assertIs(keys[1][1], replacement)
        actual_catalog, actual_profile_id, packs = self.runtime.dynamic_calls[0]
        self.assertIs(actual_catalog, catalog_obj)
        self.assertIs(actual_profile_id, second_value)
        self.assertIs(type(packs), tuple)
        self.assertEqual(packs, ())
        self.assertIs(self.registry.borrow(self.registry.reference(source)), profile)
        self.assertEqual(self.static_edges(source)["rows"], [])
        with self.assertRaises(InvalidReference):
            self.dynamic_edges(catalog, profile_id, source)
        self.assertEqual(len(self.runtime.dynamic_calls), 1)

    def test_dynamic_edges_reject_extra_fields_and_unrelated_references(self) -> None:
        profile = {"requested_edges": []}
        self.runtime.catalog.profiles["fixture-id"] = profile
        catalog = self.load()
        first = self.getter("".join(["fixture", "-id"]))
        profile_id = self.getter("".join(["fixture-", "id"]))
        source = self.lookup(catalog, profile_id)["root"]
        self.static_edges(source)
        with self.assertRaises(InvalidFrame):
            self.dynamic_edges(catalog, profile_id, source, additional_pack_ids=["extra"])
        with self.assertRaises(InvalidFrame):
            self.dispatcher.describe_value(self.frame(
                "resolver-dynamic-edges", catalog=catalog, profile_id=profile_id,
            ))
        for args in (
            (self.registry.retain(self.Catalog()).wire(), profile_id, source),
            (catalog, first, source),
            (catalog, profile_id, self.registry.retain(dict(profile)).wire()),
            (catalog, profile_id, {**source, "scope": ["1", "2", "9", "4"]}),
        ):
            with self.subTest(args=args), self.assertRaises(InvalidReference):
                self.dynamic_edges(*args)
        self.assertEqual(self.runtime.dynamic_calls, [])
        self.assertEqual(self.dynamic_edges(catalog, profile_id, source)["rows"], [])

    def test_dynamic_edges_need_successfully_serialized_static_rows(self) -> None:
        profile = {}
        catalog, profile_id, source = self.source(profile)
        with self.assertRaises(MethodRaised):
            self.static_edges(source)
        with self.assertRaises(InvalidReference):
            self.dynamic_edges(catalog, profile_id, source)
        profile["requested_edges"] = []
        request = self.frame("resolver-static-edges", source_profile=source)
        response = self.dispatcher.describe_value(request)
        # The empty rows array also counts toward the UTF-8 bound.
        with patch.object(wire, "MAX_BYTES", len(response) - 1):
            with self.assertRaises(UnsupportedValue):
                self.dispatcher.describe_value(request)
        with self.assertRaises(InvalidReference):
            self.dynamic_edges(catalog, profile_id, source)
        self.assertEqual(self.runtime.dynamic_calls, [])
        self.assertEqual(self.static_edges(source)["rows"], [])
        self.assertEqual(self.dynamic_edges(catalog, profile_id, source)["rows"], [])

    def test_dynamic_edges_clear_static_observation_when_lookup_replaces_source(
        self,
    ) -> None:
        catalog, profile_id, source = self.source({"requested_edges": []})
        self.static_edges(source)
        self.assertEqual(self.lookup(catalog, profile_id)["root"], source)
        with self.assertRaises(InvalidReference):
            self.dynamic_edges(catalog, profile_id, source)
        self.static_edges(source)
        self.runtime.catalog.profiles["fixture-id"] = {"requested_edges": []}
        replacement = self.lookup(catalog, profile_id)["root"]
        self.static_edges(replacement)
        with self.assertRaises(InvalidReference):
            self.dynamic_edges(catalog, profile_id, source)

        class FailingProfiles(dict):
            def get(self, key: str) -> object:
                raise KeyError(key)

        self.runtime.catalog.profiles = FailingProfiles()
        with self.assertRaises(MethodRaised):
            self.lookup(catalog, profile_id)
        with self.assertRaises(InvalidReference):
            self.dynamic_edges(catalog, profile_id, replacement)
        self.assertEqual(self.runtime.dynamic_calls, [])

    def test_dynamic_edges_revoke_eligibility_on_release_reissue_or_retire(self) -> None:
        profile = {"requested_edges": []}
        catalog, profile_id, source = self.source(profile)
        self.static_edges(source)
        self.dispatcher.release_values(self.frame("values-release", refs=[source]))
        with self.assertRaises(InvalidReference):
            self.dynamic_edges(catalog, profile_id, source)
        reissued = self.registry.retain(profile).wire()
        self.assertNotEqual(reissued, source)
        with self.assertRaises(InvalidReference):
            self.dynamic_edges(catalog, profile_id, reissued)
        self.assertEqual(self.lookup(catalog, profile_id)["root"], reissued)
        self.static_edges(reissued)
        self.registry.retire()
        with self.assertRaises(InvalidReference):
            self.dynamic_edges(catalog, profile_id, reissued)
        self.assertEqual(self.runtime.dynamic_calls, [])

    def test_dynamic_edges_keep_the_actual_opaque_exception_and_consume_the_call(
        self,
    ) -> None:
        catalog, profile_id, source = self.source({"requested_edges": []})
        self.static_edges(source)
        original = KeyError("actual dynamic failure")
        cause = TypeError("actual cause")

        class FailingProfiles(dict):
            def get(self, key: str) -> object:
                raise original from cause

        self.runtime.catalog.profiles = FailingProfiles()
        with self.assertRaises(MethodRaised) as raised:
            self.dynamic_edges(catalog, profile_id, source)
        self.assertNotIn("actual dynamic failure", str(raised.exception))
        packet = wire.loads(raised.exception.details)
        root = self.registry.reference(packet["root"])
        self.assertIs(self.registry.borrow(root), original)
        view = self.view(packet)
        self.assertEqual(view["origin"], "resolver-dynamic-edges-raised")
        self.assertEqual(view["kind"], "key-error")
        cause_ref = self.registry.reference(view["exception"]["cause"])
        self.assertIs(self.registry.borrow(cause_ref), cause)
        with self.assertRaises(InvalidReference):
            self.registry.describe_bootstrap_identity(root)
        self.static_edges(source)
        with self.assertRaises(InvalidReference):
            self.dynamic_edges(catalog, profile_id, source)
        self.assertEqual(len(self.runtime.dynamic_calls), 1)

    def test_dynamic_edges_refuse_unexpected_returns_without_running_hooks(self) -> None:
        class ForeignTuple(tuple):
            def __iter__(self):
                raise AssertionError("foreign result iteration")

            def __len__(self):
                raise AssertionError("foreign result length")

            def __eq__(self, other):
                raise AssertionError("foreign result comparison")

        catalog, profile_id, source = self.source({"requested_edges": []})
        for call_count, value in enumerate((ForeignTuple(), (object(),), [], None), 1):
            with self.subTest(value_type=type(value)):
                self.assertEqual(self.lookup(catalog, profile_id)["root"], source)
                self.static_edges(source)
                self.runtime.dynamic_result = value
                with self.assertRaises(UnsupportedValue):
                    self.dynamic_edges(catalog, profile_id, source)
                with self.assertRaises(InvalidReference):
                    self.dynamic_edges(catalog, profile_id, source)
                self.assertEqual(len(self.runtime.dynamic_calls), call_count)

    def test_verified_artifacts_retain_the_live_set_after_dynamic_edges(self) -> None:
        catalog, profile_id, source = self.source({"requested_edges": []})
        self.static_edges(source)
        with self.assertRaises(InvalidReference):
            self.verified_artifacts(catalog, profile_id, source)
        self.dynamic_edges(catalog, profile_id, source)
        self.runtime.catalog.packs.update({
            "one": {"pack": {"artifact_digest": "sha256:one"}},
            "two": {"pack": {"artifact_digest": "sha256:one"}},
            "three": {"pack": {"artifact_digest": "sha256:two"}},
        })
        packet = self.verified_artifacts(catalog, profile_id, source)
        root = self.registry.reference(packet["root"])
        result = self.registry.borrow(root)
        self.assertIs(type(result), set)
        self.assertEqual(result, {"sha256:one", "sha256:two"})
        view = self.view(packet)
        self.assertEqual(view["origin"], "resolver-verified-artifacts-returned")
        self.assertEqual(view["registered_builtin_kind"], "set")
        self.assertEqual(view["owned"], [])
        self.runtime.catalog.packs["four"] = {"pack": {"artifact_digest": "sha256:later"}}
        self.assertNotIn("sha256:later", result)
        with self.assertRaises(InvalidReference):
            self.verified_artifacts(catalog, profile_id, source)

    def test_verified_artifacts_do_not_run_after_dynamic_failure(self) -> None:
        catalog, profile_id, source = self.source({"requested_edges": []})
        self.static_edges(source)
        self.runtime.dynamic_result = (object(),)
        with self.assertRaises(UnsupportedValue):
            self.dynamic_edges(catalog, profile_id, source)
        with self.assertRaises(InvalidReference):
            self.verified_artifacts(catalog, profile_id, source)

    def test_verified_artifacts_preserve_the_actual_lookup_error(self) -> None:
        catalog, profile_id, source = self.source({"requested_edges": []})
        self.static_edges(source)
        self.dynamic_edges(catalog, profile_id, source)
        self.runtime.catalog.packs["missing"] = {"pack": {}}
        with self.assertRaises(MethodRaised) as raised:
            self.verified_artifacts(catalog, profile_id, source)
        packet = wire.loads(raised.exception.details)
        root = self.registry.reference(packet["root"])
        original = self.registry.borrow(root)
        self.assertIs(type(original), KeyError)
        self.assertEqual(original.args, ("artifact_digest",))
        self.assertEqual(self.view(packet)["origin"], "resolver-verified-artifacts-raised")
        with self.assertRaises(InvalidReference):
            self.verified_artifacts(catalog, profile_id, source)

    def test_fixed_profile_checks_shell_path_after_original_lock_digest(self) -> None:
        # The portable caller fixture uses the actual original helpers. The
        # pinned complete Port replay and real Rust IO run in separate CI jobs.
        source_path = (ROOT / "tobkiri_runtime/ecosystem/defaultspack/domain"
                       / "profile_resolution.py")
        original = ast.parse(source_path.read_bytes(), filename=str(source_path))
        functions = [node for node in original.body if isinstance(node, ast.FunctionDef)
                     and node.name in {"_bundle_digest", "_sha256_bytes"}]
        self.assertEqual(len(functions), 2)
        namespace = {"hashlib": hashlib}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source_path), "exec"),
             namespace)
        artifact_source = ROOT / "tobkiri_runtime/tobkiri_protocol/platform_artifact.py"
        artifact_tree = ast.parse(artifact_source.read_bytes(), filename=str(artifact_source))
        artifact_functions = [
            node for node in artifact_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {"artifact_digest", "verify_platform_artifact"}
        ]
        self.assertEqual(len(artifact_functions), 2)
        namespace["Path"] = self.bridge.path
        exec(compile(ast.Module(body=artifact_functions, type_ignores=[]),
                     str(artifact_source), "exec"), namespace)
        catalog, profile_id, source = self.source({"requested_edges": []})
        self.static_edges(source)
        self.dynamic_edges(catalog, profile_id, source)
        artifacts = self.verified_artifacts(catalog, profile_id, source)["root"]
        approved = self.registry.borrow(self.registry.reference(artifacts))
        root = self.runtime.catalog.root
        artifact_root = root.parent / "platform-artifacts"
        self.runtime.catalog.artifact_root = artifact_root
        raw_lock = b"new actual lock bytes\n"
        calls = []
        digests = []
        snapshot = "sha256:" + "1" * 64
        epoch = "9" * 100
        rows = [["z", "authority-ref:" + "2" * 64], ["a", "authority-ref:" + "3" * 64]]

        def inspect(payload: str) -> str:
            frame = wire.loads(payload)
            self.assertEqual(frame["format"], PREFIX + "catalog-request.v1")
            operation = frame["operation"]
            # This fixture connects the tree only; the full entrypoint fixture
            # lives in test_harness_native_path.
            if len(calls) == 12:
                self.assertEqual(operation, "resolve")
                raise _UnboundPlatformArtifacts
            steps = [["join", "bundle.lock.json"]]
            if len(calls) in (3, 4):
                steps = [["parent", ""], ["join", "platform-artifacts"]]
            elif len(calls) == 5:
                steps = [["shell-resolved-root", ""]]
            elif len(calls) >= 6:
                steps = [["shell-resolved-root", ""], ["join", "Tobkiri.app"]]
                if len(calls) == 7:
                    steps.append(["parent", ""])
            self.assertEqual(frame["target"], {
                "binding": "fixture-root", "steps": steps,
            })
            calls.append(operation)
            if operation == "resolve":
                self.assertEqual(frame["args"], [True])
                result = {"kind": "path", "binding": "fixture-root",
                          "value": str(artifact_root), "steps": [["shell-resolved-root", ""]]}
            elif operation == "read-bytes":
                result = {"kind": "bytes", "value": base64.b64encode(raw_lock).decode()}
            elif operation == "iterdir":
                result = {"kind": "shell-directory", "value": []}
            else:
                result = {"kind": "bool", "value": (
                    operation == "is-dir" or (operation == "is-file" and len(calls) == 2)
                )}
            return wire.dumps({
                "format": PREFIX + "inspection-response.v1",
                "scope": self.scope.wire(), "result": result,
            })

        def resolve(actual_catalog: object, actual_id: str, **kwargs: object) -> None:
            self.assertIs(actual_catalog, self.runtime.catalog)
            self.assertIs(actual_catalog.root, root)
            self.assertIs(actual_id, self.runtime.value)
            self.assertIs(kwargs["approved_artifact_digests"], approved)
            self.assertEqual(kwargs["authority_snapshot_digest"], snapshot)
            self.assertEqual(list(kwargs["authority_bindings"].items()), list(map(tuple, rows)))
            self.assertEqual(kwargs["security_epoch"], int(epoch))
            self.assertEqual(kwargs["additional_pack_ids"], ())
            digests.append(namespace["_bundle_digest"](actual_catalog))
            namespace["verify_platform_artifact"](
                actual_catalog.artifact_root,
                {"relative_path": "Tobkiri.app", "artifact_digest": "sha256:" + hashlib.sha256(b"").hexdigest(),
                 "entrypoint": "Tobkiri.app/executable"},
            )
            self.fail("fixture reached beyond the platform artifact boundary")

        self.bridge._inspect = inspect
        self.runtime.resolve_profile = resolve
        request = self.frame("resolver-profile", catalog=catalog, profile_id=profile_id,
                             source_profile=source, verified_artifacts=artifacts,
                             snapshot=snapshot, epoch=epoch, bindings=rows)
        self.assertEqual(wire.loads(self.dispatcher.call("resolve_profile", request)), {
            "format": PREFIX + "resolver-platform-artifacts-unbound.v1",
            "scope": self.scope.wire(),
        })
        self.assertEqual(calls, [
            "is-symlink", "is-file", "read-bytes", "resolve", "is-symlink", "is-dir",
            "is-symlink", "is-symlink", "is-symlink", "is-file", "is-dir",
            "iterdir",
        ])
        self.assertEqual(digests, ["sha256:" + hashlib.sha256(raw_lock).hexdigest()])
        self.assertFalse(self.bridge._resolving_catalog)
        self.assertIsNone(self.bridge._resolver_artifact_root)
        self.assertIsNone(self.bridge._resolver_resolved_root)
        self.assertIsNone(self.bridge._resolver_shell_leaf)
        self.assertFalse(self.bridge._resolver_digest_started)
        self.assertIsNone(self.bridge._resolver_file)
        self.assertIsNone(self.bridge._resolver_directory)
        with self.assertRaises(InvalidReference):
            self.dispatcher.call("resolve_profile", request)


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
