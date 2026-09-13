"""
audit_logger.py - 監査ログシステム (DI Container 対応)

Flow実行、modifier適用、python_file_call、権限操作などの
監査ログを永続化する。

設計原則:
- 全ての重要な操作を記録
- 拒否理由を明確に記録
- JSON Lines形式で永続化
- ローテーション対応

Wave 19-A 変更:
  VULN-H05: to_json() で ensure_ascii=True を設定し、
  非ASCII文字によるログインジェクション・文字化けを防止
"""

from __future__ import annotations

import atexit
import json
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Literal


AuditCategory = Literal[
    "flow_execution",
    "modifier_application", 
    "python_file_call",
    "approval",
    "permission",
    "network",
    "security",
    "system"
]

AuditSeverity = Literal["info", "warning", "error", "critical"]


def _json_safe(value: Any) -> Any:
    """Return a JSON-serializable representation for audit log payloads."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


@dataclass
class AuditEntry:
    """監査ログエントリ"""
    ts: str
    category: AuditCategory
    severity: AuditSeverity
    action: str
    success: bool
    
    # コンテキスト情報
    flow_id: Optional[str] = None
    step_id: Optional[str] = None
    phase: Optional[str] = None
    owner_pack: Optional[str] = None
    modifier_id: Optional[str] = None
    
    # 詳細情報
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    error_type: Optional[str] = None
    
    # セキュリティ関連
    rejection_reason: Optional[str] = None
    execution_mode: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """辞書に変換(None値は除外)"""
        d = asdict(self)
        return {k: _json_safe(v) for k, v in d.items() if v is not None}
    
    def to_json(self) -> str:
        """JSON文字列に変換 (VULN-H05: ensure_ascii=True)"""
        return json.dumps(self.to_dict(), ensure_ascii=True)


class AuditLogger:
    """
    監査ログ管理クラス
    
    監査ログをファイルに永続化し、検索・取得機能を提供する。
    """
    
    DEFAULT_AUDIT_DIR = "user_data/audit"
    
    def __init__(self, audit_dir: Optional[str] = None):
        self._audit_dir = Path(audit_dir) if audit_dir else Path(self.DEFAULT_AUDIT_DIR)
        self._lock = threading.RLock()
        self._buffer: List[AuditEntry] = []
        self._buffer_size = 100
        self._ensure_dir()
        
        # 終了時にフラッシュ
        atexit.register(self._atexit_flush)
    
    def _now_ts(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    
    def _today_str(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    def _ensure_dir(self) -> None:
        """監査ログディレクトリを作成"""
        self._audit_dir.mkdir(parents=True, exist_ok=True)
    
    def _get_log_file(self, category: str) -> Path:
        """カテゴリ別のログファイルパスを取得"""
        return self._audit_dir / f"{category}_{self._today_str()}.jsonl"

    def _get_log_file_for_entry(self, category: str, entry_ts: str) -> Path:
        """
        エントリの ts から日付を抽出し、カテゴリ×日付のファイルパスを返す。
        ts が不正/解析不能の場合は今日の日付にフォールバック (fail-soft)。
        """
        date_str = self._extract_date_from_ts(entry_ts)
        return self._audit_dir / f"{category}_{date_str}.jsonl"

    def _extract_date_from_ts(self, ts: str) -> str:
        """ISO8601 タイムスタンプから YYYY-MM-DD を抽出。失敗時は今日。"""
        if ts and len(ts) >= 10:
            candidate = ts[:10]
            if len(candidate) == 10 and candidate[4] == "-" and candidate[7] == "-":
                try:
                    int(candidate[:4])
                    int(candidate[5:7])
                    int(candidate[8:10])
                    return candidate
                except (ValueError, IndexError):
                    pass
        return self._today_str()

    
    def log(self, entry: AuditEntry) -> None:
        """監査ログを記録"""
        with self._lock:
            self._buffer.append(entry)
            
            if len(self._buffer) >= self._buffer_size:
                self._flush_buffer()
    
    def _flush_buffer(self) -> None:
        """バッファをファイルに書き出し（D-1: エントリの ts で日付振り分け）"""
        if not self._buffer:
            return
        
        # カテゴリ×日付別にグループ化
        by_file: Dict[Path, List[AuditEntry]] = {}
        for entry in self._buffer:
            log_file = self._get_log_file_for_entry(entry.category, entry.ts)
            if log_file not in by_file:
                by_file[log_file] = []
            by_file[log_file].append(entry)
        
        # 各ファイルに書き出し
        for log_file, entries in by_file.items():
            try:
                with open(log_file, "a", encoding="utf-8") as f:
                    for entry in entries:
                        f.write(entry.to_json() + "\n")
            except Exception as e:
                print(f"[AuditLogger] Failed to write to {log_file}: {e}")
        
        self._buffer.clear()
    
    def flush(self) -> None:
        """バッファを強制フラッシュ"""
        with self._lock:
            self._flush_buffer()
    
    def _atexit_flush(self) -> None:
        """終了時のフラッシュ(例外を握りつぶす)"""
        try:
            self.flush()
        except Exception:
            pass
    
    def log_flow_execution(
        self,
        flow_id: str,
        success: bool,
        step_count: int = 0,
        execution_time_ms: float = 0,
        error: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ) -> None:
        """Flow実行ログを記録"""
        entry = AuditEntry(
            ts=self._now_ts(),
            category="flow_execution",
            severity="info" if success else "error",
            action="execute_flow",
            success=success,
            flow_id=flow_id,
            error=error,
            details={
                "step_count": step_count,
                "execution_time_ms": execution_time_ms,
                **(details or {})
            }
        )
        self.log(entry)
    
    def log_modifier_application(
        self,
        modifier_id: str,
        target_flow_id: str,
        action: str,
        success: bool,
        target_step_id: Optional[str] = None,
        skipped_reason: Optional[str] = None,
        error: Optional[str] = None
    ) -> None:
        """modifier適用ログを記録"""
        entry = AuditEntry(
            ts=self._now_ts(),
            category="modifier_application",
            severity="info" if success else ("warning" if skipped_reason else "error"),
            action=f"modifier_{action}",
            success=success,
            flow_id=target_flow_id,
            step_id=target_step_id,
            modifier_id=modifier_id,
            rejection_reason=skipped_reason,
            error=error
        )
        self.log(entry)
    
    def log_python_file_call(
        self,
        flow_id: str,
        step_id: str,
        phase: str,
        owner_pack: str,
        file_path: str,
        success: bool,
        execution_mode: str,
        execution_time_ms: float = 0,
        error: Optional[str] = None,
        error_type: Optional[str] = None,
        rejection_reason: Optional[str] = None,
        warnings: Optional[List[str]] = None
    ) -> None:
        """python_file_call実行ログを記録"""
        severity: AuditSeverity = "info"
        if not success:
            if rejection_reason:
                severity = "warning"
            else:
                severity = "error"
        
        entry = AuditEntry(
            ts=self._now_ts(),
            category="python_file_call",
            severity=severity,
            action="execute_python_file",
            success=success,
            flow_id=flow_id,
            step_id=step_id,
            phase=phase,
            owner_pack=owner_pack,
            execution_mode=execution_mode,
            error=error,
            error_type=error_type,
            rejection_reason=rejection_reason,
            details={
                "file": file_path,
                "execution_time_ms": execution_time_ms,
                "warnings": warnings or []
            }
        )
        self.log(entry)
    
    def log_approval_event(
        self,
        pack_id: str,
        action: str,
        success: bool,
        previous_status: Optional[str] = None,
        new_status: Optional[str] = None,
        reason: Optional[str] = None,
        error: Optional[str] = None
    ) -> None:
        """承認イベントログを記録"""
        entry = AuditEntry(
            ts=self._now_ts(),
            category="approval",
            severity="info" if success else "error",
            action=f"approval_{action}",
            success=success,
            owner_pack=pack_id,
            error=error,
            details={
                "previous_status": previous_status,
                "new_status": new_status,
                "reason": reason
            }
        )
        self.log(entry)
    
    def log_permission_event(
        self,
        pack_id: str,
        permission_type: str,
        action: str,
        success: bool,
        details: Optional[Dict[str, Any]] = None,
        rejection_reason: Optional[str] = None
    ) -> None:
        """権限イベントログを記録"""
        entry = AuditEntry(
            ts=self._now_ts(),
            category="permission",
            severity="info" if success else "warning",
            action=f"permission_{action}",
            success=success,
            owner_pack=pack_id,
            rejection_reason=rejection_reason,
            details={
                "permission_type": permission_type,
                **(details or {})
            }
        )
        self.log(entry)
    
    def log_network_event(
        self,
        pack_id: str,
        domain: str,
        port: int,
        allowed: bool,
        reason: Optional[str] = None,
        request_details: Optional[Dict[str, Any]] = None
    ) -> None:
        """ネットワークイベントログを記録"""
        # severity を allowed に基づいて設定
        severity: AuditSeverity = "info" if allowed else "warning"
        
        entry = AuditEntry(
            ts=self._now_ts(),
            category="network",
            severity=severity,
            action="network_request",
            success=allowed,  # allowed を success として記録
            owner_pack=pack_id,
            rejection_reason=reason if not allowed else None,
            details={
                "domain": domain,
                "port": port,
                "allowed": allowed,  # 明示的に allowed を記録
                **(request_details or {})
            }
        )
        self.log(entry)
    
    def log_security_event(
        self,
        event_type: str,
        severity: AuditSeverity,
        description: str,
        pack_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ) -> None:
        """セキュリティイベントログを記録"""
        entry = AuditEntry(
            ts=self._now_ts(),
            category="security",
            severity=severity,
            action=event_type,
            success=severity in ("info", "warning"),
            owner_pack=pack_id,
            details={
                "description": description,
                **(details or {})
            }
        )
        self.log(entry)
    
    def log_system_event(
        self,
        event_type: str,
        success: bool,
        details: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None
    ) -> None:
        """システムイベントログを記録"""
        entry = AuditEntry(
            ts=self._now_ts(),
            category="system",
            severity="info" if success else "error",
            action=event_type,
            success=success,
            error=error,
            details=details or {}
        )
        self.log(entry)
    
    def query_logs(
        self,
        category: Optional[AuditCategory] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        pack_id: Optional[str] = None,
        flow_id: Optional[str] = None,
        success_only: Optional[bool] = None,
        limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """
        監査ログを検索
        
        Args:
            category: カテゴリでフィルタ
            start_date: 開始日(YYYY-MM-DD)
            end_date: 終了日(YYYY-MM-DD)
            pack_id: Pack IDでフィルタ
            flow_id: Flow IDでフィルタ
            success_only: 成功のみ(True)、失敗のみ(False)、全て(None)
            limit: 最大取得件数
        
        Returns:
            ログエントリのリスト
        """
        self.flush()
        
        results: List[Dict[str, Any]] = []
        
        # 対象ファイルを特定
        if category:
            pattern = f"{category}_*.jsonl"
        else:
            pattern = "*.jsonl"
        
        log_files = sorted(self._audit_dir.glob(pattern), reverse=True)
        
        for log_file in log_files:
            # 日付フィルタ
            file_date = self._extract_date_from_filename(log_file.name)
            if file_date:
                if start_date and file_date < start_date:
                    continue
                if end_date and file_date > end_date:
                    continue
            
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if len(results) >= limit:
                            break
                        
                        try:
                            entry = json.loads(line.strip())
                        except json.JSONDecodeError:
                            continue
                        
                        # フィルタ適用
                        if pack_id and entry.get("owner_pack") != pack_id:
                            continue
                        if flow_id and entry.get("flow_id") != flow_id:
                            continue
                        if success_only is not None and entry.get("success") != success_only:
                            continue
                        
                        results.append(entry)
                
                if len(results) >= limit:
                    break
                    
            except Exception as e:
                print(f"[AuditLogger] Failed to read {log_file}: {e}")
        
        return results
    
    def _extract_date_from_filename(self, filename: str) -> Optional[str]:
        """ファイル名から日付を抽出"""
        # category_YYYY-MM-DD.jsonl のパターン
        parts = filename.replace(".jsonl", "").split("_")
        if len(parts) >= 2:
            date_part = parts[-1]
            if len(date_part) == 10 and date_part[4] == "-" and date_part[7] == "-":
                return date_part
        return None
    
    def get_summary(
        self,
        category: Optional[AuditCategory] = None,
        date: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        監査ログのサマリーを取得
        
        Returns:
            カテゴリ別・成功/失敗別の集計
        """
        date = date or self._today_str()
        
        summary: Dict[str, Any] = {
            "date": date,
            "categories": {},
            "total_entries": 0,
            "total_success": 0,
            "total_failure": 0,
        }
        
        if category:
            categories = [category]
        else:
            categories = ["flow_execution", "modifier_application", "python_file_call", 
                         "approval", "permission", "network", "security", "system"]
        
        for cat in categories:
            log_file = self._audit_dir / f"{cat}_{date}.jsonl"
            cat_summary: Dict[str, int] = {"success": 0, "failure": 0, "total": 0}
            
            if log_file.exists():
                try:
                    with open(log_file, "r", encoding="utf-8") as f:
                        for line in f:
                            try:
                                entry = json.loads(line.strip())
                                cat_summary["total"] += 1
                                if entry.get("success"):
                                    cat_summary["success"] += 1
                                else:
                                    cat_summary["failure"] += 1
                            except json.JSONDecodeError:
                                continue
                except Exception:
                    pass
            
            summary["categories"][cat] = cat_summary
            summary["total_entries"] += cat_summary["total"]
            summary["total_success"] += cat_summary["success"]
            summary["total_failure"] += cat_summary["failure"]
        
        return summary
    
    def cleanup_old_logs(self, days_to_keep: int = 30) -> int:
        """
        古いログファイルを削除
        
        Args:
            days_to_keep: 保持する日数
        
        Returns:
            削除したファイル数
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_to_keep)).strftime("%Y-%m-%d")
        deleted = 0
        
        for log_file in self._audit_dir.glob("*.jsonl"):
            file_date = self._extract_date_from_filename(log_file.name)
            if file_date and file_date < cutoff:
                try:
                    log_file.unlink()
                    deleted += 1
                except Exception as e:
                    print(f"[AuditLogger] Failed to delete {log_file}: {e}")
        
        return deleted


def get_audit_logger() -> AuditLogger:
    """
    グローバルな AuditLogger を取得する。

    DI コンテナ経由で遅延初期化・キャッシュされる。

    Returns:
        AuditLogger インスタンス
    """
    from .di_container import get_container
    return get_container().get("audit_logger")


def reset_audit_logger(audit_dir: Optional[str] = None) -> AuditLogger:
    """
    AuditLogger をリセットする（テスト用）。

    既存インスタンスを flush した後、新しいインスタンスで置き換える。

    Args:
        audit_dir: 監査ログディレクトリ（省略時はデフォルト）

    Returns:
        新しい AuditLogger インスタンス
    """
    from .di_container import get_container
    container = get_container()
    old = container.get_or_none("audit_logger")
    if old is not None:
        old.flush()
    new_instance = AuditLogger(audit_dir)
    container.set_instance("audit_logger", new_instance)
    return new_instance
