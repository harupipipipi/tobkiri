from __future__ import annotations

from typing import List, Optional

from core_runtime.dependency_resolver import resolve_load_order

from .module_catalog import ModuleCatalog
from .module_state import ModuleStateManager


class ModuleDependencyResolver:
    """Resolve defaultspack module dependencies through the canonical resolver.

    Modules are not Packs, but their directed dependency graph has the same
    ordering and cycle-safety requirements.  Keeping this adapter explicit
    prevents a second, permissive graph implementation from drifting away
    from the runtime Pack resolver.
    """

    def __init__(self, state_manager: Optional[ModuleStateManager] = None) -> None:
        self.catalog = ModuleCatalog(state_manager)

    def resolve_load_order(self) -> List[str]:
        """Return module load order or fail closed for missing edges/cycles."""
        graph = self.catalog.dependency_graph()
        manifests = {
            module_id: {"dependencies": dependencies}
            for module_id, dependencies in graph.items()
        }
        return resolve_load_order(manifests, strict=True)


# Kept as a narrow import compatibility alias while callers migrate to the
# unambiguous name above.  It deliberately does not retain a separate
# implementation.
DependencyManager = ModuleDependencyResolver
