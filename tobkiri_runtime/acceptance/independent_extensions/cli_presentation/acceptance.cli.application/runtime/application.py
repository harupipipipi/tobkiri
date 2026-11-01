"""Pure CLI Application source descriptor; never an OS launch operation."""
from __future__ import annotations


def tobkiri_packvm_invoke(operation_id: str, payload: dict) -> dict:
    """Describe the selected source frontend without minting native authority."""
    if operation_id != 'launch' or payload != {}:
        raise ValueError('unknown operation or invalid input')
    return {'presentation': 'terminal_stdio', 'command_id': 'transcript.render'}
