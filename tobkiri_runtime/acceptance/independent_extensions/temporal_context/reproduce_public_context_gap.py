"""Reproduce the documented external input boundary without runtime invocations."""
from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

RUNTIME_ROOT = Path(__file__).resolve().parents[3]


def reproduce() -> dict[str, object]:
    """Validate real public input schemas; do not simulate a Host or Workflow engine."""
    saved_schema = json.loads((RUNTIME_ROOT / "tobkiri_protocol/schemas/"
                              "saved_conversation_input_v1.schema.json").read_text())
    validator = Draft202012Validator(saved_schema)
    baseline = {"request": {"turn_id": "example.turn", "conversation_id": "example.chat",
                            "conversation_revision": 1, "content": "どう？"}}
    validator.validate(baseline)
    denied = {}
    for field, value in (("internal_timing_context", {"elapsed_seconds": 3600}),
                         ("system_messages", [{"role": "system", "content": "gap"}]),
                         ("last_task_completed_at", "2026-10-08T00:00:00Z"),
                         ("approved", True)):
        payload = json.loads(json.dumps(baseline))
        payload["request"][field] = value
        errors = list(validator.iter_errors(payload))
        if not errors:
            raise AssertionError(f"public input boundary unexpectedly accepts {field}")
        denied[field] = {"validator": errors[0].validator,
                         "path": list(errors[0].path),
                         "message": errors[0].message}
    return {"scope": "public JSON-schema validation only; no engine/native call",
            "baseline_saved_input_valid": True, "denied_external_fields": denied,
            "step_output_status": "Documented in selected_conversation_workflow.md; not executed here",
            "selected_output_sink_status": "Public typed sink exists; caller request injection is rejected"}


if __name__ == "__main__":
    print(json.dumps(reproduce(), sort_keys=True, indent=2))
