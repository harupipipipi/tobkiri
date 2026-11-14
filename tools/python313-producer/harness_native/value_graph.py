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
import base64
from typing import Any, Callable

import ordered_json_v2 as wire

_INPUT = "io.tobkiri.native-value-input.v1"
_VIEW = "io.tobkiri.native-value-view.v1"
_IDENTITY_VIEW = "io.tobkiri.native-bootstrap-identity-view.v1"
_STATIC_EDGES_RESULT = "io.tobkiri.native-resolver-static-edges-result.v1"
_DYNAMIC_EDGES_RESULT = "io.tobkiri.native-resolver-dynamic-edges-result.v1"
_EDGE_FIELDS = ("caller_function_id", "target_provider_id", "contract_id", "operation_id")
_NONITERABLE_JSON = (type(None), bool, int, float)
_NONOBJECT_JSON = (*_NONITERABLE_JSON, str, list)
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

    @classmethod
    def from_wire(cls, value: Any) -> Scope:
        """Decode DATA identity only; this establishes no native ownership."""
        if type(value) is not list or len(value) != 4:
            raise InvalidFrame("invalid original scope packet")
        return cls(*(_hex_number(n, _U64) for n in value))


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
        # Same guest linear heap/account as the registry: one small live entry
        # per fresh factory result, referencing its existing string, not a new
        # native allowance or external table. Release/rollback/retire erase it.
        self._denied: dict[int, str] = {}
        # Only fixed original calls publish these origins. They use the same
        # guest heap and lose their entries with their original holds.
        self._bootstrap_identity: dict[int, str] = {}
        # The same Python string can be returned by both getter occurrences.
        # Track the resolver position separately from the identity of its DATA.
        self._resolver_stage = "awaiting-catalog"
        self._resolver_catalog: int | None = None
        self._resolver_profile_id: int | None = None
        self._resolver_source: int | None = None
        self._resolver_verified_artifacts: int | None = None
        self._resolved_profile: int | None = None
        self._resolver_authority: tuple[str, str, int] | None = None
        self._bootstrap_confirmation: int | None = None
        self._bootstrap_preview_catalog: int | None = None
        self._bootstrap_activation_started = False
        self._bootstrap_activation_body: dict[str, Any] | None = None
        self._bootstrap_activation_listing: Any = None
        self._resolver_dynamic_stage = "awaiting-static"
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
            self._denied.pop(reference.object_id, None)
            self._bootstrap_identity.pop(reference.object_id, None)

    @staticmethod
    def _denied_fields(value: BaseException, message: str) -> dict[str, Any] | None:
        # Fixed builtin descriptors bypass subclass hooks. No str/repr,
        # reflection, recursive graph or caller-selected attribute is used.
        args = BaseException.args.__get__(value)
        if (type(args) is not tuple or len(args) != 1 or type(args[0]) is not str
                or args[0] != message
                or BaseException.__cause__.__get__(value) is not None
                or BaseException.__context__.__get__(value) is not None
                or BaseException.__suppress_context__.__get__(value) is not False):
            return None
        return {"args": [args[0]], "cause": None, "context": None,
                "suppress_context": False}

    def _mark_denied(self, reference: Reference, message: str) -> None:
        value = self.borrow(reference)
        record = self._records.get(type(value))
        if (record is None or record.kind != "profile-resolution-denied"
                or not isinstance(value, BaseException)
                or self._denied_fields(value, message) is None):
            return
        try:
            self._denied[reference.object_id] = message
        except BaseException:
            self._denied.pop(reference.object_id, None)
            raise

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
            self._denied.pop(number, None)
            self._bootstrap_identity.pop(number, None)
            if number == self._resolver_catalog or number == self._resolver_profile_id:
                self._clear_resolver_inputs()
            elif number == self._resolver_source:
                self._clear_resolver_source()
            elif number == self._resolver_verified_artifacts:
                self._resolver_verified_artifacts = None
            elif number == self._resolved_profile:
                self._resolved_profile = None
                self._resolver_authority = None
            elif number == self._bootstrap_confirmation:
                self._bootstrap_confirmation = None
                self._bootstrap_activation_listing = None
            elif number == self._bootstrap_preview_catalog:
                self._bootstrap_preview_catalog = None
                self._bootstrap_activation_listing = None

    def retire(self) -> None:
        """Invalidate the whole original scope without creating a fresh budget."""
        self._retired = True
        self._objects.clear()
        self._identity.clear()
        self._permissions.clear()
        self._denied.clear()
        self._bootstrap_identity.clear()
        self._bootstrap_activation_body = None
        self._clear_resolver_inputs()

    def _clear_resolver_inputs(self) -> None:
        self._resolver_stage = "closed"
        self._resolver_catalog = None
        self._resolver_profile_id = None
        self._clear_resolver_source()

    def _clear_resolver_source(self) -> None:
        self._resolver_source = None
        self._resolver_verified_artifacts = None
        self._resolved_profile = None
        self._resolver_authority = None
        self._bootstrap_confirmation = None
        self._bootstrap_preview_catalog = None
        self._bootstrap_activation_listing = None
        self._resolver_dynamic_stage = "awaiting-static"

    def _begin_resolver_getter(self) -> None:
        if self._resolver_stage == "awaiting-first-getter":
            self._resolver_stage = "first-getter-pending"
        elif self._resolver_stage == "awaiting-second-getter":
            self._resolver_stage = "second-getter-pending"
        elif self._resolver_catalog is not None:
            self._clear_resolver_inputs()

    def _record_resolver_return(self, reference: Reference, origin: str | None) -> None:
        # Called only after successful result serialization. An interrupted
        # or throwing getter cannot advance a pending stage to the next one.
        if origin == "catalog-returned":
            if self._resolver_stage == "awaiting-catalog":
                self._resolver_catalog = reference.object_id
                self._resolver_stage = "awaiting-first-getter"
            else:
                self._clear_resolver_inputs()
        elif origin == "getter-returned":
            if self._resolver_stage == "first-getter-pending":
                self._resolver_stage = "awaiting-second-getter"
            elif self._resolver_stage == "second-getter-pending":
                self._resolver_profile_id = reference.object_id
                self._resolver_stage = "ready"
        elif origin == "resolver-source-lookup-returned":
            if self._resolver_stage == "ready" and type(self.borrow(reference)) is dict:
                self._resolver_source = reference.object_id
        elif origin == "resolver-verified-artifacts-returned":
            if self._resolver_dynamic_stage == "artifact-capture-started":
                self._resolver_verified_artifacts = reference.object_id

    def _resolver_retained_inputs(
        self, catalog_ref: Reference, profile_id_ref: Reference,
    ) -> tuple[Any, str]:
        catalog = self.borrow(catalog_ref)
        profile_id = self.borrow(profile_id_ref)
        record = self._records.get(type(catalog))
        catalog_origin = self._bootstrap_identity.get(catalog_ref.object_id)
        profile_id_origin = self._bootstrap_identity.get(profile_id_ref.object_id)
        if (self._resolver_stage != "ready"
                or catalog_ref.object_id != self._resolver_catalog
                or profile_id_ref.object_id != self._resolver_profile_id
                or catalog_origin != "catalog-returned"
                or profile_id_origin != "getter-returned"
                or record is None or record.cls is not type(catalog)
                or record.kind != "bundled-catalog" or type(profile_id) is not str):
            raise InvalidReference("reference has no fixed resolver source origin")
        return catalog, profile_id

    def _resolver_source_inputs(
        self, catalog_ref: Reference, profile_id_ref: Reference,
    ) -> tuple[Any, str]:
        inputs = self._resolver_retained_inputs(catalog_ref, profile_id_ref)
        # Replacing an authorized lookup revokes the prior source even if the
        # actual lookup throws, returns None, or cannot serialize its result.
        self._clear_resolver_source()
        return inputs

    def _current_resolver_source(self, root: Reference) -> dict[str, Any]:
        source = self.borrow(root, dict)
        if (root.object_id != self._resolver_source
                or self._bootstrap_identity.get(root.object_id)
                != "resolver-source-lookup-returned"):
            raise InvalidReference("reference has no current resolver source origin")
        return source

    def _resolver_static_source(self, root: Reference) -> dict[str, Any]:
        source = self._current_resolver_source(root)
        catalog, profile_id = self._resolver_catalog, self._resolver_profile_id
        if (self._resolver_stage != "ready"
                or catalog is None or self._bootstrap_identity.get(catalog)
                != "catalog-returned"
                or profile_id is None or self._bootstrap_identity.get(profile_id)
                != "getter-returned"):
            raise InvalidReference("reference has no current resolver static edge origin")
        return source

    def _describe_resolver_static_edges(
        self, root: Reference, source: dict[str, Any],
    ) -> str:
        # A new static observation can invalidate an earlier successful one.
        # Repeating it after the dynamic call does not authorize another call.
        if self._resolver_dynamic_stage == "ready":
            self._resolver_dynamic_stage = "awaiting-static"
        # The pinned caller subscripts the actual source and then each edge.
        # Preserve errors from the exact builtin operations without invoking
        # hooks on types that cannot occur in a strictly loaded JSON profile.
        edges = source["requested_edges"]
        if type(edges) is not list:
            if any(type(edges) is kind for kind in _NONITERABLE_JSON):
                iter(edges)  # Actual TypeError for a noniterable JSON scalar.
            raise UnsupportedValue("original requested edges are outside the fixed ABI")
        response = bytearray(wire.dumps({
            "format": _STATIC_EDGES_RESULT, "scope": self.scope.wire(),
            "source_profile": root.wire(),
        }).encode("utf-8"))
        response[-1:] = b',"rows":['
        # Encode one row at a time, using the existing frame byte limit. The
        # observation owns no refs and never materializes a second edge graph.
        for index, edge in enumerate(edges):
            if type(edge) is not dict:
                if any(type(edge) is kind for kind in _NONOBJECT_JSON):
                    edge[_EDGE_FIELDS[0]]  # Actual TypeError for a builtin non-object row.
                raise UnsupportedValue("original requested edge is outside the fixed ABI")
            row = []
            for field in _EDGE_FIELDS:
                value = edge[field]
                if type(value) is not str:
                    raise UnsupportedValue("original edge field is outside the fixed ABI")
                row.append(value)
            try:
                encoded = wire.dumps(row).encode("utf-8")
            except wire.OrderedJSONError:
                raise UnsupportedValue("original edge row cannot be encoded") from None
            separator = bool(index)
            if len(response) + len(encoded) + separator + 2 > wire.MAX_BYTES:
                raise UnsupportedValue("original edge result exceeds the transport frame")
            if separator:
                response.extend(b",")
            response.extend(encoded)
        if len(response) + 2 > wire.MAX_BYTES:
            raise UnsupportedValue("original edge result exceeds the transport frame")
        response.extend(b"]}")
        payload = response.decode("utf-8")
        if self._resolver_dynamic_stage == "awaiting-static":
            self._resolver_dynamic_stage = "ready"
        return payload

    def _begin_resolver_dynamic_edges(
        self, catalog: Reference, profile_id: Reference, source: Reference,
    ) -> tuple[Any, str]:
        inputs = self._resolver_retained_inputs(catalog, profile_id)
        self._current_resolver_source(source)
        if self._resolver_dynamic_stage != "ready":
            raise InvalidReference("reference has no completed static edge observation")
        self._resolver_dynamic_stage = "consumed"
        return inputs

    def _describe_resolver_dynamic_edges(
        self, catalog: Reference, profile_id: Reference, source: Reference, value: Any,
    ) -> str:
        if type(value) is not tuple or len(value) != 0:
            raise UnsupportedValue("original dynamic edges are outside the fresh Setup ABI")
        try:
            payload = wire.dumps({
                "format": _DYNAMIC_EDGES_RESULT,
                "scope": self.scope.wire(),
                "catalog": catalog.wire(),
                "profile_id": profile_id.wire(),
                "source_profile": source.wire(),
                "rows": [],
            })
        except wire.OrderedJSONError:
            raise UnsupportedValue("original dynamic edges cannot be encoded") from None
        self._resolver_dynamic_stage = "dynamic-returned"
        return payload

    def _begin_resolver_verified_artifacts(
        self, catalog: Reference, profile_id: Reference, source: Reference,
    ) -> Any:
        catalog_value, _ = self._resolver_retained_inputs(catalog, profile_id)
        self._current_resolver_source(source)
        if self._resolver_dynamic_stage != "dynamic-returned":
            raise InvalidReference("reference has no completed dynamic edge observation")
        self._resolver_dynamic_stage = "artifact-capture-started"
        return catalog_value

    def _begin_resolver_profile(
        self, catalog: Reference, profile_id: Reference, source: Reference,
        artifacts: Reference,
    ) -> tuple[Any, str, set[str]]:
        catalog_value, profile_value = self._resolver_retained_inputs(catalog, profile_id)
        self._current_resolver_source(source)
        approved = self.borrow(artifacts, set)
        if (self._resolver_dynamic_stage != "artifact-capture-started"
                or artifacts.object_id != self._resolver_verified_artifacts
                or self._bootstrap_identity.get(artifacts.object_id)
                != "resolver-verified-artifacts-returned"):
            raise InvalidReference("reference has no completed artifact capture")
        self._resolver_dynamic_stage = "profile-resolution-started"
        return catalog_value, profile_value, approved

    def _begin_bootstrap_predecessor(
        self, catalog: Reference, profile_id: Reference,
    ) -> tuple[Any, str]:
        inputs = self._resolver_retained_inputs(catalog, profile_id)
        if self._resolver_dynamic_stage != "profile-resolution-returned":
            raise InvalidReference("reference has no completed Profile resolution")
        self._resolver_dynamic_stage = "bootstrap-predecessor-started"
        return inputs

    def _finish_bootstrap_predecessor(self, source: Reference) -> dict[str, Any]:
        value = self._current_resolver_source(source)
        if self._resolver_dynamic_stage != "bootstrap-predecessor-returned":
            raise InvalidReference("reference has no completed predecessor lookup")
        self._resolver_dynamic_stage = "inactive-predecessor-started"
        return value

    def _begin_inactive_equality(self, source: Reference) -> dict[str, Any]:
        value = self._current_resolver_source(source)
        if self._resolver_dynamic_stage != "inactive-predecessor-returned":
            raise InvalidReference("reference has no completed inactive source check")
        self._resolver_dynamic_stage = "inactive-equality-started"
        return value

    def _begin_bootstrap_confirmation(
        self, resolved: Reference, profile_id: Reference, snapshot: str, epoch: str,
        *, has_predecessor: bool,
    ) -> tuple[Any, str, str, int]:
        value = self.borrow(resolved)
        identity = self.borrow(profile_id, str)
        authority = self._resolver_authority
        if (self._resolver_dynamic_stage not in {
                    "inactive-predecessor-returned", "inactive-equality-returned",
                    "inactive-unequal-returned",
                }
                or (has_predecessor and self._resolver_dynamic_stage != "inactive-unequal-returned")
                or resolved.object_id != self._resolved_profile
                or profile_id.object_id != self._resolver_profile_id
                or authority is None or authority[:2] != (snapshot, epoch)):
            raise InvalidReference("references have no completed inactive predecessor check")
        self._resolver_dynamic_stage = "bootstrap-confirmation-started"
        return value, identity, authority[0], authority[2]

    def _begin_bootstrap_preview(
        self, catalog: Reference, resolved: Reference, confirmation: Reference,
    ) -> tuple[Any, Any]:
        original = self.borrow(catalog)
        candidate = self.borrow(resolved)
        self.borrow(confirmation, dict)
        if (self._resolver_dynamic_stage != "bootstrap-confirmation-returned"
                or catalog.object_id != self._resolver_catalog
                or resolved.object_id != self._resolved_profile
                or confirmation.object_id != self._bootstrap_confirmation):
            raise InvalidReference("references have no completed bootstrap confirmation")
        self._resolver_dynamic_stage = "bootstrap-preview-started"
        return original, candidate

    def _begin_bootstrap_listing(
        self, catalog: Reference, confirmation: Reference,
    ) -> tuple[Any, dict[str, Any]]:
        preview = self.borrow(catalog)
        document = self.borrow(confirmation, dict)
        if (self._resolver_dynamic_stage != "bootstrap-preview-returned"
                or catalog.object_id != self._bootstrap_preview_catalog
                or confirmation.object_id != self._bootstrap_confirmation):
            raise InvalidReference("references have no completed bootstrap preview")
        self._resolver_dynamic_stage = "bootstrap-listing-started"
        return preview, document

    def _begin_activation_preflight(self) -> None:
        self._live()
        if self._bootstrap_activation_started or self._resolver_stage != "awaiting-catalog":
            raise InvalidReference("bootstrap activation preflight is outside its original call")
        self._bootstrap_activation_started = True

    def _take_activation_match(self) -> tuple[dict[str, Any], Any, dict[str, Any], Any]:
        self._live()
        if (self._resolver_dynamic_stage != "bootstrap-listing-returned"
                or self._bootstrap_activation_body is None
                or self._bootstrap_activation_listing is None
                or self._resolver_source is None
                or self._resolved_profile is None):
            raise InvalidReference("bootstrap activation has no completed listing")
        body = self._bootstrap_activation_body
        listing = self._bootstrap_activation_listing
        source = self._objects[self._resolver_source]
        resolved = self._objects[self._resolved_profile]
        self._bootstrap_activation_body = None
        self._bootstrap_activation_listing = None
        self._resolver_dynamic_stage = "bootstrap-activation-match-started"
        return body, listing, source, resolved

    @staticmethod
    def _identity_string(value: str, limit: int) -> dict[str, Any]:
        # Python len/iteration preserve lone surrogate codepoints. Never encode,
        # normalize, lowercase or replace the raw value before its validator.
        length = len(value)
        return {"length": min(length, limit + 1),
                "codepoints": [ord(c) for c in value] if length <= limit else []}

    @classmethod
    def _identity_scalar(cls, value: Any) -> dict[str, Any]:
        # Exception DATA is deliberately shallow and finite. Unsupported args
        # are an ABI gap, not a fabricated Python message or canonical denial.
        kind = _KINDS.get(type(value))
        if kind == "none":
            return {"kind": kind, "value": None}
        if kind == "bool":
            return {"kind": kind, "value": value}
        if kind == "int" and value.bit_length() <= 4096:
            return {"kind": kind, "value": ("-" if value < 0 else "")
                    + format(abs(value), "x")}
        if kind == "float":
            return {"kind": kind, "value": struct.pack(">d", value).hex()}
        if kind == "str" and len(value) <= 4096:
            return {"kind": kind, "value": cls._identity_string(value, 4096)}
        raise UnsupportedValue("raw getter exception scalar is outside the finite ABI")

    def describe_bootstrap_identity(self, root: Reference) -> str:
        """Observe one actual raw getter result, with no contents/graph walk.

        Non-string values disclose their trusted exact top-level type only.
        A real getter throw additionally discloses fixed builtin descriptors;
        its intrinsic edges remain opaque actual refs, with no recursive view.
        """
        return self._describe_fixed_call(root, {"getter-returned", "getter-raised"})

    def describe_bootstrap_call(self, root: Reference) -> str:
        """Finite observation of fixed original Setup callers."""
        return self._describe_fixed_call(root, {
            "host-root-returned", "host-root-raised",
            "bundle-root-returned", "bundle-root-raised",
            "catalog-returned", "catalog-raised",
            "manifest-returned", "manifest-raised",
            "resolver-source-lookup-returned", "resolver-source-lookup-raised",
            "resolver-static-edges-raised", "resolver-dynamic-edges-raised",
            "resolver-verified-artifacts-returned", "resolver-verified-artifacts-raised",
            "resolver-profile-raised",
            "bootstrap-predecessor-source-raised",
            "bootstrap-inactive-source-raised", "bootstrap-inactive-equality-raised",
            "bootstrap-confirmation-raised",
            "bootstrap-preview-raised", "bootstrap-listing-raised",
            "bootstrap-activation-preflight-raised", "bootstrap-activation-match-raised",
        })

    def _describe_fixed_call(self, root: Reference, origins: set[str]) -> str:
        value = self.borrow(root)
        origin = self._bootstrap_identity.get(root.object_id)
        if origin not in origins:
            raise InvalidReference("reference has no requested actual caller origin")
        kind = self._kind(value)
        if issubclass(type(value), str) and type(value) is not str:
            raise UnsupportedValue("raw getter str subclass is outside the finite ABI")
        created: list[tuple[Reference, int]] = []

        def edge(error: BaseException | None) -> dict[str, Any] | None:
            if error is None:
                return None
            if not issubclass(type(error), BaseException):
                raise UnsupportedValue("raw getter intrinsic edge is not an exception")
            self._kind(error)  # Exact trusted inventory, not a claimed kind.
            reference, _ = self._retain(error, created=created)
            return reference.wire()

        try:
            exception = None
            if origin.endswith("-raised"):
                if not issubclass(type(value), BaseException):
                    raise InvalidReference("raw getter throw is not an actual exception")
                args = BaseException.args.__get__(value)
                if type(args) is not tuple or len(args) > 8:
                    raise UnsupportedValue("raw getter exception args exceed the finite ABI")
                # The inheritance test uses the actual registered class. It
                # does not trust a frame or infer a catch from a kind string.
                catch = ("os-error" if issubclass(type(value), OSError) else
                         "value-error" if issubclass(type(value), ValueError) else "none")
                os_fields = None
                if catch == "os-error":
                    os_fields = {
                        "errno": self._identity_scalar(OSError.errno.__get__(value)),
                        "strerror": self._identity_scalar(OSError.strerror.__get__(value)),
                        "filename": self._identity_scalar(OSError.filename.__get__(value)),
                        "filename2": self._identity_scalar(OSError.filename2.__get__(value)),
                    }
                unicode_error = origin == "manifest-raised" and type(value) is UnicodeDecodeError

                def argument(item):
                    if unicode_error and type(item) is bytes:
                        return {"kind": "bytes", "value": base64.b64encode(item).decode("ascii")}
                    return self._identity_scalar(item)

                exception = {
                    "recovery_catch": catch,
                    "args": [argument(item) for item in args],
                    "cause": edge(BaseException.__cause__.__get__(value)),
                    "context": edge(BaseException.__context__.__get__(value)),
                    "suppress_context": BaseException.__suppress_context__.__get__(value),
                    "os_fields": os_fields,
                }
                if origin == "manifest-raised" and kind == "json-decode-error":
                    # Actual parser fields, no message-to-position recovery.
                    exception["json_fields"] = {
                        "msg": self._identity_string(value.msg, 4096),
                        "doc": self._identity_string(value.doc, wire.MAX_BYTES),
                        "pos": value.pos, "lineno": value.lineno, "colno": value.colno,
                    }
                if unicode_error:
                    encoding = UnicodeDecodeError.encoding.__get__(value)
                    decoded_object = UnicodeDecodeError.object.__get__(value)
                    start = UnicodeDecodeError.start.__get__(value)
                    end = UnicodeDecodeError.end.__get__(value)
                    reason = UnicodeDecodeError.reason.__get__(value)
                    if (type(encoding) is not str or len(encoding) > 4096
                            or type(decoded_object) is not bytes
                            or type(start) is not int or type(end) is not int
                            or start < 0 or end < start
                            or type(reason) is not str or len(reason) > 4096):
                        raise UnsupportedValue("actual manifest decoder fields exceed the finite ABI")
                    # Only the exact builtin from this actual parser call may
                    # use its fixed descriptor. No arbitrary __str__, subclass
                    # dispatch or message reconstruction is admitted here.
                    formatted = UnicodeDecodeError.__str__(value)
                    if type(formatted) is not str or len(formatted) > 4096:
                        raise UnsupportedValue("actual manifest decoder message exceeds the finite ABI")
                    exception["unicode_fields"] = {
                        "encoding": self._identity_string(encoding, 4096),
                        "object": argument(decoded_object),
                        "start": start, "end": end,
                        "reason": self._identity_string(reason, 4096),
                        "formatted_message": self._identity_string(formatted, 4096),
                    }
            return wire.dumps({
                "format": _IDENTITY_VIEW, "scope": self.scope.wire(),
                "root": root.wire(), "origin": origin, "kind": kind,
                "registered_builtin_kind": _KINDS.get(type(value), "record"),
                "string": self._identity_string(value, 128) if type(value) is str else None,
                "exception": exception,
                "owned": [format(ref.object_id, "x") for ref, _ in created],
            })
        except BaseException:
            self._rollback(created)
            raise

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
                    message = self._denied.get(reference.object_id)
                    if reference == root and message is not None:
                        fields = self._denied_fields(value, message)
                        if fields is None:
                            self._denied.pop(reference.object_id, None)
                        else:
                            row["factory_denied"] = fields
                rows.append(row)
            return wire.dumps({"format": _VIEW, "scope": self.scope.wire(),
                               "root": root.wire(), "nodes": rows,
                               "owned": [format(ref.object_id, "x")
                                         for ref, _ in created]})
        except BaseException:
            # Failed description never leaks newly issued holds. Existing
            # references/objects/permissions are unchanged; IDs are not reused.
            self._rollback(created)
            raise

    def import_values(
        self, payload: str,
        *, _publish: Callable[[ImportedValues], Any] | None = None,
    ) -> ImportedValues | Any:
        """Construct new closed DATA collections/scalars, or reuse issued refs.

        Class instances and native bindings cannot be constructed by a frame.
        New nodes are private until the complete graph has validated and built.
        Mutable-to-immutable cycles are supported; an all-immutable cycle is
        unconstructible and rejected. Existing issued objects are never copied.
        A trusted adapter can supply its final encoder so encoding failure also
        rolls back new holds. Frames cannot select that callback.
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
            result = ImportedValues(self.retain(root), tuple(ref for ref, _ in created))
            return result if _publish is None else _publish(result)
        except BaseException:
            self._rollback(created)
            raise
