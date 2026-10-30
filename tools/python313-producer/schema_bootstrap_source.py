"""Import-free captured-source execution and a closed two-module bootstrap.

Only the private schema experiment uses this pre-loader. It supplies the same
authenticated source bytes before resource/metadata tooling can import urllib.
There is no filesystem reader, module reload or fallback to another source.
"""


def compile_source(raw: bytes, origin: str) -> object:
    """Compile the actual selected source, as the ordinary captured loader does."""
    return compile(raw, origin, "exec", dont_inherit=True)


def execute_module(loader: object, module: object) -> None:
    """Share the existing loader's source execution and module bookkeeping."""
    module.__file__ = loader.finder.root + "/" + loader.source
    loader.finder.loaded.append(loader.name)
    exec(loader.get_code(loader.name), module.__dict__)


class BootstrapLoader:
    """Minimal ordinary import protocol over two already admitted sources."""

    def __init__(self, finder: object, name: str, source: str, package: bool):
        self.finder, self.name, self.source, self.package = (
            finder, name, source, package,
        )

    def create_module(self, spec: object) -> None:
        return None

    def get_source(self, fullname: str) -> str:
        if fullname != self.name:
            raise ImportError(fullname)
        # This optional inspection operation is not used during bootstrap.
        from importlib.util import decode_source
        return decode_source(self.finder.sources[self.source])

    def get_code(self, fullname: str) -> object:
        if fullname != self.name:
            raise ImportError(fullname)
        return compile_source(self.finder.sources[self.source],
                              self.finder.root + "/" + self.source)

    def is_package(self, fullname: str) -> bool:
        if fullname != self.name:
            raise ImportError(fullname)
        return self.package

    def exec_module(self, module: object) -> None:
        execute_module(self, module)


class BootstrapFinder:
    """Resolve exactly urllib and urllib.parse; all other names are untouched."""

    def __init__(self, files: dict, root: str):
        if (type(files) is not dict or type(root) is not str
                or not root.startswith("/") or root.endswith("/")):
            raise ValueError("explicit captured source map/root required")
        self.root, self.loaded = root, []
        self.sources = {}
        for name in ("urllib/__init__.py", "urllib/parse.py"):
            raw = files[name]
            if type(raw) is not bytes or len(raw) > 4 * 1024 * 1024:
                raise ValueError("bounded authenticated urllib sources required")
            self.sources[name] = raw

    def find_spec(self, fullname: str, path: object = None,
                  target: object = None) -> object:
        if fullname not in ("urllib", "urllib.parse"):
            return None
        if target is not None:
            raise ImportError("bootstrap module reload is not supported")
        from _frozen_importlib import ModuleSpec
        package = fullname == "urllib"
        source = "urllib/__init__.py" if package else "urllib/parse.py"
        loader = BootstrapLoader(self, fullname, source, package)
        spec = ModuleSpec(fullname, loader, origin=self.root + "/" + source,
                          is_package=package)
        spec.has_location = True
        if package:
            spec.submodule_search_locations = [self.root + "/urllib"]
        return spec


def install(files: dict, root: str) -> BootstrapFinder:
    """Bind the two sources only when neither target nor parent is loaded."""
    import sys
    if any(name in sys.modules for name in ("urllib", "urllib.parse")):
        raise ValueError("urllib bootstrap requires absent module and parent")
    finder = BootstrapFinder(files, root)
    sys.meta_path.insert(0, finder)
    return finder


def finish(bootstrap: BootstrapFinder, ordinary: object) -> None:
    """Verify the ordinary finder has the same sources before retiring this one."""
    import sys
    if (ordinary.root != bootstrap.root
            or any(ordinary.tree.files[name] != raw
                   for name, raw in bootstrap.sources.items())
            or not sys.meta_path or sys.meta_path[0] is not ordinary
            or sum(finder is bootstrap for finder in sys.meta_path) != 1):
        raise ValueError("ordinary captured source handover differs")
    for name in ("urllib", "urllib.parse"):
        module = sys.modules.get(name)
        loader = getattr(module, "__loader__", None)
        if (not isinstance(loader, BootstrapLoader) or loader.finder is not bootstrap
                or module.__spec__.loader is not loader
                or module.__file__ != bootstrap.root + "/" + loader.source):
            raise ValueError("actual bootstrap module owner differs")
    sys.meta_path.remove(bootstrap)
