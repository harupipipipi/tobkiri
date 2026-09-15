from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from domain.ui_compiler import FoundationCandidate, FoundationSpec, UIAgentTask, UICompilerArtifactStore
from domain.ui_compiler.models import canonical_id

from .agent_backend import UIAgentBackend
from .project_writer import write_json
from .prompts import foundation_prompt
from domain.tool.schema_adapter import list_or_empty, mapping_or_empty


FOUNDATION_SPECIALIST_ROLES = [
    {
        "id": "product-intent",
        "prompt": "Define product mode, audience, task priorities, constraints, and trust/speed/readability/safety order.",
    },
    {
        "id": "typography",
        "prompt": "Design font role map for heading, body, label, numeric, code, and caption roles.",
    },
    {
        "id": "color-system",
        "prompt": "Design semantic color roles and reject arbitrary colors or generic gradients.",
    },
    {
        "id": "spacing-density",
        "prompt": "Design density mode, spacing relationships, gutters, and touch rhythm.",
    },
    {
        "id": "surface-policy",
        "prompt": "Design surface, border, shadow, radius, and emphasis policy while preventing box/card abuse.",
    },
    {
        "id": "motion-state",
        "prompt": "Design motion, focus, loading, success, warning, error, and disabled state visibility.",
    },
]


class FoundationGenerator:
    def __init__(self, *, backend: UIAgentBackend, store: UICompilerArtifactStore) -> None:
        self.backend = backend
        self.store = store

    def generate(
        self,
        *,
        run_id: str,
        run_root: Path,
        count: int,
        context: dict[str, Any] | None = None,
    ) -> list[FoundationCandidate]:
        candidates: list[FoundationCandidate] = []
        for index in range(max(1, count)):
            candidate_id = f"foundation-{index + 1}"
            output_dir = run_root / "foundation" / "candidates" / candidate_id
            specialist_tasks = self._run_specialist_tasks(
                run_id=run_id,
                candidate_id=candidate_id,
                output_dir=output_dir,
                context=context,
            )
            task = UIAgentTask(
                task_id=f"{run_id}-foundation-{index + 1}",
                run_id=run_id,
                node_id="foundation",
                candidate_id=candidate_id,
                kind="foundation",
                prompt=foundation_prompt(run_id=run_id, candidate_id=candidate_id),
                output_dir=str(output_dir),
                allowed_paths=[str(output_dir)],
                metadata={"candidateIndex": index, "specialistTasks": specialist_tasks},
            )
            self.store.save_agent_task(run_id=run_id, task_id=task.task_id, task=task.to_dict())
            result = self.backend.run_task(task, context)
            if not result.ok:
                self.store.save_foundation_candidate(
                    run_id=run_id,
                    candidate_id=candidate_id,
                    foundation={"candidateId": candidate_id},
                    report={"status": "fail", "agentResult": result.to_dict()},
                )
                continue
            write_json(output_dir / "specialist-manifest.json", {"specialists": specialist_tasks})
            candidate = self._read_candidate(candidate_id, output_dir)
            validation_report = _validate_foundation_output(output_dir, candidate.report)
            self.store.save_foundation_candidate(
                run_id=run_id,
                candidate_id=candidate_id,
                foundation=candidate.spec.to_dict(),
                report=validation_report,
                tokens_css=_read_text(output_dir / "tokens.css"),
                primitive_manifest=_read_json(output_dir / "primitive-manifest.json"),
            )
            if validation_report["status"] != "pass":
                continue
            candidate = FoundationCandidate(
                candidate_id=candidate.candidate_id,
                root=candidate.root,
                spec=candidate.spec,
                score=float(validation_report.get("score") or candidate.score),
                report=validation_report,
            )
            candidates.append(candidate)
        return candidates

    def _run_specialist_tasks(
        self,
        *,
        run_id: str,
        candidate_id: str,
        output_dir: Path,
        context: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        manifests: list[dict[str, Any]] = []
        base_dir = output_dir.parent.parent / "specialist-tasks" / candidate_id
        for role in FOUNDATION_SPECIALIST_ROLES:
            task_id = f"{run_id}-{candidate_id}-{role['id']}"
            role_dir = base_dir / role["id"]
            task = UIAgentTask(
                task_id=task_id,
                run_id=run_id,
                node_id="foundation",
                candidate_id=candidate_id,
                kind=f"foundation-{role['id']}",
                prompt=str(role["prompt"]),
                output_dir=str(role_dir),
                allowed_paths=[str(role_dir)],
                metadata={"role": role["id"], "stage": "foundation"},
            )
            self.store.save_agent_task(run_id=run_id, task_id=task_id, task=task.to_dict())
            result = self.backend.run_task(task, context)
            manifest = task.to_dict()
            write_json(role_dir / "task.json", manifest)
            write_json(role_dir / "result.json", result.to_dict())
            if not result.ok:
                raise RuntimeError(f"foundation specialist task failed: {role['id']}")
            manifests.append(
                {
                    "role": role["id"],
                    "taskId": task_id,
                    "outputDir": str(role_dir),
                    "result": result.to_dict(),
                }
            )
        return manifests


    def select(self, *, run_id: str, candidates: list[FoundationCandidate]) -> FoundationCandidate:
        if not candidates:
            raise ValueError("no foundation candidates generated")
        accepted = sorted(candidates, key=lambda item: (item.score, item.candidate_id))[0]
        root = Path(accepted.root)
        self.store.save_accepted_foundation(
            run_id=run_id,
            foundation=accepted.spec.to_dict(),
            selection={
                "status": "accepted",
                "acceptedCandidateId": accepted.candidate_id,
                "rejected": [
                    {"candidateId": item.candidate_id, "score": item.score}
                    for item in candidates
                    if item.candidate_id != accepted.candidate_id
                ],
            },
            tokens_css=_read_text(root / "tokens.css"),
            primitive_manifest=_read_json(root / "primitive-manifest.json"),
        )
        return accepted

    @staticmethod
    def _read_candidate(candidate_id: str, output_dir: Path) -> FoundationCandidate:
        payload = _read_json(output_dir / "foundation.json")
        if not payload:
            raise ValueError(f"foundation candidate missing foundation.json: {candidate_id}")
        report = _read_json(output_dir / "report.json") or {"status": "pass", "score": 0.5}
        spec = FoundationSpec(
            candidate_id=canonical_id(str(payload.get("candidateId") or candidate_id)),
            direction=mapping_or_empty(payload.get("direction")),
            typography=mapping_or_empty(payload.get("typography")),
            spacing=mapping_or_empty(payload.get("spacing")),
            color=mapping_or_empty(payload.get("color")),
            surface=mapping_or_empty(payload.get("surface")),
            primitives=list_or_empty(payload.get("primitives")),
        )
        return FoundationCandidate(
            candidate_id=spec.candidate_id,
            root=str(output_dir),
            spec=spec,
            score=float(report.get("score") or 0.5),
            report=report,
        )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def _validate_foundation_output(root: Path, base_report: dict[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    required = [
        "foundation.json",
        "tokens.css",
        "primitive-manifest.json",
        "specimen/type-specimen.html",
        "specimen/color-specimen.html",
        "specimen/density-specimen.html",
        "specimen/primitive-gallery.html",
    ]
    missing = [rel for rel in required if not (root / rel).is_file()]
    if missing:
        issues.append({"code": "MISSING_FOUNDATION_FILES", "severity": "blocker", "evidence": {"missing": missing}})
    primitive_manifest = _read_json(root / "primitive-manifest.json")
    primitives = primitive_manifest.get("primitives")
    if not isinstance(primitives, list) or not primitives:
        issues.append({"code": "MISSING_PRIMITIVE_MANIFEST", "severity": "blocker", "evidence": {}})
    else:
        missing_primitives = [
            str(name)
            for name in primitives
            if not (root / "primitives" / f"{name}.tsx").is_file()
        ]
        if missing_primitives:
            issues.append(
                {
                    "code": "MISSING_PRIMITIVE_SOURCE",
                    "severity": "blocker",
                    "evidence": {"missing": missing_primitives},
                }
            )
    non_token_colors = _non_token_color_files(root)
    if non_token_colors:
        issues.append(
            {
                "code": "FOUNDATION_NON_TOKEN_COLOR",
                "severity": "blocker",
                "evidence": {"files": non_token_colors},
            }
        )
    report = dict(base_report or {})
    report["issues"] = [*list_or_empty(report.get("issues")), *issues]
    report["status"] = "fail" if any(issue["severity"] == "blocker" for issue in report["issues"]) else "pass"
    if report["status"] == "fail":
        report["score"] = 1.0
    else:
        report["score"] = float(report.get("score") or 0.5)
    return report


def _non_token_color_files(root: Path) -> list[str]:
    offenders: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.name == "tokens.css":
            continue
        if path.suffix not in {".css", ".tsx", ".ts", ".jsx", ".js", ".html", ".json"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if HEX_COLOR_RE.search(text):
            offenders.append(str(path.relative_to(root)))
    return sorted(offenders)
