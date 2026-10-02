

from blocks.memory.status import run as memory_status


def run(input_data, context=None):
    return memory_status(input_data or {}, context or {})
