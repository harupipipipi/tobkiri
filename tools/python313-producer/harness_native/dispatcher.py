"""Fixed whole-method DATA dispatch; not a native owner or approval path."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any, Callable
import base64
import json

import ordered_json_v2 as wire

from .value_graph import (
    ImportedValues,
    InvalidFrame,
    InvalidReference,
    RecordType,
    Reference,
    Registry,
    Scope,
    UnsupportedValue,
)

METHODS = (
    "bundled_profile_root",
    "host_resource_root",
    "bootstrap_profile_id",
    "setup_profile_preview",
    "_confirmation",
    "bootstrap_confirmation",
    "profile_confirmation",
    "setup_listing",
    "setup_preview",
    "setup_activation_decision",
    "setup_activation_success",
    "setup_activation_failure",
    "retired_setup_response",
    "load_catalog",
    "catalog_with_packs",
    "catalog_with_profiles",
    "resolve_profile",
    "dynamic_profile_edges",
    "denied",
)
PREFIX = "io.tobkiri.native-"


class _CatalogDiagnostic:
    """Caller-local Copy DATA, never an exception/ref/source carrier."""

    __slots__ = ("phase", "reason", "outside_builtin")
    _REASONS = {
        "unsupported concrete original DATA type": "unrepresented-result-type",
        "nested or foreign catalog call": "foreign-catalog-context",
        "native path value is not represented": "path-value",
        "invalid issued native binding": "binding-shape",
        "Path constructor input is not represented": "path-constructor",
        "foreign native Path context": "foreign-path-context",
        "invalid native inspection response": "response-json",
        "invalid or foreign native inspection response": "response-frame",
        "native error disclosure is not connected": "error-codec-not-bound",
        "native execution failures cannot be domain DATA": "invalid-error-kind",
        "invalid native inspection result": "result-frame",
        "native result kind does not match operation": "result-kind",
        "invalid native path result": "path-result",
        "catalog path DATA outside its fixed call": "path-call-context",
        "invalid catalog path DATA": "path-steps",
        "invalid native scalar result": "scalar-result",
        "invalid native byte result": "byte-result",
        "native result value is not represented": "result-value",
        "unbound lexical Path has no native IO target": "unbound-path",
    }
    _PHASES = frozenset({"frame", "argument-binding", "original-call", "result-retain"})
    _OUTSIDE_BUILTINS = (
        (TypeError, "type-error"),
        (AttributeError, "attribute-error"),
        (NameError, "name-error"),
        (ImportError, "import-error"),
        (NotImplementedError, "not-implemented-error"),
        (AssertionError, "assertion-error"),
        (ModuleNotFoundError, "module-not-found-error"),
        (KeyError, "key-error"),
        (IndexError, "index-error"),
        (StopIteration, "stop-iteration"),
        (OSError, "os-error"),
        (FileNotFoundError, "file-not-found-error"),
        (PermissionError, "permission-error"),
    )
    MAX_BYTES = 512

    def __init__(self):
        self.clear()

    def clear(self):
        self.phase = None
        self.reason = None
        self.outside_builtin = None

    def record_outside(self, error_type, codec_error_type=None):
        # Only Copy tags at the existing outside-inventory classification.
        # Identity checks do not invoke a foreign class's hash/equality hooks.
        self.outside_builtin = "unknown"
        for builtin, tag in self._OUTSIDE_BUILTINS:
            if error_type is builtin:
                self.outside_builtin = tag
                break
        else:
            # Supplied once by the trusted normal bootstrap, never obtained
            # from an exception or frame. The carrier keeps only this tag.
            if codec_error_type is not None and error_type is codec_error_type:
                self.outside_builtin = "native-exception-codec"

    def record(self, error):
        if self.reason is not None:
            return
        # Only OUR exact bridge exception and its builtin args descriptor.
        # Unknown content, subclasses, paths and foreign classes stay unknown.
        args = BaseException.args.__get__(error) if type(error) is UnsupportedValue else None
        self.reason = (
            self._REASONS.get(args[0], "unknown")
            if type(args) is tuple and len(args) == 1 and type(args[0]) is str
            and len(args[0]) <= 128 else "unknown"
        )

    def details(self, error):
        try:
            self.record(error)
            # Every value is one of these fixed ASCII literals. No error
            # object, original argument fragment or class name is serialized.
            reason = self.reason if self.reason in {
                *self._REASONS.values(), "unknown", "outside-fixed-method-inventory"
            } else "unknown"
            frame = {
                "format": "io.tobkiri.native-catalog-refusal-ci.v1",
                "phase": self.phase if self.phase in self._PHASES else "unknown",
                "reason": reason,
            }
            if reason == "outside-fixed-method-inventory":
                frame["outside_builtin"] = (
                    self.outside_builtin if type(self.outside_builtin) is str
                    and (self.outside_builtin == "native-exception-codec"
                         or self.outside_builtin in {
                             tag for _, tag in self._OUTSIDE_BUILTINS
                         }) else "unknown"
                )
            result = wire.dumps(frame)
            if len(result) > self.MAX_BYTES:
                raise MemoryError("fixed catalog diagnostic exceeds its bound")
            return result
        finally:
            self.clear()


class NotInitialized(Exception):
    """No invocation-local registry has been accepted."""


class AlreadyInitialized(Exception):
    """The accepted original scope cannot be replaced."""


class MethodRaised(Exception):
    """Private original exception retained; its text/fields are not disclosed."""

    def __init__(self, details: str) -> None:
        super().__init__("original exception fields are not disclosed")
        self.details = details


@dataclass(frozen=True)
class OriginalRuntime:
    """Only trusted bootstrap supplies these actual original objects/classes."""

    runtime: Any
    records: tuple[RecordType, ...]
    method_errors: tuple[type[Exception], ...]
    # None means actual native input admission is not connected. An explicit
    # tuple contains only preexisting opaque proxies supplied by bootstrap.
    native_inputs: tuple[tuple[str, Any], ...] | None = None
    # Exact class from the existing normal codec import. Diagnostic identity
    # only: never a record, method-error admission, exception field or grant.
    _catalog_codec_error: type[Exception] | None = None


@dataclass(frozen=True)
class _State:
    runtime: Any
    registry: Registry
    method_errors: tuple[type[Exception], ...]
    native_inputs: tuple[tuple[str, Any], ...] | None
    _catalog_codec_error: type[Exception] | None


class Dispatcher:
    """Share one registry across all adapters in one guest interpreter.

    Its scope is DATA. The nativeKit caller and actual imported IO bindings must
    independently retain their real original owner/account/invocation. No frame
    constructs a class, chooses a module, runs code or replenishes a budget.
    """

    def __init__(
        self,
        bootstrap: Callable[[Scope], OriginalRuntime],
        *,
        init_failure_observer: Callable[[Scope, str, BaseException], None] | None = None,
        init_checkpoint_observer: Callable[[int], None] | None = None,
    ) -> None:
        self._bootstrap = bootstrap
        self._init_failure_observer = init_failure_observer
        self._init_checkpoint_observer = init_checkpoint_observer
        self._state: _State | None = None
        self._init_failure: BaseException | None = None
        self._init_failure_scope: Scope | None = None
        self._init_failure_phase: str | None = None

    def _current(self) -> _State:
        if self._state is None:
            raise NotInitialized()
        return self._state

    @staticmethod
    def _load(payload: str) -> dict[str, Any]:
        try:
            frame = wire.loads(payload)
        except wire.OrderedJSONError:
            raise InvalidFrame("invalid or oversized transport frame") from None
        if type(frame) is not dict:
            raise InvalidFrame("closed frame required")
        return frame

    def init(self, payload: str) -> None:
        """Validate the complete scope before loading and committing any state."""
        if self._state is not None:
            raise AlreadyInitialized()
        self._checkpoint(1)
        # A failed attempt is retained only until the next init starts. Scope
        # validation failures cannot accidentally report an older generation.
        self._init_failure = None
        self._init_failure_scope = None
        self._init_failure_phase = None
        frame = self._load(payload)
        self._checkpoint(2)
        if set(frame) != {"format", "scope"} or frame["format"] != PREFIX + "init.v1":
            raise InvalidFrame("invalid original initialization frame")
        scope = Scope.from_wire(frame["scope"])
        self._checkpoint(3)
        try:
            self._checkpoint(4)
            original = self._bootstrap(scope)
            self._checkpoint(5)
        except BaseException as error:
            self._observe_init_failure(scope, "original-factory", error)
            raise
        try:
            self._checkpoint(6)
            state = self._registry_state(scope, original)
            self._checkpoint(7)
        except BaseException as error:
            self._observe_init_failure(scope, "transport-registry", error)
            raise
        self._state = state

    def _checkpoint(self, sequence: int) -> None:
        """Ordinary observer errors cannot replace the original init behavior."""
        try:
            if self._init_checkpoint_observer is not None:
                self._init_checkpoint_observer(sequence)
        except BaseException:
            # Runtime traps and exhausted budgets remain outer failures; this
            # only contains ordinary Python observer exceptions.
            pass

    def _observe_init_failure(
        self, scope: Scope, phase: str, error: BaseException,
    ) -> None:
        """Retain the actual failure without making reporting a new failure."""
        self._init_failure = error
        self._init_failure_scope = scope
        self._init_failure_phase = phase
        try:
            if self._init_failure_observer is not None:
                self._init_failure_observer(scope, phase, error)
        except BaseException:
            # Reporting is best effort. This does not make native traps
            # catchable, and no reporting failure may replace the original.
            pass

    @staticmethod
    def _registry_state(scope: Scope, original: OriginalRuntime) -> _State:
        if (
            type(original) is not OriginalRuntime
            or type(original.method_errors) is not tuple
            or (original._catalog_codec_error is not None
                and type(original._catalog_codec_error) is not type)
        ):
            raise UnsupportedValue("invalid trusted original runtime bundle")
        for cls in original.method_errors:
            if (
                not isinstance(cls, type)
                or not issubclass(cls, Exception)
                or issubclass(cls, (MemoryError, RecursionError))
            ):
                raise UnsupportedValue("execution failures cannot be method errors")
        registry = Registry(scope, original.records)
        records = {record.cls: record for record in original.records}
        for cls in original.method_errors:
            if cls not in records:
                raise UnsupportedValue(
                    "method exception class is not in original inventory"
                )
        # Dedicated provenance-aware exception field extraction is not connected
        # yet. Prevent describe, including nested descriptions, from exposing it.
        for record in original.records:
            if issubclass(record.cls, BaseException) and record.fields:
                raise UnsupportedValue("exception field disclosure is not connected")
        if original.native_inputs is not None:
            if type(original.native_inputs) is not tuple:
                raise UnsupportedValue("invalid trusted native input inventory")
            names = set()
            for item in original.native_inputs:
                if (
                    type(item) is not tuple
                    or len(item) != 2
                    or type(item[0]) is not str
                    or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", item[0])
                    or item[0] in names
                ):
                    raise UnsupportedValue("invalid trusted native input binding")
                record = records.get(type(item[1]))
                if record is None or not record.native_binding:
                    raise UnsupportedValue("native input must be an opaque proxy")
                names.add(item[0])
        return _State(
            original.runtime, registry, original.method_errors, original.native_inputs,
            original._catalog_codec_error,
        )

    def _frame(self, payload: str, kind: str, fields: set[str]) -> dict[str, Any]:
        return self._checked_frame(self._load(payload), kind, fields)

    def _checked_frame(
        self, frame: dict[str, Any], kind: str, fields: set[str],
    ) -> dict[str, Any]:
        state = self._current()
        if (
            set(frame) != {"format", "scope"} | fields
            or frame["format"] != PREFIX + kind + ".v1"
            or frame["scope"] != state.registry.scope.wire()
        ):
            raise InvalidFrame("invalid or foreign transport frame")
        return frame

    def _result(self, value: Any, *, set_items: bool = False,
                denied_message: str | None = None,
                identity_origin: str | None = None,
                publication_profile_id: str | None = None) -> str:
        state = self._current()
        created: list[tuple[Reference, int]] = []
        ref = None
        try:
            if (publication_profile_id is not None
                    and state.registry._kind(value) != "bundled-catalog"):
                raise UnsupportedValue("original publication loader did not return a catalog")
            ref, fresh = state.registry._retain(value, set_items=set_items, created=created)
            if publication_profile_id is not None and not fresh:
                ref = None
                raise UnsupportedValue("original publication catalog was already issued")
            # Only a fresh value returned by the actual fixed denied call may
            # gain this finite observation. Reusing/raising an existing object
            # removes its old provenance; no new authority is issued.
            state.registry._denied.pop(ref.object_id, None)
            state.registry._bootstrap_identity.pop(ref.object_id, None)
            if fresh and denied_message is not None:
                state.registry._mark_denied(ref, denied_message)
            if identity_origin is not None:
                state.registry._bootstrap_identity[ref.object_id] = identity_origin
            payload = wire.dumps(
                {
                    "format": PREFIX + "value-result.v1",
                    "scope": state.registry.scope.wire(),
                    "root": ref.wire(),
                    "owned": [format(r.object_id, "x") for r, _ in created],
                }
            )
            if publication_profile_id is None:
                state.registry._record_resolver_return(ref, identity_origin)
            else:
                state.registry._record_bootstrap_publication_reread(ref, publication_profile_id)
            return payload
        except BaseException:
            if ref is not None:
                state.registry._bootstrap_identity.pop(ref.object_id, None)
            state.registry._rollback(created)
            raise

    def _apply_original(self, action: Callable[[], Any], *, identity: bool = False,
                        caller: str | None = None,
                        _catalog_diagnostic: _CatalogDiagnostic | None = None) -> Any:
        state = self._current()
        try:
            value = action()
        except (MemoryError, RecursionError):
            raise
        except Exception as error:
            if type(error) not in state.method_errors:
                if _catalog_diagnostic is not None:
                    if type(error) is UnsupportedValue:
                        _catalog_diagnostic.record(error)
                    else:
                        _catalog_diagnostic.reason = "outside-fixed-method-inventory"
                        _catalog_diagnostic.record_outside(
                            type(error), state._catalog_codec_error)
                raise UnsupportedValue(
                    "original method failure is not represented"
                ) from None
            # The ordinary ABI stays opaque. Actual fixed callers can qualify
            # their own returned exception for the separate finite observation.
            failure = MethodRaised("")
            failure.details = self._result(
                error, identity_origin=("getter-raised" if identity else
                                        caller + "-raised" if caller else None))
            raise failure from None
        return value

    def call(self, method: str, payload: str, *,
             _catalog_diagnostic: _CatalogDiagnostic | None = None) -> str:
        """Invoke a fixed export; preserve Python's original argument binding."""
        if _catalog_diagnostic is not None:
            if method != "load_catalog" or type(_catalog_diagnostic) is not _CatalogDiagnostic:
                raise InvalidFrame("invalid private catalog diagnostic caller")
            _catalog_diagnostic.clear()
            _catalog_diagnostic.phase = "frame"
        state = self._current()
        if method not in METHODS:
            raise InvalidFrame("method is not a fixed original export")
        frame = self._load(payload)
        if (method == "load_catalog"
                and frame.get("format") == PREFIX + "bootstrap-publication-reread.v1"):
            return self._bootstrap_publication_reread(state, frame, _catalog_diagnostic)
        if method == "resolve_profile":
            if frame.get("format") == PREFIX + "resolver-profile.v1":
                return self._resolve_bundled_profile(state, frame)
            if frame.get("format") == PREFIX + "resolver-legacy-recovery.v1":
                return self._resolve_legacy_recovery(state, frame)
        if method == "bootstrap_confirmation" and frame.get("format") == PREFIX + "bootstrap-fresh-confirmation.v1":
            return self._bootstrap_fresh_confirmation(state, frame)
        if method == "catalog_with_profiles" and frame.get("format") == PREFIX + "bootstrap-preview-catalog.v1":
            return self._bootstrap_preview_catalog(state, frame)
        if method == "catalog_with_profiles" and frame.get("format") == PREFIX + "bootstrap-recovery-catalog.v1":
            return self._bootstrap_recovery_catalog(state, frame)
        if (method == "catalog_with_profiles"
                and frame.get("format") == PREFIX + "bootstrap-registered-packs.v1"):
            return self._bootstrap_registered_packs(state, frame)
        if (method == "catalog_with_profiles"
                and frame.get("format") == PREFIX + "bootstrap-registered-catalog.v1"):
            return self._bootstrap_registered_catalog(state, frame)
        if method == "setup_listing" and frame.get("format") == PREFIX + "bootstrap-setup-listing.v1":
            return self._bootstrap_setup_listing(state, frame)
        if method == "setup_activation_decision":
            if frame.get("format") == PREFIX + "bootstrap-activation-preflight.v1":
                return self._bootstrap_activation_decision(state, frame, preflight=True)
            if frame.get("format") == PREFIX + "bootstrap-activation-match.v1":
                return self._bootstrap_activation_decision(state, frame, preflight=False)
        self._checked_frame(frame, "call", {"args", "kwargs"})
        if type(frame["args"]) is not list or type(frame["kwargs"]) is not list:
            raise InvalidFrame("invalid original call arguments")
        if _catalog_diagnostic is not None:
            _catalog_diagnostic.phase = "argument-binding"
        args = [
            state.registry.borrow(state.registry.reference(ref))
            for ref in frame["args"]
        ]
        kwargs = {}
        for item in frame["kwargs"]:
            if (
                type(item) is not list
                or len(item) != 2
                or type(item[0]) is not str
                or item[0] in kwargs
            ):
                raise InvalidFrame("invalid or duplicate original keyword")
            kwargs[item[0]] = state.registry.borrow(state.registry.reference(item[1]))
        target = getattr(state.runtime, method)
        identity = method == "bootstrap_profile_id" and not args and not kwargs
        if identity:
            state.registry._begin_bootstrap_getter()
        caller = ({"host_resource_root": "host-root",
                   "bundled_profile_root": "bundle-root"}.get(method)
                  if not args and not kwargs else None)
        if method == "load_catalog":
            state.registry._begin_bootstrap_template_catalog()
        if method == "load_catalog" and len(args) == 1 and not kwargs:
            from .native_path import NativePath
            if type(args[0]) is NativePath:
                caller = "catalog"
                action = lambda: args[0]._bridge._load_catalog(target, args[0])
            else:
                action = lambda: target(*args, **kwargs)
        else:
            action = lambda: target(*args, **kwargs)
        if _catalog_diagnostic is not None:
            _catalog_diagnostic.phase = "original-call"
        value = self._apply_original(action, identity=identity, caller=caller,
                                     _catalog_diagnostic=_catalog_diagnostic)
        message = (args[0] if method == "denied" and len(args) == 1
                   and type(args[0]) is str and not kwargs else None)
        if _catalog_diagnostic is not None:
            _catalog_diagnostic.phase = "result-retain"
        return self._result(value, denied_message=message,
                            identity_origin=("getter-returned" if identity else
                                             caller + "-returned" if caller else None))

    def _bootstrap_publication_reread(
        self, state: _State, frame: dict[str, Any], diagnostic: _CatalogDiagnostic | None,
    ) -> str:
        from .native_path import NativePath

        self._checked_frame(frame, "bootstrap-publication-reread", {"root", "catalog"})
        if diagnostic is not None:
            diagnostic.phase = "argument-binding"
        registry = state.registry
        root = registry.borrow(registry.reference(frame["root"]), NativePath)
        catalog, profile_id = registry._take_bootstrap_publication_reread(
            registry.reference(frame["catalog"]),
        )
        if type(catalog.root) is not NativePath or catalog.root._bridge is not root._bridge:
            raise InvalidReference("publication loader root has a different native owner")
        if diagnostic is not None:
            diagnostic.phase = "original-call"
        value = self._apply_original(
            lambda: root._bridge._load_catalog(state.runtime.load_catalog, root),
            caller="catalog", _catalog_diagnostic=diagnostic,
        )
        if diagnostic is not None:
            diagnostic.phase = "result-retain"
        return self._result(
            value, identity_origin="catalog-returned", publication_profile_id=profile_id,
        )

    def _resolve_bundled_profile(self, state: _State, frame: dict[str, Any]) -> str:
        from .native_path import NativePath, _UnboundPlatformArtifacts

        registry = state.registry
        if (set(frame) != {"format", "scope", "catalog", "profile_id", "source_profile",
                           "verified_artifacts", "snapshot", "epoch", "bindings"}
                or frame["scope"] != registry.scope.wire()):
            raise InvalidFrame("invalid fixed bundled resolve frame")
        snapshot, epoch, rows = frame["snapshot"], frame["epoch"], frame["bindings"]
        if (type(snapshot) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", snapshot)
                or type(epoch) is not str or len(epoch) > 4300
                or not re.fullmatch(r"[1-9][0-9]*", epoch) or type(rows) is not list):
            raise InvalidFrame("invalid actual resolver authority arguments")
        bindings = {}
        for row in rows:
            if (type(row) is not list or len(row) != 2
                    or type(row[0]) is not str or row[0] in bindings
                    or type(row[1]) is not str
                    or not re.fullmatch(r"authority-ref:[0-9a-f]{64}", row[1])):
                raise InvalidFrame("invalid actual resolver authority binding")
            bindings[row[0]] = row[1]
        catalog, profile_id, approved = registry._begin_resolver_profile(
            registry.reference(frame["catalog"]),
            registry.reference(frame["profile_id"]),
            registry.reference(frame["source_profile"]),
            registry.reference(frame["verified_artifacts"]),
        )
        root = catalog.root
        if type(root) is not NativePath:
            raise UnsupportedValue("actual resolver catalog root is outside the fixed ABI")

        def resolve_and_publish(*args, **kwargs):
            value = state.runtime.resolve_profile(*args, **kwargs)
            values = (value, *self._resolved_profile_documents(registry, root, value))
            created: list[tuple[Reference, int]] = []
            try:
                refs = [registry._retain(item, created=created)[0] for item in values]
                packet = wire.dumps({
                    "format": PREFIX + "resolver-profile-result.v1",
                    "scope": registry.scope.wire(),
                    **dict(zip(("resolved", "profile", "lock", "plan"),
                               (ref.wire() for ref in refs))),
                    "profile_definition_digest": value.plan["profile_definition_digest"],
                    "plan_digest": value.plan["plan_digest"],
                    "lock_digest": value.lock["lock_digest"],
                    "owned": [format(ref.object_id, "x") for ref, _ in created],
                })
                registry._resolved_profile = refs[0].object_id
                return packet
            except BaseException:
                registry._rollback(created)
                raise

        try:
            security_epoch = int(epoch)
            payload = self._apply_original(
                lambda: root._bridge._resolve_catalog(
                    resolve_and_publish, root, catalog.artifact_root,
                    catalog, profile_id, approved_artifact_digests=approved,
                    authority_snapshot_digest=snapshot, authority_bindings=bindings,
                    security_epoch=security_epoch, additional_pack_ids=(),
                ),
                caller="resolver-profile",
            )
            registry._resolver_authority = (snapshot, epoch, security_epoch)
            registry._resolver_dynamic_stage = "profile-resolution-returned"
            return payload
        except _UnboundPlatformArtifacts:
            return self._resolver_platform_artifacts_unbound(registry)

    @staticmethod
    def _resolved_profile_documents(
        registry: Registry, root: Any, value: Any,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        from .native_path import NativePath

        bridge = root._bridge
        record = registry._records.get(type(value))
        artifact = bridge._resolver_verified_artifact
        if (record is None or record.kind != "resolved-default-profile"
                or not bridge._resolver_header_closed or type(artifact) is not NativePath
                or artifact._bridge is not bridge
                or artifact is not bridge._resolver_shell_leaf):
            raise UnsupportedValue("original resolved profile or Shell witness is not bound")
        documents = (value.profile, value.lock, value.plan)
        if (any(type(document) is not dict for document in documents)
                or len({id(document) for document in documents}) != len(documents)):
            raise UnsupportedValue("original resolved profile documents are not bound")
        return documents

    @staticmethod
    def _resolver_platform_artifacts_unbound(registry: Registry) -> str:
        return wire.dumps({
            "format": PREFIX + "resolver-platform-artifacts-unbound.v1",
            "scope": registry.scope.wire(),
        })

    def _resolve_legacy_recovery(self, state: _State, frame: dict[str, Any]) -> str:
        from .native_path import NativePath, _UnboundPlatformArtifacts
        from tobkiri_protocol.errors import ProtocolError
        from tobkiri_protocol.ids import OPAQUE_REFERENCE_PATTERN, validate_canonical_id

        self._checked_frame(frame, "resolver-legacy-recovery", {
            "catalog", "profile_id", "snapshot", "epoch", "bindings",
        })
        registry = state.registry
        profile_id = frame["profile_id"]
        snapshot, epoch = frame["snapshot"], frame["epoch"]
        rows = frame["bindings"]
        if (type(profile_id) is not str or type(snapshot) is not str
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", snapshot)
                or type(epoch) is not str or len(epoch) > wire.MAX_INTEGER_DIGITS
                or not re.fullmatch(r"[1-9][0-9]*", epoch) or type(rows) is not list):
            raise InvalidFrame("invalid legacy recovery resolver arguments")
        try:
            validate_canonical_id(profile_id, field="profile_id")
        except ProtocolError:
            raise InvalidFrame("invalid legacy recovery Profile identity") from None
        bindings: dict[str, str] = {}
        for row in rows:
            if (type(row) is not list or len(row) != 2
                    or type(row[0]) is not str or row[0].count("|") != 3
                    or row[0] in bindings or type(row[1]) is not str
                    or not row[1].startswith("authority-ref:")
                    or not OPAQUE_REFERENCE_PATTERN.fullmatch(row[1])):
                raise InvalidFrame("invalid legacy recovery Authority binding")
            bindings[row[0]] = row[1]
        security_epoch = int(epoch)
        catalog_ref = registry.reference(frame["catalog"])
        catalog = registry._current_recovery_replacement(catalog_ref)
        root = catalog.root
        if type(root) is not NativePath:
            raise UnsupportedValue("legacy recovery catalog root is outside the fixed ABI")

        # Use the exact original classes registered by bootstrap, which also
        # owns their finite exception observation. The frame names no class.
        error_types = {registry._records[cls].kind: cls for cls in state.method_errors}
        denied = error_types.get("profile-resolution-denied")
        reconfirm = error_types.get("profile-reconfirmation-required")
        if denied is None or reconfirm is None or not issubclass(reconfirm, denied):
            raise UnsupportedValue("legacy recovery resolver errors are not bound")

        def resolve_and_publish(approved: set[str]) -> str:
            try:
                value = state.runtime.resolve_profile(
                    catalog, profile_id, approved_artifact_digests=approved,
                    authority_snapshot_digest=snapshot, authority_bindings=bindings,
                    security_epoch=security_epoch,
                )
            except denied as exc:
                if str(exc).startswith("Authority Kernel reference is missing for edge "):
                    raise reconfirm(
                        f"legacy activation requires explicit reconfirmation: {exc}"
                    ) from exc
                raise
            profile, lock, plan = self._resolved_profile_documents(registry, root, value)
            return wire.dumps({
                "format": PREFIX + "resolver-legacy-recovery-result.v1",
                "scope": registry.scope.wire(),
                "profile": profile, "lock": lock, "plan": plan,
            })

        def recover() -> str:
            # The original legacy caller approves the actual replacement
            # inventory, independently of both the old pins and Setup review.
            approved = {
                str(manifest["pack"]["artifact_digest"])
                for manifest in catalog.packs.values()
            }
            return root._bridge._resolve_catalog(
                resolve_and_publish, root, catalog.artifact_root, approved,
            )

        try:
            return self._apply_original(recover, caller="resolver-legacy-recovery")
        except _UnboundPlatformArtifacts:
            return self._resolver_platform_artifacts_unbound(registry)

    def import_values(self, payload: str) -> str:
        """Import only builtin DATA and existing issued references."""
        registry = self._current().registry

        def publish(result: ImportedValues) -> str:
            return wire.dumps(
                {"format": PREFIX + "values-imported.v1", **result.wire()}
            )

        return registry.import_values(payload, _publish=publish)

    def native_inputs(self, payload: str) -> str:
        """Publish existing native proxies without IO or target selection.

        Repeated publication reuses each live issued reference and reports only
        newly owned holds. Releasing a reference drops that transport hold, not
        bootstrap's original proxy. A later publication can issue a new ID for
        that same object; a released ID never becomes valid again. These IDs do
        not authorize effects or create native ownership.
        """
        self._frame(payload, "native-inputs", set())
        state = self._current()
        if state.native_inputs is None:
            raise UnsupportedValue("native input admission is not connected")
        created: list[tuple[Reference, int]] = []
        try:
            inputs = []
            for name, value in state.native_inputs:
                ref, _ = state.registry._retain(value, created=created)
                inputs.append([name, ref.wire()])
            return wire.dumps(
                {
                    "format": PREFIX + "native-inputs-published.v1",
                    "scope": state.registry.scope.wire(),
                    "inputs": inputs,
                    "owned": [format(ref.object_id, "x") for ref, _ in created],
                }
            )
        except BaseException:
            state.registry._rollback(created)
            raise

    def _bootstrap_template_lookup(self, state: _State, frame: dict[str, Any]) -> str:
        registry = state.registry
        self._checked_frame(frame, "bootstrap-template-lookup", {"catalog", "profile_id"})
        catalog_ref = registry.reference(frame["catalog"])
        profile_id_ref = registry.reference(frame["profile_id"])
        catalog, profile_id = registry._begin_bootstrap_template_lookup(catalog_ref, profile_id_ref)

        from tobkiri_protocol.canonical import strict_loads
        from tobkiri_protocol.errors import ProtocolError
        from tobkiri_protocol.ids import validate_canonical_id

        # The Host validates the getter before reaching this caller. Enforce
        # the same syntax at the DATA boundary without replacing the object.
        try:
            validate_canonical_id(profile_id, field="bootstrap_profile_id")
        except ProtocolError:
            raise InvalidReference("bootstrap template getter is not canonical") from None
        profile = self._apply_original(
            lambda: catalog.profiles[profile_id], caller="bootstrap-template-lookup",
        )
        if type(profile) is not dict:
            raise UnsupportedValue("actual bootstrap template is not a Profile dictionary")
        # Only this selected, verified catalog document crosses as detached
        # DATA. The ordered encoder enforces the exact native JSON domain and
        # transport bounds; StrictV1 also rejects floats and unsafe integers.
        try:
            packet = wire.dumps({
                "format": PREFIX + "bootstrap-template-data.v1",
                "scope": registry.scope.wire(), "catalog": catalog_ref.wire(),
                "profile_id": profile_id_ref.wire(), "profile": profile,
            })
            strict_loads(packet)
        except (wire.OrderedJSONError, ProtocolError):
            raise UnsupportedValue(
                "actual bootstrap template exceeds the StrictV1 DATA domain",
            ) from None
        registry._complete_bootstrap_template_lookup(catalog_ref)
        return packet

    def _bootstrap_predecessor_source(self, state: _State, frame: dict[str, Any]) -> str:
        registry = state.registry
        if (set(frame) != {"format", "scope", "catalog", "profile_id", "needs_digest"}
                or frame["scope"] != registry.scope.wire()
                or type(frame["needs_digest"]) is not bool):
            raise InvalidFrame("invalid fixed bootstrap predecessor source frame")
        catalog, profile_id = registry._begin_bootstrap_predecessor(
            registry.reference(frame["catalog"]), registry.reference(frame["profile_id"]),
        )

        def lookup():
            source = catalog.profiles.get(profile_id)
            # The lookup precedes the original conditional. Digest calculation
            # happens only if the native registry and pointer predicates passed.
            if frame["needs_digest"] and isinstance(source, Mapping):
                from tobkiri_protocol.canonical import canonical_digest

                return canonical_digest(source)
            return None

        digest = self._apply_original(lookup, caller="bootstrap-predecessor-source")
        packet = wire.dumps({"format": PREFIX + "bootstrap-predecessor-source-result.v1",
                             "scope": registry.scope.wire(), "digest": digest})
        registry._bootstrap_predecessor_digest = digest
        registry._resolver_dynamic_stage = "bootstrap-predecessor-returned"
        return packet

    def _bootstrap_inactive_source(self, state: _State, frame: dict[str, Any]) -> str:
        registry = state.registry
        if (set(frame) != {"format", "scope", "source"}
                or frame["scope"] != registry.scope.wire()):
            raise InvalidFrame("invalid fixed inactive predecessor source frame")
        source = registry._finish_bootstrap_predecessor(registry.reference(frame["source"]))
        profile_id = self._apply_original(lambda: str(source["profile_id"]),
                                          caller="bootstrap-inactive-source")
        packet = wire.dumps({"format": PREFIX + "bootstrap-inactive-source-result.v1",
                             "scope": registry.scope.wire(), "profile_id": profile_id})
        registry._resolver_dynamic_stage = "inactive-predecessor-returned"
        return packet

    def _bootstrap_inactive_equality(self, state: _State, frame: dict[str, Any]) -> str:
        registry = state.registry
        if (set(frame) != {"format", "scope", "source", "existing"}
                or frame["scope"] != registry.scope.wire()
                or type(frame["existing"]) is not dict):
            raise InvalidFrame("invalid fixed inactive predecessor equality frame")
        source = registry._begin_inactive_equality(registry.reference(frame["source"]))
        existing = frame["existing"]
        equal = self._apply_original(
            lambda: dict(existing) == dict(source),
            caller="bootstrap-inactive-equality",
        )
        packet = wire.dumps({
            "format": PREFIX + "bootstrap-inactive-equality-result.v1",
            "scope": registry.scope.wire(),
            "equal": equal,
        })
        registry._resolver_dynamic_stage = (
            "inactive-equality-returned" if equal else "inactive-unequal-returned"
        )
        return packet

    def _bootstrap_fresh_confirmation(self, state: _State, frame: dict[str, Any]) -> str:
        registry = state.registry
        if (set(frame) - {"pack_pin_predecessor"} != {
                    "format", "scope", "resolved", "profile_id", "snapshot", "epoch",
                    "definition_predecessor",
                }
                or frame["scope"] != registry.scope.wire()):
            raise InvalidFrame("invalid fixed bootstrap confirmation frame")
        snapshot, epoch = frame["snapshot"], frame["epoch"]
        if type(snapshot) is not str or type(epoch) is not str:
            raise InvalidFrame("invalid fixed bootstrap confirmation authority arguments")
        predecessor = frame["definition_predecessor"]
        if predecessor is not None and (type(predecessor) is not dict or set(predecessor) != {
                "profile_revision", "definition_digest", "store_generation", "snapshot_digest",
                "handover_digest", "display_name", "pack_ids", "base_pack_id", "shell_pack_id",
                "shell_provider_id", "shell_artifact_digest", "shell_executable_artifact_digest",
                "shell_definition_revision",
        }):
            raise InvalidFrame("invalid fixed bootstrap predecessor definition")
        pin = frame.get("pack_pin_predecessor")
        if pin is not None and (
            type(pin) is not dict or set(pin) != {
                "schema", "pointer_digest", "profile_id", "profile_revision", "activation_id",
                "plan_digest", "lock_digest", "generation", "active_definition_digest",
                "registry_profile_revision", "registry_store_generation", "registry_snapshot_digest",
            }
            or pin["schema"] != "io.tobkiri.defaults-pack-pin-predecessor.v1"
            or type(pin["registry_profile_revision"]) is not str
        ):
            raise InvalidFrame("invalid fixed bootstrap Pack pin predecessor")
        resolved, profile_id, snapshot, security_epoch = registry._begin_bootstrap_confirmation(
            registry.reference(frame["resolved"]), registry.reference(frame["profile_id"]),
            snapshot, epoch, has_predecessor=predecessor is not None, pack_pin=pin,
        )

        def confirm():
            # The original outer caller makes this same shallow copy.
            arguments = {
                "resolved": resolved, "profile_id": profile_id,
                "authority_snapshot_digest": snapshot, "security_epoch": security_epoch,
                "definition_predecessor": predecessor,
            }
            if pin is not None:
                arguments["pack_pin_predecessor"] = pin
            return dict(state.runtime.bootstrap_confirmation(**arguments))

        confirmation = self._apply_original(confirm, caller="bootstrap-confirmation")
        created: list[tuple[Reference, int]] = []
        try:
            root, _ = registry._retain(confirmation, created=created)
            packet = wire.dumps({"format": PREFIX + "bootstrap-fresh-confirmation-result.v1",
                               "scope": registry.scope.wire(), "root": root.wire(),
                               "owned": [format(ref.object_id, "x") for ref, _ in created],
                               "confirmation": confirmation})
            registry._bootstrap_confirmation = root.object_id
            registry._resolver_dynamic_stage = "bootstrap-confirmation-returned"
            return packet
        except BaseException:
            registry._rollback(created)
            raise

    def _bootstrap_admitted_closure_step(
        self, state: _State, frame: dict[str, Any],
    ) -> str:
        self._checked_frame(frame, "bootstrap-admitted-closure-step", {
            "catalog", "pack_id", "manifest", "artifact_digest",
        })
        registry = state.registry
        catalog_ref = registry.reference(frame["catalog"])
        catalog = registry._current_bootstrap_recovery_catalog(catalog_ref)
        pack_id, manifest, digest = (
            frame["pack_id"], frame["manifest"], frame["artifact_digest"],
        )
        if (type(pack_id) is not str
                or (manifest is None and digest is not None)
                or (manifest is not None and (
                    type(manifest) is not dict or type(digest) is not str
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                ))):
            raise InvalidFrame("invalid fixed admitted Pack closure step")
        if type(catalog.packs) is not dict:
            raise UnsupportedValue("original Pack inventory is outside the fixed ABI")
        # The original recovery call has no artifact pins. Bundled membership
        # therefore wins before any fresh Host filesystem observation.
        if pack_id in catalog.packs:
            if manifest is not None:
                raise InvalidFrame("external manifest cannot replace a bundled Pack")
            manifest = catalog.packs[pack_id]
            kind = "bundled"
        elif manifest is not None:
            kind = "external-read"
        else:
            kind = "external-required"

        dependencies = []
        if kind != "external-required":
            if type(manifest) is not dict or type(manifest.get("requirements")) is not dict:
                raise UnsupportedValue("Pack manifest is outside the fixed ABI")
            requirements = manifest["requirements"]
            if kind == "external-read":
                # The Host rechecks semantic identity on its validated bytes
                # before this detached DATA observation. The packet confers
                # neither Pack admission nor an executable-catalog receipt.
                pack = manifest.get("pack")
                if (type(pack) is not dict or pack.get("id") != pack_id
                        or pack.get("artifact_digest") != digest
                        or pack.get("kind") not in ("normal_sandbox", "application")
                        or requirements.get("execution_boundary")
                        not in ("sandbox", "declarative_only")):
                    raise UnsupportedValue("external Pack manifest identity differs")
            dependencies_by_id = requirements.get("pack_dependencies")
            if (type(dependencies_by_id) is not dict
                    or any(type(key) is not str for key in dependencies_by_id)):
                raise UnsupportedValue("Pack dependencies are outside the fixed ABI")
            dependencies = sorted(dependencies_by_id)
        return wire.dumps({
            "format": PREFIX + "bootstrap-admitted-closure-step-result.v1",
            "scope": registry.scope.wire(), "catalog": catalog_ref.wire(),
            "pack_id": pack_id, "kind": kind, "dependencies": dependencies,
        })

    def _compose_verified_packs(
        self, state: _State, catalog: Any, rows: Any, *, registered: bool = False,
    ) -> Any:
        from .native_path import NativePath

        if (type(rows) is not list or type(catalog.root) is not NativePath
                or type(catalog.packs) is not dict or type(catalog.executable_catalogs) is not dict):
            raise UnsupportedValue("verified external catalog arguments differ")
        original_packs = catalog.packs
        original_executables = catalog.executable_catalogs
        pack_rows = tuple(original_packs.items())
        executable_rows = tuple(original_executables.items())
        shared_fields = ("root", "bases", "shells", "profiles", "artifact_root")
        shared_values = tuple(getattr(catalog, name) for name in shared_fields)
        packs = dict(pack_rows)
        seen = set()
        checked = []
        for row in rows:
            if (type(row) is not list or len(row) != 3 or type(row[0]) is not str
                    or row[0] in seen or (not registered and row[0] in original_packs)
                    or type(row[1]) is not dict or type(row[2]) is not dict):
                raise InvalidFrame("invalid verified external Pack row")
            pack_id, manifest, executable = row
            seen.add(pack_id)
            header = manifest.get("pack")
            integrity = manifest.get("integrity")
            if (type(header) is not dict or header.get("id") != pack_id
                    or header.get("kind") not in ("normal_sandbox", "application")
                    or type(integrity) is not dict or executable.get("pack_id") != pack_id
                    or type(integrity.get("source_identity")) is not str
                    or executable.get("source_identity") != integrity["source_identity"]):
                raise UnsupportedValue("verified external Pack identity differs")
            if registered:
                digest = header.get("artifact_digest")
                requirements = manifest.get("requirements")
                if (type(digest) is not str
                        or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                        or type(requirements) is not dict
                        or requirements.get("execution_boundary")
                        not in ("sandbox", "declarative_only")):
                    raise UnsupportedValue("verified registered Pack manifest differs")
                if pack_id in original_packs:
                    previous_digest = original_packs[pack_id]["pack"]["artifact_digest"]
                    if digest == previous_digest:
                        raise UnsupportedValue("verified registered Pack pin did not change")
            packs[pack_id] = manifest
            checked.append((pack_id, manifest, executable))

        ordered = sorted(checked, key=lambda row: row[0])
        expected_packs = tuple(packs.items())
        executables = dict(executable_rows)
        for pack_id, _, executable in ordered:
            executables[pack_id] = executable
        expected_executables = tuple(executables.items())

        def matches(mapping: Any, expected: tuple[tuple[str, Any], ...]) -> bool:
            return (type(mapping) is dict and len(mapping) == len(expected)
                    and all(key == wanted and item is entry
                            for (key, item), (wanted, entry)
                            in zip(mapping.items(), expected)))

        value = catalog.root._bridge._with_verified_catalog_rows(
            ordered, lambda: state.runtime.catalog_with_packs(catalog, packs),
        )
        if (type(value) is not type(catalog) or value is catalog
                or catalog.packs is not original_packs or value.packs is original_packs
                or catalog.executable_catalogs is not original_executables
                or value.executable_catalogs is original_executables
                or not matches(original_packs, pack_rows)
                or not matches(original_executables, executable_rows)
                or not matches(value.packs, expected_packs)
                or not matches(value.executable_catalogs, expected_executables)
                or any(getattr(value, name) is not original
                       or getattr(catalog, name) is not original
                       for name, original in zip(shared_fields, shared_values))):
            raise UnsupportedValue("original external Pack catalog is not bound")
        return value

    def _bootstrap_recovery_catalog(self, state: _State, frame: dict[str, Any]) -> str:
        fields = {"catalog", "profiles"}
        if "external" in frame:
            fields.add("external")
        self._checked_frame(frame, "bootstrap-recovery-catalog", fields)
        registry = state.registry
        catalog_ref = registry.reference(frame["catalog"])
        rows = frame["profiles"]
        if type(rows) is not list:
            raise InvalidFrame("invalid fixed recovery Profile rows")
        # The Host supplies its checked Profile store rows in list order.
        # They are detached DATA; only the original catalog is an issued ref.
        profiles = {}
        for row in rows:
            if (type(row) is not list or len(row) != 2 or type(row[0]) is not str
                    or type(row[1]) is not dict or row[1].get("profile_id") != row[0]
                    or row[0] in profiles):
                raise InvalidFrame("invalid fixed recovery Profile row")
            profiles[row[0]] = row[1]

        catalog = registry._begin_bootstrap_recovery_catalog(catalog_ref)
        if "external" in frame:
            catalog = self._apply_original(
                lambda: self._compose_verified_packs(state, catalog, frame["external"]),
                caller="bootstrap-recovery-catalog",
            )
        value = self._apply_original(
            lambda: state.runtime.catalog_with_profiles(catalog, profiles),
            caller="bootstrap-recovery-catalog",
        )
        if (type(value) is not type(catalog) or value is catalog
                or type(value.profiles) is not dict
                or value.profiles is profiles or value.profiles is catalog.profiles
                or len(value.profiles) != len(profiles)
                or any(key != expected or entry is not profile
                       for (key, entry), (expected, profile)
                       in zip(value.profiles.items(), profiles.items()))
                or any(getattr(value, name) is not getattr(catalog, name) for name in (
                    "root", "packs", "bases", "shells", "artifact_root", "executable_catalogs",
                ))):
            raise UnsupportedValue("original bootstrap recovery catalog is not bound")
        created: list[tuple[Reference, int]] = []
        try:
            root, fresh = registry._retain(value, created=created)
            if not fresh:
                raise UnsupportedValue("original bootstrap recovery catalog was already issued")
            response = wire.dumps({"format": PREFIX + "bootstrap-recovery-catalog-result.v1",
                               "scope": registry.scope.wire(), "root": root.wire(),
                               "owned": [format(ref.object_id, "x") for ref, _ in created]})
            registry._record_recovery_catalog_replacement(root)
            return response
        except BaseException:
            registry._rollback(created)
            raise

    def _bootstrap_registered_packs(self, state: _State, frame: dict[str, Any]) -> str:
        caller = "bootstrap-registered-packs"
        self._checked_frame(frame, caller, {"catalog", "profile_id", "external"})
        registry = state.registry
        catalog = registry._begin_bootstrap_registered_packs(
            registry.reference(frame["catalog"]), registry.reference(frame["profile_id"]),
        )
        value = self._apply_original(
            lambda: self._compose_verified_packs(
                state, catalog, frame["external"], registered=True,
            ),
            caller=caller,
        )
        created: list[tuple[Reference, int]] = []
        try:
            root, fresh = registry._retain(value, created=created)
            if not fresh:
                raise UnsupportedValue("original registered Setup Pack catalog was already issued")
            response = wire.dumps({
                "format": PREFIX + "bootstrap-registered-packs-result.v1",
                "scope": registry.scope.wire(), "root": root.wire(),
                "owned": [format(ref.object_id, "x") for ref, _ in created],
            })
            registry._record_registered_packs_replacement(root)
            return response
        except BaseException:
            registry._rollback(created)
            raise

    def _bootstrap_registered_catalog(self, state: _State, frame: dict[str, Any]) -> str:
        self._checked_frame(frame, "bootstrap-registered-catalog", {
            "catalog", "profile_id", "candidate",
        })
        candidate = frame["candidate"]
        if type(candidate) is not dict or type(candidate.get("profile_id")) is not str:
            raise InvalidFrame("invalid fixed registered Setup candidate")
        registry = state.registry
        catalog, profile_id = registry._begin_bootstrap_registered_catalog(
            registry.reference(frame["catalog"]), registry.reference(frame["profile_id"]),
            candidate["profile_id"],
        )

        fields = ("root", "packs", "bases", "shells", "artifact_root", "executable_catalogs")
        original_fields = tuple(getattr(catalog, name) for name in fields)
        original_profiles = catalog.profiles

        def overlay() -> tuple[Any, dict[str, Any], tuple[tuple[str, Any], ...]]:
            profiles = {**original_profiles, profile_id: candidate}
            rows = tuple(profiles.items())
            return state.runtime.catalog_with_profiles(catalog, profiles), profiles, rows

        value, profiles, rows = self._apply_original(
            overlay, caller="bootstrap-registered-catalog",
        )
        if (type(value) is not type(catalog) or value is catalog
                or type(value.profiles) is not dict
                or value.profiles is original_profiles or value.profiles is profiles
                or len(value.profiles) != len(rows)
                or any(key != expected or entry is not profile
                       for (key, entry), (expected, profile)
                       in zip(value.profiles.items(), rows))
                or any(getattr(value, name) is not original
                       for name, original in zip(fields, original_fields))):
            raise UnsupportedValue("original registered Setup catalog is not bound")
        created: list[tuple[Reference, int]] = []
        try:
            root, fresh = registry._retain(value, created=created)
            if not fresh:
                raise UnsupportedValue("original registered Setup catalog was already issued")
            response = wire.dumps({
                "format": PREFIX + "bootstrap-registered-catalog-result.v1",
                "scope": registry.scope.wire(), "root": root.wire(),
                "owned": [format(ref.object_id, "x") for ref, _ in created],
            })
            registry._record_registered_catalog_replacement(root)
            return response
        except BaseException:
            registry._rollback(created)
            raise

    def _bootstrap_preview_catalog(self, state: _State, frame: dict[str, Any]) -> str:
        registry = state.registry
        if (set(frame) != {"format", "scope", "catalog", "resolved", "confirmation"}
                or frame["scope"] != registry.scope.wire()):
            raise InvalidFrame("invalid fixed bootstrap preview frame")
        confirmation = registry.reference(frame["confirmation"])
        catalog, resolved = registry._begin_bootstrap_preview(
            registry.reference(frame["catalog"]), registry.reference(frame["resolved"]),
            confirmation,
        )

        def preview():
            return state.runtime.catalog_with_profiles(
                catalog, {**catalog.profiles, resolved.profile["profile_id"]: resolved.profile},
            )

        value = self._apply_original(preview, caller="bootstrap-preview")
        if type(value) is not type(catalog) or value is catalog:
            raise UnsupportedValue("original bootstrap preview catalog is not bound")
        created: list[tuple[Reference, int]] = []
        try:
            root, _ = registry._retain(value, created=created)
            packet = wire.dumps({"format": PREFIX + "bootstrap-preview-catalog-result.v1",
                                 "scope": registry.scope.wire(), "root": root.wire(),
                                 "owned": [format(ref.object_id, "x") for ref, _ in created],
                                 "confirmation": confirmation.wire()})
            registry._bootstrap_preview_catalog = root.object_id
            registry._resolver_dynamic_stage = "bootstrap-preview-returned"
            return packet
        except BaseException:
            registry._rollback(created)
            raise

    def _bootstrap_setup_listing(self, state: _State, frame: dict[str, Any]) -> str:
        registry = state.registry
        if (set(frame) != {"format", "scope", "catalog", "confirmation", "active",
                          "activation_denied", "denial_diagnostic"}
                or frame["scope"] != registry.scope.wire()
                or type(frame["active"]) is not bool
                or type(frame["activation_denied"]) is not bool
                or type(frame["denial_diagnostic"]) not in {str, type(None)}):
            raise InvalidFrame("invalid fixed bootstrap listing frame")
        catalog, confirmation = registry._begin_bootstrap_listing(
            registry.reference(frame["catalog"]), registry.reference(frame["confirmation"]),
        )

        def listing():
            original = state.runtime.setup_listing(
                catalog, confirmation, active=frame["active"],
                activation_denied=frame["activation_denied"],
                denial_diagnostic=frame["denial_diagnostic"],
            )
            return original, dict(original)

        original, value = self._apply_original(listing, caller="bootstrap-listing")
        packet = wire.dumps({"format": PREFIX + "bootstrap-setup-listing-result.v1",
                             "scope": registry.scope.wire(), "listing": value})
        if registry._bootstrap_activation_body is not None:
            registry._bootstrap_activation_listing = original
        registry._resolver_dynamic_stage = "bootstrap-listing-returned"
        return packet

    def _bootstrap_activation_decision(
        self, state: _State, frame: dict[str, Any], *, preflight: bool,
    ) -> str:
        registry = state.registry
        keys = {"format", "scope", "body"} if preflight else {"format", "scope"}
        if set(frame) != keys or frame["scope"] != registry.scope.wire():
            raise InvalidFrame("invalid fixed bootstrap activation decision frame")
        if preflight:
            body = frame["body"]
            if type(body) is not dict:
                raise InvalidFrame("bootstrap activation body must be a mapping")
            registry._begin_activation_preflight()
            listing = None
            caller = "bootstrap-activation-preflight"
        else:
            body, listing, source, resolved = registry._take_activation_match()
            caller = "bootstrap-activation-match"

        def decide():
            decision = state.runtime.setup_activation_decision(body, listing)
            if registry._kind(decision) != "setup-activation-decision":
                raise UnsupportedValue("original setup activation decision type differs")
            if decision.response is not None:
                return "rejected", dict(decision.response)
            if decision.confirmation is None:
                raise UnsupportedValue("original setup decision has no activation input")
            if preflight:
                return "continue", None
            profile, lock, plan = resolved.profile, resolved.lock, resolved.plan
            if any(type(document) is not dict for document in (profile, lock, plan)):
                raise UnsupportedValue("original resolved profile documents are not bound")
            return "matched", {
                "confirmation": dict(decision.confirmation),
                "source_profile": source,
                "profile_definition_digest": plan["profile_definition_digest"],
                "bundle_digest": lock["bundle_digest"],
                "plan_digest": plan["plan_digest"],
                "resolved": {"profile": profile, "lock": lock, "plan": plan},
            }

        outcome, value = self._apply_original(decide, caller=caller)
        publication_profile_id = None
        if outcome == "matched":
            publication_profile_id = registry._matched_publication_profile_id(
                value["confirmation"],
            )
        packet = wire.dumps({
            "format": PREFIX + caller + "-result.v1",
            "scope": registry.scope.wire(), "outcome": outcome, "value": value,
        })
        if preflight and outcome == "continue":
            registry._bootstrap_activation_body = body
        if publication_profile_id is not None:
            registry._record_bootstrap_publication_match(publication_profile_id)
        return packet

    def _bootstrap_publication_source(self, state: _State, frame: dict[str, Any]) -> str:
        self._checked_frame(frame, "bootstrap-publication-source", {"catalog"})
        registry = state.registry
        catalog, profile_id = registry._take_bootstrap_publication_source(
            registry.reference(frame["catalog"]),
        )
        source = self._apply_original(
            lambda: catalog.profiles[profile_id], caller="bootstrap-publication-source",
        )
        if type(source) is not dict:
            raise UnsupportedValue("original publication Profile is not a dictionary")
        try:
            return wire.dumps({
                "format": PREFIX + "bootstrap-publication-source-result.v1",
                "scope": registry.scope.wire(), "source": source,
            })
        except wire.OrderedJSONError:
            raise UnsupportedValue("original publication Profile exceeds DATA bounds") from None

    def describe_value(self, payload: str) -> str:
        """Serve closed DATA observations and fixed original Setup operations."""
        state = self._current()
        frame = self._load(payload)
        if frame.get("format") == PREFIX + "bootstrap-publication-host-start.v1":
            self._checked_frame(frame, "bootstrap-publication-host-start", set())
            packet = wire.dumps({
                "format": PREFIX + "bootstrap-publication-host-start-result.v1",
                "scope": state.registry.scope.wire(),
            })
            state.registry._begin_bootstrap_publication_host()
            return packet
        if frame.get("format") == PREFIX + "bootstrap-publication-source.v1":
            return self._bootstrap_publication_source(state, frame)
        if frame.get("format") == PREFIX + "bootstrap-template-lookup.v1":
            return self._bootstrap_template_lookup(state, frame)
        if frame.get("format") == PREFIX + "bootstrap-recovery-finished.v1":
            self._checked_frame(frame, "bootstrap-recovery-finished", {"catalog"})
            registry = state.registry
            catalog = registry.reference(frame["catalog"])
            result = wire.dumps({"format": PREFIX + "bootstrap-recovery-finished-result.v1",
                                 "scope": registry.scope.wire()})
            registry._finish_bootstrap_recovery(catalog)
            return result
        if frame.get("format") == PREFIX + "bootstrap-admitted-closure-step.v1":
            return self._bootstrap_admitted_closure_step(state, frame)
        if frame.get("format") == PREFIX + "bootstrap-predecessor-source.v1":
            return self._bootstrap_predecessor_source(state, frame)
        if frame.get("format") == PREFIX + "bootstrap-inactive-source.v1":
            return self._bootstrap_inactive_source(state, frame)
        if frame.get("format") == PREFIX + "bootstrap-inactive-equality.v1":
            return self._bootstrap_inactive_equality(state, frame)
        if frame.get("format") == PREFIX + "resolver-source-lookup.v1":
            registry = state.registry
            if (set(frame) != {"format", "scope", "catalog", "profile_id"}
                    or frame["scope"] != registry.scope.wire()):
                raise InvalidFrame("invalid fixed resolver source lookup frame")
            catalog, profile_id = registry._resolver_source_inputs(
                registry.reference(frame["catalog"]),
                registry.reference(frame["profile_id"]),
            )
            # The Host has validated this exact getter result. The pinned
            # validate_canonical_id returns the input object unchanged.
            value = self._apply_original(
                lambda: catalog.profiles.get(profile_id),
                caller="resolver-source-lookup",
            )
            return self._result(
                value, identity_origin="resolver-source-lookup-returned",
            )
        if frame.get("format") == PREFIX + "resolver-static-edges.v1":
            registry = state.registry
            if (set(frame) != {"format", "scope", "source_profile"}
                    or frame["scope"] != registry.scope.wire()):
                raise InvalidFrame("invalid fixed resolver static edge frame")
            root = registry.reference(frame["source_profile"])
            source = registry._resolver_static_source(root)
            return self._apply_original(
                lambda: registry._describe_resolver_static_edges(root, source),
                caller="resolver-static-edges",
            )
        if frame.get("format") == PREFIX + "resolver-dynamic-edges.v1":
            registry = state.registry
            if (set(frame) != {"format", "scope", "catalog", "profile_id",
                               "source_profile"}
                    or frame["scope"] != registry.scope.wire()):
                raise InvalidFrame("invalid fixed resolver dynamic edge frame")
            catalog_ref = registry.reference(frame["catalog"])
            profile_id_ref = registry.reference(frame["profile_id"])
            source_ref = registry.reference(frame["source_profile"])
            catalog, profile_id = registry._begin_resolver_dynamic_edges(
                catalog_ref, profile_id_ref, source_ref,
            )
            # Fresh bundled Setup passes an empty tuple. Still make the real
            # call: the Pack repeats its live profile lookup before returning.
            value = self._apply_original(
                lambda: state.runtime.dynamic_profile_edges(catalog, profile_id, ()),
                caller="resolver-dynamic-edges",
            )
            return registry._describe_resolver_dynamic_edges(
                catalog_ref, profile_id_ref, source_ref, value,
            )
        if frame.get("format") == PREFIX + "resolver-verified-artifacts.v1":
            registry = state.registry
            if (set(frame) != {"format", "scope", "catalog", "profile_id",
                               "source_profile"}
                    or frame["scope"] != registry.scope.wire()):
                raise InvalidFrame("invalid fixed resolver artifact frame")
            catalog = registry._begin_resolver_verified_artifacts(
                registry.reference(frame["catalog"]),
                registry.reference(frame["profile_id"]),
                registry.reference(frame["source_profile"]),
            )
            # The original builds a real set in catalog iteration order, before
            # calling the Port. It remains live for that call's membership checks.
            value = self._apply_original(
                lambda: {str(manifest["pack"]["artifact_digest"])
                         for manifest in catalog.packs.values()},
                caller="resolver-verified-artifacts",
            )
            return self._result(
                value, identity_origin="resolver-verified-artifacts-returned",
            )
        if frame.get("format") == PREFIX + "catalog-manifest-parse.v1":
            if (set(frame) != {"format", "scope", "bytes"}
                    or frame["scope"] != state.registry.scope.wire()
                    or type(frame["bytes"]) is not str):
                raise InvalidFrame("invalid fixed manifest parse frame")
            raw = base64.b64decode(frame["bytes"], validate=True)

            def parse_manifest():
                # Exactly Path.read_text(encoding='utf-8')'s strict decoder and
                # universal newlines, followed by the actual json.loads(str).
                text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
                return json.loads(text)

            value = self._apply_original(parse_manifest, caller="manifest")
            return self._result(value, identity_origin="manifest-returned")
        if (set(frame) != {"format", "scope", "root"}
                or frame["scope"] != state.registry.scope.wire()
                or type(frame["format"]) is not str
                or frame["format"] not in {PREFIX + "value-describe.v1",
                                           PREFIX + "bootstrap-identity-describe.v1",
                                           PREFIX + "bootstrap-call-describe.v1"}):
            raise InvalidFrame("invalid or foreign transport frame")
        # A closed request on the existing export, not general exception field
        # permission. Registry provenance is checked again against the actual
        # issued ref; passing a kind/path/metadata cannot manufacture origin.
        root = state.registry.reference(frame["root"])
        if frame["format"] == PREFIX + "bootstrap-identity-describe.v1":
            return state.registry.describe_bootstrap_identity(root)
        if frame["format"] == PREFIX + "bootstrap-call-describe.v1":
            return state.registry.describe_bootstrap_call(root)
        return state.registry.describe(root)

    def copy_data_dictionary(self, payload: str) -> str:
        """Create the original caller's fresh outer dict, retaining nested aliases."""
        frame = self._frame(payload, "value-copy-dictionary", {"source"})
        registry = self._current().registry
        source = registry.borrow(registry.reference(frame["source"]), dict)
        return self._result(dict(source), set_items=True)

    def release_values(self, payload: str) -> None:
        """Validate a whole release batch before dropping its transport holds."""
        frame = self._frame(payload, "values-release", {"refs"})
        if type(frame["refs"]) is not list:
            raise InvalidFrame("invalid reference release list")
        registry = self._current().registry
        registry.release([registry.reference(ref) for ref in frame["refs"]])

    def append_data_item(self, payload: str) -> None:
        """Use the permitted original list; other aliases observe the append."""
        frame = self._frame(payload, "value-append", {"target", "item"})
        registry = self._current().registry
        target, item = (registry.reference(frame[k]) for k in ("target", "item"))
        registry.borrow(target, list)
        if not registry._permissions[target.object_id][0]:
            raise InvalidReference("list mutation was not enabled for this DATA node")
        self._apply_original(lambda: registry.append(target, item))

    def set_data_item(self, payload: str) -> None:
        """Use the permitted original dictionary without normalizing its errors."""
        frame = self._frame(payload, "value-set", {"target", "key", "item"})
        registry = self._current().registry
        target, key, item = (
            registry.reference(frame[k]) for k in ("target", "key", "item")
        )
        # Validate transport permissions before the original dictionary operation.
        registry.borrow(target, dict)
        if not registry._permissions[target.object_id][1]:
            raise InvalidReference(
                "dictionary mutation was not enabled for this DATA node"
            )
        self._apply_original(lambda: registry.set_item(target, key, item))
