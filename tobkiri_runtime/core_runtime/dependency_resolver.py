"""
dependency_resolver.py - Pack 依存関係解決

トポロジカルソート（Kahn's algorithm）で Pack のロード順序を決定する。
循環依存を検出してエラー（またはソフトフェイル）にする。

Wave 31: registry.py の 3 ソース統合ロジックを取り込み汎用化。
  - extract_dependencies(): pack-level の明示的依存を 3 ソースから抽出
  - resolve_load_order(): heapq ベース安定ソート + soft_circular モード追加
  - validate_dependencies(): 依存関係を検証しレポートを返す

Usage:
    from core_runtime.dependency_resolver import resolve_load_order

    packs = {
        "pack_a": {"depends_on": [{"pack_id": "pack_b"}]},
        "pack_b": {"dependencies": {"pack_c": {}}},
        "pack_c": {},
    }
    order = resolve_load_order(packs)
    # => ["pack_c", "pack_b", "pack_a"]
"""

from __future__ import annotations

import heapq
import logging
from typing import Any, Dict, List

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exception classes (interface unchanged)
# ---------------------------------------------------------------------------

class DependencyError(Exception):
    """依存関係エラー"""
    pass


class CircularDependencyError(DependencyError):
    """循環依存エラー"""
    pass


class MissingDependencyError(DependencyError):
    """依存先が見つからない"""
    pass


class VersionMismatchError(DependencyError):
    """依存先のバージョンが制約を満たさない"""
    pass


# ---------------------------------------------------------------------------
# extract_dependencies — pack-level 3 source extraction
# ---------------------------------------------------------------------------

def _normalize_dependency_spec(dep: Any) -> Dict[str, str] | None:
    if isinstance(dep, str):
        return {"pack_id": dep}
    if isinstance(dep, dict):
        pack_id = dep.get("pack_id") or dep.get("id") or dep.get("name")
        if not pack_id:
            return None
        spec = {"pack_id": str(pack_id)}
        version = dep.get("version") or dep.get("constraint") or dep.get("version_constraint")
        if version:
            spec["version"] = str(version)
        return spec
    return None


def extract_dependency_specs(pack_info: Dict[str, Any]) -> List[Dict[str, str]]:
    """Return normalized dependency specs preserving optional version constraints."""
    seen: set[str] = set()
    result: List[Dict[str, str]] = []

    def _add(dep: Any) -> None:
        spec = _normalize_dependency_spec(dep)
        if not spec:
            return
        pid = spec["pack_id"]
        if pid and pid not in seen:
            seen.add(pid)
            result.append(spec)

    raw_depends_on = pack_info.get("depends_on")
    if isinstance(raw_depends_on, list):
        for dep in raw_depends_on:
            _add(dep)
    elif isinstance(raw_depends_on, dict):
        for pid, raw_spec in raw_depends_on.items():
            if isinstance(raw_spec, dict):
                spec = dict(raw_spec)
                spec.setdefault("pack_id", pid)
                _add(spec)
            else:
                _add(
                    {"pack_id": pid, "version": raw_spec}
                    if isinstance(raw_spec, str) and raw_spec.strip()
                    else str(pid)
                )

    raw_dependencies = pack_info.get("dependencies")
    if isinstance(raw_dependencies, dict):
        for pid, raw_spec in raw_dependencies.items():
            if isinstance(raw_spec, dict):
                spec = dict(raw_spec)
                spec.setdefault("pack_id", pid)
                _add(spec)
            else:
                _add(
                    {"pack_id": pid, "version": raw_spec}
                    if isinstance(raw_spec, str) and raw_spec.strip()
                    else str(pid)
                )
    elif isinstance(raw_dependencies, list):
        for dep in raw_dependencies:
            _add(dep)

    return result


def _version_satisfies(actual: Any, constraint: str) -> bool:
    """Return whether a valid PEP 440 version satisfies ``constraint``.

    Dependency constraints are policy input.  Invalid versions or specifiers
    therefore fail closed instead of being coerced by a hand-written parser.
    """
    try:
        actual_version = Version(str(actual or "").strip())
        specifier = SpecifierSet(str(constraint or "").strip())
    except (InvalidSpecifier, InvalidVersion):
        return False
    return actual_version in specifier


def version_satisfies(actual: Any, constraint: str) -> bool:
    """Return whether ``actual`` satisfies a PEP 440 ``SpecifierSet``."""
    return _version_satisfies(actual, constraint)


def extract_dependencies(pack_info: Dict[str, Any]) -> List[str]:
    """
    単一 pack の manifest / ecosystem dict から依存 pack_id を抽出する。

    明示的な pack 依存を統合し、重複を排除してユニークなリストで返す。

    Sources:
        1. ``depends_on`` — 明示的依存。
           - list の場合: 要素が dict なら ``dep["pack_id"]``、str ならそのまま。
           - dict の場合: キーを pack_id として扱う。
        2. ``dependencies`` — ecosystem.json の dependencies フィールド。
           - dict の場合: キーを pack_id として扱う。
           - list の場合: 要素をそのまま pack_id として扱う。
    ``connectivity.requires`` は Pack ID ではなくグローバル契約 ID です。
    契約プロバイダの解決は capability/profile resolver が行うため、ここで
    pack dependency に混ぜると ``rumi.action.*`` を存在しない Pack として
    解釈してしまいます。

    Args:
        pack_info: pack の manifest / ecosystem dict

    Returns:
        依存 pack_id のユニークなリスト（出現順保持）
    """
    return [spec["pack_id"] for spec in extract_dependency_specs(pack_info)]


# ---------------------------------------------------------------------------
# _build_type_to_packs — component-level provides mapping
# ---------------------------------------------------------------------------

def _build_type_to_packs(packs: Dict[str, Dict[str, Any]]) -> Dict[str, set]:
    """
    全 pack の components から type → provider pack_id のマップを構築する。

    pack dict 内の ``components`` は ``{key: component_dict}`` 形式を想定。
    各 component_dict に ``connectivity.provides`` (list[str]) があればマッピングする。
    """
    type_to_packs: Dict[str, set] = {}
    for pack_id, manifest in packs.items():
        raw_components = manifest.get("components")
        if not isinstance(raw_components, dict):
            continue
        for _comp_key, comp_data in raw_components.items():
            if not isinstance(comp_data, dict):
                continue
            comp_conn = comp_data.get("connectivity")
            if not isinstance(comp_conn, dict):
                continue
            provides = comp_conn.get("provides")
            if isinstance(provides, list):
                for ptype in provides:
                    if isinstance(ptype, str) and ptype:
                        if ptype not in type_to_packs:
                            type_to_packs[ptype] = set()
                        type_to_packs[ptype].add(pack_id)
    return type_to_packs


def _collect_component_requires(
    pack_id: str,
    manifest: Dict[str, Any],
    type_to_packs: Dict[str, set],
) -> set:
    """
    pack 内の各 component の connectivity.requires から
    type_to_packs 経由で依存 pack_id を収集する。
    """
    deps: set = set()
    raw_components = manifest.get("components")
    if not isinstance(raw_components, dict):
        return deps
    for _comp_key, comp_data in raw_components.items():
        if not isinstance(comp_data, dict):
            continue
        comp_conn = comp_data.get("connectivity")
        if not isinstance(comp_conn, dict):
            continue
        requires = comp_conn.get("requires")
        if isinstance(requires, list):
            for req_type in requires:
                provider_packs = type_to_packs.get(req_type, set())
                for provider_id in provider_packs:
                    if provider_id != pack_id:
                        deps.add(provider_id)
    return deps


# ---------------------------------------------------------------------------
# resolve_load_order
# ---------------------------------------------------------------------------

def resolve_load_order(
    packs: Dict[str, Dict[str, Any]],
    strict: bool = False,
    soft_circular: bool = False,
) -> List[str]:
    """
    Pack の依存関係をトポロジカルソートで解決し、ロード順序を返す。

    依存関係ソース:
        1. ``depends_on`` — 明示的依存（リスト / dict）
        2. ``dependencies`` — ecosystem.json の dependencies
        3. ``connectivity.requires`` — pack レベル
        4. component-level ``connectivity.requires`` → type_to_packs 解決

    Args:
        packs: ``{pack_id: pack_manifest}`` の辞書。
        strict: True なら依存先不在またはバージョン不一致で raise。
                False なら依存先不在を warning ログしてスキップし、
                バージョン制約は順序計算に影響させない。
        soft_circular: 互換シグネチャ。循環は常に fail closed するため、
                       True を指定しても循環 Pack をロードしない。

    Returns:
        ロード順の pack_id リスト

    Raises:
        CircularDependencyError: 循環依存がある場合
        MissingDependencyError: strict=True で依存先がない場合
        VersionMismatchError: strict=True で依存先のバージョンが不一致の場合
    """
    all_pack_ids = set(packs.keys())
    if not all_pack_ids:
        return []

    # component-level provides → type_to_packs mapping
    type_to_packs = _build_type_to_packs(packs)

    # 隣接リスト構築（dep → dependants）
    graph: Dict[str, List[str]] = {pid: [] for pid in all_pack_ids}
    in_degree: Dict[str, int] = {pid: 0 for pid in all_pack_ids}

    for pid, manifest in packs.items():
        # pack-level dependencies (3 sources)
        dependency_specs = extract_dependency_specs(manifest)
        deps: set = {spec["pack_id"] for spec in dependency_specs}
        version_constraints = {
            spec["pack_id"]: spec["version"]
            for spec in dependency_specs
            if spec.get("version")
        }

        # component-level requires → type_to_packs resolution
        comp_deps = _collect_component_requires(pid, manifest, type_to_packs)
        deps.update(comp_deps)

        # 自己依存を除外
        deps.discard(pid)

        for dep_id in deps:
            if dep_id not in all_pack_ids:
                if strict:
                    raise MissingDependencyError(
                        "Pack '{}' depends on '{}' which is not installed".format(
                            pid, dep_id
                        )
                    )
                logger.warning(
                    "Pack '%s' depends on '%s' which is not installed (skipping dependency)",
                    pid,
                    dep_id,
                )
                continue
            constraint = version_constraints.get(dep_id)
            if strict and constraint and not _version_satisfies(
                packs[dep_id].get("version"), constraint
            ):
                raise VersionMismatchError(
                    "Pack '{}' requires '{}' version '{}', but installed "
                    "version is '{}'".format(
                        pid,
                        dep_id,
                        constraint,
                        packs[dep_id].get("version"),
                    )
                )
            graph[dep_id].append(pid)
            in_degree[pid] += 1

    # Kahn's algorithm with heapq for stable ordering
    heap = sorted(pid for pid, deg in in_degree.items() if deg == 0)
    heapq.heapify(heap)
    result: List[str] = []

    while heap:
        pid = heapq.heappop(heap)
        result.append(pid)
        for neighbor in graph.get(pid, []):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                heapq.heappush(heap, neighbor)

    if len(result) != len(all_pack_ids):
        remaining = sorted(all_pack_ids - set(result))
        if soft_circular:
            logger.warning(
                "soft_circular is ignored: dependency cycles always fail closed"
            )
        raise CircularDependencyError(
            "Circular dependency detected among: {{{}}}".format(
                ", ".join("'{}'".format(p) for p in remaining)
            )
        )

    return result


# ---------------------------------------------------------------------------
# validate_dependencies
# ---------------------------------------------------------------------------

def validate_dependencies(
    packs: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    全 pack の依存関係を検証し、問題のリストを返す。

    検出する問題:
        - missing: 依存先が packs に存在しない
        - self_dependency: 自分自身に依存している
        - circular: 循環依存が存在する
        - version_mismatch: 依存先のバージョンが制約を満たさない

    Args:
        packs: ``{pack_id: pack_manifest}`` の辞書

    Returns:
        問題のリスト。問題がなければ空リスト。
        各要素は dict で、``type`` キーに問題種別を持つ:
            - ``{"type": "missing", "pack_id": ..., "depends_on": ...}``
            - ``{"type": "self_dependency", "pack_id": ...}``
            - ``{"type": "circular", "packs": [...]}``
            - ``{"type": "version_mismatch", "pack_id": ..., ...}``
    """
    issues: List[Dict[str, Any]] = []
    all_pack_ids = set(packs.keys())

    # component-level provides → type_to_packs mapping
    type_to_packs = _build_type_to_packs(packs)

    # 隣接リスト（循環検出用）
    graph: Dict[str, List[str]] = {pid: [] for pid in all_pack_ids}
    in_degree: Dict[str, int] = {pid: 0 for pid in all_pack_ids}

    for pid, manifest in packs.items():
        dependency_specs = extract_dependency_specs(manifest)
        deps: set = {spec["pack_id"] for spec in dependency_specs}
        comp_deps = _collect_component_requires(pid, manifest, type_to_packs)
        deps.update(comp_deps)

        # self_dependency check
        if pid in deps:
            issues.append({"type": "self_dependency", "pack_id": pid})
            deps.discard(pid)

        for dep_id in deps:
            if dep_id not in all_pack_ids:
                issues.append({
                    "type": "missing",
                    "pack_id": pid,
                    "depends_on": dep_id,
                })
                continue
            for spec in dependency_specs:
                if spec["pack_id"] != dep_id or not spec.get("version"):
                    continue
                actual_version = packs.get(dep_id, {}).get("version")
                if not _version_satisfies(actual_version, spec["version"]):
                    issues.append({
                        "type": "version_mismatch",
                        "pack_id": pid,
                        "depends_on": dep_id,
                        "required": spec["version"],
                        "actual": actual_version,
                    })
            graph[dep_id].append(pid)
            in_degree[pid] += 1

    # Kahn's algorithm for circular detection
    heap = sorted(pid for pid, deg in in_degree.items() if deg == 0)
    heapq.heapify(heap)
    visited: List[str] = []

    while heap:
        pid = heapq.heappop(heap)
        visited.append(pid)
        for neighbor in graph.get(pid, []):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                heapq.heappush(heap, neighbor)

    if len(visited) != len(all_pack_ids):
        remaining = sorted(all_pack_ids - set(visited))
        issues.append({"type": "circular", "packs": remaining})

    return issues



# ---------------------------------------------------------------------------
# validate_rule_dependencies — Pack Type 固有の依存関係検証
# ---------------------------------------------------------------------------

def validate_rule_dependencies(
    packs: Dict[str, Dict[str, Any]],
    approval_manager: Any = None,
) -> List[Dict[str, Any]]:
    """
    Pack Type 固有の依存関係検証を行い、問題のリストを返す。

    検証内容:
        1. application Pack の depends_on に指定された rule Pack が
           通常承認 + ルール拡張承認済みであること。
        2. runtime_type が "binary" の application Pack が、
           binary を提供する rule Pack を depends_on に含むこと。

    approval_manager が None の場合、承認状態の検証はスキップし
    構造的な検証のみ行う。

    Args:
        packs: ``{pack_id: pack_manifest}`` の辞書
        approval_manager: ApprovalManager インスタンス（任意）

    Returns:
        問題のリスト。問題がなければ空リスト。
        各要素は dict で、``type`` キーに問題種別を持つ:
            - ``{"type": "rule_not_approved", "pack_id": ..., "rule_pack_id": ...}``
            - ``{"type": "rule_not_rule_approved", "pack_id": ..., "rule_pack_id": ...}``
            - ``{"type": "missing_binary_provider", "pack_id": ...}``
    """
    issues: List[Dict[str, Any]] = []
    all_pack_ids = set(packs.keys())

    # rule Pack の provides_runtime マッピングを構築
    runtime_providers: Dict[str, set] = {}  # runtime_name -> set of pack_ids
    for pid, manifest in packs.items():
        pt = manifest.get("pack_type", "application")
        if pt == "rule":
            provides = manifest.get("provides_runtime", [])
            if isinstance(provides, list):
                for rt in provides:
                    if isinstance(rt, str) and rt:
                        if rt not in runtime_providers:
                            runtime_providers[rt] = set()
                        runtime_providers[rt].add(pid)

    for pid, manifest in packs.items():
        pt = manifest.get("pack_type", "application")

        # --- 検証1: depends_on 内の rule Pack の承認状態 ---
        if approval_manager is not None:
            deps = extract_dependencies(manifest)
            for dep_id in deps:
                if dep_id not in all_pack_ids:
                    continue  # missing は validate_dependencies() が検出
                dep_manifest = packs.get(dep_id, {})
                dep_type = dep_manifest.get("pack_type", "application")
                if dep_type == "rule":
                    # 通常承認チェック
                    is_approved, reason = approval_manager.is_pack_approved_and_verified(dep_id)
                    if not is_approved:
                        issues.append({
                            "type": "rule_not_approved",
                            "pack_id": pid,
                            "rule_pack_id": dep_id,
                            "reason": reason,
                        })
                    # ルール拡張承認チェック
                    elif not approval_manager.is_rule_approved(dep_id):
                        issues.append({
                            "type": "rule_not_rule_approved",
                            "pack_id": pid,
                            "rule_pack_id": dep_id,
                        })

        # --- 検証2: binary runtime_type の Pack が binary provider を含むか ---
        if pt == "application":
            rt = manifest.get("runtime_type", "python")
            if rt == "binary":
                deps = extract_dependencies(manifest)
                has_binary_provider = False
                binary_providers = runtime_providers.get("binary", set())
                for dep_id in deps:
                    if dep_id in binary_providers:
                        has_binary_provider = True
                        break
                if not has_binary_provider:
                    issues.append({
                        "type": "missing_binary_provider",
                        "pack_id": pid,
                    })

    return issues
