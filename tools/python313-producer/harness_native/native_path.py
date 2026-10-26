"""Guest Path/OS adapters for the original nativeKit inspection reachpoints.

Only trusted bootstrap supplies the native port, real input bindings, host
flavor and disclosed lexical values. These wrappers confer no authority: the
native implementation must check actual owner/target/operation/phase/occurrence
and retain FDs/iterators through physical completion and invocation teardown.
No ambient filesystem fallback is provided. Actual native admission and module
preamble integration remain separate prerequisites for whole-source execution.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Any, Callable, Iterator

import ordered_json_v2 as wire

from .value_graph import Scope, UnsupportedValue

_PREFIX = "io.tobkiri.native-inspection-"


def _binding(value: Any) -> str:
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise UnsupportedValue("invalid issued native binding")
    return value


@dataclass(frozen=True)
class _Stat:
    st_mode: int
    st_dev: int
    st_ino: int
    st_size: int
    st_mtime_ns: int

    def __getattr__(self, name: str) -> Any:
        raise UnsupportedValue("native stat observation is not represented")


class PathBridge:
    """Invocation-local port supplied by trusted nativeKit bootstrap only.

    The port uses the existing funded fixed import, not a new account/budget.
    The native side owns binding admission and validates each full expression;
    parent/child derivation alone never enlarges an allowed target set. The
    optional cwd binding is a live operation target, never an inventory-time
    cwd snapshot. Module anchors must be actual fixed resource bindings.
    """

    def __init__(
        self, scope: Scope, flavor: str, inspect: Callable[[str], str], *,
        os_flags: dict[str, int], cwd_binding: str | None = None,
        decode_error: Callable[[Any], BaseException] | None = None,
    ) -> None:
        if (type(scope) is not Scope or type(flavor) is not str
                or flavor not in {"posix", "windows"}):
            raise UnsupportedValue("unsupported native Path context")
        if (type(os_flags) is not dict or "O_RDONLY" not in os_flags
                or set(os_flags) - {"O_RDONLY", "O_NOFOLLOW"}
                or any(type(v) is not int for v in os_flags.values())):
            raise UnsupportedValue("unsupported native OS flags")
        self.scope = scope
        self._pure = PurePosixPath if flavor == "posix" else PureWindowsPath
        self._inspect = inspect
        self._decode_error = decode_error
        self._cwd = None if cwd_binding is None else _binding(cwd_binding)
        self.os = NativeOS(self, dict(os_flags))

    def bind(self, binding: str, lexical: str) -> NativePath:
        """Wrap an already admitted native target and its exact disclosed value."""
        if type(lexical) is not str:
            raise UnsupportedValue("native path value is not represented")
        return NativePath(self, self._pure(lexical), _binding(binding), ())

    def path(self, value: str | NativePath = ".") -> NativePath:
        """Construct lexical DATA without stat/resolve/cwd or target admission."""
        if type(value) is NativePath:
            if value._bridge is not self:
                raise UnsupportedValue("foreign native Path context")
            return NativePath(self, value._pure, value._binding, value._steps)
        if type(value) is not str:
            raise UnsupportedValue("Path constructor input is not represented")
        return NativePath(self, self._pure(value), None, ())

    def module_path(
        self, original_file: str, anchor: NativePath,
    ) -> type:
        """Bind one pinned module's __file__ before its top-level execution.

        Original functions/classes remain unchanged. The native anchor and its
        fixed module/resource role must be admitted independently; matching a
        guest string here never supplies authority to an ordinary Pack.
        """
        if (type(original_file) is not str or type(anchor) is not NativePath
                or anchor._bridge is not self or anchor._binding is None):
            raise UnsupportedValue("invalid native module anchor")

        bridge = self

        class PathType(type):
            def __call__(cls, value: str | NativePath = ".") -> NativePath:
                if type(value) is str and value == original_file:
                    return bridge.path(anchor)
                return bridge.path(value)

            def __instancecheck__(cls, value: Any) -> bool:
                # Original delegated verification uses isinstance(value, Path).
                # This is only a concrete wrapper type check, never authority,
                # liveness or __class__ supplied by the object being inspected.
                return type(value) is NativePath

        class Path(metaclass=PathType):
            """Fixed module-local constructor/type facade, not an IO grant."""

        return Path

    def _request(self, target: dict[str, Any], operation: str, args: list) -> dict:
        # Do not catch native resource/cancellation/panic failures. Only the
        # explicitly returned closed domain DATA channel may be decoded below.
        response = self._inspect(wire.dumps({
            "format": _PREFIX + "request.v1", "scope": self.scope.wire(),
            "target": target, "operation": operation, "args": args,
        }))
        try:
            frame = wire.loads(response)
        except wire.OrderedJSONError:
            raise UnsupportedValue("invalid native inspection response") from None
        if (type(frame) is not dict
                or frame.get("format") != _PREFIX + "response.v1"
                or frame.get("scope") != self.scope.wire()):
            raise UnsupportedValue("invalid or foreign native inspection response")
        if set(frame) == {"format", "scope", "error"}:
            if self._decode_error is None:
                raise UnsupportedValue("native error disclosure is not connected")
            error = self._decode_error(frame["error"])
            if (not isinstance(error, Exception)
                    or isinstance(error, (MemoryError, RecursionError))):
                raise UnsupportedValue("native execution failures cannot be domain DATA")
            raise error
        if (set(frame) != {"format", "scope", "result"}
                or type(frame["result"]) is not dict):
            raise UnsupportedValue("invalid native inspection result")
        return frame["result"]

    def _value(self, row: dict, kind: str) -> Any:
        if row.get("kind") != kind:
            raise UnsupportedValue("native result kind does not match operation")
        if kind == "path":
            if set(row) != {"kind", "binding", "value"}:
                raise UnsupportedValue("invalid native path result")
            return self.bind(row["binding"], row["value"])
        if kind == "unit":
            if set(row) != {"kind"}:
                raise UnsupportedValue("invalid native unit result")
            return None
        if kind == "stat":
            fields = tuple(_Stat.__dataclass_fields__)
            if (set(row) != {"kind", *fields}
                    or any(type(row[name]) is not int for name in fields)):
                raise UnsupportedValue("invalid native stat observation")
            return _Stat(*(row[name] for name in fields))
        if set(row) != {"kind", "value"}:
            raise UnsupportedValue("invalid native scalar result")
        if kind == "bool" and type(row["value"]) is bool:
            return row["value"]
        if kind == "bytes" and type(row["value"]) is str:
            try:
                return base64.b64decode(row["value"], validate=True)
            except (ValueError, binascii.Error):
                raise UnsupportedValue("invalid native byte result") from None
        raise UnsupportedValue("native result value is not represented")


class NativePath:
    """Path DATA plus a retained native base and unevaluated lexical derivation."""

    def __init__(self, bridge: PathBridge, pure: Any, binding: str | None,
                 steps: tuple[tuple[str, str], ...]) -> None:
        self._bridge, self._pure = bridge, pure
        self._binding, self._steps = binding, steps

    def _derive(self, pure: Any, op: str, arg: str = "") -> NativePath:
        return NativePath(self._bridge, pure, self._binding, self._steps + ((op, arg),))

    def _target(self) -> dict:
        if self._binding is None:
            raise UnsupportedValue("unbound lexical Path has no native IO target")
        return {"binding": self._binding, "steps": [list(s) for s in self._steps]}

    def _effect(self, operation: str, kind: str, args: list | None = None) -> Any:
        return self._bridge._value(
            self._bridge._request(self._target(), operation, args or []), kind
        )

    @property
    def parts(self) -> tuple[str, ...]:
        return self._pure.parts

    @property
    def name(self) -> str:
        return self._pure.name

    @property
    def suffix(self) -> str:
        return self._pure.suffix

    @property
    def suffixes(self) -> list[str]:
        return self._pure.suffixes

    def with_name(self, name: str) -> NativePath:
        # Let the pinned guest PurePath validate and compute lexical DATA.
        # Keep the original binding; a sibling is not a new native grant.
        return self._derive(self._pure.with_name(name), "with-name", name)

    def with_suffix(self, suffix: str) -> NativePath:
        return self._derive(self._pure.with_suffix(suffix), "with-suffix", suffix)

    @property
    def parent(self) -> NativePath:
        return self._derive(self._pure.parent, "parent")

    @property
    def parents(self) -> tuple[NativePath, ...]:
        values = []
        current = self
        for _ in self._pure.parents:
            current = current.parent
            values.append(current)
        return tuple(values)

    def __truediv__(self, value: str | NativePath) -> NativePath:
        raw = str(value._pure) if type(value) is NativePath else value
        if type(raw) is not str:
            return NotImplemented
        # Preserve native PurePath drive/root behavior, including Windows
        # drive-relative and rooted joins; admission remains native at effect.
        return self._derive(self._pure / raw, "join", raw)

    def __str__(self) -> str:
        return str(self._pure)

    def __fspath__(self) -> str:
        return str(self._pure)

    def __eq__(self, other: Any) -> Any:
        value = other._pure if type(other) is NativePath else other
        return self._pure.__eq__(value)

    def __lt__(self, other: Any) -> Any:
        value = other._pure if type(other) is NativePath else other
        return self._pure.__lt__(value)

    def __le__(self, other: Any) -> Any:
        value = other._pure if type(other) is NativePath else other
        return self._pure.__le__(value)

    def __gt__(self, other: Any) -> Any:
        value = other._pure if type(other) is NativePath else other
        return self._pure.__gt__(value)

    def __ge__(self, other: Any) -> Any:
        value = other._pure if type(other) is NativePath else other
        return self._pure.__ge__(value)

    def __hash__(self) -> int:
        return hash(self._pure)

    def is_absolute(self) -> bool:
        return self._pure.is_absolute()

    def as_posix(self) -> str:
        return self._pure.as_posix()

    def relative_to(self, other: NativePath) -> NativePath:
        value = other._pure if type(other) is NativePath else other
        return self._bridge.path(str(self._pure.relative_to(value)))

    def absolute(self) -> NativePath:
        if self._pure.is_absolute():
            return self
        if self._binding is not None:
            return self._effect("absolute", "path")
        if self._bridge._cwd is None:
            raise UnsupportedValue("live native cwd binding is not connected")
        return self._bridge._value(self._bridge._request(
            {"binding": self._bridge._cwd, "steps": []},
            "absolute", [str(self._pure)],
        ), "path")

    def resolve(self, strict: bool = False) -> NativePath:
        return self._effect("resolve", "path", [strict])

    def is_symlink(self) -> bool:
        return self._effect("is-symlink", "bool")

    def is_file(self) -> bool:
        return self._effect("is-file", "bool")

    def is_dir(self) -> bool:
        return self._effect("is-dir", "bool")

    def read_bytes(self) -> bytes:
        return self._effect("read-bytes", "bytes")

    def lstat(self) -> _Stat:
        return self._effect("lstat", "stat")

    def _iterator(self, operation: str, args: list) -> Iterator[NativePath]:
        row = self._bridge._request(self._target(), operation, args)
        if set(row) != {"kind", "binding"} or row["kind"] != "iterator":
            raise UnsupportedValue("invalid native iterator result")
        target = {"binding": _binding(row["binding"]), "steps": []}

        return self._iterate(target)

    def _iterate(self, target: dict) -> Iterator[NativePath]:
        try:
            while True:
                row = self._bridge._request(target, "iterator-next", [])
                if row == {"kind": "end"}:
                    return
                yield self._bridge._value(row, "path")
        finally:
            self._bridge._value(self._bridge._request(
                target, "iterator-close", [],
            ), "unit")

    def iterdir(self) -> Iterator[NativePath]:
        # Pinned Python 3.13 Path.iterdir obtains its scandir entries at the
        # method call, before returning its generator. NativeKit must finish
        # that original enumeration here, not defer it to iterator-next.
        return self._iterator("iterdir", [])

    def glob(self, pattern: str) -> Iterator[NativePath]:
        # No eager enumeration: the original consumer drives this generator.
        yield from self._iterator("glob", [pattern])

    def rglob(self, pattern: str) -> Iterator[NativePath]:
        # Generator creation does no inspection. Native iteration starts at the
        # first next(), stays in original order, and closes on completion/error.
        yield from self._iterator("rglob", [pattern])


class _Descriptor:
    def __init__(self, bridge: PathBridge, binding: str) -> None:
        self.bridge, self.binding, self.closed = bridge, binding, False


class NativeOS:
    """Module-local OS observations; exact reads, no coalescing or EOF caching."""

    def __init__(self, bridge: PathBridge, flags: dict[str, int]) -> None:
        self._bridge, self._flags = bridge, flags

    def __getattr__(self, name: str) -> int:
        if name in self._flags:
            return self._flags[name]
        if name == "O_NOFOLLOW":
            raise AttributeError(name)
        raise UnsupportedValue("native OS observation is not represented")

    def open(self, path: NativePath, flags: int) -> _Descriptor:
        if type(path) is not NativePath or path._bridge is not self._bridge:
            raise UnsupportedValue("native open requires its actual Path context")
        row = self._bridge._request(path._target(), "open", [flags])
        if set(row) != {"kind", "binding"} or row["kind"] != "fd":
            raise UnsupportedValue("invalid native descriptor result")
        return _Descriptor(self._bridge, _binding(row["binding"]))

    def _call(self, fd: _Descriptor, operation: str, kind: str,
              args: list | None = None) -> Any:
        if (type(fd) is not _Descriptor or fd.bridge is not self._bridge or fd.closed):
            raise UnsupportedValue("closed or foreign native descriptor")
        return self._bridge._value(self._bridge._request(
            {"binding": fd.binding, "steps": []}, operation, args or [],
        ), kind)

    def fstat(self, fd: _Descriptor) -> _Stat:
        return self._call(fd, "fstat", "stat")

    def read(self, fd: _Descriptor, size: int) -> bytes:
        return self._call(fd, "read", "bytes", [size])

    def close(self, fd: _Descriptor) -> None:
        try:
            self._call(fd, "close", "unit")
        finally:
            if type(fd) is _Descriptor and fd.bridge is self._bridge:
                fd.closed = True
