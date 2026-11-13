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
from .persistence_view import VIEW as _PERSISTENCE_VIEW
from .value_graph import UnsupportedValue

_SERVICE = "ecosystem.defaultspack.domain.runtime_v4.service"
_SERVICE_SOURCE = "ecosystem/defaultspack/domain/runtime_v4/service.py"
_ARTIFACT = "tobkiri_protocol.platform_artifact"
_ARTIFACT_SOURCE = "tobkiri_protocol/platform_artifact.py"
_RESOLUTION = "ecosystem.defaultspack.domain.profile_resolution"
_RESOLUTION_SOURCE = "ecosystem/defaultspack/domain/profile_resolution.py"
_PERSISTENCE = "tobkiri_protocol.secure_persistence"
_PERSISTENCE_EXPORTS = ("SecureDirectory", "SecurePersistenceError")


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
    def __init__(self, spec: Any, bridge: PathBridge, anchor: NativePath | None,
                 source_owner=None, catalog_capture=None):
        # This is the actual finder's existing spec/source DATA. It never
        # selects a host resource; the NativeAnchor is independently supplied.
        if (type(spec) is not ModuleSpec or type(spec.origin) is not str
                or not spec.origin or spec.has_location is not True or spec.loader is None):
            raise UnsupportedValue("original module origin is not represented")
        self.original, self.spec = spec.loader, spec
        self.origin = spec.origin
        self.bridge, self.anchor = bridge, anchor
        # The actual finder that produced this spec, not a name-selected owner.
        # This only narrows an unavailable dependency; it grants no operation.
        self.source_owner = source_owner
        self.source_tree = getattr(self.original, "_source_tree", None)
        self.catalog_capture = catalog_capture

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
        view = (None if self.anchor is None else
                _PathlibView(self.bridge, self.origin, self.anchor))
        original_import = builtins.__import__

        def owns_import(name: str, source: str, namespace: Any) -> bool:
            owner, capture = self.source_owner, self.catalog_capture
            return (
                capture is not None and owner is not None
                and owner is capture[0] and type(owner) is capture[1]
                and type(self.original) is capture[2] and self.source_tree is capture[3]
                and self.spec.name == name and namespace is attributes
                and sys.modules.get(name) is module
                and attributes.get("__spec__") is self.spec
                and attributes.get("__loader__") is self.original
                and self.spec.loader is self.original
                and type(attributes.get("__file__")) is str
                and attributes["__file__"] == self.origin
                and type(self.spec.origin) is str and self.spec.origin == self.origin
                and any(f is owner for f in sys.meta_path)
                and getattr(owner, "sealed", False) is True
                and getattr(self.original, "finder", None) is owner
                and self.source_tree is not None
                and getattr(owner, "tree", None) is self.source_tree
                and getattr(self.original, "_source_tree", None) is self.source_tree
                and getattr(self.original, "source", None) == source
                and getattr(self.original, "name", None) == name
                and type(getattr(owner, "root", None)) is str
                and self.origin == owner.root + "/" + source
            )

        def scoped_import(name, globals=None, locals=None, fromlist=(), level=0):
            if (level == 0 and name == "os" and self.spec.name == _ARTIFACT
                    and self.catalog_capture is not None):
                if fromlist or not owns_import(_ARTIFACT, _ARTIFACT_SOURCE, globals):
                    raise UnsupportedValue("artifact source owner is not bound")
                return self.bridge.os
            if (level == 0 and name == _ARTIFACT
                    and self.spec.name == _RESOLUTION and self.catalog_capture is not None):
                if (type(fromlist) is not tuple or fromlist != ("verify_platform_artifact",)
                        or not owns_import(_RESOLUTION, _RESOLUTION_SOURCE, globals)):
                    raise UnsupportedValue("resolver verifier source owner is not bound")
                original = original_import(name, globals, locals, fromlist, level)
                verifier = getattr(original, "verify_platform_artifact", None)
                if not callable(verifier):
                    raise UnsupportedValue("original verifier is not bound")
                facade = ModuleType(name)

                def verify_platform_artifact(*args, **kwargs):
                    result = verifier(*args, **kwargs)
                    if self.bridge._resolving_catalog:
                        if (self.bridge._resolver_verified_artifact is not None
                                or not self.bridge._resolver_header_closed
                                or result is not self.bridge._resolver_shell_leaf):
                            raise UnsupportedValue("original verifier result or header is not bound")
                        self.bridge._resolver_verified_artifact = result
                    return result

                facade.verify_platform_artifact = verify_platform_artifact
                return facade
            if level == 0 and name == _PERSISTENCE:
                if (type(fromlist) is not tuple or fromlist != _PERSISTENCE_EXPORTS
                        or self.bridge._catalog_root is None
                        or not owns_import(_SERVICE, _SERVICE_SOURCE, globals)):
                    raise UnsupportedValue("catalog Host dependency owner is not bound")
                # The guarded original import executes in this actual owner's
                # globals. No persistence module is imported or registered.
                return _PERSISTENCE_VIEW
            if level == 0 and name == "pathlib":
                if view is None:
                    raise UnsupportedValue("source-only module has no pathlib binding")
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

    def __init__(self, bridge: PathBridge, anchors: dict[str, NativePath],
                 *, _catalog_capture=None):
        if (type(anchors) is not dict or not anchors
                or any(type(path) is not NativePath or path._bridge is not bridge
                       for path in anchors.values())):
            raise UnsupportedValue("invalid original module anchor bindings")
        self.bridge = bridge
        self.anchors = dict(anchors)
        self.finders: tuple[Any, ...] = ()
        self.installed = False
        if _catalog_capture is not None and (
            type(_catalog_capture) is not tuple or len(_catalog_capture) != 4
            or type(_catalog_capture[0]) is not _catalog_capture[1]
            or _catalog_capture[0].sealed is not True
            or _catalog_capture[0].tree is not _catalog_capture[3]
        ):
            raise UnsupportedValue("catalog capture is not the installed owner")
        self._catalog_capture = _catalog_capture

    def install(self) -> None:
        if (self.installed or any(name in sys.modules for name in self.anchors)
                or (self._catalog_capture is not None and _RESOLUTION in sys.modules)):
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
        self._catalog_capture = None

    def find_spec(self, fullname, path=None, target=None):
        anchor = self.anchors.get(fullname)
        if anchor is None and not (fullname == _RESOLUTION and self._catalog_capture is not None):
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
            capture = self._catalog_capture
            if fullname in {_SERVICE, _ARTIFACT, _RESOLUTION} and capture is not None and (
                finder is not capture[0] or type(finder) is not capture[1]
                or type(spec.loader) is not capture[2]
                or finder.tree is not capture[3]
            ):
                raise UnsupportedValue("catalog source responder is not selected")
            spec.loader = _ModuleLoader(spec, self.bridge, anchor, finder, capture)
            return spec
        raise ModuleNotFoundError(fullname)
