"""Deprecated legacy routing-rule store surface."""

from blocks._common import error


def run(input_data, context):
    del input_data, context
    return error(
        "legacy routing-rule ownership was removed; use resolved profile policy",
        "MIGRATED_OWNER_REQUIRED",
    )
