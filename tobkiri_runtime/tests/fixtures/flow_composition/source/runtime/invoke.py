"""Independent Flow QA source Pack; no peer imports or network."""

def tobkiri_packvm_invoke(operation_id, payload):
    if operation_id != 'emit':
        raise ValueError("unknown operation")
    return dict(payload)
