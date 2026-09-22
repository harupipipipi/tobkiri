"""Production Pack v4 runtime assembled from one active immutable snapshot."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from core_runtime.authority.v4 import AuthorityStore

from .artifact_compiler import CompiledPack, compile_pack_root, routes_for_plan
from .backends import BackendRegistry
from .broker import RequestAdmissionPort, RequestBroker
from .authority_v4 import AuthorityV4Adapter
from .composition import AuthorityCeilings, HostV4Composition
from .contracts import AdapterExecutor, AdapterPlanner
from .effects import ReconciliationStore
from .materialization import MaterializationCoordinator
from .models import InvocationFrame, PackArtifact, RequestContext


@dataclass(frozen=True)
class ProductionRuntimeV4:
    """One activation-scoped runtime; it has no mutable provider discovery."""

    composition: HostV4Composition

    @classmethod
    def capture(
        cls,
        *,
        profile: Mapping[str, Any],
        lock: Mapping[str, Any],
        plan: Mapping[str, Any],
        activation: Mapping[str, Any],
        pack_roots: Mapping[str, Path],
        supporting_artifacts: Sequence[PackArtifact],
        verified_effective_artifacts: Mapping[str, str],
        authority_ceilings: Mapping[tuple[str, str], AuthorityCeilings],
    ) -> "ProductionRuntimeV4":
        """Compile only exact plan Pack roots and capture the active graph."""
        binding_pack_ids = {item["pack_id"] for item in plan["bindings"]}
        if set(pack_roots) != binding_pack_ids:
            raise ValueError("Pack roots must exactly equal ResolvedPlan binding Packs")
        compiled: tuple[CompiledPack, ...] = tuple(
            compile_pack_root(pack_roots[pack_id]) for pack_id in sorted(binding_pack_ids)
        )
        artifacts = tuple(item.artifact for item in compiled) + tuple(supporting_artifacts)
        routes = routes_for_plan(plan, compiled)
        composition = HostV4Composition.capture(
            profile=profile,
            lock=lock,
            plan=plan,
            activation=activation,
            artifacts=artifacts,
            routes=routes,
            authority_ceilings=authority_ceilings,
            effective_artifacts=verified_effective_artifacts,
        )
        return cls(composition=composition)

    def broker(
        self,
        *,
        authority_store: AuthorityStore,
        adapters: AdapterPlanner,
        adapter_executor: AdapterExecutor,
        backends: BackendRegistry,
        materialization: MaterializationCoordinator,
        admission: RequestAdmissionPort,
        reconciliation: ReconciliationStore,
        terminate_domain: Callable[[str], None] | None = None,
        authority_adapter: AuthorityV4Adapter | None = None,
    ) -> RequestBroker:
        """Build the sole request Broker using this captured authority adapter."""
        authority = authority_adapter or self.composition.authority_adapter(
            authority_store, terminate_domain=terminate_domain
        )
        return RequestBroker(
            catalog=self.composition.catalog,
            adapters=adapters,
            adapter_executor=adapter_executor,
            backends=backends,
            materialization=materialization,
            admission=admission,
            authority=authority,
            audit=authority,
            reconciliation=reconciliation,
        )

    def dispatch_session(
        self,
        *,
        broker: RequestBroker,
        context_for: Callable[..., RequestContext],
        effect_scope_for: Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]],
        providers: Mapping[str, tuple[Mapping[str, Any], ...]],
        authority_control: AuthorityV4Adapter | None = None,
        current_capture_check: Callable[[], None] | None = None,
        owned_authority_store: AuthorityStore | None = None,
        close_callbacks: tuple[Callable[[], None], ...] = (),
        stop_callbacks: tuple[Callable[[], None], ...] = (),
    ) -> "V4DispatchSession":
        """Bind request ports to identities from this captured composition."""
        return V4DispatchSession(
            broker=broker,
            context_for=context_for,
            effect_scope_for=effect_scope_for,
            providers=providers,
            profile_id=str(self.composition.profile["profile_id"]),
            plan_digest=str(self.composition.plan["plan_digest"]),
            authority_control=authority_control,
            current_capture_check=current_capture_check,
            owned_authority_store=owned_authority_store,
            close_callbacks=close_callbacks,
            stop_callbacks=stop_callbacks,
        )


@dataclass(frozen=True)
class V4DispatchSession:
    """Authenticated request adapter shared by worker, HTTP, and chat surfaces."""

    broker: RequestBroker
    context_for: Callable[..., RequestContext]
    effect_scope_for: Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]]
    providers: Mapping[str, tuple[Mapping[str, Any], ...]]
    profile_id: str
    plan_digest: str
    authority_control: AuthorityV4Adapter | None = None
    current_capture_check: Callable[[], None] | None = None
    owned_authority_store: AuthorityStore | None = None
    close_callbacks: tuple[Callable[[], None], ...] = ()
    stop_callbacks: tuple[Callable[[], None], ...] = ()

    def cancel_pending_reads(self) -> None:
        """Fence server-owned reads at a reusable stop/restart boundary."""

        for callback in self.stop_callbacks:
            callback()

    def close(self) -> None:
        """Close the Broker, then its owned Authority database, idempotently."""

        self.broker.close()
        for callback in self.close_callbacks:
            callback()
        if self.owned_authority_store is not None:
            self.owned_authority_store.close()

    def __enter__(self) -> "V4DispatchSession":
        """Return this captured session for explicit scoped ownership."""

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        """Release captured Broker and Authority resources."""

        del exc_type, exc_value, traceback
        self.close()

    def assert_current(self) -> None:
        """Fail closed when the persisted activation no longer matches."""

        if self.current_capture_check is None:
            raise RuntimeError("v4 dispatch session has no current-capture verifier")
        self.current_capture_check()

    def provider_metadata(self, contract_id: str) -> tuple[Mapping[str, Any], ...]:
        """Return metadata pinned when this session was constructed."""
        return self.providers.get(contract_id, ())

    def assert_operation_ready(self, contract_id: str, operation_id: str) -> None:
        """Require an exact selected binding and production-ready backend."""

        binding = self.broker._catalog.resolve_pinned(contract_id, operation_id)
        backend = self.broker._backends.select(binding)
        providers = tuple(
            item
            for item in self.provider_metadata(contract_id)
            if item.get("operation_id") == operation_id
        )
        if len(providers) != 1:
            raise RuntimeError("selected Provider metadata is unavailable")
        provider = providers[0]
        expected = {
            "provider_id": binding.function.function_id,
            "function_id": binding.function.function_id,
            "principal_id": binding.principal_ref.value,
            "implementation_digest": binding.function.implementation_digest,
            "backend_id": backend.status.backend_id,
            "backend_digest": backend.status.backend_digest,
            "profile_id": self.profile_id,
            "plan_digest": self.plan_digest,
        }
        if any(provider.get(key) != value for key, value in expected.items()):
            raise RuntimeError("selected Provider/backend metadata is stale or wrong")

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
        *,
        version_range: str | None = None,
    ) -> Mapping[str, Any]:
        """Dispatch through the captured Broker without identity from payload.

        An omitted compatibility requirement is bound to the exact Contract
        version in the immutable plan.  A caller-supplied range remains a
        strict additional constraint and can never select another Provider.
        """
        arguments = dict(payload)
        session_id = str(arguments.pop("_session_id", "")).strip()
        parameter_count = len(inspect.signature(self.context_for).parameters)
        if parameter_count >= 3:
            if not session_id:
                raise ValueError("authenticated session binding is required")
            context = self.context_for(contract_id, operation_id, session_id)
        else:
            context = self.context_for(contract_id, operation_id)
        return self.broker.invoke(
            InvocationFrame(
                contract_id=contract_id,
                version_range=version_range,
                operation_id=operation_id,
                payload=arguments,
            ),
            context,
            effect_scope=self.effect_scope_for(contract_id, operation_id, arguments),
        )


class DispatchContainer(Protocol):
    """Minimal composition-root port used to publish one captured session."""

    def set_instance(self, name: str, instance: Any) -> None:
        """Install an already-constructed immutable service instance."""


class CapturedDispatchSession(Protocol):
    """Common Host port for immutable runtime and Authority-control sessions."""

    @property
    def profile_id(self) -> str:
        """Return the exact captured Profile identity."""

    @property
    def plan_digest(self) -> str:
        """Return the exact captured ResolvedPlan digest."""


def install_dispatch_session(
    container: DispatchContainer, session: CapturedDispatchSession
) -> None:
    """Publish the exact captured activation to worker, HTTP, and chat code."""
    if not session.profile_id.strip():
        raise ValueError("v4 dispatch session profile_id must be non-empty")
    if not session.plan_digest.startswith("sha256:"):
        raise ValueError("v4 dispatch session plan_digest must be canonical")
    container.set_instance("v4_dispatch_session", session)


__all__ = [
    "CapturedDispatchSession",
    "DispatchContainer",
    "ProductionRuntimeV4",
    "V4DispatchSession",
    "install_dispatch_session",
]
