"""Stdlib tests for the guest native-exception materializer."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
ORIGINAL_ORDERED_JSON = (
    REPO
    / "packs/local.ai-tool-bridge-ordered-python/ordered_json_v2.py"
).resolve()
sys.path.insert(0, str(REPO / "tobkiri_runtime"))
sys.path.insert(0, str(REPO / "tools/python313-producer/harness_native"))
sys.path.insert(0, str(ORIGINAL_ORDERED_JSON.parent))

_spec = importlib.util.spec_from_file_location("ordered_json_v2", ORIGINAL_ORDERED_JSON)
if _spec is None or _spec.loader is None:
    raise RuntimeError("cannot load the original ordered_json_v2 source")
ordered_json_v2 = importlib.util.module_from_spec(_spec)
sys.modules["ordered_json_v2"] = ordered_json_v2
_spec.loader.exec_module(ordered_json_v2)

import exception_graph  # noqa: E402
from tobkiri_host.errors import InvalidArtifactError  # noqa: E402
from tobkiri_protocol.errors import SchemaValidationError  # noqa: E402


def _node(
    kind: str,
    args: list[object],
    fields: dict[str, object] | None = None,
    *,
    cause: int | None = None,
    context: int | None = None,
    suppress_context: bool = False,
) -> dict[str, object]:
    return {
        "kind": kind,
        "args": args,
        "fields": {} if fields is None else fields,
        "cause": cause,
        "context": context,
        "suppress_context": suppress_context,
    }


def _details(
    nodes: list[dict[str, object]], *, graph_id: int = 1, root: int = 0
) -> str:
    return ordered_json_v2.dumps(
        {
            "format": "tobkiri.native-exception/1",
            "graph_id": graph_id,
            "root": root,
            "nodes": nodes,
        }
    )


def _root_message(node: dict[str, object]) -> str:
    kind = node["kind"]
    args = node["args"]
    fields = node["fields"]
    if kind in {
        "os-error",
        "file-not-found-error",
        "permission-error",
        "not-a-directory-error",
        "is-a-directory-error",
        "file-exists-error",
        "interrupted-error",
        "blocking-io-error",
        "timeout-error",
        "connection-error",
        "broken-pipe-error",
        "connection-aborted-error",
        "connection-refused-error",
        "connection-reset-error",
        "process-lookup-error",
        "child-process-error",
    }:
        return f"[Errno {args[0]}] {args[1]}"
    if kind == "json-decode-error":
        error = json.JSONDecodeError(fields["msg"], fields["doc"], fields["pos"])
        return str(error)
    return args[0]


class NativeExceptionGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = exception_graph.ExceptionGraphStore()

    def materialize(
        self,
        nodes: list[dict[str, object]],
        *,
        graph_id: int = 1,
        root: int = 0,
        code: str | None = None,
        message: str | None = None,
    ) -> BaseException:
        details = _details(nodes, graph_id=graph_id, root=root)
        if code is None:
            code = str(nodes[root]["kind"])
        if message is None:
            message = _root_message(nodes[root])
        return self.store.materialize(code, message, details)

    def test_decoder_is_loaded_from_the_explicit_original_source_path(self) -> None:
        self.assertEqual(
            Path(ordered_json_v2.__file__).resolve(), ORIGINAL_ORDERED_JSON
        )
        self.assertTrue(
            issubclass(exception_graph.UnsupportedNativeException, Exception)
        )

    def test_schema_diagnostics_do_not_format_the_message_a_second_time(self) -> None:
        original = SchemaValidationError(
            "schema failed", diagnostics=("missing field", "bad value")
        )
        result = self.materialize(
            [
                _node(
                    "schema-validation-error",
                    [original.args[0]],
                    {"diagnostics": list(original.diagnostics)},
                )
            ]
        )
        self.assertIs(type(result), SchemaValidationError)
        self.assertEqual(result.args, original.args)
        self.assertEqual(result.diagnostics, original.diagnostics)
        self.assertEqual(str(result), original.args[0])
        self.assertEqual(result.args[0].count("missing field"), 1)

    def test_links_preserve_distinct_cause_context_shared_references_and_cycles(
        self,
    ) -> None:
        result = self.materialize(
            [
                _node(
                    "runtime-error",
                    ["root"],
                    cause=1,
                    context=2,
                    suppress_context=True,
                ),
                _node("value-error", ["cause"], cause=3),
                _node("protocol-error", ["context"], context=3),
                _node("canonicalization-error", ["shared"], cause=0),
            ]
        )
        self.assertIsInstance(result, RuntimeError)
        self.assertIs(result, result.__cause__.__cause__.__cause__)
        self.assertIs(result.__context__.__context__, result.__cause__.__cause__)
        self.assertIsNot(result.__cause__, result.__context__)
        self.assertTrue(result.__suppress_context__)
        self.assertFalse(result.__cause__.__suppress_context__)

    def test_raise_from_none_shape_keeps_context_but_suppresses_it(self) -> None:
        result = self.materialize(
            [
                _node(
                    "runtime-error",
                    ["wrapped"],
                    context=1,
                    suppress_context=True,
                ),
                _node("value-error", ["hidden context"]),
            ]
        )
        self.assertIsNone(result.__cause__)
        self.assertEqual(str(result.__context__), "hidden context")
        self.assertTrue(result.__suppress_context__)

    def test_same_graph_id_and_payload_reuses_actual_exception_identity(self) -> None:
        details = _details([_node("runtime-error", ["original"])], graph_id=7)
        first = self.store.materialize("runtime-error", "original", details)
        second = self.store.materialize("runtime-error", "original", details)
        self.assertIs(first, second)

        changed = _details([_node("runtime-error", ["changed"])], graph_id=7)
        with self.assertRaises(exception_graph.UnsupportedNativeException):
            self.store.materialize("runtime-error", "changed", changed)

    def test_exact_os_error_type_is_path_free_and_ignores_host_errno_mapping(
        self,
    ) -> None:
        result = self.materialize(
            [
                _node(
                    "blocking-io-error",
                    [2, "blocked"],
                    {
                        "errno": 2,
                        "strerror": "blocked",
                        "filename": None,
                        "filename2": None,
                    },
                )
            ]
        )
        self.assertIs(type(result), BlockingIOError)
        self.assertEqual(result.args, (2, "blocked"))
        self.assertEqual(result.errno, 2)
        self.assertEqual(result.strerror, "blocked")
        self.assertIsNone(result.filename)
        self.assertIsNone(result.filename2)
        self.assertIsNone(getattr(result, "characters_written", None))
        self.assertEqual(str(result), "[Errno 2] blocked")

    def test_explicit_none_filename_changes_native_os_error_display(self) -> None:
        native = FileNotFoundError(2, "missing")
        explicit_none = OSError.__new__(FileNotFoundError)
        BaseException.__init__(explicit_none, 2, "missing")
        explicit_none.errno = 2
        explicit_none.strerror = "missing"
        explicit_none.filename = None

        self.assertEqual(native.args, explicit_none.args)
        self.assertIsNone(native.filename)
        self.assertIsNone(explicit_none.filename)
        self.assertEqual(str(native), "[Errno 2] missing")
        self.assertEqual(str(explicit_none), "[Errno 2] missing: None")

    def test_os_error_rejects_path_winerror_and_chars_written_payloads(self) -> None:
        valid_fields = {
            "errno": 11,
            "strerror": "blocked",
            "filename": None,
            "filename2": None,
        }
        invalid_fields = [
            {**valid_fields, "filename": "/private/file"},
            {**valid_fields, "winerror": 5},
            {**valid_fields, "characters_written": 2},
        ]
        for fields in invalid_fields:
            with self.subTest(fields=fields):
                with self.assertRaises(exception_graph.UnsupportedNativeException):
                    self.materialize(
                        [_node("blocking-io-error", [11, "blocked"], fields)]
                    )

    def test_invalid_artifact_uses_genuine_class_and_exception_catch_tuple(
        self,
    ) -> None:
        store = exception_graph.ExceptionGraphStore(
            exception_graph.ErrorClasses(invalid_artifact_error=InvalidArtifactError)
        )
        result = store.materialize(
            "invalid-artifact-error",
            "bad artifact",
            _details([_node("invalid-artifact-error", ["bad artifact"])], graph_id=9),
        )
        self.assertIs(type(result), InvalidArtifactError)
        self.assertIsInstance(result, Exception)
        self.assertNotIsInstance(result, (ValueError, RuntimeError))
        with self.assertRaises((InvalidArtifactError, SchemaValidationError)):
            raise result
        with self.assertRaises(InvalidArtifactError):
            raise result
        with self.assertRaises(exception_graph.UnsupportedNativeException):
            exception_graph.ExceptionGraphStore().materialize(
                "invalid-artifact-error",
                "bad artifact",
                _details(
                    [_node("invalid-artifact-error", ["bad artifact"])],
                    graph_id=10,
                ),
            )

    def test_json_decode_error_checks_unicode_newline_and_derived_positions(
        self,
    ) -> None:
        result = self.materialize(
            [
                _node(
                    "json-decode-error",
                    ["unexpected token: line 2 column 1 (char 2)"],
                    {
                        "msg": "unexpected token",
                        "doc": "é\nx",
                        "pos": 2,
                        "lineno": 2,
                        "colno": 1,
                    },
                )
            ]
        )
        self.assertIs(type(result), json_decode_error_type())
        self.assertEqual((result.lineno, result.colno), (2, 1))
        self.assertEqual(
            result.args, ("unexpected token: line 2 column 1 (char 2)",)
        )

        with self.assertRaises(exception_graph.UnsupportedNativeException):
            self.materialize(
                [
                    _node(
                        "json-decode-error",
                        ["unexpected token: line 2 column 1 (char 2)"],
                        {
                            "msg": "unexpected token",
                            "doc": "é\nx",
                            "pos": 2,
                            "lineno": 2,
                            "colno": 2,
                        },
                    )
                ],
                graph_id=2,
            )

    def test_malformed_unknown_extra_and_boolean_indices_are_unsupported(self) -> None:
        malformed_nodes = [
            [_node("not-in-allowlist", ["x"])],
            [{**_node("runtime-error", ["x"]), "unexpected": 1}],
            [_node("runtime-error", ["x"], cause=True)],
            [_node("runtime-error", ["x"], context=False)],
            [_node("runtime-error", ["x"], suppress_context=1)],
        ]
        for nodes in malformed_nodes:
            with self.subTest(nodes=nodes):
                with self.assertRaises(exception_graph.UnsupportedNativeException):
                    self.materialize(nodes)

        for graph_id in (True, -1, 1 << 32):
            with self.subTest(graph_id=graph_id):
                details = _details([_node("runtime-error", ["x"])], graph_id=1)
                frame = ordered_json_v2.loads(details)
                frame["graph_id"] = graph_id
                with self.assertRaises(exception_graph.UnsupportedNativeException):
                    self.store.materialize(
                        "runtime-error", "x", ordered_json_v2.dumps(frame)
                    )

        with self.assertRaises(exception_graph.UnsupportedNativeException):
            self.store.materialize(
                "runtime-error",
                "True",
                _details([_node("runtime-error", ["x"])], root=True),
            )

        extra_frame = ordered_json_v2.loads(
            _details([_node("runtime-error", ["x"])], graph_id=12)
        )
        extra_frame["other"] = 1
        with self.assertRaises(exception_graph.UnsupportedNativeException):
            self.store.materialize(
                "runtime-error", "x", ordered_json_v2.dumps(extra_frame)
            )

    def test_outer_code_and_display_must_match_the_materialized_root(self) -> None:
        details = _details([_node("runtime-error", ["display"])], graph_id=13)
        for code, message in (("value-error", "display"), ("runtime-error", "other")):
            with self.subTest(code=code, message=message):
                with self.assertRaises(exception_graph.UnsupportedNativeException):
                    self.store.materialize(code, message, details)

    def test_schema_and_basic_shapes_reject_unmodeled_fields(self) -> None:
        cases = [
            _node("runtime-error", ["x"], {"extra": True}),
            _node("schema-validation-error", ["x"], {"diagnostics": [1]}),
            _node("value-error", ["x", "y"]),
        ]
        for index, node in enumerate(cases):
            with self.subTest(node=node):
                with self.assertRaises(exception_graph.UnsupportedNativeException):
                    self.materialize([node], graph_id=20 + index)

    def test_resource_errors_from_original_decoder_are_not_reclassified(self) -> None:
        for resource_error in (MemoryError, RecursionError):
            with self.subTest(resource_error=resource_error):
                with patch.object(
                    exception_graph.ordered_json_v2,
                    "loads",
                    side_effect=resource_error("resource limit"),
                ):
                    with self.assertRaises(resource_error):
                        self.store.materialize("runtime-error", "0", "{}")

    def test_user_message_slash_is_not_treated_as_path_data(self) -> None:
        result = self.materialize([_node("value-error", ["opaque / path-like text"])])
        self.assertEqual(str(result), "opaque / path-like text")


def json_decode_error_type() -> type[BaseException]:
    """Return the stdlib decoder's exact exception class for identity checks."""
    return json.JSONDecodeError


if __name__ == "__main__":
    unittest.main()
