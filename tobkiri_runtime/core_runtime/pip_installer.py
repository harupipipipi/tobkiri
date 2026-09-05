"""
pip_installer.py - Pack 依存ライブラリ導入システム

Pack が同梱する requirements.lock を候補として検出し、
ユーザーが API で承認すると、ビルダー用 Docker コンテナで
PyPI から取得・展開する。

永続化: capability_installer と同型
  user_data/pip/requests/requests.jsonl   (イベントログ)
  user_data/pip/requests/index.json       (最新状態)
  user_data/pip/requests/blocked.json     (ブロック状態)

candidate_key = "{pack_id}:{requirements_relpath}:{sha256(requirements.lock)}"

状態: pending | installed | rejected | blocked | failed
cooldown: 3600秒 (1時間)
reject 3回で blocked
"""

from __future__ import annotations

import os
import ipaddress
import re

import hashlib
import json
import subprocess
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .paths import (
    ECOSYSTEM_DIR,
    PACK_DATA_BASE_DIR,
    discover_pack_locations,
)


# ======================================================================
# 定数
# ======================================================================

PIP_REQUESTS_DIR = "user_data/pip/requests"
PIP_REQUESTS_JSONL = "user_data/pip/requests/requests.jsonl"
PIP_INDEX_FILE = "user_data/pip/requests/index.json"
PIP_BLOCKED_FILE = "user_data/pip/requests/blocked.json"

COOLDOWN_SECONDS = 3600          # 1時間
REJECT_THRESHOLD = 3             # 3回で blocked

DEFAULT_INDEX_URL = "https://pypi.org/simple"
BUILDER_IMAGE = "python:3.11-slim"

# requirements.lock 探索候補 (pack_subdir 基準)
REQUIREMENTS_LOCK_CANDIDATES = [
    "requirements.lock",
    "backend/requirements.lock",
]


# ======================================================================
# C-3: requirements.lock 検証
# ======================================================================

# NAME==VERSION のみ許可 (PEP 440 バージョン, extras 対応)
_LOCK_LINE_PATTERN = re.compile(
    r'^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?(\[[A-Za-z0-9,._-]+\])?\s*==\s*[^\s]+$'
)

# 禁止パターン
_LOCK_FORBIDDEN_PATTERNS = [
    re.compile(r'^\s*-e\b'),          # editable
    re.compile(r'^\s*--'),             # pip options
    re.compile(r'git\+', re.I),       # VCS
    re.compile(r'https?://', re.I),    # URL
    re.compile(r'file:', re.I),         # local file
    re.compile(r'\.\.\/'),           # relative path
    re.compile(r'^\s*/'),              # absolute path
    re.compile(r'\s+@\s+'),          # direct reference (PEP 440)
]


def validate_requirements_lock(file_path: Path) -> Tuple[bool, Optional[str]]:
    """
    requirements.lock の内容を検証。

    許可: NAME==VERSION 行のみ（コメント/空行は可）
    禁止: URL, VCS, ローカル参照, pip オプション, @ direct ref

    Returns:
        (valid, error_reason)
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        return False, f"Cannot read file: {e}"

    for line_num, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()

        # 空行・コメント行はOK
        if not line or line.startswith("#"):
            continue

        # 禁止パターンチェック
        for pattern in _LOCK_FORBIDDEN_PATTERNS:
            if pattern.search(line):
                return False, (
                    f"Line {line_num}: forbidden pattern detected: {raw_line!r}"
                )

        # NAME==VERSION 形式チェック
        if not _LOCK_LINE_PATTERN.match(line):
            return False, (
                f"Line {line_num}: not a valid NAME==VERSION format: {raw_line!r}"
            )

    return True, None


def validate_index_url(index_url: str) -> Tuple[bool, Optional[str]]:
    """
    index_url を検証。

    - https のみ許可
    - hostname が内部IP / localhost の場合は拒否

    Returns:
        (valid, error_reason)
    """
    if not index_url:
        return True, None

    from urllib.parse import urlparse
    try:
        parsed = urlparse(index_url)
    except Exception as e:
        return False, f"Invalid URL: {e}"

    if parsed.scheme != "https":
        return False, f"index_url must use https, got: {parsed.scheme}"

    hostname = parsed.hostname or ""

    # localhost チェック
    if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return False, f"index_url hostname is localhost/loopback: {hostname}"

    # IP リテラルチェック
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False, f"index_url hostname is internal/private IP: {hostname}"
    except ValueError:
        pass  # hostname（ドメイン名） — OK

    return True, None


# 状態定数
STATUS_PENDING = "pending"
STATUS_INSTALLED = "installed"
STATUS_REJECTED = "rejected"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"


# ======================================================================
# データクラス
# ======================================================================

@dataclass
class PipCandidate:
    """pip 候補の状態"""
    candidate_key: str
    pack_id: str
    requirements_relpath: str
    requirements_sha256: str
    status: str = STATUS_PENDING
    created_at: str = ""
    updated_at: str = ""
    reject_count: int = 0
    cooldown_until: Optional[str] = None
    last_error: Optional[str] = None
    allow_sdist: bool = False
    index_url: str = DEFAULT_INDEX_URL
    notes: str = ""
    actor: str = ""
    reject_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PipCandidate":
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)


@dataclass
class ScanResult:
    """スキャン結果"""
    scanned_count: int = 0
    pending_created: int = 0
    skipped_blocked: int = 0
    skipped_cooldown: int = 0
    skipped_installed: int = 0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class InstallResult:
    """インストール結果"""
    success: bool
    candidate_key: str = ""
    pack_id: str = ""
    status: str = ""
    site_packages_path: str = ""
    packages: List[Dict[str, str]] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RejectResult:
    """却下結果"""
    success: bool
    candidate_key: str = ""
    status: str = ""
    reject_count: int = 0
    cooldown_until: str = ""
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class UnblockResult:
    """ブロック解除結果"""
    success: bool
    candidate_key: str = ""
    status: str = ""
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ======================================================================
# PipInstaller 本体
# ======================================================================

class PipInstaller:
    """
    Pack 依存ライブラリ導入マネージャ

    スレッドセーフ: threading.RLock で保護
    """

    def __init__(
        self,
        requests_dir: Optional[str] = None,
        ecosystem_dir: Optional[str] = None,
    ):
        self._requests_dir = Path(requests_dir or PIP_REQUESTS_DIR)
        self._ecosystem_dir = ecosystem_dir or ECOSYSTEM_DIR
        self._lock = threading.RLock()

        # インメモリ状態
        self._index: Dict[str, PipCandidate] = {}
        self._blocked: Dict[str, Dict[str, Any]] = {}

        self._ensure_dirs()
        self._load_state()

    # ------------------------------------------------------------------
    # 時刻ヘルパー
    # ------------------------------------------------------------------

    @staticmethod
    def _now_ts() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_ts(ts_str: str) -> datetime:
        """ISO 8601 文字列を datetime に変換"""
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        return datetime.fromisoformat(ts_str)

    # ------------------------------------------------------------------
    # ファイル I/O
    # ------------------------------------------------------------------

    def _ensure_dirs(self) -> None:
        self._requests_dir.mkdir(parents=True, exist_ok=True)

    def _jsonl_path(self) -> Path:
        return self._requests_dir / "requests.jsonl"

    def _index_path(self) -> Path:
        return self._requests_dir / "index.json"

    def _blocked_path(self) -> Path:
        return self._requests_dir / "blocked.json"

    def _load_state(self) -> None:
        """永続化された状態を読み込む"""
        # index.json
        idx_path = self._index_path()
        if idx_path.exists():
            try:
                with open(idx_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for key, item in data.get("items", {}).items():
                    self._index[key] = PipCandidate.from_dict(item)
            except Exception:
                pass

        # blocked.json
        blk_path = self._blocked_path()
        if blk_path.exists():
            try:
                with open(blk_path, "r", encoding="utf-8") as f:
                    self._blocked = json.load(f)
            except Exception:
                self._blocked = {}

    def _save_index(self) -> None:
        """index.json を保存"""
        data = {
            "version": "1.0",
            "updated_at": self._now_ts(),
            "items": {k: v.to_dict() for k, v in self._index.items()},
        }
        with open(self._index_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _save_blocked(self) -> None:
        """blocked.json を保存"""
        with open(self._blocked_path(), "w", encoding="utf-8") as f:
            json.dump(self._blocked, f, ensure_ascii=False, indent=2)

    def _append_event(self, event: Dict[str, Any]) -> None:
        """requests.jsonl にイベントを追記"""
        event.setdefault("ts", self._now_ts())
        try:
            with open(self._jsonl_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # candidate_key 生成
    # ------------------------------------------------------------------

    @staticmethod
    def compute_file_sha256(file_path: Path) -> str:
        """ファイルの SHA-256 を計算"""
        sha = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha.update(chunk)
        return sha.hexdigest()

    @staticmethod
    def build_candidate_key(pack_id: str, relpath: str, sha256: str) -> str:
        return f"{pack_id}:{relpath}:{sha256}"

    @staticmethod
    def parse_candidate_key(key: str) -> Tuple[str, str, str]:
        """candidate_key を (pack_id, relpath, sha256) に分解"""
        parts = key.split(":")
        if len(parts) < 3:
            raise ValueError(f"Invalid candidate_key: {key}")
        pack_id = parts[0]
        sha256 = parts[-1]
        relpath = ":".join(parts[1:-1])
        return pack_id, relpath, sha256

    # ------------------------------------------------------------------
    # requirements.lock 探索
    # ------------------------------------------------------------------

    def _find_requirements_lock(self, pack_subdir: Path) -> Optional[Tuple[Path, str]]:
        """
        pack_subdir 内で requirements.lock を探索

        Returns:
            (絶対パス, pack_subdir からの相対パス) or None
        """
        for relpath in REQUIREMENTS_LOCK_CANDIDATES:
            candidate = pack_subdir / relpath
            if candidate.exists() and candidate.is_file():
                return candidate, relpath
        return None

    # ------------------------------------------------------------------
    # スキャン
    # ------------------------------------------------------------------

    def scan_candidates(self, ecosystem_dir: Optional[str] = None) -> ScanResult:
        """
        全 Pack を走査し、requirements.lock を検出して pending を生成
        """
        result = ScanResult()
        eco_dir = ecosystem_dir or self._ecosystem_dir

        try:
            locations = discover_pack_locations(eco_dir)
        except Exception as e:
            result.errors.append(f"discover_pack_locations failed: {e}")
            return result

        now = self._now_ts()

        with self._lock:
            for loc in locations:
                result.scanned_count += 1
                try:
                    found = self._find_requirements_lock(loc.pack_subdir)
                    if found is None:
                        continue

                    lock_path, relpath = found

                    # C-3: scan 時点でも lock 内容を検証
                    lock_valid, lock_err = validate_requirements_lock(lock_path)
                    if not lock_valid:
                        result.errors.append(
                            f"Pack {loc.pack_id}: invalid requirements.lock: {lock_err}"
                        )
                        self._audit_log("pip_requirements_lock_invalid", False, {
                            "pack_id": loc.pack_id,
                            "relpath": relpath,
                            "reason": lock_err,
                        })
                        continue

                    sha256 = self.compute_file_sha256(lock_path)
                    ckey = self.build_candidate_key(loc.pack_id, relpath, sha256)

                    # blocked チェック
                    if ckey in self._blocked:
                        result.skipped_blocked += 1
                        continue

                    # 既存エントリチェック
                    existing = self._index.get(ckey)
                    if existing:
                        if existing.status == STATUS_INSTALLED:
                            result.skipped_installed += 1
                            continue
                        if existing.status == STATUS_BLOCKED:
                            result.skipped_blocked += 1
                            continue
                        if existing.status == STATUS_REJECTED:
                            # cooldown チェック
                            if existing.cooldown_until:
                                try:
                                    cd = self._parse_ts(existing.cooldown_until)
                                    now_dt = self._parse_ts(now)
                                    if now_dt < cd:
                                        result.skipped_cooldown += 1
                                        continue
                                except Exception:
                                    pass
                            # cooldown 過ぎたら pending に戻す
                            existing.status = STATUS_PENDING
                            existing.updated_at = now
                            self._save_index()
                            self._append_event({
                                "event": "pip_request_pending_again",
                                "candidate_key": ckey,
                                "pack_id": loc.pack_id,
                            })
                            result.pending_created += 1
                            continue
                        if existing.status == STATUS_PENDING:
                            # 既に pending
                            continue
                        if existing.status == STATUS_FAILED:
                            # failed → pending に戻す
                            existing.status = STATUS_PENDING
                            existing.updated_at = now
                            self._save_index()
                            result.pending_created += 1
                            continue

                    # 新規 pending 作成
                    candidate = PipCandidate(
                        candidate_key=ckey,
                        pack_id=loc.pack_id,
                        requirements_relpath=relpath,
                        requirements_sha256=sha256,
                        status=STATUS_PENDING,
                        created_at=now,
                        updated_at=now,
                    )
                    self._index[ckey] = candidate
                    self._save_index()
                    self._append_event({
                        "event": "pip_request_created",
                        "candidate_key": ckey,
                        "pack_id": loc.pack_id,
                        "requirements_relpath": relpath,
                        "requirements_sha256": sha256,
                    })
                    self._audit_log("pip_request_created", True, {
                        "pack_id": loc.pack_id,
                        "candidate_key": ckey,
                        "requirements_relpath": relpath,
                        "requirements_sha256": sha256,
                    })
                    result.pending_created += 1

                except Exception as e:
                    result.errors.append(f"Pack {loc.pack_id}: {e}")

        # scan 完了を audit に記録
        self._audit_log("pip_scan_completed", True, {
            "scanned_count": result.scanned_count,
            "pending_created": result.pending_created,
            "skipped_blocked": result.skipped_blocked,
            "skipped_cooldown": result.skipped_cooldown,
            "skipped_installed": result.skipped_installed,
            "error_count": len(result.errors),
        })

        return result

    # ------------------------------------------------------------------
    # 一覧
    # ------------------------------------------------------------------

    def list_items(self, status_filter: str = "all") -> List[Dict[str, Any]]:
        """候補を一覧"""
        with self._lock:
            items = []
            for cand in self._index.values():
                if status_filter != "all" and cand.status != status_filter:
                    continue
                items.append(cand.to_dict())
            return items

    def list_blocked(self) -> Dict[str, Any]:
        """ブロック一覧"""
        with self._lock:
            return dict(self._blocked)


    # ------------------------------------------------------------------
    # Pack 承認チェック (C-2)
    # ------------------------------------------------------------------

    @staticmethod
    def _check_pack_approval(pack_id: str) -> Tuple[bool, Optional[str]]:
        """
        Pack が承認済みかチェック。
        strict モードでは ApprovalManager が利用不可でも拒否。
        permissive モードでは ApprovalManager 不在時のみ許可 (fail-soft)。

        Returns:
            (approved, reason)
        """
        security_mode = os.environ.get("RUMI_SECURITY_MODE", "strict").lower()

        try:
            from .approval_manager import get_approval_manager, PackStatus
            am = get_approval_manager()
            status = am.get_status(pack_id)
            if status is None:
                return False, f"Pack '{pack_id}' not found in approval registry"
            if status == PackStatus.APPROVED:
                return True, None
            if status == PackStatus.MODIFIED:
                return False, f"Pack '{pack_id}' has been modified since approval"
            if status == PackStatus.BLOCKED:
                return False, f"Pack '{pack_id}' is blocked"
            return False, f"Pack '{pack_id}' is not approved (status: {status.value})"
        except ImportError:
            if security_mode == "permissive":
                return True, None
            return False, "ApprovalManager not available (required in strict mode)"
        except Exception as e:
            return False, f"Approval check failed: {e}"

    # ------------------------------------------------------------------
    # approve + install
    # ------------------------------------------------------------------

    def approve_and_install(
        self,
        candidate_key: str,
        actor: str = "api_user",
        allow_sdist: bool = False,
        index_url: str = DEFAULT_INDEX_URL,
    ) -> InstallResult:
        """
        候補を承認し、ビルダーコンテナで pip install を実行
        """
        with self._lock:
            cand = self._index.get(candidate_key)
            if cand is None:
                return InstallResult(
                    success=False,
                    candidate_key=candidate_key,
                    error="Candidate not found",
                )
            if cand.status == STATUS_INSTALLED:
                # C-4: idempotent — already installed is success
                site_packages_dir = Path(PACK_DATA_BASE_DIR) / cand.pack_id / "python" / "site-packages"
                return InstallResult(
                    success=True,
                    candidate_key=candidate_key,
                    pack_id=cand.pack_id,
                    status=STATUS_INSTALLED,
                    site_packages_path=str(site_packages_dir) if site_packages_dir.is_dir() else "",
                )
            if cand.status == STATUS_BLOCKED:
                return InstallResult(
                    success=False,
                    candidate_key=candidate_key,
                    status=STATUS_BLOCKED,
                    error="Candidate is blocked",
                )


            # C-2: Pack approval gate
            approved, reason = self._check_pack_approval(cand.pack_id)
            if not approved:
                cand.status = STATUS_FAILED
                cand.last_error = reason
                cand.updated_at = self._now_ts()
                self._save_index()
                self._audit_log("pip_install_rejected_unapproved", False, {
                    "pack_id": cand.pack_id,
                    "candidate_key": candidate_key,
                    "reason": reason,
                })
                return InstallResult(
                    success=False,
                    candidate_key=candidate_key,
                    pack_id=cand.pack_id,
                    status=STATUS_FAILED,
                    error=reason,
                )

            cand.allow_sdist = allow_sdist
            cand.index_url = index_url
            cand.actor = actor

        # install 実行 (ロック外 — I/O が長い)
        self._audit_log("pip_install_started", True, {
            "pack_id": cand.pack_id,
            "candidate_key": candidate_key,
            "allow_sdist": allow_sdist,
            "index_url": index_url,
        })

        pack_id, relpath, sha256 = self.parse_candidate_key(candidate_key)

        # C-3: index_url 検証
        url_valid, url_err = validate_index_url(index_url)
        if not url_valid:
            with self._lock:
                cand.status = STATUS_FAILED
                cand.last_error = url_err
                cand.updated_at = self._now_ts()
                self._save_index()
            self._audit_log("pip_install_rejected_bad_index_url", False, {
                "pack_id": pack_id,
                "candidate_key": candidate_key,
                "index_url": index_url,
                "reason": url_err,
            })
            return InstallResult(
                success=False, candidate_key=candidate_key,
                pack_id=pack_id, status=STATUS_FAILED, error=url_err,
            )


        # パス確定
        pack_data_dir = Path(PACK_DATA_BASE_DIR) / pack_id
        pack_data_dir.mkdir(parents=True, exist_ok=True)
        wheelhouse_dir = pack_data_dir / "python" / "wheelhouse"
        site_packages_dir = pack_data_dir / "python" / "site-packages"
        wheelhouse_dir.mkdir(parents=True, exist_ok=True)
        site_packages_dir.mkdir(parents=True, exist_ok=True)

        # pack_subdir を探す
        pack_subdir = self._resolve_pack_subdir(pack_id)
        if pack_subdir is None:
            error_msg = f"Pack subdir not found for {pack_id}"
            with self._lock:
                cand.status = STATUS_FAILED
                cand.last_error = error_msg
                cand.updated_at = self._now_ts()
                self._save_index()
            self._audit_log("pip_install_failed", False, {
                "pack_id": pack_id,
                "candidate_key": candidate_key,
                "error": error_msg,
            })
            return InstallResult(
                success=False,
                candidate_key=candidate_key,
                pack_id=pack_id,
                status=STATUS_FAILED,
                error=error_msg,
            )

        # Docker 実行
        try:

            # C-3: TOCTOU — requirements.lock 再検証
            lock_file = pack_subdir / relpath
            if not lock_file.exists():
                raise RuntimeError(f"requirements.lock disappeared: {lock_file}")

            # SHA-256 TOCTOU チェック
            current_sha = self.compute_file_sha256(lock_file)
            if current_sha != sha256:
                raise RuntimeError(
                    f"requirements.lock SHA-256 changed since scan "
                    f"(expected: {sha256[:16]}..., got: {current_sha[:16]}...)"
                )

            # 内容再検証
            lock_valid, lock_err = validate_requirements_lock(lock_file)
            if not lock_valid:
                raise RuntimeError(
                    f"requirements.lock content invalid on re-check: {lock_err}"
                )

            # Stage 1: download
            dl_ok, dl_err = self._docker_pip_download(
                pack_subdir=pack_subdir,
                pack_data_dir=pack_data_dir,
                requirements_relpath=relpath,
                allow_sdist=allow_sdist,
                index_url=index_url,
            )
            if not dl_ok:
                raise RuntimeError(f"pip download failed: {dl_err}")

            # Stage 2: install (offline)
            inst_ok, inst_err = self._docker_pip_install(
                pack_subdir=pack_subdir,
                pack_data_dir=pack_data_dir,
                requirements_relpath=relpath,
            )
            if not inst_ok:
                raise RuntimeError(f"pip install failed: {inst_err}")

            # Stage 3: packages 列挙
            packages = self._docker_list_packages(pack_data_dir)

            # Stage 4: state.json 作成
            state = {
                "candidate_key": candidate_key,
                "requirements_sha256": sha256,
                "allow_sdist": allow_sdist,
                "index_url": index_url,
                "installed_at": self._now_ts(),
                "packages": packages,
            }
            state_path = pack_data_dir / "python" / "state.json"
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)

            # 成功
            with self._lock:
                cand.status = STATUS_INSTALLED
                cand.updated_at = self._now_ts()
                cand.last_error = None
                self._save_index()
                self._append_event({
                    "event": "pip_install_completed",
                    "candidate_key": candidate_key,
                    "pack_id": pack_id,
                    "packages_count": len(packages),
                })

            self._audit_log("pip_install_completed", True, {
                "pack_id": pack_id,
                "candidate_key": candidate_key,
                "site_packages_path": str(site_packages_dir),
                "packages_count": len(packages),
                "allow_sdist": allow_sdist,
                "index_url": index_url,
            })

            return InstallResult(
                success=True,
                candidate_key=candidate_key,
                pack_id=pack_id,
                status=STATUS_INSTALLED,
                site_packages_path=str(site_packages_dir),
                packages=packages,
            )

        except Exception as e:
            error_msg = str(e)
            with self._lock:
                cand.status = STATUS_FAILED
                cand.last_error = error_msg
                cand.updated_at = self._now_ts()
                self._save_index()
                self._append_event({
                    "event": "pip_install_failed",
                    "candidate_key": candidate_key,
                    "pack_id": pack_id,
                    "error": error_msg,
                })

            self._audit_log("pip_install_failed", False, {
                "pack_id": pack_id,
                "candidate_key": candidate_key,
                "error": error_msg,
                "allow_sdist": allow_sdist,
                "index_url": index_url,
            })

            return InstallResult(
                success=False,
                candidate_key=candidate_key,
                pack_id=pack_id,
                status=STATUS_FAILED,
                error=error_msg,
            )

    # ------------------------------------------------------------------
    # reject
    # ------------------------------------------------------------------

    def reject(
        self,
        candidate_key: str,
        actor: str = "api_user",
        reason: str = "",
    ) -> RejectResult:
        """候補を却下"""
        with self._lock:
            cand = self._index.get(candidate_key)
            if cand is None:
                return RejectResult(
                    success=False,
                    candidate_key=candidate_key,
                    error="Candidate not found",
                )

            now = self._now_ts()
            cand.reject_count += 1
            cand.reject_reason = reason
            cand.actor = actor
            cand.updated_at = now

            # cooldown 設定
            cd_dt = self._parse_ts(now)
            cd_until = (cd_dt + timedelta(seconds=COOLDOWN_SECONDS)).isoformat().replace("+00:00", "Z")
            cand.cooldown_until = cd_until

            if cand.reject_count >= REJECT_THRESHOLD:
                # blocked へ
                cand.status = STATUS_BLOCKED
                self._blocked[candidate_key] = {
                    "candidate_key": candidate_key,
                    "pack_id": cand.pack_id,
                    "blocked_at": now,
                    "reject_count": cand.reject_count,
                    "reason": reason,
                }
                self._save_blocked()
                self._append_event({
                    "event": "pip_request_blocked",
                    "candidate_key": candidate_key,
                    "pack_id": cand.pack_id,
                    "reject_count": cand.reject_count,
                    "reason": reason,
                })
                self._audit_log("pip_request_blocked", True, {
                    "pack_id": cand.pack_id,
                    "candidate_key": candidate_key,
                    "reject_count": cand.reject_count,
                    "reason": reason,
                })
            else:
                cand.status = STATUS_REJECTED
                self._append_event({
                    "event": "pip_request_rejected",
                    "candidate_key": candidate_key,
                    "pack_id": cand.pack_id,
                    "reject_count": cand.reject_count,
                    "reason": reason,
                })
                self._audit_log("pip_request_rejected", True, {
                    "pack_id": cand.pack_id,
                    "candidate_key": candidate_key,
                    "reject_count": cand.reject_count,
                    "reason": reason,
                })

            self._save_index()

            return RejectResult(
                success=True,
                candidate_key=candidate_key,
                status=cand.status,
                reject_count=cand.reject_count,
                cooldown_until=cd_until,
            )

    # ------------------------------------------------------------------
    # unblock
    # ------------------------------------------------------------------

    def unblock(
        self,
        candidate_key: str,
        actor: str = "api_user",
        reason: str = "",
    ) -> UnblockResult:
        """ブロック解除"""
        with self._lock:
            if candidate_key not in self._blocked:
                # index にも blocked があるかチェック
                cand = self._index.get(candidate_key)
                if cand is None or cand.status != STATUS_BLOCKED:
                    return UnblockResult(
                        success=False,
                        candidate_key=candidate_key,
                        error="Candidate is not blocked",
                    )

            # blocked 辞書から削除
            self._blocked.pop(candidate_key, None)
            self._save_blocked()

            cand = self._index.get(candidate_key)
            if cand:
                now = self._now_ts()
                cand.status = STATUS_PENDING
                cand.reject_count = 0
                cand.updated_at = now
                # unblock 直後も 1h cooldown (抑制)
                cd_dt = self._parse_ts(now)
                cand.cooldown_until = (
                    cd_dt + timedelta(seconds=COOLDOWN_SECONDS)
                ).isoformat().replace("+00:00", "Z")
                self._save_index()

            self._append_event({
                "event": "pip_unblocked",
                "candidate_key": candidate_key,
                "actor": actor,
                "reason": reason,
            })
            self._audit_log("pip_unblocked", True, {
                "candidate_key": candidate_key,
                "actor": actor,
                "reason": reason,
            })

            return UnblockResult(
                success=True,
                candidate_key=candidate_key,
                status=STATUS_PENDING,
            )

    # ------------------------------------------------------------------
    # Docker 実行 (ビルダーコンテナ)
    # ------------------------------------------------------------------

    def _resolve_pack_subdir(self, pack_id: str) -> Optional[Path]:
        """pack_id から pack_subdir を解決"""
        locations = discover_pack_locations(self._ecosystem_dir)
        for loc in locations:
            if loc.pack_id == pack_id:
                return loc.pack_subdir
        return None

    def _docker_pip_download(
        self,
        pack_subdir: Path,
        pack_data_dir: Path,
        requirements_relpath: str,
        allow_sdist: bool,
        index_url: str,
    ) -> Tuple[bool, Optional[str]]:
        """
        Stage 1: pip download をビルダーコンテナで実行

        --network=bridge (download に必要)
        """
        cmd = [
            "docker", "run", "--rm",
            "--network=bridge",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--read-only",
            "--tmpfs=/tmp:size=256m,nosuid",
            "--memory=512m",
            "--memory-swap=512m",
            "--cpus=1.0",
            "--pids-limit=100",
            "--user=65534:65534",
            "-v", f"{pack_data_dir.resolve()}:/data:rw",
            "-v", f"{pack_subdir.resolve()}:/src:ro",
            "--label", "rumi.managed=true",
            "--label", "rumi.type=pip_builder",
            BUILDER_IMAGE,
            "pip", "download",
            "-r", f"/src/{requirements_relpath}",
            "-d", "/data/python/wheelhouse",
            "-i", index_url,
        ]

        if not allow_sdist:
            cmd.append("--only-binary=:all:")

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if proc.returncode != 0:
                return False, proc.stderr or f"Exit code {proc.returncode}"
            return True, None
        except subprocess.TimeoutExpired:
            return False, "pip download timed out (300s)"
        except Exception as e:
            return False, str(e)

    def _docker_pip_install(
        self,
        pack_subdir: Path,
        pack_data_dir: Path,
        requirements_relpath: str,
    ) -> Tuple[bool, Optional[str]]:
        """
        Stage 2: pip install (offline) をビルダーコンテナで実行

        --network=none (オフラインインストール)
        """
        cmd = [
            "docker", "run", "--rm",
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--read-only",
            "--tmpfs=/tmp:size=256m,nosuid",
            "--memory=512m",
            "--memory-swap=512m",
            "--cpus=1.0",
            "--pids-limit=100",
            "--user=65534:65534",
            "-v", f"{pack_data_dir.resolve()}:/data:rw",
            "-v", f"{pack_subdir.resolve()}:/src:ro",
            "--label", "rumi.managed=true",
            "--label", "rumi.type=pip_builder",
            BUILDER_IMAGE,
            "pip", "install",
            "--no-index",
            "--find-links", "/data/python/wheelhouse",
            "-r", f"/src/{requirements_relpath}",
            "--target", "/data/python/site-packages",
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if proc.returncode != 0:
                return False, proc.stderr or f"Exit code {proc.returncode}"
            return True, None
        except subprocess.TimeoutExpired:
            return False, "pip install timed out (300s)"
        except Exception as e:
            return False, str(e)

    def _docker_list_packages(
        self,
        pack_data_dir: Path,
    ) -> List[Dict[str, str]]:
        """
        Stage 3: site-packages 内のパッケージ一覧を取得

        importlib.metadata を使って dist-info を走査
        """
        script = (
            "import sys, json; "
            "sys.path.insert(0, '/data/python/site-packages'); "
            "from importlib.metadata import distributions; "
            "pkgs = [{'name': d.metadata['Name'], 'version': d.metadata['Version']} "
            "for d in distributions(path=['/data/python/site-packages'])]; "
            "print(json.dumps(pkgs))"
        )

        cmd = [
            "docker", "run", "--rm",
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--read-only",
            "--tmpfs=/tmp:size=64m,nosuid",
            "--memory=256m",
            "--user=65534:65534",
            "-v", f"{pack_data_dir.resolve()}:/data:ro",
            "--label", "rumi.managed=true",
            "--label", "rumi.type=pip_builder",
            BUILDER_IMAGE,
            "python", "-c", script,
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return json.loads(proc.stdout.strip())
        except Exception:
            pass

        return []

    # ------------------------------------------------------------------
    # site-packages パス解決 (外部から参照)
    # ------------------------------------------------------------------

    @staticmethod
    def get_site_packages_path(pack_id: str) -> Optional[Path]:
        """Pack の site-packages パスを返す (存在する場合のみ)"""
        sp = Path(PACK_DATA_BASE_DIR) / pack_id / "python" / "site-packages"
        if sp.is_dir():
            return sp
        return None

    # ------------------------------------------------------------------
    # 監査ログ
    # ------------------------------------------------------------------

    @staticmethod
    def _audit_log(event_type: str, success: bool, details: Dict[str, Any]) -> None:
        """監査ログに記録"""
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            audit.log_system_event(
                event_type=event_type,
                success=success,
                details=details,
                error=str(details.get("error") or ""),
            )
        except Exception:
            pass


# ======================================================================
# グローバルインスタンス
# ======================================================================

_global_pip_installer: Optional[PipInstaller] = None
_pip_lock = threading.Lock()


def get_pip_installer() -> PipInstaller:
    """グローバルな PipInstaller を取得"""
    global _global_pip_installer
    if _global_pip_installer is None:
        with _pip_lock:
            if _global_pip_installer is None:
                _global_pip_installer = PipInstaller()
    return _global_pip_installer


def reset_pip_installer(requests_dir: str | None = None,
                        ecosystem_dir: str | None = None) -> PipInstaller:
    """PipInstaller をリセット（テスト用）"""
    global _global_pip_installer
    with _pip_lock:
        _global_pip_installer = PipInstaller(
            requests_dir=requests_dir,
            ecosystem_dir=ecosystem_dir,
        )
    return _global_pip_installer
