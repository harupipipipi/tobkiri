"""Component entrypoint for the sealed Defaultspack presentation guest."""

import json

import wit_world
from componentize_py_types import Err
from presentation import tobkiri_packvm_invoke


MAX_OPERATION_ID_BYTES = 128
MAX_INPUT_JSON_BYTES = 64 * 1024
MAX_OUTPUT_JSON_BYTES = 1024 * 1024


class WitWorld(wit_world.WitWorld):
    """Expose only the read-only presentation projection without Host imports."""

    def invoke(self, operation_id: str, payload_json: str) -> str:
        """Project validated display data and return a bounded JSON object."""
        try:
            if not 0 < len(operation_id.encode("utf-8")) <= MAX_OPERATION_ID_BYTES:
                raise ValueError("operation identity is outside the component limit")
            if not 0 < len(payload_json.encode("utf-8")) <= MAX_INPUT_JSON_BYTES:
                raise ValueError("presentation input exceeds the component limit")
            payload = json.loads(payload_json)
            result = tobkiri_packvm_invoke(operation_id, payload)
            encoded = json.dumps(
                result,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            if len(encoded) > MAX_OUTPUT_JSON_BYTES:
                raise ValueError("presentation output exceeds the component limit")
        except (RecursionError, TypeError, UnicodeError, ValueError):
            raise Err("invalid_request") from None
        return encoded.decode("utf-8")
