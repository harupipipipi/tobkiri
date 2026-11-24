"""Compare complete genuine C observations, keeping timings outside parity."""

from __future__ import annotations

import json

CASE_NAMES = (
    "errno-zero",
    "errno-e2big",
    "errno-eintr",
    "errno-ebadf",
    "errno-enotsup",
    "sentinel-zero-fds",
    "finite-positive-timeout",
    "negative-fd",
    "invalid-fd",
    "mixed-invalid-fds",
    "priority-only-validation",
)
SCOPE = "CI-only libc ABI oracle; no product or GUI parity"
OUTPUT_LIMIT = 4 * 1024 * 1024
# The SDK's alternate __header_poll.h, not the musl/Linux compatibility branch:
# wasi-libc 161b3195fc2558d2b1ba3eb9ffae3b2b47407623,
# libc-bottom-half/headers/public/__header_poll.h (Git blob f62196a1eebc459e7dab98b58829c3d734eaa2ec).
FD_INPUTS = (
    (),
    (),
    (),
    (),
    (),
    (),
    (),
    ((-1, 1),),
    ((2**31 - 1, 1),),
    ((-1, 1), (2**31 - 1, 2)),
    ((-1, 512),),
)


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate C observation member")
        result[key] = value
    return result


def decode(raw: str) -> dict:
    """Require all eleven recorded C cases and genuine, separately kept timing."""
    if type(raw) is not str or not 0 < len(raw.encode("utf-8")) <= OUTPUT_LIMIT:
        raise ValueError("C oracle output exceeds its existing DATA bound")
    result = json.loads(raw, object_pairs_hook=_unique_object)
    if (
        type(result) is not dict
        or set(result)
        != {
            "target",
            "behavior",
            "timing",
            "scope",
            "negative_timeout",
            "asynchronous_cancellation",
            "production_enabled",
        }
        or result["scope"] != SCOPE
        or result["negative_timeout"]
        != "not executed; excluded by exact source condition"
        or result["asynchronous_cancellation"]
        != "unsupported target; not a runtime pass"
        or result["production_enabled"] is not False
    ):
        raise ValueError("C oracle scope or complete result shape differs")
    target = result["target"]
    if (
        type(target) is not dict
        or set(target) != {"python", "pointer_bits", "extension_file"}
        or type(target["python"]) is not list
        or any(type(value) is not int for value in target["python"])
        or target["python"] != [3, 13, 16]
        or type(target["pointer_bits"]) is not int
        or target["pointer_bits"] != 32
        or type(target["extension_file"]) is not str
        or not target["extension_file"].endswith("_tobkiri_poll_oracle.abi3.so")
    ):
        raise ValueError("actual C target Python, pointer or extension differs")
    behavior, timings = result["behavior"], result["timing"]
    if type(behavior) is not dict or set(behavior) != {
        "abi",
        "cases",
        "synchronous_signal",
    }:
        raise ValueError("complete C ABI behavior required")
    abi = behavior["abi"]
    if (
        type(abi) is not dict
        or set(abi)
        != {
            "pointer_bytes",
            "nfds_t_bytes",
            "pollfd_bytes",
            "fd_offset",
            "events_offset",
            "revents_offset",
            "int_bytes",
            "short_bytes",
            "sig_atomic_t_bytes",
        }
        or any(type(value) is not int or not 0 <= value <= 64 for value in abi.values())
        or abi["pointer_bytes"] != 4
        or any(
            abi[name] == 0
            for name in (
                "nfds_t_bytes",
                "pollfd_bytes",
                "int_bytes",
                "short_bytes",
                "sig_atomic_t_bytes",
            )
        )
    ):
        raise ValueError("complete actual C layout observations required")
    cases = behavior["cases"]
    if type(cases) is not list or len(cases) != len(CASE_NAMES):
        raise ValueError("all eleven finite C cases required")
    for index, (name, row) in enumerate(zip(CASE_NAMES, cases)):
        if (
            type(row) is not dict
            or set(row)
            != {
                "case",
                "nfds",
                "timeout_ms",
                "incoming_errno",
                "return_value",
                "outgoing_errno",
                "entries",
                "zero_nfds_buffer_unchanged",
            }
            or row["case"] != name
            or any(
                type(row[field]) is not int
                for field in (
                    "nfds",
                    "timeout_ms",
                    "incoming_errno",
                    "return_value",
                    "outgoing_errno",
                )
            )
            or row["nfds"] != (0 if index < 7 else 2 if index == 9 else 1)
            or row["timeout_ms"] != (1 if index == 6 else 0)
            or type(row["entries"]) is not list
            or len(row["entries"]) != row["nfds"]
            or (index == 5 and row["zero_nfds_buffer_unchanged"] is not True)
            or (index != 5 and row["zero_nfds_buffer_unchanged"] is not None)
        ):
            raise ValueError("C case input, buffer or result observation differs")
        for entry in row["entries"]:
            if (
                type(entry) is not dict
                or set(entry) != {"fd", "events", "revents"}
                or any(type(value) is not int for value in entry.values())
            ):
                raise ValueError("complete actual pollfd fields required")
        if (
            tuple((entry["fd"], entry["events"]) for entry in row["entries"])
            != FD_INPUTS[index]
        ):
            raise ValueError("original finite case FD/event inputs differ")
    incoming = [row["incoming_errno"] for row in cases[:5]]
    if incoming[0] != 0 or len(set(incoming)) != 5 or any(n <= 0 for n in incoming[1:]):
        raise ValueError("five distinct incoming errno values required")
    if [row["incoming_errno"] for row in cases[5:]] != [
        incoming[1],
        incoming[2],
        incoming[1],
        incoming[2],
        incoming[3],
        incoming[1],
    ]:
        raise ValueError("original finite case errno inputs differ")
    signal = behavior["synchronous_signal"]
    if (
        type(signal) is not dict
        or set(signal)
        != {
            "kind",
            "previous_was_default",
            "raise_return",
            "handler_calls",
            "incoming_errno",
            "poll_return",
            "poll_errno",
            "restore_succeeded",
        }
        or signal["kind"] != "synchronous-userspace-raise"
        or any(type(value) is not int for key, value in signal.items() if key != "kind")
        or signal["raise_return"] != 0
        or signal["handler_calls"] != 1
        or signal["restore_succeeded"] != 1
        or signal["incoming_errno"] != incoming[1]
    ):
        raise ValueError("supported synchronous signal observation incomplete")
    if type(timings) is not list or len(timings) != len(CASE_NAMES):
        raise ValueError("separate raw timing for every C case required")
    for name, row in zip(CASE_NAMES, timings):
        if (
            type(row) is not dict
            or set(row) != {"case", "before_ns", "after_ns", "elapsed_ns"}
            or row["case"] != name
            or any(
                type(row[field]) is not int or not 0 <= row[field] < 2**64
                for field in ("before_ns", "after_ns", "elapsed_ns")
            )
            or row["after_ns"] < row["before_ns"]
            or row["elapsed_ns"] != row["after_ns"] - row["before_ns"]
        ):
            raise ValueError("raw real-clock timing is malformed")
    return result


def compare(original: dict, candidate: dict) -> dict:
    """Compare every deterministic original value; never predict errno outputs."""
    # Validation also applies when called on saved JSON rather than decode output.
    a = decode(json.dumps(original, allow_nan=False))
    b = decode(json.dumps(candidate, allow_nan=False))
    if {k: v for k, v in a.items() if k != "timing"} != {
        k: v for k, v in b.items() if k != "timing"
    }:
        raise ValueError("actual original/candidate C observations differ")
    return {
        "raw_c_observations_match": True,
        "finite_cases": len(CASE_NAMES),
        "synchronous_signal_matched": True,
        "raw_timing_compared_for_equality": False,
        "negative_timeout_executed": False,
        "asynchronous_cancellation_verified": False,
        "final_provider_resolution_verified": False,
        "c_abi_verified": False,
        "cold_schema_verified": False,
        "production_enabled": False,
    }
