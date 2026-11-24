"""Finite, hand-authored inputs for the original Python provider oracle.

The generated expectations are always obtained by invoking the source adapter;
this module contains inputs only and makes no pass/fail assertions.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    """One exact WIT operation and raw JSON payload passed to an adapter."""

    provider: str
    case_id: str
    operation: str
    payload: str


@dataclass(frozen=True)
class ScalarVector:
    """One bounded JSON value whose original decoded value gets builtin probes."""

    vector_id: str
    input_json: str


def _case(provider: str, case_id: str, operation: str, payload: str) -> Case:
    return Case(provider, case_id, operation, payload)


_USAGE_PREFIX = "rumi_ai_usage_pack.ai-usage-cost"
_STREAM_OPERATION = "rumi_ai_stream_pack.ai-stream-normalize"
_DIGITS_4300 = "9" * 4300
_DIGITS_4301 = "9" * 4301
_V2_MAX_BYTES = 4 * 1024 * 1024


def _node_limit_payload() -> str:
    """A fixed 100001-item input, large enough to exceed v2's node budget."""
    return '{"pricing_revision":[' + ",".join("0" for _ in range(100001)) + "]}"


def _stream_output_byte_limit_payload() -> str:
    """Bounded input whose request ID expands across many projected events."""
    request_id = "r" * 1024
    events = [
        '{"type":"text_delta"}' for _ in range(4999)
    ] + ['{"type":"finish"}']
    return (
        '{"request_id":"' + request_id + '","value":['
        + ",".join(events)
        + "]}"
    )


def _stream_output_node_limit_payload() -> str:
    """Stay below input-node limits while output projection exceeds its budget."""
    events = [
        '{"type":"text_delta"}' for _ in range(5263)
    ] + ['{"type":"finish"}']
    return '{"request_id":"r","value":[' + ",".join(events) + "]}"


def _nested_payload(depth: int) -> str:
    return '{"pricing_revision":' + "[" * depth + "0" + "]" * depth + "}"


_USAGE_CASES = (
    _case(
        "usage-cost", "aliases-calculate", "calculate",
        '{"usage":{"input_tokens":2,"output_tokens":3},'
        '"pricing":{"input":0.25,"output":0.5}}',
    ),
    _case(
        "usage-cost", "aliases-normalize", "normalize",
        '{"usage":{"input_tokens":2,"output_tokens":3},'
        '"pricing":{"input":0.25,"output":0.5}}',
    ),
    _case(
        "usage-cost", "aliases-base-identity", _USAGE_PREFIX,
        '{"usage":{"input_tokens":2,"output_tokens":3},'
        '"pricing":{"input":0.25,"output":0.5}}',
    ),
    _case(
        "usage-cost", "aliases-generate", _USAGE_PREFIX + ".generate",
        '{"usage":{"input_tokens":2,"output_tokens":3},'
        '"pricing":{"input":0.25,"output":0.5}}',
    ),
    _case(
        "usage-cost", "aliases-stream", _USAGE_PREFIX + ".stream",
        '{"usage":{"input_tokens":2,"output_tokens":3},'
        '"pricing":{"input":0.25,"output":0.5}}',
    ),
    _case(
        "usage-cost", "unknown-operation", "not-a-usage-operation",
        '{"usage":{"input_tokens":1}}',
    ),
    _case("usage-cost", "outer-null", "calculate", "null"),
    _case("usage-cost", "outer-array", "calculate", "[]"),
    _case("usage-cost", "outer-number", "calculate", "17"),
    _case("usage-cost", "outer-string", "calculate", '"{}"'),
    _case("usage-cost", "outer-boolean", "calculate", "true"),
    _case(
        "usage-cost", "duplicate-outer-key-rejected", "calculate",
        '{"usage":{"input_tokens":1},"usage":{"input_tokens":2},'
        '"pricing":{"input":1,"output":2}}',
    ),
    _case(
        "usage-cost", "duplicate-nested-key-rejected", "calculate",
        '{"usage":{"input_tokens":1,"input_tokens":2,"output_tokens":3},'
        '"pricing":{"input":0.25,"output":0.5}}',
    ),
    _case(
        "usage-cost", "primary-null-does-not-use-fallback", "calculate",
        '{"usage":{"input_tokens":null,"prompt_tokens":9,'
        '"output_tokens":3,"completion_tokens":8},'
        '"pricing":{"input":1,"output":1}}',
    ),
    _case(
        "usage-cost", "fallback-token-key", "calculate",
        '{"usage":{"prompt_tokens":4,"completion_tokens":5},'
        '"pricing":{"input":0.1,"output":0.2}}',
    ),
    _case(
        "usage-cost", "bool-null-negative-zero-float", "calculate",
        '{"usage":{"input_tokens":true,"output_tokens":-0.0},'
        '"pricing":{"input":0.5,"output":0.5},"pricing_revision":-0.0}',
    ),
    _case(
        "usage-cost", "numeric-value-null", "calculate",
        '{"usage":{"input_tokens":null,"output_tokens":false},'
        '"pricing":{"input":null,"output":false}}',
    ),
    _case(
        "usage-cost", "numeric-value-list-dict", "calculate",
        '{"usage":{"input_tokens":[],"output_tokens":{"n":2}},'
        '"pricing":{"input":[1],"output":{"n":1}}}',
    ),
    _case(
        "usage-cost", "numeric-string-underscores", "calculate",
        '{"usage":{"input_tokens":"1_2.5","output_tokens":"3_0"},'
        '"pricing":{"input":"0_1.5","output":" 2 "}}',
    ),
    _case(
        "usage-cost", "numeric-string-unicode-decimal", "calculate",
        '{"usage":{"input_tokens":"\u0661\u0662\u0663.5",'
        '"output_tokens":"\u0664"},"pricing":{"input":"1",'
        '"output":"1"}}',
    ),
    _case(
        "usage-cost", "numeric-string-unicode-whitespace", "calculate",
        '{"usage":{"input_tokens":"\u200312.5\u00a0",'
        '"output_tokens":"\u30004\u3000"},'
        '"pricing":{"input":"1","output":"1"}}',
    ),
    _case(
        "usage-cost", "numeric-string-nonfinite-unknown", "calculate",
        '{"usage":{"input_tokens":"Infinity","output_tokens":"NaN"},'
        '"pricing":{"input":"inf","output":"-Infinity"}}',
    ),
    _case(
        "usage-cost", "numeric-string-negative-unknown", "calculate",
        '{"usage":{"input_tokens":"-1","output_tokens":"2"},'
        '"pricing":{"input":"1","output":"1"}}',
    ),
    _case(
        "usage-cost", "truthy-currency-provenance-nested-order", "calculate",
        '{"usage":{"input_tokens":2,"output_tokens":3},'
        '"pricing":{"input":0.25,"output":0.5,'
        '"currency":{"z":[true,null],"a":"é😀"}},'
        '"usage_provenance":{"z":1,"a":{"x":true,"y":null}},'
        '"pricing_revision":{"v":[1,true,null],"u":"é"}}',
    ),
    _case(
        "usage-cost", "falsey-currency-provenance-defaults", "calculate",
        '{"usage":{"input_tokens":1,"output_tokens":1},'
        '"pricing":{"input":1,"output":1,"currency":""},'
        '"usage_provenance":0}',
    ),
    _case(
        "usage-cost", "negative-zero-int-is-not-negative-float", "calculate",
        '{"usage":{"input_tokens":-0,"output_tokens":-0.0},'
        '"pricing":{"input":1,"output":1}}',
    ),
    _case(
        "usage-cost", "finite-float-binary64-boundaries", "calculate",
        '{"usage":{"input_tokens":5e-324,"output_tokens":1.0},'
        '"pricing":{"input":1.7976931348623157e308,"output":0.0}}',
    ),
    _case(
        "usage-cost", "native-int-4300-float-overflow", "calculate",
        '{"usage":{"input_tokens":' + _DIGITS_4300 + '}}',
    ),
    _case(
        "usage-cost", "native-int-4301-codec-reject", "calculate",
        '{"usage":{"input_tokens":' + _DIGITS_4301 + '}}',
    ),
    _case(
        "usage-cost", "numeric-string-4300-finite-data-nonfinite-number", "calculate",
        '{"usage":{"input_tokens":"' + _DIGITS_4300 + '"}}',
    ),
    _case(
        "usage-cost", "finite-operands-product-overflow", "calculate",
        '{"usage":{"input_tokens":1e308,"output_tokens":1e308},'
        '"pricing":{"input":1e308,"output":1e308}}',
    ),
    _case(
        "usage-cost", "finite-operands-total-overflow", "calculate",
        '{"usage":{"input_tokens":1e308,"output_tokens":1e308}}',
    ),
    _case(
        "usage-cost", "nonfinite-json-nan", "calculate",
        '{"usage":{"input_tokens":NaN}}',
    ),
    _case(
        "usage-cost", "nonfinite-json-infinity", "calculate",
        '{"pricing":{"input":Infinity}}',
    ),
    _case(
        "usage-cost", "nonfinite-json-negative-infinity", "calculate",
        '{"pricing":{"input":-Infinity}}',
    ),
    _case(
        "usage-cost", "nonfinite-json-exponent-overflow", "calculate",
        '{"pricing":{"input":1e309}}',
    ),
    _case(
        "usage-cost", "codec-depth-below-native-limit", "calculate",
        _nested_payload(900),
    ),
    _case(
        "usage-cost", "codec-depth-native-recursion-edge", "calculate",
        _nested_payload(1100),
    ),
    _case(
        "usage-cost", "codec-node-limit", "calculate", _node_limit_payload(),
    ),
    _case(
        "usage-cost", "codec-byte-limit", "calculate",
        '{"pricing_revision":"' + "x" * _V2_MAX_BYTES + '"}',
    ),
    _case(
        "usage-cost", "encoded-output-byte-limit", "calculate",
        '{"pricing_revision":"' + "x" * (_V2_MAX_BYTES - 32) + '"}',
    ),
)


_STREAM_CASES = (
    _case(
        "stream-normalizer", "aliases-normalize", "normalize",
        '{"request_id":"r","value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "aliases-validate", "validate",
        '{"request_id":"r","value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "aliases-manifest-identity", _STREAM_OPERATION,
        '{"request_id":"r","value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "unknown-operation", "not-a-stream-operation",
        '{"request_id":"r","value":[{"type":"finish"}]}',
    ),
    _case("stream-normalizer", "outer-null", "normalize", "null"),
    _case("stream-normalizer", "outer-array", "normalize", "[]"),
    _case("stream-normalizer", "outer-number", "normalize", "17"),
    _case(
        "stream-normalizer", "duplicate-outer-keys-rejected", "normalize",
        '{"request_id":"first","request_id":"\u2003last\u00a0",'
        '"provider_attempt":0,"provider_attempt":2,'
        '"value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "request-id-pystr-nested-order", "normalize",
        '{"request_id":{"z":[true,null],"a":"é😀"},'
        '"value":[{"type":"finish","extra":{"z":1,"a":2}}]}',
    ),
    _case(
        "stream-normalizer", "request-id-falsey-default", "normalize",
        '{"request_id":false,"value":[{"type":"error"}]}',
    ),
    _case(
        "stream-normalizer", "request-id-unicode-strip", "normalize",
        '{"request_id":"\u2003req\u00a0","value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "provider-attempt-missing-default", "normalize",
        '{"request_id":"r","value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "provider-attempt-null-default", "normalize",
        '{"request_id":"r","provider_attempt":null,'
        '"value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "provider-attempt-bool-default", "normalize",
        '{"request_id":"r","provider_attempt":true,'
        '"value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "provider-attempt-negative-zero-default", "normalize",
        '{"request_id":"r","provider_attempt":-0.0,'
        '"value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "provider-attempt-float-truncates", "normalize",
        '{"request_id":"r","provider_attempt":2.9,'
        '"value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "provider-attempt-string-whitespace", "normalize",
        '{"request_id":"r","provider_attempt":" 2 ",'
        '"value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "provider-attempt-string-underscores", "normalize",
        '{"request_id":"r","provider_attempt":"1_0",'
        '"value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "provider-attempt-unicode-decimals", "normalize",
        '{"request_id":"r","provider_attempt":"\u0661\u0662\u0663",'
        '"value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "provider-attempt-string-empty-default", "normalize",
        '{"request_id":"r","provider_attempt":"",'
        '"value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "provider-attempt-string-invalid", "normalize",
        '{"request_id":"r","provider_attempt":"NaN",'
        '"value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "provider-attempt-list-invalid", "normalize",
        '{"request_id":"r","provider_attempt":[],"value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "provider-attempt-object-invalid", "normalize",
        '{"request_id":"r","provider_attempt":{"n":2},'
        '"value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "provider-attempt-int-4300", "normalize",
        '{"request_id":"r","provider_attempt":' + _DIGITS_4300 +
        ',"value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "provider-attempt-int-4301-codec-reject", "normalize",
        '{"request_id":"r","provider_attempt":' + _DIGITS_4301 +
        ',"value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "provider-attempt-string-4300", "normalize",
        '{"request_id":"r","provider_attempt":"' + _DIGITS_4300 +
        '","value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "provider-attempt-string-4301-limit", "normalize",
        '{"request_id":"r","provider_attempt":"' + _DIGITS_4301 +
        '","value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "provider-attempt-finite-float-large", "normalize",
        '{"request_id":"r","provider_attempt":1e308,'
        '"value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "provider-attempt-nonfinite-json", "normalize",
        '{"request_id":"r","provider_attempt":Infinity,'
        '"value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "events-wrapper-object", "normalize",
        '{"request_id":"r","value":{"events":[{"type":"finish"}]}}',
    ),
    _case(
        "stream-normalizer", "event-types-text-delta", "normalize",
        '{"request_id":"r","value":[{"type":"text_delta",'
        '"delta":{"z":1,"a":true},"tool_intent":{"q":"é"},'
        '"usage":{"output_tokens":2,"input_tokens":1},'
        '"finish_reason":"ignored","error_code":"ignored",'
        '"sequence":99,"request_id":"ignored","extra":"discard"},'
        '{"type":"finish","finish_reason":"stop"}]}',
    ),
    _case(
        "stream-normalizer", "event-types-thinking-delta", "normalize",
        '{"request_id":"r","value":[{"type":"thinking_delta"},'
        '{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "event-types-tool-intent-delta", "normalize",
        '{"request_id":"r","value":[{"type":"tool_intent_delta",'
        '"tool_intent":{"b":1,"a":2}},{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "event-types-usage", "normalize",
        '{"request_id":"r","value":[{"type":"usage",'
        '"usage":{"z":[true,null],"a":"é"}},{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "event-types-finish-terminal", "normalize",
        '{"request_id":"r","value":[{"type":"finish",'
        '"finish_reason":"stop","error_code":"data"}]}',
    ),
    _case(
        "stream-normalizer", "event-types-error-terminal-is-data", "normalize",
        '{"request_id":"r","value":[{"type":"error",'
        '"error_code":"provider_failed","finish_reason":"error"}]}',
    ),
    _case(
        "stream-normalizer", "unknown-event-type", "normalize",
        '{"request_id":"r","value":[{"type":"unknown"}]}',
    ),
    _case(
        "stream-normalizer", "event-type-pystr-bool-invalid", "normalize",
        '{"request_id":"r","value":[{"type":true}]}',
    ),
    _case(
        "stream-normalizer", "event-not-object", "normalize",
        '{"request_id":"r","value":["not-an-object"]}',
    ),
    _case(
        "stream-normalizer", "empty-stream", "normalize",
        '{"request_id":"r","value":[]}',
    ),
    _case(
        "stream-normalizer", "missing-terminal", "normalize",
        '{"request_id":"r","value":[{"type":"text_delta"}]}',
    ),
    _case(
        "stream-normalizer", "event-after-finish", "normalize",
        '{"request_id":"r","value":[{"type":"finish"},'
        '{"type":"text_delta"}]}',
    ),
    _case(
        "stream-normalizer", "event-after-error", "normalize",
        '{"request_id":"r","value":[{"type":"error"},'
        '{"type":"usage"}]}',
    ),
    _case(
        "stream-normalizer", "events-mapping-rejected", "normalize",
        '{"request_id":"r","value":{"events":{"0":{"type":"finish"}}}}',
    ),
    _case(
        "stream-normalizer", "events-string-rejected", "normalize",
        '{"request_id":"r","value":{"events":"finish"}}',
    ),
    _case(
        "stream-normalizer", "request-id-missing", "normalize",
        '{"value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "request-id-whitespace-empty", "normalize",
        '{"request_id":"\u2003\u00a0","value":[{"type":"finish"}]}',
    ),
    _case(
        "stream-normalizer", "codec-depth-below-native-limit", "normalize",
        _nested_payload(900),
    ),
    _case(
        "stream-normalizer", "codec-depth-native-recursion-edge", "normalize",
        _nested_payload(1100),
    ),
    _case(
        "stream-normalizer", "codec-node-limit", "normalize", _node_limit_payload(),
    ),
    _case(
        "stream-normalizer", "codec-byte-limit", "normalize",
        '{"request_id":"' + "x" * _V2_MAX_BYTES + '"}',
    ),
    _case(
        "stream-normalizer", "output-byte-limit", "normalize",
        _stream_output_byte_limit_payload(),
    ),
    _case(
        "stream-normalizer", "output-node-limit", "normalize",
        _stream_output_node_limit_payload(),
    ),
)


CASES = _USAGE_CASES + _STREAM_CASES


SCALAR_VECTORS = (
    ScalarVector("json-int-negative-zero", "-0"),
    ScalarVector("json-float-negative-zero", "-0.0"),
    ScalarVector("json-underflow-positive", "1e-400"),
    ScalarVector("json-underflow-negative", "-1e-400"),
    ScalarVector("json-max-finite-float", "1.7976931348623157e308"),
    ScalarVector("json-huge-int-4300", "9" * 4300),
    ScalarVector("string-huge-int-4300", '"' + "9" * 4300 + '"'),
    ScalarVector("string-huge-int-4301", '"' + "9" * 4301 + '"'),
    ScalarVector("string-numeric-underscores", '"1_2.5"'),
    ScalarVector("string-numeric-invalid-underscore", '"1__2"'),
    ScalarVector("string-float-infinity", '"Infinity"'),
    ScalarVector("string-float-nan", '"NaN"'),
    ScalarVector("string-float-overflow", '"' + "9" * 4300 + '"'),
    ScalarVector("string-unicode-decimals", '"\u0661\u0662\u0663.5"'),
    ScalarVector("string-unicode-whitespace", '"\u200312.5\u00a0"'),
    ScalarVector("string-control-separator-u001c", '"\\u001c"'),
    ScalarVector("string-unassigned-u1c89", '"\u1c89"'),
    ScalarVector("string-line-separator-u2028", '"\u2028"'),
    ScalarVector("string-zero-width-joiner-u200d", '"\u200d"'),
    ScalarVector(
        "string-quotes-control-nonascii",
        '"quote\\\" slash\\\\ newline\\n control\\u0001 café 😀"',
    ),
    ScalarVector(
        "ordered-nested-value",
        '{"z":[true,null],"a":{"x":"é","y":-0.0}}',
    ),
    ScalarVector("json-boolean-true", "true"),
    ScalarVector("json-null", "null"),
    ScalarVector("json-list-mixed", '[false,0,"1"]'),
)
