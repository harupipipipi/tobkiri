from __future__ import annotations

from pathlib import Path
from typing import Any

from domain.ui_compiler import UIAgentResult, UIAgentTask
from domain.tool.schema_adapter import list_or_empty, mapping_or_empty

from .project_writer import list_relative_files, write_json, write_text


SPECIALIST_TASK_KINDS = {
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
}


class FakeUIAgentBackend:
    """Deterministic backend used by tests and dogfood runs."""

    def run_task(self, task: UIAgentTask, context: dict[str, Any] | None = None) -> UIAgentResult:
        output_dir = Path(task.output_dir)
        if output_dir.exists() and any(output_dir.iterdir()):
            return UIAgentResult(
                status="error",
                task_id=task.task_id,
                output_dir=str(output_dir),
                message="fake backend refuses to patch a non-empty output directory",
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        if task.kind == "foundation":
            self._write_foundation(task, output_dir)
        elif task.kind.startswith("foundation-") or task.kind in SPECIALIST_TASK_KINDS:
            self._write_specialist_result(task, output_dir)
        else:
            self._write_component(task, output_dir)
        return UIAgentResult(
            status="ok",
            task_id=task.task_id,
            output_dir=str(output_dir),
            message="deterministic fake bundle generated",
            files=list_relative_files(output_dir),
            metadata={"backend": "fake"},
        )

    def _write_specialist_result(self, task: UIAgentTask, output_dir: Path) -> None:
        role = str(task.metadata.get("role") or task.kind)
        write_json(
            output_dir / "specialist-output.json",
            {
                "role": role,
                "kind": task.kind,
                "taskId": task.task_id,
                "status": "pass",
                "summary": f"{role} specialist completed deterministic planning.",
                "decisions": {
                    "productMode": "utility",
                    "density": "compact",
                    "avoid": ["generic-gradient", "card-abuse", "tiny-font-escape"],
                },
            },
        )
        write_json(output_dir / "report.json", {"status": "pass", "role": role})

    def _write_foundation(self, task: UIAgentTask, output_dir: Path) -> None:
        candidate_id = task.candidate_id
        variant_index = _variant_index(candidate_id)
        primitives = [
            "Button",
            "TextInput",
            "TextArea",
            "Select",
            "InlineAlert",
            "Surface",
            "Badge",
            "Tabs",
            "Dialog",
            "IconButton",
            "SegmentedControl",
            "SearchField",
        ]
        foundation = {
            "candidateId": candidate_id,
            "direction": {
                "productMode": "utility",
                "qualities": ["precise", "calm", "information-forward"],
                "avoid": ["toy-like", "over-compressed", "decorative-gradient"],
            },
            "typography": {
                "fontStack": "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
                "roles": {
                    "pageTitle": "type-title-lg",
                    "sectionTitle": "type-title-sm",
                    "body": "type-body",
                    "denseBody": "type-body-dense",
                    "label": "type-label",
                    "caption": "type-caption",
                    "numeric": "type-numeric",
                    "code": "type-code",
                },
            },
            "spacing": {
                "scale": [4, 6, 8, 12, 16, 20, 24, 32],
                "relationships": {
                    "withinControl": 8 + variant_index,
                    "withinGroup": 12 + variant_index,
                    "betweenGroups": 20,
                    "betweenRegions": 32,
                },
            },
            "color": {
                "roles": [
                    "canvas",
                    "surface",
                    "surfaceRaised",
                    "textPrimary",
                    "textSecondary",
                    "textMuted",
                    "borderSubtle",
                    "borderStrong",
                    "actionPrimary",
                    "statusCritical",
                    "statusWarning",
                    "statusPositive",
                ]
            },
            "surface": {"maxNestedDepth": 1, "borderPolicy": "semantic-only", "shadowPolicy": "rare"},
            "primitives": primitives,
        }
        write_json(output_dir / "foundation.json", foundation)
        write_json(output_dir / "primitive-manifest.json", {"primitives": primitives})
        for primitive in primitives:
            write_text(
                output_dir / "primitives" / f"{primitive}.tsx",
                "\n".join(
                    [
                        f"export function {primitive}(props: Record<string, unknown>) {{",
                        f"  return <div data-rui-primitive=\"{primitive}\" {{...props}} />;",
                        "}",
                    ]
                ),
            )
        write_text(
            output_dir / "tokens.css",
            "\n".join(
                [
                    ":root {",
                    "  --rui-canvas: rgb(248 249 251);",
                    "  --rui-surface: rgb(255 255 255);",
                    "  --rui-text-primary: rgb(18 24 32);",
                    "  --rui-text-secondary: rgb(79 90 106);",
                    "  --rui-border-subtle: rgb(218 224 232);",
                    "  --rui-action-primary: rgb(28 105 212);",
                    "  --rui-status-critical: rgb(184 40 52);",
                    "  --rui-space-2: 8px;",
                    "  --rui-space-3: 12px;",
                    "  --rui-space-4: 16px;",
                    "  --rui-space-5: 20px;",
                    "  --rui-radius-control: 6px;",
                    "}",
                ]
            ),
        )
        write_text(output_dir / "specimen" / "type-specimen.html", "<main><h1>Rumi UI Foundation</h1><p>日本語の長文と数値 12345</p></main>")
        write_text(output_dir / "specimen" / "color-specimen.html", "<main><p>semantic color roles</p></main>")
        write_text(output_dir / "specimen" / "density-specimen.html", "<main><section>comfortable compact data dense</section></main>")
        write_text(output_dir / "specimen" / "primitive-gallery.html", "<main><button>Button</button><input aria-label=\"Text input\"></main>")
        write_json(output_dir / "report.json", {"status": "pass", "score": round(0.08 + variant_index * 0.03, 3)})

    def _write_component(self, task: UIAgentTask, output_dir: Path) -> None:
        contract = mapping_or_empty(task.metadata.get("contract"))
        node_id = str(contract.get("id") or task.node_id)
        candidate_id = task.candidate_id
        fail_mode = str(task.metadata.get("fakeFailMode") or "")
        required_states = list_or_empty(contract.get("requiredStates"))
        allowed_primitives = list_or_empty(contract.get("allowedPrimitives"))
        visible_budget = int(contract.get("visibleActionBudget") or 3)
        slot_mappings = list_or_empty(contract.get("slotMappings"))
        action_count = visible_budget + 2 if fail_mode == "action-pressure" else max(1, min(visible_budget, 2))
        states = required_states if fail_mode != "missing-state" else required_states[:1]
        design_intent = {
            "primaryPerceptualTask": contract.get("primaryPerceptualTask") or contract.get("purpose") or node_id,
            "visualFocus": "primary content first, actions second",
            "readingOrder": ["title", "state", "content", "actions"],
            "visibleAtRest": ["primary content", "one primary action"],
            "progressivelyDisclosed": ["secondary metadata"],
            "typographyRoles": ["sectionTitle", "body", "label"],
            "colorRoles": ["surface", "textPrimary", "textSecondary", "actionPrimary"],
            "spacingRelationships": ["withinGroup", "betweenGroups"],
            "overflowStrategy": "wrap long Japanese text and keep primary action reachable",
            "responsiveTopology": contract.get("layoutEnvelope", {}).get("mobileBehavior", "stack")
            if isinstance(contract.get("layoutEnvelope"), dict)
            else "stack",
            "compressionAvoidancePlan": "keep gutters, reduce visible secondary actions, preserve line-height",
        }
        manifest = {
            "nodeId": node_id,
            "candidateId": candidate_id,
            "implementationMode": contract.get("implementationMode") or "component",
            "sourceFiles": ["source/Component.tsx", "source/Component.module.css"],
            "fixtureFiles": [f"fixtures/{name}.json" for name in ["default", "long", "empty", "loading", "error"]],
            "requiredStates": states,
            "allowedPrimitives": allowed_primitives,
            "visibleActionBudget": visible_budget,
            "visibleActionCount": action_count,
            "slotMappings": slot_mappings,
            "designIntent": design_intent,
        }
        manifest.update(_compression_failure_manifest(fail_mode))
        css_extra = " color: #123456;" if fail_mode == "non-token-color" else ""
        write_json(output_dir / "design-intent.json", design_intent)
        write_json(output_dir / "component.manifest.json", manifest)
        write_text(
            output_dir / "source" / "Component.tsx",
            "\n".join(
                [
                    "import type { ReactNode } from 'react';",
                    "import styles from './Component.module.css';",
                    "",
                    "type ComponentProps = { children?: ReactNode; [key: string]: unknown };",
                    "",
                    "export default function Component(props: ComponentProps) {",
                    f"  const actions = {action_count};",
                    "  const { children } = props;",
                    "  return (",
                    f"    <section className={{styles.root}} data-node-id=\"{node_id}\" data-visible-actions={{actions}}>",
                    f"      <h2>{_title(node_id)}</h2>",
                    f"      <p>{_purpose(contract)}</p>",
                    "      <div className={styles.actions}>",
                    "        {Array.from({ length: actions }).map((_, index) => (",
                    "          <button key={index} className={styles.button}>Action {index + 1}</button>",
                    "        ))}",
                    "      </div>",
                    "      {children}",
                    "    </section>",
                    "  );",
                    "}",
                ]
            ),
        )
        write_text(
            output_dir / "source" / "Component.module.css",
            "\n".join(
                [
                    ".root {",
                    "  background: var(--rui-surface);",
                    "  color: var(--rui-text-primary);",
                    "  border: 1px solid var(--rui-border-subtle);",
                    "  border-radius: var(--rui-radius-control);",
                    "  padding: var(--rui-space-4);",
                    "  display: grid;",
                    "  gap: var(--rui-space-3);",
                    "  line-height: 1.5;",
                    f"  {css_extra}",
                    "}",
                    ".actions { display: flex; flex-wrap: wrap; gap: var(--rui-space-2); }",
                    ".button { min-height: 36px; padding: 0 var(--rui-space-3); background: var(--rui-action-primary); color: var(--rui-surface); border: 0; border-radius: var(--rui-radius-control); }",
                ]
            ),
        )
        write_text(output_dir / "source" / "Component.test.tsx", "export const testContract = true;")
        write_text(output_dir / "source" / "Component.stories.tsx", "export const Default = {};")
        for state in ["default", "long", "empty", "loading", "error"]:
            write_json(output_dir / "fixtures" / f"{state}.json", {"state": state, "nodeId": node_id})
        write_json(output_dir / "status.json", {"status": "generated", "backend": "fake", "failMode": fail_mode})


def _variant_index(candidate_id: str) -> int:
    suffix = str(candidate_id).rsplit("-", 1)[-1]
    if suffix.isdigit():
        return int(suffix)
    return sum(ord(char) for char in str(candidate_id)) % 3


def _title(node_id: str) -> str:
    return str(node_id).replace("-", " ").title()


def _purpose(contract: dict[str, Any]) -> str:
    return str(contract.get("purpose") or "Generated Rumi UI component.")


def _compression_failure_manifest(fail_mode: str) -> dict[str, Any]:
    if fail_mode == "gap-pressure":
        return {"actualGap": 4}
    if fail_mode == "boundary-pressure":
        return {"actualPadding": 6}
    if fail_mode == "primary-clipped":
        return {"forcePrimaryClipped": True}
    if fail_mode == "horizontal-overflow":
        return {"forceHorizontalOverflow": True}
    if fail_mode == "nested-surfaces":
        return {"surfaceDepth": 3, "dividerCount": 6, "shadowCount": 2}
    if fail_mode == "flat-hierarchy":
        return {"hierarchyContrast": 0.1}
    if fail_mode == "tiny-font":
        return {"fontSize": 10, "lineHeight": 13}
    if fail_mode == "touch-target":
        return {"touchTargetMin": 28}
    if fail_mode == "toolbar-overflow":
        return {"toolbarRows": 3}
    if fail_mode == "primary-action-unreachable":
        return {"primaryActionReachable": False}
    return {}
