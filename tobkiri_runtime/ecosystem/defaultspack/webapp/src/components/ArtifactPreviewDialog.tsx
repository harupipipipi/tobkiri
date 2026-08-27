import { useEffect, useState } from "react";
import { Code2, Copy, ExternalLink, FileText, Image as ImageIcon, Link2, Wrench, X } from "lucide-react";

import { cn } from "../lib/cn";
import { ModalFoundation } from "./ModalFoundation";

export type ArtifactPreviewDetail = {
  label: string;
  value: string;
};

export type ArtifactPreviewDialogItem = {
  kind: "file" | "image" | "tool";
  title: string;
  subtitle?: string;
  href?: string;
  untrustedSourceUrl?: string;
  imageUrl?: string;
  imageAlt?: string;
  content?: string;
  language?: string;
  details?: ArtifactPreviewDetail[];
};

function iconFor(kind: ArtifactPreviewDialogItem["kind"]) {
  if (kind === "image") return ImageIcon;
  if (kind === "tool") return Wrench;
  return FileText;
}

function kindLabel(kind: ArtifactPreviewDialogItem["kind"]) {
  if (kind === "image") return "Image artifact";
  if (kind === "tool") return "Tool artifact";
  return "File artifact";
}

function linesFor(content: string | undefined): string[] {
  const text = content ?? "";
  return text ? text.split("\n") : ["No preview content available."];
}

export function ArtifactPreviewDialog({
  item,
  onClose,
}: {
  item: ArtifactPreviewDialogItem | null;
  onClose: () => void;
}) {
  const [copyStatus, setCopyStatus] = useState("");

  useEffect(() => {
    setCopyStatus("");
  }, [item]);

  const copyUntrustedSource = async () => {
    if (!item?.untrustedSourceUrl) return;
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard is unavailable");
      await navigator.clipboard.writeText(item.untrustedSourceUrl);
      setCopyStatus("URL をコピーしました。");
    } catch {
      setCopyStatus("URL をコピーできませんでした。");
    }
  };

  const copyArtifactContent = async () => {
    if (!item?.content) return;
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard is unavailable");
      await navigator.clipboard.writeText(item.content);
      setCopyStatus("内容をコピーしました。");
    } catch {
      setCopyStatus("内容をコピーできませんでした。");
    }
  };

  if (!item) return null;

  const Icon = iconFor(item.kind);
  const isImage = item.kind === "image" && item.imageUrl;
  const contentLines = linesFor(item.content);

  return (
    <ModalFoundation
      variant="trusted-window"
      title={item.title}
      description={item.subtitle || kindLabel(item.kind)}
      onClose={onClose}
      backdropClassName="rumi-image-preview-backdrop fixed inset-0 rumi-layer-modal flex items-center justify-center bg-black/72 px-4 py-5 backdrop-blur-md motion-reduce:backdrop-blur-none"
      panelClassName="rumi-image-preview-shell relative flex h-[min(88dvh,980px)] w-[min(94vw,1180px)] flex-col overflow-hidden rounded-2xl border border-zinc-800 bg-[#0b0b0d] shadow-[0_28px_90px_rgba(0,0,0,0.55)] outline-none"
    >
        <header className="flex min-h-12 items-center justify-between gap-3 border-b border-zinc-800 bg-zinc-950/95 px-3 py-2">
          <div className="flex min-w-0 items-center gap-2">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl border border-amber-200/10 bg-gradient-to-br from-amber-200/14 via-orange-300/10 to-stone-200/10 text-amber-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.12)]">
              <Icon size={15} />
            </span>
            <div className="min-w-0">
              <div className="truncate text-[12px] font-medium text-zinc-200">{item.title}</div>
              <div className="truncate text-[10px] text-zinc-600">{item.subtitle || kindLabel(item.kind)}</div>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {item.href && (
              <a
                href={item.href}
                target="_blank"
                rel="noreferrer"
                aria-label="元アーティファクトを開く"
                title="元アーティファクトを開く"
                className="flex h-8 w-8 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-zinc-900 hover:text-zinc-200 focus-visible:bg-zinc-900 focus-visible:text-zinc-200 focus-visible:outline-none"
              >
                <ExternalLink size={15} />
              </a>
            )}
            {item.untrustedSourceUrl && (
              <button
                type="button"
                aria-label="未検証の source URL をコピー"
                title="未検証の source URL をコピー"
                onClick={() => { void copyUntrustedSource(); }}
                className="flex h-8 w-8 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-zinc-900 hover:text-zinc-200 focus-visible:bg-zinc-900 focus-visible:text-zinc-200 focus-visible:outline-none"
              >
                <Link2 size={15} />
              </button>
            )}
            {item.content && (
              <button
                type="button"
                aria-label="アーティファクト内容をコピー"
                title="アーティファクト内容をコピー"
                onClick={() => { void copyArtifactContent(); }}
                className="flex h-8 w-8 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-zinc-900 hover:text-zinc-200 focus-visible:bg-zinc-900 focus-visible:text-zinc-200 focus-visible:outline-none"
              >
                <Copy size={15} />
              </button>
            )}
            <button
              type="button"
              aria-label="閉じる"
              title="閉じる"
              className="flex h-8 w-8 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-zinc-900 hover:text-zinc-200 focus-visible:bg-zinc-900 focus-visible:text-zinc-200 focus-visible:outline-none"
              onClick={onClose}
            >
              <X size={16} />
            </button>
          </div>
        </header>

        <div className={cn("min-h-0 flex-1 overflow-auto", isImage ? "bg-black" : "bg-[#09090b]")}>
          {isImage ? (
            <div className="rumi-image-preview-media flex min-h-full items-center justify-center p-4">
              <img
                src={item.imageUrl}
                alt={item.imageAlt || item.title}
                className="max-h-full max-w-full rounded-xl border border-zinc-800/80 object-contain shadow-[0_18px_70px_rgba(0,0,0,0.45)]"
              />
            </div>
          ) : (
            <div className="rumi-image-preview-media min-h-full p-4">
              <div className="mb-3 flex items-center justify-between gap-3 rounded-xl border border-zinc-800/80 bg-zinc-950/70 px-3 py-2">
                <div className="flex min-w-0 items-center gap-2 text-[11px] text-zinc-400">
                  {item.kind === "tool" ? <Wrench size={13} className="text-amber-300" /> : <Code2 size={13} className="text-zinc-500" />}
                  <span className="truncate">{item.language || kindLabel(item.kind)}</span>
                </div>
                <span className="shrink-0 font-mono text-[10px] text-zinc-600">{contentLines.length} lines</span>
              </div>
              <pre className="overflow-auto rounded-xl border border-zinc-800/80 bg-black/35 py-3 font-mono text-[11px] leading-[1.65] text-zinc-300">
                {contentLines.map((line, index) => (
                  <div key={`${index}-${line}`} className="grid grid-cols-[3.5rem_minmax(0,1fr)] px-3">
                    <span className="select-none pr-4 text-right text-zinc-700">{index + 1}</span>
                    <span className="min-w-0 whitespace-pre-wrap break-words">{line || " "}</span>
                  </div>
                ))}
              </pre>
            </div>
          )}
        </div>

        {item.details && item.details.length > 0 && (
          <dl className="grid max-h-36 shrink-0 grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1.5 overflow-auto border-t border-zinc-800 bg-zinc-950/95 px-3 py-2 font-mono text-[10px] leading-5">
            {item.details.map((detail) => (
              <div key={`${detail.label}-${detail.value}`} className="contents">
                <dt className="text-zinc-600">{detail.label}</dt>
                <dd className="min-w-0 break-words text-zinc-400">{detail.value}</dd>
              </div>
            ))}
          </dl>
        )}
        {item.untrustedSourceUrl && (
          <div className="border-t border-zinc-800 bg-zinc-950/95 px-3 py-2 text-[10px] text-zinc-500">
            <p role="status" className="text-amber-300">Remote preview blocked</p>
            <p className="mt-1 truncate font-mono">{item.untrustedSourceUrl}</p>
            <p aria-live="polite" className="mt-1 min-h-4">{copyStatus}</p>
          </div>
        )}
    </ModalFoundation>
  );
}
