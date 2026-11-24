"""
kernel_flow_converter.py - Flow 変換ロジック共通化

kernel_core.py の _convert_new_flow_to_pipelines() と
kernel_handlers_runtime.py の _convert_new_flow_to_legacy() を統合。

M-10: 変換ロジック共通化
K-1: kernel_core.py 責務分割
"""

from __future__ import annotations

from typing import Any, Dict, List, TYPE_CHECKING

if TYPE_CHECKING:
    from .flow_loader import FlowDefinition, FlowStep


class FlowConverter:
    """
    Flow 形式変換エンジン

    2 つの変換を提供:
    1. convert_new_flow_to_pipelines: YAML steps → pipelines 形式 (startup用)
    2. convert_flow_def_to_legacy: FlowDefinition → legacy dict 形式 (IR登録用)
    """

    # ------------------------------------------------------------------
    # 1. New flow (dict) → pipelines 形式
    #    旧 kernel_core.py._convert_new_flow_to_pipelines
    # ------------------------------------------------------------------

    def convert_new_flow_to_pipelines(self, flow_def: Dict[str, Any]) -> Dict[str, Any]:
        """
        New flow 形式 (phases/steps) を pipelines 形式に変換する。

        Kernel.run_startup() が pipelines 形式を期待しているため。
        """
        result: Dict[str, Any] = {
            "flow_version": "2.0",
            "defaults": flow_def.get(
                "defaults",
                {"fail_soft": True, "on_missing_handler": "skip"},
            ),
            "pipelines": {"startup": []},
        }

        # C4: preserve schedule field through conversion
        if flow_def.get("schedule"):
            result["schedule"] = flow_def["schedule"]

        steps = flow_def.get("steps", [])
        phases = flow_def.get("phases", [])

        # phase順 → priority順 → id順 でソート
        phase_order = {p: i for i, p in enumerate(phases)}
        sorted_steps = sorted(
            steps,
            key=lambda s: (
                phase_order.get(s.get("phase", ""), 999),
                s.get("priority", 100),
                s.get("id", ""),
            ),
        )

        for step in sorted_steps:
            pipeline_step = self._convert_step_to_pipeline(step)
            result["pipelines"]["startup"].append(pipeline_step)

        return result

    def _convert_step_to_pipeline(self, step: Dict[str, Any]) -> Dict[str, Any]:
        """単一ステップを pipeline 形式に変換する。"""
        pipeline_step: Dict[str, Any] = {
            "id": step.get("id"),
            "run": {},
        }

        step_type = step.get("type", "handler")
        step_input = step.get("input", {})

        if step_type == "handler":
            if isinstance(step_input, dict):
                pipeline_step["run"]["handler"] = step_input.get(
                    "handler", "kernel:noop"
                )
                pipeline_step["run"]["args"] = step_input.get("args", {})
            else:
                pipeline_step["run"]["handler"] = "kernel:noop"
                pipeline_step["run"]["args"] = {}
        elif step_type == "python_file_call":
            pipeline_step["run"]["handler"] = "kernel:python_file_call"
            pipeline_step["run"]["args"] = {
                "file": step.get("file"),
                "owner_pack": step.get("owner_pack"),
                "principal_id": step.get("principal_id"),
                "input": step_input,
                "timeout_seconds": step.get("timeout_seconds", 60.0),
                "_step_id": step.get("id"),
                "_phase": step.get("phase"),
            }
        else:
            pipeline_step["run"]["handler"] = "kernel:noop"
            pipeline_step["run"]["args"] = {}

        # when 条件
        if step.get("when"):
            pipeline_step["when"] = step["when"]

        # output
        if step.get("output"):
            pipeline_step["output"] = step["output"]

        return pipeline_step

    # ------------------------------------------------------------------
    # 2. FlowDefinition → legacy dict 形式
    #    旧 kernel_handlers_runtime.py._convert_new_flow_to_legacy
    # ------------------------------------------------------------------

    def convert_flow_def_to_legacy(self, flow_def: "FlowDefinition") -> Dict[str, Any]:
        """
        FlowDefinition オブジェクトを既存 Kernel が処理できる辞書形式に変換する。
        """
        legacy_steps: List[Dict[str, Any]] = []

        for step in flow_def.steps:
            legacy_step = self._convert_flow_step_to_legacy(step)
            legacy_steps.append(legacy_step)

        return {
            "flow_id": flow_def.flow_id,
            "inputs": flow_def.inputs,
            "outputs": flow_def.outputs,
            "phases": flow_def.phases,
            "defaults": flow_def.defaults,
            "steps": legacy_steps,
            "_source_file": (
                str(flow_def.source_file) if flow_def.source_file else None
            ),
            "_source_type": flow_def.source_type,
            "_source_pack_id": flow_def.source_pack_id,
        }

    def _convert_flow_step_to_legacy(
        self, step: "FlowStep"
    ) -> Dict[str, Any]:
        """FlowStep を legacy dict に変換する。"""
        legacy_step: Dict[str, Any] = {
            "id": step.id,
            "phase": step.phase,
            "priority": step.priority,
        }

        # when
        if step.when:
            legacy_step["when"] = step.when

        # output
        if step.output:
            legacy_step["output"] = step.output

        # type ごとの変換
        if step.type == "python_file_call":
            legacy_step["handler"] = "kernel:python_file_call"
            legacy_step["args"] = {
                "file": step.file,
                "owner_pack": step.owner_pack,
                "principal_id": step.principal_id,
                "input": step.input,
                "timeout_seconds": step.timeout_seconds,
                "_step_id": step.id,
                "_phase": step.phase,
            }
            if step.output:
                legacy_step["output"] = step.output
        elif step.type == "set":
            legacy_step["handler"] = "kernel:ctx.set"
            if isinstance(step.input, dict):
                legacy_step["args"] = {
                    "key": step.input.get("key", step.output or ""),
                    "value": step.input.get("value"),
                }
            else:
                legacy_step["args"] = {
                    "key": step.output or "",
                    "value": step.input,
                }
        elif step.type == "if":
            legacy_step["handler"] = "kernel:noop"
            if isinstance(step.input, dict):
                legacy_step["when"] = step.input.get("condition", "false")
        elif step.type == "handler":
            if isinstance(step.input, dict):
                legacy_step["handler"] = step.input.get(
                    "handler", "kernel:noop"
                )
                legacy_step["args"] = step.input.get("args", {})
            else:
                legacy_step["handler"] = (
                    str(step.input) if step.input else "kernel:noop"
                )
                legacy_step["args"] = {}
        else:
            # 未知の type: noop として扱う
            legacy_step["handler"] = "kernel:noop"
            legacy_step["args"] = {
                "_unknown_type": step.type,
                "_raw": step.raw,
            }

        return legacy_step
