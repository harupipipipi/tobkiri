"""Finite native-visible delegation boundary captured before mode selection."""

from __future__ import annotations

import json
from typing import Any, Mapping

from core_runtime.authority.v4 import AuthorityDenied, AuthorityScope, authority_digest
from tobkiri_host.ports import InteractiveApprovalRequestCommand
from tobkiri_host.route_bound_policy_selection import RouteBoundActionApprovalPolicyController


class BoundedPolicySelectionController(RouteBoundActionApprovalPolicyController):
    """Bind finite Host-verified operation routes and caller scopes to native choice.

    ``routes`` must be built by the Host from its verified catalog and signed
    authority binding, rather than from a browser preference. The exact grant
    kernel repeats the signed binding and Provider intersection at settlement.
    """

    def __init__(
        self, *args: Any, catalog: Any, routes: tuple[Mapping[str, Any], ...], **kwargs: Any
    ) -> None:
        self._delegation_catalog = catalog
        super().__init__(*args, catalog=catalog, **kwargs)
        if not routes or len(routes) > 16:
            raise AuthorityDenied("native delegation requires 1..16 finite routes")
        normalized = []
        for route in routes:
            binding = self.catalog_binding(route)
            scope = AuthorityScope.from_dict(route["ceiling"])
            if (
                scope.capability != "operation.invoke"
                or scope.opaque
                or scope.exact_request_digest is not None
                or scope.semantics_digest != binding.operation.revision_digest
                or scope.dimensions.get("contract") != (binding.operation.contract_id,)
                or scope.dimensions.get("operation") != (binding.operation.operation_id,)
                or not route.get("caller_principal_id")
                or route.get("operation_class") != binding.operation.effect_class.value
                or not isinstance(route.get("ancestor_chain"), list)
                or len(route["ancestor_chain"]) > 4
                or len(set(route["ancestor_chain"])) != len(route["ancestor_chain"])
                or any(
                    not isinstance(item, str) or not item or item == "*"
                    for item in route["ancestor_chain"]
                )
            ):
                raise AuthorityDenied("delegation boundary is not a finite signed route")
            if any("*" in values for values in scope.dimensions.values()):
                raise AuthorityDenied("delegation boundary cannot contain wildcards")
            normalized.append(
                {
                    **dict(route),
                    "target_principal_id": binding.principal_ref.value,
                    "artifact_digest": binding.artifact.digest,
                    "implementation_digest": binding.function.implementation_digest,
                    "ceiling": scope.to_dict(),
                }
            )
        self.delegation_routes = tuple(normalized)

    def catalog_binding(self, route: Mapping[str, Any]) -> Any:
        """Resolve exact route from the same Host-captured verified catalog."""
        return self._delegation_catalog.resolve_pinned(route["contract"], route["operation"])

    @classmethod
    def create(cls, *, catalog: Any, **kwargs: Any) -> "BoundedPolicySelectionController":
        """Retain the verified catalog before validating finite boundaries."""
        return cls(catalog=catalog, **kwargs)

    def _capture(
        self,
        command: InteractiveApprovalRequestCommand,
        *,
        mode: str,
        reviewer: Mapping[str, Any],
        workspace: str,
        conversation: str,
        turn: str,
    ) -> dict[str, Any]:
        capture = super()._capture(
            command,
            mode=mode,
            reviewer=reviewer,
            workspace=workspace,
            conversation=conversation,
            turn=turn,
        )
        capture["delegation_boundary"] = {
            "authorization_kind": "native_selected_policy_exact_v1",
            "routes": list(self.delegation_routes),
            "reviewer_boundary": dict(capture["reviewer"]),
            "workspace_root": capture["workspace"],
        }
        return capture

    def _native_boundary_metadata(self, capture: Mapping[str, Any]) -> dict[str, Any]:
        """Display the exact finite delegation scope before native confirmation."""
        boundary = capture["delegation_boundary"]
        encoded = json.dumps(boundary, sort_keys=True, separators=(",", ":"))
        chunks = [encoded[index : index + 512] for index in range(0, len(encoded), 512)]
        if len(chunks) > 12:
            raise AuthorityDenied("native delegation boundary exceeds UI display budget")
        reviewer = capture["reviewer"]
        route = reviewer.get("route_binding", {})
        metadata = {
            "delegation_boundary_digest": authority_digest(boundary),
            **{f"boundary_part_{index + 1:02}": chunk for index, chunk in enumerate(chunks)},
            "allowed_operations": ", ".join(
                f"{item['operation']} ({item['operation_class']})" for item in boundary["routes"]
            ),
            "summary": "Select "
            + capture["mode"]
            + " for finite operations: "
            + ", ".join(route["operation"] for route in boundary["routes"]),
        }
        if capture["mode"] == "agent":
            metadata.update(
                {
                    "reviewer_model": str(
                        route.get("model_id") or reviewer.get("model") or "unavailable"
                    ),
                    "reviewer_model_reference": str(
                        reviewer.get("model_reference") or "unavailable"
                    ),
                    "reviewer_provider": str(route.get("provider_instance_id") or "unavailable"),
                    "reviewer_catalog_revision": str(
                        route.get("catalog_revision") or "unavailable"
                    ),
                    "reviewer_pricing_revision": str(
                        route.get("pricing_revision") or "unavailable"
                    ),
                    "reviewer_pricing": json.dumps(route.get("pricing", {}), sort_keys=True),
                }
            )
        if any(len(value) > 512 for value in metadata.values()):
            raise AuthorityDenied("native human-readable boundary exceeds display budget")
        return metadata
