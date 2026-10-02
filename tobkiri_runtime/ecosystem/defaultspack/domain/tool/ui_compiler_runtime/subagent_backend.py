from __future__ import annotations

from pathlib import Path
from typing import Any

from domain.ui_compiler import UIAgentResult, UIAgentTask


class SubagentToolBackend:
    """Production backend that delegates implementation work to the existing subagent route."""

    def run_task(self, task: UIAgentTask, context: dict[str, Any] | None = None) -> UIAgentResult:
        context = context if isinstance(context, dict) else {}
        output_dir = Path(task.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            from importlib import import_module

            run_subagent_compat = import_module("domain.agent.subagent_orchestrator").run_subagent_compat
            role_id = _role_id_for_task(task)
            result = run_subagent_compat(
                role_id,
                {
                    "task": task.prompt,
                    "output_dir": str(output_dir),
                    "allowed_paths": list(task.allowed_paths),
                    "metadata": {"uiCompilerRole": role_id, **dict(task.metadata)},
                },
                model=str(task.metadata.get("model") or ""),
                context=context,
                call_handler=context.get("call_handler"),
                **(
                    {"settings_owner": context.get("_settings_owner_port")}
                    if context.get("_settings_owner_port") is not None
                    else {}
                ),
            )
        except Exception as exc:
            return UIAgentResult(
                status="error",
                task_id=task.task_id,
                output_dir=str(output_dir),
                message=f"subagent failed: {exc}",
            )
        files = _relative_files(output_dir)
        if not files:
            return UIAgentResult(
                status="error",
                task_id=task.task_id,
                output_dir=str(output_dir),
                message="subagent completed without writing a bundle",
                metadata={"subagent": result},
            )
        return UIAgentResult(
            status="ok",
            task_id=task.task_id,
            output_dir=str(output_dir),
            message="subagent bundle generated",
            files=files,
            metadata={"subagent": result},
        )


def _relative_files(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


def _role_id_for_task(task: UIAgentTask) -> str:
    kind = str(task.kind or "").strip()
    if kind.startswith("foundation-"):
        return kind
    if kind == "foundation":
        return "foundation-synthesizer"
    if kind == "leaf":
        return f"leaf-component-{task.node_id}"
    if kind in {
        "intent",
        "topology",
        "semantic-region",
        "state-audit",
        "responsive",
        "accessibility",
        "text-pressure-audit",
        "compression-audit",
        "candidate-selector",
        "composition",
        "refinement-selector",
        "audit",
    }:
        return kind
    return "recursive-ui-delegate"
