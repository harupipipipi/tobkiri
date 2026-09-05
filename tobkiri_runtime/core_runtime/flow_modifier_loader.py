"""
flow_modifier_loader.py - FlowModifier ローダー

Wave 13 T-048: flow_modifier.py から分割。

探索優先順:
  1. user_data/shared/flows/modifiers/ — 承認不要
  2. pack提供 modifiers — 承認+ハッシュ一致のpackのみ
  3. local_pack互換 ecosystem/flows/modifiers/ — deprecated

Wave 17-B 変更:
  step 内の principal_id が source pack_id と一致しない場合は上書き
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

from .flow_modifier_models import (
    FlowModifierDef,
    ModifierLoadResult,
    ModifierRequires,
    ModifierSkipRecord,
)

from .paths import (
    LOCAL_PACK_ID,
    LOCAL_PACK_MODIFIERS_DIR,
    ECOSYSTEM_DIR,
    resolve_pack_locations,
    get_pack_modifier_dirs,
    get_shared_modifier_dir,
)
from .resolved_profile_scope import effective_pack_ids

logger = logging.getLogger(__name__)


class FlowModifierLoader:
    """
    Flow modifierローダー

    探索優先順:
      1. user_data/shared/flows/modifiers/ — 承認不要
      2. pack提供 modifiers — 承認+ハッシュ一致のpackのみ
      3. local_pack互換 ecosystem/flows/modifiers/ — deprecated

    承認済み+ハッシュ一致のpackのみ対象。
    """

    def __init__(self, approval_manager=None):
        self._lock = threading.RLock()
        self._loaded_modifiers: Dict[str, FlowModifierDef] = {}
        self._load_errors: List[Dict[str, Any]] = []
        self._skipped_modifiers: List[ModifierSkipRecord] = []
        self._approval_manager = approval_manager
        self._wildcard_flags: Dict[str, bool] = {}  # Wave 11

    def _now_ts(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _get_approval_manager(self):
        if self._approval_manager is None:
            try:
                from .approval_manager import get_approval_manager
                self._approval_manager = get_approval_manager()
            except Exception:
                pass
        return self._approval_manager

    def _is_local_pack_mode_enabled(self) -> bool:
        mode = os.environ.get("RUMI_LOCAL_PACK_MODE", "off").lower()
        return mode == "require_approval"

    def _check_pack_approval(self, pack_id: str) -> Tuple[bool, Optional[str]]:
        am = self._get_approval_manager()
        if am is None:
            return True, None
        try:
            is_valid, reason = am.is_pack_approved_and_verified(pack_id)
            if not is_valid and reason == "hash_mismatch":
                self._handle_hash_mismatch(pack_id, am)
            return is_valid, reason
        except Exception as e:
            return False, f"approval_check_error: {e}"

    def _handle_hash_mismatch(self, pack_id: str, am) -> None:
        try:
            am.mark_modified(pack_id)
        except Exception as e:
            self._log_hash_mismatch_error(pack_id, "mark_modified", e)
        try:
            from .network_grant_manager import get_network_grant_manager
            ngm = get_network_grant_manager()
            ngm.disable_for_modified(pack_id)
        except Exception as e:
            self._log_hash_mismatch_error(pack_id, "disable_network", e)

    def _log_hash_mismatch_error(self, pack_id: str, operation: str, error: Exception) -> None:
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
            pass

    def _record_skip(self, file_path: Path, pack_id: Optional[str], reason: str) -> None:
        record = ModifierSkipRecord(
            file_path=str(file_path),
            pack_id=pack_id,
            reason=reason,
            ts=self._now_ts()
        )
        self._skipped_modifiers.append(record)
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            audit.log_system_event(
                event_type="modifier_load_skipped",
                success=False,
                details={
                    "file": str(file_path),
                    "pack_id": pack_id,
                    "reason": reason,
                }
            )
        except Exception:
            pass

    def _is_wildcard_modifier_allowed(self, pack_id: Optional[str]) -> bool:
        if pack_id is None:
            return True
        if os.environ.get("RUMI_ALLOW_WILDCARD_MODIFIERS", "").lower() == "true":
            return True
        cached = self._wildcard_flags.get(pack_id)
        if cached is not None:
            return cached
        allowed = False
        try:
            locations = resolve_pack_locations(
                (pack_id,),
                str(ECOSYSTEM_DIR),
            )
            for loc in locations:
                if loc.pack_id == pack_id:
                    allowed = self._read_wildcard_flag_from_ecosystem(loc.ecosystem_json_path)
                    break
        except Exception:
            allowed = False
        self._wildcard_flags[pack_id] = allowed
        return allowed

    @staticmethod
    def _read_wildcard_flag_from_ecosystem(ecosystem_json_path: Path) -> bool:
        try:
            with open(ecosystem_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return bool(data.get("allow_wildcard_modifiers", False))
        except Exception:
            return False

    def load_all_modifiers(self) -> Dict[str, FlowModifierDef]:
        with self._lock:
            self._loaded_modifiers.clear()
            self._load_errors.clear()
            self._skipped_modifiers.clear()
            self._wildcard_flags.clear()

            self._load_shared_modifiers()
            self._load_pack_modifiers_via_discovery()

            if self._is_local_pack_mode_enabled():
                self._load_local_pack_modifiers()

            return dict(self._loaded_modifiers)

    def _load_shared_modifiers(self) -> None:
        shared_dir = get_shared_modifier_dir()
        if not shared_dir.exists():
            return
        self._load_directory_modifiers(shared_dir, None)

    def _load_pack_modifiers_via_discovery(self) -> None:
        effective = effective_pack_ids()
        locations = resolve_pack_locations(effective, str(ECOSYSTEM_DIR))
        for loc in locations:
            pack_id = loc.pack_id
            if pack_id not in self._wildcard_flags:
                self._wildcard_flags[pack_id] = self._read_wildcard_flag_from_ecosystem(
                    loc.ecosystem_json_path
                )
            is_approved, reason = self._check_pack_approval(pack_id)
            if not is_approved:
                for mod_dir in get_pack_modifier_dirs(loc.pack_subdir):
                    for yaml_file in sorted(mod_dir.glob("**/*.modifier.yaml")):
                        self._record_skip(yaml_file, pack_id, reason or "not_approved")
                continue
            for mod_dir in get_pack_modifier_dirs(loc.pack_subdir):
                self._load_directory_modifiers(mod_dir, pack_id)

    def _load_local_pack_modifiers(self) -> None:
        is_approved, reason = self._check_pack_approval(LOCAL_PACK_ID)
        if not is_approved:
            local_modifiers_dir = Path(LOCAL_PACK_MODIFIERS_DIR)
            if local_modifiers_dir.exists():
                for yaml_file in local_modifiers_dir.glob("**/*.modifier.yaml"):
                    self._record_skip(yaml_file, LOCAL_PACK_ID, reason or "not_approved")
            return

        import sys
        print(
            "[FlowModifierLoader] WARNING: local_pack modifiers "
            "(ecosystem/flows/modifiers/) is deprecated. "
            "Use user_data/shared/flows/modifiers/ instead.",
            file=sys.stderr,
        )

        local_modifiers_dir = Path(LOCAL_PACK_MODIFIERS_DIR)
        if local_modifiers_dir.exists():
            self._load_directory_modifiers(local_modifiers_dir, LOCAL_PACK_ID)

    def _load_directory_modifiers(self, directory: Path, pack_id: Optional[str]) -> None:
        for yaml_file in sorted(directory.glob("**/*.modifier.yaml")):
            result = self.load_modifier_file(yaml_file, pack_id)

            modifier_id = result.modifier_id
            modifier_def = result.modifier_def
            if result.success and modifier_def is not None and modifier_id is not None:
                if modifier_id in self._loaded_modifiers:
                    self._load_errors.append({
                        "file": str(yaml_file),
                        "error": f"Duplicate modifier_id: {result.modifier_id}",
                        "ts": self._now_ts()
                    })
                    continue

                self._loaded_modifiers[modifier_id] = modifier_def

                if modifier_def.target_flow_id == "*":
                    if not self._is_wildcard_modifier_allowed(pack_id):
                        logger.warning(
                            "[FlowModifier] Modifier '%s' targets ALL flows "
                            "(target_flow_id='*') but pack '%s' has not been "
                            "granted wildcard modifier permission. Skipping. "
                            "Set RUMI_ALLOW_WILDCARD_MODIFIERS=true or add "
                            "'allow_wildcard_modifiers: true' to ecosystem.json.",
                            result.modifier_id,
                            pack_id,
                        )
                        self._record_skip(
                            yaml_file, pack_id, "wildcard_modifier_not_allowed"
                        )
                        del self._loaded_modifiers[modifier_id]
                        continue

                    logger.warning(
                        "[FlowModifier] Modifier '%s' targets ALL flows (target_flow_id='*'). "
                        "This modifier will be applied to every flow.",
                        result.modifier_id,
                    )
                    try:
                        from .audit_logger import get_audit_logger
                        audit = get_audit_logger()
                        audit.log_system_event(
                            event_type="wildcard_modifier_loaded",
                            success=True,
                            details={
                                "modifier_id": result.modifier_id,
                                "source_pack_id": modifier_def.source_pack_id,
                                "warning": "This modifier applies to ALL flows",
                            }
                        )
                    except Exception:
                        pass
            else:
                self._load_errors.append({
                    "file": str(yaml_file),
                    "errors": result.errors,
                    "ts": self._now_ts()
                })

    # ------------------------------------------------------------------
    # Wave 10-B: YAML safety checks
    # ------------------------------------------------------------------

    MAX_YAML_DEPTH = 20
    MAX_YAML_NODES = 10000

    @staticmethod
    def _check_yaml_complexity(data, max_depth=20, max_nodes=10000):
        node_count = 0

        def _walk(obj, depth):
            nonlocal node_count
            node_count += 1
            if node_count > max_nodes:
                return f"YAML node count exceeds limit ({max_nodes})"
            if depth > max_depth:
                return f"YAML depth exceeds limit ({max_depth})"
            if isinstance(obj, dict):
                for v in obj.values():
                    err = _walk(v, depth + 1)
                    if err:
                        return err
            elif isinstance(obj, list):
                for item in obj:
                    err = _walk(item, depth + 1)
                    if err:
                        return err
            return None

        err = _walk(data, 0)
        if err:
            return False, err
        return True, None

    def load_modifier_file(self, file_path: Path, pack_id: Optional[str] = None) -> ModifierLoadResult:
        result = ModifierLoadResult(success=False)

        if not file_path.exists():
            result.errors.append(f"File not found: {file_path}")
            return result

        if not HAS_YAML:
            result.errors.append("PyYAML is not installed")
            return result

        _max_bytes = int(os.environ.get("RUMI_MAX_MODIFIER_FILE_BYTES", 1 * 1024 * 1024))
        try:
            _file_size = file_path.stat().st_size
        except OSError as e:
            result.errors.append(f"Cannot stat file: {e}")
            return result
        if _file_size > _max_bytes:
            result.errors.append(
                f"Modifier file too large: {_file_size} bytes "
                f"(limit: {_max_bytes} bytes)"
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

        _complexity_ok, _complexity_err = self._check_yaml_complexity(
            raw_data,
            max_depth=self.MAX_YAML_DEPTH,
            max_nodes=self.MAX_YAML_NODES,
        )
        if not _complexity_ok:
            result.errors.append(f"YAML complexity check failed: {_complexity_err}")
            return result

        if not isinstance(raw_data, dict):
            result.errors.append("Modifier file must be a YAML object")
            return result

        modifier_id = raw_data.get("modifier_id")
        if modifier_id is None:
            result.errors.append("Missing or invalid 'modifier_id'")
            return result
        if not isinstance(modifier_id, str):
            result.warnings.append(
                f"modifier_id has type {type(modifier_id).__name__} "
                f"(value: {modifier_id!r}), converting to str"
            )
            modifier_id = str(modifier_id)
        if not modifier_id:
            result.errors.append("Missing or invalid 'modifier_id'")
            return result

        result.modifier_id = modifier_id

        target_flow_id = raw_data.get("target_flow_id")
        if target_flow_id is None:
            result.errors.append("Missing or invalid 'target_flow_id'")
            return result
        if not isinstance(target_flow_id, str):
            result.warnings.append(
                f"target_flow_id has type {type(target_flow_id).__name__} "
                f"(value: {target_flow_id!r}), converting to str"
            )
            target_flow_id = str(target_flow_id)
        if not target_flow_id:
            result.errors.append("Missing or invalid 'target_flow_id'")
            return result

        phase = raw_data.get("phase")
        if not phase or not isinstance(phase, str):
            result.errors.append("Missing or invalid 'phase'")
            return result

        action = raw_data.get("action")
        valid_actions = {"inject_before", "inject_after", "append", "replace", "remove"}
        if not action or action not in valid_actions:
            result.errors.append(f"Invalid 'action': must be one of {valid_actions}")
            return result

        target_step_id = raw_data.get("target_step_id")
        if action in {"inject_before", "inject_after", "replace", "remove"}:
            if not target_step_id or not isinstance(target_step_id, str):
                result.errors.append(f"'target_step_id' is required for action '{action}'")
                return result

        step = raw_data.get("step")
        if action in {"inject_before", "inject_after", "append", "replace"}:
            if not step or not isinstance(step, dict):
                result.errors.append(f"'step' is required for action '{action}'")
                return result
            if "id" not in step:
                result.errors.append("'step.id' is required")
                return result
            if "type" not in step:
                result.errors.append("'step.type' is required")
                return result

            # --- Wave 17-B: principal_id 偽装防止 ---
            # step 内の principal_id が modifier の source pack_id と一致するか検証
            step_principal_id = step.get("principal_id")
            if step_principal_id is not None and pack_id is not None:
                if step_principal_id != pack_id:
                    logger.warning(
                        "Modifier '%s' attempts to set principal_id='%s' "
                        "but source pack is '%s'. Overriding principal_id.",
                        modifier_id,
                        step_principal_id,
                        pack_id,
                    )
                    step = dict(step)  # 元データを変更しないようコピー
                    step["principal_id"] = pack_id

        priority = raw_data.get("priority", 100)
        if not isinstance(priority, (int, float)):
            result.warnings.append("Invalid priority, using 100")
            priority = 100
        priority = int(priority)

        requires_raw = raw_data.get("requires", {})
        requires = ModifierRequires(
            interfaces=requires_raw.get("interfaces", []) if isinstance(requires_raw, dict) else [],
            capabilities=requires_raw.get("capabilities", []) if isinstance(requires_raw, dict) else []
        )

        resolve_target = raw_data.get("resolve_target", False)
        resolve_namespace = raw_data.get("resolve_namespace", "flow_id")

        conflicts_with_raw = raw_data.get("conflicts_with")
        conflicts_with = None
        if isinstance(conflicts_with_raw, list):
            conflicts_with = [str(x) for x in conflicts_with_raw if x]

        compatible_with_raw = raw_data.get("compatible_with")
        compatible_with = None
        if isinstance(compatible_with_raw, list):
            compatible_with = [str(x) for x in compatible_with_raw if x]

        modifier_def = FlowModifierDef(
            modifier_id=modifier_id,
            target_flow_id=target_flow_id,
            phase=phase,
            priority=priority,
            action=action,
            target_step_id=target_step_id,
            step=step,
            requires=requires,
            source_file=file_path,
            source_pack_id=pack_id,
            resolve_target=resolve_target,
            resolve_namespace=resolve_namespace,
            conflicts_with=conflicts_with,
            compatible_with=compatible_with,
        )

        result.success = True
        result.modifier_def = modifier_def
        return result

    def get_loaded_modifiers(self) -> Dict[str, FlowModifierDef]:
        with self._lock:
            return dict(self._loaded_modifiers)

    def get_load_errors(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._load_errors)

    def get_skipped_modifiers(self) -> List[ModifierSkipRecord]:
        with self._lock:
            return list(self._skipped_modifiers)

    def get_modifiers_for_flow(self, flow_id: str, resolve: bool = False) -> List[FlowModifierDef]:
        with self._lock:
            modifiers = []

            for m in self._loaded_modifiers.values():
                target = m.target_flow_id

                if m.resolve_target or resolve:
                    try:
                        from .shared_dict import get_shared_dict_resolver
                        resolver = get_shared_dict_resolver()
                        target = resolver.resolve(m.resolve_namespace, target)
                    except Exception:
                        pass

                if fnmatch.fnmatch(flow_id, target):
                    modifiers.append(m)

            return sorted(modifiers, key=lambda m: (m.phase, m.priority, m.modifier_id))
