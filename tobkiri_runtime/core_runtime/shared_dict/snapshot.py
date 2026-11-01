"""
snapshot.py - 共有辞書のスナップショット管理

snapshot.json の読み書きを行う。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class RuleEntry:
    """辞書ルールエントリ"""
    token: str
    value: str
    conditions: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "token": self.token,
            "value": self.value,
            "conditions": self.conditions,
            "provenance": self.provenance,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'RuleEntry':
        return cls(
            token=data.get("token", ""),
            value=data.get("value", ""),
            conditions=data.get("conditions", {}),
            provenance=data.get("provenance", {}),
        )


class SharedDictSnapshot:
    """
    共有辞書スナップショット管理
    
    snapshot.json の読み書きを行う。
    """
    
    DEFAULT_PATH = "user_data/settings/shared_dict/snapshot.json"
    
    def __init__(self, snapshot_path: str | None = None):
        self._path = Path(snapshot_path) if snapshot_path else Path(self.DEFAULT_PATH)
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = self._create_empty()
        self._load()
    
    def _now_ts(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    
    def _load(self) -> None:
        """スナップショットを読み込む"""
        with self._lock:
            if self._path.exists():
                try:
                    with open(self._path, 'r', encoding='utf-8') as f:
                        self._data = json.load(f)
                except (json.JSONDecodeError, IOError) as e:
                    print(f"[SharedDictSnapshot] Load error: {e}")
                    self._data = self._create_empty()
            else:
                self._data = self._create_empty()
    
    def _create_empty(self) -> Dict[str, Any]:
        """空のスナップショットを作成"""
        return {
            "version": "1.0",
            "created_at": self._now_ts(),
            "updated_at": self._now_ts(),
            "namespaces": {}
        }
    
    def _save(self) -> None:
        """スナップショットを保存"""
        with self._lock:
            self._data["updated_at"] = self._now_ts()
            
            # ディレクトリを作成
            self._path.parent.mkdir(parents=True, exist_ok=True)
            
            try:
                with open(self._path, 'w', encoding='utf-8') as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
            except IOError as e:
                print(f"[SharedDictSnapshot] Save error: {e}")
    
    def get_namespaces(self) -> List[str]:
        """全namespaceを取得"""
        with self._lock:
            return list(self._data.get("namespaces", {}).keys())
    
    def get_rules(self, namespace: str) -> List[RuleEntry]:
        """指定namespaceのルールを取得"""
        with self._lock:
            ns_data = self._data.get("namespaces", {}).get(namespace, {})
            rules_raw = ns_data.get("rules", [])
            return [RuleEntry.from_dict(r) for r in rules_raw]
    
    def get_rule(self, namespace: str, token: str) -> Optional[RuleEntry]:
        """指定namespace/tokenのルールを取得"""
        rules = self.get_rules(namespace)
        for rule in rules:
            if rule.token == token:
                return rule
        return None
    
    def add_rule(
        self,
        namespace: str,
        token: str,
        value: str,
        conditions: Dict[str, Any] | None = None,
        provenance: Dict[str, Any] | None = None
    ) -> bool:
        """
        ルールを追加
        
        既に同じtoken/valueが存在する場合は何もしない。
        同じtokenで異なるvalueが存在する場合はFalseを返す（衝突）。
        """
        with self._lock:
            if "namespaces" not in self._data:
                self._data["namespaces"] = {}
            
            if namespace not in self._data["namespaces"]:
                self._data["namespaces"][namespace] = {"rules": []}
            
            rules = self._data["namespaces"][namespace]["rules"]
            
            # 既存ルールをチェック
            for rule in rules:
                if rule.get("token") == token:
                    if rule.get("value") == value:
                        # 同じルールが既に存在
                        return True
                    else:
                        # 衝突
                        return False
            
            # 新しいルールを追加
            new_rule = {
                "token": token,
                "value": value,
                "conditions": conditions or {},
                "provenance": provenance or {},
            }
            rules.append(new_rule)
            self._save()
            return True
    
    def remove_rule(self, namespace: str, token: str) -> bool:
        """ルールを削除"""
        with self._lock:
            if namespace not in self._data.get("namespaces", {}):
                return False
            
            rules = self._data["namespaces"][namespace]["rules"]
            original_len = len(rules)
            
            self._data["namespaces"][namespace]["rules"] = [
                r for r in rules if r.get("token") != token
            ]
            
            if len(self._data["namespaces"][namespace]["rules"]) < original_len:
                self._save()
                return True
            return False
    
    def clear_namespace(self, namespace: str) -> bool:
        """namespaceをクリア"""
        with self._lock:
            if namespace in self._data.get("namespaces", {}):
                del self._data["namespaces"][namespace]
                self._save()
                return True
            return False
    
    def get_all_data(self) -> Dict[str, Any]:
        """全データを取得"""
        with self._lock:
            return dict(self._data)
    
    def reload(self) -> None:
        """スナップショットを再読み込み"""
        self._load()


# グローバルインスタンス
_global_snapshot: Optional[SharedDictSnapshot] = None
_snapshot_lock = threading.Lock()


def get_shared_dict_snapshot() -> SharedDictSnapshot:
    """グローバルなSharedDictSnapshotを取得"""
    global _global_snapshot
    if _global_snapshot is None:
        with _snapshot_lock:
            if _global_snapshot is None:
                _global_snapshot = SharedDictSnapshot()
    return _global_snapshot


def reset_shared_dict_snapshot(snapshot_path: str | None = None) -> SharedDictSnapshot:
    """SharedDictSnapshotをリセット（テスト用）"""
    global _global_snapshot
    with _snapshot_lock:
        _global_snapshot = SharedDictSnapshot(snapshot_path)
    return _global_snapshot
