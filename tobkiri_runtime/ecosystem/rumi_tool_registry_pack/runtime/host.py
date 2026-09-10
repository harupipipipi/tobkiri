"""Captured tool-definition owner, independent of the legacy tool executor."""

from __future__ import annotations

import re
from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
    HostProviderDataRequestV4,
)
from ecosystem.rumi_tool_registry_pack.runtime.pack_data import (
    DEFAULT_TOOLS_PACK,
    definitions_from_pack_data,
)
from ecosystem.rumi_tool_registry_pack.runtime.registry import (
    ToolDefinitionRegistry,
    _composed_catalog,
    _resolve_composed,
)

PACK_ID = "rumi_tool_registry_pack"
CONTRIBUTION = "tobkiri.resource.tool.definition.contribution.v1"
_BINDINGS = {
    f"{PACK_ID}.tool-registry.definition": (
        "tobkiri.resource.tool.definition.v1",
        f"{PACK_ID}.tool-definition-resource",
    ),
    f"{PACK_ID}.tool-registry.manage": (
        "tobkiri.action.tool.definition.manage.v1",
        f"{PACK_ID}.tool-definition-manage",
    ),
    f"{PACK_ID}.tool-registry.migrate": (
        "tobkiri.action.tool.definition.migrate.v1",
        f"{PACK_ID}.tool-definition-migrate",
    ),
}
_FIELDS = {
    "list": set(),
    "get": {"tool_id"},
    "resolve": {"tool_id"},
    "save": {"definition", "expected_revision"},
    "delete": {"tool_id", "expected_revision"},
    "alias": {"alias", "target_tool_id", "expected_revision"},
    "migrate": {"definitions", "aliases", "expected_source_hash"},
    "rollback": {"migration_id", "expected_revision"},
}
_ACTIONS = {
    "definition": frozenset({"list", "get", "resolve"}),
    "manage": frozenset({"save", "delete", "alias"}),
    "migrate": frozenset({"migrate", "rollback"}),
}


class ToolRegistryHostFactoryV4:
    """Bind each registry Function to the Host's Profile, root and principal."""

    def __init__(self, function_id: str) -> None:
        if function_id not in _BINDINGS:
            raise ValueError("tool registry Function is unavailable")
        self.function_id = function_id
        self.declared_pack_data = (
            (HostProviderDataRequestV4(DEFAULT_TOOLS_PACK, "tools/"),)
            if function_id.endswith(".definition") else ()
        )

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Capture the existing data owner without discovering ambient state."""
        if context.user_data_root is None or len(context.provider_bindings) != 1:
            raise PermissionError("tool registry capture is unavailable")
        binding = context.provider_bindings[0]
        contract, operation = _BINDINGS[self.function_id]
        key = (contract, operation, binding.principal_ref.value)
        if (
            binding.function.function_id != self.function_id
            or binding.operation.contract_id != contract
            or binding.operation.operation_id != operation
            or binding.operation.contract_version != "1.0.0"
            or key not in context.domain_ids
        ):
            raise PermissionError("tool registry binding is unavailable")
        allowed_actions = _ACTIONS[self.function_id.rsplit(".", 1)[1]]
        pack_definitions = definitions_from_pack_data(context.declared_pack_data)

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            invocation.assert_current()
            envelope = invocation.envelope
            actual = envelope.context
            if (
                operation_id != operation
                or envelope.operation_id != operation
                or envelope.contract_id != contract
                or envelope.contract_version != "1.0.0"
                or envelope.target_principal.value != key[2]
                or dict(envelope.payload) != dict(payload)
                or (
                    actual.profile_id,
                    actual.activation_id,
                    actual.plan_digest,
                    actual.security_epoch,
                )
                != (
                    context.profile_id,
                    context.activation["activation_id"],
                    context.plan_digest,
                    context.security_epoch,
                )
            ):
                raise PermissionError("tool registry invocation changed")
            action = payload.get("operation")
            if not isinstance(action, str) or action not in allowed_actions:
                raise ValueError("tool registry operation is invalid")
            if (
                set(payload) - {"operation", "profile_id"} != _FIELDS[action]
                or payload.get("profile_id", context.profile_id) != context.profile_id
            ):
                raise PermissionError("tool registry payload is invalid")
            if "expected_revision" in payload and (
                type(payload["expected_revision"]) is not int
                or payload["expected_revision"] < 0
            ):
                raise ValueError("tool registry revision is invalid")
            client = invocation.contract_client(
                allowed_contract_ids=(
                    frozenset({CONTRIBUTION})
                    if action in _ACTIONS["definition"]
                    else frozenset()
                ),
                consumer_pack_id=PACK_ID,
                include_credentials=False,
            )
            registry = ToolDefinitionRegistry(
                context.profile_id,
                user_data_root=context.user_data_root,
                guard=invocation.assert_current,
            )
            result = _invoke(registry, client, action, payload, pack_definitions)
            if action == "list" and context.declared_pack_data:
                result = {
                    **result,
                    "pack_data_sources": [
                        {"pack_id": item.pack_id, "artifact_digest": item.artifact_digest}
                        for item in context.declared_pack_data
                    ],
                }
            invocation.assert_current()
            return result

        return CapturedHostProviderV4(
            (
                HostProviderContributionV4(
                    contract_id=contract,
                    contract_version="1.0.0",
                    operation_id=operation,
                    principal_id=key[2],
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=context.domain_ids[key],
                    invoke=invoke,
                ),
            ),
            lambda: None,
        )


def _invoke(
    registry: ToolDefinitionRegistry,
    client: Any,
    action: str,
    payload: Mapping[str, Any],
    pack_definitions: tuple[Mapping[str, Any], ...] = (),
) -> Mapping[str, Any]:
    if action in _ACTIONS["definition"]:
        catalog = _composed_catalog(
            client, registry, contribution_contract=CONTRIBUTION,
            pack_definitions=pack_definitions,
        )
        # Migration backups belong to the data owner, not the public resource.
        if isinstance(catalog.get("migration"), dict):
            catalog["migration"] = {
                key: catalog["migration"][key]
                for key in ("migration_id", "source_hash")
                if key in catalog["migration"]
            }
        if action == "list":
            return catalog
        resolved = _resolve_composed(catalog, _text(payload["tool_id"]))
        return {"found": False} if resolved is None else {"found": True, **resolved}
    if action == "save":
        if not isinstance(payload["definition"], dict):
            raise ValueError("tool definition is invalid")
        return registry.save(payload["definition"], payload["expected_revision"])
    if action == "delete":
        return registry.delete(_text(payload["tool_id"]), payload["expected_revision"])
    if action == "alias":
        return registry.alias(
            _text(payload["alias"]),
            _text(payload["target_tool_id"]),
            payload["expected_revision"],
        )
    if action == "migrate":
        definitions, aliases = payload["definitions"], payload["aliases"]
        if (
            not isinstance(definitions, list)
            or not all(isinstance(item, dict) for item in definitions)
            or not isinstance(aliases, dict)
        ):
            raise ValueError("tool migration source is invalid")
        return registry.migrate(
            definitions, aliases, _text(payload["expected_source_hash"])
        )
    migration_id = _text(payload["migration_id"])
    if re.fullmatch(r"migration-[0-9a-f]{32}", migration_id) is None:
        raise ValueError("tool migration identity is invalid")
    return registry.rollback_migration(migration_id, payload["expected_revision"])


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("tool registry identifier is invalid")
    return value


HOST_PROVIDER_FACTORY = {
    function_id: ToolRegistryHostFactoryV4(function_id) for function_id in _BINDINGS
}
