"""Deprecated non-executing legacy route-preview surface."""

from blocks._common import error


def run(input_data, context):
    del input_data, context
    return error(
        "legacy route preview was removed; use rumi.service.ai.route.v1 with selected descriptors",
        "MIGRATED_OWNER_REQUIRED",
    )
