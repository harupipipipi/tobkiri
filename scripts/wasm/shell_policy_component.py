"""Component entrypoint; componentize-py executes this inside its Wasm guest."""

import json

import wit_world
from componentize_py_types import Err
from policy import tobkiri_packvm_invoke


class WitWorld(wit_world.WitWorld):
    """Expose only the canonical Shell Policy inspection operation."""

    def invoke(self, operation_id: str, payload_json: str) -> str:
        """Inspect a JSON payload without providing any Host capabilities."""
        try:
            payload = json.loads(payload_json)
            result = tobkiri_packvm_invoke(operation_id, payload)
        except (ValueError, TypeError):
            raise Err("invalid_request") from None
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
