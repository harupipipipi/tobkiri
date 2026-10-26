"""Materialize the bounded native-exception graph ABI inside the guest.

The graph is data only.  Exception classes are selected from fixed tags and
trusted protocol/bootstrap references; payload text never names a module or
class.  One store is scoped to one invocation and owns its graph identity cache.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import ordered_json_v2
from tobkiri_protocol.errors import (
    CanonicalizationError,
    ProtocolError,
    SchemaValidationError,
)


_FORMAT = "tobkiri.native-exception/1"
_U32_MAX = (1 << 32) - 1
_SAFE_INTEGER_MAX = (1 << 53) - 1
_FRAME_KEYS = frozenset({"format", "graph_id", "root", "nodes"})
_NODE_KEYS = frozenset(
    {"kind", "args", "fields", "cause", "context", "suppress_context"}
)
_OS_ERROR_KEYS = frozenset(
    {"errno", "strerror", "filename", "filename2"}
)

_FIXED_EXCEPTION_CLASSES: dict[str, type[BaseException]] = {
    "value-error": ValueError,
    "runtime-error": RuntimeError,
    "protocol-error": ProtocolError,
    "canonicalization-error": CanonicalizationError,
    "schema-validation-error": SchemaValidationError,
    "json-decode-error": json.JSONDecodeError,
    "os-error": OSError,
    "file-not-found-error": FileNotFoundError,
    "permission-error": PermissionError,
    "not-a-directory-error": NotADirectoryError,
    "is-a-directory-error": IsADirectoryError,
    "file-exists-error": FileExistsError,
    "interrupted-error": InterruptedError,
    "blocking-io-error": BlockingIOError,
    "timeout-error": TimeoutError,
    "connection-error": ConnectionError,
    "broken-pipe-error": BrokenPipeError,
    "connection-aborted-error": ConnectionAbortedError,
    "connection-refused-error": ConnectionRefusedError,
    "connection-reset-error": ConnectionResetError,
    "process-lookup-error": ProcessLookupError,
    "child-process-error": ChildProcessError,
}


@dataclass(frozen=True)
class ErrorClasses:
    """Optional original domain exception classes supplied by trusted bootstrap."""

    default_profile_v4_error: type[BaseException] | None = None
    bundle_integrity_error: type[BaseException] | None = None
    profile_resolution_denied: type[BaseException] | None = None
    external_pack_catalog_denied: type[BaseException] | None = None
    pack_boundary_error: type[BaseException] | None = None
    profile_content_projection_error: type[BaseException] | None = None
    invalid_artifact_error: type[BaseException] | None = None


class UnsupportedNativeException(Exception):
    """The native exception payload is outside this materializer's ABI."""


@dataclass(frozen=True)
class _CachedGraph:
    code: str
    message: str
    details: str
    root: BaseException
    nodes: tuple[BaseException, ...]


class ExceptionGraphStore:
    """Materialize native exception graphs and preserve per-invocation identity."""

    def __init__(self, error_classes: ErrorClasses | None = None) -> None:
        """Create an invocation-scoped store with optional trusted class references."""
        if error_classes is not None and type(error_classes) is not ErrorClasses:
            raise UnsupportedNativeException("invalid trusted exception class bundle")
        self._error_classes = error_classes or ErrorClasses()
        self._domain_classes = self._validated_domain_classes(self._error_classes)
        self._graphs: dict[int, _CachedGraph] = {}

    def materialize(self, code: str, message: str, details: str) -> BaseException:
        """Decode one bounded payload and return its actual root exception object."""
        if (
            type(code) is not str
            or type(message) is not str
            or type(details) is not str
        ):
            raise UnsupportedNativeException(
                "native exception payload must use exact strings"
            )
        if self._total_utf8_size(code, message, details) > ordered_json_v2.MAX_BYTES:
            raise UnsupportedNativeException("native exception payload exceeds 4 MiB")
        try:
            frame = ordered_json_v2.loads(details)
        except ordered_json_v2.OrderedJSONError as exc:
            raise UnsupportedNativeException(
                "native exception details are invalid JSON"
            ) from exc

        if type(frame) is not dict or frame.keys() != _FRAME_KEYS:
            raise UnsupportedNativeException(
                "native exception frame has an invalid shape"
            )
        graph_id = frame["graph_id"]
        if not self._is_u32(graph_id):
            raise UnsupportedNativeException("native exception graph ID is invalid")

        cached = self._graphs.get(graph_id)
        if cached is not None:
            if (cached.code, cached.message, cached.details) == (
                code,
                message,
                details,
            ):
                return cached.root
            raise UnsupportedNativeException("native exception graph ID was reused")

        if frame["format"] != _FORMAT:
            raise UnsupportedNativeException(
                "native exception frame format is unsupported"
            )
        nodes_payload = frame["nodes"]
        if type(nodes_payload) is not list or not nodes_payload:
            raise UnsupportedNativeException(
                "native exception graph must contain nodes"
            )
        root_index = frame["root"]
        if not self._is_node_index(root_index, len(nodes_payload)):
            raise UnsupportedNativeException("native exception root index is invalid")

        specifications: list[tuple[str, list[Any], dict[str, Any], Any, Any, bool]] = []
        for node in nodes_payload:
            specifications.append(self._validate_node(node, len(nodes_payload)))
        root_kind = specifications[root_index][0]
        if code != root_kind:
            raise UnsupportedNativeException(
                "native exception code differs from root kind"
            )

        objects: list[BaseException] = []
        references: list[tuple[int | None, int | None, bool]] = []
        for kind, args, fields, cause, context, suppress_context in specifications:
            objects.append(self._create_exception(kind, args, fields))
            references.append((cause, context, suppress_context))
        if message != str(objects[root_index]):
            raise UnsupportedNativeException(
                "native exception message differs from root display"
            )

        # Link every exception before setting suppression flags.  BaseException's
        # descriptors bypass any user-defined attribute hooks on domain classes.
        for node_object, (cause, context, _suppress_context) in zip(
            objects, references
        ):
            BaseException.__setattr__(
                node_object, "__cause__", None if cause is None else objects[cause]
            )
            BaseException.__setattr__(
                node_object,
                "__context__",
                None if context is None else objects[context],
            )
        for node_object, (_cause, _context, suppress_context) in zip(
            objects, references
        ):
            BaseException.__setattr__(
                node_object, "__suppress_context__", suppress_context
            )

        materialized = tuple(objects)
        root = materialized[root_index]
        self._graphs[graph_id] = _CachedGraph(
            code=code,
            message=message,
            details=details,
            root=root,
            nodes=materialized,
        )
        return root

    @staticmethod
    def _total_utf8_size(*parts: str) -> int:
        size = 0
        for part in parts:
            try:
                size += len(part.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise UnsupportedNativeException(
                    "native exception payload contains invalid Unicode"
                ) from exc
            if size > ordered_json_v2.MAX_BYTES:
                return size
        return size

    @staticmethod
    def _is_u32(value: Any) -> bool:
        return type(value) is int and 0 <= value <= _U32_MAX

    @classmethod
    def _is_node_index(cls, value: Any, node_count: int) -> bool:
        return cls._is_u32(value) and value < node_count

    @staticmethod
    def _is_safe_integer(value: Any) -> bool:
        return type(value) is int and -_SAFE_INTEGER_MAX <= value <= _SAFE_INTEGER_MAX

    @classmethod
    def _validate_node(
        cls, node: Any, node_count: int
    ) -> tuple[str, list[Any], dict[str, Any], int | None, int | None, bool]:
        if type(node) is not dict or node.keys() != _NODE_KEYS:
            raise UnsupportedNativeException(
                "native exception node has an invalid shape"
            )
        kind = node["kind"]
        args = node["args"]
        fields = node["fields"]
        cause = node["cause"]
        context = node["context"]
        suppress_context = node["suppress_context"]
        if type(kind) is not str or type(args) is not list or type(fields) is not dict:
            raise UnsupportedNativeException("native exception node has invalid types")
        if cause is not None and not cls._is_node_index(cause, node_count):
            raise UnsupportedNativeException("native exception cause index is invalid")
        if context is not None and not cls._is_node_index(context, node_count):
            raise UnsupportedNativeException(
                "native exception context index is invalid"
            )
        if type(suppress_context) is not bool:
            raise UnsupportedNativeException(
                "native exception suppression flag is invalid"
            )
        if kind not in _FIXED_EXCEPTION_CLASSES and kind not in _DOMAIN_CLASS_SPECS:
            raise UnsupportedNativeException("native exception kind is unsupported")
        return kind, args, fields, cause, context, suppress_context

    @staticmethod
    def _class_mro(exception_class: type[BaseException]) -> tuple[type[Any], ...]:
        return type.__getattribute__(exception_class, "__mro__")

    @classmethod
    def _validated_domain_classes(
        cls, error_classes: ErrorClasses
    ) -> dict[str, type[BaseException]]:
        supplied: dict[str, type[BaseException] | None] = {
            "default-profile-v4-error": error_classes.default_profile_v4_error,
            "bundle-integrity-error": error_classes.bundle_integrity_error,
            "profile-resolution-denied": error_classes.profile_resolution_denied,
            "external-pack-catalog-denied": error_classes.external_pack_catalog_denied,
            "pack-boundary-error": error_classes.pack_boundary_error,
            "profile-content-projection-error": (
                error_classes.profile_content_projection_error
            ),
            "invalid-artifact-error": error_classes.invalid_artifact_error,
        }
        result: dict[str, type[BaseException]] = {}
        for kind, exception_class in supplied.items():
            if exception_class is None:
                continue
            expected_name, expected_base, forbid_value_or_runtime = _DOMAIN_CLASS_SPECS[
                kind
            ]
            if type(exception_class) is not type:
                raise UnsupportedNativeException("trusted domain class is not a class")
            mro = cls._class_mro(exception_class)
            name = type.__getattribute__(exception_class, "__name__")
            if (
                name != expected_name
                or BaseException not in mro
                or expected_base not in mro
                or (
                    forbid_value_or_runtime
                    and (ValueError in mro or RuntimeError in mro)
                )
            ):
                raise UnsupportedNativeException(
                    "trusted domain class has wrong inheritance"
                )
            result[kind] = exception_class

        base_class = result.get("default-profile-v4-error")
        if base_class is not None:
            for child_kind in ("bundle-integrity-error", "profile-resolution-denied"):
                child_class = result.get(child_kind)
                if (
                    child_class is not None
                    and base_class not in cls._class_mro(child_class)
                ):
                    raise UnsupportedNativeException(
                        "trusted domain class relationship is inconsistent"
                    )
        return result

    def _create_exception(
        self, kind: str, args: list[Any], fields: dict[str, Any]
    ) -> BaseException:
        if kind == "json-decode-error":
            return self._create_json_decode_error(args, fields)
        if kind in _OS_ERROR_KINDS:
            return self._create_os_error(kind, args, fields)
        exception_class = _FIXED_EXCEPTION_CLASSES.get(kind)
        if exception_class is None:
            exception_class = self._domain_classes.get(kind)
        if exception_class is None:
            raise UnsupportedNativeException("native exception class is unavailable")
        if kind == "schema-validation-error":
            return self._create_schema_validation_error(exception_class, args, fields)
        if (
            len(args) != 1
            or type(args[0]) is not str
            or fields.keys() != frozenset()
        ):
            raise UnsupportedNativeException(
                "native exception arguments are unsupported"
            )
        try:
            instance = BaseException.__new__(exception_class)
        except TypeError as exc:
            raise UnsupportedNativeException(
                "native exception class cannot be allocated"
            ) from exc
        BaseException.__init__(instance, args[0])
        if type(instance) is not exception_class or instance.args != (args[0],):
            raise UnsupportedNativeException(
                "native exception allocation is inconsistent"
            )
        return instance

    @staticmethod
    def _create_schema_validation_error(
        exception_class: type[BaseException],
        args: list[Any],
        fields: dict[str, Any],
    ) -> BaseException:
        if (
            len(args) != 1
            or type(args[0]) is not str
            or fields.keys() != frozenset({"diagnostics"})
        ):
            raise UnsupportedNativeException("schema validation error is malformed")
        diagnostics = fields["diagnostics"]
        if type(diagnostics) is not list or any(
            type(item) is not str for item in diagnostics
        ):
            raise UnsupportedNativeException("schema diagnostics are malformed")
        try:
            instance = BaseException.__new__(exception_class)
        except TypeError as exc:
            raise UnsupportedNativeException(
                "schema error class cannot be allocated"
            ) from exc
        BaseException.__init__(instance, args[0])
        BaseException.__setattr__(instance, "diagnostics", tuple(diagnostics))
        if type(instance) is not exception_class or instance.args != (args[0],):
            raise UnsupportedNativeException("schema error allocation is inconsistent")
        return instance

    @staticmethod
    def _create_json_decode_error(
        args: list[Any], fields: dict[str, Any]
    ) -> BaseException:
        expected_fields = frozenset({"msg", "doc", "pos", "lineno", "colno"})
        if (
            len(args) != 1
            or type(args[0]) is not str
            or fields.keys() != expected_fields
        ):
            raise UnsupportedNativeException("JSON decode error is malformed")
        msg = fields["msg"]
        doc = fields["doc"]
        pos = fields["pos"]
        lineno = fields["lineno"]
        colno = fields["colno"]
        if (
            type(msg) is not str
            or type(doc) is not str
            or type(pos) is not int
            or type(lineno) is not int
            or type(colno) is not int
            or not 0 <= pos <= len(doc)
        ):
            raise UnsupportedNativeException(
                "JSON decode error fields are inconsistent"
            )
        instance = json.JSONDecodeError(msg, doc, pos)
        if (
            type(instance) is not json.JSONDecodeError
            or instance.args != (args[0],)
            or instance.msg != msg
            or instance.doc != doc
            or instance.pos != pos
            or instance.lineno != lineno
            or instance.colno != colno
        ):
            raise UnsupportedNativeException("JSON decode error derived fields differ")
        return instance

    @staticmethod
    def _create_os_error(
        kind: str, args: list[Any], fields: dict[str, Any]
    ) -> BaseException:
        exception_class = _FIXED_EXCEPTION_CLASSES[kind]
        if (
            len(args) != 2
            or not ExceptionGraphStore._is_safe_integer(args[0])
            or type(args[1]) is not str
            or fields.keys() != _OS_ERROR_KEYS
            or not ExceptionGraphStore._is_safe_integer(fields["errno"])
            or type(fields["strerror"]) is not str
            or fields["filename"] is not None
            or fields["filename2"] is not None
            or fields["errno"] != args[0]
            or fields["strerror"] != args[1]
        ):
            raise UnsupportedNativeException("path-free OS error payload is malformed")

        # OSError(errno, strerror) performs host-specific errno-to-subclass
        # translation.  Allocate through the exact allowlisted base instead;
        # leave filename slots NULL so their native display stays path-free.
        try:
            instance = OSError.__new__(exception_class)
        except TypeError as exc:
            raise UnsupportedNativeException(
                "OS error class cannot be allocated"
            ) from exc
        BaseException.__init__(instance, args[0], args[1])
        instance.errno = args[0]
        instance.strerror = args[1]

        expected_display = f"[Errno {args[0]}] {args[1]}"
        if (
            type(instance) is not exception_class
            or instance.args != (args[0], args[1])
            or instance.errno != args[0]
            or instance.strerror != args[1]
            or instance.filename is not None
            or instance.filename2 is not None
            or getattr(instance, "winerror", None) is not None
            or getattr(instance, "characters_written", None) is not None
            or str(instance) != expected_display
        ):
            raise UnsupportedNativeException("OS error allocation is inconsistent")
        return instance


_DOMAIN_CLASS_SPECS: dict[str, tuple[str, type[BaseException], bool]] = {
    "default-profile-v4-error": ("DefaultProfileV4Error", RuntimeError, False),
    "bundle-integrity-error": ("BundleIntegrityError", RuntimeError, False),
    "profile-resolution-denied": ("ProfileResolutionDenied", RuntimeError, False),
    "external-pack-catalog-denied": ("ExternalPackCatalogDenied", RuntimeError, False),
    "pack-boundary-error": ("PackBoundaryError", RuntimeError, False),
    "profile-content-projection-error": (
        "ProfileContentProjectionError",
        ValueError,
        False,
    ),
    "invalid-artifact-error": ("InvalidArtifactError", Exception, True),
}

_OS_ERROR_KINDS = frozenset(
    {
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
    }
)
