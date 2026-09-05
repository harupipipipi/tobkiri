"""Workflow Pack admission through the official v4 Profile ceremony."""

from __future__ import annotations

from pathlib import Path
import shutil
from dataclasses import replace

import pytest

from core_runtime.authority.v4 import AuthorityStore, DomainBoundary
from core_runtime.bootstrap.production_v4 import capture_production_dispatch
from core_runtime.host_provider_backend_v4 import ExactHostProviderBackendV4
from core_runtime.host_provider_hooks_v4 import load_host_provider_factory
from core_runtime.pack_catalog_backend_v4 import PackControlBackendV4
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile,
    prepare_default_profile_confirmation,
)
from core_runtime.pack_control_v4 import (
    CONTROL_PRESENTATION_CONTRACT,
    PACK_CONTROL_CONTRACT,
    capture_pack_control_session,
)
from tobkiri_host.errors import ResolutionError
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_host.errors import BackendUnavailableError


def _bundle_root() -> Path:
    from tests.conformance_support.packaged_profile import packaged_profile_bundle_root

    return packaged_profile_bundle_root()


PACK_ID = "tobkiri_workflow_pack"
SESSION_ID = f"{'a' * 64}.{'b' * 24}.1"


def _capture_control_session(**kwargs):
    """Compose the Defaultspack runtime surface explicitly for direct tests."""

    from ecosystem.defaultspack.domain.runtime_surface_v4 import (
        create_runtime_surface_services,
    )

    return capture_pack_control_session(
        runtime_surface_factory=create_runtime_surface_services,
        **kwargs,
    )


def _capture_defaultspack_dispatch(active: object, **kwargs: object):
    """Compose production dispatch with Defaultspack-owned dependencies."""

    from ecosystem.defaultspack.defaultspack.runtime_composition import (
        defaultspack_activation_snapshot_loader,
    )
    from ecosystem.defaultspack.domain.runtime_surface_v4 import (
        create_runtime_surface_services,
    )

    return capture_production_dispatch(
        active,
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
        **kwargs,
    )


def _invoke(session, contract: str, operation: str, payload: dict | None = None):
    return session.invoke(
        contract,
        operation,
        {**(payload or {}), "_session_id": SESSION_ID},
    )


def test_optional_workflow_pack_enters_closure_only_after_full_ceremony(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Install, approval, enable, and Profile ceremony select exact v4 bindings."""

    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    capture_default_profile(confirmation=prepare_default_profile_confirmation())
    session = _capture_control_session()

    catalog = _invoke(session, PACK_CONTROL_CONTRACT, "catalog.read")
    workflow = next(item for item in catalog["packs"] if item["pack_id"] == PACK_ID)
    assert workflow == {
        **workflow,
        "required": False,
        "installed": False,
        "approved": False,
        "enabled": False,
    }
    _invoke(session, PACK_CONTROL_CONTRACT, "pack.install", {"pack_id": PACK_ID})
    candidate = _invoke(session, PACK_CONTROL_CONTRACT, "approval.candidate", {"pack_id": PACK_ID})
    _invoke(
        session,
        PACK_CONTROL_CONTRACT,
        "approval.approve",
        {"pack_id": PACK_ID, "candidate_id": candidate["candidate_id"]},
    )
    enabled = _invoke(session, PACK_CONTROL_CONTRACT, "pack.enable", {"pack_id": PACK_ID})
    assert enabled["enabled"] is True

    profile = _invoke(session, CONTROL_PRESENTATION_CONTRACT, "profile.read")
    desired = [
        item["pack_id"]
        for item in profile["data"]["profile_document"]["packs"]
        if item.get("role") != "application"
    ]
    assert PACK_ID in desired
    resolved = _invoke(
        session,
        CONTROL_PRESENTATION_CONTRACT,
        "profile.change.resolve",
        {
            "profile_id": "defaults",
            "expected_profile_revision": profile["profile_revision"],
            "expected_plan_digest": profile["plan_digest"],
            "desired_pack_ids": desired,
        },
    )
    review = resolved["review"]
    assert PACK_ID in {item["pack_id"] for item in review["profile"]["packs"]}
    assert PACK_ID in {item["identity"] for item in review["profile_lock"]["effective_set"]}
    workflow_bindings = [
        item for item in review["resolved_plan"]["bindings"] if item["pack_id"] == PACK_ID
    ]
    assert workflow_bindings
    assert {item["contract_id"] for item in workflow_bindings} == {"tobkiri.workflow.v4"}
    assert {item["function_principal"]["function_id"] for item in workflow_bindings} == {
        "tobkiri.workflow.provider"
    }

    reviewed = _invoke(
        session,
        CONTROL_PRESENTATION_CONTRACT,
        "profile.change.review",
        {
            "candidate_id": resolved["candidate_id"],
            "candidate_digest": resolved["candidate_digest"],
        },
    )
    approved = _invoke(
        session,
        CONTROL_PRESENTATION_CONTRACT,
        "profile.change.approve",
        {
            "candidate_id": reviewed["candidate_id"],
            "candidate_digest": reviewed["candidate_digest"],
        },
    )
    activated = _invoke(
        session,
        CONTROL_PRESENTATION_CONTRACT,
        "profile.change.activate",
        {
            "approval_id": approved["approval_id"],
            "approval_digest": approved["approval_digest"],
        },
    )
    assert activated["state"] == "active"
    assert activated["plan_digest"] == review["resolved_plan"]["plan_digest"]
    status = _invoke(session, PACK_CONTROL_CONTRACT, "pack.status", {"pack_id": PACK_ID})
    assert status["installed"] is True
    assert status["approved"] is True
    assert status["enabled"] is True

    # Rebuild the production runtime from only the durable activation and
    # exercise the exact operation route.  This is the packaged restart path
    # that previously reapplied a hidden v1-only compatibility constraint.
    restarted_active = capture_default_profile()
    authority = AuthorityStore(tmp_path / "user-data" / "authority" / "v4.sqlite3")
    restarted = _capture_defaultspack_dispatch(
        restarted_active,
        bundle_root=_bundle_root(),
        ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
        authority_store=authority,
    )
    try:
        binding = restarted.broker._catalog.resolve_pinned(
            "tobkiri.workflow.v4",
            "operation.palette",
        )
        provider_authority = next(
            item
            for item in authority.list_provider_authorities()
            if item.provider.principal_id == binding.principal_ref.value
        )
        extension_trust = authority.get_host_extension_trust(provider_authority.host_extension_id)
        assert extension_trust is not None
        assert extension_trust.package_kind == "host_extension"
        assert extension_trust.parent_artifact_digest == binding.artifact.digest
        assert extension_trust.provider_principal_ids == (binding.principal_ref.value,)
        provider_domain = authority.get_domain(provider_authority.execution_domain_id)
        assert provider_domain is not None
        assert provider_domain.boundary is DomainBoundary.DEDICATED_PROCESS
        assert binding.operation.contract_version == "4.0.0"
        assert restarted.provider_metadata("tobkiri.workflow.v4")
        restarted.assert_operation_ready(
            "tobkiri.workflow.v4",
            "operation.palette",
        )
        workflow_backend = restarted.broker._backends.select(binding)
        assert isinstance(workflow_backend, ExactHostProviderBackendV4)
        control_binding = restarted.broker._catalog.resolve_pinned(
            PACK_CONTROL_CONTRACT,
            "catalog.read",
        )
        assert isinstance(
            restarted.broker._backends.select(control_binding),
            PackControlBackendV4,
        )
        with pytest.raises(BackendUnavailableError, match="not installed"):
            restarted.broker._backends.select(
                replace(
                    binding,
                    principal_ref=OpaqueAuthorityRef("sha256:" + "0" * 64),
                )
            )
        tampered_root = tmp_path / PACK_ID
        shutil.copytree(
            Path(__file__).resolve().parents[1] / "ecosystem" / PACK_ID,
            tampered_root,
        )
        executable = tampered_root / "runtime" / "provider.py"
        executable.write_text(
            executable.read_text(encoding="utf-8") + "\n# tampered\n",
            encoding="utf-8",
        )
        with pytest.raises(Exception, match="digest"):
            load_host_provider_factory(tampered_root, binding)
        with pytest.raises(ResolutionError, match="incompatible"):
            restarted.broker._catalog.resolve(
                "tobkiri.workflow.v4",
                "operation.palette",
                ">=1,<2",
            )
        palette = restarted.invoke(
            "tobkiri.workflow.v4",
            "operation.palette",
            {"_session_id": "workflow-operation"},
        )
        step_target = next(
            item
            for item in palette["operations"]
            if item["contract_id"] == "conversation.turn.v1" and item["operation_id"] == "complete"
        )
        document = {
            "workflow_api_version": "io.tobkiri.workflow.v4",
            "name": "Restart-persistent skipped step",
            "max_concurrency": 1,
            "steps": [
                {
                    "id": "skip",
                    "when": "false",
                    "request": {
                        "contract_id": step_target["contract_id"],
                        "contract_revision_digest": step_target["contract_revision_digest"],
                        "operation_id": step_target["operation_id"],
                        "function_principal_id": step_target["function_principal_id"],
                        "input": {"messages": "${inputs.messages}"},
                    },
                    "retry": {"max_attempts": 1, "backoff_ms": 0},
                }
            ],
        }
        created = restarted.invoke(
            "tobkiri.workflow.v4",
            "definition.create",
            {
                "_session_id": "workflow-operation",
                "definition_id": "workflow.restart-proof",
                "document": document,
            },
        )
        restarted.invoke(
            "tobkiri.workflow.v4",
            "definition.publish",
            {
                "_session_id": "workflow-operation",
                "definition_id": "workflow.restart-proof",
                "if_match": created["etag"],
            },
        )
        run = restarted.invoke(
            "tobkiri.workflow.v4",
            "run.create",
            {
                "_session_id": "workflow-operation",
                "definition_id": "workflow.restart-proof",
                "run_id": "workflow-run-restart-proof",
                "inputs": {"messages": [{"role": "user"}]},
            },
        )
        assert run["state"] == "queued"
        attempt = restarted.invoke(
            "tobkiri.workflow.v4",
            "run.step.execute",
            {
                "_session_id": "workflow-operation",
                "run_id": run["run_id"],
                "step_id": "skip",
            },
        )
        assert attempt["state"] == "succeeded"
        assert attempt["skipped"] is True
    finally:
        restarted.close()

    restarted_again = _capture_defaultspack_dispatch(
        capture_default_profile(),
        bundle_root=_bundle_root(),
        ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
        authority_store=AuthorityStore(tmp_path / "user-data" / "authority" / "v4.sqlite3"),
    )
    try:
        persisted = restarted_again.invoke(
            "tobkiri.workflow.v4",
            "run.get",
            {
                "_session_id": "workflow-operation-restart",
                "run_id": "workflow-run-restart-proof",
            },
        )
        assert persisted["run"]["state"] == "succeeded"
        assert len(persisted["attempts"]) == 1
        assert persisted["attempts"][0]["state"] == "succeeded"
        assert persisted["attempts"][0]["skipped"] is True
    finally:
        restarted_again.close()
