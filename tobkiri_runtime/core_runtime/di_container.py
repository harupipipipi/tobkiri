"""
di_container.py - lightweight DI container

Provides service factory registration, lazy initialization, and caching.
Thread-safe via RLock.

Usage:
    from core_runtime.di_container import get_container, reset_container

    container = get_container()
    audit = container.get("audit_logger")
"""

from __future__ import annotations

import sys
import threading
from typing import Any, Callable, Dict, List, Optional

_this_module = sys.modules.get(__name__)
if _this_module is not None:
    if __name__.startswith("tobkiri_runtime."):
        sys.modules.setdefault(__name__.removeprefix("tobkiri_runtime."), _this_module)
    else:
        sys.modules.setdefault(f"tobkiri_runtime.{__name__}", _this_module)


class DIContainer:
    """
    Lightweight service registry with lazy initialization and caching.

    register() stores a zero-argument factory. get() runs the factory on
    first access and caches the instance. Factory exceptions are not cached.
    """

    def __init__(self) -> None:
        self._lock: threading.RLock = threading.RLock()
        self._factories: Dict[str, Callable[[], Any]] = {}
        self._instances: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, name: str, factory: Callable[[], Any]) -> None:
        """
        Register a service factory.

        Re-registering a service replaces the factory and drops any cached instance.

        Args:
            name:    Service name.
            factory: Zero-argument callable factory.
        """
        with self._lock:
            self._factories[name] = factory
            self._instances.pop(name, None)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> Any:
        """
        Get a service instance.

        Cached instances are reused. Otherwise the factory is executed and
        cached only when it succeeds. Factory exceptions are re-raised.

        Args:
            name: Service name.

        Returns:
            Service instance.

        Raises:
            KeyError: Unknown service name.
            Exception: Exception raised by the factory.
        """
        with self._lock:
            if name in self._instances:
                return self._instances[name]
            if name not in self._factories:
                raise KeyError(f"Service not registered: {name}")
            factory = self._factories[name]
            # RLock allows same-thread re-entry while preserving one-time creation.
            instance = factory()  # Do not cache when the factory raises.
            self._instances[name] = instance
            return instance

    def get_or_none(self, name: str) -> Optional[Any]:
        """
        Get a service instance, returning None for missing services or factory errors.

        Args:
            name: Service name.

        Returns:
            Service instance, or None.
        """
        try:
            return self.get(name)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def has(self, name: str) -> bool:
        """
        Return whether a service is registered.

        Args:
            name: Service name.

        Returns:
            True if registered, otherwise False.
        """
        with self._lock:
            return name in self._factories

    def registered_names(self) -> List[str]:
        """
        Return registered service names.

        Returns:
            List of service names.
        """
        with self._lock:
            return list(self._factories.keys())

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self, name: str) -> None:
        """
        Drop the cached instance for a service while keeping its factory.

        Args:
            name: Service name.
        """
        with self._lock:
            self._instances.pop(name, None)

    def reset_all(self) -> None:
        """
        Drop all cached instances while keeping factory registrations.
        """
        with self._lock:
            self._instances.clear()

    def set_instance(self, name: str, instance: Any) -> None:
        """
        Set a cached instance directly.

        Useful for services initialized with arguments elsewhere.

        Args:
            name:     Service name.
            instance: Instance to cache.
        """
        with self._lock:
            self._instances[name] = instance


# ======================================================================
# Global container
# ======================================================================

_container: Optional[DIContainer] = None
_container_lock: threading.Lock = threading.Lock()


def get_container() -> DIContainer:
    """
    Get the global DIContainer, lazily initialized.

    The first call registers all default factories.

    Returns:
        DIContainer instance.
    """
    global _container
    if _container is None:
        with _container_lock:
            if _container is None:
                c = DIContainer()
                _register_defaults(c)
                _container = c
    return _container


def reset_container() -> None:
    """
    Reset the global DIContainer for tests.

    The next get_container() call creates a new container.
    """
    global _container
    with _container_lock:
        _container = None


# ======================================================================
# Default factory registration
# ======================================================================

def _register_defaults(container: DIContainer) -> None:
    """
    Register all default service factories on a container.

    Wave 1-4: AuditLogger, HMACKeyManager, VocabRegistry,
              NetworkGrantManager, StoreRegistry,
              ApprovalManager, PermissionManager,
              ContainerOrchestrator, HostPrivilegeManager,
              FlowComposer, FunctionAliasRegistry,
              SecretsStore, FlowModifierLoader, FlowModifierApplier
    Wave 5:   PackAPIServer, EgressProxyManager,
              PythonFileExecutor, SecureExecutor,
              LibExecutor, UnitExecutor, CapabilityExecutor
    Wave 8:   Diagnostics, InstallJournal, InterfaceRegistry,
              EventBus, ComponentLifecycleExecutor
    Wave 15:  HealthChecker, MetricsCollector, Profiler
    Wave 24:  FunctionRegistry

    Args:
        container: Target DIContainer.
    """
    # --- Wave 1: core ---
    def _audit_logger_factory() -> "AuditLogger":  # noqa: F821
        from .audit_logger import AuditLogger
        from .paths import USER_DATA_DIR

        return AuditLogger(str(USER_DATA_DIR / "audit"))

    def _hmac_key_manager_factory() -> "HMACKeyManager":  # noqa: F821
        from .hmac_key_manager import HMACKeyManager
        return HMACKeyManager()

    # --- Wave 2: registry ---
    def _vocab_registry_factory() -> "VocabRegistry":  # noqa: F821
        from .vocab_registry import VocabRegistry
        return VocabRegistry()

    def _network_grant_manager_factory() -> "NetworkGrantManager":  # noqa: F821
        from .network_grant_manager import NetworkGrantManager
        return NetworkGrantManager()

    def _store_registry_factory() -> "StoreRegistry":  # noqa: F821
        from .store_registry import StoreRegistry
        return StoreRegistry()

    # --- Wave 3: approval / permission ---
    def _approval_manager_factory() -> "ApprovalManager":  # noqa: F821
        from .approval_manager import ApprovalManager
        instance = ApprovalManager()
        instance.initialize()
        return instance

    def _permission_manager_factory() -> "PermissionManager":  # noqa: F821
        from .permission_manager import PermissionManager
        return PermissionManager()

    def _capability_trust_store_factory() -> "CapabilityTrustStore":  # noqa: F821
        from .capability_trust_store import CapabilityTrustStore
        return CapabilityTrustStore()

    def _capability_grant_manager_factory() -> "CapabilityGrantManager":  # noqa: F821
        from .capability_grant_manager import get_capability_grant_manager
        instance = get_capability_grant_manager()
        try:
            from .bootstrap.default_builtin_grants import apply_default_builtin_grants

            apply_default_builtin_grants(instance)
        except Exception:
            pass
        return instance

    # --- Wave 4: orchestration / composition ---
    def _container_orchestrator_factory() -> "ContainerOrchestrator":  # noqa: F821
        from .container_orchestrator import ContainerOrchestrator
        return ContainerOrchestrator()

    def _host_privilege_manager_factory() -> "HostPrivilegeManager":  # noqa: F821
        from .host_privilege_manager import HostPrivilegeManager
        return HostPrivilegeManager()

    def _flow_composer_factory() -> "FlowComposer":  # noqa: F821
        from .flow_composer import FlowComposer
        return FlowComposer()

    def _function_alias_registry_factory() -> "FunctionAliasRegistry":  # noqa: F821
        from .function_alias import FunctionAliasRegistry
        return FunctionAliasRegistry()

    def _secrets_store_factory() -> "SecretsStore":  # noqa: F821
        from .secrets_store import SecretsStore
        return SecretsStore()

    def _secrets_grant_manager_factory() -> "SecretsGrantManager":  # noqa: F821
        from .secrets_grant_manager import SecretsGrantManager
        return SecretsGrantManager()

    def _modifier_loader_factory() -> "FlowModifierLoader":  # noqa: F821
        from .flow_modifier import FlowModifierLoader
        return FlowModifierLoader()

    def _modifier_applier_factory() -> "FlowModifierApplier":  # noqa: F821
        from .flow_modifier import FlowModifierApplier
        return FlowModifierApplier()

    # --- Wave 5: executors / proxy / API server ---
    def _pack_api_server_factory() -> None:
        # PackAPIServer requires explicit initialization with args.
        # Returns None; real instance set via initialize_pack_api_server().
        return None

    def _egress_proxy_manager_factory() -> "UDSEgressProxyManager":  # noqa: F821
        from .egress_proxy import UDSEgressProxyManager
        c = get_container()
        return UDSEgressProxyManager(
            network_grant_manager=c.get("network_grant_manager"),
            audit_logger=c.get_or_none("audit_logger"),
        )

    def _python_file_executor_factory() -> "PythonFileExecutor":  # noqa: F821
        from .python_file_executor import PythonFileExecutor
        return PythonFileExecutor()

    def _secure_executor_factory() -> "SecureExecutor":  # noqa: F821
        from .secure_executor import SecureExecutor
        return SecureExecutor()

    def _lib_executor_factory() -> "LibExecutor":  # noqa: F821
        from .lib_executor import LibExecutor
        return LibExecutor()

    def _unit_executor_factory() -> "UnitExecutor":  # noqa: F821
        from .unit_executor import UnitExecutor
        return UnitExecutor()

    def _capability_executor_factory() -> "CapabilityExecutor":  # noqa: F821
        from .capability_executor import CapabilityExecutor
        instance = CapabilityExecutor()
        instance.initialize()
        return instance

    def _authority_service_factory() -> "AuthorityService":  # noqa: F821
        from .authority.service import AuthorityService
        return AuthorityService(
            capability_grant_manager=container.get("capability_grant_manager"),
            secrets_grant_manager=container.get("secrets_grant_manager"),
            network_grant_manager=container.get("network_grant_manager"),
            host_privilege_manager=container.get("host_privilege_manager"),
            hmac_key_manager=container.get("hmac_key_manager"),
        )

    # --- Wave 8: Kernel core services ---
    def _diagnostics_factory() -> "Diagnostics":  # noqa: F821
        from .diagnostics import Diagnostics
        return Diagnostics()

    def _install_journal_factory() -> "InstallJournal":  # noqa: F821
        from .install_journal import InstallJournal
        return InstallJournal()

    def _interface_registry_factory() -> "InterfaceRegistry":  # noqa: F821
        from .interface_registry import InterfaceRegistry
        return InterfaceRegistry()

    def _event_bus_factory() -> "EventBus":  # noqa: F821
        from .event_bus import EventBus
        return EventBus()

    def _component_lifecycle_factory() -> "ComponentLifecycleExecutor":  # noqa: F821
        from .component_lifecycle import ComponentLifecycleExecutor
        c = get_container()
        return ComponentLifecycleExecutor(
            diagnostics=c.get("diagnostics"),
            install_journal=c.get("install_journal"),
        )

    # --- Wave 15: Foundation services ---
    def _health_checker_factory() -> "HealthChecker":  # noqa: F821
        from .health import HealthChecker
        return HealthChecker()

    def _metrics_collector_factory() -> "MetricsCollector":  # noqa: F821
        from .metrics import MetricsCollector
        return MetricsCollector()

    def _profiler_factory() -> "Profiler":  # noqa: F821
        from .profiling import Profiler
        return Profiler()

    # --- Wave 22: Docker capability ---
    def _docker_capability_handler_factory() -> "DockerCapabilityHandler":  # noqa: F821
        from .docker_capability import DockerCapabilityHandler
        return DockerCapabilityHandler()



    # --- Wave V2: Viewer capability ---
    def _viewer_capability_handler_factory() -> "ViewerCapabilityHandler":  # noqa: F821
        from .viewer_capability import ViewerCapabilityHandler
        return ViewerCapabilityHandler()

    # --- Wave V-4: Desktop app capability ---
    def _desktop_capability_handler_factory() -> "DesktopCapabilityHandler":  # noqa: F821
        from .desktop_capability import DesktopCapabilityHandler
        return DesktopCapabilityHandler()

    # --- Wave 24: FunctionRegistry ---
    def _function_registry_factory() -> "FunctionRegistry":  # noqa: F821
        from .function_registry import FunctionRegistry
        c = get_container()
        vr = c.get_or_none("vocab_registry")
        return FunctionRegistry(vocab_registry=vr)

    # --- Managed sandbox boundary ---
    def _managed_sandbox_supervisor_factory() -> Any:
        from ecosystem.defaultspack.backend.sandbox.isolation import ManagedSandboxSupervisor

        return ManagedSandboxSupervisor()

    # --- Register all (each name exactly once) ---
    container.register("audit_logger", _audit_logger_factory)
    container.register("hmac_key_manager", _hmac_key_manager_factory)
    container.register("vocab_registry", _vocab_registry_factory)
    container.register("network_grant_manager", _network_grant_manager_factory)
    container.register("store_registry", _store_registry_factory)
    container.register("approval_manager", _approval_manager_factory)
    container.register("permission_manager", _permission_manager_factory)
    container.register("capability_trust_store", _capability_trust_store_factory)
    container.register("capability_grant_manager", _capability_grant_manager_factory)
    container.register("container_orchestrator", _container_orchestrator_factory)
    container.register("host_privilege_manager", _host_privilege_manager_factory)
    container.register("flow_composer", _flow_composer_factory)
    container.register("function_alias_registry", _function_alias_registry_factory)
    container.register("secrets_store", _secrets_store_factory)
    container.register("secrets_grant_manager", _secrets_grant_manager_factory)
    container.register("modifier_loader", _modifier_loader_factory)
    container.register("modifier_applier", _modifier_applier_factory)
    container.register("pack_api_server", _pack_api_server_factory)
    container.register("egress_proxy_manager", _egress_proxy_manager_factory)
    container.register("python_file_executor", _python_file_executor_factory)
    container.register("secure_executor", _secure_executor_factory)
    container.register("lib_executor", _lib_executor_factory)
    container.register("unit_executor", _unit_executor_factory)
    container.register("capability_executor", _capability_executor_factory)
    container.register("authority_service", _authority_service_factory)
    container.register("diagnostics", _diagnostics_factory)
    container.register("install_journal", _install_journal_factory)
    container.register("interface_registry", _interface_registry_factory)
    container.register("event_bus", _event_bus_factory)
    container.register("component_lifecycle", _component_lifecycle_factory)
    container.register("health_checker", _health_checker_factory)
    container.register("metrics_collector", _metrics_collector_factory)
    container.register("profiler", _profiler_factory)
    container.register("docker_capability_handler", _docker_capability_handler_factory)
    container.register("viewer_capability_handler", _viewer_capability_handler_factory)
    container.register("desktop_capability_handler", _desktop_capability_handler_factory)
    container.register("function_registry", _function_registry_factory)
    container.register("managed_sandbox_supervisor", _managed_sandbox_supervisor_factory)


def get_authority_service():
    """Return the process-wide AuthorityService."""
    return get_container().get("authority_service")
