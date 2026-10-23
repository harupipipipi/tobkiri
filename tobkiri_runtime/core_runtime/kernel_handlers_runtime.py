"""
kernel_handlers_runtime.py - 運用/実行系ハンドラ Mixin

Kernelの _h_* メソッドのうち運用・実行系を提供する。
Mixin方式でKernelクラスに合成される。

含まれるハンドラ:
- flow.load_all / flow.execute_by_id / modifier.load_all / modifier.apply
- python_file_call
- network.grant/revoke/check/list
- egress_proxy.start/stop/status
- uds_proxy.init/ensure_socket/stop/stop_all/status
- capability_proxy.init/status/stop_all
- lib.process_all/check/execute/clear_record/list_records
- audit.query/summary/flush
- vocab.list_groups/list_converters/summary/convert
- shared_dict.resolve/propose/explain/list/remove
"""

from __future__ import annotations


import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Protocol, Tuple

from .flow_loader import FlowDefinition

from .paths import ECOSYSTEM_DIR
from .kernel_flow_converter import FlowConverter

from .logging_utils import get_structured_logger
from .metrics import get_metrics_collector
from .capability_proxy import HostCapabilityProxyServer
from .diagnostics import Diagnostics
from .egress_proxy import UDSEgressProxyManager


class _InterfaceRegistryPort(Protocol):
    def get(self, key: str, strategy: str = "first") -> Any: ...

    def list(self) -> Dict[str, Any]: ...

    def register(
        self,
        key: str,
        value: Any,
        *,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None: ...

# M-10: 共通 FlowConverter インスタンス
_flow_converter = FlowConverter()

# Wave 15-D: structured logger
_logger = get_structured_logger("rumi.kernel.handlers.runtime")


# B4: 環境変数でverboseモード判定
def _is_diagnostics_verbose() -> bool:
    """RUMI_DIAGNOSTICS_VERBOSE=1 かどうか"""
    return os.environ.get("RUMI_DIAGNOSTICS_VERBOSE", "0") == "1"


class KernelRuntimeHandlersMixin:
    """
    運用/実行系ハンドラ Mixin

    __init__ を持たない。self の属性（diagnostics, interface_registry 等）は
    KernelCore.__init__ で初期化済みの前提でアクセスする。
    """

    diagnostics: Diagnostics
    interface_registry: _InterfaceRegistryPort
    _uds_proxy_manager: Optional[UDSEgressProxyManager]
    _capability_proxy: Optional[HostCapabilityProxyServer]

    if TYPE_CHECKING:
        def execute_flow_sync(
            self,
            flow_id: str,
            context: Optional[Dict[str, Any]] = None,
            timeout: Optional[float] = None,
            trusted_context: Optional[Dict[str, Any]] = None,
        ) -> Dict[str, Any]: ...

        async def _handle_universal_call_async(
            self,
            step: Dict[str, Any],
            ctx: Dict[str, Any],
        ) -> Dict[str, Any]: ...

        def _resolve_value(
            self,
            value: Any,
            ctx: Dict[str, Any],
            depth: int = 0,
        ) -> Any: ...

        def _get_uds_proxy_manager(self) -> UDSEgressProxyManager: ...

        def _get_capability_proxy(self) -> HostCapabilityProxyServer: ...

        def _now_ts(self) -> str: ...

    # ------------------------------------------------------------------
    # ハンドラ登録（Kernel._init_kernel_handlers から呼ばれる）
    # ------------------------------------------------------------------

    def _register_runtime_handlers(self) -> Dict[str, Any]:
        """ランタイム系ハンドラの辞書を返す"""
        return {
            "kernel:flow.load_all": self._h_flow_load_all,
            "kernel:flow.execute_by_id": self._h_flow_execute_by_id,
            "kernel:python_file_call": self._h_python_file_call,
            "kernel:universal_call": self._h_universal_call,
            "kernel:modifier.load_all": self._h_modifier_load_all,
            "kernel:modifier.apply": self._h_modifier_apply,
            "kernel:network.grant": self._h_network_grant,
            "kernel:network.revoke": self._h_network_revoke,
            "kernel:network.check": self._h_network_check,
            "kernel:network.list": self._h_network_list,
            "kernel:egress_proxy.start": self._h_egress_proxy_start,
            "kernel:egress_proxy.stop": self._h_egress_proxy_stop,
            "kernel:egress_proxy.status": self._h_egress_proxy_status,
            "kernel:lib.process_all": self._h_lib_process_all,
            "kernel:lib.check": self._h_lib_check,
            "kernel:lib.execute": self._h_lib_execute,
            "kernel:lib.clear_record": self._h_lib_clear_record,
            "kernel:lib.list_records": self._h_lib_list_records,
            "kernel:audit.query": self._h_audit_query,
            "kernel:audit.summary": self._h_audit_summary,
            "kernel:audit.flush": self._h_audit_flush,
            # vocab ハンドラ
            "kernel:vocab.list_groups": self._h_vocab_list_groups,
            "kernel:vocab.list_converters": self._h_vocab_list_converters,
            "kernel:vocab.summary": self._h_vocab_summary,
            "kernel:vocab.convert": self._h_vocab_convert,
            # shared_dict ハンドラ
            "kernel:shared_dict.resolve": self._h_shared_dict_resolve,
            "kernel:shared_dict.propose": self._h_shared_dict_propose,
            "kernel:shared_dict.explain": self._h_shared_dict_explain,
            "kernel:shared_dict.list": self._h_shared_dict_list,
            "kernel:shared_dict.remove": self._h_shared_dict_remove,
            # UDS Egress Proxy ハンドラ
            "kernel:uds_proxy.init": self._h_uds_proxy_init,
            "kernel:uds_proxy.ensure_socket": self._h_uds_proxy_ensure_socket,
            "kernel:uds_proxy.stop": self._h_uds_proxy_stop,
            "kernel:uds_proxy.stop_all": self._h_uds_proxy_stop_all,
            "kernel:uds_proxy.status": self._h_uds_proxy_status,
            "kernel:capability_proxy.init": self._h_capability_proxy_init,
            "kernel:capability_proxy.status": self._h_capability_proxy_status,
            "kernel:capability_proxy.stop_all": self._h_capability_proxy_stop_all,
            # Capability Grant ハンドラ (G-1)
            "kernel:capability.grant": self._h_capability_grant,
            "kernel:capability.revoke": self._h_capability_revoke,
            "kernel:capability.list": self._h_capability_list,
            # Pending export ハンドラ (G-2)
            "kernel:pending.export": self._h_pending_export,
            # Desktop app handlers (V-4)
            "kernel:desktop.launch": self._h_desktop_launch,
            "kernel:desktop.stop": self._h_desktop_stop,
        }

    # ------------------------------------------------------------------
    # flow.load_all + ヘルパー
    # ------------------------------------------------------------------

    def _h_flow_load_all(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """
        全Flowファイルをロードし、modifierを適用し、InterfaceRegistryに登録

        PR-B追加: 未承認/スキップ理由をdiagnosticsに記録（B4）
        """
        try:
            from .flow_loader import get_flow_loader
            from .flow_modifier import get_modifier_loader, get_modifier_applier
            from .audit_logger import get_audit_logger

            # 1. Flowをロード
            loader = get_flow_loader()
            flows = loader.load_all_flows()

            # Flowロードエラーを記録
            flow_errors = loader.get_load_errors()
            for err in flow_errors:
                self.diagnostics.record_step(
                    phase="startup",
                    step_id="flow.load.error",
                    handler="kernel:flow.load_all",
                    status="failed",
                    error={"errors": err.get("errors", [])},
                    meta={"file": err.get("file")}
                )

            # B4: スキップされたFlowをdiagnosticsに記録
            self._record_skipped_flows_to_diagnostics(loader)

            # 2. modifierをロード
            modifier_loader = get_modifier_loader()
            all_modifiers = modifier_loader.load_all_modifiers()

            # modifierロードエラーを記録
            modifier_errors = modifier_loader.get_load_errors()
            for err in modifier_errors:
                self.diagnostics.record_step(
                    phase="startup",
                    step_id="modifier.load.error",
                    handler="kernel:flow.load_all",
                    status="failed",
                    error={"errors": err.get("errors", [])},
                    meta={"file": err.get("file")}
                )

            # B4: スキップされたmodifierをdiagnosticsに記録
            self._record_skipped_modifiers_to_diagnostics(modifier_loader)

            # 3. 各Flowにmodifierを適用してIRに登録
            registered = []
            modifier_results_all = []

            applier = get_modifier_applier()
            applier.set_interface_registry(self.interface_registry)

            for flow_id, flow_def in flows.items():
                # このFlowに対するmodifierを取得
                modifiers_for_flow = modifier_loader.get_modifiers_for_flow(flow_id)

                # modifier適用
                if modifiers_for_flow:
                    modified_flow, results = applier.apply_modifiers(flow_def, modifiers_for_flow)
                    modifier_results_all.extend(results)

                    # 適用結果をログ
                    for result in results:
                        if result.success:
                            self.diagnostics.record_step(
                                phase="startup",
                                step_id=f"modifier.apply.{result.modifier_id}",
                                handler="kernel:flow.load_all",
                                status="success",
                                meta={
                                    "action": result.action,
                                    "target_flow": flow_id,
                                    "target_step_id": result.target_step_id
                                }
                            )
                        elif result.skipped_reason:
                            self.diagnostics.record_step(
                                phase="startup",
                                step_id=f"modifier.apply.{result.modifier_id}",
                                handler="kernel:flow.load_all",
                                status="skipped",
                                meta={
                                    "reason": result.skipped_reason,
                                    "target_flow": flow_id
                                }
                            )
                        else:
                            self.diagnostics.record_step(
                                phase="startup",
                                step_id=f"modifier.apply.{result.modifier_id}",
                                handler="kernel:flow.load_all",
                                status="failed",
                                error={"errors": result.errors},
                                meta={"target_flow": flow_id}
                            )

                    final_flow = modified_flow
                    applied_modifiers = [r.modifier_id for r in results if r.success]
                else:
                    final_flow = flow_def
                    applied_modifiers = []

                # 4. IRに登録(1回のみ)
                converted = _flow_converter.convert_flow_def_to_legacy(final_flow)

                # M-8: modifier 適用前のオリジナルを保存
                if modifiers_for_flow and applied_modifiers:
                    original_key = f"flow._original.{flow_id}"
                    original_converted = _flow_converter.convert_flow_def_to_legacy(flow_def)
                    self.interface_registry.register(
                        original_key,
                        original_converted,
                        meta={"_is_original": True, "_flow_id": flow_id},
                    )

                ir_key = f"flow.{flow_id}"
                self.interface_registry.register(ir_key, converted, meta={
                    "_source_file": str(final_flow.source_file) if final_flow.source_file else None,
                    "_source_type": final_flow.source_type,
                    "_flow_loader": True,
                    "_modifiers_applied": applied_modifiers,
                })
                registered.append(flow_id)

            # 5. 完了ログ
            modifier_success = sum(1 for r in modifier_results_all if r.success)
            modifier_skipped = sum(1 for r in modifier_results_all if r.skipped_reason)
            modifier_failed = sum(1 for r in modifier_results_all if not r.success and not r.skipped_reason)

            # スキップ数を取得
            skipped_flows = loader.get_skipped_flows()
            skipped_modifiers = modifier_loader.get_skipped_modifiers()

            # Wave 15-D: structured logging
            _logger.info(
                "Flow load completed",
                flows_registered=len(registered),
                flow_errors=len(flow_errors),
                modifiers_applied=modifier_success,
                modifiers_skipped=modifier_skipped,
                modifiers_failed=modifier_failed,
            )

            # Wave 15-D: metrics
            try:
                _mc = get_metrics_collector()
                _mc.set_gauge("flows.registered", len(registered))
            except Exception:
                pass

            self.diagnostics.record_step(
                phase="startup",
                step_id="flow.load_all.complete",
                handler="kernel:flow.load_all",
                status="success",
                meta={
                    "flows_registered": len(registered),
                    "flow_ids": registered,
                    "flow_errors": len(flow_errors),
                    "modifiers_loaded": len(all_modifiers),
                    "modifiers_applied": modifier_success,
                    "modifiers_skipped": modifier_skipped,
                    "modifiers_failed": modifier_failed,
                    "flows_skipped_count": len(skipped_flows),
                    "modifiers_skipped_count": len(skipped_modifiers),
                }
            )

            # 監査ログに記録
            audit = get_audit_logger()
            audit.log_system_event(
                event_type="flow_load_all",
                success=True,
                details={
                    "flows_registered": len(registered),
                    "flow_ids": registered,
                    "modifiers_loaded": len(all_modifiers),
                    "modifiers_applied": modifier_success,
                    "flows_skipped": len(skipped_flows),
                    "modifiers_skipped": len(skipped_modifiers),
                }
            )

            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {
                    "flows_registered": registered,
                    "flow_error_count": len(flow_errors),
                    "modifiers_loaded": len(all_modifiers),
                    "modifiers_applied": modifier_success,
                    "modifiers_skipped": modifier_skipped,
                    "flows_skipped_count": len(skipped_flows),
                    "modifiers_skipped_by_approval": len(skipped_modifiers),
                }
            }

        except Exception as e:
            _logger.error("Flow load failed", error=str(e), exc_info=True)
            self.diagnostics.record_step(
                phase="startup",
                step_id="flow.load_all.failed",
                handler="kernel:flow.load_all",
                status="failed",
                error=e
            )
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": str(e)}
            }

    def _record_skipped_flows_to_diagnostics(self, loader) -> None:
        """
        スキップされたFlowをdiagnosticsに記録（B4）

        - pack単位でグループ化して記録（必須）
        - RUMI_DIAGNOSTICS_VERBOSE=1 の場合はファイル単位も記録
        """
        skipped_flows = loader.get_skipped_flows()
        if not skipped_flows:
            return

        verbose = _is_diagnostics_verbose()

        # pack_id + reason でグループ化
        by_pack_reason: Dict[Tuple[str, str], List] = {}
        for record in skipped_flows:
            pack_id = record.pack_id or "unknown"
            reason = record.reason or "unknown"
            key = (pack_id, reason)
            if key not in by_pack_reason:
                by_pack_reason[key] = []
            by_pack_reason[key].append(record)

        # pack単位で記録（必須）
        for (pack_id, reason), records in by_pack_reason.items():
            self.diagnostics.record_step(
                phase="startup",
                step_id=f"flow.skipped.{pack_id}",
                handler="kernel:flow.load_all",
                status="skipped",
                meta={
                    "pack_id": pack_id,
                    "reason": reason,
                    "skipped_files_count": len(records),
                }
            )

        # verbose時はファイル単位も記録
        if verbose:
            for record in skipped_flows:
                self.diagnostics.record_step(
                    phase="startup",
                    step_id=f"flow.skipped.file.{Path(record.file_path).stem}",
                    handler="kernel:flow.load_all",
                    status="skipped",
                    meta={
                        "file": record.file_path,
                        "pack_id": record.pack_id,
                        "reason": record.reason,
                        "ts": record.ts,
                    }
                )

    def _record_skipped_modifiers_to_diagnostics(self, modifier_loader) -> None:
        """
        スキップされたmodifierをdiagnosticsに記録（B4）

        - pack単位でグループ化して記録（必須）
        - RUMI_DIAGNOSTICS_VERBOSE=1 の場合はファイル単位も記録
        """
        skipped_modifiers = modifier_loader.get_skipped_modifiers()
        if not skipped_modifiers:
            return

        verbose = _is_diagnostics_verbose()

        # pack_id + reason でグループ化
        by_pack_reason: Dict[Tuple[str, str], List] = {}
        for record in skipped_modifiers:
            pack_id = record.pack_id or "unknown"
            reason = record.reason or "unknown"
            key = (pack_id, reason)
            if key not in by_pack_reason:
                by_pack_reason[key] = []
            by_pack_reason[key].append(record)

        # pack単位で記録（必須）
        for (pack_id, reason), records in by_pack_reason.items():
            self.diagnostics.record_step(
                phase="startup",
                step_id=f"modifier.skipped.{pack_id}",
                handler="kernel:flow.load_all",
                status="skipped",
                meta={
                    "pack_id": pack_id,
                    "reason": reason,
                    "skipped_files_count": len(records),
                }
            )

        # verbose時はファイル単位も記録
        if verbose:
            for record in skipped_modifiers:
                self.diagnostics.record_step(
                    phase="startup",
                    step_id=f"modifier.skipped.file.{Path(record.file_path).stem}",
                    handler="kernel:flow.load_all",
                    status="skipped",
                    meta={
                        "file": record.file_path,
                        "pack_id": record.pack_id,
                        "reason": record.reason,
                        "ts": record.ts,
                    }
                )

    def _convert_new_flow_to_legacy(self, flow_def: FlowDefinition) -> Dict[str, Any]:
        """後方互換ラッパー。FlowConverter に委譲 (M-10)。"""
        return _flow_converter.convert_flow_def_to_legacy(flow_def)


    # ------------------------------------------------------------------
    # flow.execute_by_id
    # ------------------------------------------------------------------

    def _h_flow_execute_by_id(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """
        flow_idを指定してFlowを実行

        Args:
            flow_id: 実行するFlow ID（必須）
            inputs: Flow入力（任意）
            timeout: タイムアウト秒数（任意）
            resolve: 共有辞書で解決するか（任意、デフォルトFalse）
            resolve_namespace: 解決に使用するnamespace（任意、デフォルト"flow_id"）
        """
        flow_id = args.get("flow_id")
        if not flow_id:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "missing flow_id"}}

        inputs = args.get("inputs", {})
        timeout = args.get("timeout")
        resolve = args.get("resolve", False)
        resolve_namespace = args.get("resolve_namespace", "flow_id")

        # 共有辞書での解決（オプトイン）
        original_flow_id = flow_id
        resolved_flow_id = flow_id
        resolution_info = None

        if resolve:
            try:
                from .shared_dict import get_shared_dict_resolver
                resolver = get_shared_dict_resolver()
                result = resolver.resolve_chain(resolve_namespace, flow_id, ctx)
                resolved_flow_id = result.resolved

                resolution_info = {
                    "original": original_flow_id,
                    "resolved": resolved_flow_id,
                    "hops": result.hops,
                    "cycle_detected": result.cycle_detected,
                    "max_hops_reached": result.max_hops_reached,
                }

                # 解決された場合は監査ログに記録
                if resolved_flow_id != original_flow_id:
                    try:
                        from .audit_logger import get_audit_logger
                        audit = get_audit_logger()
                        audit.log_system_event(
                            event_type="flow_id_resolved",
                            success=True,
                            details={
                                "namespace": resolve_namespace,
                                "original": original_flow_id,
                                "resolved": resolved_flow_id,
                                "hops": result.hops,
                            }
                        )
                    except Exception:
                        pass

                    self.diagnostics.record_step(
                        phase="flow",
                        step_id=f"flow.{original_flow_id}.resolved",
                        handler="kernel:flow.execute_by_id",
                        status="success",
                        meta={
                            "original_flow_id": original_flow_id,
                            "resolved_flow_id": resolved_flow_id,
                            "namespace": resolve_namespace,
                        }
                    )
            except Exception as e:
                # 解決失敗時は元のflow_idを使用
                self.diagnostics.record_step(
                    phase="flow",
                    step_id=f"flow.{original_flow_id}.resolve_failed",
                    handler="kernel:flow.execute_by_id",
                    status="failed",
                    error=e,
                    meta={"namespace": resolve_namespace}
                )

        # Flow実行
        exec_ctx = dict(ctx)
        exec_ctx.update(inputs)

        if resolution_info:
            exec_ctx["_flow_resolution"] = resolution_info

        flow_result = self.execute_flow_sync(resolved_flow_id, exec_ctx, timeout)

        return {
            "_kernel_step_status": "success" if "_error" not in flow_result else "failed",
            "_kernel_step_meta": {
                "flow_id": resolved_flow_id,
                "original_flow_id": original_flow_id if resolve else None,
                "resolved": resolve and (resolved_flow_id != original_flow_id),
            },
            "result": flow_result
        }

    # ------------------------------------------------------------------
    # python_file_call
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # universal_call
    # ------------------------------------------------------------------

    def _h_universal_call(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """kernel:universal_call handler — delegates to flow execution mixin."""
        import asyncio

        # Build step dict from args
        step = {
            "id": args.get("_step_id", "universal_call"),
            "type": "universal_call",
            "owner_pack": args.get("owner_pack", ""),
            "file": args.get("file", ""),
            "runtime": args.get("runtime", "python"),
            "protocol": args.get("protocol", "stdio_json"),
            "docker_image": args.get("docker_image"),
            "timeout_seconds": args.get("timeout_seconds", 30.0),
            "input": args.get("input", {}),
            "output": args.get("output", ""),
            "principal_id": args.get("principal_id", ""),
        }

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self._handle_universal_call_async(step, ctx), loop,
            )
            return future.result(timeout=120.0)
        else:
            return asyncio.run(
                self._handle_universal_call_async(step, ctx)
            )

    def _h_python_file_call(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """
        python_file_call ステップを実行

        UDS Egress Proxy連携:
        - strict モードでは実行前にUDSソケットを確保
        - コンテナは --network=none で実行
        - 外部通信は UDS 経由でのみ可能

        Args (from args):
            file: 実行するファイルパス(必須)
            owner_pack: 所有Pack ID(任意、パスから推測可能)
            input: 入力データ(任意)
            timeout_seconds: タイムアウト秒数(任意、デフォルト60)
            _step_id: ステップID(内部用)
            _phase: フェーズ名(内部用)
        """
        from .python_file_executor import get_python_file_executor, ExecutionContext

        file_path = args.get("file")
        if not file_path:
            self.diagnostics.record_step(
                phase=args.get("_phase", "flow"),
                step_id=args.get("_step_id", "unknown"),
                handler="kernel:python_file_call",
                status="failed",
                error={"type": "missing_file", "message": "No 'file' specified"}
            )
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": "No 'file' specified"}
            }

        owner_pack = args.get("owner_pack")
        principal_id = args.get("principal_id")
        input_data = args.get("input", {})
        timeout_seconds = args.get("timeout_seconds", 60.0)
        step_id = args.get("_step_id", "unknown")
        phase = args.get("_phase", "flow")

        # 入力データの変数解決
        resolved_input = self._resolve_value(input_data, ctx)

        # UDSプロキシマネージャを取得（strictモードでは必須）
        security_mode = os.environ.get("RUMI_SECURITY_MODE", "strict").lower()
        uds_manager = self._get_uds_proxy_manager()

        # Capability Proxy: principal_id 用のソケットを確保
        effective_principal = owner_pack  # v1 principal enforcement: always owner_pack
        capability_sock_path = None
        if effective_principal:
            cap_proxy = self._get_capability_proxy()
            if cap_proxy:
                cap_ok, cap_err, cap_sock = cap_proxy.ensure_principal_socket(effective_principal)
                if cap_ok:
                    capability_sock_path = cap_sock
                else:
                    self.diagnostics.record_step(
                        phase=phase, step_id=f"{step_id}.capability_socket_warning",
                        handler="kernel:python_file_call", status="success",
                        meta={"warning": f"Failed to ensure capability socket: {cap_err}"}
                    )

        if owner_pack and uds_manager:
            # UDSソケットを確保（Docker実行前に必須）
            success, error, sock_path = uds_manager.ensure_pack_socket(owner_pack)
            if not success:
                if security_mode == "strict":
                    self.diagnostics.record_step(
                        phase=phase,
                        step_id=f"{step_id}.socket_ensure_failed",
                        handler="kernel:python_file_call",
                        status="failed",
                        error={"type": "socket_ensure_failed", "message": error}
                    )
                    return {
                        "_kernel_step_status": "failed",
                        "_kernel_step_meta": {"error": f"Failed to ensure UDS socket: {error}"}
                    }
                else:
                    # permissive モードでは警告のみ
                    self.diagnostics.record_step(
                        phase=phase,
                        step_id=f"{step_id}.socket_ensure_warning",
                        handler="kernel:python_file_call",
                        status="success",
                        meta={"warning": f"Failed to ensure UDS socket: {error}"}
                    )

        # 実行コンテキストを構築
        exec_context = ExecutionContext(
            flow_id=ctx.get("_flow_id", "unknown"),
            step_id=step_id,
            phase=phase,
            ts=self._now_ts(),
            owner_pack=owner_pack,
            principal_id=principal_id,
            capability_sock_path=capability_sock_path,
            inputs=resolved_input,
            diagnostics_callback=lambda data: self.diagnostics.record_step(
                phase=data.get("phase", phase),
                step_id=f"{step_id}.{data.get('type', 'event')}",
                handler="kernel:python_file_call",
                status="failed" if "error" in data else "success",
                error=data.get("error"),
                meta=data
            )
        )

        # Wave 15-D: log execution start
        _logger.info(
            "python_file_call start",
            file=file_path, owner_pack=owner_pack, step_id=step_id,
        )

        # 実行
        executor = get_python_file_executor()

        # UDSプロキシマネージャを設定
        if uds_manager:
            executor.set_uds_proxy_manager(uds_manager)

        result = executor.execute(
            file_path=file_path,
            owner_pack=owner_pack,
            input_data=resolved_input,
            context=exec_context,
            principal_id=effective_principal,
            capability_sock_path=capability_sock_path,
            timeout_seconds=timeout_seconds,
        )

        # 結果を記録
        status: Literal["success", "failed"] = (
            "success" if result.success else "failed"
        )
        self.diagnostics.record_step(
            phase=phase,
            step_id=step_id,
            handler="kernel:python_file_call",
            status=status,
            error={"type": result.error_type, "message": result.error} if result.error else None,
            meta={
                "file": file_path,
                "owner_pack": owner_pack,
                "execution_mode": result.execution_mode,
                "execution_time_ms": result.execution_time_ms,
                "warnings": result.warnings if result.warnings else None,
            }
        )

        # Wave 15-D: metrics
        try:
            _mc = get_metrics_collector()
            if result.execution_time_ms is not None:
                _mc.observe("python_file_call.duration_ms", result.execution_time_ms)
        except Exception:
            pass

        # 警告をログ出力
        for warning in result.warnings:
            print(f"[python_file_call] WARNING: {warning}", file=sys.stderr)

        if result.success:
            _logger.info(
                "python_file_call completed",
                file=file_path, execution_time_ms=result.execution_time_ms,
                execution_mode=result.execution_mode,
            )
            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {
                    "execution_mode": result.execution_mode,
                    "execution_time_ms": result.execution_time_ms,
                },
                "output": result.output
            }
        else:
            _logger.error(
                "python_file_call failed",
                file=file_path, error=result.error,
                error_type=result.error_type,
            )
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {
                    "error": result.error,
                    "error_type": result.error_type,
                    "execution_mode": result.execution_mode,
                }
            }

    # ------------------------------------------------------------------
    # modifier.load_all / modifier.apply
    # ------------------------------------------------------------------

    def _h_modifier_load_all(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """全modifierファイルをロード(単独実行用)"""
        try:
            from .flow_modifier import get_modifier_loader
            loader = get_modifier_loader()
            modifiers = loader.load_all_modifiers()

            errors = loader.get_load_errors()
            for err in errors:
                self.diagnostics.record_step(
                    phase="startup",
                    step_id="modifier.load.error",
                    handler="kernel:modifier.load_all",
                    status="failed",
                    error={"errors": err.get("errors", [])},
                    meta={"file": err.get("file")}
                )

            self.diagnostics.record_step(
                phase="startup",
                step_id="modifier.load_all.complete",
                handler="kernel:modifier.load_all",
                status="success",
                meta={
                    "loaded_count": len(modifiers),
                    "modifier_ids": list(modifiers.keys()),
                    "error_count": len(errors)
                }
            )

            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {
                    "loaded": list(modifiers.keys()),
                    "error_count": len(errors)
                }
            }
        except Exception as e:
            self.diagnostics.record_step(
                phase="startup",
                step_id="modifier.load_all.failed",
                handler="kernel:modifier.load_all",
                status="failed",
                error=e
            )
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": str(e)}
            }

    def _h_modifier_apply(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """指定Flowにmodifierを再適用(単独実行用)"""
        try:
            from .flow_loader import get_flow_loader
            from .flow_modifier import get_modifier_loader, get_modifier_applier

            target_flow_id = args.get("flow_id")

            flow_loader = get_flow_loader()
            modifier_loader = get_modifier_loader()
            applier = get_modifier_applier()
            applier.set_interface_registry(self.interface_registry)

            flows = flow_loader.get_loaded_flows()
            all_results = []

            for flow_id, flow_def in flows.items():
                if target_flow_id and flow_id != target_flow_id:
                    continue

                modifiers = modifier_loader.get_modifiers_for_flow(flow_id)
                if not modifiers:
                    continue

                modified_flow, results = applier.apply_modifiers(flow_def, modifiers)
                all_results.extend(results)

                # IRを更新
                converted = _flow_converter.convert_flow_def_to_legacy(modified_flow)
                self.interface_registry.register(f"flow.{flow_id}", converted, meta={
                    "_source_file": str(modified_flow.source_file) if modified_flow.source_file else None,
                    "_source_type": modified_flow.source_type,
                    "_flow_loader": True,
                    "_modifiers_applied": [r.modifier_id for r in results if r.success],
                })

            success_count = sum(1 for r in all_results if r.success)
            skip_count = sum(1 for r in all_results if r.skipped_reason)
            fail_count = sum(1 for r in all_results if not r.success and not r.skipped_reason)

            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {
                    "success_count": success_count,
                    "skip_count": skip_count,
                    "fail_count": fail_count
                }
            }
        except Exception as e:
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": str(e)}
            }

    # ------------------------------------------------------------------
    # network.grant / revoke / check / list
    # ------------------------------------------------------------------

    def _h_network_grant(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """ネットワークアクセスを許可"""
        pack_id = args.get("pack_id")
        if not pack_id:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "Missing pack_id"}}

        allowed_domains = args.get("allowed_domains", [])
        allowed_ports = args.get("allowed_ports", [])

        if not allowed_domains and not allowed_ports:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "Must specify allowed_domains or allowed_ports"}}

        try:
            from .network_grant_manager import get_network_grant_manager
            ngm = get_network_grant_manager()
            grant = ngm.grant_network_access(
                pack_id=pack_id,
                allowed_domains=allowed_domains,
                allowed_ports=allowed_ports,
                granted_by=args.get("granted_by", "kernel"),
                notes=args.get("notes", "")
            )

            # Wave 15-D: log + metrics
            _logger.info(
                "Network access granted",
                pack_id=pack_id, allowed_domains=allowed_domains,
                allowed_ports=allowed_ports,
            )
            try:
                get_metrics_collector().increment("network.grant.count")
            except Exception:
                pass

            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {"pack_id": pack_id},
                "grant": grant.to_dict()
            }
        except Exception as e:
            _logger.error("Network grant failed", pack_id=pack_id, error=str(e))
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_network_revoke(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """ネットワークアクセスを取り消し"""
        pack_id = args.get("pack_id")
        if not pack_id:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "Missing pack_id"}}

        try:
            from .network_grant_manager import get_network_grant_manager
            ngm = get_network_grant_manager()
            success = ngm.revoke_network_access(pack_id=pack_id, reason=args.get("reason", ""))

            # Wave 15-D: log + metrics
            _logger.info("Network access revoked", pack_id=pack_id, revoked=success)
            try:
                get_metrics_collector().increment("network.revoke.count")
            except Exception:
                pass

            return {
                "_kernel_step_status": "success" if success else "failed",
                "_kernel_step_meta": {"pack_id": pack_id, "revoked": success}
            }
        except Exception as e:
            _logger.error("Network revoke failed", pack_id=pack_id, error=str(e))
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_network_check(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """ネットワークアクセスをチェック"""
        pack_id = args.get("pack_id")
        domain = args.get("domain")
        port = args.get("port")

        if not pack_id or not domain or port is None:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "Missing pack_id, domain, or port"}}

        try:
            from .network_grant_manager import get_network_grant_manager
            ngm = get_network_grant_manager()
            result = ngm.check_access(pack_id, domain, int(port))
            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {"allowed": result.allowed, "reason": result.reason},
                "result": {"allowed": result.allowed, "reason": result.reason, "pack_id": result.pack_id, "domain": result.domain, "port": result.port}
            }
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_network_list(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """全ネットワークGrantを一覧"""
        try:
            from .network_grant_manager import get_network_grant_manager
            ngm = get_network_grant_manager()
            grants = ngm.get_all_grants()
            disabled = ngm.get_disabled_packs()
            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {"grant_count": len(grants), "disabled_count": len(disabled)},
                "grants": {k: v.to_dict() for k, v in grants.items()},
                "disabled_packs": list(disabled)
            }
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    # ------------------------------------------------------------------
    # egress_proxy.start / stop / status
    # ------------------------------------------------------------------

    def _h_egress_proxy_start(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """Egress Proxyを起動"""
        try:
            from .network_grant_manager import get_network_grant_manager
            from .audit_logger import get_audit_logger
            from .egress_proxy import EgressProxyServer, initialize_egress_proxy
            ngm = get_network_grant_manager()
            audit = get_audit_logger()
            raw_host = args.get("host")
            host = (
                raw_host
                if isinstance(raw_host, str)
                else EgressProxyServer.DEFAULT_HOST
            )
            raw_port = args.get("port")
            port = (
                raw_port
                if isinstance(raw_port, int)
                else EgressProxyServer.DEFAULT_PORT
            )
            proxy = initialize_egress_proxy(
                host=host, port=port,
                network_grant_manager=ngm, audit_logger=audit, auto_start=True
            )
            if proxy.is_running():
                return {"_kernel_step_status": "success", "_kernel_step_meta": {"endpoint": proxy.get_endpoint(), "running": True}}
            else:
                return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "Failed to start proxy"}}
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_egress_proxy_stop(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """Egress Proxyを停止"""
        try:
            from .egress_proxy import shutdown_egress_proxy
            shutdown_egress_proxy()
            return {"_kernel_step_status": "success"}
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_egress_proxy_status(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """Egress Proxyの状態を取得"""
        try:
            from .egress_proxy import get_egress_proxy
            proxy = get_egress_proxy()
            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {"running": proxy.is_running(), "endpoint": proxy.get_endpoint() if proxy.is_running() else None}
            }
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    # ------------------------------------------------------------------
    # lib.process_all / check / execute / clear_record / list_records
    # ------------------------------------------------------------------

    def _h_lib_process_all(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """全Packのlibを処理"""
        try:
            from .lib_executor import get_lib_executor
            packs_dir = Path(args.get("packs_dir", "ecosystem"))
            executor = get_lib_executor()
            results = executor.process_all_packs(packs_dir, ctx)
            return {"_kernel_step_status": "success", "_kernel_step_meta": {"installed": results["installed"], "updated": results["updated"], "failed_count": len(results["failed"])}, "results": results}
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_lib_check(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """Packのlib実行が必要かチェック"""
        pack_id = args.get("pack_id")
        if not pack_id:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "Missing pack_id"}}
        pack_dir = Path(args.get("pack_dir", f"{ECOSYSTEM_DIR}/{pack_id}"))
        try:
            from .lib_executor import get_lib_executor
            executor = get_lib_executor()
            result = executor.check_pack(pack_id, pack_dir)
            return {"_kernel_step_status": "success", "_kernel_step_meta": {"needs_install": result.needs_install, "needs_update": result.needs_update, "reason": result.reason}}
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_lib_execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """Packのlibを手動実行"""
        pack_id = args.get("pack_id")
        lib_type = args.get("lib_type")
        if not pack_id or not lib_type:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "Missing pack_id or lib_type"}}
        if lib_type not in ("install", "update"):
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "lib_type must be 'install' or 'update'"}}
        pack_dir = Path(args.get("pack_dir", f"{ECOSYSTEM_DIR}/{pack_id}"))
        try:
            from .lib_executor import get_lib_executor
            executor = get_lib_executor()
            lib_dir = pack_dir / "backend" / "lib"
            if not lib_dir.exists():
                lib_dir = pack_dir / "lib"
            lib_file = lib_dir / f"{lib_type}.py"
            if not lib_file.exists():
                return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": f"File not found: {lib_file}"}}
            result = executor.execute_lib(pack_id, lib_file, lib_type, ctx)
            return {"_kernel_step_status": "success" if result.success else "failed", "_kernel_step_meta": {"pack_id": pack_id, "lib_type": lib_type, "success": result.success, "error": result.error}, "output": result.output}
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_lib_clear_record(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """lib実行記録をクリア"""
        try:
            from .lib_executor import get_lib_executor
            executor = get_lib_executor()
            pack_id = args.get("pack_id")
            if pack_id:
                success = executor.clear_record(pack_id)
                return {"_kernel_step_status": "success" if success else "failed", "_kernel_step_meta": {"pack_id": pack_id, "cleared": success}}
            else:
                count = executor.clear_all_records()
                return {"_kernel_step_status": "success", "_kernel_step_meta": {"cleared_count": count}}
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_lib_list_records(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """lib実行記録を一覧"""
        try:
            from .lib_executor import get_lib_executor
            executor = get_lib_executor()
            records = executor.get_all_records()
            return {"_kernel_step_status": "success", "_kernel_step_meta": {"count": len(records)}, "records": {pack_id: record.to_dict() for pack_id, record in records.items()}}
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    # ------------------------------------------------------------------
    # audit.query / summary / flush
    # ------------------------------------------------------------------

    def _h_audit_query(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """監査ログを検索"""
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            results = audit.query_logs(
                category=args.get("category"),
                start_date=args.get("start_date"),
                end_date=args.get("end_date"),
                pack_id=args.get("pack_id"),
                flow_id=args.get("flow_id"),
                success_only=args.get("success_only"),
                limit=args.get("limit", 100)
            )

            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {"count": len(results)},
                "results": results
            }
        except Exception as e:
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": str(e)}
            }

    def _h_audit_summary(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """監査ログのサマリーを取得"""
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            summary = audit.get_summary(
                category=args.get("category"),
                date=args.get("date")
            )

            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": summary,
                "summary": summary
            }
        except Exception as e:
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": str(e)}
            }

    def _h_audit_flush(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """監査ログバッファをフラッシュ"""
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            audit.flush()

            return {"_kernel_step_status": "success"}
        except Exception as e:
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": str(e)}
            }

    # ------------------------------------------------------------------
    # vocab.list_groups / list_converters / summary / convert
    # ------------------------------------------------------------------

    def _h_vocab_list_groups(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """登録された同義語グループを一覧"""
        try:
            from .vocab_registry import get_vocab_registry
            vr = get_vocab_registry()
            groups = vr.list_groups()
            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {"count": len(groups)},
                "groups": groups
            }
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_vocab_list_converters(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """登録されたconverterを一覧"""
        try:
            from .vocab_registry import get_vocab_registry
            vr = get_vocab_registry()
            converters = vr.list_converters()
            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {"count": len(converters)},
                "converters": converters
            }
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_vocab_summary(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """vocab/converterの登録状況サマリーを取得"""
        try:
            from .vocab_registry import get_vocab_registry
            vr = get_vocab_registry()
            summary = vr.get_registration_summary()
            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": summary.get("totals", {}),
                "summary": summary
            }
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_vocab_convert(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """データを変換"""
        from_term = args.get("from_term")
        to_term = args.get("to_term")
        data = args.get("data")
        log_success = args.get("log_success", False)

        if not from_term or not to_term:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "Missing from_term or to_term"}}

        try:
            from .vocab_registry import get_vocab_registry
            vr = get_vocab_registry()
            result, success = vr.convert(from_term, to_term, data, log_success=log_success)
            return {
                "_kernel_step_status": "success" if success else "failed",
                "_kernel_step_meta": {"converted": success, "from": from_term, "to": to_term},
                "result": result
            }
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    # ------------------------------------------------------------------
    # shared_dict.resolve / propose / explain / list / remove
    # ------------------------------------------------------------------

    def _h_shared_dict_resolve(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """共有辞書でtokenを解決"""
        namespace = args.get("namespace")
        token = args.get("token")

        if not namespace or not token:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "Missing namespace or token"}}

        try:
            from .shared_dict import get_shared_dict_resolver
            resolver = get_shared_dict_resolver()
            result = resolver.resolve_chain(namespace, token, ctx)

            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {
                    "original": result.original,
                    "resolved": result.resolved,
                    "hop_count": len(result.hops),
                    "cycle_detected": result.cycle_detected,
                    "max_hops_reached": result.max_hops_reached,
                },
                "resolved": result.resolved,
                "hops": result.hops,
            }
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_shared_dict_propose(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """共有辞書にルールを提案"""
        namespace = args.get("namespace")
        token = args.get("token")
        value = args.get("value")
        provenance = args.get("provenance", {})

        if not namespace or not token or value is None:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "Missing namespace, token, or value"}}

        try:
            from .shared_dict import get_shared_dict_journal
            journal = get_shared_dict_journal()
            result = journal.propose(namespace, token, value, provenance)

            return {
                "_kernel_step_status": "success" if result.accepted else "failed",
                "_kernel_step_meta": {
                    "status": result.status.value,
                    "accepted": result.accepted,
                    "reason": result.reason,
                },
                "result": {
                    "status": result.status.value,
                    "namespace": result.namespace,
                    "token": result.token,
                    "value": result.value,
                    "reason": result.reason,
                }
            }
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_shared_dict_explain(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """共有辞書の解決を説明"""
        namespace = args.get("namespace")
        token = args.get("token")

        if not namespace or not token:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "Missing namespace or token"}}

        try:
            from .shared_dict import get_shared_dict_resolver
            resolver = get_shared_dict_resolver()
            result = resolver.explain(namespace, token, ctx)

            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {
                    "original": result.original,
                    "resolved": result.resolved,
                    "hop_count": len(result.hops),
                },
                "explanation": {
                    "original": result.original,
                    "resolved": result.resolved,
                    "hops": result.hops,
                    "cycle_detected": result.cycle_detected,
                    "max_hops_reached": result.max_hops_reached,
                }
            }
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_shared_dict_list(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """共有辞書のnamespace/ルールを一覧"""
        namespace = args.get("namespace")

        try:
            from .shared_dict import get_shared_dict_resolver
            resolver = get_shared_dict_resolver()

            if namespace:
                rules = resolver.list_rules(namespace)
                return {
                    "_kernel_step_status": "success",
                    "_kernel_step_meta": {"namespace": namespace, "rule_count": len(rules)},
                    "rules": rules,
                }
            else:
                namespaces = resolver.list_namespaces()
                return {
                    "_kernel_step_status": "success",
                    "_kernel_step_meta": {"namespace_count": len(namespaces)},
                    "namespaces": namespaces,
                }
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_shared_dict_remove(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """共有辞書からルールを削除"""
        namespace = args.get("namespace")
        token = args.get("token")
        provenance = args.get("provenance", {})

        if not namespace or not token:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "Missing namespace or token"}}

        try:
            from .shared_dict import get_shared_dict_journal
            journal = get_shared_dict_journal()
            success = journal.remove(namespace, token, provenance)

            return {
                "_kernel_step_status": "success" if success else "failed",
                "_kernel_step_meta": {"removed": success, "namespace": namespace, "token": token},
            }
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    # ------------------------------------------------------------------
    # UDS Egress Proxy ハンドラ
    # ------------------------------------------------------------------

    def _h_uds_proxy_init(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """UDS Egress Proxyを初期化"""
        try:
            from .egress_proxy import initialize_uds_egress_proxy
            from .network_grant_manager import get_network_grant_manager
            from .audit_logger import get_audit_logger

            ngm = get_network_grant_manager()
            audit = get_audit_logger()

            self._uds_proxy_manager = initialize_uds_egress_proxy(
                network_grant_manager=ngm,
                audit_logger=audit
            )

            self.diagnostics.record_step(
                phase="startup",
                step_id="uds_proxy.init",
                handler="kernel:uds_proxy.init",
                status="success",
                meta={"base_dir": str(self._uds_proxy_manager.get_base_dir())}
            )

            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {
                    "base_dir": str(self._uds_proxy_manager.get_base_dir())
                }
            }
        except Exception as e:
            self.diagnostics.record_step(
                phase="startup",
                step_id="uds_proxy.init",
                handler="kernel:uds_proxy.init",
                status="failed",
                error=e
            )
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": str(e)}
            }

    def _h_uds_proxy_ensure_socket(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """Pack用UDSソケットを確保"""
        pack_id = args.get("pack_id")
        if not pack_id:
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": "Missing pack_id"}
            }

        uds_manager = self._get_uds_proxy_manager()
        if not uds_manager:
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": "UDS proxy manager not available"}
            }

        success, error, sock_path = uds_manager.ensure_pack_socket(pack_id)

        if success:
            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {
                    "pack_id": pack_id,
                    "socket_path": str(sock_path)
                }
            }
        else:
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {
                    "pack_id": pack_id,
                    "error": error
                }
            }

    def _h_uds_proxy_stop(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """Pack用UDSサーバーを停止"""
        pack_id = args.get("pack_id")
        if not pack_id:
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": "Missing pack_id"}
            }

        uds_manager = self._get_uds_proxy_manager()
        if not uds_manager:
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": "UDS proxy manager not available"}
            }

        uds_manager.stop_pack_server(pack_id)

        return {
            "_kernel_step_status": "success",
            "_kernel_step_meta": {"pack_id": pack_id, "stopped": True}
        }

    def _h_uds_proxy_stop_all(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """全UDSサーバーを停止"""
        uds_manager = self._get_uds_proxy_manager()
        if not uds_manager:
            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {"stopped": 0}
            }

        active_packs = uds_manager.list_active_packs()
        uds_manager.stop_all()

        return {
            "_kernel_step_status": "success",
            "_kernel_step_meta": {"stopped": len(active_packs), "packs": active_packs}
        }

    def _h_uds_proxy_status(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """UDSプロキシの状態を取得"""
        uds_manager = self._get_uds_proxy_manager()
        if not uds_manager:
            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {
                    "initialized": False,
                    "active_packs": []
                }
            }

        pack_id = args.get("pack_id")

        if pack_id:
            is_running = uds_manager.is_running(pack_id)
            sock_path = uds_manager.get_socket_path(pack_id)
            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {
                    "pack_id": pack_id,
                    "is_running": is_running,
                    "socket_path": str(sock_path) if sock_path else None
                }
            }
        else:
            active_packs = uds_manager.list_active_packs()
            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {
                    "initialized": True,
                    "active_packs": active_packs,
                    "base_dir": str(uds_manager.get_base_dir())
                }
            }

    # ------------------------------------------------------------------
    # Capability Proxy ハンドラ
    # ------------------------------------------------------------------

    def _h_capability_proxy_init(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """Capability Proxy を初期化"""
        try:
            proxy = self._get_capability_proxy()
            if proxy and proxy._initialized:
                self.diagnostics.record_step(
                    phase="startup", step_id="capability_proxy.init",
                    handler="kernel:capability_proxy.init", status="success",
                    meta={"base_dir": str(proxy.get_base_dir())}
                )
                return {"_kernel_step_status": "success", "_kernel_step_meta": {"base_dir": str(proxy.get_base_dir())}}
            else:
                return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "Capability proxy initialization failed"}}
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_capability_proxy_status(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """Capability Proxy のステータスを取得"""
        proxy = self._capability_proxy
        if proxy is None:
            return {"_kernel_step_status": "success", "_kernel_step_meta": {"initialized": False}}
        return {"_kernel_step_status": "success", "_kernel_step_meta": proxy.status()}

    def _h_capability_proxy_stop_all(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """全 Capability サーバーを停止"""
        proxy = self._capability_proxy
        if proxy:
            active = proxy.list_active_principals()
            proxy.stop_all()
            return {"_kernel_step_status": "success", "_kernel_step_meta": {"stopped": len(active), "principals": active}}
        return {"_kernel_step_status": "success", "_kernel_step_meta": {"stopped": 0}}


    # ------------------------------------------------------------------
    # Capability Grant ハンドラ (G-1)
    # ------------------------------------------------------------------

    def _h_capability_grant(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """Capability Grant を付与"""
        principal_id = args.get("principal_id")
        permission_id = args.get("permission_id")
        config = args.get("config")

        if not principal_id or not permission_id:
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": "Missing principal_id or permission_id"},
            }

        try:
            from .capability_grant_manager import get_capability_grant_manager
            gm = get_capability_grant_manager()
            gm.grant_permission(principal_id, permission_id, config)

            try:
                from .audit_logger import get_audit_logger
                audit = get_audit_logger()
                audit.log_permission_event(
                    pack_id=principal_id,
                    permission_type="capability_grant",
                    action="grant",
                    success=True,
                    details={
                        "principal_id": principal_id,
                        "permission_id": permission_id,
                        "has_config": config is not None,
                    },
                )
            except Exception:
                pass

            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {
                    "principal_id": principal_id,
                    "permission_id": permission_id,
                    "granted": True,
                },
            }
        except Exception as e:
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": str(e)},
            }

    def _h_capability_revoke(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """Capability Grant を取り消し"""
        principal_id = args.get("principal_id")
        permission_id = args.get("permission_id")

        if not principal_id or not permission_id:
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": "Missing principal_id or permission_id"},
            }

        try:
            from .capability_grant_manager import get_capability_grant_manager
            gm = get_capability_grant_manager()
            gm.revoke_permission(principal_id, permission_id)

            try:
                from .audit_logger import get_audit_logger
                audit = get_audit_logger()
                audit.log_permission_event(
                    pack_id=principal_id,
                    permission_type="capability_grant",
                    action="revoke",
                    success=True,
                    details={
                        "principal_id": principal_id,
                        "permission_id": permission_id,
                    },
                )
            except Exception:
                pass

            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {
                    "principal_id": principal_id,
                    "permission_id": permission_id,
                    "revoked": True,
                },
            }
        except Exception as e:
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": str(e)},
            }

    def _h_capability_list(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """Capability Grant を一覧"""
        principal_id = args.get("principal_id")

        try:
            from .capability_grant_manager import get_capability_grant_manager
            gm = get_capability_grant_manager()

            if principal_id:
                grant = gm.get_grant(principal_id)
                if grant is None:
                    return {
                        "_kernel_step_status": "success",
                        "_kernel_step_meta": {"principal_id": principal_id, "found": False},
                        "grant": None,
                    }
                return {
                    "_kernel_step_status": "success",
                    "_kernel_step_meta": {"principal_id": principal_id, "found": True},
                    "grant": grant.to_dict() if hasattr(grant, "to_dict") else grant,
                }
            else:
                all_grants = gm.get_all_grants()
                result_grants = {}
                for pid, g in all_grants.items():
                    result_grants[pid] = g.to_dict() if hasattr(g, "to_dict") else g
                return {
                    "_kernel_step_status": "success",
                    "_kernel_step_meta": {"grant_count": len(result_grants)},
                    "grants": result_grants,
                }
        except Exception as e:
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": str(e)},
            }

    # ------------------------------------------------------------------
    # Pending Export ハンドラ (G-2)
    # ------------------------------------------------------------------

    def _h_pending_export(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """
        承認待ち状況を user_data/pending/summary.json に書き出す。
        個別モジュールが import できなくても取れた範囲だけ書く (fail-soft)。
        """
        import json as _json
        from datetime import datetime, timezone
        from pathlib import Path as _Path

        output_dir = _Path(args.get("output_dir", "user_data/pending"))
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / "summary.json"

        summary: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "version": "1.0",
            "packs": {},
            "capability": {},
            "pip": {},
        }

        # Packs
        try:
            from .approval_manager import get_approval_manager, PackStatus
            am = get_approval_manager()
            all_packs = am.scan_packs() if hasattr(am, "scan_packs") else []
            pending_packs = [p for p in all_packs if am.get_status(p) in (PackStatus.INSTALLED, PackStatus.PENDING)] if all_packs else []
            modified_packs = [p for p in all_packs if am.get_status(p) == PackStatus.MODIFIED] if all_packs else []
            blocked_packs = [p for p in all_packs if am.get_status(p) == PackStatus.BLOCKED] if all_packs else []
            summary["packs"] = {
                "pending_count": len(pending_packs),
                "pending_ids": pending_packs,
                "modified_count": len(modified_packs),
                "modified_ids": modified_packs,
                "blocked_count": len(blocked_packs),
                "blocked_ids": blocked_packs,
            }
        except Exception as e:
            summary["packs"] = {"error": str(e)}

        # Capability requests
        try:
            from .capability_installer import get_capability_installer
            installer = get_capability_installer()
            for status_name in ("pending", "rejected", "blocked", "failed", "installed"):
                items = installer.list_items(status_name)
                summary["capability"][f"{status_name}_count"] = len(items)
        except Exception as e:
            summary["capability"] = {"error": str(e)}

        # Pip requests
        try:
            from .pip_installer import get_pip_installer
            pip_inst = get_pip_installer()
            for status_name in ("pending", "rejected", "blocked", "failed", "installed"):
                items = pip_inst.list_items(status_name)
                summary["pip"][f"{status_name}_count"] = len(items)
        except Exception as e:
            summary["pip"] = {"error": str(e)}

        # Write
        try:
            with open(output_file, "w", encoding="utf-8") as f:
                _json.dump(summary, f, ensure_ascii=False, indent=2)
        except Exception as e:
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": f"Failed to write summary: {e}"},
            }

        return {
            "_kernel_step_status": "success",
            "_kernel_step_meta": {
                "output_file": str(output_file),
                "packs_pending": summary.get("packs", {}).get("pending_count", 0),
            },
        }

    # ------------------------------------------------------------------
    # desktop handlers (Phase V-4)
    # ------------------------------------------------------------------

    def _desktop_principal_id(self, ctx: Dict[str, Any]) -> str:
        """Return the authenticated principal for desktop actions, failing closed."""
        principal_id = ctx.get("_principal_id") or ctx.get("principal_id")
        return principal_id if isinstance(principal_id, str) else ""

    def _h_desktop_action(self, action: str, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """Run desktop actions only through the grant-aware capability path."""
        pack_id = args.get("pack_id", "")
        if not pack_id:
            return {"success": False, "error": "Missing pack_id"}

        principal_id = self._desktop_principal_id(ctx)
        if not principal_id:
            return {"success": False, "error": "Missing principal_id"}

        try:
            from .capability_grant_manager import get_capability_grant_manager
            from .di_container import get_container

            grant_result = get_capability_grant_manager().check(
                principal_id,
                "desktop_app.execute",
            )
            if not grant_result.allowed:
                return {"success": False, "error": grant_result.reason}

            handler = get_container().get_or_none("desktop_capability_handler")
            if handler is None:
                return {"success": False, "error": "Desktop capability handler not available"}

            capability_args = dict(args)
            capability_args["action"] = action
            result = handler.handle_execute(
                principal_id=principal_id,
                args=capability_args,
                grant_config=grant_result.config or {},
            )
            if "error" in result:
                return {"success": False, "error": result["error"]}

            app_result = result.get("app", {})
            return {"success": app_result.get("success", False), "data": app_result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _h_desktop_launch(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """kernel:desktop.launch — Pack のデスクトップアプリを起動する。"""
        return self._h_desktop_action("launch", args, ctx)

    def _h_desktop_stop(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """kernel:desktop.stop — Pack のデスクトップアプリを停止する。"""
        return self._h_desktop_action("stop", args, ctx)
