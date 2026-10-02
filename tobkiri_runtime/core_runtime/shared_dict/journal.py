"""
journal.py - 共有辞書のジャーナル管理

全ての操作（提案/採用/拒否/無効化）を追記ログとして記録する。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class ProposalStatus(Enum):
    """提案ステータス"""
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CONFLICT = "conflict"
    CYCLE_DETECTED = "cycle_detected"


@dataclass
class ProposalResult:
    """提案結果"""
    status: ProposalStatus
    namespace: str
    token: str
    value: str
    reason: Optional[str] = None
    
    @property
    def accepted(self) -> bool:
        return self.status == ProposalStatus.ACCEPTED


@dataclass
class JournalEntry:
    """ジャーナルエントリ"""
    ts: str
    action: str  # propose, remove, clear
    namespace: str
    token: str
    value: str
    result: str  # accepted, rejected, conflict, cycle_detected
    reason: Optional[str] = None
    provenance: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        d = {
            "ts": self.ts,
            "action": self.action,
            "namespace": self.namespace,
            "token": self.token,
            "value": self.value,
            "result": self.result,
            "provenance": self.provenance,
        }
        if self.reason:
            d["reason"] = self.reason
        return d
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'JournalEntry':
        return cls(
            ts=data.get("ts", ""),
            action=data.get("action", ""),
            namespace=data.get("namespace", ""),
            token=data.get("token", ""),
            value=data.get("value", ""),
            result=data.get("result", ""),
            reason=data.get("reason"),
            provenance=data.get("provenance", {}),
        )


class SharedDictJournal:
    """
    共有辞書ジャーナル管理
    
    全ての操作を journal.jsonl に追記する。
    """
    
    DEFAULT_PATH = "user_data/settings/shared_dict/journal.jsonl"
    
    def __init__(self, journal_path: str | None = None, snapshot=None):
        self._path = Path(journal_path) if journal_path else Path(self.DEFAULT_PATH)
        self._lock = threading.RLock()
        self._snapshot = snapshot
    
    def _now_ts(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    
    def _get_snapshot(self):
        """スナップショットを取得（遅延初期化）"""
        if self._snapshot is None:
            from .snapshot import get_shared_dict_snapshot
            self._snapshot = get_shared_dict_snapshot()
        return self._snapshot
    
    def _append_entry(self, entry: JournalEntry) -> None:
        """エントリをジャーナルに追記"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            with open(self._path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        except IOError as e:
            print(f"[SharedDictJournal] Append error: {e}")
        
        # 監査ログにも記録
        self._log_to_audit(entry)
    
    def _log_to_audit(self, entry: JournalEntry) -> None:
        """監査ログに記録"""
        try:
            from ..audit_logger import get_audit_logger
            audit = get_audit_logger()
            audit.log_system_event(
                event_type=f"shared_dict_{entry.action}",
                success=entry.result == "accepted",
                details={
                    "namespace": entry.namespace,
                    "token": entry.token,
                    "value": entry.value,
                    "result": entry.result,
                    "reason": entry.reason,
                    "provenance": entry.provenance,
                }
            )
        except Exception:
            pass
    
    def _check_cycle(self, namespace: str, token: str, value: str, max_hops: int = 10) -> bool:
        """
        循環を検出
        
        token -> value -> ... -> token となるパスがあれば True
        """
        snapshot = self._get_snapshot()
        visited = {token}
        current = value
        
        for _ in range(max_hops):
            if current in visited:
                return True  # 循環検出
            
            visited.add(current)
            rule = snapshot.get_rule(namespace, current)
            if rule is None:
                return False  # 終端に達した
            
            current = rule.value
        
        # ホップ上限に達した（潜在的な循環）
        return True
    
    def propose(
        self,
        namespace: str,
        token: str,
        value: str,
        provenance: Dict[str, Any] | None = None
    ) -> ProposalResult:
        """
        ルールを提案
        
        衝突チェック、循環チェックを行い、結果を返す。
        """
        with self._lock:
            provenance = provenance or {}
            provenance.setdefault("ts", self._now_ts())
            
            # 循環チェック
            if self._check_cycle(namespace, token, value):
                entry = JournalEntry(
                    ts=self._now_ts(),
                    action="propose",
                    namespace=namespace,
                    token=token,
                    value=value,
                    result="cycle_detected",
                    reason=f"Cycle detected: {token} -> {value} creates a loop",
                    provenance=provenance,
                )
                self._append_entry(entry)
                
                return ProposalResult(
                    status=ProposalStatus.CYCLE_DETECTED,
                    namespace=namespace,
                    token=token,
                    value=value,
                    reason=entry.reason,
                )
            
            # スナップショットに追加を試みる
            snapshot = self._get_snapshot()
            success = snapshot.add_rule(
                namespace=namespace,
                token=token,
                value=value,
                conditions={},
                provenance=provenance,
            )
            
            if success:
                entry = JournalEntry(
                    ts=self._now_ts(),
                    action="propose",
                    namespace=namespace,
                    token=token,
                    value=value,
                    result="accepted",
                    provenance=provenance,
                )
                self._append_entry(entry)
                
                return ProposalResult(
                    status=ProposalStatus.ACCEPTED,
                    namespace=namespace,
                    token=token,
                    value=value,
                )
            else:
                # 衝突
                existing = snapshot.get_rule(namespace, token)
                entry = JournalEntry(
                    ts=self._now_ts(),
                    action="propose",
                    namespace=namespace,
                    token=token,
                    value=value,
                    result="conflict",
                    reason=f"Conflict: token '{token}' already maps to '{existing.value if existing else '?'}'",
                    provenance=provenance,
                )
                self._append_entry(entry)
                
                return ProposalResult(
                    status=ProposalStatus.CONFLICT,
                    namespace=namespace,
                    token=token,
                    value=value,
                    reason=entry.reason,
                )
    
    def remove(
        self,
        namespace: str,
        token: str,
        provenance: Dict[str, Any] | None = None
    ) -> bool:
        """ルールを削除"""
        with self._lock:
            provenance = provenance or {}
            provenance.setdefault("ts", self._now_ts())
            
            snapshot = self._get_snapshot()
            existing = snapshot.get_rule(namespace, token)
            
            if existing is None:
                return False
            
            success = snapshot.remove_rule(namespace, token)
            
            entry = JournalEntry(
                ts=self._now_ts(),
                action="remove",
                namespace=namespace,
                token=token,
                value=existing.value,
                result="accepted" if success else "rejected",
                provenance=provenance,
            )
            self._append_entry(entry)
            
            return success
    
    def get_history(
        self,
        namespace: str | None = None,
        token: str | None = None,
        limit: int = 100
    ) -> List[JournalEntry]:
        """履歴を取得"""
        entries: List[JournalEntry] = []
        
        if not self._path.exists():
            return entries
        
        try:
            with open(self._path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    
                    try:
                        data = json.loads(line)
                        entry = JournalEntry.from_dict(data)
                        
                        # フィルタ
                        if namespace and entry.namespace != namespace:
                            continue
                        if token and entry.token != token:
                            continue
                        
                        entries.append(entry)
                    except (json.JSONDecodeError, Exception):
                        continue
        except IOError:
            pass
        
        # 最新のlimit件を返す
        return entries[-limit:] if len(entries) > limit else entries


# グローバルインスタンス
_global_journal: Optional[SharedDictJournal] = None
_journal_lock = threading.Lock()


def get_shared_dict_journal() -> SharedDictJournal:
    """グローバルなSharedDictJournalを取得"""
    global _global_journal
    if _global_journal is None:
        with _journal_lock:
            if _global_journal is None:
                _global_journal = SharedDictJournal()
    return _global_journal


def reset_shared_dict_journal(journal_path: str | None = None, snapshot=None) -> SharedDictJournal:
    """SharedDictJournalをリセット（テスト用）"""
    global _global_journal
    with _journal_lock:
        _global_journal = SharedDictJournal(journal_path, snapshot)
    return _global_journal
