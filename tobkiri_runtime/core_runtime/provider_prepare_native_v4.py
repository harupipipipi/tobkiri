"""Host-private, synchronous borrowing for keyless Provider preparation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
import threading
from types import MappingProxyType
from typing import Any

from tobkiri_host.admission import ResourceAmount
from tobkiri_host.models import RequestContext
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.secure_persistence import SecureDirectory, SecurePersistenceError


FUNCTION = "rumi_provider_registry_pack.provider-configure.prepare"
CONTRACT = "tobkiri.action.ai.provider.registry.manage.v1"
OPERATION = "rumi_provider_registry_pack.provider-configure-prepare"
DOCUMENT_LIMIT = 1024 * 1024
# Two decoded Python strings can each occupy four bytes per source byte.
MINIMUM_PREPARE_MEMORY = 10 * DOCUMENT_LIMIT


def is_keyless_local(payload: Mapping[str, Any]) -> bool:
    """Select only the original keyless local adapter request shape."""
    return (
        payload.get("protocol") == "local-openai-compatible"
        and payload.get("key_value") == ""
    )


@dataclass(frozen=True)
class NativeProviderPreparePlanner:
    """Trusted Host composition dependency with a declared working-memory bound."""

    memory_bytes: int
    plan: Callable[[NativeProviderPrepareBorrow], Mapping[str, Any]]

    def __post_init__(self) -> None:
        if (
            type(self.memory_bytes) is not int
            or self.memory_bytes < MINIMUM_PREPARE_MEMORY
            or not callable(self.plan)
        ):
            raise ValueError("native Provider preparation resource bound is invalid")


class NativeProviderPrepareBorrow:
    """Lend only this running operation's input, funding and fixed Registry reads."""

    def __init__(
        self,
        *,
        context: RequestContext,
        payload: Mapping[str, Any],
        user_data_root: Path,
        document_path: Path,
        deadline_monotonic: float,
        resources: ResourceAmount,
        guard: Callable[[], None],
    ) -> None:
        self._context = context
        self._request = MappingProxyType({
            key: tuple(value) if key == "capabilities" else value
            for key, value in payload.items() if key not in {"key_value", "protocol"}
        })
        self._documents = SecureDirectory(user_data_root, create=False)
        self._document_path = document_path.relative_to(user_data_root)
        self._deadline = deadline_monotonic
        self._resources = resources
        self._guard = guard
        self._thread = threading.get_ident()
        self._active = True
        self._read_attempts = 0
        self._successful_reads = 0

    def assert_current(self) -> None:
        """Recheck admission, dispatched lease, cancellation and Profile custody."""
        if not self._active or self._thread != threading.get_ident():
            raise PermissionError("native Provider preparation borrow is unavailable")
        self._guard()

    @property
    def request(self) -> Mapping[str, Any]:
        """Borrow immutable keyless fields, keeping original endpoint spelling."""
        self.assert_current()
        return self._request

    @property
    def context(self) -> RequestContext:
        """Borrow the original Host-authenticated Profile and request identity."""
        self.assert_current()
        return self._context

    @property
    def deadline_monotonic(self) -> float:
        """Return the original Broker deadline for this invocation."""
        self.assert_current()
        return self._deadline

    @property
    def resources(self) -> ResourceAmount:
        """Return the capacity reserved by the original Broker admission."""
        self.assert_current()
        return self._resources

    def read_legacy_registry(self) -> str | None:
        """Read the fixed Profile Registry once for each of the two observations."""
        self.assert_current()
        if self._read_attempts >= 2:
            raise PermissionError("native Provider preparation reads are exhausted")
        self._read_attempts += 1
        opened = False

        def check_open_file(_path: Path, _descriptor: int) -> None:
            nonlocal opened
            opened = True
            self.assert_current()

        try:
            content = self._documents.read_bytes_bounded(
                self._document_path,
                max_bytes=DOCUMENT_LIMIT,
                verify_open_file=check_open_file,
            ).decode("utf-8")
        except FileNotFoundError:
            if opened:
                raise
            content = None
        except SecurePersistenceError as error:
            if opened or not isinstance(error.__cause__, FileNotFoundError):
                raise
            content = None
        self.assert_current()
        self._successful_reads += 1
        return content

    def _close(self) -> None:
        self._active = False


def invoke_native_provider_prepare(
    planner: NativeProviderPreparePlanner,
    borrow: NativeProviderPrepareBorrow,
    *,
    payload: Mapping[str, Any],
    normalized: Mapping[str, Any],
) -> dict[str, Any]:
    """Close the borrow on every exit and accept only the original plan schema."""
    try:
        borrow.assert_current()
        plan = dict(planner.plan(borrow))
        borrow.assert_current()
        revision = plan.get("expected_revision")
        expected = {
            "profile_id": borrow.context.profile_id,
            "provider_instance_id": "provider." + normalized["connection_name"],
            "adapter_id": "local-openai-compatible",
            "endpoint": normalized["endpoint"],
            "expected_revision": revision,
            "request_digest": canonical_digest(dict(payload)),
        }
        if "capabilities" in normalized:
            expected["credential_scopes"] = sorted(normalized["capabilities"])
        if (
            borrow._successful_reads != 2 or type(revision) is not int or revision < 0
            or plan != expected
        ):
            raise ValueError("native Provider preparation returned an invalid plan")
        return expected
    finally:
        borrow._close()
