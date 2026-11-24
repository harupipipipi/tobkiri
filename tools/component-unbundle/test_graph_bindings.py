"""Source-edge semantics only; synthetic fixtures do not execute Wasm."""

from copy import deepcopy
import unittest

from graph_bindings import SourceBindings


class SourceBindingTests(unittest.TestCase):
    def setUp(self):
        self.modules = [
            {
                "id": 0,
                "imports": [],
                "exports": [["poll", "Func", "0"], ["errno", "Global", "0"]],
                "bodies": [{"index": 0, "offset": 30, "bytes": 4}],
            },
            {
                "id": 1,
                "imports": [
                    ["env", "poll", "Func", "0"],
                    ["got", "errno", "Global", "0"],
                ],
                "exports": [["call", "Func", "0"]],
                "bodies": [],
            },
        ]
        self.rows = [
            ["module", "0", "0", "0", "10", "30"],
            ["module", "0", "1", "1", "50", "30"],
            ["global", "0", "0", "mutable-i32", "I32Const { value: 100 }; End"],
            ["instantiate", "0", "0", "0"],
            ["core-alias", "0", "Func", "0", "0", "poll"],
            ["core-alias", "0", "Global", "0", "0", "errno"],
            ["instance-exports", "0", "1"],
            ["instance-export", "0", "1", "poll", "Func", "0"],
            ["instance-export", "0", "1", "errno", "Global", "0"],
            ["instantiate", "0", "2", "1"],
            ["argument", "0", "2", "env", "Instance", "1"],
            ["argument", "0", "2", "got", "Instance", "1"],
        ]

    def test_direct_import_alias_export_chain_keeps_kind_and_instance(self):
        bindings = SourceBindings(self.rows, self.modules)
        result = bindings.resolve(0, 2, "Func", "call")
        self.assertEqual(result["status"], "source-edge-resolved")
        self.assertEqual(result["endpoint"]["instance"], 0)
        self.assertEqual(result["endpoint"]["module"], 0)
        self.assertEqual(result["endpoint"]["kind"], "Func")
        self.assertFalse(result["runtime_provider_verified"])
        self.assertEqual(len(bindings.import_queries()), 2)

    def test_same_module_and_numeric_global_value_do_not_merge_instances(self):
        rows = self.rows + [["instantiate", "0", "3", "0"]]
        bindings = SourceBindings(rows, self.modules)
        a = bindings.resolve(0, 0, "Global", "errno")["endpoint"]
        b = bindings.resolve(0, 3, "Global", "errno")["endpoint"]
        self.assertEqual(a["definition"], b["definition"])
        self.assertNotEqual(a, b)

    def test_component_export_alias_and_nested_outer_alias_preserve_indexes(self):
        rows = self.rows + [
            ["component-export", "0", "alias", "Module", "2", "0"],
            ["instantiate", "0", "3", "2"],
            ["component", "0", "0", "1", "100", "10"],
            ["outer-alias", "1", "CoreModule", "0", "1", "2"],
            ["instantiate", "1", "0", "0"],
        ]
        bindings = SourceBindings(rows, self.modules)
        a = bindings.resolve(0, 3, "Func", "poll")["endpoint"]
        b = bindings.resolve(1, 0, "Func", "poll")["endpoint"]
        self.assertEqual(a["module"], b["module"])
        self.assertNotEqual(a["scope"], b["scope"])

    def test_cycles_and_missing_component_derived_modules_remain_unresolved(self):
        rows = self.rows + [["instantiate", "0", "3", "99"]]
        bindings = SourceBindings(rows, self.modules)
        self.assertEqual(bindings.resolve(0, 3, "Func", "poll")["status"], "unresolved")
        rows = deepcopy(self.rows)
        rows[4][4] = "1"
        bindings = SourceBindings(rows, self.modules)
        result = bindings.resolve(0, 2, "Func", "call")
        self.assertEqual(result["status"], "unresolved")
        self.assertIn("cycle", result["reason"])

    def test_duplicate_namespace_and_mismatched_body_rows_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            SourceBindings(self.rows + [self.rows[0]], self.modules)
        with self.assertRaisesRegex(ValueError, "body"):
            SourceBindings(self.rows + [["body", "0", "0", "31", "4"]], self.modules)

    def test_canonical_core_function_is_not_labeled_as_known_runtime_code(self):
        rows = self.rows + [
            ["canonical-core-function", "0", "1", "Lower { func_index: 4 }"],
            ["instance-exports", "0", "3"],
            ["instance-export", "0", "3", "host", "Func", "1"],
        ]
        result = SourceBindings(rows, self.modules).resolve(0, 3, "Func", "host")
        self.assertFalse(result["endpoint"]["semantics_resolved"])
        self.assertFalse(result["runtime_provider_verified"])


if __name__ == "__main__":
    unittest.main()
