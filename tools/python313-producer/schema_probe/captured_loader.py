"""Standard Python module/resource/metadata IO over a closed immutable wheel tree.

This adapter supplies only already authenticated DATA. It never resolves an OS
path, writes a file, fetches a missing dependency, or substitutes module code.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
from collections.abc import Mapping
from importlib import metadata
from importlib.resources.abc import Traversable, TraversableResources
import io
from pathlib import PurePosixPath
import re
import sys
from types import MappingProxyType
from weakref import ref

MAX_FILES = 4096
MAX_TREE = 64 * 1024 * 1024
MAX_READ = 4 * 1024 * 1024
MAX_PATH_BYTES = 4096
MAX_DEPTH = 256


def parts(value: str | PurePosixPath) -> tuple[str, ...]:
    """Accept only canonical paths within this captured tree."""
    if not isinstance(value, (str, PurePosixPath)):
        raise TypeError("captured resource name must be text")
    name = str(value)
    if name == "":
        return ()
    if len(name.encode("utf-8")) > MAX_PATH_BYTES or name.count("/") >= MAX_DEPTH:
        raise ValueError("captured resource path metadata exceeds its bound")
    if (
        "\\" in name
        or ":" in name
        or "\0" in name
        or any(p in ("", ".", "..") for p in name.split("/"))
    ):
        raise ValueError("resource path leaves its captured namespace")
    return tuple(name.split("/"))


class Tree:
    """Retain every file and directory index within the original input bound."""

    def __init__(self, files: dict[str, bytes] | tuple, *, compressed: bool = False):
        if type(compressed) is not bool:
            raise ValueError("explicit captured storage mode required")
        storage = _CompressedFiles(files) if compressed else files
        if (not compressed and type(files) is not dict) or len(storage) > MAX_FILES:
            raise ValueError("captured resource file bound exceeded")
        copied, directories, size = {}, {"": set()}, 0
        for name in storage:
            path = parts(name)
            data = None if compressed else files[name]
            length = (
                storage.raw_length(name)
                if compressed
                else len(data)
                if type(data) is bytes
                else 0
            )
            if (
                not path
                or (not compressed and type(data) is not bytes)
                or length > MAX_READ
            ):
                raise ValueError("invalid or oversized captured resource")
            size += len(name.encode("utf-8")) + length + 256
            prefix_bytes = 0
            # Charge every full parent-prefix string before constructing indexes.
            for index, component in enumerate(path):
                component_bytes = len(component.encode("utf-8"))
                size += prefix_bytes + component_bytes + 256
                if size > MAX_TREE:
                    raise ValueError(
                        "captured tree and full index exceed the input bound"
                    )
                prefix_bytes += component_bytes + (1 if index else 0)
            if size > MAX_TREE:
                raise ValueError("captured tree and index exceed the input bound")
            if not compressed:
                copied[name] = data
            for index in range(len(path)):
                parent = "/".join(path[:index])
                directories.setdefault(parent, set()).add(path[index])
            for index in range(1, len(path)):
                directories.setdefault("/".join(path[:index]), set())
        if set(storage) & directories.keys():
            raise ValueError("captured file/directory collision")
        self.compressed_storage_bytes = storage.accounted_bytes if compressed else 0
        # Retain the old full raw capture admission as well as compressed bytes.
        # This does not subtract decoded/compile/stream overlap from the bound.
        size += self.compressed_storage_bytes
        if size > MAX_TREE:
            raise ValueError("captured storage and expansion exceed the input bound")
        self.files = storage if compressed else MappingProxyType(copied)
        self.directories = MappingProxyType(
            {k: tuple(sorted(v)) for k, v in directories.items()}
        )
        self.accounted_bytes = size
        self.reads = 0
        self.bytes_read = 0

    def path(self, name: str = "") -> "ResourcePath":
        return ResourcePath(self, parts(name))


class _CompressedFiles(Mapping):
    """Immutable manifest index; each value read verifies one bounded expansion.

    Rows are (original path, codec, stored bytes, raw length, raw SHA-256).
    Membership, iteration, length and raw-length queries never decode a member.
    Expanded values and code objects are deliberately never cached here.
    """

    def __init__(self, rows: tuple):
        import hashlib
        import types
        import zlib

        if (
            type(rows) is not tuple
            or len(rows) > MAX_FILES
            or not isinstance(zlib.decompressobj, types.BuiltinFunctionType)
        ):
            raise ValueError("immutable captured manifest and genuine zlib required")
        records, accounted = {}, 0
        for row in rows:
            if type(row) is not tuple or len(row) != 5:
                raise ValueError("invalid captured compressed record")
            name, codec, payload, length, digest = row
            if type(name) is not str:
                raise ValueError("invalid captured compressed path")
            path = parts(name)
            if (
                not path
                or type(name) is not str
                or name in records
                or type(codec) is not str
                or codec not in ("zlib", "identity")
                or type(payload) is not bytes
                or len(payload) > MAX_READ
                or type(length) is not int
                or not 0 <= length <= MAX_READ
                or type(digest) is not str
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
                or (codec == "identity" and len(payload) != length)
            ):
                raise ValueError("invalid captured compressed record")
            # One immutable row is shared with the generated manifest, not copied.
            accounted += len(payload) + len(name.encode("utf-8")) + len(digest) + 256
            if accounted > MAX_TREE:
                raise ValueError("captured compressed storage exceeds the input bound")
            records[name] = row
        self._records = MappingProxyType(records)
        self._decompress = zlib.decompressobj
        self._error = zlib.error
        self._sha256 = hashlib.sha256
        self.accounted_bytes = accounted

    def __len__(self):
        return len(self._records)

    def __iter__(self):
        return iter(self._records)

    def __contains__(self, name):
        return name in self._records

    def raw_length(self, name):
        return self._records[name][3]

    def __getitem__(self, name):
        return self._read_verified(name)[0]

    def _read_verified(self, name):
        """Return these immutable bytes and their actual computed digest.

        Every read restores/verifies again. The returned digest is never the
        expected manifest value; expanded bytes and results are never cached.
        """
        _path, codec, payload, length, digest = self._records[name]
        if codec == "identity":
            raw = payload
        else:
            decoder = self._decompress()
            try:
                # max_length is enforced inside genuine zlib, before full output
                # allocation. Never use an unbounded decompress()/flush() call.
                raw = decoder.decompress(payload, length + 1)
            except self._error as error:
                raise ValueError(
                    "corrupt captured compressed member: " + name
                ) from error
            if (
                len(raw) != length
                or not decoder.eof
                or decoder.unused_data
                or decoder.unconsumed_tail
            ):
                raise ValueError("incomplete or oversized captured member: " + name)
        actual = self._sha256(raw).hexdigest()
        if len(raw) != length or actual != digest:
            raise ValueError("captured member differs from its original hash: " + name)
        return raw, actual


class ResourcePath(Traversable):
    """A virtual Traversable, deliberately without an OS fspath conversion."""

    def __init__(self, tree: Tree, path: tuple[str, ...]):
        self.tree, self.path = tree, path

    @property
    def name(self) -> str:
        return self.path[-1] if self.path else ""

    @property
    def parent(self) -> "ResourcePath":
        return ResourcePath(self.tree, self.path[:-1])

    def __str__(self) -> str:
        return "verified-wheel:" + "/".join(self.path)

    def is_dir(self) -> bool:
        return "/".join(self.path) in self.tree.directories

    def is_file(self) -> bool:
        return "/".join(self.path) in self.tree.files

    def exists(self) -> bool:
        return self.is_file() or self.is_dir()

    def iterdir(self):
        key = "/".join(self.path)
        if self.is_file():
            raise NotADirectoryError(key)
        if key not in self.tree.directories:
            raise FileNotFoundError(key)
        return (
            ResourcePath(self.tree, self.path + (name,))
            for name in self.tree.directories[key]
        )

    def joinpath(self, *descendants):
        path = self.path
        length = sum(len(part.encode("utf-8")) for part in path) + max(0, len(path) - 1)
        for descendant in descendants:
            incoming = parts(descendant)
            length += sum(len(part.encode("utf-8")) for part in incoming)
            length += max(0, len(incoming) - 1) + int(bool(path and incoming))
            if len(path) + len(incoming) > MAX_DEPTH or length > MAX_PATH_BYTES:
                raise ValueError("joined resource path metadata exceeds its bound")
            path += incoming
        return ResourcePath(self.tree, path)

    def __truediv__(self, descendant):
        return self.joinpath(descendant)

    def _read_verified_bytes(self):
        """Read owned DATA once; retain actual digest and ordinary read checks."""
        key = "/".join(self.path)
        if self.is_dir():
            raise IsADirectoryError(key)
        if key not in self.tree.files:
            raise FileNotFoundError(key)
        if type(self.tree.files) is _CompressedFiles:
            raw, actual = self.tree.files._read_verified(key)
        else:
            import hashlib

            raw = self.tree.files[key]
            actual = hashlib.sha256(raw).hexdigest()
        self.tree.reads += 1
        self.tree.bytes_read += len(raw)
        return raw, actual

    def open(self, mode="r", *args, **kwargs):
        if mode not in ("r", "rb"):
            raise PermissionError("captured resources are read-only")
        key = "/".join(self.path)
        if self.is_dir():
            raise IsADirectoryError(key)
        if key not in self.tree.files:
            raise FileNotFoundError(key)
        data = self.tree.files[key]
        self.tree.reads += 1
        self.tree.bytes_read += len(data)
        stream = io.BytesIO(data)
        if mode == "rb":
            if args or kwargs:
                raise ValueError("binary resource open does not accept text options")
            return stream
        return io.TextIOWrapper(stream, *args, **kwargs)


class Reader(TraversableResources):
    """Expose the standard package resource protocol over an exact directory."""

    def __init__(self, path: ResourcePath):
        self.path = path

    def files(self) -> ResourcePath:
        return self.path


class Loader(importlib.abc.InspectLoader):
    """Execute the original captured source, retaining package/relative imports."""

    def __init__(self, finder: "Finder", name: str, source: str, package: bool):
        self.finder, self.name, self.source, self.package = (
            finder,
            name,
            source,
            package,
        )
        self._source_tree = finder.tree
        self._executed_module = None

    def create_module(self, spec):
        return None

    def get_source(self, fullname):
        if fullname != self.name:
            raise ImportError(fullname)
        return importlib.util.decode_source(self._source_tree.files[self.source])

    def get_code(self, fullname):
        if fullname != self.name:
            raise ImportError(fullname)
        return compile(
            self._source_tree.files[self.source],
            self.finder.root + "/" + self.source,
            "exec",
            dont_inherit=True,
        )

    def is_package(self, fullname):
        if fullname != self.name:
            raise ImportError(fullname)
        return self.package

    def exec_module(self, module):
        module.__file__ = self.finder.root + "/" + self.source
        self.finder.loaded.append(self.name)
        exec(self.get_code(self.name), module.__dict__)
        self._executed_module = ref(module)

    def _require_executed_owner(self, module):
        """Require the actual completed module and the same captured source tree."""
        if (
            self._executed_module is None
            or self._executed_module() is not module
            or sys.modules.get(self.name) is not module
            or module.__spec__ is None
            or module.__spec__.loader is not self
            or module.__loader__ is not self
            or not any(finder is self.finder for finder in sys.meta_path)
            or self.finder.tree is not self._source_tree
            or self.source not in self._source_tree.files
            or module.__file__ != self.finder.root + "/" + self.source
        ):
            raise ImportError("captured schema module owner is unavailable")

    def _tobkiri_schema_resources(self, module):
        """Return only fixed sibling schemas; never a filesystem capability."""
        self._require_executed_owner(module)
        if not self.name.endswith(".validation") or not self.source.endswith("/validation.py"):
            raise ImportError("captured schema module owner is unavailable")
        path = self._source_tree.path(self.source.rsplit("/", 1)[0] + "/schemas")
        if not path.is_dir():
            raise FileNotFoundError("captured package schemas are missing")
        return path

    def _tobkiri_bundle_defaults(self, module):
        """Return only the immutable bundle member of the actual App adapter."""
        self._require_executed_owner(module)
        if self.name != "bundle_catalog_adapter" or self.source != "bundle_catalog_adapter.py":
            raise ImportError("captured bundle module owner is unavailable")
        path = self._source_tree.path("bundled-defaults")
        if not path.is_dir() or not (path / "index.json").is_file():
            raise FileNotFoundError("captured immutable bundle defaults are missing")
        return path

    def get_resource_reader(self, fullname):
        if fullname != self.name or not self.package:
            return None
        return Reader(self._source_tree.path(self.source.rsplit("/", 1)[0]))


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


class Finder(importlib.abc.MetaPathFinder):
    """Serve only captured Python modules and captured distribution metadata."""

    def __init__(
        self,
        files: dict[str, bytes] | tuple,
        distributions: dict[str, str],
        root: str,
        namespaces: tuple[str, ...] = (),
        *,
        compressed: bool = False,
    ):
        self.tree, self.root = Tree(files, compressed=compressed), root
        self.distributions = MappingProxyType(dict(distributions))
        self.loaded = []
        self.metadata_queries = 0
        self.sealed = False
        if type(namespaces) is not tuple or len(set(namespaces)) != len(namespaces):
            raise ValueError("explicit unique captured namespace packages required")
        for name in namespaces:
            if (
                type(name) is not str
                or not name
                or any(not part.isidentifier() for part in name.split("."))
            ):
                raise ValueError("invalid captured namespace package")
            relative = name.replace(".", "/")
            if (
                relative not in self.tree.directories
                or relative + "/__init__.py" in self.tree.files
                or relative + ".py" in self.tree.files
            ):
                raise ValueError("captured namespace conflicts with original modules")
        self.namespaces = frozenset(namespaces)
        for name, directory in self.distributions.items():
            if name != normalized(name) or not self.tree.path(directory).is_dir():
                raise ValueError("invalid captured distribution metadata directory")

    def find_spec(self, fullname, path=None, target=None):
        relative = fullname.replace(".", "/")
        for source, package in (
            (relative + "/__init__.py", True),
            (relative + ".py", False),
        ):
            if source in self.tree.files:
                loader = Loader(self, fullname, source, package)
                spec = importlib.machinery.ModuleSpec(
                    fullname,
                    loader,
                    origin=self.root + "/" + source,
                    is_package=package,
                )
                spec.has_location = True
                if package:
                    spec.submodule_search_locations = [self.root + "/" + relative]
                return spec
        if fullname in self.namespaces:
            # A genuine namespace has no initializer or invented source body.
            spec = importlib.machinery.ModuleSpec(fullname, None, is_package=True)
            spec.submodule_search_locations = [self.root + "/" + relative]
            return spec
        if self.sealed and (
            importlib.machinery.BuiltinImporter.find_spec(fullname) is None
            and importlib.machinery.FrozenImporter.find_spec(fullname) is None
        ):
            raise ModuleNotFoundError(
                "lazy module is outside the captured source closure: " + fullname,
                name=fullname,
            )
        return None

    def seal(self):
        """Refuse unknown late source before any filesystem import fallback."""
        self.sealed = True

    def find_distributions(self, context=None):
        context = context or metadata.DistributionFinder.Context()
        if self.root not in context.path:
            return iter(())
        self.metadata_queries += 1
        selected = normalized(context.name) if context.name else None
        return (
            metadata.PathDistribution(self.tree.path(directory))
            for name, directory in self.distributions.items()
            if selected is None or name == selected
        )


def install(
    files: dict[str, bytes] | tuple,
    distributions: dict[str, str],
    root: str,
    namespaces: tuple[str, ...] = (),
    *,
    compressed: bool = False,
) -> Finder:
    """Bind one closed resource/module namespace before ordinary imports."""
    if not root.startswith("/") or root.endswith("/"):
        raise ValueError("explicit virtual Python search root required")
    finder = Finder(files, distributions, root, namespaces, compressed=compressed)
    sys.meta_path.insert(0, finder)
    return finder
