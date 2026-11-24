"""Source/stdio/session tests against an isolated fixture, never a real Host/VM."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).parent
RUNTIME = ROOT.parents[2]
# Standalone source tests need the public SDK import root explicitly.
sys.path.insert(0, str(RUNTIME))
from tobkiri_protocol.cli_application import (  # noqa: E402
    CliApplicationError, PanelContractSession, run_application_stdio,
)

spec = importlib.util.spec_from_file_location('own_frontend', ROOT / 'source/cli_frontend/frontend.py')
frontend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(frontend)
spec = importlib.util.spec_from_file_location('own_application_builder', ROOT / 'build_cli_application.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@contextmanager
def inherited_pipe(value: bytes):
    """Provide explicit fixture material through a real inherited pipe."""
    reader, writer = os.pipe()
    os.write(writer, value)
    os.close(writer)
    try:
        yield reader
    finally:
        os.close(reader)


@contextmanager
def isolated_endpoint(deny_bootstrap: bool = False, deny_contract: bool = False):
    """Serve a fixture protocol response without minting Host approval/evidence."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append((self.path, payload, dict(self.headers)))
            if self.path == '/api/panel/auth/exchange':
                result = {'success': not deny_bootstrap,
                          'data': {'csrf_token': 'fixture-csrf', 'journal_scope': 'fixture-only'}}
                self.send_response(200)
                self.send_header('Set-Cookie', 'fixture_session=isolated; HttpOnly; Path=/')
            else:
                result = {'success': not deny_contract, 'data': {'status': 'ok',
                          'value': {'stdout': '[assistant]\nisolated fixture result\n',
                                    'stderr': '', 'exit_status': 0, 'stream': 'complete'}}}
                self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class CliFrontendTests(unittest.TestCase):
    """Exercise explicit bootstrap and captured transport through public APIs."""

    def commands(self) -> dict:
        """Load only our generated Application's finite command resource."""
        return json.loads((ROOT / 'acceptance.cli.application/frontend/cli_frontend/commands.json').read_text())

    def frame(self, **changes: object) -> dict:
        """Create a schema-conforming fixture request without identity/approval."""
        frame = {'protocol': 'io.tobkiri.cli.io.v1', 'type': 'command',
                 'request_id': 'cli:req:frontend001', 'command': 'application.invoke',
                 'arguments': {'command_id': 'transcript.render',
                               'input': {'messages': [], 'columns': 40, 'output_limit': 4096}},
                 'stdin': None, 'tty': False, 'output_limit': 4096}
        frame.update(changes)
        return frame

    def test_authenticated_fixture_stdio_uses_finite_contract_http(self) -> None:
        with isolated_endpoint() as (endpoint, seen):
            with inherited_pipe(json.dumps({'endpoint': endpoint,
                                             'bootstrap_code': 'fixture-one-time'}).encode()) as fd:
                output = io.StringIO()
                frontend.run_frontend(fd, self.commands(),
                                      io.StringIO(json.dumps(self.frame()) + '\n'), output)
            result = json.loads(output.getvalue())
            self.assertEqual(result['type'], 'result')
            self.assertEqual(len(seen), 2)
            self.assertEqual(seen[0][0], '/api/panel/auth/exchange')
            self.assertEqual(seen[1][0], '/api/contracts/acceptance.cli.application/POST%20%2Fapi%2Ftranscript%2Frender')
            self.assertEqual(seen[1][2]['X-Rumi-Csrf'], 'fixture-csrf')
            self.assertIn('fixture_session=isolated', seen[1][2]['Cookie'])
            self.assertNotIn('fixture-one-time', output.getvalue())

    def test_denied_bootstrap_has_no_alternate_invocation(self) -> None:
        with isolated_endpoint(deny_bootstrap=True) as (endpoint, seen):
            with inherited_pipe(json.dumps({'endpoint': endpoint, 'bootstrap_code': 'fixture'}).encode()) as fd:
                with self.assertRaises(CliApplicationError):
                    frontend.run_frontend(fd, self.commands(), io.StringIO(), io.StringIO())
            self.assertEqual(len(seen), 1)

    def test_denied_contract_is_generic_error_not_direct_renderer(self) -> None:
        with isolated_endpoint(deny_contract=True) as (endpoint, seen):
            session = PanelContractSession(endpoint, 'fixture')
            output = io.StringIO()
            run_application_stdio(session, self.commands(), io.StringIO(json.dumps(self.frame()) + '\n'), output)
            self.assertEqual(json.loads(output.getvalue())['type'], 'error')
            self.assertEqual(len(seen), 2)

    def test_unknown_command_and_identity_injection_do_not_make_http_calls(self) -> None:
        with isolated_endpoint() as (endpoint, seen):
            session = PanelContractSession(endpoint, 'fixture')
            frames = [self.frame(arguments={'command_id': 'unknown.command', 'input': {}}),
                      self.frame(profile_id='injected.profile'),
                      self.frame(arguments={'command_id': 'transcript.render', 'input': {}, 'approved': True})]
            output = io.StringIO()
            run_application_stdio(session, self.commands(),
                                  io.StringIO(''.join(json.dumps(f) + '\n' for f in frames)), output)
            self.assertEqual([json.loads(line)['type'] for line in output.getvalue().splitlines()], ['error'] * 3)
            self.assertEqual(len(seen), 1)

    def test_closed_renderer_input_is_denied_before_contract_http(self) -> None:
        with isolated_endpoint() as (endpoint, seen):
            session = PanelContractSession(endpoint, 'fixture')
            frame = self.frame(arguments={'command_id': 'transcript.render', 'input': {'approved': True}})
            output = io.StringIO()
            run_application_stdio(session, self.commands(), io.StringIO(json.dumps(frame) + '\n'), output)
            self.assertEqual(json.loads(output.getvalue())['type'], 'error')
            self.assertEqual(len(seen), 1)

    def test_cancel_does_not_invoke_contract(self) -> None:
        with isolated_endpoint() as (endpoint, seen):
            session = PanelContractSession(endpoint, 'fixture')
            output = io.StringIO()
            run_application_stdio(session, self.commands(), io.StringIO(json.dumps(self.frame(cancel=True)) + '\n'), output)
            self.assertEqual(json.loads(output.getvalue())['stream'], 'cancelled')
            self.assertEqual(len(seen), 1)

    def test_local_auth_refuses_non_loopback_missing_or_redirectable_endpoints(self) -> None:
        for endpoint in ('https://127.0.0.1:1', 'http://localhost:1',
                         'http://127.0.0.1:1/path', 'http://user@127.0.0.1:1'):
            with self.subTest(endpoint=endpoint), self.assertRaises(CliApplicationError):
                PanelContractSession(endpoint, 'fixture')
        with self.assertRaises(CliApplicationError):
            PanelContractSession('http://127.0.0.1:1', '')

    def test_bootstrap_refuses_stdin_file_extra_fields_and_oversize(self) -> None:
        with self.assertRaises(CliApplicationError):
            frontend.read_bootstrap(0)
        with tempfile.TemporaryFile() as file:
            with self.assertRaises(CliApplicationError):
                frontend.read_bootstrap(file.fileno())
        for raw in (b'{}', b'{"endpoint":"x","bootstrap_code":"secret","approved":true}', b'x' * 2049):
            with self.subTest(raw=raw[:8]), inherited_pipe(raw) as fd:
                with self.assertRaises(Exception):
                    frontend.read_bootstrap(fd)

    def test_archive_real_process_refuses_absent_or_invalid_bootstrap(self) -> None:
        python = '/Users/haru/Desktop/puroguramukei/issue_review/tobikiri-pr-1322/.venv/bin/python'
        for arguments in ([], ['--bootstrap-fd', '0']):
            result = subprocess.run([python, '-B', str(ROOT / 'cli-frontend-source.pyz'), *arguments],
                                    cwd=RUNTIME, env={**os.environ, 'PYTHONPATH': str(RUNTIME),
                                                     'PYTHONDONTWRITEBYTECODE': '1'},
                                    capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')

    def test_archive_process_uses_explicit_pipe_and_authenticated_fixture(self) -> None:
        python = '/Users/haru/Desktop/puroguramukei/issue_review/tobikiri-pr-1322/.venv/bin/python'
        with isolated_endpoint() as (endpoint, seen):
            with inherited_pipe(json.dumps({'endpoint': endpoint,
                                             'bootstrap_code': 'fixture-private'}).encode()) as fd:
                result = subprocess.run(
                    [python, '-B', str(ROOT / 'cli-frontend-source.pyz'),
                     '--bootstrap-fd', str(fd)],
                    cwd=RUNTIME, env={**os.environ, 'PYTHONPATH': str(RUNTIME),
                                     'PYTHONDONTWRITEBYTECODE': '1'},
                    pass_fds=(fd,), input=json.dumps(self.frame()) + '\n',
                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['type'], 'result')
            self.assertEqual(len(seen), 2)
            self.assertNotIn('fixture-private', result.stdout + result.stderr)

    def test_cli_profile_compiles_source_pins_without_native_shell_claim(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            output = builder.compile_cli_profile(Path(temporary) / 'release')
            lock = json.loads((output / 'authored.profile.lock.v5.json').read_text())
        self.assertEqual(lock['application']['pack_id'], 'acceptance.cli.application')
        self.assertEqual(lock['shell']['provider_id'], 'shell.cli.default')
        self.assertEqual(lock['activation_authority'], 'unbound')
        shell = json.loads((RUNTIME / 'ecosystem/defaultspack/v4/shell.cli.default.shell.v1.json').read_text())
        self.assertEqual(shell['availability'], 'build_required')
        self.assertEqual(shell['launch']['variants'], [])

    def test_application_and_archive_builds_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            first, second = Path(temporary) / 'first', Path(temporary) / 'second'
            builder.build_application(first)
            builder.build_application(second)
            self.assertEqual({str(p.relative_to(first)): p.read_bytes() for p in first.rglob('*') if p.is_file()},
                             {str(p.relative_to(second)): p.read_bytes() for p in second.rglob('*') if p.is_file()})
            a, b = Path(temporary) / 'a.pyz', Path(temporary) / 'b.pyz'
            self.assertEqual(builder.build_source_archive(a), builder.build_source_archive(b))

    def test_frontend_map_targets_actual_selected_renderer(self) -> None:
        app = ROOT / 'acceptance.cli.application'
        target = json.loads((app / 'frontend_contract_map.v4.json').read_text())['routes'][0]['targets'][0]
        manifest = json.loads((ROOT / 'acceptance.cli.presentation/pack.v4.json').read_text())
        self.assertEqual(target['provider_id'], manifest['provider_catalog'][0]['provider_id'])
        self.assertEqual(target['function_id'], manifest['functions'][0]['id'])
        self.assertEqual(target['operation_id'], manifest['functions'][0]['operations'][0])


if __name__ == '__main__':
    unittest.main()
