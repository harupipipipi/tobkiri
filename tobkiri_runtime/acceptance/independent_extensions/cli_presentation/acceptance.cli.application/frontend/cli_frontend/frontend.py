"""CLI source frontend; credentials arrive only through an inherited pipe."""
from __future__ import annotations

import argparse
import importlib.resources
import os
import stat
import sys
from types import MappingProxyType
from typing import Any, Mapping, TextIO

from tobkiri_protocol.canonical import strict_loads
from tobkiri_protocol.cli_application import (
    CliApplicationError,
    PanelContractSession,
    run_application_stdio,
)


def read_bootstrap(fd: int) -> dict[str, str]:
    """Read bounded explicit Host connection material, never ambient credentials."""
    if type(fd) is not int or fd < 3:
        raise CliApplicationError('inherited bootstrap pipe required')
    if not stat.S_ISFIFO(os.fstat(fd).st_mode):
        raise CliApplicationError('inherited bootstrap pipe required')
    data = bytearray()
    while len(data) <= 2048:
        chunk = os.read(fd, min(512, 2049 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    if len(data) > 2048:
        raise CliApplicationError('bootstrap input exceeds limit')
    value = strict_loads(bytes(data))
    if (not isinstance(value, dict) or set(value) != {'endpoint', 'bootstrap_code'}
            or not all(isinstance(item, str) for item in value.values())):
        raise CliApplicationError('explicit Host bootstrap required')
    return value


def load_declarations() -> Mapping[str, Mapping[str, Any]]:
    """Load finite commands from the selected immutable Application resource."""
    raw = importlib.resources.files('cli_frontend').joinpath('commands.json').read_bytes()
    value = strict_loads(raw)
    if not isinstance(value, dict) or set(value) != {'transcript.render'}:
        raise CliApplicationError('finite Application command declaration required')
    return MappingProxyType({key: MappingProxyType(item) for key, item in value.items()})


def run_frontend(
    bootstrap_fd: int, declarations: Mapping[str, Mapping[str, Any]],
    input_stream: TextIO, output_stream: TextIO,
) -> int:
    """Exchange normal Host bootstrap then use public captured Contract transport."""
    connection = read_bootstrap(bootstrap_fd)
    session = PanelContractSession(connection['endpoint'], connection['bootstrap_code'])
    return run_application_stdio(session, declarations, input_stream, output_stream)


def main() -> int:
    """Require an explicit inherited bootstrap pipe and run the finite frontend."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bootstrap-fd', type=int, required=True)
    args = parser.parse_args()
    try:
        return run_frontend(args.bootstrap_fd, load_declarations(), sys.stdin, sys.stdout)
    except Exception:
        # Neither bootstrap data nor transport exception text reaches output/logs.
        sys.stderr.write('Tobkiri CLI bootstrap denied or unavailable\n')
        return 64


if __name__ == '__main__':
    raise SystemExit(main())
