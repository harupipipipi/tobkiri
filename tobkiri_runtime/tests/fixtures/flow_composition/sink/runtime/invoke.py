"""Independent Flow QA sink Pack; no peer imports or network."""

def tobkiri_packvm_invoke(operation_id, payload):
    if operation_id != 'collect':
        raise ValueError("unknown operation")
    return dict(payload)
