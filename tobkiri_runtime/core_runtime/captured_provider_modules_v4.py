"""Private Python packages backed only by captured Host Provider bytes."""

from __future__ import annotations

import importlib
import builtins
from contextlib import contextmanager
import hashlib
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
import importlib.util
from pathlib import Path, PurePosixPath
import sys
import threading
from types import ModuleType
from typing import Any, Iterator

from tobkiri_host.artifact_materialization import MaterializedPackArtifact
from tobkiri_host.errors import AuthorizationError

_CAPTURES: dict[str, CapturedProviderModules] = {}
_CAPTURE_LOCK = threading.RLock()


class CapturedProviderModules(MetaPathFinder, Loader):
    """Keep relative imports inside one immutable, verified artifact capture.

    Empty package paths prevent filesystem lookup. A private finder also rejects
    undeclared names before another finder can supply them. Equal captures reuse
    one namespace. Accepted captures survive failed subsequent loads because an
    already returned factory can still import helpers lazily.
    """

    def __init__(
        self,
        pack_root: Path,
        captured: MaterializedPackArtifact,
        *,
        artifact_digest: str,
        pack_id: str,
    ) -> None:
        self.namespace = self._namespace(pack_root, captured, artifact_digest)
        self._lock = threading.RLock()
        self._accepted = False
        self._pack_id = pack_id
        self._canonical_prefix = "ecosystem." + self._pack_id
        self._ecosystem = ModuleType("ecosystem")
        self._ecosystem.__path__ = []
        # (origin, immutable source or None for a synthetic package, package?)
        self._modules: dict[str, tuple[str, bytes | None, bool]] = {
            self.namespace: (str(pack_root), None, True)
        }
        self._entry = ""
        for item in captured.files:
            path = PurePosixPath(item.path)
            if path.suffix != ".py":
                continue
            package = path.name == "__init__.py"
            parts = path.parts[:-1] if package else (*path.parts[:-1], path.stem)
            if any(not part.isidentifier() for part in parts):
                if item.path == captured.implementation_path:
                    raise AuthorizationError("Host Provider module path is invalid")
                continue
            name = ".".join((self.namespace, *parts))
            existing = self._modules.get(name)
            if existing is not None and existing[1] is not None:
                raise AuthorizationError("Host Provider module path is ambiguous")
            self._modules[name] = (
                str(pack_root / item.path),
                bytes(item.content),
                package,
            )
            for length in range(1, len(parts)):
                parent = ".".join((self.namespace, *parts[:length]))
                self._modules.setdefault(
                    parent,
                    (str(pack_root.joinpath(*parts[:length])), None, True),
                )
            if item.path == captured.implementation_path:
                self._entry = name
        if not self._entry:
            raise AuthorizationError("Host Provider implementation is unavailable")
        for name, (_origin, _source, package) in self._modules.items():
            if not package and any(
                other.startswith(name + ".") for other in self._modules
            ):
                raise AuthorizationError("Host Provider module path is ambiguous")

    @staticmethod
    def _namespace(
        pack_root: Path, captured: MaterializedPackArtifact, artifact_digest: str
    ) -> str:
        identity = "\0".join(
            (str(pack_root), artifact_digest, captured.materialization_digest)
        )
        return "_tobkiri_host_provider_" + hashlib.sha256(identity.encode()).hexdigest()

    @classmethod
    def for_capture(
        cls,
        pack_root: Path,
        captured: MaterializedPackArtifact,
        *,
        artifact_digest: str,
        pack_id: str,
    ) -> CapturedProviderModules:
        """Reuse one import context for one exact artifact and materialization."""
        namespace = cls._namespace(pack_root, captured, artifact_digest)
        with _CAPTURE_LOCK:
            existing = _CAPTURES.get(namespace)
            if existing is None:
                existing = cls(
                    pack_root,
                    captured,
                    artifact_digest=artifact_digest,
                    pack_id=pack_id,
                )
                _CAPTURES[namespace] = existing
            return existing

    @contextmanager
    def loading(self) -> Iterator[CapturedProviderModules]:
        """Serialize this capture's load and clean up only unaccepted imports."""
        with self._lock:
            # A waiter can hold this context while an initial failed load
            # removes it and another caller registers a replacement.
            with _CAPTURE_LOCK:
                current = _CAPTURES.setdefault(self.namespace, self)
            if current is not self:
                with current.loading() as active:
                    yield active
                return
            try:
                yield self
            finally:
                if not self._accepted:
                    self.close()

    def accept(self) -> None:
        """Retain immutable helpers for a successfully validated live factory."""
        self._accepted = True

    def load(self) -> ModuleType:
        """Import the entrypoint and parents without consulting live Pack paths."""
        if self not in sys.meta_path:
            sys.meta_path.insert(0, self)
        return importlib.import_module(self._entry)

    def find_spec(
        self,
        fullname: str,
        path: Any = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        """Resolve only this capture's names, rejecting undeclared relatives."""
        del path, target
        if fullname != self.namespace and not fullname.startswith(self.namespace + "."):
            return None
        entry = self._modules.get(fullname)
        if entry is None:
            raise ModuleNotFoundError(
                "Host Provider helper is not declared", name=fullname
            )
        origin, _source, package = entry
        spec = importlib.util.spec_from_loader(
            fullname, self, origin=origin, is_package=package
        )
        if spec is not None:
            spec.has_location = True
            if package:
                spec.submodule_search_locations = []
        return spec

    def create_module(self, spec: ModuleSpec) -> ModuleType | None:
        """Use Python's normal module allocation and import locking."""
        del spec
        return None

    def exec_module(self, module: ModuleType) -> None:
        """Execute only source bytes copied during digest verification."""
        origin, source, _package = self._modules[module.__name__]
        module.__dict__["__builtins__"] = {
            **vars(builtins),
            "__import__": self._import,
        }
        if source is not None:
            exec(compile(source, origin, "exec", dont_inherit=True), module.__dict__)

    def _import(
        self,
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] | list[str] | None = (),
        level: int = 0,
    ) -> Any:
        """Bind canonical imports of this Pack to its captured private package."""
        if level == 0 and (
            name == self._canonical_prefix
            or name.startswith(self._canonical_prefix + ".")
        ):
            private_name = self.namespace + name[len(self._canonical_prefix) :]
            value = builtins.__import__(private_name, globals, locals, fromlist, 0)
            if fromlist:
                return value
            # Python's plain import returns the top-level package. Preserve
            # that shape locally without publishing canonical sys.modules keys.
            setattr(self._ecosystem, self._pack_id, sys.modules[self.namespace])
            return self._ecosystem
        if level == 0 and name == "ecosystem" and self._pack_id in (fromlist or ()):
            if tuple(fromlist or ()) != (self._pack_id,):
                raise ImportError("mixed Host Provider package imports are unsupported")
            setattr(
                self._ecosystem, self._pack_id, importlib.import_module(self.namespace)
            )
            return self._ecosystem
        return builtins.__import__(name, globals, locals, fromlist, level)

    def close(self) -> None:
        """Remove only this load's private modules and import finder."""
        while self in sys.meta_path:
            sys.meta_path.remove(self)
        for name in tuple(sys.modules):
            if name == self.namespace or name.startswith(self.namespace + "."):
                sys.modules.pop(name, None)
        with _CAPTURE_LOCK:
            if _CAPTURES.get(self.namespace) is self:
                _CAPTURES.pop(self.namespace)
