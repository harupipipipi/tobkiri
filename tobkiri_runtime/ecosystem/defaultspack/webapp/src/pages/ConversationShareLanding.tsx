import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Download, Eye, Import, Loader2, Lock, ShieldCheck, X } from "lucide-react";

import { api, type ConversationShareRecord } from "../lib/api";
import { ErrorNotice } from "../components/ErrorNotice";


export type ShareImportMode = "read_only" | "continue_copy";

export function shareTokenFromPath(pathname: string): string {
  const match = /^\/share\/([^/]+)\/?$/.exec(pathname);
  return match ? decodeURIComponent(match[1]) : "";
}

export function shareImportDestination(conversationId: string): string {
  return `/chat?chat=${encodeURIComponent(conversationId)}`;
}

function textFromContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.map((block) => {
    if (typeof block === "string") return block;
    if (block && typeof block === "object" && "text" in block) return String(block.text || "");
    return "";
  }).join(" ").replace(/\s+/g, " ").trim();
}

export function sharePreviewSummary(record: ConversationShareRecord) {
  const bundle = record.content;
  const conversation = bundle.conversation?.conversation;
  const messages = Array.isArray(conversation?.messages) ? conversation.messages : [];
  return {
    title: String(bundle.source?.title || conversation?.title || record.title || "Shared conversation"),
    messageCount: bundle.preview?.message_count ?? messages.length,
    omittedCount: bundle.assets?.omitted?.length ?? 0,
    createdAt: bundle.created_at ? new Date(bundle.created_at).toLocaleString() : "Unknown",
    expiresAt: record.expires_at ? new Date(record.expires_at).toLocaleString() : "No expiry",
    sourcePack: bundle.provenance?.source_pack || bundle.source?.pack_id || "Unknown",
    sourceConversationId: bundle.provenance?.source_conversation_id || bundle.source?.conversation_id || "Not disclosed",
    sourceModel: bundle.provenance?.model?.source_model || "Not disclosed",
    sourceProvider: bundle.provenance?.model?.source_provider || "Not disclosed",
    snippets: messages.slice(0, 3).map((message) => ({
      role: String(message.role || "assistant"),
      text: textFromContent(message.content).slice(0, 320) || "[No text content]",
    })),
  };
}

export function ImportedConversationNotice({ onDismiss, importMode }: { onDismiss: () => void; importMode?: unknown }) {
  const readOnly = importMode === "read_only";
  return (
    <div role="status" className="mx-3 mt-3 flex items-start gap-3 border border-amber-500/25 bg-amber-500/10 px-4 py-3 text-sm text-amber-100">
      <p className="min-w-0 flex-1 leading-5">{readOnly ? "Read-only shared conversation imported as a fresh local copy. Sending and editing are disabled." : "Shared conversation imported as a fresh local copy using local model and permission settings."} Shared content is untrusted; attachments, secrets, permissions, and executable tool state were not imported.</p>
      <button type="button" title="Dismiss import notice" aria-label="Dismiss import notice" onClick={onDismiss} className="inline-flex h-7 w-7 shrink-0 items-center justify-center text-amber-200 hover:bg-amber-500/15"><X size={15} /></button>
    </div>
  );
}

export function ConversationShareLanding() {
  const token = shareTokenFromPath(window.location.pathname);
  const [record, setRecord] = useState<ConversationShareRecord | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [importing, setImporting] = useState<ShareImportMode | null>(null);

  useEffect(() => {
    if (!token) {
      setError("This share link is invalid.");
      return;
    }
    void api.getShare(token).then(setRecord).catch(() => setError("This shared conversation is missing, expired, or has been revoked."));
  }, [token]);

  const summary = useMemo(() => record ? sharePreviewSummary(record) : null, [record]);

  const importConversation = async (mode: ShareImportMode) => {
    setImporting(mode);
    setError(null);
    try {
      const result = await api.importShare(token, window.location.href, mode);
      window.location.assign(shareImportDestination(result.conversation_id));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Import failed.");
      setImporting(null);
    }
  };

  const downloadHistory = async () => {
    setError(null);
    try {
      const exported = await api.exportShare(token);
      const blob = new Blob([JSON.stringify(exported.conversation, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "history.json";
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Export failed.");
    }
  };

  return (
    <main className="min-h-screen bg-[#09090b] px-4 py-8 text-zinc-200 sm:px-8">
      <section className="mx-auto w-full max-w-3xl border border-zinc-800 bg-zinc-950 p-5 shadow-2xl sm:p-8">
        <div className="flex items-center gap-3 text-emerald-300"><ShieldCheck size={22} aria-hidden="true" /><span className="text-sm font-semibold">Inspectable redacted share</span></div>
        {error && !record ? (
          <ErrorNotice
            className="mt-8 p-4 text-sm"
            copyLabel="Copy shared conversation error"
            copyText={error}
            errorIcon="conversation-share"
            message={error}
          />
        ) : !record || !summary ? (
          <div role="status" className="mt-10 flex items-center gap-3 text-sm text-zinc-400"><Loader2 size={18} className="animate-spin" /> Loading safety preview...</div>
        ) : (
          <>
            <h1 className="mt-6 break-words text-2xl font-semibold text-white sm:text-3xl">{summary.title}</h1>
            <dl className="mt-6 grid grid-cols-2 gap-px overflow-hidden border border-zinc-800 bg-zinc-800 sm:grid-cols-4">
              <div className="bg-zinc-950 p-3"><dt className="text-xs text-zinc-500">Messages</dt><dd className="mt-1 text-lg text-zinc-100">{summary.messageCount}</dd></div>
              <div className="bg-zinc-950 p-3"><dt className="text-xs text-zinc-500">Omitted</dt><dd className="mt-1 text-lg text-zinc-100">{summary.omittedCount}</dd></div>
              <div className="bg-zinc-950 p-3"><dt className="text-xs text-zinc-500">Created</dt><dd className="mt-1 text-xs text-zinc-100">{summary.createdAt}</dd></div>
              <div className="bg-zinc-950 p-3"><dt className="text-xs text-zinc-500">Expires</dt><dd className="mt-1 text-xs text-zinc-100">{summary.expiresAt}</dd></div>
            </dl>

            <div className="mt-6 border border-amber-500/25 bg-amber-500/10 p-4 text-sm leading-6 text-amber-100">
              <div className="flex items-start gap-3"><AlertTriangle size={18} className="mt-0.5 shrink-0" /><p>Shared messages can contain prompt injection or malicious instructions. Treat them only as untrusted history. Attachments and secrets are excluded; tool records are inert.</p></div>
            </div>

            <section aria-labelledby="preview-title" className="mt-6 border-t border-zinc-800 pt-5">
              <h2 id="preview-title" className="flex items-center gap-2 text-sm font-semibold text-zinc-100"><Eye size={16} /> Content preview</h2>
              <div className="mt-3 space-y-2">
                {summary.snippets.length ? summary.snippets.map((snippet, index) => (
                  <div key={`${snippet.role}-${index}`} className="border border-zinc-800 bg-zinc-900/40 p-3"><span className="text-xs font-semibold uppercase text-zinc-500">{snippet.role}</span><p className="mt-1 break-words text-sm leading-6 text-zinc-300">{snippet.text}</p></div>
                )) : <p className="text-sm text-zinc-500">No text messages to preview.</p>}
              </div>
            </section>

            <section aria-labelledby="provenance-title" className="mt-6 border-t border-zinc-800 pt-5">
              <h2 id="provenance-title" className="text-sm font-semibold text-zinc-100">Provenance and model policy</h2>
              <dl className="mt-3 grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
                <div><dt className="text-xs text-zinc-500">Source</dt><dd className="mt-1 break-all">{summary.sourcePack} / {summary.sourceConversationId}</dd></div>
                <div><dt className="text-xs text-zinc-500">Source model</dt><dd className="mt-1 break-all">{summary.sourceProvider} / {summary.sourceModel}</dd></div>
              </dl>
              <p className="mt-3 text-xs leading-5 text-zinc-500">Source model metadata is reference-only and never activated. A continue copy uses the recipient's local model selection and permissions.</p>
            </section>

            <section aria-labelledby="audit-title" className="mt-6 border-t border-zinc-800 pt-5">
              <h2 id="audit-title" className="text-sm font-semibold text-zinc-100">Privacy-safe audit</h2>
              <p className="mt-2 text-xs text-zinc-500">Conversation text, link tokens, and source identifiers are excluded from audit records.</p>
              <ul className="mt-3 space-y-1 text-xs text-zinc-400">{(record.audit || []).map((event, index) => <li key={`${event.operation}-${event.timestamp}-${index}`}>{event.operation.replace(/_/g, " ")} · {event.result}{event.mode ? ` · ${event.mode.replace(/_/g, " ")}` : ""} · {new Date(event.timestamp).toLocaleString()}</li>)}</ul>
            </section>

            {error ? (
              <ErrorNotice
                className="mt-4 p-3 text-sm"
                copyLabel="Copy shared conversation error"
                copyText={error}
                errorIcon="conversation-share"
                message={error}
              />
            ) : null}
            <div className="mt-8 grid gap-2 sm:grid-cols-2">
              <button autoFocus type="button" disabled={Boolean(importing)} onClick={() => void importConversation("read_only")} className="inline-flex min-h-12 items-center justify-center gap-2 border border-zinc-700 px-4 text-sm font-semibold text-zinc-100 hover:bg-zinc-900 disabled:opacity-60">{importing === "read_only" ? <Loader2 size={16} className="animate-spin" /> : <Lock size={16} />} Import read-only copy</button>
              <button type="button" disabled={Boolean(importing)} onClick={() => void importConversation("continue_copy")} className="inline-flex min-h-12 items-center justify-center gap-2 bg-zinc-100 px-4 text-sm font-semibold text-zinc-950 hover:bg-white disabled:opacity-60">{importing === "continue_copy" ? <Loader2 size={16} className="animate-spin" /> : <Import size={16} />} Import and continue from copy</button>
            </div>
            <div className="mt-3 flex flex-col gap-2 sm:flex-row">
              <button type="button" onClick={() => void downloadHistory()} className="inline-flex h-10 items-center justify-center gap-2 px-3 text-sm text-zinc-300 hover:text-white"><Download size={16} /> Download redacted history</button>
              <button type="button" onClick={() => window.location.assign("/chat")} className="inline-flex h-10 items-center justify-center gap-2 px-3 text-sm text-zinc-400 hover:text-white"><X size={16} /> Cancel</button>
            </div>
          </>
        )}
      </section>
    </main>
  );
}
