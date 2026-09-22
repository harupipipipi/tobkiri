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
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from .approval_manager import ApprovalManager
    from .audit_logger import AuditLogger
    from .capability_grant_manager import CapabilityGrantManager
    from .capability_trust_store import CapabilityTrustStore
    from .desktop_capability import DesktopCapabilityHandler
    from .diagnostics import Diagnostics
    from .event_bus import EventBus
    from .function_alias import FunctionAliasRegistry
    from .health import HealthChecker
    from .hmac_key_manager import HMACKeyManager
    from .install_journal import InstallJournal
    from .metrics import MetricsCollector
    from .network_grant_manager import NetworkGrantManager
    from .profiling import Profiler
    from .secrets_grant_manager import SecretsGrantManager
    from .secrets_store import SecretsStore
    from .store_registry import StoreRegistry
    from .vocab_registry import VocabRegistry

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

    The first call registers Host support services only. Pack execution is
    installed explicitly as a captured v4 Broker session.

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
              NetworkGrantManager, StoreRegistry, ApprovalManager,
              FunctionAliasRegistry, SecretsStore
    Wave 8:   Diagnostics, InstallJournal, InterfaceRegistry,
              EventBus, ComponentLifecycleExecutor
    Wave 15:  HealthChecker, MetricsCollector, Profiler
    Wave 24:  FunctionRegistry

    Args:
        container: Target DIContainer.
    """
    # --- Wave 1: core ---
    def _audit_logger_factory() -> "AuditLogger":
        from .audit_logger import AuditLogger
        from .paths import USER_DATA_DIR

        return AuditLogger(str(USER_DATA_DIR / "audit"))

    def _hmac_key_manager_factory() -> "HMACKeyManager":
        from .hmac_key_manager import HMACKeyManager
        return HMACKeyManager()

    # --- Wave 2: registry ---
    def _vocab_registry_factory() -> "VocabRegistry":
        from .vocab_registry import VocabRegistry
        return VocabRegistry()

    def _network_grant_manager_factory() -> "NetworkGrantManager":
        from .network_grant_manager import NetworkGrantManager
        return NetworkGrantManager()

    def _store_registry_factory() -> "StoreRegistry":
        from .store_registry import StoreRegistry
        return StoreRegistry()

    # --- Wave 3: approval / permission ---
    def _approval_manager_factory() -> "ApprovalManager":
        from .approval_manager import ApprovalManager
        instance = ApprovalManager()
        instance.initialize()
        return instance

    def _capability_trust_store_factory() -> "CapabilityTrustStore":
        from .capability_trust_store import CapabilityTrustStore
        return CapabilityTrustStore()

    def _capability_grant_manager_factory() -> "CapabilityGrantManager":
        from .capability_grant_manager import get_capability_grant_manager
        instance = get_capability_grant_manager()
        try:
            from .bootstrap.default_builtin_grants import apply_default_builtin_grants

            apply_default_builtin_grants(instance)
        except Exception:
            pass
        return instance

    # --- Wave 4: orchestration / composition ---
    def _function_alias_registry_factory() -> "FunctionAliasRegistry":
        from .function_alias import FunctionAliasRegistry
        return FunctionAliasRegistry()

    def _secrets_store_factory() -> "SecretsStore":
        from .secrets_store import SecretsStore
        return SecretsStore()

    def _secrets_grant_manager_factory() -> "SecretsGrantManager":
        from .secrets_grant_manager import SecretsGrantManager
        return SecretsGrantManager()

    # --- Wave 8: Kernel core services ---
    def _diagnostics_factory() -> "Diagnostics":
        from .diagnostics import Diagnostics
        return Diagnostics()

    def _install_journal_factory() -> "InstallJournal":
        from .install_journal import InstallJournal
        return InstallJournal()

    def _event_bus_factory() -> "EventBus":
        from .event_bus import EventBus
        return EventBus()

    # --- Wave 15: Foundation services ---
    def _health_checker_factory() -> "HealthChecker":
        from .health import HealthChecker
        return HealthChecker()

    def _metrics_collector_factory() -> "MetricsCollector":
        from .metrics import MetricsCollector
        return MetricsCollector()

    def _profiler_factory() -> "Profiler":
        from .profiling import Profiler
        return Profiler()

    # --- Wave V-4: Desktop app capability ---
    def _desktop_capability_handler_factory() -> "DesktopCapabilityHandler":
        from .desktop_capability import DesktopCapabilityHandler
        return DesktopCapabilityHandler()

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
    container.register("capability_trust_store", _capability_trust_store_factory)
    container.register("capability_grant_manager", _capability_grant_manager_factory)
    container.register("function_alias_registry", _function_alias_registry_factory)
    container.register("secrets_store", _secrets_store_factory)
    container.register("secrets_grant_manager", _secrets_grant_manager_factory)
    container.register("diagnostics", _diagnostics_factory)
    container.register("install_journal", _install_journal_factory)
    container.register("event_bus", _event_bus_factory)
    container.register("health_checker", _health_checker_factory)
    container.register("metrics_collector", _metrics_collector_factory)
    container.register("profiler", _profiler_factory)
    container.register("desktop_capability_handler", _desktop_capability_handler_factory)
    container.register("managed_sandbox_supervisor", _managed_sandbox_supervisor_factory)
