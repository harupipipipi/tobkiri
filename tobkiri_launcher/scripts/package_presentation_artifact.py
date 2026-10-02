#!/usr/bin/env python3
"""Retired direct caller for presentation packaging.

Presentation packaging is a core-owned operation.  The only production
boundary is the Rust API ``run_formal_defaults_packaging`` with the formal
boundary label ``tobkiri-core-package-defaults-v1``.  That API keeps the
verified source snapshot and lease alive, invokes the generator with its own
trusted arguments, and returns only a verified catalog; it does not return a
snapshot pathname or file descriptor to a Python caller.

This module intentionally performs no filesystem, child-process, import, or
environment discovery.  Keeping the former script name as a hard failure
gives stale local commands a deterministic diagnostic without leaving a
second packaging implementation available.
"""

FORMAL_BOUNDARY_LABEL = "tobkiri-core-package-defaults-v1"
FORMAL_API = "run_formal_defaults_packaging(DefaultsPackagingRequest)"
VERIFIED_OUTPUT = "verified_catalog"


def _reject_direct_caller():
    """Reject every non-core invocation before inspecting caller inputs."""
    raise RuntimeError(
        "direct presentation packaging is disabled; use Rust "
        f"{FORMAL_API} at {FORMAL_BOUNDARY_LABEL}; "
        f"only {VERIFIED_OUTPUT} is returned while the core lease is held"
    )


def package_artifact(*args, **kwargs):
    """Reject the retired Python packaging API."""
    del args, kwargs
    _reject_direct_caller()


def main(argv=None):
    """Reject the retired command-line entrypoint."""
    del argv
    _reject_direct_caller()


# Keep the stale module name unusable even when imported rather than executed.
# This is deliberately after the compatibility definitions so static callers
# cannot mistake the file for an implementation while every real load fails.
_reject_direct_caller()
