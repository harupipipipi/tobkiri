#!/usr/bin/env python3
"""
Rumi AI OS - エントリポイント

Kernelを起動し、Packが提供するサービス（HTTPサーバー等）を開始する。
Flask/dotenv等の特定フレームワークには依存しない。

HTTPサーバーが必要な場合:
  Packが io.http.server をInterfaceRegistryに登録する。

Wave 19-A 変更:
  VULN-C01: production 環境での --permissive 起動を拒否
  host_execution ガード: 未承認 Pack の起動時拒否
"""

import sys
import atexit
import argparse
import os
import traceback
import threading
from pathlib import Path
import logging
from typing import Dict

from core_runtime.env_compat import read_migrated_env

_kernel = None
_logger = logging.getLogger(__name__)


# Fallback L() — overwritten if core_runtime.lang loads successfully
def L(key, **kwargs):
    return key


def _check_permissive_production_guard():
    """
    VULN-C01: 明示的な許可がない限り permissive モードの使用を拒否する。
    ホワイトリスト方式: RUMI_ALLOW_PERMISSIVE=true または
    RUMI_ENVIRONMENT=development|dev の場合のみ許可。
    追加条件: user_data/permissive.lock ファイルの存在も必須。
    """
    import os
    # --- 環境変数チェック ---
    env_val = os.environ.get("RUMI_ENVIRONMENT", "").lower()
    if env_val in ("production", "prod"):
        print(
            "FATAL: permissive mode is not allowed in production.",
            file=sys.stderr,
        )
        sys.exit(1)

    env_ok = (
        os.environ.get("RUMI_ALLOW_PERMISSIVE", "").lower() == "true"
        or env_val in ("development", "dev")
    )

    if not env_ok:
        print(
            "FATAL: permissive mode requires explicit opt-in.",
            file=sys.stderr,
        )
        print(
            "Set RUMI_ALLOW_PERMISSIVE=true or "
            "RUMI_ENVIRONMENT=development to use permissive mode.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- lockfile チェック ---
    user_data_dir = read_migrated_env("TOBKIRI_USER_DATA", "RUMI_USER_DATA")
    if user_data_dir:
        lockfile = Path(user_data_dir) / "permissive.lock"
    else:
        lockfile = Path(__file__).resolve().parent / "user_data" / "permissive.lock"

    if not lockfile.is_file():
        print(
            "FATAL: permissive mode requires lockfile: "
            f"{lockfile}",
            file=sys.stderr,
        )
        print(
            "Create it with: touch "
            f"{lockfile}",
            file=sys.stderr,
        )
        sys.exit(1)


def _start_permissive_warning_loop():
    """permissive モード中に 30 秒間隔で WARNING ログを出力する。"""
    import time as _time
    _logger = logging.getLogger(__name__)

    def _warn_loop():
        while True:
            _time.sleep(30)
            _logger.warning(
                "Rumi is running in PERMISSIVE mode. "
                "Sandbox is disabled. Do NOT use in production."
            )

    t = threading.Thread(target=_warn_loop, daemon=True)
    t.start()


def _check_critical_dependencies():
    """
    Check that critical dependencies are importable before proceeding.
    If missing, print a helpful message and exit.
    """
    missing = []
    for mod_name in ("yaml", "cryptography"):
        try:
            __import__(mod_name)
        except ImportError:
            missing.append(mod_name)
    if missing:
        print(
            "FATAL: Missing critical dependencies: " + ", ".join(missing),
            file=sys.stderr,
        )
        print(
            "Install them with: pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(1)


def main():
    global _kernel

    parser = argparse.ArgumentParser(description="Rumi AI OS")
    parser.add_argument("command", nargs="?", help="Optional command such as 'migrate-hmac'")
    parser.add_argument("--headless", action="store_true", help="Run without HTTP server")
    parser.add_argument("--permissive", action="store_true", help="Run in permissive security mode (development only)")
    parser.add_argument("--validate", action="store_true", help="Validate all Pack ecosystem.json files and exit")
    parser.add_argument("--health", action="store_true", help="Run health check and exit with status")
    parser.add_argument(
        "--migrate-trust-store-hmac",
        action="store_true",
        help="Rewrite legacy trusted_handlers.json with an HMAC signature and exit",
    )
    args = parser.parse_args()

    _check_critical_dependencies()

    # --- ログ設定 ---
    import os
    from core_runtime.logging_utils import configure_logging
    _log_level = read_migrated_env("TOBKIRI_LOG_LEVEL", "RUMI_LOG_LEVEL", "INFO")
    _log_format = read_migrated_env("TOBKIRI_LOG_FORMAT", "RUMI_LOG_FORMAT", "json")
    configure_logging(level=_log_level, fmt=_log_format)

    # --- Health check mode (early exit) ---
    if args.health:
        from core_runtime.health import (
            get_health_checker, probe_disk_space, probe_file_writable,
        )
        import json
        import os
        import tempfile
        checker = get_health_checker()
        if os.name == "nt":
            disk_path = os.environ.get("SystemDrive", "C:") + "\\"
        else:
            disk_path = "/"
        tmp_dir = tempfile.gettempdir()
        checker.register_probe("disk", lambda: probe_disk_space(disk_path))
        checker.register_probe("writable_tmp", lambda: probe_file_writable(tmp_dir))
        result = checker.aggregate_health()
        print(json.dumps(result, indent=2))
        sys.exit(0 if result["status"] == "UP" else 1)

    # --- Pack validation mode (early exit) ---
    if args.validate:
        _run_validation()
        return

    if args.command == "migrate-hmac" or args.migrate_trust_store_hmac:
        _run_hmac_migration()
        return

    # セキュリティモード設定 — デフォルトは strict（secure）。
    # CLI と環境変数のどちらで要求されても、permissive 化は同じ
    # 明示的 opt-in / lockfile ガードを必ず通す。
    requested_permissive = (
        args.permissive
        or os.environ.get("RUMI_SECURITY_MODE", "").lower() == "permissive"
    )
    if requested_permissive:
        # VULN-C01: production 環境および lockfile なしの permissive 化を拒否
        _check_permissive_production_guard()

        os.environ["RUMI_SECURITY_MODE"] = "permissive"
        print("=" * 60)
        print("WARNING: Running in permissive mode. Sandbox is disabled.")
        print("Pack code may execute on host without Docker isolation.")
        print("Do NOT use --permissive in production.")
        print("=" * 60)
        _start_permissive_warning_loop()
    else:
        # 未対応の外部値も含め、通常起動は strict に固定する。
        os.environ["RUMI_SECURITY_MODE"] = "strict"

    # --- host_execution ガード (W19-A) ---
    try:
        from core_runtime.pack_validator import validate_host_execution
        validate_host_execution()
    except SystemExit:
        raise
    except Exception:
        # Pack 探索に失敗してもメイン起動は妨げない（ecosystem 未構築時など）
        _logger.debug("validate_host_execution failed during startup preflight", exc_info=True)

    try:
        from core_runtime import Kernel
        try:
            from core_runtime.app_lifecycle_manager import (
                mark_runtime_failed,
                reset_runtime_readiness,
            )
        except (ImportError, ModuleNotFoundError):
            def reset_runtime_readiness() -> None:
                return None

            def mark_runtime_failed(_error: str) -> None:
                return None

        try:
            from core_runtime.lang import L as _L, load_system_lang
            global L
            L = _L
        except ImportError:
            load_system_lang = lambda: None

        # Langシステム初期化
        load_system_lang()

        _kernel = Kernel()

        print(f"[Rumi] {L('startup.starting')}")
        deferred_runtime_thread = None
        if args.headless:
            _kernel.run_startup()
        else:
            reset_runtime_readiness()
            _kernel.run_startup_until("api_init")

        # --- W19-D: host_execution guard ---
        from core_runtime.pack_validator import validate_host_execution_single
        from core_runtime.paths import discover_pack_locations
        import json
        blocked_packs = []
        for loc in discover_pack_locations():
            try:
                with open(loc.ecosystem_json_path, "r", encoding="utf-8") as f:
                    eco = json.load(f)
                he_ok, he_msg = validate_host_execution_single(eco)
                if not he_ok:
                    blocked_packs.append((loc.pack_id, he_msg))
                elif he_msg:
                    print(f'[Rumi] [{loc.pack_id}] {he_msg}')
            except Exception:
                _logger.debug("host_execution validation skipped for pack '%s'", loc.pack_id, exc_info=True)
        if blocked_packs:
            for pid, he_msg in blocked_packs:
                print(f'[Rumi] BLOCKED [{pid}]: {he_msg}')
            print('[Rumi] Startup aborted: host_execution packs require RUMI_ALLOW_HOST_EXECUTION=true')
            sys.exit(1)


        atexit.register(lambda: _kernel.shutdown() if _kernel else None)

        def _start_auto_update_thread():
            def _run_auto_updates():
                try:
                    from core_runtime.github_update_manager import get_github_update_manager

                    result = get_github_update_manager().run_auto_updates_once()
                    if result.due:
                        _logger.info("Auto update check finished: %s", result.to_dict())
                except Exception:
                    _logger.debug("Auto update check skipped", exc_info=True)

            threading.Thread(
                target=_run_auto_updates,
                daemon=True,
                name="rumi-auto-update",
            ).start()

        def _finish_runtime_startup():
            try:
                _kernel.run_startup_remaining()
                try:
                    from backend_core.ecosystem.compat import mark_ecosystem_initialized

                    mark_ecosystem_initialized()
                except Exception:
                    _logger.debug("mark_ecosystem_initialized failed", exc_info=True)
                try:
                    from core_runtime.startup_surface_launcher import (
                        launch_pending_startup_profile_surface,
                    )

                    launch_result = launch_pending_startup_profile_surface()
                    if launch_result.get("launched"):
                        _logger.info(
                            "Startup profile surface launched: %s",
                            launch_result,
                        )
                    elif launch_result.get("reason") not in {None, "not_pending"}:
                        _logger.warning(
                            "Startup profile surface launch skipped: %s",
                            launch_result,
                        )
                except Exception:
                    _logger.debug("startup profile surface launch skipped", exc_info=True)
                print(f"[Rumi] {L('startup.success')}")
                _start_auto_update_thread()
            except Exception as e:
                mark_runtime_failed(str(e))
                _logger.error("Deferred runtime startup failed", exc_info=True)
                traceback.print_exc()

        if not args.headless:
            deferred_runtime_thread = threading.Thread(
                target=_finish_runtime_startup,
                daemon=True,
                name="rumi-runtime-startup",
            )
            deferred_runtime_thread.start()
        else:
            try:
                from backend_core.ecosystem.compat import mark_ecosystem_initialized

                mark_ecosystem_initialized()
            except Exception:
                _logger.debug("mark_ecosystem_initialized failed", exc_info=True)
            print(f"[Rumi] {L('startup.success')}")
            _start_auto_update_thread()

        if args.headless:
            print(f"[Rumi] {L('startup.headless')}")
            return

        # HTTPサーバーがPackから提供されている場合は起動
        http_server = None
        pack_http_server = None

        # interface_overrides で優先 Pack が指定されていればそれを使う
        try:
            from backend_core.ecosystem.active_ecosystem import get_active_ecosystem_manager
            aem = get_active_ecosystem_manager()
            override_pack = aem.get_interface_override("io.http.server")
            if override_pack:
                pack_http_server = _kernel.interface_registry.get_by_owner(
                    "io.http.server", override_pack
                )
                http_server = pack_http_server
        except Exception:
            _logger.debug("HTTP server override lookup failed", exc_info=True)

        # override が見つからなければ通常の last を使う
        if pack_http_server is None:
            try:
                registered_http_servers = _kernel.interface_registry.find(
                    lambda key, entry: key == "io.http.server"
                )
                for entry in reversed(registered_http_servers):
                    candidate = entry.get("value")
                    meta = entry.get("meta", {})
                    if callable(candidate) and not meta.get("_system"):
                        pack_http_server = candidate
                        break
            except Exception:
                _logger.debug("Pack-provided HTTP server lookup failed", exc_info=True)
        if http_server is None:
            http_server = pack_http_server or _kernel.interface_registry.get("io.http.server")
        if http_server and callable(http_server):
            print(f"[Rumi] {L('startup.http_starting')}")
            # Wave 17-A: KernelFacade でラップし、Pack コードへの Kernel 直接参照を遮断
            from core_runtime.kernel_facade import KernelFacade
            kernel_facade = KernelFacade(_kernel)
            if pack_http_server and pack_http_server is not http_server:
                def _run_pack_http_server():
                    try:
                        pack_http_server(kernel_facade)
                    except Exception:
                        _logger.error("Pack HTTP server failed", exc_info=True)

                threading.Thread(target=_run_pack_http_server, daemon=True).start()
            _start_restart_signal_monitor()
            http_server(kernel_facade)
            _wait_for_signal()
        else:
            # Wave fix: フォールバック — pack_api_server が直接起動済みかチェック
            try:
                from core_runtime.pack_api_server import get_pack_api_server
                _api_srv = get_pack_api_server()
                if _api_srv is not None:
                    _srv_host = getattr(_api_srv, 'host', '127.0.0.1')
                    _srv_port = getattr(_api_srv, 'port', 8765)
                    print(f"[Rumi] Pack API server running on http://{_srv_host}:{_srv_port}")
                    _wait_for_signal()
                else:
                    print(f"[Rumi] {L('startup.no_http')}")
                    print(f"[Rumi] {L('startup.install_http_pack')}")
                    print(f"[Rumi] {L('startup.press_ctrl_c')}")
                    _wait_for_signal()
            except Exception:
                _logger.debug("Pack API server fallback inspection failed", exc_info=True)
                print(f"[Rumi] {L('startup.no_http')}")
                print(f"[Rumi] {L('startup.install_http_pack')}")
                print(f"[Rumi] {L('startup.press_ctrl_c')}")
                _wait_for_signal()

    except KeyboardInterrupt:
        print(f"\n[Rumi] {L('shutdown.starting')}")
    except Exception as e:
        print(f"[Rumi] {L('startup.failed')}: {e}")
        traceback.print_exc()
        sys.exit(1)


def _wait_for_signal():
    """停止シグナルまたは restart 要求を待機する。"""
    from core_runtime.api.control_panel_handlers import is_kernel_restart_requested

    stop_event = threading.Event()
    while not stop_event.wait(timeout=1.0):
        if is_kernel_restart_requested():
            _exit_for_restart_request()


def _start_restart_signal_monitor():
    """ブロッキング HTTP server 実行中も restart 要求を処理する。"""

    def _monitor():
        stop_event = threading.Event()
        while not stop_event.wait(timeout=1.0):
            from core_runtime.api.control_panel_handlers import is_kernel_restart_requested

            if is_kernel_restart_requested():
                _exit_for_restart_request()

    thread = threading.Thread(target=_monitor, daemon=True)
    thread.start()
    return thread


def _exit_for_restart_request():
    """restart 要求を exit code 42 としてプロセス全体へ反映する。"""
    from core_runtime.api.control_panel_handlers import (
        _RESTART_EXIT_CODE,
        clear_kernel_restart_request,
    )

    clear_kernel_restart_request()
    print(f"[Rumi] {L('shutdown.starting')}")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_RESTART_EXIT_CODE)


def _run_validation():
    """Pack ecosystem.json を検証し結果を出力する。"""
    from core_runtime.pack_validator import validate_packs

    def _print_validation_line(level: str, message: str) -> None:
        line = f"{level}: {message}"
        try:
            print(line)
        except UnicodeEncodeError:
            encoding = sys.stdout.encoding or "utf-8"
            sys.stdout.write(
                line.encode(encoding, errors="backslashreplace").decode(encoding) + "\n"
            )

    report = validate_packs()

    for err in report.errors:
        _print_validation_line("ERROR", err)
    for warn in report.warnings:
        _print_validation_line("WARNING", warn)

    summary = (
        f"{report.pack_count} packs scanned, {report.valid_count} valid, "
        f"{len(report.warnings)} warnings, {len(report.errors)} errors"
    )
    print(summary)


def _run_hmac_migration():
    """署名なしの HMAC 対象ファイルを再署名する。"""
    from core_runtime.capability_trust_store import CapabilityTrustStore
    from core_runtime.store_sharing_manager import SharedStoreManager
    from core_runtime.capability_installer import CapabilityInstaller
    from backend_core.ecosystem.active_ecosystem import ActiveEcosystemManager

    result_counts = {"already_signed": 0, "migrated": 0, "failed": 0}
    detailed: Dict[str, str] = {}

    trust_store = CapabilityTrustStore()
    detailed["trust_store"] = trust_store.migrate_hmac_signature()

    sharing_manager = SharedStoreManager()
    detailed["store_sharing"] = sharing_manager.migrate_hmac_signature()

    installer = CapabilityInstaller()
    installer_results = installer.migrate_hmac_signatures()
    detailed["capability_index"] = installer_results["index"]
    detailed["capability_blocked"] = installer_results["blocked"]

    active_ecosystem = ActiveEcosystemManager()
    detailed["active_ecosystem"] = active_ecosystem.migrate_hmac_signature()

    for status in detailed.values():
        if status in result_counts:
            result_counts[status] += 1

    for name, status in detailed.items():
        print(f"{name}: {status}")

    print(
        "Summary: "
        f"already_signed={result_counts['already_signed']} "
        f"migrated={result_counts['migrated']} "
        f"failed={result_counts['failed']}"
    )

    if result_counts["failed"] > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
