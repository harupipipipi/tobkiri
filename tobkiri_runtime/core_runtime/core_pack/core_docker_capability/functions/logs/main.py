#!/usr/bin/env python3
"""
logs - Core function registry entry point.

This is a core function. It is executed in-process by the kernel.
This file exists to satisfy pack_validator checks.
The stdin/stdout JSON interface below returns an explicit failure for
unsupported direct invocation.
"""

import json
import sys


def main():
    """Direct entry point for core function 'logs'."""
    try:
        raw = sys.stdin.read()
        if raw.strip():
            json.loads(raw)
    except json.JSONDecodeError:
        pass

    response = {
        "success": False,
        "status": "error",
        "error_type": "invalid_dispatch",
        "message": (
            "This is a core function. "
            "It is executed in-process by the kernel. "
            "Direct invocation via stdin/stdout is not supported."
        ),
        "error": (
            "This is a core function. "
            "It is executed in-process by the kernel. "
            "Direct invocation via stdin/stdout is not supported."
        ),
        "function_id": "logs",
    }

    json.dump(response, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
