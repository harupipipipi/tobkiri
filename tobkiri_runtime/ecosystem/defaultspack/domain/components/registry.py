from __future__ import annotations

import threading
from pathlib import Path
from typing import Iterable, List

from .discovery import ComponentDiscoveryIssue, discover_components
from .manifest import DomainComponent
from core_runtime.resolved_profile_scope import effective_pack_ids

_LOCK = threading.Lock()
_REGISTRY: "DomainComponentRegistry | None" = None


def _default_pack_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _coerce_domain_root(path: Path | str) -> Path:
    candidate = Path(path).expanduser()
    if _is_file(candidate / "pack.v4.json"):
        return candidate / "domain"
    return candidate


def _append_unique(roots: list[Path], root: Path | str) -> None:
    candidate = _coerce_domain_root(root)
    if candidate not in roots:
        roots.append(candidate)


def _has_v4_pack(pack_root: Path, pack_id: str) -> bool:
    import json

    candidates = (
        pack_root / "pack.v4.json",
        pack_root / "v4" / "packs" / f"{pack_id}.pack.v4.json",
    )
    for manifest_path in candidates:
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if str((raw.get("pack") or {}).get("id") or "").strip() == pack_id:
                return True
        except (OSError, UnicodeError, ValueError, TypeError):
            continue
    return False


def build_domain_component_roots(
    pack_root: Path | str,
    *,
    extra_roots: Iterable[Path | str] | None = None,
) -> list[Path]:
    pack_root = Path(pack_root)
    ecosystem_dir = pack_root.parent
    roots: list[Path] = []

    _append_unique(roots, pack_root / "domain")
    if _is_dir(ecosystem_dir):
        effective = effective_pack_ids()
        candidate_pack_ids = set(effective)
        siblings = [ecosystem_dir / pack_id for pack_id in sorted(candidate_pack_ids)]
        for sibling in siblings:
            if sibling == pack_root:
                continue
            if _is_dir(sibling) and _has_v4_pack(sibling, sibling.name) and _is_dir(sibling / "domain"):
                _append_unique(roots, sibling / "domain")

    for root in extra_roots or ():
        _append_unique(roots, root)
    return roots


def get_domain_component_roots() -> list[Path]:
    return build_domain_component_roots(_default_pack_root())


class DomainComponentRegistry:
    def __init__(
        self,
        roots: Path | str | Iterable[Path | str] | None = None,
        *,
        strict: bool = False,
    ) -> None:
        if roots is None:
            self._roots = get_domain_component_roots()
        elif isinstance(roots, (str, Path)):
            self._roots = [_coerce_domain_root(roots)]
        else:
            self._roots = [_coerce_domain_root(root) for root in roots]
        self._strict = strict
        self._components: dict[str, dict[str, DomainComponent]] = {}
        self._aliases: dict[str, dict[str, str]] = {}
        self._issues: list[ComponentDiscoveryIssue] = []
        self.reload()

    @property
    def roots(self) -> list[Path]:
        return list(self._roots)

    @property
    def issues(self) -> list[ComponentDiscoveryIssue]:
        return list(self._issues)

    def reload(self) -> "DomainComponentRegistry":
        self._components = {}
        self._aliases = {}
        self._issues = []
        result = discover_components(self._roots, strict=self._strict)
        self._issues.extend(result.issues)
        for component in result.components:
            bucket = self._components.setdefault(component.category, {})
            bucket[component.id] = component
            alias_bucket = self._aliases.setdefault(component.category, {})
            for alias in component.aliases:
                alias_bucket.setdefault(alias, component.id)
        return self

    def diagnostics(self) -> list[dict[str, str]]:
        return [
            {"path": issue.path, "category": issue.category, "message": issue.message}
            for issue in self._issues
        ]

    def categories(self) -> list[str]:
        return sorted(self._components.keys())

    def list(
        self,
        category: str | None = None,
        *,
        status: str | None = None,
    ) -> list[DomainComponent]:
        categories = [category] if category else self.categories()
        items: list[DomainComponent] = []
        for current_category in categories:
            items.extend(self._components.get(str(current_category), {}).values())
        if status:
            items = [item for item in items if item.status == status]
        items.sort(key=lambda item: (item.category, item.id))
        return items

    def manifests(self, category: str | None = None) -> List[dict]:
        return [component.as_dict() for component in self.list(category)]

    def get(self, category: str, component_id: str) -> DomainComponent | None:
        category = str(category or "").strip()
        component_id = str(component_id or "").strip()
        if not category or not component_id:
            return None
        bucket = self._components.get(category, {})
        if component_id in bucket:
            return bucket[component_id]
        alias_target = self._aliases.get(category, {}).get(component_id)
        if alias_target:
            return bucket.get(alias_target)
        return None

    def manifest_for(self, category: str, component_id: str) -> dict | None:
        component = self.get(category, component_id)
        return component.as_dict() if component else None

    def aliases_for(self, category: str) -> dict[str, str]:
        return dict(self._aliases.get(str(category or "").strip(), {}))


def get_domain_component_registry(
    *,
    force_reload: bool = False,
    strict: bool = False,
) -> DomainComponentRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        with _LOCK:
            if _REGISTRY is None:
                _REGISTRY = DomainComponentRegistry(strict=strict)
    elif force_reload:
        with _LOCK:
            _REGISTRY = DomainComponentRegistry(strict=strict)
    return _REGISTRY
