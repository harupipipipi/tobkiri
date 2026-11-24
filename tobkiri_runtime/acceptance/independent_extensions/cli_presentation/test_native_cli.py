"""Actual arm64 executable tests against isolated HTTP fixtures, never Host/VM."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).parent
BINARY = ROOT / 'native-build/TobkiriCLI.app/Contents/MacOS/tobkiri-shell'
spec = importlib.util.spec_from_file_location('own_frontend_fixtures', ROOT / 'test_cli_frontend.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


@contextmanager
def response_fixture(mode: str):
    """Return intentionally invalid HTTP protocol data from an isolated server."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers['Content-Length']))
            seen.append(self.path)
            if mode == 'redirect':
                self.send_response(302)
                self.send_header('Location', '/never-follow')
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            if self.path == '/api/panel/auth/exchange':
                value = {'success': True, 'data': {'csrf_token': 'fixture-csrf',
                                                  'journal_scope': 'isolated-only'}}
            else:
                value = {'success': True, 'data': {'status': 'ok', 'value': {
                    'stdout': 'isolated native fixture', 'stderr': '',
                    'exit_status': 0, 'stream': 'complete'}}}
                if mode == 'output_keys':
                    value['data']['value']['approved'] = True
                if mode == 'numeric_success':
                    value['success'] = 1
                if mode == 'oversize':
                    self.wfile.write(b' ' * (1048576 + 1))
                    return
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', seen
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


class NativeCliTests(unittest.TestCase):
    """Invoke the actual binary directly with no Python/SDK runtime environment."""

    def frame(self, **changes: object) -> dict:
        """Use the closed public application.invoke frame shape."""
        value = {'protocol': 'io.tobkiri.cli.io.v1', 'type': 'command',
                 'request_id': 'cli:req:native001', 'command': 'application.invoke',
                 'arguments': {'command_id': 'transcript.render', 'input': {
                     'messages': [], 'columns': 40, 'output_limit': 4096}},
                 'stdin': None, 'tty': False, 'output_limit': 4096}
        value.update(changes)
        return value

    def run_binary(self, endpoint: str, frames: list[dict] | str,
                   binary: Path = BINARY, bootstrap: bytes | None = None) -> subprocess.CompletedProcess:
        """Provide explicit fixture bootstrap; execute with only system PATH."""
        raw = bootstrap or json.dumps({'endpoint': endpoint, 'bootstrap_code': 'fixture-native-secret'}).encode()
        text = frames if isinstance(frames, str) else ''.join(json.dumps(f) + '\n' for f in frames)
        with fixtures.inherited_pipe(raw) as fd:
            return subprocess.run([str(binary), '--bootstrap-fd', str(fd)],
                                  pass_fds=(fd,), env={'PATH': '/usr/bin:/bin'},
                                  input=text, capture_output=True, text=True,
                                  timeout=20, check=False)

    def test_native_stdio_bootstrap_cookie_csrf_and_exact_contract_route(self) -> None:
        with fixtures.isolated_endpoint() as (endpoint, seen):
            result = self.run_binary(endpoint, [self.frame()])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['type'], 'result')
            self.assertEqual(len(seen), 2)
            self.assertEqual(seen[1][0], '/api/contracts/acceptance.cli.application/POST%20%2Fapi%2Ftranscript%2Frender')
            headers = {key.lower(): value for key, value in seen[1][2].items()}
            self.assertEqual(headers['x-rumi-csrf'], 'fixture-csrf')
            self.assertIn('fixture_session=isolated', headers['cookie'])
            self.assertNotIn('fixture-native-secret', result.stdout + result.stderr)

    def test_missing_stdin_file_or_invalid_bootstrap_fd_is_refused(self) -> None:
        for args in ([], ['--bootstrap-fd', '0'], ['--bootstrap-fd', '-1']):
            result = subprocess.run([str(BINARY), *args], env={'PATH': '/usr/bin:/bin'},
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 64)
            self.assertEqual(result.stdout, '')
        with tempfile.TemporaryFile() as file:
            result = subprocess.run([str(BINARY), '--bootstrap-fd', str(file.fileno())],
                                    pass_fds=(file.fileno(),), env={'PATH': '/usr/bin:/bin'},
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 64)

    def test_duplicate_oversize_extra_or_nonloopback_bootstrap_is_refused(self) -> None:
        for raw in (b'{"endpoint":"http://localhost:1","bootstrap_code":"fixture"}',
                    b'{"endpoint":"http://127.0.0.1:1","bootstrap_code":"fixture","approved":true}',
                    b'{"endpoint":"x","endpoint":"y","bootstrap_code":"fixture"}', b'x' * 2049):
            with self.subTest(raw=raw[:12]):
                result = self.run_binary('http://127.0.0.1:1', [], bootstrap=raw)
                self.assertEqual(result.returncode, 64)
                self.assertEqual(result.stdout, '')

    def test_unknown_identity_input_and_duplicate_frames_are_denied_before_http(self) -> None:
        with fixtures.isolated_endpoint() as (endpoint, seen):
            invalid = [self.frame(profile_id='forbidden.profile'),
                       self.frame(arguments={'command_id': 'unknown.command', 'input': {}}),
                       self.frame(arguments={'command_id': 'transcript.render', 'input': {'approved': True}}),
                       self.frame(tty=1)]
            text = ''.join(json.dumps(f) + '\n' for f in invalid)
            text += json.dumps(self.frame()).replace('"columns": 40', '"columns": 40, "columns": 50') + '\n'
            result = self.run_binary(endpoint, text)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([json.loads(line)['type'] for line in result.stdout.splitlines()], ['error'] * 5)
            self.assertEqual(len(seen), 1)

    def test_cancel_and_signal_do_not_dispatch(self) -> None:
        with fixtures.isolated_endpoint() as (endpoint, seen):
            result = self.run_binary(endpoint, [self.frame(cancel=True), self.frame(signal='SIGTERM')])
            self.assertEqual(result.returncode, 0)
            self.assertEqual([json.loads(line)['stream'] for line in result.stdout.splitlines()], ['cancelled'] * 2)
            self.assertEqual(len(seen), 1)

    def test_denied_bootstrap_or_contract_never_falls_back(self) -> None:
        with fixtures.isolated_endpoint(deny_bootstrap=True) as (endpoint, seen):
            result = self.run_binary(endpoint, [self.frame()])
            self.assertEqual(result.returncode, 64)
            self.assertEqual(len(seen), 1)
        with fixtures.isolated_endpoint(deny_contract=True) as (endpoint, seen):
            result = self.run_binary(endpoint, [self.frame()])
            self.assertEqual(json.loads(result.stdout)['type'], 'error')
            self.assertEqual(len(seen), 2)

    def test_redirect_is_refused_without_following(self) -> None:
        with response_fixture('redirect') as (endpoint, seen):
            result = self.run_binary(endpoint, [self.frame()])
            self.assertEqual(result.returncode, 64)
            self.assertEqual(seen, ['/api/panel/auth/exchange'])

    def test_oversize_output_extra_keys_and_numeric_success_are_refused(self) -> None:
        for mode in ('oversize', 'output_keys', 'numeric_success'):
            with self.subTest(mode=mode), response_fixture(mode) as (endpoint, seen):
                result = self.run_binary(endpoint, [self.frame()])
                self.assertEqual(json.loads(result.stdout)['type'], 'error')
                self.assertEqual(len(seen), 2)
        with fixtures.isolated_endpoint() as (endpoint, _seen):
            result = self.run_binary(endpoint, [self.frame(output_limit=1)])
            self.assertEqual(json.loads(result.stdout)['type'], 'error')

    def test_pinned_resource_mutation_is_refused_before_authentication(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / 'native-build') as temporary:
            bundle = Path(temporary) / 'Tampered.app'
            shutil.copytree(ROOT / 'native-build/TobkiriCLI.app', bundle)
            (bundle / 'Contents/Resources/commands.json').write_text('{}\n')
            with fixtures.isolated_endpoint() as (endpoint, seen):
                result = self.run_binary(endpoint, [self.frame()], bundle / 'Contents/MacOS/tobkiri-shell')
                self.assertEqual(result.returncode, 64)
                self.assertEqual(seen, [])


if __name__ == '__main__':
    unittest.main()
