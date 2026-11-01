"""Bind original module Path imports before their original code executes.

This is a guest-side adapter over already admitted native inputs. Neither a
module name, lexical path nor this importer authorizes native filesystem IO.
Only the fixed native inspector can validate the actual target and operation.
"""

from __future__ import annotations

import builtins
from importlib.machinery import ModuleSpec
from pathlib import PurePath as OriginalPurePath, PurePosixPath, PureWindowsPath
import sys
from types import ModuleType
from typing import Any

from .native_path import NativePath, PathBridge
from .value_graph import UnsupportedValue


class _PathlibView(ModuleType):
    """One module's scoped constructor; no ambient filesystem Path fallback."""

    def __init__(self, bridge: PathBridge, original_file: str, anchor: NativePath):
        super().__init__("pathlib")
        self.Path = bridge.module_path(original_file, anchor)
        self.PurePosixPath = PurePosixPath
        self.PureWindowsPath = PureWindowsPath
        # PurePath's constructor uses the native flavor, while its base-type
        # checks still accept BOTH pure flavors (including captured namespace
        # paths). Replacing the base class with PureWindowsPath would reject
        # the original CapturedPath's valid PurePosixPath arguments on Windows.
        class PurePathType(type):
            def __call__(cls, *paths):
                return bridge._pure(*(
                    path._pure if type(path) is NativePath else path
                    for path in paths
                ))

            def __instancecheck__(cls, value):
                return type(value) is NativePath or isinstance(value, OriginalPurePath)

            def __subclasscheck__(cls, value):
                return value is NativePath or issubclass(value, OriginalPurePath)

        class PurePath(OriginalPurePath, metaclass=PurePathType):
            """Pure lexical constructor/type facade; no native binding or IO."""

        self.PurePath = PurePath

    def __getattr__(self, name: str) -> Any:
        raise UnsupportedValue(f"native pathlib attribute is not represented: {name}")


class _ModuleLoader:
    def __init__(self, spec: Any, bridge: PathBridge, anchor: NativePath):
        # This is the actual finder's existing spec/source DATA. It never
        # selects a host resource; the NativeAnchor is independently supplied.
        if (type(spec) is not ModuleSpec or type(spec.origin) is not str
                or not spec.origin or spec.has_location is not True or spec.loader is None):
            raise UnsupportedValue("original module origin is not represented")
        self.original, self.spec = spec.loader, spec
        self.origin = spec.origin
        self.bridge, self.anchor = bridge, anchor

    def create_module(self, spec):
        create = getattr(self.original, "create_module", None)
        return None if create is None else create(spec)

    def exec_module(self, module: ModuleType) -> None:
        if type(module) is not ModuleType:
            raise UnsupportedValue("original module execution owner changed")
        attributes = module.__dict__
        if (attributes.get("__spec__") is not self.spec
                or attributes.get("__loader__") is not self or self.spec.loader is not self
                or type(attributes.get("__file__")) is not str
                or attributes["__file__"] != self.origin
                or type(self.spec.origin) is not str or self.spec.origin != self.origin):
            raise UnsupportedValue("original module execution owner changed")
        execute = getattr(self.original, "exec_module", None)
        if not callable(execute):
            raise UnsupportedValue("original module loader cannot execute its code")
        view = _PathlibView(self.bridge, self.origin, self.anchor)
        original_import = builtins.__import__

        def scoped_import(name, globals=None, locals=None, fromlist=(), level=0):
            if level == 0 and name == "pathlib":
                return view
            if level == 0 and name.startswith("pathlib."):
                raise UnsupportedValue("native pathlib submodule is not represented")
            return original_import(name, globals, locals, fromlist, level)

        namespace = dict(vars(builtins))
        namespace["__import__"] = scoped_import
        module.__dict__["__builtins__"] = namespace
        # Install scoped imports BEFORE eager constants execute, then let the
        # actual owner perform its one original execution and success witness.
        # Its captured __file__/resource tree stay intact. Path(__file__) maps
        # that known origin to the already supplied anchor, without OS fallback.
        module.__loader__ = self.original
        self.spec.loader = self.original
        execute(module)
        if (attributes.get("__spec__") is not self.spec
                or attributes.get("__loader__") is not self.original
                or self.spec.loader is not self.original
                or type(attributes.get("__file__")) is not str
                or attributes["__file__"] != self.origin
                or type(self.spec.origin) is not str or self.spec.origin != self.origin
                or attributes.get("__builtins__") is not namespace):
            raise UnsupportedValue("original module execution owner changed")


class ModulePathBindings:
    """Install exactly the immutable source index's required module anchors.

    The caller validates the fixed inventory/source-index coherence first.
    Installation must precede every original module import; a late binding is
    rejected instead of accepting constants produced by another Path context.
    The finder stays alive for the interpreter's lazy original imports.
    """

    def __init__(self, bridge: PathBridge, anchors: dict[str, NativePath]):
        if (type(anchors) is not dict or not anchors
                or any(type(path) is not NativePath or path._bridge is not bridge
                       for path in anchors.values())):
            raise UnsupportedValue("invalid original module anchor bindings")
        self.bridge = bridge
        self.anchors = dict(anchors)
        self.finders: tuple[Any, ...] = ()
        self.installed = False

    def install(self) -> None:
        if self.installed or any(name in sys.modules for name in self.anchors):
            raise UnsupportedValue("original module already imported before native binding")
        self.finders = tuple(sys.meta_path)
        sys.meta_path.insert(0, self)
        self.installed = True

    def close(self) -> None:
        """Remove only this hook, preserving other module/resource owners.

        Existing NativePath objects are not revoked here. Actual Host phase,
        owner and Store retirement/IO Join separately close their IO scope.
        """
        if self.installed:
            sys.meta_path[:] = [finder for finder in sys.meta_path if finder is not self]
            self.installed = False

    def find_spec(self, fullname, path=None, target=None):
        anchor = self.anchors.get(fullname)
        if anchor is None:
            return None
        for finder in self.finders:
            find = getattr(finder, "find_spec", None)
            if find is None:
                continue
            spec = find(fullname, path, target)
            if spec is None:
                continue
            if spec.loader is None:
                raise UnsupportedValue("original bound module has no code loader")
            spec.loader = _ModuleLoader(spec, self.bridge, anchor)
            return spec
        raise ModuleNotFoundError(fullname)
