"""
flow_loader.py - Flow定義ファイルのローダー

flows/(公式)、user_data/shared/flows/(共有)、pack提供flows、local_pack互換から
YAMLファイルを読み込み、InterfaceRegistryに登録する。

探索優先順（上書き規則）:
  1. 公式 flows/ — 承認不要、上書き不可
  2. user_data/shared/flows/ — 承認不要（source_type="shared"）、公式を上書き不可
  3. pack提供 flows — 承認+ハッシュ一致のpackのみ
  4. local_pack互換 ecosystem/flows/ — RUMI_LOCAL_PACK_MODE=require_approval のみ（deprecated）

設計原則:
- pack提供Flow は承認+ハッシュ一致のpackのみロード
- local_pack互換: RUMI_LOCAL_PACK_MODE=require_approval の場合のみ
- phases/priority/idによる決定的な実行順序

Phase2追加:
- pack配下探索 (ecosystem/packs/{pack_id}/backend/flows/)
- 承認ゲート (ApprovalManager連携)
- local_pack互換 (ecosystem/flows/ を仮想packとして扱う)

PR-B追加:
- hash_mismatch検知時にMODIFIED昇格 + network権限無効化（B3）

パス刷新:
- pack_subdir 基準で flows/ と backend/flows/ の両方を探索
- user_data/shared/flows/ を shared source として追加

PR-C追加:
- FlowStep に principal_id を追加（Capability Proxy連携）

Wave 9追加:
- Pack提供 Flow で {pack_id}. プレフィックスを持たない場合に警告

Wave 10-A追加:
- FlowStep に depends_on を追加（同一phase内トポロジカルソート）
- YAML ファイルサイズ上限 + パース後データ構造サイズチェック
- YAML 1.1 型変換警告（bool/int/float 検出、クォート推奨）
"""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


from .paths import (
    LOCAL_PACK_ID,
    LOCAL_PACK_DIR,
    OFFICIAL_FLOWS_DIR,
    ECOSYSTEM_DIR,
    resolve_pack_locations,
    get_pack_flow_dirs,
    get_shared_flow_dir,
)
from .resolved_profile_scope import effective_pack_ids

logger = logging.getLogger(__name__)


# ======================================================================
# YAML 安全性: データ構造サイズ上限
# ======================================================================

MAX_YAML_DEPTH = 20
MAX_YAML_NODES = 10000


def _check_yaml_complexity(
    data: Any,
    max_depth: int = MAX_YAML_DEPTH,
    max_nodes: int = MAX_YAML_NODES,
) -> None:
    """
    パース済み YAML データの深さとノード数を検証する。

    スタックベースの反復走査で実装（再帰深さ制限を回避）。
    上限超過時は ValueError を送出する。

    flow_modifier.py など他モジュールからも利用可能。

    Args:
        data: yaml.safe_load() の戻り値
        max_depth: 許容する最大ネスト深さ
        max_nodes: 許容する最大ノード数

    Raises:
        ValueError: 深さまたはノード数が上限を超えた場合
    """
    node_count = 0
    # stack items: (node, current_depth)
    stack: list = [(data, 0)]

    while stack:
        node, depth = stack.pop()
        node_count += 1

        if node_count > max_nodes:
            raise ValueError(
                f"YAML data exceeds maximum node count ({max_nodes})"
            )

        if depth > max_depth:
            raise ValueError(
                f"YAML data exceeds maximum depth ({max_depth})"
            )

        if isinstance(node, dict):
            for key, value in node.items():
                # key もノードとして数える
                node_count += 1
                if node_count > max_nodes:
                    raise ValueError(
                        f"YAML data exceeds maximum node count ({max_nodes})"
                    )
                stack.append((value, depth + 1))
        elif isinstance(node, list):
            for item in node:
                stack.append((item, depth + 1))


@dataclass
class FlowStep:
    """Flowステップの正規化表現"""
    id: str
    phase: str
    priority: int
    type: str
    when: Optional[str]
    input: Any
    output: Optional[str]
    raw: Dict[str, Any]

    # python_file_call用
    owner_pack: Optional[str] = None
    file: Optional[str] = None
    timeout_seconds: float = 60.0
    principal_id: Optional[str] = None

    # Wave 10-A: ステップ間依存
    depends_on: Optional[List[str]] = None
    # universal_call 用
    runtime: Optional[str] = None
    protocol: Optional[str] = None
    docker_image: Optional[str] = None


@dataclass
class FlowDefinition:
    """Flow定義の正規化表現"""
    flow_id: str
    inputs: Dict[str, str]
    outputs: Dict[str, str]
    phases: List[str]
    defaults: Dict[str, Any]
    steps: List[FlowStep]
    source_file: Optional[Path] = None
    source_type: str = "unknown"  # "official", "shared", "pack", "local_pack"
    source_pack_id: Optional[str] = None  # pack提供の場合のpack_id
    schedule: Optional[Dict] = None  # schedule定義（cron/interval）

    def to_dict(self) -> Dict[str, Any]:
        """既存Kernelが処理できる形式に変換"""
        d = {
            "flow_id": self.flow_id,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "phases": self.phases,
            "defaults": self.defaults,
            "steps": [self._step_to_dict(s) for s in self.steps],
            "_source_file": str(self.source_file) if self.source_file else None,
            "_source_type": self.source_type,
            "_source_pack_id": self.source_pack_id,
        }
        if self.schedule is not None:
            d["schedule"] = self.schedule
        return d

    def _step_to_dict(self, step: FlowStep) -> Dict[str, Any]:
        """ステップを辞書形式に変換"""
        d = {
            "id": step.id,
            "phase": step.phase,
            "priority": step.priority,
            "type": step.type,
        }
        if step.when:
            d["when"] = step.when
        if step.input is not None:
            d["input"] = step.input
        if step.output:
            d["output"] = step.output
        if step.owner_pack:
            d["owner_pack"] = step.owner_pack
        if step.file:
            d["file"] = step.file
        if step.timeout_seconds != 60.0:
            d["timeout_seconds"] = step.timeout_seconds
        if step.principal_id:
            d["principal_id"] = step.principal_id
        if step.depends_on is not None:
            d["depends_on"] = step.depends_on
        if step.runtime is not None:
            d["runtime"] = step.runtime
        if step.protocol is not None:
            d["protocol"] = step.protocol
        if step.docker_image is not None:
            d["docker_image"] = step.docker_image
        return d


@dataclass
class FlowLoadResult:
    """Flowロード結果"""
    success: bool
    flow_id: Optional[str] = None
    flow_def: Optional[FlowDefinition] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    skipped_reason: Optional[str] = None


@dataclass
class FlowSkipRecord:
    """スキップされたFlowの記録"""
    file_path: str
    pack_id: Optional[str]
    reason: str
    ts: str


class FlowLoader:
    """
    Flowファイルローダー

    flows/(公式)、user_data/shared/flows/(共有)、pack提供flows、local_pack互換から
    YAMLファイルを読み込み、正規化する。

    承認ゲート:
    - 公式Flow (flows/) は承認不要
    - 共有Flow (user_data/shared/flows/) は承認不要
    - pack提供Flow は承認+ハッシュ一致が必要
    - local_pack (ecosystem/flows/) は環境変数で制御（deprecated）
    """

    OFFICIAL_FLOWS_DIR = OFFICIAL_FLOWS_DIR

    def __init__(self, approval_manager=None):
        self._lock = threading.RLock()
        self._loaded_flows: Dict[str, FlowDefinition] = {}
        self._load_errors: List[Dict[str, Any]] = []
        self._skipped_flows: List[FlowSkipRecord] = []
        self._approval_manager = approval_manager

    def _now_ts(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _get_approval_manager(self):
        """ApprovalManagerを取得（遅延初期化）"""
        if self._approval_manager is None:
            try:
                from .approval_manager import get_approval_manager
                self._approval_manager = get_approval_manager()
            except Exception:
                pass
        return self._approval_manager

    def _is_local_pack_mode_enabled(self) -> bool:
        """local_packモードが有効かチェック"""
        mode = os.environ.get("RUMI_LOCAL_PACK_MODE", "off").lower()
        return mode == "require_approval"

    def _check_pack_approval(self, pack_id: str) -> Tuple[bool, Optional[str]]:
        """
        packの承認状態をチェック

        PR-B追加: hash_mismatch検知時にMODIFIED昇格 + network権限無効化

        Returns:
            (is_approved: bool, skip_reason: Optional[str])
        """
        am = self._get_approval_manager()
        if am is None:
            # ApprovalManagerがない場合は承認済みとみなす（後方互換）
            return True, None

        try:
            is_valid, reason = am.is_pack_approved_and_verified(pack_id)

            # B3: hash_mismatch検知時の処理
            if not is_valid and reason == "hash_mismatch":
                self._handle_hash_mismatch(pack_id, am)

            return is_valid, reason
        except Exception as e:
            return False, f"approval_check_error: {e}"

    def _handle_hash_mismatch(self, pack_id: str, am) -> None:
        """
        hash_mismatch検知時の処理（B3）

        - ApprovalManager.mark_modified() を呼ぶ（必須）
        - NetworkGrantManager.disable_for_modified() も呼ぶ（best-effort）
        """
        # 1. MODIFIEDへ昇格（必須）
        try:
            am.mark_modified(pack_id)
        except Exception as e:
            # 失敗してもログに記録して継続
            self._log_hash_mismatch_error(pack_id, "mark_modified", e)

        # 2. ネットワーク権限無効化（best-effort）
        try:
            from .network_grant_manager import get_network_grant_manager
            ngm = get_network_grant_manager()
            ngm.disable_for_modified(pack_id)
        except Exception as e:
            # 失敗してもログに記録して継続（best-effort）
            self._log_hash_mismatch_error(pack_id, "disable_network", e)

    def _log_hash_mismatch_error(self, pack_id: str, operation: str, error: Exception) -> None:
        """hash_mismatch処理のエラーをログに記録"""
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            audit.log_system_event(
                event_type="hash_mismatch_handling_error",
                success=False,
                details={
                    "pack_id": pack_id,
                    "operation": operation,
                    "error": str(error),
                }
            )
        except Exception:
            pass  # 監査ログのエラーで処理を止めない

    def _record_skip(self, file_path: Path, pack_id: Optional[str], reason: str) -> None:
        """スキップを記録"""
        record = FlowSkipRecord(
            file_path=str(file_path),
            pack_id=pack_id,
            reason=reason,
            ts=self._now_ts()
        )
        self._skipped_flows.append(record)

        # 監査ログにも記録
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            audit.log_system_event(
                event_type="flow_load_skipped",
                success=False,
                details={
                    "file": str(file_path),
                    "pack_id": pack_id,
                    "reason": reason,
                }
            )
        except Exception:
            pass

    def load_all_flows(self) -> Dict[str, FlowDefinition]:
        """
        全Flowファイルをロード

        探索優先順:
          1. 公式 flows/
          2. user_data/shared/flows/
          3. pack提供 flows
          4. local_pack互換 ecosystem/flows/

        Returns:
            flow_id -> FlowDefinition のマップ
        """
        with self._lock:
            self._loaded_flows.clear()
            self._load_errors.clear()
            self._skipped_flows.clear()

            # 1. 公式Flowをロード（承認不要）
            official_dir = Path(OFFICIAL_FLOWS_DIR)
            if official_dir.exists():
                self._load_official_flows(official_dir)

            # 2. shared Flowをロード（承認不要、source_type="shared"）
            self._load_shared_flows()

            # 3. pack提供Flowをロード（承認必須）
            self._load_pack_flows_via_discovery()

            # 4. local_pack互換（環境変数で制御、deprecated）
            if self._is_local_pack_mode_enabled():
                self._load_local_pack_flows()

            return dict(self._loaded_flows)

    def _load_official_flows(self, directory: Path) -> None:
        """公式Flowをロード（承認不要）"""
        for yaml_file in sorted(directory.glob("*.flow.yaml")):
            result = self.load_flow_file(yaml_file, "official", None)

            flow_id = result.flow_id
            flow_def = result.flow_def
            if result.success and flow_def is not None and flow_id is not None:
                # 重複チェック
                if flow_id in self._loaded_flows:
                    self._load_errors.append({
                        "file": str(yaml_file),
                        "error": f"Duplicate flow_id: {result.flow_id}",
                        "ts": self._now_ts()
                    })
                    continue

                self._loaded_flows[flow_id] = flow_def
            else:
                self._load_errors.append({
                    "file": str(yaml_file),
                    "errors": result.errors,
                    "ts": self._now_ts()
                })

    def _load_shared_flows(self) -> None:
        """
        shared Flowをロード（承認不要、source_type="shared"）

        user_data/shared/flows/**/*.flow.yaml を読み込む。
        公式Flowを上書きしない。
        """
        shared_dir = get_shared_flow_dir()
        if not shared_dir.exists():
            return

        for yaml_file in sorted(shared_dir.glob("**/*.flow.yaml")):
            # modifiers配下はFlowではないのでスキップ
            if "modifiers" in str(yaml_file):
                continue

            result = self.load_flow_file(yaml_file, "shared", None)

            flow_id = result.flow_id
            flow_def = result.flow_def
            if result.success and flow_def is not None and flow_id is not None:
                if flow_id in self._loaded_flows:
                    existing = self._loaded_flows[flow_id]
                    if existing.source_type == "official":
                        self._load_errors.append({
                            "file": str(yaml_file),
                            "error": f"Cannot override official flow '{result.flow_id}' from shared",
                            "ts": self._now_ts()
                        })
                        continue

                self._loaded_flows[flow_id] = flow_def
            else:
                self._load_errors.append({
                    "file": str(yaml_file),
                    "errors": result.errors,
                    "ts": self._now_ts()
                })

    def _load_pack_flows_via_discovery(self) -> None:
        """
        pack提供Flowをロード（承認必須）

        active ResolvedProfileのeffective packだけをIDで直接解決し、
        pack_subdir 基準で flows/ と backend/flows/ を探索する。
        """
        effective = effective_pack_ids()
        locations = resolve_pack_locations(effective, str(ECOSYSTEM_DIR))

        for loc in locations:
            pack_id = loc.pack_id

            # 承認チェック
            is_approved, reason = self._check_pack_approval(pack_id)
            if not is_approved:
                for flows_dir in get_pack_flow_dirs(loc.pack_subdir):
                    for yaml_file in sorted(flows_dir.glob("**/*.flow.yaml")):
                        if "modifiers" not in str(yaml_file):
                            self._record_skip(yaml_file, pack_id, reason or "not_approved")
                continue

            for flows_dir in get_pack_flow_dirs(loc.pack_subdir):
                self._load_directory_flows(flows_dir, "pack", pack_id)

    def _load_local_pack_flows(self) -> None:
        """
        local_pack互換: ecosystem/flows/ をロード（deprecated）

        RUMI_LOCAL_PACK_MODE=require_approval の場合のみ。
        優先順位最低。公式・shared・pack提供を上書きしない。
        """
        # 承認チェック
        is_approved, reason = self._check_pack_approval(LOCAL_PACK_ID)
        if not is_approved:
            local_dir = Path(LOCAL_PACK_DIR)
            if local_dir.exists():
                for yaml_file in local_dir.glob("**/*.flow.yaml"):
                    if "modifiers" not in str(yaml_file):
                        self._record_skip(yaml_file, LOCAL_PACK_ID, reason or "not_approved")
            return

        # deprecated 警告
        import sys
        print(
            "[FlowLoader] WARNING: local_pack (ecosystem/flows/) is deprecated. "
            "Use user_data/shared/flows/ instead.",
            file=sys.stderr,
        )

        # ロード
        local_dir = Path(LOCAL_PACK_DIR)
        if local_dir.exists():
            for yaml_file in sorted(local_dir.glob("**/*.flow.yaml")):
                # modifiers配下はFlowではないのでスキップ
                if "modifiers" in str(yaml_file):
                    continue

                result = self.load_flow_file(yaml_file, "local_pack", LOCAL_PACK_ID)

                flow_id = result.flow_id
                flow_def = result.flow_def
                if result.success and flow_def is not None and flow_id is not None:
                    if flow_id in self._loaded_flows:
                        existing = self._loaded_flows[flow_id]
                        # 公式・shared・pack提供を上書きしない
                        if existing.source_type in ("official", "shared", "pack"):
                            self._load_errors.append({
                                "file": str(yaml_file),
                                "error": f"Cannot override {existing.source_type} flow "
                                         f"'{result.flow_id}' from local_pack",
                                "ts": self._now_ts()
                            })
                            continue

                    self._loaded_flows[flow_id] = flow_def
                else:
                    self._load_errors.append({
                        "file": str(yaml_file),
                        "errors": result.errors,
                        "ts": self._now_ts()
                    })

    def _load_directory_flows(self, directory: Path, source_type: str, pack_id: Optional[str]) -> None:
        """ディレクトリ内のFlowファイルをロード（modifiers配下除外）"""
        for yaml_file in sorted(directory.glob("**/*.flow.yaml")):
            # modifiers配下はFlowではないのでスキップ
            if "modifiers" in str(yaml_file):
                continue

            result = self.load_flow_file(yaml_file, source_type, pack_id)

            flow_id = result.flow_id
            flow_def = result.flow_def
            if result.success and flow_def is not None and flow_id is not None:
                # Wave 9: Pack提供 Flow の ID プレフィックスチェック
                if source_type == "pack" and pack_id:
                    expected_prefix = f"{pack_id}."
                    if not flow_id.startswith(expected_prefix):
                        warn_msg = (
                            f"Pack '{pack_id}' provides flow '{result.flow_id}' "
                            f"without expected prefix '{expected_prefix}'. "
                            f"Recommended flow_id: '{expected_prefix}{result.flow_id}'"
                        )
                        logger.warning("[FlowLoader] %s", warn_msg)
                        result.warnings.append(warn_msg)
                        # 監査ログにも記録（best-effort）
                        try:
                            from .audit_logger import get_audit_logger
                            audit = get_audit_logger()
                            audit.log_system_event(
                                event_type="flow_id_prefix_warning",
                                success=True,
                                details={
                                    "flow_id": result.flow_id,
                                    "pack_id": pack_id,
                                    "expected_prefix": expected_prefix,
                                    "file": str(yaml_file),
                                },
                            )
                        except Exception:
                            pass

                # 重複チェック
                if flow_id in self._loaded_flows:
                    existing = self._loaded_flows[flow_id]
                    # 公式を上書きしない
                    if existing.source_type == "official" and source_type != "official":
                        self._load_errors.append({
                            "file": str(yaml_file),
                            "error": f"Cannot override official flow "
                                     f"'{result.flow_id}' from {source_type}",
                            "ts": self._now_ts()
                        })
                        continue
                    # shared を pack が上書きしようとした場合は shared 優先
                    if existing.source_type == "shared" and source_type == "pack":
                        self._load_errors.append({
                            "file": str(yaml_file),
                            "error": f"Cannot override shared flow "
                                     f"'{result.flow_id}' from pack "
                                     f"(shared takes precedence)",
                            "ts": self._now_ts()
                        })
                        continue

                self._loaded_flows[flow_id] = flow_def
            else:
                self._load_errors.append({
                    "file": str(yaml_file),
                    "errors": result.errors,
                    "ts": self._now_ts()
                })

    def load_flow_file(self, file_path: Path, source_type: str = "unknown", pack_id: Optional[str] = None) -> FlowLoadResult:
        """
        単一のFlowファイルをロード

        Args:
            file_path: YAMLファイルのパス
            source_type: "official", "shared", "pack", "local_pack"
            pack_id: pack提供の場合のpack_id

        Returns:
            FlowLoadResult
        """
        result = FlowLoadResult(success=False)

        if not file_path.exists():
            result.errors.append(f"File not found: {file_path}")
            return result

        if not HAS_YAML:
            result.errors.append("PyYAML is not installed")
            return result

        # Wave 10-A: ファイルサイズ上限チェック
        max_bytes = int(os.environ.get("RUMI_MAX_FLOW_FILE_BYTES", 1 * 1024 * 1024))
        try:
            file_size = file_path.stat().st_size
        except OSError as e:
            result.errors.append(f"Cannot stat file: {e}")
            return result

        if file_size > max_bytes:
            result.errors.append(
                f"Flow file exceeds size limit: {file_size} bytes > {max_bytes} bytes"
            )
            return result

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                raw_data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            result.errors.append(f"YAML parse error: {e}")
            return result
        except Exception as e:
            result.errors.append(f"File read error: {e}")
            return result

        if not isinstance(raw_data, dict):
            result.errors.append("Flow file must be a YAML object")
            return result

        # Wave 10-A: パース後データ構造サイズチェック
        try:
            _check_yaml_complexity(raw_data)
        except ValueError as e:
            result.errors.append(f"YAML complexity check failed: {e}")
            return result

        # 必須フィールドチェック
        flow_id = raw_data.get("flow_id")

        # Wave 10-A: flow_id の型変換警告
        if flow_id is not None and isinstance(flow_id, (bool, int, float)):
            logger.warning(
                "[FlowLoader] flow_id '%s' (type %s) was auto-converted to string. "
                "Quote the value in your YAML file. (file: %s)",
                flow_id, type(flow_id).__name__, file_path,
            )
            result.warnings.append(
                f"flow_id '{flow_id}' (type {type(flow_id).__name__}) was auto-converted "
                f"to string. Quote the value in your YAML file."
            )
            flow_id = str(flow_id)

        if not flow_id or not isinstance(flow_id, str):
            result.errors.append("Missing or invalid 'flow_id'")
            return result

        result.flow_id = flow_id

        # inputs/outputs(任意だがあれば型チェック)
        inputs = raw_data.get("inputs", {})
        if not isinstance(inputs, dict):
            result.errors.append("'inputs' must be an object")
            return result

        outputs = raw_data.get("outputs", {})
        if not isinstance(outputs, dict):
            result.errors.append("'outputs' must be an object")
            return result

        # phases(必須)
        phases = raw_data.get("phases", [])
        if not isinstance(phases, list) or not phases:
            result.errors.append("'phases' must be a non-empty array")
            return result

        for i, phase in enumerate(phases):
            if not isinstance(phase, str):
                result.errors.append(f"phases[{i}] must be a string")
                return result

        # defaults(任意)
        defaults = raw_data.get("defaults", {})
        if not isinstance(defaults, dict):
            defaults = {}

        defaults.setdefault("fail_soft", True)
        defaults.setdefault("on_missing_step", "skip")

        # steps(必須)
        raw_steps = raw_data.get("steps", [])
        if not isinstance(raw_steps, list):
            result.errors.append("'steps' must be an array")
            return result

        # ステップをパース
        steps, step_errors, step_warnings = self._parse_steps(raw_steps, phases, file_path)
        result.errors.extend(step_errors)
        result.warnings.extend(step_warnings)

        if result.errors:
            return result

        # ステップをソート(phase順 → priority順 → id順、同一phase内depends_onトポロジカルソート)
        sorted_steps = self._sort_steps(steps, phases)

        # FlowDefinitionを作成
        # schedule フィールド（オプション）
        schedule = raw_data.get("schedule")
        if schedule is not None and not isinstance(schedule, dict):
            result.warnings.append("'schedule' should be a dict, ignoring")
            schedule = None

        flow_def = FlowDefinition(
            flow_id=flow_id,
            inputs=inputs,
            outputs=outputs,
            phases=phases,
            defaults=defaults,
            steps=sorted_steps,
            source_file=file_path,
            source_type=source_type,
            source_pack_id=pack_id,
            schedule=schedule
        )

        result.success = True
        result.flow_def = flow_def
        return result

    def _parse_steps(
        self,
        raw_steps: List[Any],
        phases: List[str],
        file_path: Path
    ) -> Tuple[List[FlowStep], List[str], List[str]]:
        """ステップをパースして正規化"""
        steps = []
        errors = []
        warnings = []
        seen_ids = set()

        for i, raw_step in enumerate(raw_steps):
            if not isinstance(raw_step, dict):
                errors.append(f"steps[{i}] must be an object")
                continue

            # id(必須)
            step_id = raw_step.get("id")

            # Wave 10-A: step_id の型変換警告（bool チェックは int/str より先）
            if step_id is not None and isinstance(step_id, bool):
                warnings.append(
                    f"step id '{step_id}' was interpreted as boolean by YAML 1.1. "
                    f"Quote the value in your YAML file."
                )
                step_id = str(step_id)

            if not step_id or not isinstance(step_id, str):
                errors.append(f"steps[{i}]: missing or invalid 'id'")
                continue

            if step_id in seen_ids:
                errors.append(f"steps[{i}]: duplicate id '{step_id}'")
                continue
            seen_ids.add(step_id)

            # phase(必須)
            phase = raw_step.get("phase")

            # Wave 10-A: phase の型変換警告
            if phase is not None and isinstance(phase, bool):
                warnings.append(
                    f"step id '{step_id}': phase '{phase}' was interpreted as boolean "
                    f"by YAML 1.1. Quote the value in your YAML file."
                )
                phase = str(phase)

            if not phase or not isinstance(phase, str):
                errors.append(f"steps[{i}] ({step_id}): missing or invalid 'phase'")
                continue

            if phase not in phases:
                errors.append(f"steps[{i}] ({step_id}): phase '{phase}' not in phases list")
                continue

            # type(必須)
            step_type = raw_step.get("type")
            if not step_type or not isinstance(step_type, str):
                errors.append(f"steps[{i}] ({step_id}): missing or invalid 'type'")
                continue

            # priority(任意、デフォルト100)
            priority = raw_step.get("priority", 100)
            if not isinstance(priority, (int, float)):
                warnings.append(f"steps[{i}] ({step_id}): invalid priority, using 100")
                priority = 100
            priority = int(priority)

            # when(任意)
            when = raw_step.get("when")

            # Wave 10-A: when の型変換警告
            if when is not None and isinstance(when, bool):
                warnings.append(
                    f"step id '{step_id}': when '{when}' was interpreted as boolean "
                    f"by YAML 1.1. Quote the value in your YAML file."
                )
                when = str(when)

            if when is not None and not isinstance(when, str):
                warnings.append(f"steps[{i}] ({step_id}): 'when' must be a string")
                when = None

            # input(任意)
            step_input = raw_step.get("input")

            # output(任意)
            output = raw_step.get("output")

            # Wave 10-A: output の型変換警告
            if output is not None and isinstance(output, bool):
                warnings.append(
                    f"step id '{step_id}': output '{output}' was interpreted as boolean "
                    f"by YAML 1.1. Quote the value in your YAML file."
                )
                output = str(output)

            if output is not None and not isinstance(output, str):
                warnings.append(f"steps[{i}] ({step_id}): 'output' must be a string")
                output = None

            # Wave 10-A: depends_on(任意)
            depends_on_raw = raw_step.get("depends_on")
            depends_on: Optional[List[str]] = None
            if depends_on_raw is not None:
                if not isinstance(depends_on_raw, list):
                    warnings.append(
                        f"steps[{i}] ({step_id}): 'depends_on' must be a list, ignoring"
                    )
                else:
                    valid = True
                    for dep_idx, dep in enumerate(depends_on_raw):
                        if not isinstance(dep, str):
                            warnings.append(
                                f"steps[{i}] ({step_id}): depends_on[{dep_idx}] must be "
                                f"a string, ignoring entire depends_on"
                            )
                            valid = False
                            break
                    if valid:
                        depends_on = depends_on_raw

            # FlowStepを作成
            step = FlowStep(
                id=step_id,
                phase=phase,
                priority=priority,
                type=step_type,
                when=when,
                input=step_input,
                output=output,
                raw=raw_step,
                depends_on=depends_on,
            )

            # python_file_call固有のフィールド
            if step_type == "python_file_call":
                step.owner_pack = raw_step.get("owner_pack")
                step.file = raw_step.get("file")
                step.principal_id = raw_step.get("principal_id")
                step.timeout_seconds = raw_step.get("timeout_seconds", 60.0)

                if not step.file:
                    errors.append(f"steps[{i}] ({step_id}): python_file_call requires 'file'")
                    continue

            steps.append(step)

        return steps, errors, warnings

    def _sort_steps(self, steps: List[FlowStep], phases: List[str]) -> List[FlowStep]:
        """
        ステップを決定的にソート

        ソート順:
        1. phase(phasesリストでの順序)
        2. priority(昇順、小さいほど先)
        3. id(アルファベット順、タイブレーク)

        Wave 10-A追加:
        既存ソート後、同一phase内で depends_on に基づくトポロジカルソートを実施。
        循環依存時は既存ソート結果を維持し warning を出力。
        """
        phase_order = {phase: i for i, phase in enumerate(phases)}

        # 既存ソート
        sorted_steps = sorted(
            steps,
            key=lambda s: (phase_order.get(s.phase, 999), s.priority, s.id)
        )

        # 全ステップの id -> phase マッピングを構築（cross-phase 検出用）
        step_id_to_phase: Dict[str, str] = {s.id: s.phase for s in sorted_steps}
        all_step_ids: Set[str] = set(step_id_to_phase.keys())

        # depends_on の事前検証: 存在しない step_id / cross-phase 参照を警告
        for s in sorted_steps:
            if s.depends_on is None:
                continue
            for dep_id in s.depends_on:
                if dep_id not in all_step_ids:
                    logger.warning(
                        "[FlowLoader] Step '%s' depends_on '%s' which does not exist",
                        s.id, dep_id,
                    )
                elif step_id_to_phase[dep_id] != s.phase:
                    logger.warning(
                        "[FlowLoader] Step '%s' (phase '%s') depends_on '%s' "
                        "(phase '%s'): cross-phase dependency; phase order takes precedence",
                        s.id, s.phase, dep_id, step_id_to_phase[dep_id],
                    )

        # 同一phase内ステップ群をグループ化（順序保持）
        phase_groups: Dict[str, List[FlowStep]] = {}
        for s in sorted_steps:
            phase_groups.setdefault(s.phase, []).append(s)

        # 各phaseグループ内でトポロジカルソート
        result: List[FlowStep] = []
        for phase in phases:
            group = phase_groups.get(phase, [])
            if not group:
                continue

            # このグループ内の step_id 集合
            group_ids = {s.id for s in group}

            # depends_on があるステップが1つもなければソート不要
            has_deps = any(s.depends_on for s in group)
            if not has_deps:
                result.extend(group)
                continue

            # Kahn's algorithm
            # group 内の index マップ（既存ソート順の位置を保持）
            id_to_step: Dict[str, FlowStep] = {s.id: s for s in group}
            id_to_order: Dict[str, int] = {s.id: idx for idx, s in enumerate(group)}

            # 入次数を計算（group 内の依存のみ考慮）
            in_degree: Dict[str, int] = {s.id: 0 for s in group}
            # 隣接リスト: dep_id -> [step_ids that depend on dep_id]
            adjacency: Dict[str, List[str]] = {s.id: [] for s in group}

            for s in group:
                if s.depends_on is None:
                    continue
                for dep_id in s.depends_on:
                    # group 内の依存のみ（cross-phase / 存在しない依存は無視）
                    if dep_id in group_ids:
                        in_degree[s.id] += 1
                        adjacency[dep_id].append(s.id)

            # 入次数0のノードを既存ソート順でキューに入れる
            queue: deque = deque()
            for s in group:
                if in_degree[s.id] == 0:
                    queue.append(s.id)

            topo_order: List[str] = []
            while queue:
                # 複数候補がある場合、既存ソート順（id_to_order）で最小のものを選ぶ
                # deque から全取り出しして既存順でソートし直す
                candidates = sorted(queue, key=lambda sid: id_to_order[sid])
                queue.clear()
                for sid in candidates:
                    topo_order.append(sid)
                    for neighbor in adjacency[sid]:
                        in_degree[neighbor] -= 1
                        if in_degree[neighbor] == 0:
                            queue.append(neighbor)

            if len(topo_order) != len(group):
                # 循環検出: 既存ソート結果を維持
                cycle_ids = [s.id for s in group if s.id not in set(topo_order)]
                logger.warning(
                    "[FlowLoader] Circular dependency detected in phase '%s' "
                    "involving steps: %s. Using default sort order.",
                    phase, cycle_ids,
                )
                result.extend(group)
            else:
                result.extend(id_to_step[sid] for sid in topo_order)

        return result

    def get_loaded_flows(self) -> Dict[str, FlowDefinition]:
        """ロード済みFlowを取得"""
        with self._lock:
            return dict(self._loaded_flows)

    def get_load_errors(self) -> List[Dict[str, Any]]:
        """ロードエラーを取得"""
        with self._lock:
            return list(self._load_errors)

    def get_skipped_flows(self) -> List[FlowSkipRecord]:
        """スキップされたFlowを取得"""
        with self._lock:
            return list(self._skipped_flows)

    def get_flow(self, flow_id: str) -> Optional[FlowDefinition]:
        """特定のFlowを取得"""
        with self._lock:
            return self._loaded_flows.get(flow_id)


# グローバルインスタンス
_global_flow_loader: Optional[FlowLoader] = None
_loader_lock = threading.Lock()


def get_flow_loader() -> FlowLoader:
    """グローバルなFlowLoaderを取得"""
    global _global_flow_loader
    if _global_flow_loader is None:
        with _loader_lock:
            if _global_flow_loader is None:
                _global_flow_loader = FlowLoader()
    return _global_flow_loader


def reset_flow_loader() -> FlowLoader:
    """FlowLoaderをリセット(テスト用)"""
    global _global_flow_loader
    with _loader_lock:
        _global_flow_loader = FlowLoader()
    return _global_flow_loader


def load_all_flows() -> Dict[str, FlowDefinition]:
    """全Flowをロード(ショートカット)"""
    return get_flow_loader().load_all_flows()
