"""Render draft public Contract schemas; never produce activation authority."""
import hashlib
from tobkiri_protocol.canonical import canonical_digest
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def digest(value: object) -> str:
    """Hash deterministic draft schema bytes (not an official release seal)."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def render(pack_id: str = "acceptance.temporal.context") -> dict:
    """Return exact proposed reducer contract for composition review."""
    identifier = {"type": "string", "minLength": 1, "maxLength": 128}
    offset_time = {"type": "string", "maxLength": 64,
                   "pattern": r"(Z|[+-][0-9]{2}:[0-9]{2})$"}
    state = {"type": "object", "additionalProperties": False,
             "required": ["namespace", "sequence", "event_id", "last_event_at",
                          "last_task_completed_at"],
             "properties": {"namespace": identifier, "sequence": {
                 "type": "integer", "minimum": 1}, "event_id": identifier,
                 "last_event_at": offset_time, "last_task_completed_at": {
                     "oneOf": [offset_time, {"type": "null"}]}}}
    empty = {"type": "object", "maxProperties": 0}
    event = {"type": "object", "additionalProperties": False,
             "required": ["id", "sequence", "kind", "at"],
             "properties": {"id": identifier, "sequence": {
                 "type": "integer", "minimum": 1}, "at": offset_time,
                 "kind": {"enum": ["assistant.completed", "assistant.error",
                                    "assistant.cancelled", "tool.completed",
                                    "user.received"]}}}
    input_schema = {"type": "object", "additionalProperties": False,
                    "required": ["namespace", "state", "event"],
                    "properties": {"namespace": identifier,
                                   "state": {"oneOf": [empty, state]},
                                   "event": event}}
    temporal = {"type": "object", "additionalProperties": False,
                "required": ["previous_task_completed_at", "current_user_message_at",
                             "elapsed_seconds"],
                "properties": {"previous_task_completed_at": offset_time,
                               "current_user_message_at": offset_time,
                               "elapsed_seconds": {"type": "number",
                                                   "minimum": 3600}}}
    output_schema = {"type": "object", "additionalProperties": False,
                     "required": ["state", "internal_temporal_context"],
                     "properties": {"state": state, "internal_temporal_context": {
                         "oneOf": [{"type": "null"}, temporal]}}}
    error = {"type": "object", "additionalProperties": False,
             "required": ["code"], "properties": {"code": {
                 "enum": ["invalid_input", "stale_event", "namespace_mismatch"]}}}
    schemas = [input_schema, output_schema, error]
    source = {"issue": "https://github.com/harupipipipi/tobkiri/issues/1409",
              "handler_digest": "sha256:" + hashlib.sha256(
                  (ROOT / "source/temporal.py")
                  .read_bytes()).hexdigest()}
    provenance = {"schema": "io.tobkiri.provenance.v1", "source_kind": "generated",
                  "source_path": "author_contract.py", "source_digest": digest(source),
                  "generator": "acceptance.temporal.author", "generator_version": "0.1.0",
                  "repository_commit": "working-tree", "repository_tree":
                  digest(source).split(":")[1], "normative": False, "evidence": []}
    contract = {"contract_api_version": "io.tobkiri.contract.v4",
                "contract_id": f"{pack_id}.v1",
                "version": "0.1.0", "revision_digest": digest(schemas),
                "owner": pack_id, "status": "draft",
                "operations": [{"operation_id": "temporal.reduce",
                    "input_schema_digest": digest(input_schema),
                    "output_schema_digest": digest(output_schema),
                    "error_schema_digest": digest(error), "effect_ceiling": [],
                    "scope_semantics": "declarative",
                    "idempotency": {"mode": "none"}}],
                "schema_catalog": {digest(schema): schema for schema in schemas},
                "provider_semantics": {"provider_id": f"{pack_id}.provider",
                    "cardinality": "one", "security": "internal",
                    "failure": "fail_closed", "isolation": "sandbox",
                    "required_capabilities": [], "lifecycle": {}},
                "provenance": provenance}
    contract["revision_digest"] = canonical_digest({
        key: value for key, value in contract.items()
        if key not in {"revision_digest", "provenance"}
    })
    return contract


if __name__ == "__main__":
    (ROOT / "temporal.contract.draft.v4.json").write_text(
        json.dumps(render(), sort_keys=True, indent=2) + "\n")
