"""Source-level graph/identity contracts; not nativeKit/Wasm execution proof."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools/python313-producer"),
                str(ROOT / "packs/local.ai-tool-bridge-ordered-python")]
from harness_native.value_graph import (  # noqa: E402
    ImportedValues, InvalidFrame, InvalidReference, RecordType, Reference,
    Registry, Scope, UnsupportedValue,
)
import ordered_json_v2 as wire  # noqa: E402


@dataclass(frozen=True)
class DataRecord:
    """Portable fixture class supplied by trusted test code, not a fake Host."""
    first: object
    second: object


class OpaqueBinding:
    def __repr__(self) -> str:
        raise AssertionError("opaque bindings must not be formatted")


class FailingMap(dict):
    """Inject one allocation failure after a registry map has been changed."""

    def __setitem__(self, key: object, value: object) -> None:
        super().__setitem__(key, value)
        raise MemoryError("injected registry insertion failure")


class FailingOwnershipList(list):
    def append(self, value: object) -> None:
        super().append(value)
        raise MemoryError("injected ownership append failure")


def payload(registry: Registry, nodes: list, root: dict | None = None) -> str:
    return wire.dumps({"format": "io.tobkiri.native-value-input.v1",
                       "scope": registry.scope.wire(), "nodes": nodes,
                       "root": {"node": 0} if root is None else root})


class ValueGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = Registry(Scope(1, 2, 2, 1), (
            RecordType("fixture-record", DataRecord, ("first", "second")),
            RecordType("fixture-native-binding", OpaqueBinding, native_binding=True),
        ))

    def test_partial_registration_and_ownership_failure_leave_no_unissued_rows(self) -> None:
        for field in ("_objects", "_identity", "_permissions", "ownership"):
            with self.subTest(field=field):
                registry = Registry(Scope(1, 2, 2, 1))
                existing = registry.retain([])
                before = (dict(registry._objects), dict(registry._identity),
                          dict(registry._permissions))
                owned = FailingOwnershipList() if field == "ownership" else []
                if field != "ownership":
                    setattr(registry, field, FailingMap(getattr(registry, field)))
                failed_id = registry._next
                with self.assertRaises(MemoryError):
                    registry._retain([], created=owned)
                self.assertEqual(owned, [])
                self.assertEqual((registry._objects, registry._identity,
                                  registry._permissions), before)
                self.assertEqual(registry.borrow(existing), [])
                with self.assertRaises(InvalidReference):
                    registry.borrow(Reference(registry.scope, failed_id))
                self.assertGreater(registry._next, failed_id)

    def test_description_rolls_back_all_new_holds_after_partial_registration_failure(self) -> None:
        registry = Registry(Scope(1, 2, 2, 1))
        original = [[], []]
        root = registry.retain(original)

        class FailSecondInsertion(dict):
            count = 0

            def __setitem__(self, key: object, value: object) -> None:
                super().__setitem__(key, value)
                self.count += 1
                if self.count == 2:
                    raise MemoryError("injected second child registration failure")

        registry._permissions = FailSecondInsertion(registry._permissions)
        with self.assertRaises(MemoryError):
            registry.describe(root)
        self.assertEqual(list(registry._objects), [root.object_id])
        self.assertEqual(len(registry._identity), 1)
        self.assertEqual(len(registry._permissions), 1)
        self.assertIs(registry.borrow(root), original)

    def test_import_rolls_back_all_new_holds_after_partial_registration_failure(self) -> None:
        registry = Registry(Scope(1, 2, 2, 1))
        root = registry.retain({"existing": []})
        registry._identity = FailingMap(registry._identity)
        with self.assertRaises(MemoryError):
            registry.import_values(payload(registry, [{"kind": "list", "items": []}]))
        self.assertEqual(list(registry._objects), [root.object_id])
        self.assertEqual(len(registry._identity), 1)
        self.assertEqual(len(registry._permissions), 1)
        self.assertEqual(registry.borrow(root), {"existing": []})

    def test_rollback_reuses_precomputed_identities_without_allocating_new_ids(self) -> None:
        registry = Registry(Scope(1, 2, 2, 1))
        created = []
        registry._retain([], created=created)
        registry._retain({}, created=created)
        with patch("harness_native.value_graph.id", create=True,
                   side_effect=MemoryError("id allocation unavailable")):
            registry._rollback(created)
        self.assertEqual(created, [])
        self.assertEqual(registry._objects, {})
        self.assertEqual(registry._identity, {})
        self.assertEqual(registry._permissions, {})

    def test_actual_identity_and_equal_but_distinct_objects_are_not_merged(self) -> None:
        a, b = [], []
        first = self.registry.retain(a)
        second = self.registry.retain(b)
        self.assertEqual(first, self.registry.retain(a))
        self.assertNotEqual(first, second)
        self.assertIs(self.registry.borrow(first), a)
        self.assertIs(self.registry.borrow(second), b)
        self.assertEqual(self.registry.reference(first.wire()), first)

    def test_foreign_generation_unissued_type_tags_and_actual_types_are_checked(self) -> None:
        reference = self.registry.retain([])
        for scope in (Scope(1, 3, 3, 1), Scope(1, 2, 9, 1), Scope(1, 2, 2, 2)):
            with self.assertRaises(InvalidReference):
                self.registry.borrow(Reference(scope, reference.object_id))
        with self.assertRaises(InvalidReference):
            self.registry.borrow(Reference(self.registry.scope, 999))
        with self.assertRaises(InvalidReference):
            self.registry.reference({**reference.wire(), "declared_type": "dict"})
        with self.assertRaises(InvalidReference):
            self.registry.borrow(reference, dict)
        self.assertIsInstance(self.registry.borrow(reference, object, exact=False), list)
        with self.assertRaises(InvalidReference):
            self.registry.borrow(reference, object)

    def test_original_record_nested_alias_and_shallow_dictionary_copy(self) -> None:
        items = []
        original = {"requested_edges": items}
        record = DataRecord(original, original)
        reference = self.registry.retain(record)
        view = json.loads(self.registry.describe(reference))
        row = next(n for n in view["nodes"] if n["kind"] == "fixture-record")
        self.assertEqual(row["fields"][0][1], row["fields"][1][1])
        dictionary = self.registry.retain(original)
        copy = self.registry.copy_dictionary(dictionary)
        self.assertIsNot(self.registry.borrow(copy), original)
        self.assertIs(self.registry.borrow(copy)["requested_edges"], items)
        append_ref = self.registry.retain(items, append=True)
        self.registry.append(append_ref, self.registry.retain({"new": "edge"}))
        self.assertEqual(self.registry.borrow(reference).second["requested_edges"], [{"new": "edge"}])

    def test_new_typed_graph_preserves_mutable_immutable_cycles_and_existing_refs(self) -> None:
        shared = {"original": []}
        old = self.registry.retain(shared)
        imported = self.registry.import_values(payload(self.registry, [
            {"kind": "list", "items": [{"node": 1}, {"node": 2}]},
            {"kind": "tuple", "items": [{"node": 0}]},
            {"kind": "dict", "items": [[{"node": 3}, {"ref": old.wire()}]]},
            {"kind": "str", "value": "shared"},
        ]))
        self.assertIsInstance(imported, ImportedValues)
        result = self.registry.borrow(imported.root)
        self.assertIs(type(result), list)
        self.assertIs(type(result[0]), tuple)
        self.assertIs(result[0][0], result)
        self.assertIs(result[1]["shared"], shared)
        view = json.loads(self.registry.describe(imported.root))
        self.assertEqual(view["format"], "io.tobkiri.native-value-view.v1")
        with self.assertRaises(InvalidFrame):
            self.registry.import_values(wire.dumps(view))

    def test_exact_scalar_and_collection_types_preserve_bits_and_big_integers(self) -> None:
        integer = (1 << 4096) + 7
        imported = self.registry.import_values(payload(self.registry, [
            {"kind": "list", "items": [{"node": n} for n in range(1, 7)]},
            {"kind": "none"}, {"kind": "bool", "value": True},
            {"kind": "int", "value": format(integer, "x")},
            {"kind": "float", "value": "8000000000000000"},
            {"kind": "set", "items": [{"node": 3}]},
            {"kind": "frozenset", "items": [{"node": 3}]},
        ]))
        result = self.registry.borrow(imported.root)
        self.assertIs(result[0], None)
        self.assertIs(type(result[1]), bool)
        self.assertEqual(result[2], integer)
        self.assertEqual(struct.pack(">d", result[3]).hex(), "8000000000000000")
        self.assertIs(type(result[4]), set)
        self.assertIs(type(result[5]), frozenset)
        self.assertEqual(result[4], result[5])
        view = json.loads(self.registry.describe(imported.root))
        self.assertEqual(next(n for n in view["nodes"] if n["kind"] == "int")["value"],
                         format(integer, "x"))

    def test_release_is_atomic_and_does_not_destroy_other_original_aliases(self) -> None:
        value = []
        direct = self.registry.retain(value)
        parent = self.registry.retain([value])
        invalid = Reference(Scope(1, 9, 9, 1), direct.object_id)
        with self.assertRaises(InvalidReference):
            self.registry.release([direct, invalid])
        self.assertIs(self.registry.borrow(direct), value)
        self.registry.release([direct])
        with self.assertRaises(InvalidReference):
            self.registry.borrow(direct)
        self.assertIs(self.registry.borrow(parent)[0], value)
        replacement = self.registry.retain(value)
        self.assertNotEqual(replacement.object_id, direct.object_id)
        with self.assertRaises(InvalidReference):
            self.registry.borrow(direct)

    def test_readonly_reference_cannot_be_upgraded_by_importing_it_as_input(self) -> None:
        dictionary = self.registry.retain({})
        imported = self.registry.import_values(payload(
            self.registry, [], {"ref": dictionary.wire()},
        ))
        self.assertEqual(imported.root, dictionary)
        self.assertEqual(imported.owned, ())
        with self.assertRaises(InvalidReference):
            self.registry.set_item(imported.root, self.registry.retain("x"),
                                   self.registry.retain(1))

    def test_new_input_nested_mutations_keep_aliases_and_return_all_ownership(self) -> None:
        imported = self.registry.import_values(payload(self.registry, [
            {"kind": "dict", "items": [[{"node": 1}, {"node": 2}]]},
            {"kind": "str", "value": "requested_edges"},
            {"kind": "list", "items": []},
        ]))
        self.assertEqual(len(imported.owned), 3)
        nested = self.registry.retain(self.registry.borrow(imported.root)["requested_edges"])
        item = self.registry.retain("inserted")
        self.registry.append(nested, item)
        self.assertEqual(self.registry.borrow(imported.root)["requested_edges"], ["inserted"])
        # All input-created rows have explicit holds in the response, not hidden
        # child registrations which a caller cannot release.
        self.registry.release(list(imported.owned))
        for reference in imported.owned:
            with self.assertRaises(InvalidReference):
                self.registry.borrow(reference)
        self.assertEqual(self.registry.borrow(item), "inserted")

    def test_original_collection_error_and_invalid_transport_are_distinct(self) -> None:
        dictionary = self.registry.retain({}, set_items=True)
        invalid_key = self.registry.retain([])
        with self.assertRaises(TypeError) as error:
            self.registry.set_item(dictionary, invalid_key, self.registry.retain("value"))
        self.assertNotIsInstance(error.exception, InvalidFrame)
        self.assertEqual(self.registry.borrow(dictionary), {})
        with self.assertRaises(InvalidReference):
            self.registry.append(dictionary, self.registry.retain(None))

    def test_bad_graphs_and_failed_description_do_not_publish_new_objects(self) -> None:
        cases = [
            [{"kind": "tuple", "items": [{"node": 0}]}],
            [{"kind": "list", "items": [{"node": 8}]}],
            [{"kind": "bool", "value": 1}],
            [{"kind": "str", "value": "root"}, {"kind": "list", "items": []}],
        ]
        for nodes in cases:
            with self.subTest(nodes=nodes):
                before = len(self.registry._objects)
                with self.assertRaises(InvalidFrame):
                    self.registry.import_values(payload(self.registry, nodes))
                self.assertEqual(len(self.registry._objects), before)
        root = self.registry.retain(DataRecord([], object()))
        before = len(self.registry._objects)
        with self.assertRaises(UnsupportedValue):
            self.registry.describe(root)
        self.assertEqual(len(self.registry._objects), before)

    def test_native_bindings_are_opaque_and_frames_cannot_construct_classes(self) -> None:
        reference = self.registry.retain(OpaqueBinding())
        view = json.loads(self.registry.describe(reference))
        self.assertEqual(view["nodes"][0]["fields"], [])
        self.assertTrue(view["nodes"][0]["opaque_native_binding"])
        with self.assertRaises(UnsupportedValue):
            self.registry.import_values(payload(self.registry, [
                {"kind": "fixture-native-binding", "value": {}},
            ]))
        with self.assertRaises(InvalidReference):
            self.registry.append(reference, self.registry.retain(None))

    def test_exception_object_is_a_normal_value_until_the_method_actually_raises(self) -> None:
        registry = Registry(Scope(2, 3, 3, 1), (
            RecordType("value-error", ValueError,
                       ("args", "__cause__", "__context__", "__suppress_context__")),
        ))
        error = ValueError("original")
        error.__cause__ = error
        reference = registry.retain(error)
        self.assertIs(registry.borrow(reference), error)
        view = json.loads(registry.describe(reference))
        root = next(n for n in view["nodes"] if n["kind"] == "value-error")
        self.assertEqual(dict(root["fields"])["__cause__"]["ref"], reference.wire())

    def test_retirement_and_original_envelope_bound_remain_failures(self) -> None:
        with self.assertRaises(InvalidFrame):
            self.registry.import_values("x" * (wire.MAX_BYTES + 1))
        reference = self.registry.retain([])
        self.registry.retire()
        for operation in (lambda: self.registry.borrow(reference),
                          lambda: self.registry.reference(reference.wire()),
                          lambda: self.registry.retain([]),
                          lambda: self.registry.import_values("{}")):
            with self.assertRaises(InvalidReference):
                operation()


if __name__ == "__main__":
    unittest.main()
