

from blocks._common import error, ok
from domain.share.store import ShareStore


def run(input_data, context=None):
    token = input_data.get("token")
    if not token:
        return error("'token' is required", code="INVALID_INPUT")
    record = ShareStore().get(str(token))
    if record is None:
        return error("Share link not found", code="NOT_FOUND")
    return ok(record)
