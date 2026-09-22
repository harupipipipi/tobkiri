"""Explicit host-to-runtime contract values.

The process environment may carry non-sensitive routing information, but it is
never a credential source.  The launcher may bind a signed contract for a
request (or point to one through the non-secret contract path); consumers only
read values through this module and receive an empty result when the contract
is absent, foreign, or malformed.
"""

from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, Mapping


_CONTRACT: ContextVar[Mapping[str, Any] | None] = ContextVar(
    "tobkiri_host_contract", default=None
)


@contextmanager
def bind_host_contract(contract: Mapping[str, Any]) -> Iterator[None]:
    """Bind an explicit host contract for the current request/task."""

    if not isinstance(contract, Mapping):
        raise ValueError("host contract must be an object")
    token = _CONTRACT.set(dict(contract))
    try:
        yield
    finally:
        _CONTRACT.reset(token)


def _load_contract_file() -> Mapping[str, Any] | None:
    """Load a launcher-owned contract file, if the host supplied its path."""

    raw_path = os.getenv("TOBKIRI_HOST_CONTRACT_PATH", "").strip()
    if not raw_path:
        return None
    path = Path(raw_path)
    try:
        configured_root = os.getenv("RUMI_USER_DATA", "").strip()
        user_data_root = (
            Path(configured_root)
            if configured_root
            else Path(__file__).resolve().parents[1] / "user_data"
        )
        expected = user_data_root / "host_contract.json"
        if path.absolute() != expected.absolute() or path.is_symlink():
            return None
        root_metadata = user_data_root.stat()
        if not stat.S_ISDIR(root_metadata.st_mode) or root_metadata.st_mode & 0o077:
            return None
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            return None
        getuid = getattr(os, "geteuid", None)
        if callable(getuid) and (
            root_metadata.st_uid != getuid() or metadata.st_uid != getuid()
        ):
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    if payload.get("schema_version") != "tobkiri.host-contract.v1":
        return None
    return payload


def host_contract_value(
    name: str,
    *,
    profile_id: str | None = None,
    provider_id: str | None = None,
) -> str:
    """Return one contract value after profile/provider binding checks."""

    contract = _CONTRACT.get() or _load_contract_file()
    if not isinstance(contract, Mapping):
        return ""
    expected_profile = str(profile_id or "").strip()
    if not expected_profile:
        try:
            from .profile_credentials import active_profile_id

            expected_profile = str(active_profile_id() or "").strip()
        except ImportError:
            expected_profile = ""
    contract_profile = str(contract.get("profile_id") or "").strip()
    if not contract_profile or (expected_profile and contract_profile != expected_profile):
        return ""
    expected_provider = str(provider_id or "").strip()
    contract_provider = str(contract.get("provider_id") or "").strip()
    if expected_provider and contract_provider != expected_provider:
        return ""
    values = contract.get("values")
    if not isinstance(values, Mapping):
        values = contract.get("credentials")
    if not isinstance(values, Mapping):
        return ""
    value = values.get(str(name or "").strip())
    return str(value or "").strip()


def host_contract_mapping() -> dict[str, Any]:
    """Return redacted metadata only; material is intentionally omitted."""

    contract = _CONTRACT.get() or _load_contract_file()
    if not isinstance(contract, Mapping):
        return {}
    return {
        "profile_id": str(contract.get("profile_id") or ""),
        "provider_id": str(contract.get("provider_id") or ""),
        "bound": isinstance(contract.get("values", contract.get("credentials")), Mapping),
    }
