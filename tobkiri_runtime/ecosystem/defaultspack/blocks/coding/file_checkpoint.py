"""Sunset compatibility diagnostic for legacy workspace checkpoints."""

from blocks._common import error


def run(input_data, context=None):
    """Fail closed because Wave 8 selected no checkpoint data owner."""

    method = str(input_data.get("_method") or input_data.get("method") or "POST").upper()
    if method not in {"GET", "POST"}:
        return error("unsupported method: " + method, code="INVALID_INPUT")
    return error(
        "workspace checkpoints have no selected Wave 8 owner",
        code="UNAVAILABLE",
        details={"migration": "use coding sandbox diff/discard or Git commit"},
    )
