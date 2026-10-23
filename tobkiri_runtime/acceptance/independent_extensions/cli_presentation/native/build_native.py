"""Build an owned arm64 macOS CLI bundle using system libraries, never Python at runtime."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess

ROOT = Path(__file__).parents[1]
BUILD = ROOT / 'native-build'
BUNDLE = BUILD / 'TobkiriCLI.app'


def build() -> Path:
    """Compile and ad-hoc sign a native source artifact without installing it."""
    for path in (BUILD, BUNDLE, BUNDLE / 'Contents',
                 BUNDLE / 'Contents/MacOS', BUNDLE / 'Contents/Resources'):
        if path.is_symlink():
            raise ValueError('native output symlink denied')
    commands = (ROOT / 'acceptance.cli.application/frontend/cli_frontend/commands.json').read_bytes()
    digest = hashlib.sha256(commands).hexdigest()
    executable = BUNDLE / 'Contents/MacOS/tobkiri-shell'
    resources = BUNDLE / 'Contents/Resources'
    executable.parent.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    (resources / 'commands.json').write_bytes(commands)
    info = {'CFBundleIdentifier': 'io.tobkiri.shell.cli.default',
            'CFBundleName': 'Tobkiri CLI', 'CFBundleExecutable': 'tobkiri-shell',
            'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1',
            'CFBundleShortVersionString': '1.0.0'}
    (BUNDLE / 'Contents/Info.plist').write_bytes(plistlib.dumps(info, sort_keys=True))
    tool_env = {'PATH': '/usr/bin:/bin'}
    clang = subprocess.check_output(['/usr/bin/xcrun', '--find', 'clang'],
                                    text=True, env=tool_env).strip()
    sdk = subprocess.check_output(['/usr/bin/xcrun', '--sdk', 'macosx', '--show-sdk-path'], text=True, env=tool_env).strip()
    subprocess.run([clang, '-isysroot', sdk, '-arch', 'arm64', '-fobjc-arc', '-O2', '-framework', 'Foundation',
                    f'-DCOMMAND_DIGEST="{digest}"', str(ROOT / 'native/main.m'),
                    '-o', str(executable)], check=True, env=tool_env)
    codesign = shutil.which('codesign')
    if codesign:
        subprocess.run([codesign, '--force', '--sign', '-', '--timestamp=none', str(BUNDLE)], check=True, env=tool_env)
        subprocess.run([codesign, '--verify', '--strict', str(BUNDLE)], check=True, env=tool_env)
    print(json.dumps({'binary': str(executable), 'binary_digest': 'sha256:' + hashlib.sha256(executable.read_bytes()).hexdigest(),
                      'command_resource_digest': 'sha256:' + digest,
                      'signature_kind': 'ad-hoc' if codesign else 'unsigned',
                      'runtime': 'macOS system Foundation/CoreFoundation/libSystem/libobjc only; no Python'}, indent=2))
    return executable


if __name__ == '__main__':
    build()
