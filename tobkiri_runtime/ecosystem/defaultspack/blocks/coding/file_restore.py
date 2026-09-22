"""Sunset compatibility diagnostic for legacy workspace snapshot restore."""

from blocks._common import error


def run(input_data, context=None):
    """Fail closed because snapshot ownership was not migrated."""

    snapshot_id = input_data.get("snapshot_id")
    if not snapshot_id:
        return error("'snapshot_id' is required", code="INVALID_INPUT")
    return error(
        "workspace snapshot restore has no selected Wave 8 owner",
        code="UNAVAILABLE",
        details={
            "snapshot_id": snapshot_id,
            "migration": "use Git restore or coding sandbox discard",
        },
    )
