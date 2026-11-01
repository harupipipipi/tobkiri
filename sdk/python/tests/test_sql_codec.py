"""Frozen typed SQL transport checks, independent of the pending SQL engine."""

import json
import math
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tobkiri_sdk.sql_codec import decode_cell, encode_cell


class SqlCodecTests(unittest.TestCase):
    def test_tags_distinguish_number_text_and_record_looking_text(self):
        cases = [None, -(2**63), 2**63-1, 1.0, -0.0, "1", "a\0b",
                 '{"type":"integer","value":"1"}', b"\0\xff"]
        for value in cases:
            actual = decode_cell(encode_cell(value))
            self.assertIs(type(actual), type(value))
            self.assertEqual(actual, value)
        self.assertEqual(math.copysign(1, decode_cell(encode_cell(-0.0))), -1)

    def test_exact_null_integer_real_and_blob_spellings(self):
        self.assertEqual(encode_cell(None), '{"type":"null","value":null}')
        self.assertEqual(encode_cell(1), '{"type":"integer","value":"1"}')
        self.assertEqual(encode_cell(-0.0), '{"type":"real","value":"-0e0"}')
        self.assertEqual(encode_cell(b"\0\xff"), '{"type":"blob","value":[0,255]}')

    def test_rejects_unapproved_parameter_types_and_nonfinite_values(self):
        for value in [True, {}, [], bytearray(b"a"), memoryview(b"a"), 2**63,
                      -(2**63)-1, math.inf, math.nan, "\ud800"]:
            with self.assertRaises((TypeError, ValueError)):
                encode_cell(value)

    def test_rejects_malformed_noncanonical_or_ambiguous_cells(self):
        cells = ['null', '{}', '{"type":"null"}',
                 '{"type":"null","value":0}',
                 '{"type":"null","value":null,"extra":1}',
                 '{"type":"integer","value":1}',
                 '{"type":"integer","value":"01"}',
                 '{"type":"integer","value":"-0"}',
                 '{"type":"integer","value":"9223372036854775808"}',
                 '{"type":"real","value":"1.0"}',
                 '{"type":"real","value":"NaN"}',
                 '{"type":"real","value":1}',
                 '{"type":"text","value":[]}',
                 '{"type":"text","value":"\\ud800"}',
                 '{"type":"blob","value":[true]}',
                 '{"type":"blob","value":[256]}',
                 '{"type":"blob","value":"AP8"}',
                 '{"type":"null","type":"text","value":null}']
        for cell in cells:
            with self.assertRaises(ValueError, msg=cell):
                decode_cell(cell)

    def test_real_cells_match_frozen_rust_float_vectors(self):
        vectors = json.loads((Path(__file__).resolve().parents[2]
                              / "js/tests/float-golden.json").read_text())
        for vector in vectors:
            if vector["width"] != "f64":
                continue
            value = struct.unpack("!d", bytes.fromhex(vector["bits"]))[0]
            cell = {"type": "real", "value": vector["expect"]}
            self.assertEqual(json.loads(encode_cell(value)), cell)
            decoded = decode_cell(json.dumps(cell))
            self.assertEqual(struct.pack("!d", decoded), struct.pack("!d", value))


if __name__ == "__main__":
    unittest.main()
