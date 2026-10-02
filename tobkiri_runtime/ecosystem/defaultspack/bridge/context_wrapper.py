"""bridge.context_wrapper — Wraps kernel context for handler use."""


import logging

logger = logging.getLogger("defaults.bridge")


class ContextWrapper:
    """Thin wrapper around the kernel-provided context dict."""

    def __init__(self, kernel_context):
        self._ctx = kernel_context if kernel_context else {}

    def call_handler(self, handler_id, input_data):
        """Invoke a handler via the capability socket, if available."""
        cap = self._ctx.get("capability_socket")
        if cap and callable(cap):
            return cap(handler_id, input_data)
        return {
            "status": "error",
            "error": {
                "code": "NO_CAPABILITY",
                "message": "capability_socket not available",
            },
        }

    def emit_event(self, event_name, data):
        """Emit a named event (MVP: log only)."""
        logger.info("Event: %s data=%s", event_name, data)
        return True

    def get_config(self, key, default=None):
        """Read a value from the kernel context."""
        return self._ctx.get(key, default)
