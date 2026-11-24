"""Independent Flow QA transform Pack; no peer imports or network."""

def tobkiri_packvm_invoke(operation_id, payload):
    if operation_id != 'map':
        raise ValueError("unknown operation")
    return {**payload, "text": payload["text"].upper(), "count": payload["count"] + 1}
