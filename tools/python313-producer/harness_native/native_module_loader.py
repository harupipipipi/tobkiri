"""Bind original module Path imports before their original code executes.

This is a guest-side adapter over already admitted native inputs. Neither a
module name, lexical path nor this importer authorizes native filesystem IO.
Only the fixed native inspector can validate the actual target and operation.
"""

from __future__ import annotations

import builtins
from pathlib import PurePath as OriginalPurePath, PurePosixPath, PureWindowsPath
import sys
from types import CodeType, ModuleType
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
    def __init__(self, original: Any, bridge: PathBridge, anchor: NativePath):
        self.original, self.bridge, self.anchor = original, bridge, anchor

    def create_module(self, spec):
        create = getattr(self.original, "create_module", None)
        return None if create is None else create(spec)

    def exec_module(self, module: ModuleType) -> None:
        # Load the unchanged original code from the component's authenticated
        # package. Do not delegate exec_module: it could execute constants
        # before these globals/constructors have been bound.
        get_code = getattr(self.original, "get_code", None)
        if get_code is None:
            raise UnsupportedValue("original module loader cannot expose its code")
        code = get_code(module.__name__)
        if type(code) is not CodeType:
            raise UnsupportedValue("original module code is unavailable")
        filename = str(self.anchor)
        module.__file__ = filename
        view = _PathlibView(self.bridge, filename, self.anchor)
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
        # Original functions/classes/constants are executed without AST/body
        # rewriting. This is not a new source selected by a transport frame.
        exec(code, module.__dict__)


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
        """Remove only this hook; never reset another module or native scope."""
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
            spec.loader = _ModuleLoader(spec.loader, self.bridge, anchor)
            return spec
        raise ModuleNotFoundError(fullname)
