"""
lib_executor.py - lib install/update 実行システム

Packの lib/install.py と lib/update.py を管理する。
全ての実行は SecureExecutor 経由で Docker 隔離される（strictモード）。

パス刷新:
- pack_subdir 基準で lib/ と backend/lib/ の両方を探索
- discover_pack_locations() ベースで全pack走査
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, TypedDict


from .paths import (
    LOCAL_PACK_ID,
    ECOSYSTEM_DIR,
    discover_pack_locations,
    get_pack_lib_dirs,
    find_ecosystem_json,
)


@dataclass
class LibExecutionRecord:
    """lib実行記録"""
    pack_id: str
    lib_type: str
    executed_at: str
    file_hash: str
    success: bool
    error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "lib_type": self.lib_type,
            "executed_at": self.executed_at,
            "file_hash": self.file_hash,
            "success": self.success,
            "error": self.error
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'LibExecutionRecord':
        return cls(
            pack_id=data.get("pack_id", ""),
            lib_type=data.get("lib_type", ""),
            executed_at=data.get("executed_at", ""),
            file_hash=data.get("file_hash", ""),
            success=data.get("success", False),
            error=data.get("error")
        )


@dataclass
class LibCheckResult:
    """lib実行チェック結果"""
    pack_id: str
    needs_install: bool
    needs_update: bool
    install_file: Optional[Path] = None
    update_file: Optional[Path] = None
    reason: str = ""


@dataclass
class LibExecutionResult:
    """lib実行結果"""
    pack_id: str
    lib_type: str
    success: bool
    output: Any = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    execution_time_ms: float = 0.0


class PackProcessingResults(TypedDict):
    """Aggregate result of processing every discovered pack library."""

    processed: int
    installed: list[str]
    updated: list[str]
    skipped: list[Dict[str, str]]
    failed: list[Dict[str, str | None]]
    errors: list[Dict[str, str]]


class LibExecutor:
    """lib実行管理クラス"""
    
    RECORDS_FILE = "user_data/settings/lib_execution_records.json"
    LIB_DIR_NAME = "lib"
    INSTALL_FILE = "install.py"
    UPDATE_FILE = "update.py"
    
    def __init__(self, records_file: str | None = None,
                 packs_dir: str = ECOSYSTEM_DIR):
        self._records_file = Path(records_file) if records_file else Path(self.RECORDS_FILE)
        self._records: Dict[str, LibExecutionRecord] = {}
        self._lock = threading.RLock()
        self._packs_dir = packs_dir
        self._load_records()
    
    def _now_ts(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    
    def _get_secure_executor(self):
        """SecureExecutor を取得（遅延インポート）"""
        from .secure_executor import get_secure_executor
        return get_secure_executor()
    
    def _load_records(self) -> None:
        if not self._records_file.exists():
            return
        try:
            with open(self._records_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for pack_id, record_data in data.get("records", {}).items():
                self._records[pack_id] = LibExecutionRecord.from_dict(record_data)
        except Exception as e:
            print(f"[LibExecutor] Failed to load records: {e}")
    
    def _save_records(self) -> None:
        try:
            self._records_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": "1.0",
                "updated_at": self._now_ts(),
                "records": {pack_id: record.to_dict() for pack_id, record in self._records.items()}
            }
            with open(self._records_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[LibExecutor] Failed to save records: {e}")
    
    def _compute_file_hash(self, file_path: Path) -> str:
        if not file_path.exists():
            return ""
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()
    
    def _find_lib_dir(self, pack_dir: Path, pack_subdir: Optional[Path] = None) -> Optional[Path]:
        """
        pack_subdir 基準で lib ディレクトリを探索。
        paths.get_pack_lib_dirs() を使い候補順に返す。
        """
        if pack_subdir is None:
            _, pack_subdir = find_ecosystem_json(pack_dir)
            if pack_subdir is None:
                pack_subdir = pack_dir  # フォールバック
        
        lib_dirs = get_pack_lib_dirs(pack_subdir)
        if lib_dirs:
            return lib_dirs[0]  # 最初に見つかった候補
        return None
    
    def check_pack(self, pack_id: str, pack_dir: Path, pack_subdir: Optional[Path] = None) -> LibCheckResult:
        """
        Pack の lib 実行要否をチェック
        
        local_pack は常にスキップ。
        
        Args:
            pack_id: Pack ID
            pack_dir: Packのルートディレクトリ
            pack_subdir: pack_subdir (省略時は内部で探索)
        """
        result = LibCheckResult(pack_id=pack_id, needs_install=False, needs_update=False)
        
        # local_pack は lib をサポートしない
        if pack_id == LOCAL_PACK_ID:
            result.reason = "local_pack does not support lib execution"
            return result
        
        lib_dir = self._find_lib_dir(pack_dir, pack_subdir)
        if not lib_dir:
            result.reason = "No lib directory found"
            return result
        
        install_file = lib_dir / self.INSTALL_FILE
        update_file = lib_dir / self.UPDATE_FILE
        if install_file.exists():
            result.install_file = install_file
        if update_file.exists():
            result.update_file = update_file
        if not result.install_file and not result.update_file:
            result.reason = "No install.py or update.py found"
            return result
        
        with self._lock:
            existing_record = self._records.get(pack_id)
        
        if existing_record is None:
            if result.install_file:
                result.needs_install = True
                result.reason = "First time installation"
            return result
        
        if result.install_file:
            current_hash = self._compute_file_hash(result.install_file)
            if current_hash != existing_record.file_hash:
                if result.update_file:
                    result.needs_update = True
                    result.reason = "File hash changed, update needed"
                else:
                    result.needs_install = True
                    result.reason = "File hash changed, re-install needed"
        
        if not result.needs_install and not result.needs_update:
            result.reason = "No changes detected"
        return result
    
    def execute_lib(self, pack_id: str, lib_file: Path, lib_type: str,
                    context: Dict[str, Any] | None = None) -> LibExecutionResult:
        """
        lib を SecureExecutor 経由で実行
        
        全ての実行は Docker 隔離される（strictモード）。
        permissive モードでは警告付きでホスト実行。
        """
        # local_pack チェック
        if pack_id == LOCAL_PACK_ID:
            result = LibExecutionResult(
                pack_id=pack_id,
                lib_type=lib_type,
                success=False,
                error="local_pack does not support lib execution",
                error_type="local_pack_skip"
            )
            self._log_execution(pack_id, lib_type, False, result.error, "skipped")
            return result
        
        if not lib_file.exists():
            result = LibExecutionResult(
                pack_id=pack_id,
                lib_type=lib_type,
                success=False,
                error=f"File not found: {lib_file}",
                error_type="file_not_found"
            )
            self._log_execution(pack_id, lib_type, False, result.error, "rejected")
            return result
        
        # 承認チェック
        try:
            from .approval_manager import get_approval_manager
            am = get_approval_manager()
            
            # 承認 + ハッシュ検証
            is_valid, reason = am.is_pack_approved_and_verified(pack_id)
            if not is_valid:
                result = LibExecutionResult(
                    pack_id=pack_id,
                    lib_type=lib_type,
                    success=False,
                    error=f"Pack not approved or modified: {reason}",
                    error_type=reason
                )
                self._log_execution(pack_id, lib_type, False, result.error, "rejected")
                return result
                
        except Exception as e:
            result = LibExecutionResult(
                pack_id=pack_id,
                lib_type=lib_type,
                success=False,
                error=f"Approval check failed: {e}",
                error_type="approval_check_error"
            )
            self._log_execution(pack_id, lib_type, False, result.error, "rejected")
            return result
        
        # SecureExecutor 経由で実行
        secure_executor = self._get_secure_executor()
        exec_result = secure_executor.execute_lib(
            pack_id=pack_id,
            lib_type=lib_type,
            lib_file=lib_file,
            context=context
        )
        
        # ExecutionResult を LibExecutionResult に変換
        result = LibExecutionResult(
            pack_id=pack_id,
            lib_type=lib_type,
            success=exec_result.success,
            output=exec_result.output,
            error=exec_result.error,
            error_type=exec_result.error_type,
            execution_time_ms=exec_result.execution_time_ms
        )
        
        # 実行記録を保存
        file_hash = self._compute_file_hash(lib_file)
        self._save_execution_record(pack_id, lib_type, file_hash, result.success, result.error)
        self._log_execution(pack_id, lib_type, result.success, result.error, exec_result.execution_mode)
        
        return result
    
    def _save_execution_record(self, pack_id: str, lib_type: str, file_hash: str, success: bool, error: Optional[str]) -> None:
        with self._lock:
            self._records[pack_id] = LibExecutionRecord(
                pack_id=pack_id,
                lib_type=lib_type,
                executed_at=self._now_ts(),
                file_hash=file_hash,
                success=success,
                error=error or ""
            )
            self._save_records()
    
    def _log_execution(self, pack_id: str, lib_type: str, success: bool, error: Optional[str], execution_mode: str = "unknown") -> None:
        """監査ログに lib 実行を記録"""
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            audit.log_system_event(
                event_type=f"lib_{lib_type}",
                success=success,
                details={
                    "pack_id": pack_id,
                    "lib_type": lib_type,
                    "execution_mode": execution_mode
                },
                error=error or ""
            )
        except Exception:
            pass
    
    def process_all_packs(
        self, packs_dir: Path, context: Dict[str, Any] | None = None
    ) -> PackProcessingResults:
        results: PackProcessingResults = {
            "processed": 0,
            "installed": [],
            "updated": [],
            "skipped": [],
            "failed": [],
            "errors": []
        }
        
        locations = discover_pack_locations(str(packs_dir))
        
        for loc in locations:
            pack_id = loc.pack_id
            pack_dir = loc.pack_dir
            
            # local_pack はスキップ（明示的に）
            if pack_id == LOCAL_PACK_ID:
                results["skipped"].append({"pack_id": pack_id, "reason": "local_pack does not support lib"})
                continue
            
            results["processed"] += 1
            
            try:
                check_result = self.check_pack(pack_id, pack_dir, pack_subdir=loc.pack_subdir)
                if check_result.needs_install and check_result.install_file:
                    exec_result = self.execute_lib(pack_id, check_result.install_file, "install", context)
                    if exec_result.success:
                        results["installed"].append(pack_id)
                    else:
                        results["failed"].append({"pack_id": pack_id, "lib_type": "install", "error": exec_result.error})
                elif check_result.needs_update and check_result.update_file:
                    exec_result = self.execute_lib(pack_id, check_result.update_file, "update", context)
                    if exec_result.success:
                        results["updated"].append(pack_id)
                    else:
                        results["failed"].append({"pack_id": pack_id, "lib_type": "update", "error": exec_result.error})
                else:
                    results["skipped"].append({"pack_id": pack_id, "reason": check_result.reason})
            except Exception as e:
                results["errors"].append({"pack_id": pack_id, "error": str(e)})
        return results
    
    def get_record(self, pack_id: str) -> Optional[LibExecutionRecord]:
        with self._lock:
            return self._records.get(pack_id)
    
    def get_all_records(self) -> Dict[str, LibExecutionRecord]:
        with self._lock:
            return dict(self._records)
    
    def clear_record(self, pack_id: str) -> bool:
        with self._lock:
            if pack_id in self._records:
                del self._records[pack_id]
                self._save_records()
                return True
            return False
    
    def clear_all_records(self) -> int:
        with self._lock:
            count = len(self._records)
            self._records.clear()
            self._save_records()
            return count


_global_lib_executor: Optional[LibExecutor] = None
_lib_lock = threading.Lock()


def get_lib_executor() -> LibExecutor:
    """
    グローバルなLibExecutorを取得する。

    DI コンテナ経由で遅延初期化・キャッシュされる。
    """
    from .di_container import get_container
    return get_container().get("lib_executor")


def reset_lib_executor(records_file: str | None = None) -> LibExecutor:
    """LibExecutorをリセット（テスト用）"""
    global _global_lib_executor
    from .di_container import get_container
    container = get_container()
    new = LibExecutor(records_file)
    with _lib_lock:
        _global_lib_executor = new
    container.set_instance("lib_executor", new)
    return new
