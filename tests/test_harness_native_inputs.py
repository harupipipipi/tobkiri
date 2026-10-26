"""Portable publication/ownership tests; no actual native target admission."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "tools/python313-producer"),
    str(ROOT / "packs/local.ai-tool-bridge-ordered-python"),
]

from harness_native.dispatcher import (  # noqa: E402
    Dispatcher, NotInitialized, OriginalRuntime, PREFIX,
)
from harness_native.value_graph import (  # noqa: E402
    InvalidFrame, InvalidReference, RecordType, Scope, UnsupportedValue,
)
import ordered_json_v2 as wire  # noqa: E402


class OpaqueProxy:
    """Fixture identity only: never a native root, lease or authority."""

    def __str__(self):
        raise AssertionError("publication must not inspect the proxy")

    __repr__ = __str__


class FailingMap(dict):
    """Inject a registration allocation failure after the first real hold."""

    def __setitem__(self, key, value):
        if len(self) == 1:
            raise MemoryError("registration allocation failed")
        super().__setitem__(key, value)


class NativeInputPublicationTests(unittest.TestCase):
    def setUp(self):
        self.scope = Scope(1, 2, 3, 4)
        self.first, self.second = OpaqueProxy(), OpaqueProxy()
        self.inputs = (
            ("bundle-root", self.first),
            ("same-root", self.first),
            ("artifact-root", self.second),
        )
        self.dispatcher = self.make(self.inputs)
        self.registry = self.dispatcher._current().registry

    def frame(self, kind="native-inputs", **extra):
        return wire.dumps(
            {"format": PREFIX + kind + ".v1", "scope": self.scope.wire(), **extra}
        )

    def make(self, inputs, records=None):
        if records is None:
            records = (RecordType("bound-path", OpaqueProxy, native_binding=True),)
        dispatcher = Dispatcher(lambda: OriginalRuntime(object(), records, (), inputs))
        dispatcher.init(self.frame("init"))
        return dispatcher

    def publish(self):
        return json.loads(self.dispatcher.native_inputs(self.frame()))

    def test_publication_preserves_real_proxy_identity_and_only_new_holds(self):
        result = self.publish()
        self.assertEqual(result["format"], PREFIX + "native-inputs-published.v1")
        self.assertEqual(result["scope"], self.scope.wire())
        self.assertEqual([name for name, _ in result["inputs"]],
                         ["bundle-root", "same-root", "artifact-root"])
        first, alias, second = [packet for _, packet in result["inputs"]]
        self.assertEqual(first, alias)
        self.assertNotEqual(first, second)
        self.assertIs(self.registry.borrow(self.registry.reference(first)), self.first)
        self.assertIs(self.registry.borrow(self.registry.reference(second)), self.second)
        self.assertEqual(result["owned"], [first["object"], second["object"]])
        self.assertTrue(all(p == (False, False)
                            for p in self.registry._permissions.values()))
        again = self.publish()
        self.assertEqual(again["inputs"], result["inputs"])
        self.assertEqual(again["owned"], [])

    def test_release_drops_transport_hold_and_republication_never_revives_old_id(self):
        before = self.publish()
        old = before["inputs"][0][1]
        self.dispatcher.release_values(self.frame("values-release", refs=[old]))
        after = self.publish()
        new = after["inputs"][0][1]
        self.assertNotEqual(new, old)
        self.assertEqual(after["inputs"][1][1], new)
        self.assertEqual(after["inputs"][2], before["inputs"][2])
        self.assertEqual(after["owned"], [new["object"]])
        self.assertIs(self.registry.borrow(self.registry.reference(new)), self.first)
        with self.assertRaises(InvalidReference):
            self.registry.reference(old)

    def test_closed_frame_rejects_selection_scope_and_preinit_without_publication(self):
        other = Dispatcher(lambda: self.fail("must not bootstrap on publication"))
        with self.assertRaises(NotInitialized):
            other.native_inputs(self.frame())
        for payload in (
            self.frame(target="bundle-root"), self.frame(path="/tmp/guest-chosen"),
            self.frame(binding="1"), self.frame(cls="OpaqueProxy"),
            self.frame("value-describe"),
            wire.dumps({"format": PREFIX + "native-inputs.v1",
                        "scope": Scope(1, 2, 5, 4).wire()}),
        ):
            with self.assertRaises(InvalidFrame):
                self.dispatcher.native_inputs(payload)
        self.assertEqual(self.registry._objects, {})

    def test_unconnected_bootstrap_is_explicitly_unavailable(self):
        disconnected = self.make(None)
        with self.assertRaises(UnsupportedValue):
            disconnected.native_inputs(self.frame())
        # Only an explicitly supplied empty inventory is a connected empty result.
        empty = self.make(())
        self.assertEqual(json.loads(empty.native_inputs(self.frame()))["inputs"], [])

    def test_invalid_trusted_inventory_never_commits_initialization(self):
        for inputs, records in (
            (list(self.inputs), (RecordType("bound-path", OpaqueProxy,
                                           native_binding=True),)),
            ((("root", self.first), ("root", self.second)),
             (RecordType("bound-path", OpaqueProxy, native_binding=True),)),
            ((("root", []),), ()),
            ((("root", self.first),), (RecordType("data-only", OpaqueProxy),)),
            ((("not a role", self.first),),
             (RecordType("bound-path", OpaqueProxy, native_binding=True),)),
        ):
            dispatcher = Dispatcher(
                lambda: OriginalRuntime(object(), records, (), inputs)
            )
            with self.assertRaises(UnsupportedValue):
                dispatcher.init(self.frame("init"))
            with self.assertRaises(NotInitialized):
                dispatcher.native_inputs(self.frame())

    def test_partial_registration_and_final_encoding_failure_roll_back_new_holds(self):
        self.registry._objects = FailingMap()
        with self.assertRaises(MemoryError):
            self.publish()
        self.assertEqual(self.registry._objects, {})
        self.assertEqual(self.registry._identity, {})
        self.assertEqual(self.registry._permissions, {})
        self.registry._objects = {}
        existing = self.registry.retain(self.first)
        request = self.frame()
        with patch("harness_native.dispatcher.wire.dumps", side_effect=MemoryError):
            with self.assertRaises(MemoryError):
                self.dispatcher.native_inputs(request)
        self.assertEqual(set(self.registry._objects), {existing.object_id})
        self.assertIs(self.registry.borrow(existing), self.first)
        after = self.publish()
        self.assertEqual(after["inputs"][0][1], existing.wire())
        self.assertNotIn(format(existing.object_id, "x"), after["owned"])
        self.assertGreater(int(after["owned"][0], 16), existing.object_id + 1)

    def test_opaque_inputs_cannot_be_described_as_fields_created_or_mutated(self):
        result = self.publish()
        target = result["inputs"][0][1]
        view = json.loads(self.dispatcher.describe_value(
            self.frame("value-describe", root=target)))
        self.assertTrue(view["nodes"][0]["opaque_native_binding"])
        self.assertEqual(view["nodes"][0]["fields"], [])
        with self.assertRaises(InvalidFrame):
            self.dispatcher.import_values(self.frame(
                "value-input", nodes=[{"kind": "bound-path", "fields": []}],
                root={"node": 0}))
        with self.assertRaises(InvalidReference):
            self.dispatcher.append_data_item(self.frame(
                "value-append", target=target, item=target))
        with self.assertRaises(InvalidReference):
            self.dispatcher.set_data_item(self.frame(
                "value-set", target=target, key=target, item=target))
        # Existing refs can be retained in DATA, without creating another proxy.
        imported = json.loads(self.dispatcher.import_values(self.frame(
            "value-input", nodes=[], root={"ref": target})))
        self.assertEqual(imported["root"], target)
        self.assertEqual(imported["owned"], [])

    def test_retired_and_foreign_scopes_cannot_use_published_references(self):
        ref = self.publish()["inputs"][0][1]
        other = self.make(self.inputs)
        # Equal serialized scope alone does not make an ID issued in another registry.
        with self.assertRaises(InvalidReference):
            other._current().registry.reference(ref)
        self.registry.retire()
        with self.assertRaises(InvalidReference):
            self.dispatcher.native_inputs(self.frame())


if __name__ == "__main__":
    unittest.main()
