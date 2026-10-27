"""Read-only input DATA for isolated Setup UI computation.

The native User inspection must retain the immutable source and enforce its
original owner, byte, time and cancellation budgets. This adapter supplies input
DATA, not an OS filesystem API, Profile approval, Session, or custody proof.
Its reader accepts only a captured file ordinal and bounded byte range. Never
bind it to an arbitrary-path filesystem API or expose it as a Pack operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath
from types import MappingProxyType
from typing import Callable, Iterable, Iterator

from .errors import ProtocolError

MAX_CAPTURE_FILES = 4096
MAX_CAPTURE_METADATA_BYTES = 1024 * 1024
MAX_CAPTURE_READ_BYTES = 4 * 1024 * 1024
CAPTURE_CHUNK_BYTES = 64 * 1024


def _namespace_retention(path: str) -> int:
    """Admit complete path/ancestor retention before constructing PurePaths."""
    if len(path) > MAX_CAPTURE_METADATA_BYTES:
        raise ProtocolError("captured input metadata exceeds its bound")
    try:
        retained = len(path.encode("utf-8")) + 256
        prefix_bytes = 0
        # Include all components conservatively, even repeated ancestors. This
        # bounds inventory AND untrusted query paths before quadratic expansion.
        for depth, part in enumerate(path.split("/"), 1):
            prefix_bytes += len(part.encode("utf-8")) + 1
            retained += 256 + prefix_bytes + 8 * depth
            if retained > MAX_CAPTURE_METADATA_BYTES:
                raise ProtocolError("captured input metadata exceeds its bound")
        return retained
    except UnicodeEncodeError as exc:
        raise ProtocolError("captured input path is not Unicode scalar text") from exc


@dataclass(frozen=True, slots=True)
class CapturedFile:
    """Description of one immutable input; the description grants no authority."""

    path: str
    size: int


class CapturedInputs:
    """Closed namespace over one native-retained, read-only input capture.

    File contents are streamed, so an inspected application artifact need not
    be copied into UI memory. This does not change Pack/catalog size limits.
    The native binding must independently validate the capture and ordinal on
    every read; these Python objects are untrusted presentation DATA.
    """

    def __init__(
        self,
        files: Iterable[CapturedFile],
        read_range: Callable[[int, int, int], bytes],
    ) -> None:
        entries: dict[PurePosixPath, tuple[int, CapturedFile]] = {}
        directories = {PurePosixPath("/")}
        metadata_bytes = 0
        for ordinal, item in enumerate(files):
            if ordinal >= MAX_CAPTURE_FILES or type(item) is not CapturedFile:
                raise ProtocolError("captured input inventory is invalid or too large")
            if (
                type(item.path) is not str
                or not item.path
                or len(item.path) > MAX_CAPTURE_METADATA_BYTES
                or "\\" in item.path
                or "\0" in item.path
                or any(part in ("", ".", "..") for part in item.path.split("/"))
                or type(item.size) is not int
                or not 0 <= item.size < 2**63
            ):
                raise ProtocolError("captured input file is malformed")
            # PurePath ancestors retain parts tuples and possibly full text.
            # Admit their complete worst-case retention BEFORE constructing
            # any parents; counting only the leaf permits quadratic growth.
            metadata_bytes += _namespace_retention(item.path)
            if metadata_bytes > MAX_CAPTURE_METADATA_BYTES:
                raise ProtocolError("captured input metadata exceeds its bound")
            relative = PurePosixPath(item.path)
            if relative.is_absolute():
                raise ProtocolError("captured input path must be relative")
            path = PurePosixPath("/") / relative
            if path in entries or path in directories:
                raise ProtocolError("captured input path collides")
            if any(parent in entries for parent in path.parents):
                raise ProtocolError("captured input file is used as a directory")
            entries[path] = (ordinal, item)
            directories.update(path.parents)
        self._entries = MappingProxyType(entries)
        self._files = tuple(item for _, item in entries.values())
        self._directories = frozenset(directories)
        self._read_range = read_range

    def root(self) -> CapturedPath:
        """Return this capture's root, which cannot be converted to an OS path."""
        return CapturedPath(self, PurePosixPath("/"))


@dataclass(frozen=True, slots=True)
class CapturedPath:
    """Path operations restricted to the captured namespace, with no OS fallback."""

    _capture: CapturedInputs
    _path: PurePosixPath

    def __post_init__(self) -> None:
        _namespace_retention(str(self._path))

    def __fspath__(self) -> str:
        """Refuse accidental use by pathlib, os.open, or a native filesystem API."""
        raise TypeError("captured input is not an operating-system path")

    def __str__(self) -> str:
        return str(self._path)

    def __truediv__(self, relative: str | PurePath) -> CapturedPath:
        if isinstance(relative, PurePath):
            if relative.is_absolute() or relative.drive:
                raise ProtocolError("captured input path escapes its namespace")
            text = relative.as_posix()
        elif type(relative) is str:
            text = relative
        else:
            raise ProtocolError("captured input path is malformed")
        _namespace_retention(text)
        _namespace_retention(str(self._path) + "/" + text)
        path = PurePosixPath(text)
        if path.is_absolute() or ".." in path.parts or "\\" in str(path) or "\0" in str(path):
            raise ProtocolError("captured input path escapes its namespace")
        return CapturedPath(self._capture, self._path / path)

    @property
    def name(self) -> str:
        """Return the same lexical file name as a normal Path."""
        return self._path.name

    @property
    def suffix(self) -> str:
        """Expose the lexical suffix used by the original schema loader."""
        return self._path.suffix

    def __lt__(self, other: object) -> bool:
        if type(other) is not CapturedPath or other._capture is not self._capture:
            return NotImplemented
        return self._path < other._path

    @property
    def parent(self) -> CapturedPath:
        """Return a lexical parent within this capture."""
        return CapturedPath(self._capture, self._path.parent)

    @property
    def parents(self) -> tuple[CapturedPath, ...]:
        """Return lexical ancestors without looking at the local filesystem."""
        _namespace_retention(str(self._path))
        return tuple(CapturedPath(self._capture, path) for path in self._path.parents)

    def relative_to(self, other: CapturedPath) -> PurePosixPath:
        """Reject paths from another capture even when their text matches."""
        if type(other) is not CapturedPath or other._capture is not self._capture:
            raise ValueError("captured inputs have different owners")
        return self._path.relative_to(other._path)

    def resolve(self, *, strict: bool = False) -> CapturedPath:
        """Check this immutable namespace; no symlink or OS resolution is possible."""
        if strict and not (self.is_file() or self.is_dir()):
            raise FileNotFoundError("captured input does not exist")
        return self

    def is_symlink(self) -> bool:
        """Captures contain only regular files and derived directories."""
        return False

    def is_file(self) -> bool:
        """Test membership in this capture's regular-file inventory."""
        return self._path in self._capture._entries

    def is_dir(self) -> bool:
        """Test membership in this capture's derived directory inventory."""
        return self._path in self._capture._directories

    def iterdir(self) -> Iterator[CapturedPath]:
        """Enumerate only direct children already present in the capture."""
        if not self.is_dir():
            raise NotADirectoryError("captured input is not a directory")
        for path in self._capture._directories:
            if path != self._path and path.parent == self._path:
                yield CapturedPath(self._capture, path)
        for path in self._capture._entries:
            if path.parent == self._path:
                yield CapturedPath(self._capture, path)

    def glob(self, pattern: str) -> Iterator[CapturedPath]:
        """Match direct captured children, without recursive discovery."""
        if type(pattern) is not str or "/" in pattern or "\\" in pattern or "**" in pattern:
            raise ProtocolError("captured input glob is not a direct-child pattern")
        for path in self.iterdir():
            if path._path.match(pattern):
                yield path

    def open_binary(self) -> CapturedReader:
        """Open a bounded cursor for one inventoried file, never a writable handle."""
        entry = self._capture._entries.get(self._path)
        if entry is None:
            raise FileNotFoundError("captured input file is absent")
        ordinal, item = entry
        return CapturedReader(self._capture, ordinal, item.size)

    def read_bytes(self) -> bytes:
        """Read a small complete input; large artifacts must use streaming reads."""
        with self.open_binary() as reader:
            return reader.read()


class CapturedReader:
    """Small read/seek cursor over a single native-retained immutable input."""

    def __init__(self, capture: CapturedInputs, ordinal: int, size: int) -> None:
        if (
            type(ordinal) is not int
            or not 0 <= ordinal < len(capture._files)
            or type(size) is not int
            or size != capture._files[ordinal].size
        ):
            raise ProtocolError("captured input cursor is outside its inventory")
        self._capture = capture
        self._ordinal = ordinal
        self._size = size
        self._position = 0
        self._closed = False

    @property
    def size(self) -> int:
        """Return the fixed captured length; a cursor cannot enlarge it."""
        return self._size

    def __enter__(self) -> CapturedReader:
        return self

    def __exit__(self, *_: object) -> None:
        self._closed = True

    def read(self, count: int = -1) -> bytes:
        """Request at most 64 KiB per native read and 4 MiB per returned buffer."""
        if self._closed or type(count) is not int or count < -1:
            raise ProtocolError("captured input read is invalid")
        remaining = self.size - self._position
        count = remaining if count == -1 else min(count, remaining)
        if count > MAX_CAPTURE_READ_BYTES:
            raise ProtocolError("captured input read exceeds its buffer bound")
        parts = []
        position = self._position
        while count:
            requested = min(count, CAPTURE_CHUNK_BYTES)
            part = self._capture._read_range(self._ordinal, position, requested)
            if type(part) is not bytes or len(part) != requested:
                raise ProtocolError("captured input range changed or was truncated")
            parts.append(part)
            position += requested
            count -= requested
        self._position = position
        return b"".join(parts)

    def seek(self, offset: int) -> int:
        """Seek only within this file, as required for bounded PE header reads."""
        if self._closed or type(offset) is not int or not 0 <= offset <= self.size:
            raise ProtocolError("captured input seek is outside the file")
        self._position = offset
        return offset
