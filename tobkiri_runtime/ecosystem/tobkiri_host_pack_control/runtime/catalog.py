"""Inert artifact marker for the Host-owned Pack catalog reader.

The production implementation is materialized by the Host backend.  Keeping
this artifact code-free prevents a selected Pack from gaining filesystem or
catalog access outside the authenticated RequestBroker path.
"""

from __future__ import annotations


def unavailable() -> None:
    """Reject direct execution outside the Host Pack-control backend."""

    raise RuntimeError("Host Pack control is available only through RequestBroker")
