from __future__ import annotations



from domain.ui_compiler.service import compile_ui_plan


def run(input_data, context):
    del context
    return compile_ui_plan(input_data if isinstance(input_data, dict) else {})
