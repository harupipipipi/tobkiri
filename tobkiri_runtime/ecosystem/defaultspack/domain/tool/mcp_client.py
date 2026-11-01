"""Compatibility pool; transport resources are implemented by the Host.

New captured providers use the Host connection owner instead of this ambient
legacy pool. This module keeps existing callers working during that migration.
"""

from core_runtime.mcp.transport import McpConnections


class McpClient(McpConnections):
    """Legacy process-wide pool retained until callers migrate to an owner."""

    _instance = None
    _initialized: bool

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if not self._initialized:
            super().__init__()
            self._initialized = True
