from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from domain.ui_compiler import (
    CandidateBundle,
    ComponentBundleManifest,
    ComponentContract,
    UIAgentTask,
    UICompilerArtifactStore,
)

from .agent_backend import UIAgentBackend
from .prompts import leaf_prompt
from .validation import validate_candidate_bundle
from domain.tool.schema_adapter import list_or_empty, mapping_or_empty


class CandidateGenerator:
    def __init__(self, *, backend: UIAgentBackend, store: UICompilerArtifactStore) -> None:
        self.backend = backend
        self.store = store

    def generate_for_contracts(
        self,
        *,
        run_id: str,
        run_root: Path,
        contracts: list[ComponentContract],
        foundation: dict[str, Any],
        context: dict[str, Any] | None = None,
        fake_failures: dict[str, str] | None = None,
    ) -> dict[str, list[CandidateBundle]]:
        bundles: dict[str, list[CandidateBundle]] = {}
        for contract in contracts:
            bundles[contract.id] = self.generate_for_contract(
                run_id=run_id,
                run_root=run_root,
                contract=contract,
                foundation=foundation,
                context=context,
                fake_failures=fake_failures or {},
            )
        return bundles

    def generate_for_contract(
        self,
        *,
        run_id: str,
        run_root: Path,
        contract: ComponentContract,
        foundation: dict[str, Any],
        context: dict[str, Any] | None = None,
        fake_failures: dict[str, str] | None = None,
    ) -> list[CandidateBundle]:
        contract_payload = contract.to_dict()
        bundles: list[CandidateBundle] = []
        for index in range(max(1, int(contract.candidate_count))):
            candidate_id = f"candidate-{index + 1}"
            output_dir = run_root / "candidates" / contract.id / candidate_id
            fail_key = f"{contract.id}/{candidate_id}"
            task = UIAgentTask(
                task_id=f"{run_id}-{contract.id}-{candidate_id}",
                run_id=run_id,
                node_id=contract.id,
                candidate_id=candidate_id,
                kind="leaf",
                prompt=leaf_prompt(contract=contract_payload, foundation=foundation, candidate_id=candidate_id),
                output_dir=str(output_dir),
                allowed_paths=[str(output_dir)],
                metadata={
                    "contract": contract_payload,
                    "foundation": foundation,
                    "candidateIndex": index,
                    "fakeFailMode": (fake_failures or {}).get(fail_key, ""),
                },
            )
            self.store.save_agent_task(run_id=run_id, task_id=task.task_id, task=task.to_dict())
            result = self.backend.run_task(task, context)
            validation = validate_candidate_bundle(output_dir, contract_payload) if result.ok else {
                "status": "fail",
                "issues": [{"code": "AGENT_FAILED", "severity": "blocker", "evidence": result.to_dict()}],
                "manifest": {},
                "designIntent": {},
            }
            manifest = _manifest_from_payload(contract, candidate_id, validation)
            bundle = CandidateBundle(
                node_id=contract.id,
                candidate_id=candidate_id,
                root=str(output_dir),
                manifest=manifest,
                agent_result=result,
            )
            self.store.save_candidate_bundle(
                run_id=run_id,
                node_id=contract.id,
                candidate_id=candidate_id,
                bundle=bundle.to_dict(),
                status={
                    "status": validation["status"],
                    "validation": validation,
                    "agentResult": result.to_dict(),
                },
            )
            bundles.append(bundle)
        return bundles

    def generate_retry_for_contract(
        self,
        *,
        run_id: str,
        run_root: Path,
        contract: ComponentContract,
        foundation: dict[str, Any],
        attempt: int,
        context: dict[str, Any] | None = None,
        fake_failures: dict[str, str] | None = None,
    ) -> CandidateBundle:
        candidate_id = f"candidate-retry-{max(1, int(attempt))}"
        contract_payload = contract.to_dict()
        output_dir = run_root / "candidates" / contract.id / candidate_id
        fail_key = f"{contract.id}/{candidate_id}"
        task = UIAgentTask(
            task_id=f"{run_id}-{contract.id}-{candidate_id}",
            run_id=run_id,
            node_id=contract.id,
            candidate_id=candidate_id,
            kind="leaf",
            prompt=leaf_prompt(contract=contract_payload, foundation=foundation, candidate_id=candidate_id),
            output_dir=str(output_dir),
            allowed_paths=[str(output_dir)],
            metadata={
                "contract": contract_payload,
                "foundation": foundation,
                "retryAttempt": attempt,
                "regenerateInsteadOfPatch": True,
                "fakeFailMode": (fake_failures or {}).get(fail_key, ""),
            },
        )
        self.store.save_agent_task(run_id=run_id, task_id=task.task_id, task=task.to_dict())
        result = self.backend.run_task(task, context)
        validation = validate_candidate_bundle(output_dir, contract_payload) if result.ok else {
            "status": "fail",
            "issues": [{"code": "AGENT_FAILED", "severity": "blocker", "evidence": result.to_dict()}],
            "manifest": {},
            "designIntent": {},
        }
        manifest = _manifest_from_payload(contract, candidate_id, validation)
        bundle = CandidateBundle(
            node_id=contract.id,
            candidate_id=candidate_id,
            root=str(output_dir),
            manifest=manifest,
            agent_result=result,
        )
        self.store.save_candidate_bundle(
            run_id=run_id,
            node_id=contract.id,
            candidate_id=candidate_id,
            bundle=bundle.to_dict(),
            status={
                "status": validation["status"],
                "validation": validation,
                "agentResult": result.to_dict(),
                "retryAttempt": attempt,
            },
        )
        return bundle


def _manifest_from_payload(
    contract: ComponentContract,
    candidate_id: str,
    validation: dict[str, Any],
) -> ComponentBundleManifest:
    payload = mapping_or_empty(validation.get("manifest"))
    return ComponentBundleManifest(
        node_id=str(payload.get("nodeId") or contract.id),
        candidate_id=str(payload.get("candidateId") or candidate_id),
        implementation_mode=str(payload.get("implementationMode") or contract.implementation_mode),
        source_files=_list(payload.get("sourceFiles")) or ["source/Component.tsx"],
        fixture_files=_list(payload.get("fixtureFiles")),
        required_states=_list(payload.get("requiredStates")),
        allowed_primitives=_list(payload.get("allowedPrimitives")),
        visible_action_budget=int(payload.get("visibleActionBudget") or contract.visible_action_budget),
        slot_mappings=list_or_empty(payload.get("slotMappings")),
        design_intent=mapping_or_empty(payload.get("designIntent") or validation.get("designIntent")),
    )


def read_candidate_manifest(root: Path) -> dict[str, Any]:
    try:
        payload = json.loads((root / "component.manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _list(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []
