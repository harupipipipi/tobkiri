

from blocks._common import error, ok
from domain.skill_feedback import create_skill_from_feedback


def run(input_data, context=None):
    del context
    try:
        return ok(create_skill_from_feedback(input_data if isinstance(input_data, dict) else {}))
    except ValueError as exc:
        return error(str(exc), "INVALID_INPUT")
    except Exception as exc:
        return error("failed to create skill: " + str(exc), "SKILL_CREATE_FAILED")
