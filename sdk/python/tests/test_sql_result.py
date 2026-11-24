"""Named DB-API metadata semantics; no native SQL execution substitutes a guest."""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tobkiri_sdk.sql_codec import encode_cell
from tobkiri_sdk.sql_result import decode_result, parameters


def result(columns, rows, affected_rows=None, last_insert_rowid=None):
    return SimpleNamespace(columns=columns, rows=rows, affected_rows=affected_rows,
                           last_insert_rowid=last_insert_rowid)


class SqlResultTests(unittest.TestCase):
    def test_zero_rows_keeps_duplicate_names_and_seven_field_description(self):
        value = decode_result(result(["same", "same"], []), 10)
        self.assertEqual(value.description,
                         (("same", None, None, None, None, None, None),)*2)
        self.assertEqual(value.rows, ())
        self.assertEqual(value.rowcount, -1)

    def test_rows_preserve_types_order_and_connection_lastrowid_snapshot(self):
        value = decode_result(result(["blob", "text", "number"],
                                     [[encode_cell(b"\xff"), encode_cell("1"),
                                       encode_cell(1)]], "1", "-5"), 10)
        self.assertEqual(value.rows, ((b"\xff", "1", 1),))
        self.assertEqual(value.rowcount, 1)
        self.assertEqual(value.last_insert_rowid, -5)

    def test_never_truncates_rows_or_silently_accepts_wrong_metadata(self):
        for value, limit in [(result(["x"], [[encode_cell(1)]]), 0),
                             (result(["x"], [[]]), 1),
                             (result([], [] , "-1"), 1),
                             (result([1], []), 1),
                             (result(["\ud800"], []), 1),
                             (result([], [], None, 1), 1)]:
            with self.assertRaises(ValueError):
                decode_result(value, limit)

    def test_positional_parameters_reject_records_and_converters(self):
        self.assertEqual(parameters([None, 1, 1.0, "x", b"a"]),
                         [encode_cell(v) for v in [None, 1, 1.0, "x", b"a"]])
        for value in [{"x": 1}, "abc", b"x", iter([1]), [object()]]:
            with self.assertRaises(TypeError):
                parameters(value)


if __name__ == "__main__":
    unittest.main()
