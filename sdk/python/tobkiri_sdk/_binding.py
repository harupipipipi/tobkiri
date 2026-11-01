"""Explicit generated-import adapter contract; guest effects always use WIT."""

from typing import Any, Protocol


class GuestError(RuntimeError):
    """A core import rejected or could not execute a guest operation."""


class CoreImports(Protocol):
    """An adapter around bindings generated from the selected Pack world."""

    def invoke(self, interface: str, operation: str, **arguments: Any) -> Any:
        """Invoke one generated core import without changing the principal."""

    def enum_value(self, interface: str, name: str, case: str) -> Any:
        """Resolve a type-only value from this world's generated interface."""


_imports: CoreImports | None = None


def bind(imports: CoreImports) -> None:
    """Bind generated WIT imports once during guest initialization."""
    global _imports
    if _imports is not None:
        raise GuestError("core imports already bound")
    _imports = imports


def invoke(interface: str, operation: str, **arguments: Any) -> Any:
    """Forward an import; absent bindings fail closed instead of using the OS."""
    if _imports is None:
        raise GuestError("generated WIT core imports are not bound")
    return _imports.invoke(interface, operation, **arguments)


def enum_value(interface: str, name: str, case: str) -> Any:
    """Use the selected world's generated enum; never infer an ABI version."""
    if _imports is None:
        raise GuestError("generated WIT core imports are not bound")
    factory = getattr(_imports, "enum_value", None)
    if not callable(factory):
        raise GuestError("generated WIT type adapter is not available")
    return factory(interface, name, case)
