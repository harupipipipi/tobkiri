"""Retired dynamic-Python Tool creation route."""



from blocks._common import error


def run(input_data, context):
    """Return an explicit migration result without compiling or running Python."""

    del input_data, context
    return error(
        "Dynamic Python Tools are retired. Migrate to a reviewed pack, MCP server, or connector.",
        "MIGRATION_REQUIRED",
        details={
            "migration_required": True,
            "supported_targets": ["reviewed_pack", "mcp_server", "connector"],
        },
    )
