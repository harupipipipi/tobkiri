
from blocks._common import ok, error, not_implemented, timestamp, gen_id


def run(input_data, context):
    event_name = input_data.get("event")
    event_data = input_data.get("data")
    if not event_name:
        return error("event name is required")
    return ok({
        "event": event_name,
        "data": event_data,
        "delivered": False,
        "reason": "no connected clients",
        "ts": timestamp(),
    })
