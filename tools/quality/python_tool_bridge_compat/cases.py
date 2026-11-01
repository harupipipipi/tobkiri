"""Finite input cases for the independent Tool Bridge v2 source oracle.

The archived v1 oracle contributes only its frozen operation and input text.
Its recorded v1 outputs are deliberately never used as v2 expectations.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
from typing import Any


PROVIDER = "tool-bridge"
PREFIX = "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize"
LEGACY_OPERATION_GROUPS = {
    "base": ("base", PREFIX),
    ".generate": ("generate", PREFIX + ".generate"),
    ".stream": ("stream", PREFIX + ".stream"),
}
LEGACY_GROUP_CASE_COUNTS = {"base": 11, ".generate": 11, ".stream": 11}
FROZEN_CASE_COUNT = 33
FROZEN_ORACLE_SHA256 = (
    "9aae37387a6b0758451abe3cb6070d8a3d8106b744de053a5bc16e8715f3481d"
)
FROZEN_CASE_INPUTS_SHA256 = (
    "39bf861b89825925890e3d1bae5fa99bdd9992323a7d25b19e49f57859366796"
)


@dataclass(frozen=True)
class Case:
    """One exact v2 operation and raw JSON payload passed to the adapter."""

    provider: str
    case_id: str
    operation: str
    payload: str


def _sha256(data: bytes) -> str:
    """Return the lowercase SHA-256 digest of exact source bytes."""
    return hashlib.sha256(data).hexdigest()


def _case(case_id: str, payload: str, operation: str = PREFIX) -> Case:
    """Create one hand-authored v2 probe without an expected result."""
    return Case(PROVIDER, case_id, operation, payload)


def _json(value: Any) -> str:
    """Encode a test request as ASCII JSON while preserving object order."""
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, separators=(",", ":"),
    )


def _intent_payload(
    intents: list[Any], request_id: Any = "request", *, include_request_id: bool = True,
) -> str:
    """Build a normalizer-shaped request for the captured adapter."""
    request: dict[str, Any] = {}
    if include_request_id:
        request["request_id"] = request_id
    request["intents"] = intents
    return _json(request)


def _one_intent_payload(arguments_json: str, **intent_fields: Any) -> str:
    """Put raw inner argument JSON in the original opaque-string field."""
    intent = {"name": "echo", **intent_fields, "arguments": arguments_json}
    return _intent_payload([intent])


def frozen_input_cases(
    oracle_bytes: bytes,
    binding_bytes: bytes,
    provenance_bytes: bytes,
    freeze_script_bytes: bytes,
) -> tuple[Case, ...]:
    """Use all 33 frozen v1 inputs, validating only their input provenance.

    The old oracle's ``original`` and ``transport`` observations are ignored.
    Fresh v2 expectations are produced by the captured v2 adapter.
    """
    oracle = json.loads(oracle_bytes.decode("utf-8"))
    binding = json.loads(binding_bytes.decode("utf-8"))
    provenance_digest = _sha256(provenance_bytes)
    freeze_script_digest = _sha256(freeze_script_bytes)

    if not isinstance(oracle, dict) or not isinstance(binding, dict):
        raise ValueError("frozen Tool Bridge input metadata must be objects")
    if oracle.get("format") != "tobkiri.ordered-python-tool-bridge-oracle/1":
        raise ValueError("unexpected frozen Tool Bridge oracle format")
    if oracle.get("protocol") != "ordered-finite-json-v1":
        raise ValueError("frozen source input set is not the v1 corpus")
    if _sha256(oracle_bytes) != FROZEN_ORACLE_SHA256:
        raise ValueError("frozen Tool Bridge oracle bytes changed")
    if binding.get("oracle_sha256") != FROZEN_ORACLE_SHA256:
        raise ValueError("frozen binding does not identify the pinned oracle")
    if binding.get("source_provenance_sha256") != provenance_digest:
        raise ValueError("frozen binding does not identify current provenance")
    if oracle.get("source_provenance_sha256") != provenance_digest:
        raise ValueError("frozen oracle does not identify current provenance")
    if binding.get("freeze_script_sha256") != freeze_script_digest:
        raise ValueError("frozen binding does not identify the current freeze script")
    if oracle.get("freeze_script_sha256") != freeze_script_digest:
        raise ValueError("frozen oracle does not identify the current freeze script")

    observations = oracle.get("cases")
    if not isinstance(observations, list) or len(observations) != FROZEN_CASE_COUNT:
        raise ValueError("frozen Tool Bridge input case count changed")
    inputs: list[dict[str, str]] = []
    for record in observations:
        if not isinstance(record, dict):
            raise ValueError("frozen Tool Bridge input record is malformed")
        case_id = record.get("id")
        operation = record.get("operation")
        payload = record.get("input_json")
        if not all(type(value) is str for value in (case_id, operation, payload)):
            raise ValueError("frozen Tool Bridge input fields must be strings")
        inputs.append({
            "id": case_id,
            "operation": operation,
            "input_json": payload,
        })
    encoded_inputs = json.dumps(
        inputs, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("ascii")
    if _sha256(encoded_inputs) != FROZEN_CASE_INPUTS_SHA256:
        raise ValueError("frozen Tool Bridge case inputs changed")
    if binding.get("case_inputs_sha256") != FROZEN_CASE_INPUTS_SHA256:
        raise ValueError("frozen binding does not identify the pinned case inputs")
    if binding.get("case_count") != FROZEN_CASE_COUNT:
        raise ValueError("frozen binding case count changed")

    cases: list[Case] = []
    group_counts = {group: 0 for group in LEGACY_GROUP_CASE_COUNTS}
    for record in inputs:
        legacy_id = record["id"]
        if ":" not in legacy_id:
            raise ValueError("frozen Tool Bridge case id has no operation group")
        legacy_group, suffix = legacy_id.split(":", 1)
        group = LEGACY_OPERATION_GROUPS.get(legacy_group)
        if group is None:
            raise ValueError("frozen Tool Bridge case id has an unknown group")
        normalized_group, expected_operation = group
        if record["operation"] != expected_operation:
            raise ValueError("frozen Tool Bridge id group disagrees with operation")
        group_counts[legacy_group] += 1
        case_id = f"frozen-v1-{normalized_group}-{suffix}"
        cases.append(Case(PROVIDER, case_id, record["operation"], record["input_json"]))
    if group_counts != LEGACY_GROUP_CASE_COUNTS:
        raise ValueError("frozen Tool Bridge operation-group inventory changed")
    return tuple(cases)


def authored_cases() -> tuple[Case, ...]:
    """Return bounded edge inputs for the unchanged Tool Bridge v2 sources."""
    digits_4300 = "9" * 4300
    digits_4301 = "9" * 4301
    duplicate_buffer = io.StringIO()
    duplicate_buffer.write('{"dup":0')
    for value in range(1, 100_001):
        duplicate_buffer.write(',"dup":')
        duplicate_buffer.write(str(value))
    duplicate_buffer.write("}")

    cases = [
        _case("generate-alias", _intent_payload([]), PREFIX + ".generate"),
        _case("stream-alias", _intent_payload([]), PREFIX + ".stream"),
        _case(
            "opaque-escaped-duplicate-key",
            _one_intent_payload('{"a":1,"\\u0061":2,"b":3}'),
        ),
        _case(
            "outer-escaped-duplicate-key",
            '{"intents":[],"\\u0069ntents":[{"name":"echo"}]}',
        ),
        _case(
            "opaque-negative-zero-underflow",
            _one_intent_payload('{"negative-int":-0,"negative-float":-0.0,'
                                '"underflow":1e-9999}'),
        ),
        _case("opaque-inner-int-4300", _one_intent_payload(
            '{"n":' + digits_4300 + '}')),
        _case("opaque-inner-int-4301", _one_intent_payload(
            '{"n":' + digits_4301 + '}')),
        _case(
            "outer-int-4300",
            '{"request_id":' + digits_4300 + ',"intents":[]}',
        ),
        _case(
            "outer-int-4301",
            '{"request_id":' + digits_4301 + ',"intents":[]}',
        ),
        _case("opaque-nan", _one_intent_payload('{"n":NaN}')),
        _case("opaque-positive-infinity", _one_intent_payload('{"n":Infinity}')),
        _case("opaque-negative-infinity", _one_intent_payload('{"n":-Infinity}')),
        _case("opaque-exponent-overflow", _one_intent_payload('{"n":1e9999}')),
        _case("outer-array", "[]"),
        _case("outer-null", "null"),
        _case("outer-malformed-json", '{"intents":'),
        _case("intents-nonlist-default-empty", '{"intents":"ignored"}'),
        _case("unknown-operation", "{}", "not-a-tool-bridge-operation"),
        _case("arguments-array-value", _intent_payload([
            {"name": "echo", "arguments": [1]},
        ])),
        _case("arguments-json-list", _one_intent_payload("[1]")),
        _case("arguments-json-null", _one_intent_payload("null")),
        _case("arguments-malformed-json", _one_intent_payload("{")),
        _case("function-null-fallback", _intent_payload([
            {"function": None, "name": "outer-name", "arguments": "{}"},
        ])),
        _case("function-list-fallback", _intent_payload([
            {"function": ["ignored"], "name": "outer-name", "arguments": "{}"},
        ])),
        _case("function-name-null-fallback", _intent_payload([
            {"function": {"name": None}, "name": "outer-name", "arguments": "{}"},
        ])),
        _case("function-name-missing-fallback", _intent_payload([
            {"function": {}, "name": "outer-name", "arguments": "{}"},
        ])),
        _case("function-name-empty-fallback", _intent_payload([
            {"function": {"name": ""}, "name": "outer-name", "arguments": "{}"},
        ])),
        _case("function-name-null-without-fallback", _intent_payload([
            {"function": {"name": None}, "arguments": "{}"},
        ])),
        _case("arguments-omitted-default-empty", _intent_payload([
            {"name": "echo"},
        ])),
        _case("nested-arguments-omitted-default-empty", _intent_payload([
            {"function": {"name": "echo"}},
        ])),
        _case("nested-arguments-missing-fallback", _intent_payload([
            {"function": {"name": "echo"}, "name": "outer-name",
             "arguments": '{"from":"outer"}'},
        ])),
        _case("nested-arguments-null-no-fallback", _intent_payload([
            {"function": {"name": "echo", "arguments": None},
             "arguments": "{}"},
        ])),
        _case("outer-arguments-null", _intent_payload([
            {"name": "echo", "arguments": None},
        ])),
        _case("request-id-null", _intent_payload([
            {"name": "echo", "arguments": "{}"},
        ], request_id=None)),
        _case("intent-id-null-default", _intent_payload([
            {"id": None, "name": "echo", "arguments": "{}"},
        ])),
        _case("mixed-nonmapping-before-valid", _intent_payload([
            17, {"name": "echo"},
        ])),
        _case("invalid-mapping-before-nonmapping", _intent_payload([
            {"name": " "}, 17,
        ])),
        _case("nested-truthy-identifiers-repr", _intent_payload([
            {"id": {"tag": [1, "id"]},
             "function": {"name": " echo ", "arguments": "{}"}},
        ], request_id={"\u1c89": [True, {"k": -0.0}]})),
        _case("opaque-100001-collapsible-duplicate-keys", _one_intent_payload(
            duplicate_buffer.getvalue())),
        _case("nan-before-surrogate-value", _intent_payload([
            {"name": "echo", "arguments": '{"n":NaN}'},
            {"name": "echo", "arguments": '{"s":"\\ud800"}'},
        ])),
        _case("surrogate-value-before-nan", _intent_payload([
            {"name": "echo", "arguments": '{"s":"\\ud800"}'},
            {"name": "echo", "arguments": '{"n":NaN}'},
        ])),
        _case("nan-before-surrogate-key", _intent_payload([
            {"name": "echo", "arguments": '{"n":NaN}'},
            {"name": "echo", "arguments": '{"\\ud800":1}'},
        ])),
        _case("surrogate-key-before-nan", _intent_payload([
            {"name": "echo", "arguments": '{"\\ud800":1}'},
            {"name": "echo", "arguments": '{"n":NaN}'},
        ])),
        _case("surrogate-then-malformed-json", _intent_payload([
            {"name": "echo", "arguments": '{"s":"\\ud800"}'},
            {"name": "echo", "arguments": "{"},
        ])),
        _case("nan-before-surrogate-value-in-map", _one_intent_payload(
            '{"n":NaN,"s":"\\ud800"}')),
        _case("surrogate-value-before-nan-in-map", _one_intent_payload(
            '{"s":"\\ud800","n":NaN}')),
        _case("nan-before-surrogate-key-in-map", _one_intent_payload(
            '{"n":NaN,"\\ud800":1}')),
        _case("surrogate-key-before-nan-in-map", _one_intent_payload(
            '{"\\ud800":1,"n":NaN}')),
    ]
    return tuple(cases)
