"""Pack-owned transport; the captured Python normalizer retains its semantics."""

from ordered_json import dumps, loads
from original_bridge import GlobalContractInvocationError, create_tool_intent_operation

PROTOCOL = "ordered-finite-json-v1"
PREFIX = "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize"
OPERATIONS = (PREFIX, PREFIX + ".generate", PREFIX + ".stream")


def request(payload: object) -> str:
    """Encode only the explicitly bounded migration domain."""
    return dumps(payload)


def invoke(operation: str, payload: str) -> str:
    """Preserve original results/errors, never authorize or execute a tool."""
    if operation not in OPERATIONS:
        raise ValueError("unknown ordered tool bridge operation")
    value = loads(payload)
    if type(value) is not dict:
        raise ValueError("tool bridge request must be an object")
    # The original Host factory maps all three registered operations to normalize.
    output = create_tool_intent_operation(None)("normalize", value)
    # Original inner JSON may produce big integers/non-finite values. They are
    # exclusions of this transport profile, not rewritten original behavior.
    return dumps(output)


def response(payload: str) -> object:
    """Decode a successful result without sorting keys or coercing numbers."""
    value = loads(payload)
    if type(value) is not dict or set(value) != {"intents"}:
        raise ValueError("invalid ordered tool bridge response")
    return value
