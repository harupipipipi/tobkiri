"""Real source/selection compilers in an explicitly offline structural context."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sys
from typing import Any, Callable

from jsonschema import Draft202012Validator

from core_runtime.profile_authoring import build_named_profile
from core_runtime.external_pack_catalog_v4 import resolve_admitted_pack_roots
from core_runtime.profile_content_projection import selected_projection_roots
from core_runtime.profile_workflow_catalog import (
    compile_selected_workflow,
    selected_workflow_definitions,
)
from core_runtime.resolved_profile_scope import (
    V4PackView,
    V4ResolvedProfileView,
    activate_resolved_profile,
    restore_resolved_profile,
)
from core_runtime.workflow_v4.engine import WorkflowEngineV4
from core_runtime.workflow_v4.store import WorkflowStoreV4
from ecosystem.defaultspack.domain.runtime_v4 import resolve_default_profile
from tobkiri_host.artifact_compiler import compile_pack_root, routes_for_plan
from tobkiri_host.contracts import OperationCatalog
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.canonical import strict_loads
from tobkiri_protocol.validation import validate_file


class _NoEffects:
    """No authority or invocation can be supplied by this observation."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"offline compile attempted an effect: {name}")


class _Catalog:
    """Project exact bindings verified by the real OperationCatalog."""

    def __init__(self, bindings: list[Any]) -> None:
        self.schemas = {
            canonical_digest(item.operation.input_schema): item.operation.input_schema
            for item in bindings
        }
        body = {
            "security_epoch": 1,
            "activation": {
                "activation_id": "offline-structural-only",
                "activation_digest": canonical_digest({"live": False}),
            },
            "operations": [
                {
                    "contract_id": item.operation.contract_id,
                    "contract_revision_digest": item.operation.revision_digest,
                    "operation_id": item.operation.operation_id,
                    "function_principal_id": item.principal_ref.value,
                    "provider_id": item.function.function_id,
                    "input_schema_digest": canonical_digest(item.operation.input_schema),
                    "effect_ceiling": [item.operation.effect_class.value],
                }
                for item in bindings
            ],
        }
        self.value = {**body, "catalog_digest": canonical_digest(body)}

    def snapshot(self) -> dict[str, Any]:
        """Return data bindings, never a Kernel activation or grant."""
        return self.value

    def validate(self, schema_digest: str, value: dict[str, Any]) -> list[str]:
        """Use the actual compiled Operation input schema."""
        return [
            error.message
            for error in Draft202012Validator(self.schemas[schema_digest]).iter_errors(value)
        ]


def observe_named_profiles(
    *,
    bundle_root: Path,
    catalog: Any,
    admitted_roots: dict[str, Path],
    output_root: Path,
    label: str = "named",
) -> list[dict[str, Any]]:
    """Compile new Profiles and bind selected resources; never execute a Pack."""
    cases: list[dict[str, Any]] = []

    def record(
        name: str, operation: Callable[[], Any], denial: tuple[str, str] | None = None
    ) -> Any:
        try:
            value = operation()
        except Exception as error:
            cases.append(
                {
                    "case": name,
                    "actual": "deny",
                    "pass": denial is not None
                    and type(error).__name__ == denial[0]
                    and denial[1] in str(error),
                    "error_type": type(error).__name__,
                    "reason": str(error),
                }
            )
            return None
        cases.append({"case": name, "actual": "allow", "pass": denial is None})
        return value

    pack_ids = tuple(sorted(admitted_roots))
    source = catalog.profiles["defaults"]
    projections = []
    for pack_id, root in sorted(admitted_roots.items()):
        manifest = validate_file(root / "pack.v4.json", "pack")
        if any(item["path"].startswith("content/") for item in manifest["artifacts"]):
            projections.append(
                {
                    "projection_id": pack_id + ".content",
                    "kind": "profile_content",
                    "artifact_root": "content",
                    "source_pack_id": pack_id,
                    "source_artifact_digest": None,
                    "content_digest": None,
                }
            )

    def intent(selected: tuple[str, ...], profile_id: str) -> dict[str, Any]:
        value = {
            "intent_api_version": "io.tobkiri.profile-intent.v1",
            "profile_id": profile_id,
            "state": "needs_resolution",
            "mode": "interactive",
            "catalog_revision": None,
            "base": deepcopy(source["base"]),
            "shell": deepcopy(source["shell"]),
            "packs": deepcopy(source["packs"]),
            "requested_edges": [],
            "content_projections": [
                deepcopy(item) for item in projections if item["source_pack_id"] in selected
            ],
            "authority_references": [],
            "profile_authority_snapshot_digest": None,
        }
        value["base"].update(
            artifact_digest=None,
            definition_revision=None,
            resolution="verified_exact_artifact_required",
        )
        value["shell"].update(
            artifact_digest=None,
            executable_artifact_digest=None,
            definition_revision=None,
        )
        for pack in value["packs"]:
            pack["artifact_digest"] = None
        for pack_id in selected:
            value["packs"].append({"pack_id": pack_id, "artifact_digest": None, "role": "provider"})
            manifest = catalog.packs[pack_id]
            for function in manifest["functions"]:
                for operation_id in function["operations"]:
                    contract = next(
                        item
                        for item in manifest["contracts"]
                        if operation_id in item["operations"]
                        and item["revision_digest"] == function["contract_revision_digest"]
                    )
                    value["requested_edges"].append(
                        {
                            "caller_function_id": source["shell"]["provider_id"],
                            "target_provider_id": function["id"],
                            "contract_id": contract["contract_id"],
                            "operation_id": operation_id,
                            "requested_scope_template": {},
                        }
                    )
            # Preview catalogs include every exact step target from selected
            # public source, including unchanged bundled resource owners.
            # These are Shell-to-step structural edges only: no execution
            # authority, Workflow caller edge or activation is inferred.
            for path in sorted((admitted_roots[pack_id] / "content/workflows").glob(
                "*.workflow.intent.v1.json"
            )):
                for step in strict_loads(path.read_bytes())["steps"]:
                    request = step["request"]
                    candidates = [
                        (identity, function)
                        for identity, manifest in catalog.packs.items()
                        for function in manifest["functions"]
                        if function["id"] == request["function_id"]
                        and function["contract_revision_digest"] == request["contract_revision_digest"]
                        and request["operation_id"] in function["operations"]
                        and any(
                            contract["contract_id"] == request["contract_id"]
                            and contract["revision_digest"] == request["contract_revision_digest"]
                            and request["operation_id"] in contract["operations"]
                            for contract in manifest["contracts"]
                        )
                    ]
                    if len(candidates) != 1:
                        raise ValueError("selected preview target has no unique exact source binding")
                    dependency, _function = candidates[0]
                    if dependency not in {item["pack_id"] for item in value["packs"]}:
                        value["packs"].append({
                            "pack_id": dependency, "artifact_digest": None, "role": "provider",
                        })
                    edge = {
                        "caller_function_id": source["shell"]["provider_id"],
                        "target_provider_id": request["function_id"],
                        "contract_id": request["contract_id"],
                        "operation_id": request["operation_id"],
                        "requested_scope_template": {},
                    }
                    if edge not in value["requested_edges"]:
                        value["requested_edges"].append(edge)
        return value

    roots = tuple(admitted_roots.values())
    for index, selected in enumerate(((), (pack_ids[0],), (pack_ids[1],), pack_ids)):
        prefix = label + "." + "+".join(selected or ("unselected",))
        author_intent = intent(selected, f"independent.composed.{index}")
        target = record(
            prefix + ".compile",
            lambda: build_named_profile(
                output_root / f"release-{index}",
                template_bundle=bundle_root,
                intent=author_intent,
                additional_pack_roots=roots,
            ),
        )
        if target is None:
            continue
        profile = validate_file(target / "authored.profile.v5.json", "profile")
        lock = validate_file(target / "authored.profile.lock.v5.json", "profile_artifact_lock")
        assert lock["activation_authority"] == "unbound"
        assert set(pack_ids) & {item["identity"] for item in lock["effective_set"]} == set(selected)
        candidate = replace(catalog, profiles={profile["profile_id"]: profile})
        refs = {
            "|".join(
                str(edge[field])
                for field in (
                    "caller_function_id",
                    "target_provider_id",
                    "contract_id",
                    "operation_id",
                )
            ): "authority-ref:" + canonical_digest(edge).removeprefix("sha256:")
            for edge in profile["requested_edges"]
        }
        resolved = record(
            prefix + ".resolve",
            lambda: resolve_default_profile(
                candidate,
                profile["profile_id"],
                approved_artifact_digests={
                    item["pack"]["artifact_digest"] for item in candidate.packs.values()
                },
                authority_snapshot_digest=canonical_digest({"offline": True}),
                authority_bindings=refs,
                security_epoch=1,
            ),
        )
        if resolved is None:
            continue
        plan = {
            "bindings": list(resolved.plan["bindings"])
        }
        runtime_root = Path(__file__).resolve().parents[3]
        binding_roots = resolve_admitted_pack_roots(
            tuple(sorted({item["pack_id"] for item in plan["bindings"]})),
            runtime_root / "ecosystem",
        )
        compiled = [compile_pack_root(root) for root in binding_roots.values()]
        routes = routes_for_plan(plan, compiled)
        operations = OperationCatalog([item.artifact for item in compiled], routes)
        bindings = [
            operations.resolve(route.contract_id, route.operation_id, None) for route in routes
        ]
        captured = _Catalog(bindings)
        store = WorkflowStoreV4(output_root / f"compile-{index}.sqlite3")
        engine = WorkflowEngineV4(
            store=store,
            catalog=captured,
            authority=_NoEffects(),
            invoker=_NoEffects(),
            validator=captured,
        )
        packs = tuple(
            V4PackView(item["identity"], "", item["artifact_digest"], item["artifact_digest"])
            for item in resolved.lock["effective_set"]
        )
        # Structural context solely for source observation: no ActivationRecord
        # is minted/stored and these data refs cannot authorize an invocation.
        token = activate_resolved_profile(
            V4ResolvedProfileView(
                profile["profile_id"],
                resolved.plan["profile_revision"],
                resolved.plan["plan_digest"],
                tuple(item.pack_id for item in packs),
                packs,
                (),
                tuple(resolved.profile["content_projections"]),
            )
        )
        try:
            record(
                prefix + ".content",
                lambda: selected_projection_roots(resolved.profile["content_projections"]),
            )
            definitions = record(prefix + ".workflow-sources", selected_workflow_definitions)
            for definition in definitions or ():
                record(
                    prefix + ".workflow-bind",
                    lambda definition=definition: compile_selected_workflow(
                        engine, definition["definition_id"]
                    ),
                )
            # The established Defaults entrypoint supplies this package root
            # for its legacy absolute domain imports; use that same import
            # layout, without changing a provider, authority or resource.
            defaultspack = Path(__file__).resolve().parents[3] / "ecosystem/defaultspack"
            if str(defaultspack) not in sys.path:
                sys.path.insert(0, str(defaultspack))
            from domain.capability.catalog import CapabilityCatalog

            def prompt_catalog() -> list[dict[str, Any]]:
                prompts = CapabilityCatalog().prompts()
                expected = {
                    item["projection_id"] for item in resolved.profile["content_projections"]
                }
                actual = {
                    item["source_projection_id"]
                    for item in prompts
                    if item.get("source_authority_kind") == "profile_projection"
                }
                assert actual == expected
                for item in prompts:
                    if item.get("source_projection_id") in expected:
                        assert CapabilityCatalog().prompt_text(
                            item["id"], item["source_projection_id"]
                        )
                return prompts

            record(prefix + ".prompt-catalog", prompt_catalog)
            assert not store.list_definitions()
        finally:
            restore_resolved_profile(token)
            store.close()
    if projections:
        value = intent((), "independent.removed.origin")
        value["content_projections"] = [deepcopy(projections[0])]
        record(
            label + ".removed-pack-kept-projection",
            lambda: build_named_profile(
                output_root / "removed-invalid",
                template_bundle=bundle_root,
                intent=value,
                additional_pack_roots=roots,
            ),
            ("ProfileContentProjectionError", "unselected"),
        )
    return cases
