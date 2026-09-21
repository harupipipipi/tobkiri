"""Model selector reads remain captured, read-only and credential-free."""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
from typing import Any

import pytest

from core_runtime.global_contracts.http_contract_dispatch import HTTPContractTarget
from ecosystem.defaultspack.defaultspack.http_surface_presentation import (
    DefaultspackHTTPPresentation,
)
from ecosystem.defaultspack.defaultspack.model_profile_presentation import (
    MODEL_PROFILE_LIST_TARGET,
    present_model_profiles,
    present_model_profile_saved,
)
from ecosystem.rumi_model_registry_pack.runtime.registry import ModelRegistry


def test_model_save_preserves_broker_error_response() -> None:
    """A rejected write remains an error envelope, not a disconnected socket."""
    error = {"state": "error", "code": "provider_execution_failed"}
    assert present_model_profile_saved(error) == error


def test_model_list_is_bound_to_captured_profile() -> None:
    """A browser cannot choose another Profile or a mutation action."""
    target = HTTPContractTarget(*MODEL_PROFILE_LIST_TARGET)
    session = SimpleNamespace(profile_id="defaults", assert_current=lambda: None)
    presentation = DefaultspackHTTPPresentation()
    assert presentation.normalize_payload(
        target,
        {},
        session=session,
        workspace_binding_resolver=None,
    ) == {"profile_id": "defaults", "operation": "list"}
    for payload in ({"profile_id": "other"}, {"operation": "delete"}):
        with pytest.raises(ValueError):
            presentation.normalize_payload(
                target,
                payload,
                session=session,
                workspace_binding_resolver=None,
            )


def test_model_list_rejects_stale_capture() -> None:
    """Read normalization does not survive a changed active capture."""

    def stale() -> None:
        raise RuntimeError("stale capture")

    with pytest.raises(RuntimeError, match="stale capture"):
        DefaultspackHTTPPresentation().normalize_payload(
            HTTPContractTarget(*MODEL_PROFILE_LIST_TARGET),
            {},
            session=SimpleNamespace(profile_id="defaults", assert_current=stale),
            workspace_binding_resolver=None,
        )


def test_model_list_projects_real_registry_without_opaque_credentials(tmp_path: Path) -> None:
    """Project persisted identities, not synthetic catalog entries."""
    registry = ModelRegistry("defaults", user_data_root=tmp_path)
    registry.save(
        {
            "model_profile_id": "local-test",
            "display_name": "Local test",
            "model_id": "test-model",
            "requirements": {"preferred_provider_instance_id": "provider.fixture"},
            "credential_handle": "opaque:test-only",
            "parameters": {"private-note": "not-for-ui"},
            "metadata": {"private-note": "not-for-ui"},
        },
        expected_revision=0,
    )
    assert present_model_profiles(registry.snapshot()) == {
        "profiles": [
            {
                "profile_id": "local-test",
                "display_name": "Local test",
                "model_id": "test-model",
                "provider_id": "provider.fixture",
                "route_configured": True,
            }
        ],
        "count": 1,
        "registry_revision": 1,
    }
    assert present_model_profiles(ModelRegistry("other", user_data_root=tmp_path).snapshot()) == {
        "profiles": [],
        "count": 0,
        "registry_revision": 0,
    }


def test_model_list_preserves_broker_failure_and_omits_disabled_models() -> None:
    """A denied read or disabled model must not be presented as available."""
    failure = {"state": "error", "code": "authority_denied"}
    assert present_model_profiles(failure) == failure
    assert present_model_profiles(
        {
            "profiles": [
                {
                    "model_profile_id": "disabled",
                    "display_name": "Disabled",
                    "model_id": "test-model",
                    "enabled": False,
                }
            ]
        }
    ) == {"profiles": [], "count": 0}


@pytest.mark.parametrize("result", [{}, {"profiles": None}, {"profiles": [{}]}])
def test_model_list_rejects_invalid_provider_result(result: dict[str, Any]) -> None:
    """Missing results must not become a successful empty list."""
    with pytest.raises(ValueError):
        present_model_profiles(result)
