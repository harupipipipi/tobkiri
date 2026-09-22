

from domain.tool.ui_compiler_tools import ui_generation_status


def run(input_data, context):
    return ui_generation_status(input_data, context if isinstance(context, dict) else {})
