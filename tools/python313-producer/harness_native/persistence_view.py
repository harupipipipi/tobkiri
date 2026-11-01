"""Private unavailable Host dependency for the fixed catalog-only slice.

This is an explicit dependency replacement, not the original persistence
module or an implementation of its IO. The source guard proves that neither
export is semantically used on the closed catalog success/failure paths.
"""

from types import ModuleType

from .value_graph import UnsupportedValue


# Exact original class body, checked against the captured source at assembly.
# This translated class belongs to THIS module. It is never an original-class
# identity witness, a method error, a registry record, or a Guest cause codec.
class SecurePersistenceError(OSError):
    """Raised when a persistence root or entry cannot be used safely."""


def _unbound(*_args, **_kwargs):
    raise UnsupportedValue("Host persistence is not bound in the catalog slice")


class _UnavailableDirectory:
    """No fields, source owner, native handle, callback, or successful operation."""

    __slots__ = ()
    __call__ = _unbound
    __getattribute__ = _unbound
    __setattr__ = _unbound
    __delattr__ = _unbound
    __bool__ = _unbound
    __iter__ = _unbound
    __next__ = _unbound
    __getitem__ = _unbound
    __setitem__ = _unbound
    __delitem__ = _unbound
    __enter__ = _unbound
    __exit__ = _unbound
    __str__ = _unbound
    __repr__ = _unbound
    __eq__ = _unbound
    __ne__ = _unbound
    __hash__ = _unbound


_DIRECTORY = _UnavailableDirectory()


class _PersistenceView(ModuleType):
    """Exactly two fixed from-import results; no ambient module forwarding."""

    def __getattribute__(self, name):
        if name == "SecureDirectory":
            return _DIRECTORY
        if name == "SecurePersistenceError":
            return SecurePersistenceError
        return _unbound()

    __setattr__ = _unbound
    __delattr__ = _unbound


# Never installed as tobkiri_protocol.secure_persistence in sys.modules.
# Ordinary component module/heap/fuel ownership covers these small objects.
VIEW = _PersistenceView(__name__)
