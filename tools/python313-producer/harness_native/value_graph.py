"""Invocation-local original DATA objects for the nativeKit string-frame ABI.

References are DATA identities, never native permissions. Trusted bootstrap
provides scope and original record classes; frames cannot import/construct a
class or manufacture a root, retained origin, owner or grant. The containing
Wasm/native invocation enforces the original memory, fuel and deadline limits.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import struct
from typing import Any

import ordered_json_v2 as wire

_INPUT = "io.tobkiri.native-value-input.v1"
_VIEW = "io.tobkiri.native-value-view.v1"
_U64 = (1 << 64) - 1
_U32 = (1 << 32) - 1
_HEX = re.compile(r"^(?:0|[1-9a-f][0-9a-f]*)$")
_INTEGER = re.compile(r"^-?(?:0|[1-9a-f][0-9a-f]*)$")
_KINDS = {type(None): "none", bool: "bool", int: "int", float: "float",
          str: "str", list: "list", tuple: "tuple", dict: "dict",
          set: "set", frozenset: "frozenset"}


class InvalidFrame(ValueError):
    """The closed transport frame is malformed, not an original method error."""


class InvalidReference(ValueError):
    """The reference is foreign, released, retired, unissued or wrongly typed."""


class UnsupportedValue(ValueError):
    """This concrete original type/observation is not represented by this ABI."""


def _hex_number(value: Any, maximum: int) -> int:
    if (type(value) is not str or len(value) > 16 or not _HEX.fullmatch(value)):
        raise InvalidFrame("invalid reference integer")
    number = int(value, 16)
    if not 1 <= number <= maximum:
        raise InvalidFrame("reference integer is out of range")
    return number


@dataclass(frozen=True)
class Scope:
    """Trusted invocation/Store identity; not a Profile, principal or proof."""

    account: int
    invocation: int
    store: int
    generation: int

    def __post_init__(self) -> None:
        if any(type(n) is not int or not 1 <= n <= _U64 for n in (
            self.account, self.invocation, self.store, self.generation,
        )):
            raise InvalidFrame("invalid original scope identity")

    def wire(self) -> list[str]:
        """Represent original integer identities without JSON float rounding."""
        return [format(n, "x") for n in (
            self.account, self.invocation, self.store, self.generation,
        )]


@dataclass(frozen=True)
class Reference:
    """One issued transport hold on an actual original object."""

    scope: Scope
    object_id: int

    def wire(self) -> dict[str, Any]:
        """Return the closed reference packet, with no authority fields."""
        return {"scope": self.scope.wire(), "object": format(self.object_id, "x")}


@dataclass(frozen=True)
class RecordType:
    """An exact original class and reviewed DATA fields from trusted bootstrap.

    Native-bound records are opaque. Field selection does not by itself prove
    confidentiality; bootstrap must honor the original disclosure contract.
    There is deliberately no frame-driven constructor or module/class lookup.
    """

    kind: str
    cls: type
    fields: tuple[str, ...] = ()
    native_binding: bool = False


@dataclass(frozen=True)
class ImportedValues:
    """Root plus every new transport hold issued by one input transaction.

    The caller receives the full ownership list, so releasing only a root does
    not silently hide unreleasable child registrations. Other aliases remain
    actual Python references. Released IDs never become valid again.
    """

    root: Reference
    owned: tuple[Reference, ...]

    def wire(self) -> dict[str, Any]:
        """Return compact issued IDs under the same original scope."""
        return {"scope": self.root.scope.wire(), "root": self.root.wire(),
                "owned": [format(r.object_id, "x") for r in self.owned]}


class Registry:
    """Original objects and aliases held until explicit release or retirement."""

    def __init__(self, scope: Scope, records: tuple[RecordType, ...] = ()) -> None:
        if type(scope) is not Scope or type(records) is not tuple:
            raise InvalidFrame("trusted scope and record inventory required")
        self.scope = scope
        self._records: dict[type, RecordType] = {}
        kinds = set(_KINDS.values())
        for record in records:
            if (type(record) is not RecordType or not isinstance(record.cls, type)
                    or record.cls in _KINDS or record.cls in self._records
                    or type(record.kind) is not str
                    or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", record.kind)
                    or record.kind in kinds or type(record.fields) is not tuple
                    or any(type(f) is not str or not f.isidentifier() for f in record.fields)
                    or len(set(record.fields)) != len(record.fields)
                    or type(record.native_binding) is not bool
                    or (record.native_binding and record.fields)):
                raise UnsupportedValue("invalid trusted original record specification")
            kinds.add(record.kind)
            self._records[record.cls] = record
        self._objects: dict[int, Any] = {}
        self._identity: dict[int, int] = {}
        self._permissions: dict[int, tuple[bool, bool]] = {}
        self._next = 1
        self._retired = False

    def _live(self) -> None:
        if self._retired:
            raise InvalidReference("original invocation was retired")

    def _kind(self, value: Any) -> str:
        kind = _KINDS.get(type(value))
        if kind is not None:
            return kind
        record = self._records.get(type(value))
        if record is None:
            raise UnsupportedValue("unsupported concrete original DATA type")
        return record.kind

    def _retain(
        self, value: Any, append: bool = False, set_items: bool = False,
        *, created: list[tuple[Reference, int]] | None = None,
    ) -> tuple[Reference, bool]:
        self._live()
        self._kind(value)
        if ((append and type(value) is not list)
                or (set_items and type(value) is not dict)):
            raise InvalidReference("mutation requires its original DATA collection type")
        identity = id(value)
        existing = self._identity.get(identity)
        if existing is not None:
            # Strong registry ownership prevents id reuse while this row lives.
            assert self._objects[existing] is value
            old = self._permissions[existing]
            result = Reference(self.scope, existing), False
            if append or set_items:
                self._permissions[existing] = (old[0] or append, old[1] or set_items)
            return result
        if self._next > _U32:
            raise InvalidReference("original reference namespace is exhausted")
        object_id = self._next
        reference = Reference(self.scope, object_id)
        result = reference, True
        permissions = append, set_items
        registration = reference, identity
        self._next += 1
        mark = len(created) if created is not None else 0
        try:
            # Register rollback ownership before publishing any map entry.
            # Failure in this append or any later map insertion leaves no row.
            if created is not None:
                created.append(registration)
            self._objects[object_id] = value
            self._identity[identity] = object_id
            self._permissions[object_id] = permissions
        except BaseException:
            self._objects.pop(object_id, None)
            self._identity.pop(identity, None)
            self._permissions.pop(object_id, None)
            if created is not None:
                del created[mark:]
            raise
        return result

    def _rollback(self, created: list[tuple[Reference, int]]) -> None:
        # These are trusted fresh rows, so avoid public release's allocating
        # validation/set construction on a path that may be handling MemoryError.
        while created:
            reference, identity = created.pop()
            self._objects.pop(reference.object_id)
            self._identity.pop(identity)
            self._permissions.pop(reference.object_id)

    def retain(
        self, value: Any, *, append: bool = False, set_items: bool = False,
    ) -> Reference:
        """Issue an identity; mutation permissions come only from nativeKit code."""
        return self._retain(value, append, set_items)[0]

    def reference(self, packet: Any) -> Reference:
        """Validate a closed packet against this exact live registry."""
        self._live()
        if type(packet) is not dict or set(packet) != {"scope", "object"}:
            raise InvalidReference("invalid reference packet")
        if packet["scope"] != self.scope.wire():
            raise InvalidReference("reference belongs to another original scope")
        try:
            number = _hex_number(packet["object"], _U32)
        except InvalidFrame:
            raise InvalidReference("invalid reference object identifier") from None
        result = Reference(self.scope, number)
        self.borrow(result)
        return result

    def borrow(
        self, reference: Reference, expected: type | tuple[type, ...] | None = None,
        *, exact: bool = True,
    ) -> Any:
        """Use the actual object/type, never a frame's declared type tag."""
        self._live()
        if (type(reference) is not Reference or type(reference.scope) is not Scope
                or reference.scope != self.scope
                or type(reference.object_id) is not int
                or reference.object_id not in self._objects):
            raise InvalidReference("unissued, released or foreign reference")
        value = self._objects[reference.object_id]
        if expected is not None:
            types = expected if type(expected) is tuple else (expected,)
            if not types or any(not isinstance(t, type) for t in types):
                raise InvalidReference("invalid trusted type requirement")
            matches = type(value) in types if exact else isinstance(value, types)
            if not matches:
                raise InvalidReference("original argument type does not match")
        return value

    def release(self, references: list[Reference]) -> None:
        """Drop only these transport holds, after validating the entire batch."""
        for reference in references:
            self.borrow(reference)
        for number in {r.object_id for r in references}:
            value = self._objects.pop(number)
            del self._identity[id(value)]
            del self._permissions[number]

    def retire(self) -> None:
        """Invalidate the whole original scope without creating a fresh budget."""
        self._retired = True
        self._objects.clear()
        self._identity.clear()
        self._permissions.clear()

    def append(self, target: Reference, item: Reference) -> None:
        """Apply only an explicitly permitted original list append, atomically."""
        values = self.borrow(target, list)
        value = self.borrow(item)
        if not self._permissions[target.object_id][0]:
            raise InvalidReference("list mutation was not enabled for this DATA node")
        values.append(value)

    def set_item(self, target: Reference, key: Reference, item: Reference) -> None:
        """Set a new/copy DATA dictionary item, preserving Python operation errors."""
        values = self.borrow(target, dict)
        k, v = self.borrow(key), self.borrow(item)
        if not self._permissions[target.object_id][1]:
            raise InvalidReference("dictionary mutation was not enabled for this DATA node")
        values[k] = v

    def copy_dictionary(self, source: Reference) -> Reference:
        """Make the source caller's new outer dict, retaining the same values."""
        value = self.borrow(source, dict)
        return self.retain(dict(value), set_items=True)

    def describe(self, root: Reference) -> str:
        """Return a bounded read view, not a replacement for original objects."""
        self.borrow(root)
        pending = [root]
        seen: set[int] = set()
        rows = []
        created: list[tuple[Reference, int]] = []

        def edge(value: Any) -> dict[str, Any]:
            reference, _ = self._retain(value, created=created)
            if reference.object_id not in seen:
                pending.append(reference)
            return {"ref": reference.wire()}

        try:
            while pending:
                reference = pending.pop()
                if reference.object_id in seen:
                    continue
                seen.add(reference.object_id)
                value = self.borrow(reference)
                kind = self._kind(value)
                row: dict[str, Any] = {"ref": reference.wire(), "kind": kind}
                if kind == "int":
                    row["value"] = ("-" if value < 0 else "") + format(abs(value), "x")
                elif kind == "float":
                    row["value"] = struct.pack(">d", value).hex()
                elif kind in {"str", "bool"}:
                    row["value"] = value
                elif kind in {"list", "tuple", "set", "frozenset"}:
                    row["items"] = [edge(item) for item in value]
                elif kind == "dict":
                    row["items"] = [[edge(k), edge(v)] for k, v in value.items()]
                elif kind != "none":
                    record = self._records[type(value)]
                    row["opaque_native_binding"] = record.native_binding
                    row["fields"] = [[name, edge(getattr(value, name))]
                                     for name in record.fields]
                rows.append(row)
            return wire.dumps({"format": _VIEW, "scope": self.scope.wire(),
                               "root": root.wire(), "nodes": rows})
        except BaseException:
            # Failed description never leaks newly issued holds. Existing
            # references/objects/permissions are unchanged; IDs are not reused.
            self._rollback(created)
            raise

    def import_values(self, payload: str) -> ImportedValues:
        """Construct new closed DATA collections/scalars, or reuse issued refs.

        Class instances and native bindings cannot be constructed by a frame.
        New nodes are private until the complete graph has validated and built.
        Mutable-to-immutable cycles are supported; an all-immutable cycle is
        unconstructible and rejected. Existing issued objects are never copied.
        """
        self._live()
        try:
            frame = wire.loads(payload)
        except wire.OrderedJSONError:
            raise InvalidFrame("invalid or oversized input frame") from None
        if (type(frame) is not dict or set(frame) != {"format", "scope", "nodes", "root"}
                or frame["format"] != _INPUT or frame["scope"] != self.scope.wire()
                or type(frame["nodes"]) is not list):
            raise InvalidFrame("invalid original input graph frame")
        nodes = frame["nodes"]
        external: dict[int, Any] = {}

        def checked_edge(edge: Any) -> None:
            if type(edge) is not dict or len(edge) != 1:
                raise InvalidFrame("invalid DATA graph edge")
            if "node" in edge:
                index = edge["node"]
                if type(index) is not int or not 0 <= index < len(nodes):
                    raise InvalidFrame("invalid local graph node")
            elif "ref" in edge:
                reference = self.reference(edge["ref"])
                external[reference.object_id] = self.borrow(reference)
            else:
                raise InvalidFrame("unknown DATA graph edge")

        checked_edge(frame["root"])
        values: dict[int, Any] = {}
        waiting = set()
        for index, node in enumerate(nodes):
            if type(node) is not dict or type(node.get("kind")) is not str:
                raise InvalidFrame("invalid DATA graph node")
            kind = node["kind"]
            expected = {"kind"} if kind == "none" else (
                {"kind", "items"} if kind in {"list", "tuple", "dict", "set", "frozenset"}
                else {"kind", "value"})
            if set(node) != expected:
                raise InvalidFrame("invalid DATA graph node fields")
            if kind in {"list", "tuple", "dict", "set", "frozenset"}:
                if type(node["items"]) is not list:
                    raise InvalidFrame("invalid DATA collection items")
                for item in node["items"]:
                    if kind == "dict":
                        if type(item) is not list or len(item) != 2:
                            raise InvalidFrame("invalid DATA dictionary entry")
                        checked_edge(item[0])
                        checked_edge(item[1])
                    else:
                        checked_edge(item)
                if kind in {"tuple", "frozenset"}:
                    waiting.add(index)
                else:
                    values[index] = {"list": list, "dict": dict, "set": set}[kind]()
            elif kind == "none":
                values[index] = None
            elif kind in {"str", "bool"}:
                if type(node["value"]) is not {"str": str, "bool": bool}[kind]:
                    raise InvalidFrame("invalid exact scalar type")
                values[index] = node["value"]
            elif kind == "int":
                raw = node["value"]
                if (type(raw) is not str or not _INTEGER.fullmatch(raw) or raw == "-0"):
                    raise InvalidFrame("invalid exact integer representation")
                values[index] = int(raw, 16)
            elif kind == "float":
                raw = node["value"]
                if type(raw) is not str or not re.fullmatch(r"[0-9a-f]{16}", raw):
                    raise InvalidFrame("invalid exact float representation")
                values[index] = struct.unpack(">d", bytes.fromhex(raw))[0]
            else:
                raise UnsupportedValue("frames cannot construct this original concrete type")

        def ready(edge: dict[str, Any]) -> bool:
            return "ref" in edge or edge["node"] in values

        def value(edge: dict[str, Any]) -> Any:
            return external[_hex_number(edge["ref"]["object"], _U32)] if "ref" in edge else values[edge["node"]]

        reachable: set[int] = set()
        pending = [frame["root"]]
        while pending:
            current = pending.pop()
            if "ref" in current or current["node"] in reachable:
                continue
            index = current["node"]
            reachable.add(index)
            node = nodes[index]
            if node["kind"] == "dict":
                pending.extend(edge for pair in node["items"] for edge in pair)
            elif node["kind"] in {"list", "tuple", "set", "frozenset"}:
                pending.extend(node["items"])
        if len(reachable) != len(nodes):
            raise InvalidFrame("unreachable local DATA graph nodes")

        while waiting:
            progressed = []
            for index in waiting:
                node = nodes[index]
                if all(ready(item) for item in node["items"]):
                    constructor = tuple if node["kind"] == "tuple" else frozenset
                    try:
                        values[index] = constructor(value(item) for item in node["items"])
                    except TypeError:
                        raise InvalidFrame("unconstructible immutable DATA collection") from None
                    progressed.append(index)
            if not progressed:
                raise InvalidFrame("unconstructible immutable DATA cycle")
            waiting.difference_update(progressed)
        try:
            for index, node in enumerate(nodes):
                kind = node["kind"]
                if kind == "list":
                    values[index].extend(value(item) for item in node["items"])
                elif kind == "set":
                    values[index].update(value(item) for item in node["items"])
                elif kind == "dict":
                    for key, item in node["items"]:
                        k = value(key)
                        if k in values[index]:
                            raise InvalidFrame("duplicate DATA dictionary key")
                        values[index][k] = value(item)
        except TypeError:
            raise InvalidFrame("unconstructible DATA collection") from None
        root = value(frame["root"])
        created: list[tuple[Reference, int]] = []
        try:
            # Permission is granted only for genuinely NEW input collections.
            # Importing a previously issued readonly ref cannot upgrade it.
            for original in values.values():
                fresh = id(original) not in self._identity
                self._retain(
                    original, append=fresh and type(original) is list,
                    set_items=fresh and type(original) is dict,
                    created=created,
                )
            return ImportedValues(self.retain(root), tuple(ref for ref, _ in created))
        except BaseException:
            self._rollback(created)
            raise
