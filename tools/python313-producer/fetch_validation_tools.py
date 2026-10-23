"""Restore only pinned WAC and Wasmtime validation inputs; execute neither."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import zipfile

from build_native import HERE, fingerprint
from fetch_sdk import inventory
from fetch_sources import capture

MAX_FILE = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_ENTRIES = 512

INPUTS = (
    {'filename': 'wac-cli', 'bytes': 22922992,
     'sha256': '7cd490edbd452221ef89e6750fc43eacc9b71b1242095eb577cb9a3bf0d3beda',
     'url': 'https://github.com/bytecodealliance/wac/releases/download/v0.12.0/wac-cli-x86_64-unknown-linux-musl'},
    {'filename': 'wasmtime-48.0.0-py3-none-manylinux1_x86_64.whl', 'bytes': 9796354,
     'sha256': '58544d539053dff7bd4cf30c40d7a540862d683013c0dfa6ba46a063f5b682f7',
     'url': 'https://files.pythonhosted.org/packages/a2/92/e144fcf578fc394678c24b042efe45f3b0614acdb87ea95d8b839b208842/wasmtime-48.0.0-py3-none-manylinux1_x86_64.whl'},
)


def extract_wheel(archive: Path, destination: Path) -> dict:
    """Extract a bounded wheel as data, rejecting links and unsafe path aliases."""
    destination.mkdir(mode=0o700)
    seen, expanded = set(), 0
    with zipfile.ZipFile(archive) as bundle:
        if len(bundle.infolist()) > MAX_ENTRIES:
            raise ValueError('validation wheel entry bound exceeded')
        for member in bundle.infolist():
            name = member.filename.rstrip('/')
            if (not name or '\\' in name or ':' in name or '\0' in name
                    or PurePosixPath(name).is_absolute()
                    or any(p in ('', '.', '..') for p in name.split('/'))
                    or name.casefold() in seen):
                raise ValueError('unsafe validation wheel path')
            seen.add(name.casefold())
            mode = member.external_attr >> 16
            kind = stat.S_IFMT(mode)
            allowed = (0, stat.S_IFDIR) if member.is_dir() else (0, stat.S_IFREG)
            if (kind not in allowed or mode & 0o7000 or member.flag_bits & 1
                    or not 0 <= member.file_size <= MAX_FILE):
                raise ValueError('unsafe validation wheel entry')
            path = destination / name
            if member.is_dir():
                if member.file_size:
                    raise ValueError('nonempty wheel directory')
                path.mkdir(parents=True, exist_ok=True)
                continue
            expanded += member.file_size
            if expanded > MAX_EXPANDED:
                raise ValueError('validation wheel expanded bound exceeded')
            path.parent.mkdir(parents=True, exist_ok=True)
            count = 0
            with bundle.open(member) as incoming, path.open('xb') as output:
                while data := incoming.read(65536):
                    count += len(data)
                    if count > member.file_size:
                        raise ValueError('validation wheel member grew')
                    output.write(data)
            if count != member.file_size:
                raise ValueError('truncated validation wheel member')
            path.chmod(0o644)
    return inventory(destination)


def run(destination: Path) -> None:
    """Download complete pinned bytes before naming or extracting any tool."""
    if sys.platform != 'linux' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('validation tool restoration is restricted to Linux CI')
    if (not destination.is_absolute() or not destination.parent.is_dir()
            or destination.parent.resolve() != destination.parent):
        raise ValueError('physical new validation tool directory required')
    repo = HERE.parents[1]
    lock = json.loads((repo / 'sdk/python/toolchain-lock.json').read_bytes())
    pin = lock['tools']['wasmtime']['wheels']['py3-none-manylinux1_x86_64']
    if (lock['tools']['wasmtime']['version'] != '48.0.0'
            or any(pin[k] != INPUTS[1][k] for k in ('filename', 'sha256'))):
        raise ValueError('validation wheel differs from existing SDK pin')
    destination.mkdir(mode=0o700)
    for item in INPUTS:
        request = Request(item['url'], headers={'User-Agent': 'Tobkiri-producer-replay'})
        with urlopen(request, timeout=60) as response:
            if urlsplit(response.geturl()).scheme != 'https':
                raise ValueError('validation input redirect must remain HTTPS')
            with tempfile.TemporaryFile(dir=destination) as temporary:
                if capture(response, temporary, item['sha256']) != item['bytes']:
                    raise ValueError('validation input size changed')
                temporary.seek(0)
                with (destination / item['filename']).open('xb') as output:
                    while data := temporary.read(65536):
                        output.write(data)
    (destination / 'wac-cli').chmod(0o755)
    files = extract_wheel(destination / INPUTS[1]['filename'], destination / 'wasmtime')
    receipt = {'format': 'tobkiri.python313-validation-inputs/1', 'outcome': 'success',
               'inputs': INPUTS, 'files': files, 'upstream_execution': False,
               'sdk_lock': fingerprint(repo / 'sdk/python/toolchain-lock.json')}
    (destination / 'validation-inputs.json').write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', type=Path, required=True)
    run(parser.parse_args().destination)
