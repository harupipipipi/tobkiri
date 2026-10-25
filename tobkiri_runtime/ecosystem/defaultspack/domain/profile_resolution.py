"""Original Defaultspack Profile candidate calculation, without native services.

Native activation, persistence and admission remain in their enforcing domains.
The legacy runtime_v4.service facade re-exports these same owned definitions.
Function globals now belong to this data layer; calculation bodies are unchanged.
"""

from __future__ import annotations
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping
from types import FunctionType as _FunctionType
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.errors import ProtocolError
from tobkiri_protocol.executable_catalog import materialization_catalog_digest
from tobkiri_protocol.profile_scope import normalize_requested_scope_template
from tobkiri_protocol.platform_artifact import verify_platform_artifact
from tobkiri_protocol.validation import validate_document
from tobkiri_protocol.bundle_catalog import BundledCatalog, DefaultProfileV4Error


_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_FOUNDATIONAL_CONTRACT = "conversation.turn.v1"

_AUTHORITY_MODES = frozenset({"profile_grant", "interactive_only"})

class ProfileResolutionDenied(DefaultProfileV4Error):
    """Raised when an exact, approved v4 composition cannot be produced."""

def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()

def _bundle_digest(catalog: "BundledCatalog") -> str:
    """Digest the exact lock bytes that admitted the finite bundle."""

    lock_path = catalog.root / "bundle.lock.json"
    if lock_path.is_symlink() or not lock_path.is_file():
        raise ProfileResolutionDenied("bundle lock is unavailable")
    return _sha256_bytes(lock_path.read_bytes())

def _require_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ProfileResolutionDenied(f"{field} must be an exact sha256 digest")
    return value

def _require_optional_pin(
    requested: object,
    actual: str,
    field: str,
) -> None:
    """Reject a stale exact source pin while permitting unresolved ``null``."""

    if requested is not None and requested != actual:
        raise ProfileResolutionDenied(f"{field} is stale or mismatched")

@dataclass(frozen=True)
class ResolvedDefaultProfile:
    """Schema-valid v4 records ready for Host activation ceremony."""

    profile: Mapping[str, Any]
    lock: Mapping[str, Any]
    plan: Mapping[str, Any]

def _application_launch_identity(
    manifest: Mapping[str, Any],
) -> tuple[str, str, str]:
    """Return the one neutral Application contribution or fail closed."""

    pack_id = str(manifest["pack"]["id"])
    operations = [
        item
        for item in manifest["operation_catalog"]
        if item["owner"] == pack_id and item["operation_id"] == "launch"
    ]
    if len(operations) != 1:
        raise ProfileResolutionDenied("Application launch contribution is ambiguous")
    operation = operations[0]
    providers = [
        item
        for item in manifest["provider_catalog"]
        if item["owner"] == pack_id
        and item["provider_id"] == operation["provider_id"]
        and item["contract_reference"] == operation["contract_reference"]
        and operation["operation_id"] in item["operations"]
    ]
    functions = [
        item
        for item in manifest["functions"]
        if item["id"] == operation["provider_id"]
        and operation["operation_id"] in item["operations"]
    ]
    if len(providers) != 1 or len(functions) != 1:
        raise ProfileResolutionDenied("Application launch contribution is ambiguous")
    provider = providers[0]
    function = functions[0]
    contracts = [
        item
        for item in manifest["contracts"]
        if item["contract_id"] == operation["contract_reference"]
        and item["revision_digest"] == function["contract_revision_digest"]
        and operation["operation_id"] in item["operations"]
    ]
    if len(contracts) != 1:
        raise ProfileResolutionDenied("Application launch contribution is stale")
    return (
        str(provider["provider_id"]),
        str(operation["contract_reference"]),
        str(operation["operation_id"]),
    )

def _edge_key(edge: Mapping[str, Any]) -> str:
    return "|".join(
        str(edge.get(field) or "")
        for field in (
            "caller_function_id",
            "target_provider_id",
            "contract_id",
            "operation_id",
        )
    )

def _authority_mode(edge: Mapping[str, Any]) -> str:
    """Return the closed activation authority policy for one requested edge."""

    value = edge.get("authority_mode", "profile_grant")
    if value not in _AUTHORITY_MODES:
        raise ProfileResolutionDenied("requested edge authority mode is invalid")
    return str(value)

def _provider_candidates(
    packs: list[Mapping[str, Any]], contract_id: str, operation_id: str
) -> list[tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]]:
    candidates: list[tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]] = []
    for manifest in packs:
        contracts = [
            contract
            for contract in manifest["contracts"]
            if contract["contract_id"] == contract_id and operation_id in contract["operations"]
        ]
        if len(contracts) > 1:
            raise ProfileResolutionDenied(
                f"duplicate Contract declaration in {manifest['pack']['id']}: {contract_id}"
            )
        if not contracts:
            continue
        contract = contracts[0]
        for function in manifest["functions"]:
            if (
                operation_id in function["operations"]
                and function["contract_revision_digest"] == contract["revision_digest"]
            ):
                candidates.append((manifest, function, contract))
    return candidates

def dynamic_profile_edges(
    catalog: BundledCatalog,
    profile_id: str,
    additional_pack_ids: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    """Derive exact operation edges for newly enabled Pack dependencies.

    The bundled Profile owns its static edges.  An approved optional Pack may
    contribute its own operations to the selected Shell, may call exact
    operations a direct signed Pack dependency provides for its required
    Contracts, and may serve its provided operations to a direct signed
    dependency that declared the matching Contract dependency.  Transitive
    dependencies remain selected and verified, but they do not gain new caller
    edges merely by appearing in the dependency closure.  Every edge is
    persisted into the immutable resolved Profile before dispatch.
    """

    source = catalog.profiles.get(profile_id)
    if source is None or not additional_pack_ids:
        return ()
    source_keys = {
        (
            str(edge["caller_function_id"]),
            str(edge["target_provider_id"]),
            str(edge["contract_id"]),
            str(edge["operation_id"]),
        )
        for edge in source["requested_edges"]
    }
    shell_request = source.get("shell")
    if not isinstance(shell_request, Mapping):
        raise ProfileResolutionDenied("dynamic Pack edges require the selected Shell")
    shell_definition = catalog.shells.get(str(shell_request["provider_id"]))
    if shell_definition is None:
        raise ProfileResolutionDenied("dynamic Pack Shell Provider is unavailable")
    shell_manifest = catalog.packs.get(str(shell_definition["pack_id"]))
    if shell_manifest is None or not shell_manifest["functions"]:
        raise ProfileResolutionDenied("dynamic Pack Shell Function is unavailable")
    # The signed Profile's selected Shell provider is the only caller role
    # allowed to project optional Pack operations.  Never infer authority from
    # lexical Function ordering: a newly added Shell Function must not silently
    # inherit every dynamic Pack capability.
    selected_caller_id = str(shell_request.get("provider_id") or "")
    caller_functions = [
        function
        for function in shell_manifest["functions"]
        if str(function.get("id") or "") == selected_caller_id
        and str(function.get("role") or "") == "brokered"
    ]
    if len(caller_functions) != 1:
        raise ProfileResolutionDenied("dynamic Pack Shell caller role is absent or ambiguous")
    caller_function_id = selected_caller_id
    def _operation_contract(
        manifest: Mapping[str, Any], operation_id: str
    ) -> str | None:
        return next(
            (
                str(contract["contract_id"])
                for contract in manifest["contracts"]
                if operation_id in contract["operations"]
            ),
            None,
        )

    pending: list[tuple[str, int]] = [
        (str(pack_id), 0) for pack_id in dict.fromkeys(additional_pack_ids)
    ]
    depth_for_pack: dict[str, int] = {}
    # Keep exact required operations attached to each caller. A Contract is a
    # compatibility boundary, not an authority grant: expanding one signed
    # dependency to every operation in that Contract would let a caller reach
    # sibling operations it never named.
    provider_caller_operations: dict[
        str, dict[str, set[tuple[str, str]]]
    ] = {}
    consumer_links: list[tuple[str, str, tuple[tuple[str, str], ...]]] = []

    def _required_operations(
        consumer: Mapping[str, Any],
        provider: Mapping[str, Any],
        contract_ids: tuple[str, ...],
    ) -> tuple[tuple[str, str], ...]:
        """Return only operations explicitly bound by a signed dependency."""

        provider_operations = {
            str(contract["contract_id"]): tuple(
                str(operation_id) for operation_id in contract["operations"]
            )
            for contract in provider["contracts"]
        }
        requirements = {
            str(item["contract_id"]): item
            for item in consumer["requirements"]["contract_dependencies"]
        }
        selected: list[tuple[str, str]] = []
        for contract_id in contract_ids:
            provided = provider_operations[contract_id]
            requirement = requirements.get(contract_id)
            if requirement is None:
                raise ProfileResolutionDenied(
                    "dynamic Pack dependency is missing its signed Contract "
                    f"requirement: {consumer['pack']['id']} -> "
                    f"{provider['pack']['id']} ({contract_id})"
                )
            # ``optional`` permits the provider to be absent. Once this exact
            # declared dependency supplies the Contract, it remains a signed
            # consumer relationship and must retain the same narrow operation
            # selection rules as a required dependency.
            requested = requirement.get("operations")
            if requested is None:
                if len(provided) != 1:
                    raise ProfileResolutionDenied(
                        "dynamic Pack dependency must name exact operations for "
                        f"multi-operation Contract: {consumer['pack']['id']} -> "
                        f"{provider['pack']['id']} ({contract_id})"
                    )
                requested_operations = provided
            elif (
                not isinstance(requested, list)
                or not requested
                or any(not isinstance(item, str) or not item for item in requested)
                or len(requested) != len(set(requested))
            ):
                raise ProfileResolutionDenied(
                    "dynamic Pack dependency operations are invalid: "
                    f"{consumer['pack']['id']} -> {provider['pack']['id']} "
                    f"({contract_id})"
                )
            else:
                requested_operations = tuple(requested)
            unknown = set(requested_operations) - set(provided)
            if unknown:
                raise ProfileResolutionDenied(
                    "dynamic Pack dependency names an operation outside its "
                    f"required Contract: {consumer['pack']['id']} -> "
                    f"{provider['pack']['id']} ({contract_id})"
                )
            selected.extend(
                (contract_id, operation_id)
                for operation_id in requested_operations
            )
        return tuple(sorted(selected))
    closure: set[str] = set()
    while pending:
        pack_id, depth = pending.pop(0)
        prior_depth = depth_for_pack.setdefault(pack_id, depth)
        if prior_depth != depth:
            # An explicitly selected root can also be another root's signed
            # dependency.  The queue visits every root before its dependencies,
            # so the shallower visit already processed that Pack.  Its exact
            # caller/Contract pairs were recorded by the parent separately.
            if prior_depth < depth:
                continue
            raise ProfileResolutionDenied(
                f"dynamic Pack dependency caller is ambiguous: {pack_id}"
            )
        if pack_id in closure:
            continue
        manifest = catalog.packs.get(pack_id)
        if manifest is None:
            raise ProfileResolutionDenied(f"dynamic Pack is not in the exact inventory: {pack_id}")
        closure.add(pack_id)
        if depth >= 1:
            # Only a selected optional Pack and its direct signed dependency
            # receive dynamic caller edges.  The resolver validates the full
            # transitive implementation closure separately; inferring callers
            # beyond this point would both broaden authority and walk valid
            # Host-provider dependency cycles.
            continue
        pack_caller_ids = tuple(
            str(function["id"]) for function in manifest["functions"]
        )
        required_contracts = {
            str(item["contract_id"])
            for item in manifest["requirements"]["contract_dependencies"]
            if not item["optional"]
        }
        provided_contracts = {
            str(item["contract_id"]) for item in manifest["contracts"]
        }
        dependencies = manifest["requirements"]["pack_dependencies"]
        for dependency in tuple(str(item) for item in dependencies):
            dependency_manifest = catalog.packs.get(dependency)
            if dependency_manifest is None:
                raise ProfileResolutionDenied(
                    f"dynamic Pack is not in the exact inventory: {dependency}"
                )
            dependency_provided = {
                str(item["contract_id"]) for item in dependency_manifest["contracts"]
            }
            dependency_required = {
                str(item["contract_id"])
                for item in dependency_manifest["requirements"]["contract_dependencies"]
            }
            served_contracts = tuple(sorted(required_contracts & dependency_provided))
            consumed_contracts = tuple(sorted(dependency_required & provided_contracts))
            if not served_contracts and not consumed_contracts:
                raise ProfileResolutionDenied(
                    "dynamic Pack dependency does not provide a signed required "
                    f"Contract: {pack_id} -> {dependency}"
                )
            if served_contracts:
                # A presentation-only application has no Function principal.
                # Its signed frontend contribution runs in the selected Shell,
                # so that exact Shell Function is the caller for only the
                # required Contracts this application declared. Other Pack
                # kinds cannot acquire Shell authority by omitting Functions.
                if pack_caller_ids:
                    dependency_callers = pack_caller_ids
                elif manifest["pack"].get("kind") == "application":
                    dependency_callers = (caller_function_id,)
                else:
                    raise ProfileResolutionDenied(
                        f"dynamic Pack dependency caller is ambiguous: {pack_id}"
                    )
                served_operations = _required_operations(
                    manifest, dependency_manifest, served_contracts
                )
                caller_operations = provider_caller_operations.setdefault(dependency, {})
                for caller_id in dependency_callers:
                    caller_operations.setdefault(caller_id, set()).update(
                        served_operations
                    )
            if consumed_contracts:
                consumer_links.append(
                    (
                        pack_id,
                        dependency,
                        _required_operations(
                            dependency_manifest, manifest, consumed_contracts
                        ),
                    )
                )
            pending.append((dependency, depth + 1))

    def _edge(
        caller_id: str, target_id: str, contract_id: str, operation_id: str
    ) -> dict[str, Any]:
        # Dynamic Pack edges are never user-selected interactive authority.
        # Their absent mode deliberately means the closed ``profile_grant``
        # default, preserving older resolved Profile bytes while preventing a
        # dynamic source from opting in.
        return {
            "caller_function_id": caller_id,
            "target_provider_id": target_id,
            "contract_id": contract_id,
            "operation_id": operation_id,
            "requested_scope_template": {
                "capability": "operation.invoke",
                "dimensions": {
                    "contract": [contract_id],
                    "operation": [operation_id],
                },
            },
        }

    # Dispatch only authorizes callers that hold a Function principal: the
    # selected Shell's Functions and every Function named as an edge target.
    # A Function that is never a target cannot be reached to invoke anything,
    # so it cannot carry a caller edge either.
    reachable_callers = {
        str(edge["target_provider_id"]) for edge in source["requested_edges"]
    }
    reachable_callers.update(
        str(function["id"]) for function in shell_manifest["functions"]
    )
    result: list[dict[str, Any]] = []
    minted: set[tuple[str, str, str, str]] = set()
    consumer_owned_operations = {
        (provider_id, contract_id, operation_id)
        for provider_id, _consumer_id, shared_operations in consumer_links
        for contract_id, operation_id in shared_operations
    }
    for pack_id in sorted(closure):
        # Only the optional Pack and its direct signed dependencies contribute
        # dynamic Authority edges.  Deeper dependencies are implementation
        # closure and cannot inherit the ancestor caller.
        if depth_for_pack[pack_id] > 1:
            continue
        manifest = catalog.packs[pack_id]
        caller_operations = provider_caller_operations.get(pack_id, {})
        for function in sorted(manifest["functions"], key=lambda item: str(item["id"])):
            for operation_id in sorted(str(item) for item in function["operations"]):
                contract_id = _operation_contract(manifest, operation_id)
                if contract_id is None:
                    continue
                callers: set[str] = set()
                if depth_for_pack[pack_id] == 0 and (
                    pack_id,
                    contract_id,
                    operation_id,
                ) not in consumer_owned_operations:
                    callers.add(caller_function_id)
                callers.update(
                    caller_id
                    for caller_id, required in caller_operations.items()
                    if (contract_id, operation_id) in required
                )
                for caller_id in sorted(callers):
                    key = (caller_id, str(function["id"]), contract_id, operation_id)
                    if key in source_keys or key in minted:
                        continue
                    minted.add(key)
                    result.append(
                        _edge(caller_id, str(function["id"]), contract_id, operation_id)
                    )
    # A declared Pack dependency may also be a signed consumer of the enabling
    # Pack's own Contracts: it receives caller edges into the exact operations
    # it depends on, while still never inheriting the Shell caller.
    for provider_id, consumer_id, shared_operations in sorted(set(consumer_links)):
        provider_manifest = catalog.packs[provider_id]
        consumer_manifest = catalog.packs[consumer_id]
        for consumer_function in consumer_manifest["functions"]:
            for provider_function in provider_manifest["functions"]:
                for operation_id in sorted(
                    str(item) for item in provider_function["operations"]
                ):
                    contract_id = _operation_contract(provider_manifest, operation_id)
                    if (
                        contract_id is None
                        or (contract_id, operation_id) not in shared_operations
                    ):
                        continue
                    key = (
                        str(consumer_function["id"]),
                        str(provider_function["id"]),
                        contract_id,
                        operation_id,
                    )
                    if key in source_keys or key in minted:
                        continue
                    minted.add(key)
                    result.append(_edge(*key))
    # A caller edge is realizable only while its Function stays reachable as
    # a target (or belongs to the Shell).  Drop unrealizable caller edges to a
    # fixpoint so a newly minted target edge can carry its own callers, while
    # an unreachable Function never gains authority it cannot exercise.
    while True:
        callers = reachable_callers | {
            str(edge["target_provider_id"]) for edge in result
        }
        filtered = [
            edge
            for edge in result
            if str(edge["caller_function_id"]) in callers
        ]
        if len(filtered) == len(result):
            return tuple(result)
        result = filtered

def _exact_executable_variant(
    catalog: BundledCatalog,
    manifest: Mapping[str, Any],
    function: Mapping[str, Any],
    contract_id: str,
    operation_id: str,
) -> Mapping[str, Any]:
    """Resolve one Function through its bundled executable catalog only."""
    pack_id = str(manifest["pack"]["id"])
    executable = catalog.executable_catalogs.get(pack_id)
    if executable is None:
        raise ProfileResolutionDenied(f"executable catalog is not bundled for Pack: {pack_id}")
    if (
        executable["pack_id"] != pack_id
        or executable["source_identity"] != manifest["integrity"]["source_identity"]
    ):
        raise ProfileResolutionDenied("executable catalog identity is stale")
    expected_catalog_digest = canonical_digest(
        {key: value for key, value in executable.items() if key != "catalog_digest"}
    )
    if executable["catalog_digest"] != expected_catalog_digest:
        raise ProfileResolutionDenied("executable catalog digest is stale")
    variants = [item for item in executable["variants"] if item["function_id"] == function["id"]]
    if len(variants) != 1:
        raise ProfileResolutionDenied(
            f"executable variant is not unique: {pack_id}/{function['id']}"
        )
    variant = variants[0]
    if variant["implementation_digest"] != function["implementation_digest"]:
        raise ProfileResolutionDenied("executable variant implementation is stale")
    operations = [
        item
        for item in variant["operations"]
        if item["contract_id"] == contract_id
        and item["operation_id"] == operation_id
        and item["revision_digest"] == function["contract_revision_digest"]
    ]
    if len(operations) != 1:
        raise ProfileResolutionDenied("executable variant Operation is not exact")
    expected_execution_kind = (
        "host_extension"
        if manifest["pack"]["kind"] == "host_extension"
        else (
            "wasm"
            if function.get("isolation") == "wasm_component"
            else "remote"
            if function.get("isolation") == "remote"
            else "pack_vm"
        )
    )
    if variant["execution_kind"] != expected_execution_kind:
        raise ProfileResolutionDenied("executable variant execution kind is stale")
    return variant

def resolve_default_profile(
    catalog: BundledCatalog,
    profile_id: str,
    *,
    approved_artifact_digests: set[str] | frozenset[str],
    authority_snapshot_digest: str,
    authority_bindings: Mapping[str, str],
    security_epoch: int,
    additional_pack_ids: tuple[str, ...] = (),
) -> ResolvedDefaultProfile:
    """Resolve one bundled default Profile without live discovery or fallback.

    ``approved_artifact_digests`` and ``authority_bindings`` are captured Host
    inputs. This function never interprets client approval flags and never mints
    authority.
    """
    snapshot_digest = _require_digest(authority_snapshot_digest, "authority_snapshot_digest")
    if (
        not isinstance(security_epoch, int)
        or isinstance(security_epoch, bool)
        or security_epoch < 0
    ):
        raise ProfileResolutionDenied("security_epoch must be a non-negative integer")
    source = catalog.profiles.get(profile_id)
    if source is None:
        raise ProfileResolutionDenied(f"profile is not in the bundled inventory: {profile_id}")
    if source["state"] != "needs_resolution":
        raise ProfileResolutionDenied("bundled Profile must begin in needs_resolution state")
    if (
        source["profile_authority_snapshot_digest"] is not None
        or source["authority_references"]
        or any(edge.get("authority_reference") is not None for edge in source["requested_edges"])
    ):
        raise ProfileResolutionDenied(
            "bundled Profile source must not contain resolved Authority state"
        )
    profile_definition_digest = canonical_digest(source)
    bundle_digest = _bundle_digest(catalog)

    base_id = source["base"]["pack_id"]
    base_manifest = catalog.packs.get(base_id)
    base_definition = catalog.bases.get(base_id)
    if base_manifest is None or base_definition is None:
        raise ProfileResolutionDenied(f"Base is incomplete: {base_id}")
    if base_manifest["pack"]["kind"] != "base":
        raise ProfileResolutionDenied(f"selected Base manifest is not kind=base: {base_id}")
    if base_definition["artifact_digest"] != base_manifest["pack"]["artifact_digest"]:
        raise ProfileResolutionDenied("Base definition does not pin its exact artifact")
    _require_optional_pin(
        source["base"]["artifact_digest"],
        str(base_manifest["pack"]["artifact_digest"]),
        "Profile Base artifact pin",
    )
    _require_optional_pin(
        source["base"]["definition_revision"],
        str(base_definition["definition_revision"]),
        "Profile Base definition pin",
    )
    if source["base"].get("resolution") not in {
        None,
        "verified_exact_artifact_required",
    }:
        raise ProfileResolutionDenied("bundled Profile Base resolution state is invalid")

    base_dependency_ids: list[str] = []
    for dependency in base_definition["dependencies"]:
        dependency_id = str(dependency["pack_id"])
        if dependency_id in base_dependency_ids:
            raise ProfileResolutionDenied(
                f"Base definition contains a duplicate dependency: {dependency_id}"
            )
        dependency_manifest = catalog.packs.get(dependency_id)
        if dependency_manifest is None:
            raise ProfileResolutionDenied(f"Base dependency is unavailable: {dependency_id}")
        if dependency["artifact_digest"] != dependency_manifest["pack"]["artifact_digest"]:
            raise ProfileResolutionDenied(
                f"Base dependency artifact is stale or mismatched: {dependency_id}"
            )
        base_dependency_ids.append(dependency_id)

    shell_request = source.get("shell")
    if not isinstance(shell_request, dict):
        raise ProfileResolutionDenied("default interactive Profile requires one Shell")
    provider_id = shell_request["provider_id"]
    shell_definition = catalog.shells.get(provider_id)
    if shell_definition is None:
        raise ProfileResolutionDenied(f"Shell Provider is not inventoried: {provider_id}")
    if shell_definition.get("availability") != "verified":
        raise ProfileResolutionDenied(
            f"Shell artifact is unavailable for this source/build: {provider_id}"
        )
    if shell_request["pack_id"] != shell_definition["pack_id"]:
        raise ProfileResolutionDenied("Profile Shell Pack binding is stale or mismatched")
    if shell_request["contract_id"] != shell_definition["contract_id"]:
        raise ProfileResolutionDenied("Profile Shell Contract binding is stale or mismatched")
    variants = [
        variant
        for variant in shell_definition["launch"]["variants"]
        if variant["platform"] == shell_request["platform"]
        and variant["architecture"] == shell_request["architecture"]
    ]
    if len(variants) != 1 or catalog.artifact_root is None:
        raise ProfileResolutionDenied("verified Shell platform artifact is unavailable")
    selected_variant = variants[0]
    try:
        verify_platform_artifact(catalog.artifact_root, selected_variant)
    except ProtocolError as exc:
        raise ProfileResolutionDenied(f"verified Shell artifact rejected: {exc}") from exc
    shell_pack_id = shell_definition["pack_id"]
    shell_manifest = catalog.packs.get(shell_pack_id)
    if shell_manifest is None or shell_manifest["pack"]["kind"] != "shell":
        raise ProfileResolutionDenied(f"Shell Pack is missing or invalid: {shell_pack_id}")
    shell_contracts = [
        contract
        for contract in shell_manifest["contracts"]
        if contract["contract_id"] == shell_definition["contract_id"]
    ]
    shell_functions = [
        function
        for function in shell_manifest["functions"]
        if function["id"] == provider_id
        and any(
            function["contract_revision_digest"] == contract["revision_digest"]
            and set(function["operations"]).issubset(set(contract["operations"]))
            for contract in shell_contracts
        )
    ]
    if len(shell_contracts) != 1 or len(shell_functions) != 1:
        raise ProfileResolutionDenied("Shell Provider does not implement the exact Shell Contract")
    if shell_definition["artifact_digest"] != selected_variant["artifact_digest"]:
        raise ProfileResolutionDenied("Shell definition does not pin its exact artifact")
    _require_optional_pin(
        shell_request["artifact_digest"],
        str(selected_variant["artifact_digest"]),
        "Profile Shell artifact pin",
    )
    _require_optional_pin(
        shell_request.get("executable_artifact_digest"),
        str(selected_variant["entrypoint_digest"]),
        "Profile Shell executable pin",
    )
    _require_optional_pin(
        shell_request["definition_revision"],
        str(shell_definition["definition_revision"]),
        "Profile Shell definition pin",
    )
    shell_requirements = base_definition["shell_requirements"]
    if source["mode"] != shell_requirements["mode"]:
        raise ProfileResolutionDenied("Profile mode does not match the Base definition")
    if (
        shell_definition["presentation"]["family"]
        not in shell_requirements["presentation_families"]
    ):
        raise ProfileResolutionDenied("Shell presentation family is incompatible with Base")
    if not set(shell_requirements["required_capabilities"]).issubset(
        set(shell_definition["presentation"]["capabilities"])
    ):
        raise ProfileResolutionDenied("Shell does not satisfy Base capabilities")

    requested_pack_ids = [item["pack_id"] for item in source["packs"]]
    if len(requested_pack_ids) != len(set(requested_pack_ids)):
        raise ProfileResolutionDenied("Profile composition contains a duplicate Pack")
    requested_pack_roles = {
        item["pack_id"]: item.get("role", "provider") for item in source["packs"]
    }
    application_ids = sorted(
        pack_id for pack_id, role in requested_pack_roles.items() if role == "application"
    )
    if len(application_ids) != 1:
        raise ProfileResolutionDenied(
            "canonical defaults Profile requires exactly one Application Pack"
        )
    application_manifest = catalog.packs.get(application_ids[0])
    if application_manifest is None or application_manifest["pack"]["kind"] != "application":
        raise ProfileResolutionDenied(
            "canonical defaults Application must be a Pack v4 application"
        )
    for request in source["packs"]:
        manifest = catalog.packs.get(str(request["pack_id"]))
        if manifest is None:
            raise ProfileResolutionDenied(
                f"Pack is not in the exact inventory: {request['pack_id']}"
            )
        _require_optional_pin(
            request["artifact_digest"],
            str(manifest["pack"]["artifact_digest"]),
            f"Profile Pack artifact pin for {request['pack_id']}",
        )
    application_artifacts = application_manifest["artifacts"]
    expected_application = (
        selected_variant["relative_path"],
        selected_variant["artifact_digest"],
        selected_variant["entrypoint_digest"],
        f"{selected_variant['platform']}-{selected_variant['architecture']}",
        selected_variant["entrypoint"],
    )
    actual_applications = [
        (
            item.get("path"),
            item.get("digest"),
            item.get("entrypoint_digest"),
            item.get("platform"),
            item.get("entrypoint"),
        )
        for item in application_artifacts
        if item.get("kind") == "executable"
    ]
    if actual_applications != [expected_application] or any(
        function["implementation_digest"] != selected_variant["entrypoint_digest"]
        for function in application_manifest["functions"]
    ):
        raise ProfileResolutionDenied(
            "Application Pack does not pin the selected packaged artifact"
        )
    if len(additional_pack_ids) != len(set(additional_pack_ids)):
        raise ProfileResolutionDenied("additional Pack selection contains a duplicate")
    canonical_additional_ids = tuple(sorted(additional_pack_ids))
    selected_ids = [base_id, shell_pack_id, *requested_pack_ids]
    selected_ids.extend(
        dependency_id
        for dependency_id in sorted(base_dependency_ids)
        if dependency_id not in selected_ids
    )
    selected_ids.extend(
        pack_id for pack_id in canonical_additional_ids if pack_id not in selected_ids
    )
    pending = list(selected_ids)
    while pending:
        current_id = pending.pop(0)
        current = catalog.packs.get(current_id)
        if current is None:
            raise ProfileResolutionDenied(f"Pack is not in the exact inventory: {current_id}")
        for dependency_id, version_range in sorted(
            current["requirements"]["pack_dependencies"].items()
        ):
            dependency = catalog.packs.get(dependency_id)
            if dependency is None:
                raise ProfileResolutionDenied(
                    f"required Pack dependency is unavailable: {dependency_id}"
                )
            try:
                compatible = Version(dependency["pack"]["version"]) in SpecifierSet(
                    version_range.replace(" ", ",")
                )
            except (InvalidSpecifier, InvalidVersion) as exc:
                raise ProfileResolutionDenied(
                    f"invalid Pack dependency constraint: {dependency_id}"
                ) from exc
            if not compatible:
                raise ProfileResolutionDenied(
                    f"Pack dependency version is incompatible: {dependency_id}"
                )
            if dependency_id not in selected_ids:
                selected_ids.append(dependency_id)
                pending.append(dependency_id)
    if len(selected_ids) != len(set(selected_ids)):
        raise ProfileResolutionDenied("Profile composition contains a duplicate Pack")
    selected: list[Mapping[str, Any]] = []
    for pack_id in selected_ids:
        manifest = catalog.packs.get(pack_id)
        if manifest is None:
            raise ProfileResolutionDenied(f"Pack is not in the exact inventory: {pack_id}")
        artifact_digest = manifest["pack"]["artifact_digest"]
        if artifact_digest not in approved_artifact_digests:
            raise ProfileResolutionDenied(f"Pack artifact is not approved: {pack_id}")
        selected.append(manifest)

    provided_contracts = {
        contract["contract_id"] for manifest in selected for contract in manifest["contracts"]
    }
    for manifest in selected:
        for dependency in manifest["requirements"]["contract_dependencies"]:
            if not dependency["optional"] and dependency["contract_id"] not in provided_contracts:
                raise ProfileResolutionDenied(
                    f"required Contract dependency is unavailable: {dependency['contract_id']}"
                )

    foundational = _provider_candidates(selected, _FOUNDATIONAL_CONTRACT, "complete")
    if len(foundational) != 1:
        raise ProfileResolutionDenied(
            "foundational conversation Provider must resolve exactly once; "
            f"found {len(foundational)}"
        )

    available_function_ids = {
        function["id"] for manifest in selected for function in manifest["functions"]
    }
    available_function_ids.update(function["id"] for function in shell_manifest["functions"])
    dynamic_edges = dynamic_profile_edges(catalog, profile_id, canonical_additional_ids)
    all_source_edges = (*source["requested_edges"], *dynamic_edges)
    edge_keys = [_edge_key(edge) for edge in all_source_edges]
    if len(edge_keys) != len(set(edge_keys)):
        raise ProfileResolutionDenied("Profile contains a duplicate requested edge")
    for edge in all_source_edges:
        caller_function_id = edge["caller_function_id"]
        if caller_function_id not in available_function_ids:
            raise ProfileResolutionDenied(
                "requested edge caller is not in the selected Profile closure: "
                f"{caller_function_id}"
            )

    bindings: list[dict[str, Any]] = []
    variant_pins: dict[tuple[str, str], dict[str, Any]] = {}
    resolved_edges: list[dict[str, Any]] = []
    references: list[str] = []
    for source_edge in all_source_edges:
        edge = dict(source_edge)
        authority_mode = _authority_mode(edge)
        candidates = _provider_candidates(selected, edge["contract_id"], edge["operation_id"])
        candidates = [item for item in candidates if item[1]["id"] == edge["target_provider_id"]]
        if len(candidates) != 1:
            raise ProfileResolutionDenied(
                f"requested edge {_edge_key(edge)} must resolve exactly once; "
                f"found {len(candidates)}"
            )
        reference = authority_bindings.get(_edge_key(edge))
        if not isinstance(reference, str) or not reference.startswith("authority-ref:"):
            raise ProfileResolutionDenied(
                f"Authority Kernel reference is missing for edge {_edge_key(edge)}"
            )
        edge["authority_reference"] = reference
        if reference not in references:
            references.append(reference)
        manifest, function, contract = candidates[0]
        # ``candidates`` was filtered by ``target_provider_id`` above, so the
        # captured Function principal is the exact target of this Profile edge.
        try:
            edge["requested_scope_template"] = normalize_requested_scope_template(
                edge["requested_scope_template"],
                contract_id=str(edge["contract_id"]),
                operation_id=str(edge["operation_id"]),
                semantics_digest=str(contract["revision_digest"]),
            )
        except ProtocolError as exc:
            raise ProfileResolutionDenied(
                f"requested scope is invalid for edge {_edge_key(edge)}: {exc}"
            ) from exc
        resolved_edges.append(edge)
        principal = {
            "parent_artifact_digest": manifest["pack"]["artifact_digest"],
            "function_implementation_digest": function["implementation_digest"],
            "function_id": function["id"],
            "contract_revision_digest": contract["revision_digest"],
            "operation_id": edge["operation_id"],
        }
        variant = _exact_executable_variant(
            catalog,
            manifest,
            function,
            str(edge["contract_id"]),
            str(edge["operation_id"]),
        )
        domain_kind = function.get("isolation", "pack_vm")
        try:
            executable_catalog_digest = materialization_catalog_digest(
                manifest,
                catalog.executable_catalogs[manifest["pack"]["id"]],
            )
        except ValueError as exc:
            raise ProfileResolutionDenied(str(exc)) from exc
        pin = {
            "pack_id": manifest["pack"]["id"],
            "artifact_digest": manifest["pack"]["artifact_digest"],
            "executable_catalog_digest": executable_catalog_digest,
            "variant_id": variant["variant_id"],
            "platform": variant["platform"],
            "architecture": variant["architecture"],
            "runtime_abi": variant["runtime_abi"],
            "backend": variant["backend"],
            "execution_kind": variant["execution_kind"],
            "domain_kind": domain_kind,
        }
        pin_key = (str(pin["pack_id"]), str(pin["variant_id"]))
        previous_pin = variant_pins.setdefault(pin_key, pin)
        if previous_pin != pin:
            raise ProfileResolutionDenied("one executable variant has conflicting pins")
        binding = {
            "caller_function_id": edge["caller_function_id"],
            "pack_id": manifest["pack"]["id"],
            "artifact_digest": manifest["pack"]["artifact_digest"],
            "function_principal": principal,
            "contract_id": edge["contract_id"],
            "operation_id": edge["operation_id"],
            "domain_kind": domain_kind,
            "executable_catalog_digest": pin["executable_catalog_digest"],
            "variant_id": pin["variant_id"],
            "platform": pin["platform"],
            "architecture": pin["architecture"],
            "runtime_abi": pin["runtime_abi"],
            "backend": pin["backend"],
            "execution_kind": pin["execution_kind"],
            "authority_reference": reference,
            "requested_scope_digest": canonical_digest(edge["requested_scope_template"]),
            "adapter_digests": [],
        }
        if authority_mode == "interactive_only":
            binding["authority_mode"] = authority_mode
        bindings.append(binding)

    profile = dict(source)
    profile["state"] = "resolved"
    profile["base"] = {
        "pack_id": base_id,
        "artifact_digest": base_manifest["pack"]["artifact_digest"],
        "definition_revision": base_definition["definition_revision"],
        "resolution": "verified",
    }
    resolved_shell = {
        "provider_id": provider_id,
        "pack_id": shell_pack_id,
        "artifact_digest": shell_manifest["pack"]["artifact_digest"],
        "definition_revision": shell_definition["definition_revision"],
        "contract_id": "app.shell.v1",
        "platform": shell_request["platform"],
        "architecture": shell_request["architecture"],
    }
    if source.get("profile_api_version") == "io.tobkiri.profile.v5":
        resolved_shell["executable_artifact_digest"] = selected_variant["entrypoint_digest"]
    profile["shell"] = resolved_shell
    profile["packs"] = [
        {
            "pack_id": manifest["pack"]["id"],
            "artifact_digest": manifest["pack"]["artifact_digest"],
            "role": requested_pack_roles.get(manifest["pack"]["id"], "provider"),
        }
        for manifest in selected
        if manifest["pack"]["id"] not in {base_id, shell_pack_id}
    ]
    profile["requested_edges"] = resolved_edges
    from core_runtime.profile_content_projection import (
        resolve_profile_projection,
        selected_projection_roots,
    )
    from core_runtime.profile_pack_projection import admitted_projection_sources

    projection_sources = admitted_projection_sources(
        source.get("content_projections") or [],
        {item["pack"]["id"]: item["pack"]["artifact_digest"] for item in selected},
    )

    profile["content_projections"] = sorted(
        [
            resolve_profile_projection(item, pack_sources=projection_sources)
            for item in source.get("content_projections") or []
        ],
        key=lambda item: item["projection_id"],
    )
    selected_projection_roots(profile["content_projections"], pack_sources=projection_sources)
    profile["authority_references"] = references
    profile["profile_authority_snapshot_digest"] = snapshot_digest
    catalog_revision = canonical_digest(
        {manifest["pack"]["id"]: manifest["integrity"]["source_identity"] for manifest in selected}
    )
    _require_optional_pin(
        source["catalog_revision"],
        catalog_revision,
        "Profile catalog revision pin",
    )
    profile["catalog_revision"] = catalog_revision
    profile = validate_document(profile, "profile")
    profile_revision = canonical_digest(profile)
    effective_set = [
        {
            "role": (
                "base"
                if manifest["pack"]["id"] == base_id
                else "shell"
                if manifest["pack"]["id"] == shell_pack_id
                else "pack"
            ),
            "identity": manifest["pack"]["id"],
            "artifact_digest": manifest["pack"]["artifact_digest"],
        }
        for manifest in selected
    ]
    requested_edges_digest = canonical_digest(resolved_edges)
    constraints_digest = canonical_digest(
        {
            "base": {
                "definition_revision": base_definition["definition_revision"],
                "shell_requirements": base_definition["shell_requirements"],
                "dependencies": base_definition["dependencies"],
            },
            "shell": {
                "definition_revision": shell_definition["definition_revision"],
                "presentation": shell_definition["presentation"],
                "launch": shell_definition["launch"],
            },
            "packs": {
                manifest["pack"]["id"]: manifest["requirements"]
                for manifest in sorted(selected, key=lambda item: item["pack"]["id"])
            },
            "requested_scope_templates": [
                edge["requested_scope_template"] for edge in resolved_edges
            ],
        }
    )
    closure_digest = canonical_digest(
        {
            "effective_set": effective_set,
            "content_projections": profile["content_projections"],
        }
    )
    provenance_digest = canonical_digest(profile["provenance"])
    application = {
        "pack_id": application_ids[0],
        "artifact_digest": application_manifest["pack"]["artifact_digest"],
        "executable_artifact_digest": selected_variant["entrypoint_digest"],
        "definition_digest": canonical_digest(application_manifest),
    }
    launch_provider_id, launch_contract_id, launch_operation_id = _application_launch_identity(
        application_manifest
    )
    launch_contribution = {
        "provider_id": launch_provider_id,
        "contract_id": launch_contract_id,
        "operation_id": launch_operation_id,
        "platform": selected_variant["platform"],
        "architecture": selected_variant["architecture"],
        "artifact_digest": selected_variant["artifact_digest"],
        "relative_path": selected_variant["relative_path"],
        "entrypoint": selected_variant["entrypoint"],
    }

    plan: dict[str, Any] = {
        "plan_api_version": "io.tobkiri.resolved-plan.v2",
        "profile_id": profile_id,
        "profile_revision": profile_revision,
        "profile_definition_digest": profile_definition_digest,
        "catalog_revision": profile["catalog_revision"],
        "bundle_digest": bundle_digest,
        "profile_authority_snapshot_digest": snapshot_digest,
        "security_epoch": security_epoch,
        "base": {
            "pack_id": base_id,
            "artifact_digest": base_manifest["pack"]["artifact_digest"],
            "definition_digest": canonical_digest(base_definition),
        },
        "shell": {
            "provider_id": provider_id,
            "pack_id": shell_pack_id,
            "artifact_digest": shell_manifest["pack"]["artifact_digest"],
            "executable_artifact_digest": selected_variant["entrypoint_digest"],
            "contract_id": "app.shell.v1",
            "definition_digest": canonical_digest(shell_definition),
        },
        "application": application,
        "launch_contribution": launch_contribution,
        "effective_set": effective_set,
        "content_projections": profile["content_projections"],
        "requested_edges_digest": requested_edges_digest,
        "constraints_digest": constraints_digest,
        "closure_digest": closure_digest,
        "provenance_digest": provenance_digest,
        "bindings": bindings,
        "plan_digest": "sha256:" + "0" * 64,
    }
    plan["plan_digest"] = canonical_digest(
        {key: value for key, value in plan.items() if key != "plan_digest"}
    )
    plan = validate_document(plan, "resolved_plan")

    lock: dict[str, Any] = {
        "lock_api_version": "io.tobkiri.profile-lock.v5",
        "profile_id": profile_id,
        "profile_revision": profile_revision,
        "profile_definition_digest": profile_definition_digest,
        "catalog_revision": profile["catalog_revision"],
        "bundle_digest": bundle_digest,
        "security_epoch": security_epoch,
        "base": {
            "pack_id": base_id,
            "artifact_digest": base_manifest["pack"]["artifact_digest"],
            "definition_revision": base_definition["definition_revision"],
        },
        "shell": {
            **dict(profile["shell"]),
            "executable_artifact_digest": selected_variant["entrypoint_digest"],
        },
        "application": application,
        "effective_set": effective_set,
        "content_projections": profile["content_projections"],
        "variant_pins": sorted(
            variant_pins.values(),
            key=lambda item: (item["pack_id"], item["variant_id"]),
        ),
        "requested_edges_digest": requested_edges_digest,
        "constraints_digest": constraints_digest,
        "closure_digest": closure_digest,
        "provenance_digest": provenance_digest,
        "plan_digest": plan["plan_digest"],
        "profile_authority_snapshot_digest": snapshot_digest,
        "lock_digest": "sha256:" + "0" * 64,
    }
    lock["lock_digest"] = canonical_digest(
        {key: value for key, value in lock.items() if key != "lock_digest"}
    )
    lock = validate_document(lock, "profile_lock")
    return ResolvedDefaultProfile(profile=profile, lock=lock, plan=plan)

# Preserve legacy public error/function/pickle names after the single relocation.
# Imported protocol/third-party definitions are deliberately not rewritten.

_LEGACY_MODULE = "ecosystem.defaultspack.domain.runtime_v4.service"
_OWNED_DEFINITIONS = (
    ProfileResolutionDenied,
    ResolvedDefaultProfile,
    _application_launch_identity,
    _authority_mode,
    _bundle_digest,
    _edge_key,
    _exact_executable_variant,
    _provider_candidates,
    _require_digest,
    _require_optional_pin,
    _sha256_bytes,
    dynamic_profile_edges,
    resolve_default_profile,
)
for _definition in _OWNED_DEFINITIONS:
    _definition.__module__ = _LEGACY_MODULE
    if isinstance(_definition, type):
        for _member in vars(_definition).values():
            if isinstance(_member, _FunctionType):
                _function = _member
                while isinstance(_function, _FunctionType) and _function.__module__ == __name__:
                    _function.__module__ = _LEGACY_MODULE
                    _function = getattr(_function, "__wrapped__", None)
del _definition, _member, _function
