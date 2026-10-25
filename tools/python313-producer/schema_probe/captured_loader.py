"""Standard Python module/resource/metadata IO over a closed immutable wheel tree.

This adapter supplies only already authenticated DATA. It never resolves an OS
path, writes a file, fetches a missing dependency, or substitutes module code.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
from importlib import metadata
from importlib.resources.abc import Traversable, TraversableResources
import io
from pathlib import PurePosixPath
import re
import sys
from types import MappingProxyType

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

    def __init__(self, files: dict[str, bytes]):
        if type(files) is not dict or len(files) > MAX_FILES:
            raise ValueError("captured resource file bound exceeded")
        copied, directories, size = {}, {"": set()}, 0
        for name, data in files.items():
            path = parts(name)
            if not path or type(data) is not bytes or len(data) > MAX_READ:
                raise ValueError("invalid or oversized captured resource")
            size += len(name.encode("utf-8")) + len(data) + 256
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
            copied[name] = data
            for index in range(len(path)):
                parent = "/".join(path[:index])
                directories.setdefault(parent, set()).add(path[index])
            for index in range(1, len(path)):
                directories.setdefault("/".join(path[:index]), set())
        if copied.keys() & directories.keys():
            raise ValueError("captured file/directory collision")
        self.files = MappingProxyType(copied)
        self.directories = MappingProxyType(
            {k: tuple(sorted(v)) for k, v in directories.items()}
        )
        self.accounted_bytes = size
        self.reads = 0
        self.bytes_read = 0

    def path(self, name: str = "") -> "ResourcePath":
        return ResourcePath(self, parts(name))


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

    def create_module(self, spec):
        return None

    def get_source(self, fullname):
        if fullname != self.name:
            raise ImportError(fullname)
        return importlib.util.decode_source(self.finder.tree.files[self.source])

    def get_code(self, fullname):
        if fullname != self.name:
            raise ImportError(fullname)
        return compile(
            self.finder.tree.files[self.source],
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

    def get_resource_reader(self, fullname):
        if fullname != self.name or not self.package:
            return None
        return Reader(self.finder.tree.path(self.source.rsplit("/", 1)[0]))


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


class Finder(importlib.abc.MetaPathFinder):
    """Serve only captured Python modules and captured distribution metadata."""

    def __init__(
        self, files: dict[str, bytes], distributions: dict[str, str], root: str
    ):
        self.tree, self.root = Tree(files), root
        self.distributions = MappingProxyType(dict(distributions))
        self.loaded = []
        self.metadata_queries = 0
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
        return None

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
    files: dict[str, bytes], distributions: dict[str, str], root: str
) -> Finder:
    """Bind one closed resource/module namespace before ordinary imports."""
    if not root.startswith("/") or root.endswith("/"):
        raise ValueError("explicit virtual Python search root required")
    finder = Finder(files, distributions, root)
    sys.meta_path.insert(0, finder)
    return finder
