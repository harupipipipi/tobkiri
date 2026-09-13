"""
resolver.py - 共有辞書の解決エンジン

namespace/token から value を解決する。
循環検出、ホップ上限対応。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class ResolveResult:
    """解決結果"""
    original: str
    resolved: str
    hops: List[str]  # 解決パス
    cycle_detected: bool = False
    max_hops_reached: bool = False


@dataclass
class ExplainResult:
    """解決の説明"""
    original: str
    resolved: str
    hops: List[Dict[str, Any]]  # 各ホップの詳細
    cycle_detected: bool = False
    max_hops_reached: bool = False


class SharedDictResolver:
    """
    共有辞書解決エンジン
    
    namespace/token を解決して value を返す。
    """
    
    DEFAULT_MAX_HOPS = 10
    
    def __init__(self, snapshot=None, max_hops: int | None = None):
        self._snapshot = snapshot
        self._max_hops = max_hops or self.DEFAULT_MAX_HOPS
        self._lock = threading.RLock()
    
    def _now_ts(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    
    def _get_snapshot(self):
        """スナップショットを取得（遅延初期化）"""
        if self._snapshot is None:
            from .snapshot import get_shared_dict_snapshot
            self._snapshot = get_shared_dict_snapshot()
        return self._snapshot
    
    def resolve(
        self,
        namespace: str,
        token: str,
        context: Dict[str, Any] | None = None
    ) -> str:
        """
        token を解決して value を返す
        
        見つからなければ token をそのまま返す。
        循環検出時も token をそのまま返す。
        
        Args:
            namespace: 名前空間
            token: 解決するトークン
            context: 解決コンテキスト（将来の条件評価用、現在は未使用）
        
        Returns:
            解決後の値
        """
        result = self.resolve_chain(namespace, token, context)
        return result.resolved
    
    def resolve_chain(
        self,
        namespace: str,
        token: str,
        context: Dict[str, Any] | None = None
    ) -> ResolveResult:
        """
        チェーン解決（A→B→C）
        
        循環検出・ホップ上限付き。
        
        Returns:
            ResolveResult（解決パス付き）
        """
        with self._lock:
            snapshot = self._get_snapshot()
            
            visited = set()
            hops: List[str] = [token]
            current = token
            
            for _ in range(self._max_hops):
                if current in visited:
                    # 循環検出
                    return ResolveResult(
                        original=token,
                        resolved=token,  # 元の値を返す
                        hops=hops,
                        cycle_detected=True,
                    )
                
                visited.add(current)
                rule = snapshot.get_rule(namespace, current)
                
                if rule is None:
                    # 終端に達した
                    return ResolveResult(
                        original=token,
                        resolved=current,
                        hops=hops,
                    )
                
                current = rule.value
                hops.append(current)
            
            # ホップ上限に達した
            return ResolveResult(
                original=token,
                resolved=current,
                hops=hops,
                max_hops_reached=True,
            )
    
    def explain(
        self,
        namespace: str,
        token: str,
        context: Dict[str, Any] | None = None
    ) -> ExplainResult:
        """
        どのルールが適用されたかを説明
        
        Returns:
            ExplainResult（各ホップの詳細付き）
        """
        with self._lock:
            snapshot = self._get_snapshot()
            
            visited = set()
            hops: List[Dict[str, Any]] = []
            current = token
            
            for _ in range(self._max_hops):
                if current in visited:
                    # 循環検出
                    return ExplainResult(
                        original=token,
                        resolved=token,
                        hops=hops,
                        cycle_detected=True,
                    )
                
                visited.add(current)
                rule = snapshot.get_rule(namespace, current)
                
                if rule is None:
                    # 終端に達した
                    hops.append({
                        "token": current,
                        "value": None,
                        "rule_found": False,
                    })
                    return ExplainResult(
                        original=token,
                        resolved=current,
                        hops=hops,
                    )
                
                hops.append({
                    "token": current,
                    "value": rule.value,
                    "rule_found": True,
                    "provenance": rule.provenance,
                })
                
                current = rule.value
            
            # ホップ上限に達した
            return ExplainResult(
                original=token,
                resolved=current,
                hops=hops,
                max_hops_reached=True,
            )
    
    def has_rule(self, namespace: str, token: str) -> bool:
        """ルールが存在するかチェック"""
        snapshot = self._get_snapshot()
        return snapshot.get_rule(namespace, token) is not None
    
    def list_namespaces(self) -> List[str]:
        """全namespaceを取得"""
        snapshot = self._get_snapshot()
        return snapshot.get_namespaces()
    
    def list_rules(self, namespace: str) -> List[Dict[str, Any]]:
        """指定namespaceのルールを取得"""
        snapshot = self._get_snapshot()
        rules = snapshot.get_rules(namespace)
        return [r.to_dict() for r in rules]


# グローバルインスタンス
_global_resolver: Optional[SharedDictResolver] = None
_resolver_lock = threading.Lock()


def get_shared_dict_resolver() -> SharedDictResolver:
    """グローバルなSharedDictResolverを取得"""
    global _global_resolver
    if _global_resolver is None:
        with _resolver_lock:
            if _global_resolver is None:
                _global_resolver = SharedDictResolver()
    return _global_resolver


def reset_shared_dict_resolver(snapshot=None, max_hops: int | None = None) -> SharedDictResolver:
    """SharedDictResolverをリセット（テスト用）"""
    global _global_resolver
    with _resolver_lock:
        _global_resolver = SharedDictResolver(snapshot, max_hops)
    return _global_resolver
