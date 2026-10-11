"""Composer owner lookup units using verified canonical topology, not Grants."""

from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any
import inspect

import pytest
from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.bootstrap.saved_tool_policy_composition_v4 import (
    compose_saved_tool_policy_v4,
)
from ecosystem.tobkiri_ui_settings_pack.runtime import settings
from tobkiri_protocol.bundle_catalog import BundledCatalog

RUNTIME = Path(__file__).resolve().parents[1]


class _ReachedSettingsOwner(Exception):
    """Stop after the original finite settings capture guard has succeeded."""


@pytest.mark.parametrize("failure", [None, "missing", "duplicate", "foreign"])
def test_composer_resolves_canonical_settings_owner_and_preserves_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    """Exercise original lookup and settings guard without creating authority."""
    catalog = BundledCatalog.load(RUNTIME / "ecosystem/defaultspack/v4")
    functions = {
        function["id"]: function
        for pack in catalog.packs.values()
        for function in pack["functions"]
    }
    profile = next(iter(catalog.profiles.values()))
    bindings: dict[tuple[str, str], Any] = {}
    for edge in profile["requested_edges"]:
        function_id = edge["target_provider_id"]
        assert function_id in functions
        key = (edge["contract_id"], edge["operation_id"])
        bindings[key] = NS(
            function=NS(function_id=function_id),
            principal_ref=NS(value=function_id),
            operation=NS(
                contract_id=key[0], operation_id=key[1], contract_version="1.0.0"
            ),
        )
    policy = bindings[
        ("tobkiri.action.host.approval-policy.v1", "host.action_approval_policy.select")
    ]
    edges = [
        NS(
            caller=NS(
                function_id=e["caller_function_id"],
                principal_id=e["caller_function_id"],
            ),
            target=NS(principal_id=e["target_provider_id"]),
            authority_mode=e.get("authority_mode", "profile_grant"),
            resolved_binding=bindings[(e["contract_id"], e["operation_id"])],
        )
        for e in profile["requested_edges"]
    ]
    settings_binding = bindings[(settings.CONTRACT_ID, settings.OPERATION_ID)]
    contexts = {function_id: NS(provider_bindings=()) for function_id in functions}
    contexts[policy.function.function_id].provider_bindings = (policy,)
    settings_context = NS(
        provider_bindings=(settings_binding,),
        profile_id=profile["profile_id"],
        user_data_root=tmp_path,
        domain_ids={
            (
                settings.CONTRACT_ID,
                settings.OPERATION_ID,
                settings_binding.principal_ref.value,
            ): "domain"
        },
    )
    contexts[settings_binding.function.function_id] = settings_context
    if failure == "missing":
        del contexts[settings_binding.function.function_id]
    elif failure == "duplicate":
        settings_context.provider_bindings = (settings_binding, settings_binding)
    elif failure == "foreign":
        settings_binding.function.function_id = "foreign.owner"

    requested: list[str] = []

    def host_context(function_id: str) -> Any:
        requested.append(function_id)
        if function_id not in contexts:
            raise AuthorityDenied("reviewer captured owner source unavailable")
        return contexts[function_id]

    original = settings.build_reviewer_configuration_revision

    def observe_validated(*args: Any, **kwargs: Any) -> Any:
        assert requested == [
            "rumi_host_authority_bridge_pack.host-authority.approval-policy",
            "rumi_default_tools_pack.file-create-tool",
            "rumi_host_authority_bridge_pack.host-authority.interactive-effect",
            "tobkiri.ui.settings.read",
        ]
        original(*args, **kwargs)
        raise _ReachedSettingsOwner

    monkeypatch.setattr(
        settings, "build_reviewer_configuration_revision", observe_validated
    )
    kwargs = {
        key: None for key in inspect.signature(compose_saved_tool_policy_v4).parameters
    }
    kwargs.update(
        catalog=NS(
            resolve_pinned=lambda contract, operation: bindings[(contract, operation)]
        ),
        edges=edges,
        host_context=host_context,
        profile_id=profile["profile_id"],
        assert_current_capture=lambda: None,
    )
    expected = (
        _ReachedSettingsOwner if failure is None else (AuthorityDenied, PermissionError)
    )
    with pytest.raises(expected):
        compose_saved_tool_policy_v4(**kwargs)
