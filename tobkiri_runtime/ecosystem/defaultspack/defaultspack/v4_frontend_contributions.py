"""Project inert UI routes from the selected, verified Pack v4 closure."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator

from core_runtime.external_pack_catalog_v4 import (
    load_admitted_pack_catalog,
    resolve_admitted_pack_root,
)
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_protocol.canonical import strict_loads
from tobkiri_protocol.validation import validate_file


_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "frontend_contribution.schema.json"
_SCHEMA = Draft202012Validator(strict_loads(_SCHEMA_PATH.read_bytes()))
_ROUTE = re.compile(r"/(?:[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*)?")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,255}\Z")
_MAX_DESCRIPTOR_BYTES = 64 * 1024
_STRATEGY_CONTRIBUTION_FIELDS = frozenset(
    {
        "version",
        "id",
        "kind",
        "mode",
        "label",
        "description",
        "priority",
        "strategy_reference",
        "command",
        "localization",
        "accessibility",
    }
)


class FrontendPackDenied(ValueError):
    """One Pack cannot supply a safely verified declarative contribution."""


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _descriptor_path(root: Path, relative: str) -> Path:
    parts = relative.split("/")
    if (
        len(parts) != 3
        or parts[:2] != ["frontend", "contributions"]
        or not re.fullmatch(r"[a-zA-Z0-9._-]+\.json", parts[2])
    ):
        raise FrontendPackDenied("frontend descriptor path is invalid")
    parent = root / "frontend"
    directory = parent / "contributions"
    candidate = directory / parts[2]
    if any(path.is_symlink() for path in (parent, directory, candidate)) or not candidate.is_file():
        raise FrontendPackDenied("frontend descriptor path is unavailable")
    return candidate


def _is_frontend_descriptor_artifact(artifact: Mapping[str, Any]) -> bool:
    """Recognize both v4 UI artifacts and signed contribution sidecars."""

    path = artifact.get("path")
    return (
        artifact.get("kind") in {"ui.contribution", "sidecar"}
        and isinstance(path, str)
        and re.fullmatch(r"frontend/contributions/[a-zA-Z0-9._-]+\.json", path)
        is not None
    )


def _load_pack_routes(
    pack_id: str,
    expected_digest: str,
    *,
    profile_id: str,
    profile_revision: str,
    activation_id: str,
    plan_digest: str,
) -> list[dict[str, object]]:
    root = resolve_admitted_pack_root(pack_id)
    if root.is_symlink() or not root.is_dir():
        raise FrontendPackDenied("selected Pack root is unavailable")
    if (root / "pack.v4.json").is_symlink() or (root / "artifact-index.v4.json").is_symlink():
        raise FrontendPackDenied("selected Pack authority file is unsafe")
    manifest = validate_file(root / "pack.v4.json", "pack")
    descriptors = [
        item for item in manifest["artifacts"]
        if _is_frontend_descriptor_artifact(item)
    ]
    if not descriptors:
        return []
    if (
        manifest["pack"]["id"] != pack_id
        or manifest["pack"]["artifact_digest"] != expected_digest
        or manifest["pack"]["kind"] != "normal_sandbox"
    ):
        raise FrontendPackDenied("selected Pack manifest identity changed")
    compiled = compile_pack_root(root)
    if compiled.artifact.pack_id != pack_id or compiled.artifact.digest != expected_digest:
        raise FrontendPackDenied("selected Pack artifact digest changed")
    index = validate_file(root / "artifact-index.v4.json", "pack_artifact_index")
    if index["pack_id"] != pack_id or index["artifact_set_digest"] != expected_digest:
        raise FrontendPackDenied("selected Pack artifact index changed")
    index_by_path = {str(item["path"]): item for item in index["artifacts"]}
    projected: list[dict[str, object]] = []
    for artifact in descriptors:
        relative = str(artifact["path"])
        index_entry = index_by_path.get(relative)
        if (
            index_entry is None
            or index_entry["role"] != "sidecar"
            or index_entry["digest"] != artifact["digest"]
        ):
            raise FrontendPackDenied("frontend descriptor is not bound by the artifact index")
        path = _descriptor_path(root, relative)
        if path.stat().st_size > _MAX_DESCRIPTOR_BYTES:
            raise FrontendPackDenied("frontend descriptor exceeds its size limit")
        raw = path.read_bytes()
        digest = _digest(raw)
        if digest != artifact["digest"]:
            raise FrontendPackDenied("frontend descriptor digest changed")
        payload = strict_loads(raw)
        if not isinstance(payload, Mapping) or not _SCHEMA.is_valid(payload):
            raise FrontendPackDenied("frontend descriptor schema is invalid")
        if payload["kind"] != "route":
            continue
        if (
            payload["mode"] != "declarative"
            or len(str(payload["label"])) > 256
            or any(
                field in payload
                for field in (
                    "action_contract", "data_source_contract", "isolated", "module",
                    "region", "renderer", "schema",
                )
            )
        ):
            raise FrontendPackDenied("only inert declarative routes are admitted")
        route = str(payload["route"])
        if _ROUTE.fullmatch(route) is None:
            raise FrontendPackDenied("frontend route is invalid")
        view = payload["view"]
        if (
            not isinstance(view, Mapping)
            or set(view) - {"title", "body", "input"}
            or any(
                not isinstance(view[field], str) or len(view[field]) > 4096
                for field in ("title", "body") if field in view
            )
        ):
            raise FrontendPackDenied("declarative route view is invalid")
        input_view = view.get("input")
        if "input" in view and (
            not isinstance(input_view, Mapping)
            or set(input_view) - {"label", "placeholder"}
            or not isinstance(input_view.get("label"), str)
            or not input_view["label"].strip()
            or len(input_view["label"]) > 256
            or (
                "placeholder" in input_view
                and (
                    not isinstance(input_view["placeholder"], str)
                    or len(input_view["placeholder"]) > 256
                )
            )
        ):
            raise FrontendPackDenied("declarative route input is invalid")
        verified_view = dict(view)
        if isinstance(input_view, Mapping):
            verified_view["input"] = dict(input_view)
        projected.append({
            "contribution_id": str(payload["id"]),
            "kind": "route",
            "mode": "declarative",
            "label": str(payload["label"]),
            "priority": int(payload["priority"]),
            "owner_pack_id": pack_id,
            "owner_pack_hash": expected_digest,
            "build_identity": str(manifest["integrity"]["source_identity"]),
            "resolved_profile_id": profile_id,
            "resolved_profile_revision": profile_revision,
            "resolved_activation_id": activation_id,
            "resolved_plan_hash": plan_digest,
            "descriptor_hash": digest,
            "route": route,
            "route_match": "exact",
            "view": verified_view,
            "localization": dict(payload.get("localization") or {}),
            "accessibility": dict(payload["accessibility"]),
        })
    return projected


def project_selected_declarative_routes(
    effective_set: Sequence[Mapping[str, Any]],
    occupied: Sequence[Mapping[str, object]],
    *,
    profile_id: str,
    profile_revision: str,
    activation_id: str,
    plan_digest: str,
) -> tuple[list[dict[str, object]], list[dict[str, str]], list[str]]:
    """Expose only collision-free routes from exact selected v4 Pack digests."""
    proposed: list[dict[str, object]] = []
    diagnostics: list[dict[str, str]] = []
    quarantined: set[str] = set()
    seen_pack_ids: set[str] = set()
    admitted = load_admitted_pack_catalog()
    for item in effective_set:
        if item.get("role") != "pack":
            continue
        pack_id = str(item.get("identity") or "")
        digest = str(item.get("artifact_digest") or "")
        if not pack_id or pack_id in seen_pack_ids:
            raise FrontendPackDenied("selected Pack closure is ambiguous")
        seen_pack_ids.add(pack_id)
        record = admitted.get(pack_id)
        artifacts = record.get("runtime_artifacts", ()) if isinstance(record, Mapping) else ()
        if not isinstance(artifacts, (list, tuple)) or not any(
            isinstance(artifact, Mapping)
            and _is_frontend_descriptor_artifact(artifact)
            for artifact in artifacts
        ):
            continue
        try:
            proposed.extend(_load_pack_routes(
                pack_id, digest,
                profile_id=profile_id,
                profile_revision=profile_revision,
                activation_id=activation_id,
                plan_digest=plan_digest,
            ))
        except Exception as exc:
            # A bad optional Pack cannot make the selected Application vanish.
            quarantined.add(pack_id)
            diagnostics.append({
                "code": "v4_frontend_pack_quarantined",
                "severity": "error",
                "owner_pack_id": pack_id,
                "message": f"Selected Pack frontend is unavailable: {type(exc).__name__}",
            })
    proposed = [item for item in proposed if item["owner_pack_id"] not in quarantined]
    identities: dict[str, set[str]] = {}
    routes: dict[str, set[str]] = {}
    for item in [*occupied, *proposed]:
        owner = str(item.get("owner_pack_id") or "")
        contribution_id = str(item.get("contribution_id") or "")
        route = str(item.get("route") or "") if item.get("kind") == "route" else ""
        if contribution_id:
            identities.setdefault(contribution_id, set()).add(owner)
        if route:
            routes.setdefault(route, set()).add(owner)
    occupied_ids = {str(item.get("contribution_id") or "") for item in occupied}
    occupied_routes = [item for item in occupied if item.get("kind") == "route"]
    for item in proposed:
        owner = str(item["owner_pack_id"])
        identity = str(item["contribution_id"])
        route = str(item["route"])
        if (
            identity in occupied_ids
            or any(
                route == str(candidate.get("route") or "")
                or (
                    candidate.get("route_match") == "subpath"
                    and route.startswith(str(candidate.get("route") or "").rstrip("/") + "/")
                )
                for candidate in occupied_routes
            )
            or len(identities[identity]) != 1 or len(routes[route]) != 1
            or sum(candidate["contribution_id"] == identity for candidate in proposed) != 1
            or sum(candidate["route"] == route for candidate in proposed) != 1
        ):
            quarantined.add(owner)
    for pack_id in sorted(quarantined):
        if not any(item["owner_pack_id"] == pack_id for item in diagnostics):
            diagnostics.append({
                "code": "v4_frontend_identity_collision",
                "severity": "error",
                "owner_pack_id": pack_id,
                "message": "Selected Pack frontend route or identity collides",
            })
    accepted = [item for item in proposed if item["owner_pack_id"] not in quarantined]
    accepted.sort(key=lambda item: (str(item["route"]), str(item["contribution_id"])))
    return accepted, diagnostics, sorted(quarantined)


def project_selected_ai_strategies(
    effective_set: Sequence[Mapping[str, Any]],
    strategy_catalog: Mapping[str, Any],
    *,
    profile_id: str,
    profile_revision: str,
    activation_id: str,
    plan_digest: str,
) -> tuple[list[dict[str, object]], list[dict[str, str]], list[str]]:
    """Project strategy choices only from signed Pack data and one Host Plan.

    The resource catalog is the execution authority.  A Pack descriptor can
    supply display text only after its signed artifact identity and its exact
    public strategy reference both match that catalog.  Neither Pack metadata
    nor browser input can claim that a strategy is available.
    """

    records = strategy_catalog.get("strategies")
    if not isinstance(records, list):
        raise FrontendPackDenied("strategy catalog is invalid")
    by_reference: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        reference = record.get("strategy_reference")
        if not isinstance(reference, str) or _IDENTIFIER.fullmatch(reference) is None:
            continue
        if reference in by_reference:
            raise FrontendPackDenied("strategy catalog has duplicate references")
        by_reference[reference] = record

    proposed: list[dict[str, object]] = []
    diagnostics: list[dict[str, str]] = []
    quarantined: set[str] = set()
    admitted = load_admitted_pack_catalog()
    seen_pack_ids: set[str] = set()
    for item in effective_set:
        if item.get("role") != "pack":
            continue
        pack_id = str(item.get("identity") or "")
        digest = str(item.get("artifact_digest") or "")
        if not pack_id or pack_id in seen_pack_ids:
            raise FrontendPackDenied("selected Pack closure is ambiguous")
        seen_pack_ids.add(pack_id)
        record = admitted.get(pack_id)
        artifacts = record.get("runtime_artifacts", ()) if isinstance(record, Mapping) else ()
        if not isinstance(artifacts, (list, tuple)) or not any(
            isinstance(artifact, Mapping)
            and _is_frontend_descriptor_artifact(artifact)
            for artifact in artifacts
        ):
            continue
        try:
            proposed.extend(
                _load_pack_ai_strategies(
                    pack_id,
                    digest,
                    catalog=by_reference,
                    profile_id=profile_id,
                    profile_revision=profile_revision,
                    activation_id=activation_id,
                    plan_digest=plan_digest,
                )
            )
        except Exception as exc:
            quarantined.add(pack_id)
            diagnostics.append(
                {
                    "code": "v4_strategy_pack_quarantined",
                    "severity": "error",
                    "owner_pack_id": pack_id,
                    "message": (
                        "Selected Pack strategy contribution is unavailable: "
                        f"{type(exc).__name__}"
                    ),
                }
            )

    by_reference_items: dict[str, list[dict[str, object]]] = {}
    for item in proposed:
        by_reference_items.setdefault(str(item["strategy_reference"]), []).append(item)
    for reference, items in by_reference_items.items():
        if len(items) > 1:
            for item in items:
                quarantined.add(str(item["owner_pack_id"]))
            diagnostics.append(
                {
                    "code": "v4_strategy_reference_collision",
                    "severity": "error",
                    "owner_pack_id": "",
                    "message": f"Strategy reference is ambiguous: {reference}",
                }
            )
    accepted = [
        item for item in proposed if str(item["owner_pack_id"]) not in quarantined
    ]
    accepted.sort(
        key=lambda item: (
            -_strategy_priority(item),
            str(item["label"]),
            str(item["strategy_reference"]),
        )
    )
    return accepted, diagnostics, sorted(quarantined)


def _strategy_priority(item: Mapping[str, object]) -> int:
    """Return the schema-validated strategy priority without accepting bools."""

    priority = item.get("priority")
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise FrontendPackDenied("strategy contribution priority is invalid")
    return priority


def _load_pack_ai_strategies(
    pack_id: str,
    expected_digest: str,
    *,
    catalog: Mapping[str, Mapping[str, Any]],
    profile_id: str,
    profile_revision: str,
    activation_id: str,
    plan_digest: str,
) -> list[dict[str, object]]:
    """Verify and project the strategy declarations from one exact Pack."""

    root = resolve_admitted_pack_root(pack_id)
    if root.is_symlink() or not root.is_dir():
        raise FrontendPackDenied("selected Pack root is unavailable")
    if (root / "pack.v4.json").is_symlink() or (root / "artifact-index.v4.json").is_symlink():
        raise FrontendPackDenied("selected Pack authority file is unsafe")
    manifest = validate_file(root / "pack.v4.json", "pack")
    descriptors = [
        item for item in manifest["artifacts"]
        if _is_frontend_descriptor_artifact(item)
    ]
    if not descriptors:
        return []
    if (
        manifest["pack"]["id"] != pack_id
        or manifest["pack"]["artifact_digest"] != expected_digest
        or manifest["pack"]["kind"] not in {"normal_sandbox", "host_extension"}
    ):
        raise FrontendPackDenied("selected Pack manifest identity changed")
    compiled = compile_pack_root(root)
    if compiled.artifact.pack_id != pack_id or compiled.artifact.digest != expected_digest:
        raise FrontendPackDenied("selected Pack artifact digest changed")
    index = validate_file(root / "artifact-index.v4.json", "pack_artifact_index")
    if index["pack_id"] != pack_id or index["artifact_set_digest"] != expected_digest:
        raise FrontendPackDenied("selected Pack artifact index changed")
    index_by_path = {str(item["path"]): item for item in index["artifacts"]}
    projected: list[dict[str, object]] = []
    for artifact in descriptors:
        relative = str(artifact["path"])
        index_entry = index_by_path.get(relative)
        if (
            index_entry is None
            or index_entry["role"] != "sidecar"
            or index_entry["digest"] != artifact["digest"]
        ):
            raise FrontendPackDenied(
                "frontend descriptor is not bound by the artifact index"
            )
        path = _descriptor_path(root, relative)
        if path.stat().st_size > _MAX_DESCRIPTOR_BYTES:
            raise FrontendPackDenied("frontend descriptor exceeds its size limit")
        raw = path.read_bytes()
        digest = _digest(raw)
        if digest != artifact["digest"]:
            raise FrontendPackDenied("frontend descriptor digest changed")
        payload = strict_loads(raw)
        if not isinstance(payload, Mapping) or not _SCHEMA.is_valid(payload):
            raise FrontendPackDenied("frontend descriptor schema is invalid")
        if (
            payload.get("kind") != "ai_strategy"
            or payload.get("mode") != "declarative"
            or set(payload) - _STRATEGY_CONTRIBUTION_FIELDS
            or any(
                field in payload
                for field in (
                    "route",
                    "action_contract",
                    "data_source_contract",
                    "isolated",
                    "module",
                    "region",
                    "renderer",
                    "schema",
                    "view",
                )
            )
        ):
            continue
        reference = payload.get("strategy_reference")
        if not isinstance(reference, str) or _IDENTIFIER.fullmatch(reference) is None:
            raise FrontendPackDenied("strategy reference is invalid")
        catalog_record = catalog.get(reference)
        if (
            catalog_record is None
            or catalog_record.get("pack_id") != pack_id
            or catalog_record.get("artifact_digest") != expected_digest
        ):
            raise FrontendPackDenied("strategy reference is not admitted by the Plan")
        label = payload.get("label")
        description = payload.get("description")
        command = payload.get("command")
        if (
            not isinstance(label, str)
            or not label.strip()
            or len(label) > 256
            or (description is not None and not isinstance(description, str))
            or (isinstance(description, str) and len(description) > 1024)
            or (command is not None and not isinstance(command, Mapping))
        ):
            raise FrontendPackDenied("strategy contribution presentation is invalid")
        result: dict[str, object] = {
            "contribution_id": str(payload["id"]),
            "kind": "ai_strategy",
            "mode": "declarative",
            "strategy_reference": reference,
            "label": label.strip(),
            "priority": int(payload["priority"]),
            "owner_pack_id": pack_id,
            "owner_pack_hash": expected_digest,
            "build_identity": str(manifest["integrity"]["source_identity"]),
            "resolved_profile_id": profile_id,
            "resolved_profile_revision": profile_revision,
            "resolved_activation_id": activation_id,
            "resolved_plan_hash": plan_digest,
            "descriptor_hash": digest,
            # These values are a server projection of verification facts.  No
            # caller-supplied availability flag participates in this decision.
            "signature_verified": True,
            "plan_admitted": True,
            "available": True,
            "accessibility": dict(payload["accessibility"]),
        }
        if isinstance(description, str) and description.strip():
            result["description"] = description.strip()
        if command is not None:
            result["command"] = dict(command)
        projected.append(result)
    return projected
