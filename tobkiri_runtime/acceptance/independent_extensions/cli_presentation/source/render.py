"""Pure bounded terminal transcript rendering; no filesystem/network/Host access."""
from __future__ import annotations

import textwrap
import unicodedata
from typing import Any


def printable(text: str) -> str:
    """Escape terminal controls including ANSI, OSC, bidi and DEL sequences."""
    return ''.join(
        ch if ch == '\n' or not unicodedata.category(ch).startswith('C')
        else f'\\u{ord(ch):04x}' for ch in text
    )


def render(payload: dict[str, Any]) -> dict[str, Any]:
    """Render exact validated messages without invoking a terminal or authority."""
    if not isinstance(payload, dict) or set(payload) != {
        'messages', 'columns', 'output_limit'
    }:
        raise ValueError('invalid_input')
    columns = payload['columns']
    limit = payload['output_limit']
    messages = payload['messages']
    if (type(columns) is not int or not 20 <= columns <= 200
            or type(limit) is not int or not 1 <= limit <= 1048576
            or not isinstance(messages, list) or len(messages) > 128):
        raise ValueError('invalid_input')
    sections = []
    for message in messages:
        if (not isinstance(message, dict) or set(message) != {'role', 'text'}
                or message['role'] not in ('user', 'assistant', 'system', 'tool')
                or not isinstance(message['text'], str)
                or len(message['text']) > 8192):
            raise ValueError('invalid_input')
        lines = printable(message['text']).split('\n')
        wrapped = '\n'.join(textwrap.fill(line, width=columns) for line in lines)
        sections.append(f"[{message['role']}]\n{wrapped}")
    stdout = '\n\n'.join(sections) + ('\n' if sections else '')
    if len(stdout.encode('utf-8')) > limit:
        raise ValueError('output_limit')
    return {'stdout': stdout, 'stderr': '', 'exit_status': 0, 'stream': 'complete'}
