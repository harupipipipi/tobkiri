"""Run using python -B -m unittest -v; no dependencies or repository writes."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import random
import struct
import unittest

from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.errors import CanonicalizationError

from tobkiri_protocol.data_codec import (
    CodecError, DOMAIN, Limits, MAX_SAFE_INTEGER, RECORD_MARKER, VERSION,
    clone_payload, decode_payload, decode_record, digest_payload, digest_record,
    encode_payload, encode_record,
)

KEY = b"prototype-only-test-seal-key-32byt"
PATHS = (("request", "input"), ("outcome",))


def packed(value: float) -> bytes:
    return struct.pack(">d", value)


class PayloadTests(unittest.TestCase):
    def test_legacy_bytes_and_digests_match_repository_exactly(self) -> None:
        values = [None, True, False, 0, 1, -1, MAX_SAFE_INTEGER,
                  -MAX_SAFE_INTEGER, "", "雪\n\x00😀", [], {},
                  {"z": [None, True, False, 5], "a": {"quoted": '"\\'}},
                  {RECORD_MARKER: {"version": VERSION, "paths": []}},
                  ["f", "8000000000000000"]]
        for value in values:
            with self.subTest(value=value):
                encoded = encode_payload(value)
                self.assertIsNone(encoded.encoding)
                self.assertEqual(encoded.payload, canonical_json(value))
                self.assertEqual(digest_payload(value), canonical_digest(value))
                self.assertEqual(decode_payload(encoded.payload), value)

    def test_finite_float_edges_round_trip_exactly(self) -> None:
        values = [0.0, -0.0, 1.0, -1.0, 0.1, 1.5, 5e-324,
                  -5e-324, float.fromhex("0x1.fffffffffffffp1023"),
                  float.fromhex("0x1.0000000000000p-1022"), 1e100]
        for value in values:
            with self.subTest(value=value):
                encoded = encode_payload(value)
                restored = decode_payload(encoded.payload, encoding=encoded.encoding,
                                          authenticated_record=True)
                self.assertEqual(packed(restored), packed(value))
                self.assertEqual(encoded.encoding, VERSION)
                # The encoded body remains acceptable to unchanged canonical.py.
                self.assertEqual(canonical_json(strict_loads(encoded.payload)),
                                 encoded.payload)

    def test_random_finite_ieee754_values(self) -> None:
        generator = random.Random(83481)
        for _ in range(2500):
            raw = generator.getrandbits(64).to_bytes(8, "big")
            value = struct.unpack(">d", raw)[0]
            if math.isfinite(value):
                self.assertEqual(packed(clone_payload(value)), raw)

    def test_nested_mixed_round_trip_and_independent_clone(self) -> None:
        value = {"x": [None, True, False, 7, -0.0, 0.1, {"nested": 1.5}],
                 "f": "8000000000000000", "empty": {}}
        clone = clone_payload(value)
        self.assertEqual(clone, value)
        self.assertEqual(packed(clone["x"][4]), packed(-0.0))
        clone["x"].append("changed")
        self.assertNotEqual(clone, value)

    def test_float_integer_boolean_and_signed_zero_distinct(self) -> None:
        values = [0, 0.0, -0.0, False, 1, 1.0, True, "1", [1.0]]
        self.assertEqual(len({digest_payload(value) for value in values}), len(values))

    def test_no_collision_with_arbitrary_tag_shaped_objects(self) -> None:
        values = [1.5, ["f", "3ff8000000000000"],
                  {"$float": "3ff8000000000000"},
                  {"encoding": VERSION, "value": ["f", "3ff8000000000000"]},
                  {RECORD_MARKER: {"version": VERSION, "paths": [["value"]]},
                   "value": ["f", "3ff8000000000000"]},
                  {"float": 1.5, "tag": ["f", "3ff8000000000000"]}]
        self.assertEqual(len({digest_payload(value) for value in values}), len(values))
        for value in values:
            self.assertEqual(clone_payload(value), value)

    def test_domain_separation_even_from_exact_encoded_body(self) -> None:
        value = {"ratio": 1.5}
        encoded = encode_payload(value)
        ordinary_tokens = strict_loads(encoded.payload)
        self.assertNotEqual(digest_payload(value), canonical_digest(ordinary_tokens))
        self.assertEqual(digest_payload(value), "sha256:" + hashlib.sha256(
            DOMAIN + encoded.payload).hexdigest())

    def test_default_decode_never_interprets_user_tags(self) -> None:
        raw = b'["f","8000000000000000"]'
        self.assertEqual(decode_payload(raw), ["f", "8000000000000000"])
        with self.assertRaises(CodecError):
            decode_payload(raw, encoding=VERSION)
        with self.assertRaises(CodecError):
            decode_payload(raw, encoding=VERSION, authenticated_record=1)

    def test_unknown_versions_fail_closed(self) -> None:
        for version in ["tobkiri.flow-data.ieee754.v2", "", 1, True, {}, []]:
            with self.subTest(version=version), self.assertRaises(CodecError):
                decode_payload(b'["f","0000000000000000"]', encoding=version,
                               authenticated_record=True)

    def test_nan_and_infinity_rejected_on_both_sides(self) -> None:
        for value in [float("nan"), float("inf"), -float("inf")]:
            with self.assertRaises(CodecError):
                encode_payload({"number": value})
            raw = json.dumps(["f", packed(value).hex()], separators=(",", ":")).encode()
            with self.assertRaises(CodecError):
                decode_payload(raw, encoding=VERSION, authenticated_record=True)
        for raw in [b"NaN", b"Infinity", b"-Infinity", b"1.5", b"1e309"]:
            with self.assertRaises(CodecError):
                decode_payload(raw)

    def test_unsafe_integers_rejected_and_boolean_not_integer_tag(self) -> None:
        for value in [MAX_SAFE_INTEGER + 1, -MAX_SAFE_INTEGER - 1, 10**1000]:
            with self.assertRaises(CodecError):
                encode_payload(value)
            with self.assertRaises(CodecError):
                decode_payload(str(value))
        with self.assertRaises(CodecError):
            decode_payload('["a",2,"i",true,"f","0000000000000000"]',
                           encoding=VERSION, authenticated_record=True)

    def test_depth_boundary_uses_logical_not_tag_tree_nesting(self) -> None:
        value = 1.5
        for _ in range(64):
            value = [value]
        encoded = encode_payload(value)
        self.assertEqual(clone_payload(value), value)
        # The deepest legal value still uses a depth-one encoded token array.
        strict_loads(encoded.payload, max_depth=1)
        with self.assertRaises(CodecError):
            encode_payload([value])
        with self.assertRaises(CodecError):
            decode_payload(encoded.payload, encoding=VERSION,
                           authenticated_record=True, limits=Limits(max_depth=63))

    def test_cycles_and_non_json_types_rejected(self) -> None:
        cycle = []
        cycle.append(cycle)
        for value in [cycle, {"bad": b"bytes"}, {1: "bad"}, {1, 2}, (1, 2)]:
            with self.assertRaises(CodecError):
                encode_payload(value)

    def test_shared_references_are_values_not_cycles(self) -> None:
        shared = [1.5]
        self.assertEqual(clone_payload([shared, shared]), [[1.5], [1.5]])

    def test_size_and_node_limits_enforced(self) -> None:
        limits = Limits(max_bytes=32, max_nodes=8)
        for value in ["x" * 33, "\x00" * 10, [1] * 8, {"x": "y" * 32}]:
            with self.assertRaises(CodecError):
                encode_payload(value, limits=limits)
        with self.assertRaises(CodecError):
            decode_payload(b'"' + b"x" * 33 + b'"', limits=limits)
        with self.assertRaises(CodecError):
            decode_payload(b'["a",1000000]', encoding=VERSION,
                           authenticated_record=True, limits=limits)

    def test_invalid_unicode_invalid_utf8_and_duplicate_keys_rejected(self) -> None:
        for value in ["\ud800", {"\udfff": "value"}]:
            with self.assertRaises(CodecError):
                encode_payload(value)
        for raw in [b'"\xff"', b'{"x":1,"x":2}', b'"\\ud800"']:
            with self.assertRaises(CodecError):
                decode_payload(raw)

    def test_malformed_tag_streams_rejected(self) -> None:
        streams = [[], ["f"], ["f", "xyz"], ["f", "00000000000000000"],
                   ["f", "3FF8000000000000"], ["f", 1], ["z", 0],
                   ["n"], ["a", -1], ["a", True], ["a", 2, "f", "0000000000000000"],
                   ["f", "0000000000000000", "n"], ["s", []],
                   ["o", 2, "x", "n", "x", "f", "0000000000000000"],
                   ["o", 2, "z", "n", "a", "f", "0000000000000000"],
                   ["o", 1, 1, "f", "0000000000000000"]]
        for stream in streams:
            with self.subTest(stream=stream), self.assertRaises(CodecError):
                decode_payload(json.dumps(stream, separators=(",", ":")),
                               encoding=VERSION, authenticated_record=True)

    def test_noncanonical_extended_spacing_rejected(self) -> None:
        with self.assertRaises(CodecError):
            decode_payload('["f", "0000000000000000"]', encoding=VERSION,
                           authenticated_record=True)

    def test_repository_authority_canonicalizer_remains_strict(self) -> None:
        with self.assertRaises(CanonicalizationError):
            canonical_json({"security_epoch": 1.5})
        with self.assertRaises(CanonicalizationError):
            strict_loads('{"security_epoch":1.5}')


class RecordTests(unittest.TestCase):
    def record(self) -> dict:
        return {"request": {"request_id": "req-1", "input": {"fraction": -0.0}},
                "outcome": {"fraction": 1.5}, "security_epoch": 9, "state": "succeeded"}

    def test_sealed_round_trip_preserves_queryable_authority_paths(self) -> None:
        record = self.record()
        raw, seal = encode_record(record, value_paths=PATHS, seal_key=KEY)
        wire = strict_loads(raw)
        self.assertEqual(wire["request"]["request_id"], "req-1")
        self.assertEqual(wire["security_epoch"], 9)
        restored = decode_record(raw, seal, value_paths=PATHS, seal_key=KEY)
        self.assertEqual(restored, record)
        self.assertEqual(packed(restored["request"]["input"]["fraction"]), packed(-0.0))
        self.assertNotIn(RECORD_MARKER, restored)

    def test_legacy_records_bytes_hashes_and_hmac_are_unchanged(self) -> None:
        record = {"request": {"input": {"n": 3}}, "outcome": {"ok": True}}
        raw, seal = encode_record(record, value_paths=PATHS, seal_key=KEY)
        self.assertEqual(raw, canonical_json(record))
        self.assertEqual(seal, hmac.new(KEY, raw, hashlib.sha256).hexdigest())
        self.assertEqual(digest_record(record, value_paths=PATHS), canonical_digest(record))
        self.assertEqual(decode_record(raw, seal, value_paths=PATHS, seal_key=KEY), record)

    def test_record_digest_binds_float_bit_identity(self) -> None:
        record = self.record()
        first = digest_record(record, value_paths=PATHS)
        record["request"]["input"]["fraction"] = 0.0
        self.assertNotEqual(first, digest_record(record, value_paths=PATHS))

    def test_record_authority_floats_rejected(self) -> None:
        for path in [("security_epoch",), ("request", "request_id")]:
            record = self.record()
            target = record
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = 1.5
            with self.assertRaises(CodecError):
                encode_record(record, value_paths=PATHS, seal_key=KEY)
            with self.assertRaises(CodecError):
                digest_record(record, value_paths=PATHS)

    def test_tampering_or_missing_authentication_fails_before_decoding(self) -> None:
        raw, seal = encode_record(self.record(), value_paths=PATHS, seal_key=KEY)
        for changed in [raw.replace(b"req-1", b"req-2"), raw.replace(b"v1", b"v2"),
                        b"not json", raw.replace(b"8000000000000000", b"0000000000000000")]:
            with self.assertRaisesRegex(CodecError, "authentication"):
                decode_record(changed, seal, value_paths=PATHS, seal_key=KEY)

    def test_authenticated_marker_cannot_choose_unapproved_authority_field(self) -> None:
        wire = {"security_epoch": ["f", "3ff8000000000000"],
                RECORD_MARKER: {"version": VERSION, "paths": [["security_epoch"]]}}
        raw = canonical_json(wire)
        seal = hmac.new(KEY, raw, hashlib.sha256).hexdigest()
        with self.assertRaisesRegex(CodecError, "authority or unknown"):
            decode_record(raw, seal, value_paths=PATHS, seal_key=KEY)

    def test_user_tags_inside_value_are_never_detected_as_markers(self) -> None:
        record = self.record()
        user_object = {RECORD_MARKER: {"version": VERSION, "paths": [["x"]]},
                       "x": ["f", "8000000000000000"]}
        record["request"]["input"] = user_object
        raw, seal = encode_record(record, value_paths=PATHS, seal_key=KEY)
        restored = decode_record(raw, seal, value_paths=PATHS, seal_key=KEY)
        self.assertEqual(restored["request"]["input"], user_object)
        wire = strict_loads(raw)
        self.assertEqual(wire[RECORD_MARKER]["paths"], [["outcome"]])

    def test_unmarked_encoded_looking_value_stays_a_list(self) -> None:
        record = {"request": {"input": ["f", "8000000000000000"]}, "outcome": {}}
        raw = canonical_json(record)
        seal = hmac.new(KEY, raw, hashlib.sha256).hexdigest()
        self.assertEqual(decode_record(raw, seal, value_paths=PATHS, seal_key=KEY), record)

    def test_overlapping_invalid_missing_paths_rejected(self) -> None:
        for paths in [((),), (("request",), ("request", "input")),
                      (("request", "input"), ("request", "input")),
                      (("request", True),), ((RECORD_MARKER,),), (("missing",),)]:
            with self.subTest(paths=paths), self.assertRaises(CodecError):
                encode_record(self.record(), value_paths=paths, seal_key=KEY)

    def test_malformed_authenticated_record_markers_rejected(self) -> None:
        for marker in [None, VERSION, {"version": VERSION},
                       {"version": VERSION, "paths": []},
                       {"version": VERSION, "paths": ["request/input"]},
                       {"version": VERSION, "paths": [["outcome"], ["outcome"]]},
                       {"version": VERSION + "x", "paths": [["outcome"]]}]:
            wire = {"outcome": ["f", "0000000000000000"], RECORD_MARKER: marker}
            raw = canonical_json(wire)
            seal = hmac.new(KEY, raw, hashlib.sha256).hexdigest()
            with self.subTest(marker=marker), self.assertRaises(CodecError):
                decode_record(raw, seal, value_paths=PATHS, seal_key=KEY)

    def test_runtime_record_marker_cannot_be_supplied_on_encode(self) -> None:
        record = self.record()
        record[RECORD_MARKER] = {"version": VERSION, "paths": [["outcome"]]}
        with self.assertRaises(CodecError):
            encode_record(record, value_paths=PATHS, seal_key=KEY)


class AdditionalAbuseTests(unittest.TestCase):
    def test_seal_shape_rejected_consistently(self) -> None:
        raw, _ = encode_record({"outcome": 1.5}, value_paths=(("outcome",),), seal_key=KEY)
        for seal in [None, 1, True, b"x", "not-a-seal", "é" * 64]:
            with self.subTest(seal=seal), self.assertRaises(CodecError):
                decode_record(raw, seal, value_paths=(("outcome",),), seal_key=KEY)

    def test_extreme_json_depth_is_bounded_failure(self) -> None:
        raw = b"[" * 5000 + b"0" + b"]" * 5000
        with self.assertRaises(CodecError):
            decode_payload(raw)

    def test_random_structured_values_match_round_trip_and_legacy_digest(self) -> None:
        generator = random.Random(74528)
        def generate(depth: int):
            choices = [None, True, False, generator.randint(-1000, 1000),
                       "雪\n\x00" + str(generator.randint(0, 100)),
                       generator.random(), -0.0]
            if depth:
                choices += [[generate(depth - 1) for _ in range(generator.randrange(4))],
                            {str(i): generate(depth - 1)
                             for i in range(generator.randrange(4))}]
            return generator.choice(choices)
        for _ in range(500):
            value = generate(3)
            encoded = encode_payload(value)
            self.assertEqual(clone_payload(value), value)
            self.assertEqual(encode_payload(clone_payload(value)), encoded)
            if encoded.encoding is None:
                self.assertEqual(encoded.payload, canonical_json(value))
                self.assertEqual(digest_payload(value), canonical_digest(value))

    def test_array_value_path_is_supported(self) -> None:
        paths = (("steps", 0, "request", "input"),)
        record = {"steps": [{"request": {"input": {"x": 1.5}, "contract_id": "x"}}]}
        raw, seal = encode_record(record, value_paths=paths, seal_key=KEY)
        self.assertEqual(decode_record(raw, seal, value_paths=paths, seal_key=KEY), record)

    def test_record_near_node_budget_round_trips_despite_wire_expansion(self) -> None:
        paths = (("outcome",),)
        limits = Limits(max_nodes=128)
        record = {"outcome": [-0.0] * 125}
        raw, seal = encode_record(record, value_paths=paths, seal_key=KEY, limits=limits)
        self.assertEqual(decode_record(raw, seal, value_paths=paths,
                                       seal_key=KEY, limits=limits), record)
        record["outcome"].append(1.5)
        with self.assertRaises(CodecError):
            encode_record(record, value_paths=paths, seal_key=KEY, limits=limits)

    def test_full_default_node_budget_list_round_trips(self) -> None:
        paths = (("outcome",),)
        record = {"outcome": [-0.0] * 99_997}
        raw, seal = encode_record(record, value_paths=paths, seal_key=KEY)
        restored = decode_record(raw, seal, value_paths=paths, seal_key=KEY)
        self.assertEqual(restored, record)
        self.assertEqual(packed(restored["outcome"][-1]), packed(-0.0))

    def test_record_depth_budget_and_marker_overhead_round_trip(self) -> None:
        for depth in [1, 2, 4, 63, 64]:
            record = -0.0
            for _ in range(depth):
                record = {"x": record}
            paths = (("x",) * depth,)
            limits = Limits(max_depth=depth)
            with self.subTest(depth=depth):
                raw, seal = encode_record(record, value_paths=paths,
                                          seal_key=KEY, limits=limits)
                self.assertEqual(decode_record(raw, seal, value_paths=paths,
                                               seal_key=KEY, limits=limits), record)

    def test_small_budgets_never_encode_a_record_they_cannot_decode(self) -> None:
        paths = (("outcome",),)
        successful = 0
        for budget in range(3, 50):
            limits = Limits(max_nodes=budget)
            for length in range(0, 25):
                record = {"outcome": [1.5] * length}
                try:
                    raw, seal = encode_record(record, value_paths=paths,
                                              seal_key=KEY, limits=limits)
                except CodecError:
                    continue
                restored = decode_record(raw, seal, value_paths=paths,
                                         seal_key=KEY, limits=limits)
                self.assertEqual(restored, record)
                successful += 1
        self.assertGreater(successful, 500)


if __name__ == "__main__":
    unittest.main()
