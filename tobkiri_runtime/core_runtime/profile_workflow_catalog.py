"""Read selected immutable Workflow v4 documents without granting authority."""

from __future__ import annotations

import os
import hashlib
from pathlib import Path
import re
import stat
from typing import Any

from jsonschema import Draft202012Validator
from tobkiri_protocol.canonical import strict_loads

from .profile_content_projection import _inventory, selected_projection_roots
from .resolved_profile_scope import effective_profile_projections
from .workflow_v4.engine import WorkflowEngineV4


_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "ecosystem/tobkiri_workflow_pack/schemas/workflow-definition.v4.schema.json"
)
_INTENT_SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "tobkiri_protocol/schemas/profile_workflow_intent_v1.schema.json"
)


def selected_workflow_definitions() -> tuple[dict[str, Any], ...]:
    """List only Workflow definitions in the captured Profile's projections.

    Documents live at ``workflows/<id>.workflow.v4.json``. This is a read-only
    source catalog, not a persisted draft, published Workflow, run or approval.
    Duplicate IDs fail closed across projections. Reading selected Pack content
    also revalidates Host admission and the exact Profile artifact pins.
    """

    selections = effective_profile_projections()
    roots = selected_projection_roots(selections, kind="profile_content")
    validator = Draft202012Validator(strict_loads(_SCHEMA.read_bytes()))
    result: dict[str, dict[str, Any]] = {}
    for projection_id, root in roots:
        consumed: dict[str, str] = {}
        paths = [
            *sorted((root / "workflows").glob("*.workflow.v4.json")),
            *sorted((root / "workflows").glob("*.workflow.intent.v1.json")),
        ]
        for path in paths:
            is_intent = path.name.endswith(".workflow.intent.v1.json")
            suffix = ".workflow.intent.v1.json" if is_intent else ".workflow.v4.json"
            definition_id = path.name.removesuffix(suffix)
            if _ID.fullmatch(definition_id) is None or definition_id in result:
                raise ValueError("selected Workflow definition identity is invalid or duplicate")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > 1024 * 1024:
                    raise ValueError("selected Workflow document is not a bounded regular file")
                raw = stream.read(1024 * 1024 + 1)
                after = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) or len(raw) != before.st_size:
                raise ValueError("selected Workflow document changed during read")
            document = strict_loads(raw)
            if is_intent:
                Draft202012Validator(strict_loads(_INTENT_SCHEMA.read_bytes())).validate(document)
            else:
                validator.validate(document)
            result[definition_id] = {
                "definition_id": definition_id,
                "source_projection_id": projection_id,
                "document": document,
            }
            consumed[path.relative_to(root).as_posix()] = (
                "sha256:" + hashlib.sha256(raw).hexdigest()
            )
        inventory = _inventory(root)
        if any(inventory.get(path) != digest for path, digest in consumed.items()):
            raise ValueError("selected Workflow read differs from its verified inventory")
    # Bind the bytes read above to a still-current immutable inventory.
    selected_projection_roots(selections, kind="profile_content")
    return tuple(result[key] for key in sorted(result))


def compile_selected_workflow(engine: WorkflowEngineV4, definition_id: str) -> dict[str, Any]:
    """Use the real engine to bind one selected source to its captured catalog.

    The engine must come from the Host's normal captured Workflow provider.
    Validation/compile-preview checks exact Contract/Operation/Function pins
    and schemas. This performs no store writes or attempt/authority operations.
    """

    for selection in selected_workflow_definitions():
        if selection["definition_id"] == definition_id:
            document = selection["document"]
            if document.get("workflow_intent_api_version") is not None:
                palette = engine.operation_palette()["operations"]
                steps = []
                for step in document["steps"]:
                    request = step["request"]
                    candidates = [
                        operation
                        for operation in palette
                        if operation["provider_id"] == request["function_id"]
                        and all(
                            operation[key] == request[key]
                            for key in ("contract_id", "contract_revision_digest", "operation_id")
                        )
                    ]
                    if len(candidates) != 1:
                        raise ValueError(
                            "selected Workflow Function has no unique exact palette binding"
                        )
                    bound = {key: value for key, value in request.items() if key != "function_id"}
                    bound["function_principal_id"] = candidates[0]["function_principal_id"]
                    steps.append({**step, "request": bound})
                document = {
                    **{
                        key: value
                        for key, value in document.items()
                        if key not in {"workflow_intent_api_version", "steps"}
                    },
                    "workflow_api_version": "io.tobkiri.workflow.v4",
                    "steps": steps,
                }
            return engine.compile_preview(document)
    raise ValueError("Workflow definition is outside the captured Profile selection")
