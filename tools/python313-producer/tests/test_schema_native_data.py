"""Reject invalid native observation receipts without executing a guest locally."""

import copy
import importlib.util
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "schema_probe/native_driver.py"
spec = importlib.util.spec_from_file_location("native_schema_observer_contract", SOURCE)
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)


def sample():
    return {
        "format": "tobkiri.native-data-schema-observation/1",
        "component_sha256": "sha256:" + "a" * 64,
        "operation": "run",
        "memory_limit_bytes": 64 * 1024 * 1024,
        "fuel_limit": 2_000_000_000,
        "wall_seconds": 30,
        "original_deadline_preserved": True,
        "portable_owner_fixture": True,
        "actual_os_owner_proof": False,
        "production_enabled": False,
        "elapsed_seconds": 1.5,
        "genuine_unreachable": False,
        "observation": {
            "fuel_remaining": 1_000_000_000,
            "post_return": True,
            "unreachable_trap": False,
            "store_retired": True,
            "deadline_exceeded": False,
            "clock_calls": 4,
            "clock_completed": 4,
            "wall_now": 1,
            "wall_resolution": 1,
            "monotonic_now": 1,
            "monotonic_resolution": 1,
            "wall_resolution_ns": 1,
            "monotonic_resolution_ns": 1,
        },
    }


class NativeObserverContract(unittest.TestCase):
    def validate(self, record):
        return observer.validate_report(record, "sha256:" + "a" * 64, "run")

    def test_receipt_is_only_a_portable_execution_observation(self):
        record = sample()
        self.assertIs(self.validate(record), record["observation"])
        for key, value in (
            ("actual_os_owner_proof", True),
            ("production_enabled", True),
            ("portable_owner_fixture", False),
            ("component_sha256", "sha256:" + "b" * 64),
            ("operation", "another-function"),
        ):
            with self.subTest(key=key):
                changed = copy.deepcopy(record)
                changed[key] = value
                with self.assertRaises(ValueError):
                    self.validate(changed)

    def test_larger_memory_or_renewed_lifetime_cannot_pass(self):
        for key, value in (
            ("memory_limit_bytes", 128 * 1024 * 1024),
            ("fuel_limit", 4_000_000_000),
            ("wall_seconds", 60),
            ("original_deadline_preserved", False),
            ("elapsed_seconds", 30.0),
            ("elapsed_seconds", float("nan")),
        ):
            with self.subTest(key=key, value=value):
                changed = sample()
                changed[key] = value
                with self.assertRaises(ValueError):
                    self.validate(changed)

    def test_failed_callback_or_live_store_cannot_be_success(self):
        for key, value in (
            ("clock_completed", 3),
            ("clock_calls", 5),
            ("wall_now", True),
            ("wall_resolution_ns", None),
            ("store_retired", False),
            ("deadline_exceeded", True),
            ("fuel_remaining", 0),
        ):
            with self.subTest(key=key):
                changed = sample()
                changed["observation"][key] = value
                with self.assertRaises(ValueError):
                    self.validate(changed)

    def test_denial_flag_requires_the_actual_typed_unreachable(self):
        changed = sample()
        changed["genuine_unreachable"] = True
        with self.assertRaises(ValueError):
            self.validate(changed)
        changed["observation"]["unreachable_trap"] = True
        with self.assertRaises(ValueError):
            self.validate(changed)
        changed["observation"]["post_return"] = False
        self.validate(changed)


if __name__ == "__main__":
    unittest.main()
