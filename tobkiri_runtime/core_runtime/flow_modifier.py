"""
flow_modifier.py - Flow modifier(差し込み)システム

Wave 13 T-048: データクラスを flow_modifier_models.py、
FlowModifierLoader を flow_modifier_loader.py に分離。
本ファイルは FlowModifierApplier + 公開 API + re-export を担う。

後方互換のため __init__.py が期待する全シンボルを re-export する。
"""

from __future__ import annotations

import copy
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple, Set

from .flow_loader import FlowDefinition, FlowStep
from .flow_modifier_models import (
    FlowModifierDef,
    ModifierRequires,
    ModifierLoadResult,
    ModifierApplyResult,
    ModifierSkipRecord,
)
from .flow_modifier_loader import FlowModifierLoader

logger = logging.getLogger(__name__)

__all__ = [
    "FlowModifierApplier",
    "FlowModifierDef",
    "FlowModifierLoader",
    "ModifierApplyResult",
    "ModifierLoadResult",
    "ModifierRequires",
    "ModifierSkipRecord",
]


class FlowModifierApplier:
    """
    Flow modifier適用エンジン

    modifierをFlowDefinitionに適用する。

    Phase3: 適用決定性の強化
    - 同一注入点での順序: priority → step.id → modifier_id
    - inject相対位置を保持（再ソートしない）
    """

    def __init__(self, interface_registry=None, dry_run: bool = False):
        self._interface_registry = interface_registry
        self._available_interfaces: Set[str] = set()
        self._available_capabilities: Set[str] = set()
        self._dry_run = dry_run

    def set_interface_registry(self, ir) -> None:
        self._interface_registry = ir
        self._refresh_available()

    def _refresh_available(self) -> None:
        if not self._interface_registry:
            return
        ir_list = self._interface_registry.list() or {}
        self._available_interfaces = set(ir_list.keys())
        all_caps = self._interface_registry.get("component.capabilities", strategy="all") or []
        self._available_capabilities = set()
        for cap_dict in all_caps:
            if isinstance(cap_dict, dict):
                for k, v in cap_dict.items():
                    if v:
                        self._available_capabilities.add(k)

    def check_requires(self, requires: ModifierRequires) -> Tuple[bool, Optional[str]]:
        for iface in requires.interfaces:
            if iface not in self._available_interfaces:
                return False, f"interface '{iface}' not available"
        for cap in requires.capabilities:
            if cap not in self._available_capabilities:
                return False, f"capability '{cap}' not available"
        return True, None

    # ------------------------------------------------------------------
    # Wave 9: Modifier 衝突検出
    # ------------------------------------------------------------------

    def _detect_conflicts(
        self,
        modifiers: List[FlowModifierDef],
        results: List[ModifierApplyResult],
    ) -> None:
        skipped_ids = {r.modifier_id for r in results if r.skipped_reason}
        active_modifiers = [m for m in modifiers if m.modifier_id not in skipped_ids]

        by_target: Dict[str, List[FlowModifierDef]] = {}
        for m in active_modifiers:
            tsid = m.target_step_id
            if tsid:
                if tsid not in by_target:
                    by_target[tsid] = []
                by_target[tsid].append(m)

        for tsid, group in by_target.items():
            if len(group) < 2:
                continue
            actions = {m.action for m in group}
            modifier_ids = [m.modifier_id for m in group]
            has_remove = "remove" in actions
            has_mutating = actions & {"replace", "inject_before", "inject_after"}

            if has_remove and has_mutating:
                msg = (
                    "[FlowModifier] CONFLICT (severe): target_step_id '%s' "
                    "has both 'remove' and %s actions from "
                    "modifiers %s. Injecting/replacing a removed step "
                    "is likely unintended."
                )
                logger.warning(msg, tsid, sorted(has_mutating), modifier_ids)
                self._audit_conflict(tsid, modifier_ids, sorted(actions), severity="severe")
            else:
                msg = (
                    "[FlowModifier] CONFLICT (info): target_step_id '%s' "
                    "is targeted by multiple modifiers %s "
                    "with actions %s."
                )
                logger.warning(msg, tsid, modifier_ids, sorted(actions))
                self._audit_conflict(tsid, modifier_ids, sorted(actions), severity="info")

        active_ids = {m.modifier_id for m in active_modifiers}
        for m in active_modifiers:
            if m.conflicts_with:
                for cid in m.conflicts_with:
                    if cid in active_ids:
                        msg = (
                            "[FlowModifier] CONFLICT (declared): modifier '%s' "
                            "declares conflicts_with '%s', but both are active."
                        )
                        logger.warning(msg, m.modifier_id, cid)
                        self._audit_conflict(
                            m.target_step_id or "(global)",
                            [m.modifier_id, cid],
                            ["conflicts_with"],
                            severity="declared",
                        )
            if m.compatible_with:
                for cid in m.compatible_with:
                    if cid not in active_ids and cid != m.modifier_id:
                        msg = (
                            "[FlowModifier] CONFLICT (compatibility): modifier '%s' "
                            "declares compatible_with '%s', but '%s' is not active."
                        )
                        logger.warning(msg, m.modifier_id, cid, cid)
                        self._audit_conflict(
                            m.target_step_id or "(global)",
                            [m.modifier_id, cid],
                            ["compatible_with_missing"],
                            severity="compatibility",
                        )

    def _audit_conflict(
        self,
        target_step_id: str,
        modifier_ids: List[str],
        actions: List[str],
        severity: str = "info",
    ) -> None:
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            audit.log_system_event(
                event_type="modifier_conflict_detected",
                success=True,
                details={
                    "target_step_id": target_step_id,
                    "modifier_ids": modifier_ids,
                    "actions": actions,
                    "severity": severity,
                },
            )
        except Exception:
            pass

    # ------------------------------------------------------------------

    def apply_modifiers(
        self,
        flow_def: FlowDefinition,
        modifiers: List[FlowModifierDef]
    ) -> Tuple[FlowDefinition, List[ModifierApplyResult]]:
        new_steps = copy.deepcopy(flow_def.steps)
        results: List[ModifierApplyResult] = []

        inject_before_groups: Dict[str, List[FlowModifierDef]] = {}
        inject_after_groups: Dict[str, List[FlowModifierDef]] = {}
        append_groups: Dict[str, List[FlowModifierDef]] = {}
        other_modifiers: List[FlowModifierDef] = []

        for modifier in modifiers:
            satisfied, reason = self.check_requires(modifier.requires)
            if not satisfied:
                result = ModifierApplyResult(
                    success=False,
                    modifier_id=modifier.modifier_id,
                    action=modifier.action,
                    target_flow_id=modifier.target_flow_id,
                    target_step_id=modifier.target_step_id,
                    skipped_reason=f"requires_not_satisfied: {reason}"
                )
                self._log_modifier_skip(modifier, result.skipped_reason)
                results.append(result)
                continue

            if modifier.phase not in flow_def.phases:
                if modifier.action == "append" and flow_def.phases:
                    logger.info(
                        "[FlowModifier] Phase '%s' not found for append modifier '%s'. "
                        "Falling back to last phase '%s'.",
                        modifier.phase, modifier.modifier_id, flow_def.phases[-1],
                    )
                    modifier = copy.copy(modifier)
                    modifier.phase = flow_def.phases[-1]
                else:
                    result = ModifierApplyResult(
                        success=False,
                        modifier_id=modifier.modifier_id,
                        action=modifier.action,
                        target_flow_id=modifier.target_flow_id,
                        target_step_id=modifier.target_step_id,
                        skipped_reason=f"phase_not_found: {modifier.phase}"
                    )
                    self._log_modifier_skip(modifier, result.skipped_reason)
                    results.append(result)
                    continue

            if modifier.action == "inject_before":
                target = modifier.target_step_id or ""
                inject_before_groups.setdefault(target, []).append(modifier)
            elif modifier.action == "inject_after":
                target = modifier.target_step_id or ""
                inject_after_groups.setdefault(target, []).append(modifier)
            elif modifier.action == "append":
                phase = modifier.phase
                append_groups.setdefault(phase, []).append(modifier)
            else:
                other_modifiers.append(modifier)

        for key in inject_before_groups:
            inject_before_groups[key] = sorted(
                inject_before_groups[key],
                key=lambda m: (m.priority, m.step.get("id", "") if m.step else "", m.modifier_id)
            )
        for key in inject_after_groups:
            inject_after_groups[key] = sorted(
                inject_after_groups[key],
                key=lambda m: (m.priority, m.step.get("id", "") if m.step else "", m.modifier_id)
            )
        for key in append_groups:
            append_groups[key] = sorted(
                append_groups[key],
                key=lambda m: (m.priority, m.step.get("id", "") if m.step else "", m.modifier_id)
            )

        self._detect_conflicts(modifiers, results)

        for modifier in other_modifiers:
            result = self._apply_single_modifier(new_steps, modifier, flow_def.phases)
            results.append(result)

        for target_step_id, group in inject_before_groups.items():
            if target_step_id == "__first__":
                target_index = 0 if new_steps else -1
            elif target_step_id == "__last__":
                target_index = (len(new_steps) - 1) if new_steps else -1
            else:
                target_index = self._find_step_index(new_steps, target_step_id)
            if target_index < 0:
                for modifier in group:
                    result = ModifierApplyResult(
                        success=False,
                        modifier_id=modifier.modifier_id,
                        action=modifier.action,
                        target_flow_id=modifier.target_flow_id,
                        target_step_id=modifier.target_step_id,
                        skipped_reason=f"target_step_not_found: {target_step_id}"
                    )
                    self._log_modifier_skip(modifier, result.skipped_reason)
                    results.append(result)
                continue

            for i, modifier in enumerate(group):
                new_step = self._step_from_dict(modifier.step, modifier.phase, modifier.modifier_id)
                new_steps.insert(target_index + i, new_step)
                result = ModifierApplyResult(
                    success=True,
                    modifier_id=modifier.modifier_id,
                    action=modifier.action,
                    target_flow_id=modifier.target_flow_id,
                    target_step_id=modifier.target_step_id
                )
                results.append(result)
                self._log_modifier_success(modifier)

        for target_step_id, group in inject_after_groups.items():
            if target_step_id == "__first__":
                target_index = 0 if new_steps else -1
            elif target_step_id == "__last__":
                target_index = (len(new_steps) - 1) if new_steps else -1
            else:
                target_index = self._find_step_index(new_steps, target_step_id)
            if target_index < 0:
                for modifier in group:
                    result = ModifierApplyResult(
                        success=False,
                        modifier_id=modifier.modifier_id,
                        action=modifier.action,
                        target_flow_id=modifier.target_flow_id,
                        target_step_id=modifier.target_step_id,
                        skipped_reason=f"target_step_not_found: {target_step_id}"
                    )
                    self._log_modifier_skip(modifier, result.skipped_reason)
                    results.append(result)
                continue

            insert_pos = target_index + 1
            for i, modifier in enumerate(group):
                new_step = self._step_from_dict(modifier.step, modifier.phase, modifier.modifier_id)
                new_steps.insert(insert_pos + i, new_step)
                result = ModifierApplyResult(
                    success=True,
                    modifier_id=modifier.modifier_id,
                    action=modifier.action,
                    target_flow_id=modifier.target_flow_id,
                    target_step_id=modifier.target_step_id
                )
                results.append(result)
                self._log_modifier_success(modifier)

        for phase, group in append_groups.items():
            for modifier in group:
                self._action_append(new_steps, modifier, flow_def.phases)
                result = ModifierApplyResult(
                    success=True,
                    modifier_id=modifier.modifier_id,
                    action=modifier.action,
                    target_flow_id=modifier.target_flow_id,
                    target_step_id=modifier.target_step_id
                )
                results.append(result)
                self._log_modifier_success(modifier)

        new_flow_def = FlowDefinition(
            flow_id=flow_def.flow_id,
            inputs=copy.deepcopy(flow_def.inputs),
            outputs=copy.deepcopy(flow_def.outputs),
            phases=list(flow_def.phases),
            defaults=copy.deepcopy(flow_def.defaults),
            steps=new_steps,
            source_file=flow_def.source_file,
            source_type=flow_def.source_type,
            source_pack_id=flow_def.source_pack_id
        )

        if self._dry_run:
            return flow_def, results

        return new_flow_def, results

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    @staticmethod
    def _find_step_index(steps: List[FlowStep], step_id: str) -> int:
        for i, s in enumerate(steps):
            if s.id == step_id:
                return i
        return -1

    @staticmethod
    def _step_from_dict(
        step_dict: Optional[Dict[str, Any]],
        phase: str,
        modifier_id: str,
    ) -> FlowStep:
        if step_dict is None:
            step_dict = {}
        step_id = step_dict.get("id", f"_modifier_{modifier_id}")
        step_type = step_dict.get("type", "handler")
        priority = step_dict.get("priority", 100)
        when = step_dict.get("when")
        step_input = step_dict.get("input")
        output = step_dict.get("output")
        depends_on = step_dict.get("depends_on")
        principal_id = step_dict.get("principal_id")

        step = FlowStep(
            id=step_id,
            phase=phase,
            priority=priority,
            type=step_type,
            when=when,
            input=step_input,
            output=output,
            raw=step_dict,
            depends_on=depends_on,
        )
        # PR-C: principal_id 引き継ぎ
        if principal_id:
            step.principal_id = principal_id
        # python_file_call 固有フィールド
        if step_type == "python_file_call":
            step.owner_pack = step_dict.get("owner_pack")
            step.file = step_dict.get("file")
            step.timeout_seconds = step_dict.get("timeout_seconds", 60.0)
        return step

    def _apply_single_modifier(
        self,
        steps: List[FlowStep],
        modifier: FlowModifierDef,
        phases: List[str],
    ) -> ModifierApplyResult:
        if modifier.action == "replace":
            target_step_id = modifier.target_step_id or ""
            if target_step_id == "__first__":
                idx = 0 if steps else -1
            elif target_step_id == "__last__":
                idx = (len(steps) - 1) if steps else -1
            else:
                idx = self._find_step_index(steps, target_step_id)
            if idx < 0:
                result = ModifierApplyResult(
                    success=False,
                    modifier_id=modifier.modifier_id,
                    action=modifier.action,
                    target_flow_id=modifier.target_flow_id,
                    target_step_id=modifier.target_step_id,
                    skipped_reason=f"target_step_not_found: {target_step_id}",
                )
                self._log_modifier_skip(modifier, result.skipped_reason)
                return result
            new_step = self._step_from_dict(modifier.step, modifier.phase, modifier.modifier_id)
            steps[idx] = new_step
            self._log_modifier_success(modifier)
            return ModifierApplyResult(
                success=True,
                modifier_id=modifier.modifier_id,
                action=modifier.action,
                target_flow_id=modifier.target_flow_id,
                target_step_id=modifier.target_step_id,
            )
        elif modifier.action == "remove":
            target_step_id = modifier.target_step_id or ""
            if target_step_id == "__first__":
                idx = 0 if steps else -1
            elif target_step_id == "__last__":
                idx = (len(steps) - 1) if steps else -1
            else:
                idx = self._find_step_index(steps, target_step_id)
            if idx < 0:
                result = ModifierApplyResult(
                    success=False,
                    modifier_id=modifier.modifier_id,
                    action=modifier.action,
                    target_flow_id=modifier.target_flow_id,
                    target_step_id=modifier.target_step_id,
                    skipped_reason=f"target_step_not_found: {target_step_id}",
                )
                self._log_modifier_skip(modifier, result.skipped_reason)
                return result
            steps.pop(idx)
            self._log_modifier_success(modifier)
            return ModifierApplyResult(
                success=True,
                modifier_id=modifier.modifier_id,
                action=modifier.action,
                target_flow_id=modifier.target_flow_id,
                target_step_id=modifier.target_step_id,
            )
        else:
            return ModifierApplyResult(
                success=False,
                modifier_id=modifier.modifier_id,
                action=modifier.action,
                target_flow_id=modifier.target_flow_id,
                target_step_id=modifier.target_step_id,
                skipped_reason=f"unexpected_action_in_single: {modifier.action}",
            )

    def _action_append(
        self,
        steps: List[FlowStep],
        modifier: FlowModifierDef,
        phases: List[str],
    ) -> None:
        new_step = self._step_from_dict(modifier.step, modifier.phase, modifier.modifier_id)
        # phase 末尾に挿入
        insert_idx = len(steps)
        phase_idx = phases.index(modifier.phase) if modifier.phase in phases else len(phases) - 1
        # 自分の phase より後の phase に属するステップの直前に挿入
        for i, s in enumerate(steps):
            s_phase_idx = phases.index(s.phase) if s.phase in phases else len(phases) - 1
            if s_phase_idx > phase_idx:
                insert_idx = i
                break
        steps.insert(insert_idx, new_step)

    @staticmethod
    def _log_modifier_skip(modifier: FlowModifierDef, reason: Optional[str]) -> None:
        logger.warning(
            "[FlowModifier] Modifier '%s' (action=%s, target=%s) skipped: %s",
            modifier.modifier_id, modifier.action,
            modifier.target_step_id, reason,
        )

    @staticmethod
    def _log_modifier_success(modifier: FlowModifierDef) -> None:
        logger.debug(
            "[FlowModifier] Modifier '%s' (action=%s) applied successfully",
            modifier.modifier_id, modifier.action,
        )


# ======================================================================
# Global instances
# ======================================================================

_global_modifier_loader: Optional[FlowModifierLoader] = None
_global_modifier_applier: Optional[FlowModifierApplier] = None
_modifier_loader_lock = threading.Lock()
_modifier_applier_lock = threading.Lock()


def get_modifier_loader() -> FlowModifierLoader:
    from .di_container import get_container
    global _global_modifier_loader
    loader = get_container().get("modifier_loader")
    _global_modifier_loader = loader
    return loader


def get_modifier_applier() -> FlowModifierApplier:
    from .di_container import get_container
    global _global_modifier_applier
    applier = get_container().get("modifier_applier")
    _global_modifier_applier = applier
    return applier


def reset_modifier_loader() -> FlowModifierLoader:
    from .di_container import get_container
    global _global_modifier_loader
    with _modifier_loader_lock:
        _global_modifier_loader = FlowModifierLoader()
    get_container().set_instance("modifier_loader", _global_modifier_loader)
    return _global_modifier_loader


def reset_modifier_applier() -> FlowModifierApplier:
    from .di_container import get_container
    global _global_modifier_applier
    with _modifier_applier_lock:
        _global_modifier_applier = FlowModifierApplier()
    get_container().set_instance("modifier_applier", _global_modifier_applier)
    return _global_modifier_applier
