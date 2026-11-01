

from blocks._common import ok
from domain.prompt.effective import resolve_effective_prompt


def run(input_data, context):
    del context
    return ok(resolve_effective_prompt(input_data if isinstance(input_data, dict) else {}))
