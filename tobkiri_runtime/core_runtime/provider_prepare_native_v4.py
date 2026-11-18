"""Host-private, synchronous borrowing for keyless Provider preparation."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import threading
from types import MappingProxyType
from typing import Any

from tobkiri_host.admission import ResourceAmount
from tobkiri_host.models import RequestContext
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.secure_persistence import (
    SecureDirectory,
    SecurePersistenceError,
    SecurePersistenceLimitError,
)


FUNCTION = "rumi_provider_registry_pack.provider-configure.prepare"
CONTRACT = "tobkiri.action.ai.provider.registry.manage.v1"
OPERATION = "rumi_provider_registry_pack.provider-configure-prepare"
EXECUTE_FUNCTION = "rumi_provider_registry_pack.provider-configure.execute"
EXECUTE_OPERATION = "rumi_provider_registry_pack.provider-configure"
DOCUMENT_LIMIT = 1024 * 1024
# Two decoded Python strings can each occupy four bytes per source byte.
MINIMUM_PREPARE_MEMORY = 10 * DOCUMENT_LIMIT

_http_scope: ContextVar[tuple[str, Callable[[], None]] | None] = ContextVar(
    "provider_prepare_http_scope", default=None,
)


class NativeProviderRegistryRequiresLegacyRead(RuntimeError):
    """The fixed Provider Registry exceeds the native observation envelope."""


@contextmanager
def bind_provider_prepare_http_scope(
    instance: str, check: Callable[[], None],
) -> Iterator[None]:
    """Bind the currently serving Host capture through nested Broker work."""
    token = _http_scope.set((instance, check))
    try:
        yield
    finally:
        _http_scope.reset(token)


def current_provider_prepare_http_scope() -> tuple[str, Callable[[], None]] | None:
    """Borrow the original HTTP server lifecycle and its live capture check."""
    return _http_scope.get()


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
        runtime_instance: str | None = None,
        frozen_plan: Mapping[str, Any] | None = None,
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
        self._runtime_instance = runtime_instance
        self._frozen_plan = (
            MappingProxyType({
                key: tuple(value) if key == "credential_scopes" else value
                for key, value in frozen_plan.items()
            }) if frozen_plan is not None else None
        )
        self._read_limit = 3 if frozen_plan is not None else 2
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
    def runtime_instance(self) -> str | None:
        """Identify the live original HTTP server owning the request."""
        self.assert_current()
        return self._runtime_instance

    @property
    def resources(self) -> ResourceAmount:
        """Return the capacity reserved by the original Broker admission."""
        self.assert_current()
        return self._resources

    @property
    def frozen_plan(self) -> Mapping[str, Any] | None:
        """Borrow the exact approved base plan during commit validation."""
        self.assert_current()
        return self._frozen_plan

    def read_legacy_registry(self) -> str | None:
        """Read the fixed Profile Registry at the current observation."""
        self.assert_current()
        if self._read_attempts >= self._read_limit:
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
        except SecurePersistenceLimitError:
            raise NativeProviderRegistryRequiresLegacyRead(
                "Provider Registry exceeds the native observation envelope",
            ) from None
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
        expected = _expected_plan(borrow.context, payload, normalized, revision)
        if (
            borrow._successful_reads != 2 or type(revision) is not int or revision < 0
            or plan != expected
        ):
            raise ValueError("native Provider preparation returned an invalid plan")
        return expected
    finally:
        borrow._close()


def _expected_plan(
    context: RequestContext,
    payload: Mapping[str, Any],
    normalized: Mapping[str, Any],
    revision: object,
) -> dict[str, Any]:
    expected = {
        "profile_id": context.profile_id,
        "provider_instance_id": "provider." + normalized["connection_name"],
        "adapter_id": "local-openai-compatible",
        "endpoint": normalized["endpoint"],
        "expected_revision": revision,
        "request_digest": canonical_digest(dict(payload)),
    }
    if "capabilities" in normalized:
        expected["credential_scopes"] = sorted(normalized["capabilities"])
    return expected


def invoke_native_provider_commit_validation(
    planner: NativeProviderPreparePlanner,
    borrow: NativeProviderPrepareBorrow,
    *,
    payload: Mapping[str, Any],
    normalized: Mapping[str, Any],
) -> tuple[dict[str, Any], int]:
    """Return a bounded keyless record tied to the request and its approved revision."""
    try:
        frozen = borrow.frozen_plan
        if frozen is None:
            raise PermissionError("native Provider commit requires an approved plan")
        revision = frozen.get("expected_revision")
        expected = _expected_plan(borrow.context, payload, normalized, revision)
        if "credential_scopes" in expected:
            expected["credential_scopes"] = tuple(expected["credential_scopes"])
        if type(revision) is not int or revision < 0 or dict(frozen) != expected:
            raise PermissionError("native Provider approved plan differs")
        result = planner.plan(borrow)
        borrow.assert_current()
        record = result.get("record")
        result_revision = result.get("expected_revision")
        if (
            borrow._successful_reads != 3
            or set(result) != {"record", "expected_revision"}
            or type(result_revision) is not int
            or result_revision != revision
            or not isinstance(record, dict)
            or record.get("provider_instance_id") != expected["provider_instance_id"]
            or record.get("adapter_id") != expected["adapter_id"]
            or record.get("endpoint") != expected["endpoint"]
            or "credential_handle" not in record
            or record["credential_handle"] is not None
        ):
            raise ValueError("native Provider validation returned an invalid record")
        return record, revision
    finally:
        borrow._close()


class _LocalSaveAuthorityLost(PermissionError):
    """The approved lease expired before the Registry write started."""


def save_validated_local_provider(
    profile_id: str,
    user_data_root: Path,
    record: Mapping[str, Any],
    *,
    expected_revision: int,
    guard: Callable[[], None],
) -> dict[str, Any]:
    """Save through the original Registry while rechecking authority under its lock."""
    from ecosystem.rumi_provider_registry_pack.runtime.configuration import (
        _connection_save_confirmed,
        _save_snapshot,
    )
    from ecosystem.rumi_provider_registry_pack.runtime.registry import (
        ProviderRegistry,
        ProviderRegistryConflict,
    )

    class _GuardedRegistry(ProviderRegistry):
        def _write(self, state: Mapping[str, Any]) -> None:
            # The original save holds NamedLock at this final publication step.
            try:
                guard()
            except Exception as error:
                raise _LocalSaveAuthorityLost(
                    "Provider save authority changed",
                ) from error
            super()._write(state)

    registry = _GuardedRegistry(profile_id, user_data_root=user_data_root)
    try:
        registry.save(record, expected_revision=expected_revision)
    except _LocalSaveAuthorityLost:
        raise
    except Exception as error:
        if isinstance(error, (ValueError, ProviderRegistryConflict)) or not (
            _connection_save_confirmed(
                _save_snapshot(registry), record, expected_revision=expected_revision,
            )
        ):
            raise RuntimeError("provider connection save was not confirmed") from None
    return {"configured": True, "provider_instance_id": record["provider_instance_id"]}
