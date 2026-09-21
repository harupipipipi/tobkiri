"""Sunset compatibility diagnostic for legacy workspace snapshots."""

from blocks._common import error


def run(input_data, context=None):
    """Fail closed instead of creating a second snapshot owner."""

    return error(
        "workspace snapshots have no selected Wave 8 owner",
        code="UNAVAILABLE",
        details={"migration": "use coding sandbox diff/discard or Git commit"},
    )
