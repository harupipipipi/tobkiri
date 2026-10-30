"""Offline rejection tests for the fuel/time-only diagnostic receipt."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_schema_kernel import validate_epoch_observation  # noqa: E402

COMPONENT = "sha256:" + "a" * 64


def receipt() -> dict:
    return {
        "format": "tobkiri.schema-native-epoch-observation/1",
        "component_sha256": COMPONENT,
        "original_deadline_preserved": True,
        "qualification": False,
        "production_enabled": False,
        "observation": {
            "format": "tobkiri.native-epoch-observation/1",
            "diagnostic_only": True,
            "qualification": False,
            "captures_backtrace": False,
            "function_costs_classified": False,
            "max_records": 128,
            "seen": 1,
            "recorded": 1,
            "overflow": False,
            "fuel_missing": False,
            "fuel_increased": False,
            "deadline_changed": False,
            "poisoned": False,
            "records": [{"time_since_binding_ns": 1, "fuel_remaining": 123}],
        },
    }


class EpochReceiptTests(unittest.TestCase):
    def test_fixed_cap_is_explicit_without_fabricating_later_records(self):
        value = receipt()
        self.assertIs(validate_epoch_observation(value, COMPONENT), value)
        trace = value["observation"]
        trace.update(seen=129, recorded=128, overflow=True)
        trace["records"] = [
            {"time_since_binding_ns": index, "fuel_remaining": 200 - index}
            for index in range(128)
        ]
        validate_epoch_observation(value, COMPONENT)
        trace["records"].append(trace["records"][-1])
        with self.assertRaises(ValueError):
            validate_epoch_observation(value, COMPONENT)

    def test_diagnostic_cannot_be_promoted_or_rebound(self):
        for key, changed in (
            ("component_sha256", "sha256:" + "b" * 64),
            ("original_deadline_preserved", False),
            ("qualification", True), ("production_enabled", True),
        ):
            with self.subTest(key=key):
                value = receipt()
                value[key] = changed
                with self.assertRaises(ValueError):
                    validate_epoch_observation(value, COMPONENT)
        for key in ("captures_backtrace", "function_costs_classified", "qualification",
                    "fuel_missing", "fuel_increased", "deadline_changed", "poisoned"):
            with self.subTest(key=key):
                value = receipt()
                value["observation"][key] = True
                with self.assertRaises(ValueError):
                    validate_epoch_observation(value, COMPONENT)

    def test_nonmonotonic_or_missing_actual_fuel_is_refused(self):
        for row in (
            {"time_since_binding_ns": 2, "fuel_remaining": 124},
            {"time_since_binding_ns": 0, "fuel_remaining": 122},
            {"time_since_binding_ns": 2, "fuel_remaining": None},
            {"time_since_binding_ns": 2, "fuel_remaining": True},
        ):
            with self.subTest(row=row):
                value = receipt()
                value["observation"].update(seen=2, recorded=2)
                value["observation"]["records"].append(row)
                with self.assertRaises(ValueError):
                    validate_epoch_observation(value, COMPONENT)

    def test_empty_unknown_or_inconsistent_counts_are_refused(self):
        for changes in (
            {"seen": 0, "recorded": 0, "records": []},
            {"seen": True}, {"recorded": 129}, {"max_records": 129},
            {"seen": 129}, {"overflow": True}, {"extra": True},
        ):
            with self.subTest(changes=changes):
                value = receipt()
                value["observation"].update(changes)
                with self.assertRaises(ValueError):
                    validate_epoch_observation(value, COMPONENT)


if __name__ == "__main__":
    unittest.main()
