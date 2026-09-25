"""
test_python_file_executor.py - PythonFileExecutor ユニットテスト

対象: core_runtime/python_file_executor.py
全テストは mock ベースで外部依存なし。
"""
from __future__ import annotations

import json
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from core_runtime.python_file_executor import (
    PythonFileExecutor,
    ExecutionContext,
    ExecutionResult,
    PathValidator,
)


def _make_context(**kwargs) -> ExecutionContext:
    """テスト用 ExecutionContext を生成"""
    defaults = {
        "flow_id": "test_flow",
        "step_id": "test_step",
        "phase": "startup",
        "ts": "2025-01-01T00:00:00Z",
        "owner_pack": "test_pack",
        "inputs": {},
    }
    defaults.update(kwargs)
    return ExecutionContext(**defaults)


class TestPathValidationTraversal(unittest.TestCase):
    """パストラバーサル拒否"""

    def test_path_validation_traversal(self):
        """../../etc/passwd のようなパストラバーサルは拒否される"""
        executor = PythonFileExecutor()

        ctx = _make_context(owner_pack="my_pack")

        # PathValidator を mock して traversal を検出
        mock_validator = MagicMock()
        mock_validator.validate.return_value = (
            False,
            "Path outside allowed roots: /etc/passwd",
            None,
        )
        executor._path_validator = mock_validator

        # approval_checker も mock（承認済みにする）
        mock_approval = MagicMock()
        mock_approval.is_approved.return_value = (True, None)
        mock_approval.verify_hash.return_value = (True, None)
        executor._approval_checker = mock_approval

        with patch.object(executor, '_audit', MagicMock()):
            result = executor.execute(
                file_path="../../etc/passwd",
                owner_pack="my_pack",
                input_data={},
                context=ctx,
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "path_rejected")
        self.assertEqual(result.execution_mode, "rejected")


class TestApprovalCheckFailure(unittest.TestCase):
    """未承認 Pack の実行拒否"""

    def test_approval_check_failure(self):
        """未承認 Pack の python_file_call は拒否される"""
        executor = PythonFileExecutor()

        ctx = _make_context(owner_pack="unapproved_pack")

        # approval_checker を mock して未承認を返す
        mock_approval = MagicMock()
        mock_approval.is_approved.return_value = (
            False,
            "Pack 'unapproved_pack' is not approved (status: pending)",
        )
        executor._approval_checker = mock_approval

        with patch.object(executor, '_audit', MagicMock()):
            result = executor.execute(
                file_path="run.py",
                owner_pack="unapproved_pack",
                input_data={},
                context=ctx,
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "approval_rejected")
        self.assertEqual(result.execution_mode, "rejected")


class TestDockerExecutionSuccess(unittest.TestCase):
    """Docker モードの正常実行（subprocess mock）"""

    def test_docker_execution_success(self):
        """Docker コンテナ実行が正常に完了するケース"""
        executor = PythonFileExecutor()
        executor._security_mode = "strict"

        ctx = _make_context(owner_pack="my_pack")

        # approval_checker mock
        mock_approval = MagicMock()
        mock_approval.is_approved.return_value = (True, None)
        mock_approval.verify_hash.return_value = (True, None)
        executor._approval_checker = mock_approval

        # path_validator mock
        resolved_path = Path("/fake/ecosystem/my_pack/run.py")
        mock_validator = MagicMock()
        mock_validator.validate.return_value = (True, None, resolved_path)
        executor._path_validator = mock_validator

        # docker available
        with patch.object(executor, '_check_docker_available', return_value=True):
            # UDS proxy mock
            mock_uds = MagicMock()
            mock_uds.ensure_pack_socket.return_value = (True, None, Path("/run/rumi/egress.sock"))
            executor._uds_proxy_manager = mock_uds

            # _execute_in_container を mock して成功を返す
            mock_result = ExecutionResult(
                success=True,
                output={"status": "ok"},
                execution_mode="container",
            )
            with patch.object(executor, '_execute_in_container', return_value=mock_result):
                with patch.object(executor, '_audit', MagicMock()):
                    result = executor.execute(
                        file_path="run.py",
                        owner_pack="my_pack",
                        input_data={"key": "val"},
                        context=ctx,
                    )

        self.assertTrue(result.success)
        self.assertEqual(result.execution_mode, "container")
        self.assertEqual(result.output, {"status": "ok"})


class TestHostExecutionTimeout(unittest.TestCase):
    """ホスト実行のタイムアウト"""

    def test_host_execution_timeout(self):
        """permissive モードでのホスト実行がタイムアウトする"""
        executor = PythonFileExecutor()
        executor._security_mode = "permissive"

        ctx = _make_context(owner_pack="my_pack")

        # approval_checker mock
        mock_approval = MagicMock()
        mock_approval.is_approved.return_value = (True, None)
        mock_approval.verify_hash.return_value = (True, None)
        executor._approval_checker = mock_approval

        # path_validator mock
        resolved_path = Path("/fake/ecosystem/my_pack/slow.py")
        mock_validator = MagicMock()
        mock_validator.validate.return_value = (True, None, resolved_path)
        executor._path_validator = mock_validator

        # docker unavailable → host execution
        with patch.object(executor, '_check_docker_available', return_value=False):
            # _execute_on_host を mock してタイムアウト結果を返す
            timeout_result = ExecutionResult(
                success=False,
                error="Host execution timed out after 0.1s",
                error_type="timeout",
                execution_mode="host_permissive",
            )
            with patch.object(executor, '_execute_on_host', return_value=timeout_result):
                with patch.object(executor, '_audit', MagicMock()):
                    result = executor.execute(
                        file_path="slow.py",
                        owner_pack="my_pack",
                        input_data={},
                        context=ctx,
                        timeout_seconds=0.1,
                    )

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "timeout")
        self.assertIn("timed out", result.error.lower())


def _fork_available() -> bool:
    try:
        multiprocessing.get_context("fork")
        return True
    except ValueError:
        return False


class _NonBuiltinError(Exception):
    """whitelist 外（非 builtins）の例外型の格下げ確認用。"""


@unittest.skipUnless(_fork_available(), "fork start method required")
class TestHostWorkerProcessIsolation(unittest.TestCase):
    """PR#1322: タイムアウトしたホストワーカーは capability 権限ごと終了させる。

    旧実装では daemon thread で実行していたため、タイムアウト後もワーカーが
    rumi_capability / RUMI_CAPABILITY_SOCKET の ambient authority を持ったまま
    動き続けた（finally の env 除去は生存ワーカーには無効）。
    子プロセス化により、タイムアウト時に terminate+reap してから除去する。
    """

    def _write_pack_file(self, directory: Path, body: str) -> Path:
        pack_file = directory / "pack_run.py"
        pack_file.write_text(body, encoding="utf-8")
        return pack_file

    def test_timed_out_worker_is_killed_before_capability_strip(self):
        """タイムアウト後、ワーカーは実際に死んでおり capability env も復元される"""
        executor = PythonFileExecutor()
        ctx = _make_context(owner_pack="my_pack")

        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            heartbeat = tmpdir / "heartbeat.txt"
            env_seen = tmpdir / "env_seen.txt"
            cap_sock = tmpdir / "capability.sock"
            cap_sock.touch()

            # capability env を読んで記録し、ハートビートを書き続ける run()
            pack_file = self._write_pack_file(
                tmpdir,
                "import os, time\n"
                "def run(input_data, context):\n"
                f"    open({json.dumps(str(env_seen))}, 'w').write(\n"
                "        os.environ.get('RUMI_CAPABILITY_SOCKET', ''))\n"
                "    while True:\n"
                f"        with open({json.dumps(str(heartbeat))}, 'a') as f:\n"
                "            f.write('x')\n"
                "        time.sleep(0.02)\n",
            )

            prev_env = "previous-capability-socket"
            os.environ["RUMI_CAPABILITY_SOCKET"] = prev_env
            try:
                result = executor._execute_on_host(
                    pack_file,
                    "my_pack",
                    {},
                    ctx,
                    timeout_seconds=0.3,
                    capability_sock_path=cap_sock,
                )
            finally:
                os.environ.pop("RUMI_CAPABILITY_SOCKET", None)

            self.assertFalse(result.success)
            self.assertEqual(result.error_type, "timeout")

            # ワーカーは capability env を実際に持って実行されていたこと
            self.assertEqual(env_seen.read_text(), str(cap_sock))

            # ワーカーが死んでいること: ハートビートが増えない
            # （旧実装では daemon thread が存続し書き続けた）
            size_at_return = heartbeat.stat().st_size
            time.sleep(0.6)
            self.assertEqual(heartbeat.stat().st_size, size_at_return)

            # 子プロセスが残留していないこと
            self.assertEqual(multiprocessing.active_children(), [])

    def test_worker_process_returns_result_with_capability_env(self):
        """正常系: 子プロセス内で capability env/module が使え、結果が戻る"""
        executor = PythonFileExecutor()
        ctx = _make_context(owner_pack="my_pack")

        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            cap_sock = tmpdir / "capability.sock"
            cap_sock.touch()

            pack_file = self._write_pack_file(
                tmpdir,
                "import os\n"
                "import rumi_capability\n"
                "def run(input_data, context):\n"
                "    return {\n"
                "        'echo': input_data,\n"
                "        'cap_env': os.environ.get('RUMI_CAPABILITY_SOCKET', ''),\n"
                "        'ctx_sock': context.get('capability_socket', ''),\n"
                "    }\n",
            )

            result = executor._execute_on_host(
                pack_file,
                "my_pack",
                {"key": "val"},
                ctx,
                timeout_seconds=30.0,
                capability_sock_path=cap_sock,
            )

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.output["echo"], {"key": "val"})
        self.assertEqual(result.output["cap_env"], str(cap_sock))
        self.assertEqual(result.output["ctx_sock"], str(cap_sock))

    def test_worker_process_exception_propagates(self):
        """ワーカー内例外は型を保って呼び出し側へ伝播する"""
        executor = PythonFileExecutor()
        ctx = _make_context(owner_pack="my_pack")

        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            pack_file = self._write_pack_file(
                tmpdir,
                "def run(input_data, context):\n"
                "    raise ValueError('kaboom')\n",
            )

            result = executor._execute_on_host(
                pack_file,
                "my_pack",
                {},
                ctx,
                timeout_seconds=30.0,
                capability_sock_path=None,
            )

        self.assertFalse(result.success)
        self.assertIn("kaboom", result.error)
        self.assertEqual(result.error_type, "ValueError")

    def test_sigterm_ignoring_worker_and_grandchild_killed_by_killpg(self):
        """SIGTERM 無視ワーカーとその孫が killpg でグループごと掃討される。

        setsid なし旧実装では terminate が直接子だけに届き、os.fork で作った
        孫が RUMI_CAPABILITY_SOCKET を継承したまま残った。setsid+killpg で
        SIGTERM 無視ワーカーにも SIGKILL が届き、孫も巻き込んで全滅する。
        """
        executor = PythonFileExecutor()
        ctx = _make_context(owner_pack="my_pack")

        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            worker_hb = tmpdir / "worker_hb.txt"
            grandchild_hb = tmpdir / "grandchild_hb.txt"
            grandchild_pid_file = tmpdir / "grandchild_pid.txt"

            pack_file = self._write_pack_file(
                tmpdir,
                "import os, signal, time\n"
                "def run(input_data, context):\n"
                "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "    pid = os.fork()\n"
                "    if pid == 0:\n"
                "        # 孫プロセス: SIGTERM無視を継承したまま鼓動し続ける\n"
                "        while True:\n"
                f"            with open({json.dumps(str(grandchild_hb))}, 'a') as f:\n"
                "                f.write('x')\n"
                "            time.sleep(0.02)\n"
                f"    open({json.dumps(str(grandchild_pid_file))}, 'w').write(str(pid))\n"
                "    while True:\n"
                f"        with open({json.dumps(str(worker_hb))}, 'a') as f:\n"
                "            f.write('x')\n"
                "        time.sleep(0.02)\n",
            )

            result = executor._execute_on_host(
                pack_file,
                "my_pack",
                {},
                ctx,
                timeout_seconds=0.3,
                capability_sock_path=None,
            )

            self.assertFalse(result.success)
            self.assertEqual(result.error_type, "timeout")

            # ワーカー・孫ともに鼓動が止まる（killpg でグループごと SIGKILL）
            worker_size = worker_hb.stat().st_size
            grandchild_size = grandchild_hb.stat().st_size
            time.sleep(0.6)
            self.assertEqual(worker_hb.stat().st_size, worker_size)
            self.assertEqual(grandchild_hb.stat().st_size, grandchild_size)

            # 孫プロセスが死んでいること（init が reap するまで pid が
            # 残り得るため短くリトライして確認）
            grandchild_pid = int(grandchild_pid_file.read_text().strip())
            dead = False
            for _ in range(50):
                try:
                    os.kill(grandchild_pid, 0)
                except (ProcessLookupError, PermissionError):
                    dead = True
                    break
                time.sleep(0.05)
            self.assertTrue(dead, "grandchild process survived killpg")

            self.assertEqual(multiprocessing.active_children(), [])

    def test_dribbling_result_frame_does_not_wedge_parent(self):
        """虚偽フレーム+dribble送信でも親はデッドラインで返る。

        旧実装の Queue.get(timeout) は到着 poll しか bounded でなく、
        ヘッダ到着後の _recv_bytes() が無制限ブロックするため、巨大
        フレームを宣言して少しずつ送るワーカーが親を永遠に詰まらせた。
        """
        import core_runtime.python_file_executor as pfe
        import os as _os
        import time as _t

        def _evil_entry(conn_w, target, serialize):
            try:
                _os.setsid()
            except OSError:
                pass
            fd = conn_w.fileno()
            # 上限内サイズ(1KiB)のフレームを宣言しつつ中身を dribble し続ける
            _os.write(fd, (1024).to_bytes(4, "big"))
            while True:
                _os.write(fd, b"x")
                _t.sleep(0.05)

        with patch.object(pfe, "_worker_process_entry", _evil_entry):
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                pfe._run_in_worker_process(lambda: None, 0.3, lambda v: v)
            elapsed = time.monotonic() - start

        # 旧実装ではここに到達しない（永久ブロック）。猶予+kill時間を
        # 十分に下回る bounded な応答であること。
        self.assertLess(elapsed, 15.0)
        self.assertEqual(multiprocessing.active_children(), [])

    def test_bogus_giant_frame_header_rejected_and_killed(self):
        """上限超過のフレームヘッダは即拒否し、dribbling ワーカーを殺す"""
        import core_runtime.python_file_executor as pfe
        import os as _os
        import time as _t

        def _evil_entry(conn_w, target, serialize):
            try:
                _os.setsid()
            except OSError:
                pass
            fd = conn_w.fileno()
            # 1GiB の虚偽ヘッダを書いてから永遠に dribble
            _os.write(fd, (1 << 30).to_bytes(4, "big"))
            while True:
                _os.write(fd, b"x")
                _t.sleep(0.05)

        with patch.object(pfe, "_worker_process_entry", _evil_entry):
            start = time.monotonic()
            with self.assertRaises(RuntimeError):
                pfe._run_in_worker_process(lambda: None, 30.0, lambda v: v)
            elapsed = time.monotonic() - start

        # 30s の timeout を待たずに即座に拒否されること
        self.assertLess(elapsed, 15.0)
        self.assertEqual(multiprocessing.active_children(), [])

    def test_hostile_pickle_frame_does_not_execute_in_parent(self):
        """細工した pickle フレームは親で gadget を実行せず bounded error。

        ワーカーが書き込みパイプ fd を直接掴み、os.system REDUCE gadget を
        含む pickle を送りつけても、親側の whitelist Unpickler が
        find_class("posix", "system") を拒否して RuntimeError に収束し、
        マーカーファイルは作られない（旧実装の素の pickle.loads は
        信頼側親プロセスでの任意コード実行を許した — R2 指摘1）。
        """
        import core_runtime.python_file_executor as pfe
        import pickle as _pickle
        import os as _os

        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "pwned.txt"

            class _Gadget:
                def __reduce__(self):
                    return (_os.system, (f"touch {marker}",))

            blob = _pickle.dumps(("ok", _Gadget()))

            def _evil_entry(conn_w, target, serialize):
                try:
                    _os.setsid()
                except OSError:
                    pass
                conn_w.send_bytes(blob)

            with patch.object(pfe, "_worker_process_entry", _evil_entry):
                start = time.monotonic()
                with self.assertRaises(RuntimeError) as cm:
                    pfe._run_in_worker_process(lambda: None, 30.0, lambda v: v)
                elapsed = time.monotonic() - start

            self.assertIn("undecodable", str(cm.exception))
            self.assertFalse(
                marker.exists(), "pickle gadget executed in parent process"
            )
            self.assertLess(elapsed, 15.0)
            self.assertEqual(multiprocessing.active_children(), [])

    def test_hostile_pickle_wedge_gadget_rejected_fast(self):
        """sleep(10**9) gadget も即拒否（deserialization 自体が bounded）。

        deadline は read をカバーするが loads 後の実行まではカバーしない。
        whitelist Unpickler が gadget 実行を防ぐため、細工フレームでも
        親は wedge しない。
        """
        import core_runtime.python_file_executor as pfe
        import pickle as _pickle
        import os as _os
        import time as _t

        class _Wedge:
            def __reduce__(self):
                return (_t.sleep, (10 ** 9,))

        blob = _pickle.dumps(("ok", _Wedge()))

        def _evil_entry(conn_w, target, serialize):
            try:
                _os.setsid()
            except OSError:
                pass
            conn_w.send_bytes(blob)

        with patch.object(pfe, "_worker_process_entry", _evil_entry):
            start = _t.monotonic()
            with self.assertRaises(RuntimeError):
                pfe._run_in_worker_process(lambda: None, 30.0, lambda v: v)
            elapsed = _t.monotonic() - start

        # 旧実装では loads 内の sleep(10**9) が親を永久ブロックする
        self.assertLess(elapsed, 15.0)
        self.assertEqual(multiprocessing.active_children(), [])

    def test_decode_worker_result_rejects_non_safe_globals(self):
        """_decode_worker_result: whitelist 外型・壊れた blob・不正形は
        全て bounded な RuntimeError に収束する（例外は起きない）。"""
        import core_runtime.python_file_executor as pfe
        import pickle as _pickle
        import datetime as _dt

        # builtins 外の型（datetime）は find_class で拒否
        with self.assertRaises(RuntimeError):
            pfe._decode_worker_result(
                _pickle.dumps(("ok", _dt.datetime(2025, 1, 1)))
            )
        # 非 builtins 例外も拒否
        with self.assertRaises(RuntimeError):
            pfe._decode_worker_result(
                _pickle.dumps(("err", _NonBuiltinError("x")))
            )
        # pickle ですらない blob
        with self.assertRaises(RuntimeError):
            pfe._decode_worker_result(b"\x00\x01not-a-pickle")
        # 形が不正（tuple 以外 / status 名が違う / 要素数が違う）
        with self.assertRaises(RuntimeError):
            pfe._decode_worker_result(_pickle.dumps(["ok", 1]))
        with self.assertRaises(RuntimeError):
            pfe._decode_worker_result(_pickle.dumps(("bogus", 1)))
        with self.assertRaises(RuntimeError):
            pfe._decode_worker_result(_pickle.dumps(("ok", 1, 2)))
        # 正常形は通る
        status, payload = pfe._decode_worker_result(
            _pickle.dumps(("ok", {"a": [1, "x", None]}))
        )
        self.assertEqual(status, "ok")
        self.assertEqual(payload, {"a": [1, "x", None]})

    def test_worker_non_builtin_exception_degrades_to_runtime_error(self):
        """whitelist 外の例外ペイロードは RuntimeError に安全に格下げ"""
        import core_runtime.python_file_executor as pfe
        import pickle as _pickle
        import os as _os

        blob = _pickle.dumps(("err", _NonBuiltinError("boom")))

        def _evil_entry(conn_w, target, serialize):
            try:
                _os.setsid()
            except OSError:
                pass
            conn_w.send_bytes(blob)

        with patch.object(pfe, "_worker_process_entry", _evil_entry):
            with self.assertRaises(RuntimeError) as cm:
                pfe._run_in_worker_process(lambda: None, 30.0, lambda v: v)
        self.assertIn("undecodable", str(cm.exception))
        self.assertEqual(multiprocessing.active_children(), [])

    def test_worker_base_exception_and_non_error_payload_wrapped(self):
        """SystemExit 等の非 Exception / 非例外ペイロードは RuntimeError に
        包まれ、呼び出し側の except Exception を抜けて親を落とさない。"""
        import core_runtime.python_file_executor as pfe
        import pickle as _pickle
        import os as _os

        for blob, needle in (
            (_pickle.dumps(("err", SystemExit(9))), "SystemExit"),
            (_pickle.dumps(("err", "just a string")), "non-exception"),
        ):
            def _evil_entry(conn_w, target, serialize, _blob=blob):
                try:
                    _os.setsid()
                except OSError:
                    pass
                conn_w.send_bytes(_blob)

            with patch.object(pfe, "_worker_process_entry", _evil_entry):
                with self.assertRaises(RuntimeError) as cm:
                    pfe._run_in_worker_process(lambda: None, 30.0, lambda v: v)
            self.assertIn(needle, str(cm.exception))
        self.assertEqual(multiprocessing.active_children(), [])

    def test_worker_custom_pack_exception_degrades_to_runtime_error(self):
        """pack 独自例外型は whitelist 外のため error_type=RuntimeError に
        格下げされる（builtins 例外のみ型が round-trip する）。"""
        executor = PythonFileExecutor()
        ctx = _make_context(owner_pack="my_pack")

        with tempfile.TemporaryDirectory() as td:
            pack_file = self._write_pack_file(
                Path(td),
                "class MyPackError(Exception):\n"
                "    pass\n"
                "def run(input_data, context):\n"
                "    raise MyPackError('custom boom')\n",
            )
            result = executor._execute_on_host(
                pack_file,
                "my_pack",
                {},
                ctx,
                timeout_seconds=30.0,
                capability_sock_path=None,
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "RuntimeError")
        self.assertIn("undecodable", result.error)

    def test_worker_builtin_exception_type_round_trips(self):
        """builtins 例外は型を保って round-trip し error_type 分類に使える"""
        executor = PythonFileExecutor()
        ctx = _make_context(owner_pack="my_pack")

        with tempfile.TemporaryDirectory() as td:
            pack_file = self._write_pack_file(
                Path(td),
                "def run(input_data, context):\n"
                "    raise KeyError('missing_key')\n",
            )
            result = executor._execute_on_host(
                pack_file,
                "my_pack",
                {},
                ctx,
                timeout_seconds=30.0,
                capability_sock_path=None,
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "KeyError")
        self.assertIn("missing_key", result.error)

    def test_reaped_worker_is_not_signaled_in_cleanup(self):
        """正常系で reap 済みのワーカーへ finally がシグナルを送らない。

        join で回収後に pid が再利用されていると、無条件の killpg/os.kill
        が無関係のプロセスグループを殺す stale-pid TOCTOU になる
        （R2 指摘2）。正常経路では _kill_worker_tree 自体が呼ばれず、
        直接呼んでも生存チェックで早期 return することを確認する。
        """
        import core_runtime.python_file_executor as pfe
        import signal as _signal

        with patch.object(pfe, "_kill_worker_tree") as mock_kill:
            value = pfe._run_in_worker_process(
                lambda: {"ok": True}, 30.0, lambda v: v
            )
        self.assertEqual(value, {"ok": True})
        mock_kill.assert_not_called()

        # 既に reap 済みの Process へ _kill_worker_tree を呼んでも
        # os.killpg / os.kill は発行されない（内部の生存ガード）
        ctx = multiprocessing.get_context("fork")
        proc = ctx.Process(target=lambda: None, daemon=True)
        proc.start()
        proc.join(30.0)
        self.assertFalse(proc.is_alive())
        with patch("os.killpg") as mock_killpg, patch("os.kill") as mock_os_kill:
            pfe._kill_worker_tree(proc, _signal.SIGKILL)
        mock_killpg.assert_not_called()
        mock_os_kill.assert_not_called()
        self.assertEqual(multiprocessing.active_children(), [])

    def test_set_range_amplification_frame_rejected_fast(self):
        """set(range(N)) REDUCE フレームは親側でメモリ増幅せず即拒否。

        range は lazy なため ~60B のフレームが builtins.set の materialize
        で ~1GB（N=3e7）に増幅し、deadline 到達後に実行される親側の
        デコードを永久 wedge させ得た（R3 指摘1）。builtins 例外クラス
        以外を find_class で全拒否するため増幅 gadget は構築されず、
        bounded な RuntimeError に収束する。
        """
        import core_runtime.python_file_executor as pfe
        import pickle as _pickle
        import os as _os

        class _Amplify:
            def __reduce__(self):
                # ~60B のフレームが親で set(range(3e7)) ≈ 1GB を構築する
                return (set, (range(3 * 10 ** 7),))

        blob = _pickle.dumps(("ok", _Amplify()))
        # フレーム自体は小さいこと（range は lazy に pickle される）
        self.assertLess(len(blob), 4096)

        # デコード自体が即 RuntimeError（増幅構築が走らない）
        with self.assertRaises(RuntimeError):
            pfe._decode_worker_result(blob)

        def _evil_entry(conn_w, target, serialize):
            try:
                _os.setsid()
            except OSError:
                pass
            conn_w.send_bytes(blob)

        with patch.object(pfe, "_worker_process_entry", _evil_entry):
            start = time.monotonic()
            with self.assertRaises(RuntimeError) as cm:
                pfe._run_in_worker_process(lambda: None, 30.0, lambda v: v)
            elapsed = time.monotonic() - start

        self.assertIn("undecodable", str(cm.exception))
        # 旧実装ではここで ~1GB 確保 / 永久 wedge。bounded であること。
        self.assertLess(elapsed, 15.0)
        self.assertEqual(multiprocessing.active_children(), [])

    def test_grandchild_killed_on_success_path_by_worker_suicide_pact(self):
        """正常復帰したワーカーの孫も残留しない（worker 側自殺協定）。

        ワーカーが os.fork で孫を残したまま結果を返すと、親は leader を
        reap 済みで is_alive() ガードが親側 killpg を発火させず、孫が
        RUMI_CAPABILITY_SOCKET / 継承 fd を握ったまま残った（R3 指摘2）。
        ワーカー側 finally の killpg(0, SIGKILL) が自身のグループを
        生きたまま掃討するため、成功パスでも孫は死ぬ。
        """
        executor = PythonFileExecutor()
        ctx = _make_context(owner_pack="my_pack")

        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            grandchild_hb = tmpdir / "grandchild_hb.txt"
            grandchild_pid_file = tmpdir / "grandchild_pid.txt"

            pack_file = self._write_pack_file(
                tmpdir,
                "import os, time\n"
                "def run(input_data, context):\n"
                "    pid = os.fork()\n"
                "    if pid == 0:\n"
                "        # 孫: 呼び出し側が返った後も鼓動し続ける\n"
                "        while True:\n"
                f"            with open({json.dumps(str(grandchild_hb))}, 'a') as f:\n"
                "                f.write('x')\n"
                "            time.sleep(0.02)\n"
                f"    open({json.dumps(str(grandchild_pid_file))}, 'w').write(str(pid))\n"
                "    return {'status': 'ok'}\n",
            )

            result = executor._execute_on_host(
                pack_file,
                "my_pack",
                {},
                ctx,
                timeout_seconds=30.0,
                capability_sock_path=None,
            )

            # 正常系であること（孫の掃討が結果取得を妨げない）
            self.assertTrue(result.success, result.error)
            self.assertEqual(result.output, {"status": "ok"})

            # 孫は worker exit 時の killpg で死んでいる
            # （init が reap するまで pid が残り得るため短くリトライ）
            grandchild_pid = int(grandchild_pid_file.read_text().strip())
            dead = False
            for _ in range(50):
                try:
                    os.kill(grandchild_pid, 0)
                except (ProcessLookupError, PermissionError):
                    dead = True
                    break
                time.sleep(0.05)
            self.assertTrue(
                dead, "grandchild survived worker's success-path exit"
            )

            # 鼓動が止まることも確認
            if grandchild_hb.exists():
                size = grandchild_hb.stat().st_size
                time.sleep(0.3)
                self.assertEqual(grandchild_hb.stat().st_size, size)

            self.assertEqual(multiprocessing.active_children(), [])

    def test_worker_timeout_error_not_mislabeled_as_executor_timeout(self):
        """ワーカー内の builtins TimeoutError は executor timeout と区別される。

        builtins 例外は型を保って round-trip するため、ワーカーが投げた
        TimeoutError が ``except TimeoutError`` に誤認され error_type
        ="timeout" + timeout 診断イベントになっていた（R3 指摘3）。
        executor deadline は専用の ``_HostDeadlineError`` を投げるため、
        ワーカー由来は error_type="TimeoutError" に正しく分類される。
        """
        executor = PythonFileExecutor()
        ctx = _make_context(owner_pack="my_pack")

        with tempfile.TemporaryDirectory() as td:
            pack_file = self._write_pack_file(
                Path(td),
                "def run(input_data, context):\n"
                "    raise TimeoutError('pack-side deadline')\n",
            )
            result = executor._execute_on_host(
                pack_file,
                "my_pack",
                {},
                ctx,
                timeout_seconds=30.0,
                capability_sock_path=None,
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "TimeoutError")
        self.assertIn("pack-side deadline", result.error)

    def test_grandchild_killed_when_leader_exits_via_os_exit(self):
        """リーダーが os._exit で pact を迂回しても孫は親側 killpg で死ぬ。

        os._exit(0) は finally を実行しないため worker 側自殺協定は
        発火せず、leader は結果フレーム未送信のまま zombie になる。
        未回収の zombie は pid/pgid を保持し続けるため、親側の
        pre-reap ``killpg(proc.pid, SIGKILL)`` がグループごと孫を
        掃討する（R4 指摘1・macOS 実証済み）。
        """
        executor = PythonFileExecutor()
        ctx = _make_context(owner_pack="my_pack")

        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            grandchild_hb = tmpdir / "grandchild_hb.txt"
            grandchild_pid_file = tmpdir / "grandchild_pid.txt"

            pack_file = self._write_pack_file(
                tmpdir,
                "import os, time\n"
                "def run(input_data, context):\n"
                "    pid = os.fork()\n"
                "    if pid == 0:\n"
                "        # 孫: リーダー死亡後も鼓動し続ける\n"
                "        while True:\n"
                f"            with open({json.dumps(str(grandchild_hb))}, 'a') as f:\n"
                "                f.write('x')\n"
                "            time.sleep(0.02)\n"
                f"    open({json.dumps(str(grandchild_pid_file))}, 'w').write(str(pid))\n"
                "    os._exit(0)\n",  # worker pact(finally)を完全迂回
            )

            result = executor._execute_on_host(
                pack_file,
                "my_pack",
                {},
                ctx,
                timeout_seconds=30.0,
                capability_sock_path=None,
            )

            # フレーム未送信で死亡 → 無言終了として RuntimeError
            self.assertFalse(result.success)
            self.assertEqual(result.error_type, "RuntimeError")

            # 孫は親側 pre-reap killpg で死んでいる
            # （init が reap するまで pid が残り得るため短くリトライ）
            grandchild_pid = int(grandchild_pid_file.read_text().strip())
            dead = False
            for _ in range(50):
                try:
                    os.kill(grandchild_pid, 0)
                except (ProcessLookupError, PermissionError):
                    dead = True
                    break
                time.sleep(0.05)
            self.assertTrue(
                dead, "grandchild survived os._exit leader bypass"
            )

            if grandchild_hb.exists():
                size = grandchild_hb.stat().st_size
                time.sleep(0.3)
                self.assertEqual(grandchild_hb.stat().st_size, size)

            self.assertEqual(multiprocessing.active_children(), [])

    def test_grandchild_killed_when_pack_patches_os_killpg(self):
        """pack が os.killpg を潰しても孫は死ぬ（monkeypatch 迂回対策）。

        worker 側 pact は import 時捕捉の ``_OS_KILLPG``/``_SIGKILL`` を
        使い、さらに親は reap 前に ``killpg(proc.pid, SIGKILL)`` を送る
        ため、``os.killpg = lambda *a: None`` では双方を無効化できない
        （R4 指摘1）。
        """
        executor = PythonFileExecutor()
        ctx = _make_context(owner_pack="my_pack")

        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            grandchild_hb = tmpdir / "grandchild_hb.txt"
            grandchild_pid_file = tmpdir / "grandchild_pid.txt"

            pack_file = self._write_pack_file(
                tmpdir,
                "import os, time\n"
                # worker 側 pact(finally の os.killpg)の無効化を試みる
                "os.killpg = lambda *a, **k: None\n"
                "def run(input_data, context):\n"
                "    pid = os.fork()\n"
                "    if pid == 0:\n"
                "        # 孫: 呼び出し側が返った後も鼓動し続ける\n"
                "        while True:\n"
                f"            with open({json.dumps(str(grandchild_hb))}, 'a') as f:\n"
                "                f.write('x')\n"
                "            time.sleep(0.02)\n"
                f"    open({json.dumps(str(grandchild_pid_file))}, 'w').write(str(pid))\n"
                "    return {'status': 'ok'}\n",
            )

            result = executor._execute_on_host(
                pack_file,
                "my_pack",
                {},
                ctx,
                timeout_seconds=30.0,
                capability_sock_path=None,
            )

            self.assertTrue(result.success, result.error)
            self.assertEqual(result.output, {"status": "ok"})

            grandchild_pid = int(grandchild_pid_file.read_text().strip())
            dead = False
            for _ in range(50):
                try:
                    os.kill(grandchild_pid, 0)
                except (ProcessLookupError, PermissionError):
                    dead = True
                    break
                time.sleep(0.05)
            self.assertTrue(
                dead, "grandchild survived killpg monkeypatch bypass"
            )
            self.assertEqual(multiprocessing.active_children(), [])

    def test_decode_worker_result_rejects_ext1_extension_cache_bypass(self):
        """copyreg._extension_cache を seeded した EXT1 フレームは拒否する。

        C 実装 ``pickle.Unpickler`` は EXT1/2/4 opcode を ``find_class``
        より先にプロセス共有の ``_extension_cache`` で解決し、
        サブクラスの ``get_extension`` を一切呼ばないため、
        code -> os.system を seeded したフレームが whitelist を迂回
        し得た（R4 指摘2）。純 Python ``pickle._Unpickler`` 基底では
        ``get_extension`` オーバーライドが発火して必ず拒否する。
        """
        import copyreg
        import core_runtime.python_file_executor as pfe
        import pickle as _pickle
        import os as _os

        # ("ok", <EXT1 code=0x2a>) フレームを手組みする
        ext_frame = (
            b"\x80\x02"          # PROTO 2
            b"\x8c\x02ok"        # SHORT_BINUNICODE "ok"
            b"\x82\x2a"          # EXT1 code=0x2a
            b"\x86"              # TUPLE2
            b"\x2e"              # STOP
        )

        copyreg._extension_cache[0x2A] = _os.system
        try:
            # 攻撃の前提確認: C 実装は seeded cache 経由で whitelist 外の
            # os.system を解決する（後続 REDUCE があればそのまま実行）。
            self.assertIs(_pickle.loads(ext_frame)[1], _os.system)
            # 制限付き Unpickler は EXT opcode を必ず拒否して
            # bounded な RuntimeError に収束する
            with self.assertRaises(RuntimeError) as cm:
                pfe._decode_worker_result(ext_frame)
            self.assertIn("undecodable", str(cm.exception))
        finally:
            copyreg._extension_cache.pop(0x2A, None)

    def test_json_subclass_results_round_trip_cleanly(self):
        """str/int サブクラス（IntEnum 等）の結果がデコード拒否されず戻る。

        ``_ensure_json_compatible`` がサブクラスを素通り返すと pickle が
        GLOBAL/STACK_GLOBAL opcode を emit し、親側 whitelist decoder が
        非 builtins global として拒否するため、従来返っていた IntEnum
        等の結果が RuntimeError 化していた（R4 指摘3）。
        基底型へ正規化して round-trip させる。
        """
        executor = PythonFileExecutor()
        ctx = _make_context(owner_pack="my_pack")

        with tempfile.TemporaryDirectory() as td:
            pack_file = self._write_pack_file(
                Path(td),
                "import enum\n"
                "class Priority(enum.IntEnum):\n"
                "    HIGH = 5\n"
                "class Label(str):\n"
                "    pass\n"
                "def run(input_data, context):\n"
                "    return {\n"
                "        'priority': Priority.HIGH,\n"
                "        'label': Label('work'),\n"
                "        'ok': True,\n"
                "    }\n",
            )
            result = executor._execute_on_host(
                pack_file,
                "my_pack",
                {},
                ctx,
                timeout_seconds=30.0,
                capability_sock_path=None,
            )

        self.assertTrue(result.success, result.error)
        self.assertEqual(
            result.output,
            {"priority": 5, "label": "work", "ok": True},
        )
        self.assertIs(type(result.output["priority"]), int)
        self.assertIs(type(result.output["label"]), str)
        self.assertEqual(multiprocessing.active_children(), [])


if __name__ == "__main__":
    unittest.main()
