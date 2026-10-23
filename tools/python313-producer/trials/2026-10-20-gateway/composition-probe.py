"""Capture the fixed composition without compiling or executing any guest."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
sys.path.insert(0, '/replay-source/scripts/wasm')
from replay_gateway_tool_caller import WAC_SOURCE
assert hashlib.sha256(WAC_SOURCE).hexdigest() == '107675f2bfcdbe32bd7eab4c77a9aa9bde2e6dc2e6d1aa9378989a006f2ba4b4'
Path('/work/replay.wac').write_bytes(WAC_SOURCE)
subprocess.run(['/wac', 'compose', '/work/replay.wac', '--dep',
                'local:caller=/caller/component.wasm', '--dep',
                'local:provider=/provider/component.wasm', '--deps-dir',
                '/work/no-ambient-dependencies', '-o', '/work/composed.wasm'],
               check=True, timeout=60, cwd='/work')
binary = Path('/work/composed.wasm').read_bytes()
sha = hashlib.sha256(binary).hexdigest()
assert sha == '2530b6a090f6584cfc6ffeef131b070af429c0b120381be463c767f47bb2fe67'
Path('/work/composition.json').write_text(json.dumps({'composition_sha256':sha,
    'composition_bytes':len(binary), 'guest_compilation_or_execution':False},indent=2)+'\n')
