import { Component, createRef, type ErrorInfo, type ReactNode } from "react";

import { diagnosticFingerprint, reportClientDiagnosticResult, sanitizeDiagnosticDetail } from "../lib/clientDiagnostics";
import { crashDraftExport, recoverableDraftSnapshot, recordCrash, resetAffectedClientState, type CrashDraftSnapshot } from "../lib/crashRecovery";
import { ErrorNotice } from "./ErrorNotice";

type Props = { children: ReactNode };
type DiagnosticStatus = "idle" | "sending" | "recorded" | "not_recorded";
type State = {
  failed: boolean;
  diagnosticStatus: DiagnosticStatus;
  diagnosticReference: string;
  safeDetails: string;
  draft: CrashDraftSnapshot | null;
  crashCount: number;
};

export class AppErrorBoundary extends Component<Props, State> {
  private headingRef = createRef<HTMLHeadingElement>();
  state: State = { failed: false, diagnosticStatus: "idle", diagnosticReference: "", safeDetails: "", draft: null, crashCount: 0 };

  static getDerivedStateFromError(error: Error): Partial<State> {
    const fingerprint = diagnosticFingerprint({ source: "react.error_boundary", category: "render_crash", message: "React render failure", detail: { name: error.name, stack: error.stack } });
    return {
      failed: true,
      diagnosticReference: fingerprint,
      safeDetails: JSON.stringify(sanitizeDiagnosticDetail({ name: error.name, stack: error.stack }), null, 2),
      draft: recoverableDraftSnapshot(typeof localStorage === "undefined" ? null : localStorage),
      crashCount: recordCrash(typeof sessionStorage === "undefined" ? null : sessionStorage),
    };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    this.setState({ diagnosticStatus: "sending" });
    void reportClientDiagnosticResult({
      source: "react.error_boundary", category: "render_crash", level: "error",
      message: "React renderer crashed",
      detail: { name: error.name, stack: error.stack, componentStack: info.componentStack },
    }).then((result) => this.setState({
      diagnosticStatus: result.recorded ? "recorded" : "not_recorded",
      diagnosticReference: result.diagnosticId || this.state.diagnosticReference,
    }));
  }

  componentDidMount(): void {
    if (this.state.failed) this.headingRef.current?.focus();
  }

  componentDidUpdate(_previousProps: Props, previousState: State): void {
    if (!previousState.failed && this.state.failed) this.headingRef.current?.focus();
  }

  private retry = () => this.setState({ failed: false, diagnosticStatus: "idle", safeDetails: "" });
  private stableWorkspace = () => { window.history.replaceState({}, "", "/chat"); this.retry(); };
  private safeMode = () => {
    resetAffectedClientState(typeof localStorage === "undefined" ? null : localStorage);
    window.location.assign("/chat?safe_mode=1");
  };
  private exportDraft = () => {
    if (!this.state.draft) return;
    const blob = new Blob([crashDraftExport(this.state.draft)], { type: "application/json" });
    const href = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = href; anchor.download = "rumi-recoverable-drafts.json"; anchor.click();
    // Safari may defer consuming the object URL until after the click handler returns.
    window.setTimeout(() => URL.revokeObjectURL(href), 0);
  };

  private diagnosticCopy(): string {
    if (this.state.diagnosticStatus === "sending") return "診断情報を送信中です。記録完了とはまだ確認されていません。";
    if (this.state.diagnosticStatus === "recorded") return `診断情報はbackendに記録されました。参照: ${this.state.diagnosticReference}`;
    if (this.state.diagnosticStatus === "not_recorded") return `診断情報は記録されていません。端末内参照: ${this.state.diagnosticReference}`;
    return `診断の記録状態を確認しています。端末内参照: ${this.state.diagnosticReference}`;
  }

  private errorCopyText(): string {
    return [
      "Tobkiri application recovery",
      "この画面の表示処理が停止しました",
      this.diagnosticCopy(),
      this.state.safeDetails || "Render failure",
    ].join("\n\n");
  }

  render() {
    if (!this.state.failed) return this.props.children;
    const repeated = this.state.crashCount >= 2;
    return (
      <main className="flex min-h-screen items-center justify-center bg-[#09090b] px-6 py-10 text-zinc-100 motion-reduce:scroll-auto">
        <section className="w-full max-w-2xl rounded-3xl border border-red-500/20 bg-zinc-950 p-7 shadow-2xl">
          <p className="text-xs font-semibold uppercase tracking-wider text-red-300">Application recovery</p>
          <h1 ref={this.headingRef} tabIndex={-1} className="mt-3 text-2xl font-semibold outline-none focus-visible:ring-2 focus-visible:ring-red-300">この画面の表示処理が停止しました</h1>
          <ErrorNotice
            className="mt-3 text-sm leading-6"
            copyLabel="クラッシュ情報をコピー"
            copyText={this.errorCopyText()}
            errorIcon="application-recovery"
            message="未保存の入力は削除せず、利用可能な復帰方法を表示しています。"
            messageClassName="text-zinc-300"
          />
          <p role="status" className="mt-3 rounded-xl border border-zinc-800 bg-black/20 p-3 text-sm text-zinc-400">{this.diagnosticCopy()}</p>
          {repeated ? <p className="mt-3 rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-100">短時間に同じ復帰画面が繰り返されました。再読み込みよりセーフモードを推奨します。</p> : null}
          {this.state.draft ? (
            <div className="mt-3 rounded-xl border border-sky-500/25 bg-sky-500/10 p-3 text-sm text-sky-100">
              未保存の入力を端末内に保持しています。内容を画面や診断には表示しません。
              <button type="button" onClick={this.exportDraft} className="ml-2 underline focus-visible:ring-2">入力をJSONで保存</button>
            </div>
          ) : null}
          <details className="mt-4 text-sm text-zinc-400">
            <summary className="cursor-pointer focus-visible:ring-2">安全化された技術情報</summary>
            <pre className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap rounded-xl bg-black/30 p-3 text-xs">{this.state.safeDetails || "Render failure"}</pre>
          </details>
          <div className="mt-6 flex flex-wrap gap-2">
            <button type="button" onClick={this.retry} className="rounded-xl bg-zinc-100 px-4 py-2 text-sm font-semibold text-zinc-950 focus-visible:ring-2">この画面を再試行</button>
            <button type="button" onClick={this.stableWorkspace} className="rounded-xl border border-zinc-700 px-4 py-2 text-sm focus-visible:ring-2">チャットへ戻る</button>
            <button type="button" onClick={this.safeMode} className="rounded-xl border border-amber-500/40 px-4 py-2 text-sm text-amber-100 focus-visible:ring-2">表示設定をリセットしてセーフモード</button>
            <button type="button" onClick={() => window.location.reload()} className="rounded-xl border border-zinc-700 px-4 py-2 text-sm focus-visible:ring-2">ページ全体を再読み込み</button>
          </div>
        </section>
      </main>
    );
  }
}
