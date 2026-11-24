"""Bind private Native Pack operations to Host-composed ownership."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from tobkiri_protocol.secure_persistence import SecureDirectory


@dataclass(frozen=True)
class NativePackOwnerBinding:
    """One captured Host owner and policy path, never request-supplied authority."""

    owner_root: Path
    trust_store_path: Path | None
    _reader: SecureDirectory = field(repr=False, compare=False)

    @contextmanager
    def scope(self) -> Iterator[None]:
        """Pin the original owner and restore the caller's context on every exit."""
        from .bootstrap.profile_capture import runtime_user_data_scope

        with self._reader._root_descriptor():
            with runtime_user_data_scope(self.owner_root):
                yield


def capture_native_pack_owner_binding(
    session: Any,
    *,
    owner_root: Path | None = None,
    trust_store_path: Path | None = None,
) -> NativePackOwnerBinding | None:
    """Capture only existing trusted Host composition inputs.

    Production sessions provide their already-open Authority store. Host control
    sessions provide the owner captured by their upstream constructor. Other
    trusted compositions may explicitly supply an owner. No environment or
    request field is consulted here, and no Authority store is initialized.
    """
    from .authority.v4 import AuthorityStore
    from .pack_control_v4 import HostProfileControlSession

    selected = None
    authority = getattr(session, "owned_authority_store", None)
    if isinstance(authority, AuthorityStore):
        path = authority.path
        if path.name != "v4.sqlite3" or path.parent.name != "authority":
            raise ValueError("Native Pack Authority owner is invalid")
        selected = path.parent.parent
    elif isinstance(session, HostProfileControlSession):
        selected = session._user_data_root
    if owner_root is not None:
        supplied = Path(owner_root).absolute()
        if selected is not None and supplied != Path(selected).absolute():
            raise ValueError("Native Pack composition owners differ")
        selected = supplied
    if selected is None:
        return None
    selected = Path(selected).absolute()
    reader = SecureDirectory(selected, create=False)
    policy = Path(trust_store_path).absolute() if trust_store_path is not None else None
    if trust_store_path is not None and not Path(trust_store_path).is_absolute():
        raise ValueError("Native Pack policy path must be absolute")
    return NativePackOwnerBinding(reader.root, policy, reader)
