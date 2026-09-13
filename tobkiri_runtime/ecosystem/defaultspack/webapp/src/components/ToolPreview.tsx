import React, { useState, useEffect, useMemo } from 'react';
import {
  X, Globe, FileText, Image, ExternalLink,
  Eye, EyeOff, Code, NotebookPen, Maximize2,
  Plus, Clock, Layers
} from 'lucide-react';
import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

import { ArtifactPreviewDialog, type ArtifactPreviewDialogItem } from './ArtifactPreviewDialog';
import { ErrorNotice } from './ErrorNotice';

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// ============================================================
// Types
// ============================================================

export type PreviewType = 'web' | 'code' | 'file' | 'image';

export type WebPreview = {
  type: 'web';
  url: string;
  title: string;
  favicon?: string;
  screenshot?: string;
  snippet?: string;
};

export type CodePreview = {
  type: 'code';
  filename: string;
  language: string;
  diff?: string;
  content?: string;
  additions?: number;
  deletions?: number;
};

export type FilePreview = {
  type: 'file';
  filename: string;
  size: string;
  content?: string;
  url?: string;
  path?: string;
  downloadName?: string;
  mimeType?: string;
};

export type ImagePreview = {
  type: 'image';
  url: string;
  alt: string;
  prompt?: string;
  path?: string;
};

export type ToolPreviewData = WebPreview | CodePreview | FilePreview | ImagePreview;

export type ToolPreviewItem = {
  id: string;
  toolStepId: string;
  timestamp: number;
  data: ToolPreviewData;
};

export type ToolPreviewMode = 'auto' | 'manual';

export const MEMO_PREVIEW_ID = '__memo__';
const TIMELINE_TAB_ID = '__timeline__';
// Keep web preview documents in an opaque origin even when they are served from
// the same loopback host as the panel. Combining allow-scripts with
// allow-same-origin would let preview HTML escape the sandbox boundary.
export const WEB_PREVIEW_IFRAME_SANDBOX = '';

function matchesPreviewId(item: ToolPreviewItem, previewId?: string | null) {
  return Boolean(previewId && (item.id === previewId || item.toolStepId === previewId));
}

function memoPreviewItem(memo = ''): ToolPreviewItem {
  return {
    id: MEMO_PREVIEW_ID,
    toolStepId: 'memo',
    timestamp: 0,
    data: {
      type: 'file',
      filename: 'memo.md',
      size: 'local memo',
      content: memo,
    },
  };
}

function isArtifactPlaceholderContent(content: string | undefined, path?: string): boolean {
  const text = String(content ?? '').trim();
  if (!text) return false;
  if (path && text === `artifact: ${path}`) return true;
  return /^artifact:\s*.+$/i.test(text);
}

export function isCanvasPreviewItemRenderable(item: ToolPreviewItem): boolean {
  const data = item.data;
  if (
    data.type === 'code'
    && String(data.content ?? data.diff ?? '').trim().startsWith('Tool planned or referenced:')
  ) {
    return false;
  }
  if (
    data.type === 'file'
    && !data.url
    && !data.path
    && isArtifactPlaceholderContent(data.content)
  ) {
    return false;
  }
  return true;
}

export function hasCanvasItems(previews: ToolPreviewItem[], memo?: string | null) {
  return previews.some(isCanvasPreviewItemRenderable) || Boolean(memo?.trim());
}

export function buildToolPreviewDisplayItems(
  previews: ToolPreviewItem[],
  memo?: string,
  activePreviewId?: string | null,
): ToolPreviewItem[] {
  const shouldShowMemo = Boolean(memo?.trim()) || activePreviewId === MEMO_PREVIEW_ID || activePreviewId === 'memo';
  const memoPreview = shouldShowMemo ? memoPreviewItem(memo) : null;
  const renderablePreviews = previews.filter(isCanvasPreviewItemRenderable);
  const items = memoPreview ? [memoPreview, ...renderablePreviews] : [...renderablePreviews];
  if (!activePreviewId) return items;

  const active = items.find((item) => matchesPreviewId(item, activePreviewId));
  if (!active) return items;
  return [active, ...items.filter((item) => item.id !== active.id)];
}

export function buildCanvasTabPickerItems(
  displayItems: ToolPreviewItem[],
  memo: string | undefined,
  memoEnabled: boolean,
): ToolPreviewItem[] {
  if (!memoEnabled || displayItems.some((item) => item.id === MEMO_PREVIEW_ID)) {
    return displayItems;
  }
  return [memoPreviewItem(memo), ...displayItems];
}

export function selectCanvasTab(openPreviewIds: string[], item: ToolPreviewItem) {
  return {
    openPreviewIds: openPreviewIds.includes(item.id)
      ? openPreviewIds
      : [...openPreviewIds, item.id],
    activeTabId: item.id,
    memoTabCreated: item.id === MEMO_PREVIEW_ID,
  };
}

export function buildToolPreviewTimelineItems(items: ToolPreviewItem[]): ToolPreviewItem[] {
  return items.filter(isCanvasPreviewItemRenderable).sort((left, right) => {
    const leftTime = Number.isFinite(left.timestamp) ? left.timestamp : 0;
    const rightTime = Number.isFinite(right.timestamp) ? right.timestamp : 0;
    return leftTime - rightTime || left.id.localeCompare(right.id);
  });
}

function shortPreviewDetail(value: unknown, limit = 260): string {
  const text = typeof value === 'string' ? value : String(value ?? '');
  const compact = text.replace(/\s+/g, ' ').trim();
  return compact.length > limit ? `${compact.slice(0, limit - 3)}...` : compact;
}

function previewTitle(data: ToolPreviewData): string {
  if (data.type === 'web') return data.title || data.url || 'Web preview';
  if (data.type === 'code') return data.filename || 'Code preview';
  if (data.type === 'file') return data.filename || data.path || 'File preview';
  return data.alt || data.path || 'Image preview';
}

function previewTypeLabel(data: ToolPreviewData): string {
  if (data.type === 'web') return 'Web';
  if (data.type === 'code') return 'Code';
  if (data.type === 'file') return 'File';
  return 'Image';
}

function previewIcon(data: ToolPreviewData, size = 12) {
  if (data.type === 'web') return <Globe size={size} className="text-emerald-400" />;
  if (data.type === 'code') return <Code size={size} className="text-amber-400" />;
  if (data.type === 'file') return <FileText size={size} className="text-violet-400" />;
  return <Image size={size} className="text-blue-400" />;
}

function previewBaseUrl(): string {
  try {
    return typeof window === 'undefined' ? '' : window.location.href;
  } catch {
    return '';
  }
}

export function safePreviewHref(url: string | undefined, baseUrl = previewBaseUrl()): string | undefined {
  if (!url || !baseUrl) return undefined;
  try {
    const base = new URL(baseUrl);
    const parsed = new URL(url, base);
    if (parsed.origin !== base.origin) return undefined;
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return undefined;
    return parsed.toString();
  } catch {
    return undefined;
  }
}

export function safePreviewImageUrl(url: string | undefined, baseUrl = previewBaseUrl()): string | undefined {
  if (!url) return undefined;
  if (/^data:image\/(?:png|jpe?g|gif|webp);base64,/i.test(url)) return url;
  return safePreviewHref(url, baseUrl);
}

function safeHref(url: string | undefined): string | undefined {
  return safePreviewHref(url);
}

function localPreviewUrl(url: string | undefined): boolean {
  return Boolean(safePreviewHref(url));
}

function looksLikeHtml(data: FilePreview, content?: string): boolean {
  const name = data.filename.toLowerCase();
  const mime = String(data.mimeType ?? '').toLowerCase();
  const text = String(content ?? data.content ?? '').trimStart().toLowerCase();
  return mime.includes('html') || /\.(html?|xhtml)$/.test(name) || text.startsWith('<!doctype html') || text.startsWith('<html');
}

function looksLikeDiff(filename: string, content?: string): boolean {
  const name = filename.toLowerCase();
  const text = String(content ?? '').trimStart();
  return /\.(diff|patch)$/.test(name) || text.startsWith('diff --git') || text.startsWith('--- ') || text.startsWith('+++ ');
}

function displayPreviewContent(data: FilePreview): string | undefined {
  if (isArtifactPlaceholderContent(data.content, data.path)) return undefined;
  return data.content;
}

function escapeHtmlAttribute(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

const HTML_PREVIEW_CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; connect-src 'none'; media-src 'none'; frame-src 'none'; object-src 'none'; form-action 'none'; base-uri 'none'";

export function hardenedHtmlPreviewDocument(content: string): string {
  const csp = `<meta http-equiv="Content-Security-Policy" content="${escapeHtmlAttribute(HTML_PREVIEW_CSP)}">`;
  const metadata = `${csp}<meta name="referrer" content="no-referrer">`;
  if (/<head[^>]*>/i.test(content)) return content.replace(/<head([^>]*)>/i, `<head$1>${metadata}`);
  return `<!doctype html><html><head>${metadata}</head><body>${content}</body></html>`;
}

function htmlWithBase(content: string, _url?: string): string {
  return hardenedHtmlPreviewDocument(content);
}

export function artifactDialogItemFromToolPreview(item: ToolPreviewItem): ArtifactPreviewDialogItem {
  const details = [
    { label: 'toolStepId', value: item.toolStepId },
    { label: 'timestamp', value: String(item.timestamp) },
  ];

  if (item.id === MEMO_PREVIEW_ID) {
    return {
      kind: 'file',
      title: 'memo.md',
      subtitle: 'local memo',
      language: 'markdown',
      content: item.data.type === 'file' ? item.data.content : '',
      details,
    };
  }

  const data = item.data;
  if (data.type === 'image') {
    return {
      kind: 'image',
      title: previewTitle(data),
      subtitle: data.path || data.prompt || 'image artifact',
      href: safeHref(data.url),
      imageUrl: safePreviewImageUrl(data.url),
      imageAlt: data.alt,
      details: [
        ...details,
        ...(data.path ? [{ label: 'path', value: data.path }] : []),
        ...(data.prompt ? [{ label: 'prompt', value: shortPreviewDetail(data.prompt) }] : []),
      ],
    };
  }

  if (data.type === 'file') {
    return {
      kind: 'file',
      title: previewTitle(data),
      subtitle: data.path || data.size,
      href: safeHref(data.url),
      content: data.content,
      language: data.filename.split('.').pop()?.toLowerCase() || 'text',
      details: [
        ...details,
        { label: 'size', value: data.size },
        ...(data.path ? [{ label: 'path', value: data.path }] : []),
      ],
    };
  }

  if (data.type === 'code') {
    return {
      kind: 'file',
      title: previewTitle(data),
      subtitle: data.language || 'code',
      content: data.diff || data.content,
      language: data.language,
      details: [
        ...details,
        ...(data.additions !== undefined ? [{ label: 'additions', value: String(data.additions) }] : []),
        ...(data.deletions !== undefined ? [{ label: 'deletions', value: String(data.deletions) }] : []),
      ],
    };
  }

  return {
    kind: 'tool',
    title: previewTitle(data),
    subtitle: data.url,
    href: safeHref(data.url),
    imageUrl: safePreviewImageUrl(data.screenshot),
    imageAlt: data.title,
    content: [data.title, data.url, data.snippet].filter(Boolean).join('\n\n'),
    language: 'web',
    details,
  };
}

// ============================================================
// Mock preview data
// ============================================================

export const MOCK_PREVIEWS: ToolPreviewItem[] = [
  {
    id: 'prev-1',
    toolStepId: 's1',
    timestamp: Date.now() - 50000,
    data: {
      type: 'web',
      url: 'https://glassnode.com/metrics/btc',
      title: 'Glassnode - On-Chain Market Intelligence',
      snippet: 'Glassnode provides on-chain data and intelligence for Bitcoin and digital assets. Track exchange flows, miner activity, and whale movements.',
      screenshot: '',
    },
  },
  {
    id: 'prev-2',
    toolStepId: 's3',
    timestamp: Date.now() - 40000,
    data: {
      type: 'web',
      url: 'https://cryptoquant.com/asset/btc/chart/exchange-flows',
      title: 'CryptoQuant - Exchange Netflow',
      snippet: 'Exchange Netflow shows the net amount of BTC flowing in/out of exchanges. Positive values indicate potential selling pressure.',
      screenshot: '',
    },
  },
  {
    id: 'prev-3',
    toolStepId: 's7',
    timestamp: Date.now() - 35000,
    data: {
      type: 'web',
      url: 'https://coinglass.com/bitcoin-exchange-flow',
      title: 'Coinglass - BTC Exchange Flow',
      snippet: 'Real-time Bitcoin exchange flow data including inflows, outflows and net flows across major exchanges.',
      screenshot: '',
    },
  },
  {
    id: 'prev-4',
    toolStepId: 's11',
    timestamp: Date.now() - 30000,
    data: {
      type: 'web',
      url: 'https://bitinfocharts.com/top-100-richest-bitcoin-addresses.html',
      title: 'BitInfoCharts - Top 100 Richest Bitcoin Addresses',
      snippet: 'Bitcoin distribution among addresses. Top 2,000 addresses hold approximately 40% of all BTC.',
      screenshot: '',
    },
  },
  {
    id: 'prev-5',
    toolStepId: 'c1',
    timestamp: Date.now() - 25000,
    data: {
      type: 'file',
      filename: 'package.json',
      size: '1.2KB',
      content: `{
  "name": "dashboard-app",
  "version": "1.0.0",
  "private": true,
  "dependencies": {
    "react": "^19.0.0",
    "react-dom": "^19.0.0",
    "tailwindcss": "^4.0.0"
  },
  "devDependencies": {
    "typescript": "^5.8.0",
    "vite": "^6.0.0"
  }
}`,
    },
  },
  {
    id: 'prev-6',
    toolStepId: 'c2',
    timestamp: Date.now() - 20000,
    data: {
      type: 'code',
      filename: 'terminal',
      language: 'bash',
      content: `$ npm install recharts date-fns

added 12 packages in 2.4s

4 packages are looking for funding
  run \`npm fund\` for details`,
    },
  },
  {
    id: 'prev-7',
    toolStepId: 'c4',
    timestamp: Date.now() - 15000,
    data: {
      type: 'code',
      filename: 'src/types/sales.ts',
      language: 'typescript',
      additions: 18,
      deletions: 0,
      diff: `+export type SalesRecord = {
+  id: string;
+  date: string;
+  amount: number;
+  category: "electronics" | "clothing" | "food";
+  region: "tokyo" | "osaka" | "fukuoka";
+};
+
+export type DashboardFilter = {
+  period: "7d" | "30d" | "90d";
+  category?: string;
+  region?: string;
+};
+
+export type AggregatedData = {
+  date: string;
+  total: number;
+  count: number;
+};`,
    },
  },
  {
    id: 'prev-8',
    toolStepId: 'c5',
    timestamp: Date.now() - 10000,
    data: {
      type: 'code',
      filename: 'src/components/Dashboard.tsx',
      language: 'typescript',
      additions: 58,
      deletions: 0,
      diff: `+import { useMemo, useState } from "react";
+import {
+  LineChart, Line, XAxis, YAxis,
+  Tooltip, ResponsiveContainer
+} from "recharts";
+import type { SalesRecord, DashboardFilter } from "../types/sales";
+
+type Props = {
+  data: SalesRecord[];
+};
+
+export function Dashboard({ data }: Props) {
+  const [filter, setFilter] = useState<DashboardFilter>({
+    period: "30d",
+  });
+
+  const filtered = useMemo(() => {
+    const days =
+      filter.period === "7d" ? 7 :
+      filter.period === "30d" ? 30 : 90;
+    return data.slice(-days);
+  }, [data, filter]);
+
+  const aggregated = useMemo(() => {
+    const map = new Map<string, number>();
+    for (const r of filtered) {
+      map.set(r.date, (map.get(r.date) || 0) + r.amount);
+    }
+    return Array.from(map.entries()).map(
+      ([date, total]) => ({ date, total })
+    );
+  }, [filtered]);
+
+  return (
+    <div className="p-6 bg-zinc-950 rounded-xl border border-zinc-800">
+      <div className="flex justify-between items-center mb-6">
+        <h2 className="text-lg font-bold text-white">
+          売上推移
+        </h2>
+        <PeriodSelector
+          value={filter.period}
+          onChange={(p) => setFilter(f => ({ ...f, period: p }))}
+        />
+      </div>
+      <ResponsiveContainer width="100%" height={300}>
+        <LineChart data={aggregated}>
+          <XAxis dataKey="date" stroke="#52525b" />
+          <YAxis stroke="#52525b" />
+          <Tooltip />
+          <Line
+            type="monotone"
+            dataKey="total"
+            stroke="#10b981"
+            strokeWidth={2}
+            dot={false}
+          />
+        </LineChart>
+      </ResponsiveContainer>
+    </div>
+  );
+}`,
    },
  },
  {
    id: 'prev-9',
    toolStepId: 'c6',
    timestamp: Date.now() - 5000,
    data: {
      type: 'code',
      filename: 'src/components/FilterBar.tsx',
      language: 'typescript',
      additions: 32,
      deletions: 0,
      diff: `+import type { DashboardFilter } from "../types/sales";
+
+type Props = {
+  filter: DashboardFilter;
+  onChange: (f: DashboardFilter) => void;
+};
+
+const CATEGORIES = ["all", "electronics", "clothing", "food"];
+const REGIONS = ["all", "tokyo", "osaka", "fukuoka"];
+
+export function FilterBar({ filter, onChange }: Props) {
+  return (
+    <div className="flex gap-3 items-center">
+      <select
+        value={filter.category || "all"}
+        onChange={(e) =>
+          onChange({
+            ...filter,
+            category: e.target.value === "all"
+              ? undefined
+              : e.target.value,
+          })
+        }
+        className="bg-zinc-800 text-zinc-200 text-sm
+                   rounded-lg px-3 py-1.5 border border-zinc-700"
+      >
+        {CATEGORIES.map((c) => (
+          <option key={c} value={c}>
+            {c === "all" ? "全カテゴリ" : c}
+          </option>
+        ))}
+      </select>
+    </div>
+  );
+}`,
    },
  },
  {
    id: 'prev-10',
    toolStepId: 'c8',
    timestamp: Date.now() - 2000,
    data: {
      type: 'code',
      filename: 'src/index.css',
      language: 'css',
      additions: 8,
      deletions: 2,
      diff: `@import "tailwindcss";
 
-:root {
-  color-scheme: light;
+:root {
+  color-scheme: dark;
 }
 
+body {
+  background-color: #09090b;
+  color: #fafafa;
+  -webkit-font-smoothing: antialiased;
+}
+
+.dark-chart .recharts-cartesian-grid line {
+  stroke: #27272a;
+}`,
    },
  },
];

// ============================================================
// Preview Content Renderers
// ============================================================

function WebPreviewContent({ data }: { data: WebPreview }) {
  const canEmbed = localPreviewUrl(data.url);
  return (
    <div className="flex flex-col h-full">
      {/* Browser chrome */}
      <div className="flex items-center gap-2 px-3 py-2 bg-zinc-900 border-b border-zinc-800/60 flex-shrink-0">
        <div className="flex gap-1">
          <div className="w-2 h-2 rounded-full bg-zinc-700" />
          <div className="w-2 h-2 rounded-full bg-zinc-700" />
          <div className="w-2 h-2 rounded-full bg-zinc-700" />
        </div>
        <div className="flex-1 flex items-center gap-2 bg-zinc-800 rounded px-2.5 py-1 text-[10px] text-zinc-500 font-mono truncate">
          <Globe size={10} className="flex-shrink-0 text-zinc-600" />
          <span className="truncate">{data.url}</span>
        </div>
        <a
          href={safeHref(data.url)}
          target="_blank"
          rel="noreferrer"
          className="text-zinc-600 hover:text-zinc-400 transition-colors"
        >
          <ExternalLink size={12} />
        </a>
      </div>

      {/* Page content */}
      <div className="flex-1 overflow-hidden">
        {canEmbed ? (
          <iframe
            src={safeHref(data.url)}
            title={data.title || data.url}
            className="h-full w-full border-0 bg-white"
            sandbox={WEB_PREVIEW_IFRAME_SANDBOX}
          />
        ) : safePreviewImageUrl(data.screenshot) ? (
          <img
            src={safePreviewImageUrl(data.screenshot)}
            alt={data.title}
            className="m-4 w-[calc(100%-2rem)] rounded border border-zinc-800"
          />
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-3 p-4 text-center">
            <Globe size={28} className="text-zinc-700" />
            <div className="max-w-full">
              <h3 className="truncate text-sm font-medium text-zinc-200">{data.title}</h3>
              <p className="mt-1 truncate font-mono text-[10px] text-emerald-600">{data.url}</p>
              {data.snippet && (
                <p className="mt-2 text-xs leading-relaxed text-zinc-500">{data.snippet}</p>
              )}
            </div>
            <a
              href={safeHref(data.url)}
              target="_blank"
              rel="noreferrer"
              className="inline-flex h-8 items-center gap-1 rounded-md border border-zinc-800 bg-zinc-950 px-3 text-[11px] text-zinc-400 hover:border-zinc-700 hover:text-zinc-200"
            >
              <ExternalLink size={12} />
              開く
            </a>
          </div>
        )}
      </div>
    </div>
  );
}

function CodePreviewContent({ data }: { data: CodePreview }) {
  return (
    <div className="flex flex-col h-full">
      {/* File tab */}
      <div className="flex items-center justify-between px-3 py-2 bg-zinc-900 border-b border-zinc-800/60 flex-shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <FileText size={12} className="text-zinc-500 flex-shrink-0" />
          <span className="text-[11px] font-mono text-zinc-300 truncate">{data.filename}</span>
        </div>
        <div className="flex items-center gap-2 text-[10px] font-mono flex-shrink-0 ml-2">
          {data.additions !== undefined && (
            <span className="text-emerald-500">+{data.additions}</span>
          )}
          {data.deletions !== undefined && data.deletions > 0 && (
            <span className="text-red-400">-{data.deletions}</span>
          )}
          <span className="text-zinc-600 bg-zinc-800 px-1.5 py-0.5 rounded">{data.language}</span>
        </div>
      </div>

      {/* Code / Diff */}
      <div className="flex-1 overflow-y-auto">
        <pre className="text-[11px] font-mono leading-[1.6]">
          {(data.diff || data.content || '').split('\n').map((line, i) => {
            const isAdd = line.startsWith('+') && !line.startsWith('+++');
            const isDel = line.startsWith('-') && !line.startsWith('---');
            return (
              <div
                key={i}
                className={cn(
                  'px-3 min-h-[1.6em]',
                  isAdd && 'bg-emerald-500/8 text-emerald-400',
                  isDel && 'bg-red-500/8 text-red-400',
                  !isAdd && !isDel && 'text-zinc-400'
                )}
              >
                <span className="inline-block w-7 text-right mr-3 text-zinc-700 select-none text-[10px]">
                  {i + 1}
                </span>
                <span>{line}</span>
              </div>
            );
          })}
        </pre>
      </div>
    </div>
  );
}

function useRemotePreviewText(data: FilePreview) {
  const inlineContent = displayPreviewContent(data);
  const [remoteText, setRemoteText] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setRemoteText(null);
    setError(null);
    if (inlineContent !== undefined || !data.url) {
      setIsLoading(false);
      return () => {
        cancelled = true;
      };
    }
    const fetchUrl = safeHref(data.url);
  if (!fetchUrl) {
    setIsLoading(false);
    setError('Remote preview blocked by URL policy.');
    return () => {
      cancelled = true;
    };
  }
  setIsLoading(true);
  void fetch(fetchUrl, { cache: 'no-store', credentials: 'omit', referrerPolicy: 'no-referrer' })
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.text();
      })
      .then((text) => {
        if (!cancelled) setRemoteText(text);
      })
      .catch((fetchError) => {
        if (!cancelled) setError(fetchError instanceof Error ? fetchError.message : String(fetchError));
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [data.url, inlineContent]);

  return {
    error,
    isLoading,
    text: inlineContent ?? remoteText ?? '',
  };
}

function HtmlPreviewContent({
  data,
  content,
  error,
  isLoading,
}: {
  data: FilePreview;
  content: string;
  error: string | null;
  isLoading: boolean;
}) {
  const srcDoc = content ? htmlWithBase(content, data.url) : undefined;
  return (
    <div className="flex h-full flex-col">
      <div className="flex min-h-9 items-center justify-between gap-2 border-b border-zinc-800/60 bg-zinc-900 px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <Globe size={12} className="shrink-0 text-emerald-400" />
          <span className="truncate font-mono text-[11px] text-zinc-300">{data.filename}</span>
        </div>
        {data.url && (
          <a
            href={safeHref(data.url)}
            target="_blank"
            rel="noreferrer"
            className="inline-flex h-6 shrink-0 items-center gap-1 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-[10px] text-zinc-400 hover:border-zinc-700 hover:text-zinc-200"
          >
            <ExternalLink size={11} />
            開く
          </a>
        )}
      </div>
      <div className="relative min-h-0 flex-1 bg-white">
        {isLoading && (
          <div className="absolute inset-0 rumi-layer-panel flex items-center justify-center bg-zinc-950 text-[11px] text-zinc-500">
            HTML を読み込んでいます
          </div>
        )}
        {srcDoc ? (
          <iframe
            title={data.filename}
            srcDoc={srcDoc}
            className="h-full w-full border-0 bg-white"
            sandbox={WEB_PREVIEW_IFRAME_SANDBOX}
            referrerPolicy="no-referrer"
          />
        ) : !isLoading ? (
          <div className="flex h-full items-center justify-center bg-zinc-950 px-4 text-center text-[11px] text-zinc-500">
            {error ? (
              <ErrorNotice
                className="max-w-md text-left"
                copyLabel="HTML プレビューエラーをコピー"
                message={`HTML を読み込めませんでした: ${error}`}
              />
            ) : 'HTML preview の内容がありません。'}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function FilePreviewContent({ data }: { data: FilePreview }) {
  const loaded = useRemotePreviewText(data);
  const content = loaded.text;
  const looksLikeJson = data.filename.toLowerCase().endsWith('.json') || String(content ?? '').trimStart().startsWith('{');
  if (looksLikeHtml(data, content) && (content || data.url)) {
    return <HtmlPreviewContent data={data} content={content} error={loaded.error} isLoading={loaded.isLoading} />;
  }
  if (looksLikeDiff(data.filename, content)) {
    if (loaded.isLoading && !content) {
      return <div className="flex h-full items-center justify-center text-[11px] text-zinc-600">diff を読み込んでいます</div>;
    }
    if (loaded.error && !content) {
      return (
        <ErrorNotice
          className="m-3 text-[11px]"
          copyLabel="diff プレビューエラーをコピー"
          message={`diff を読み込めませんでした: ${loaded.error}`}
        />
      );
    }
    return <CodePreviewContent data={{ type: 'code', filename: data.filename, language: 'diff', diff: content }} />;
  }
  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-3 py-2 bg-zinc-900 border-b border-zinc-800/60 flex-shrink-0">
        <div className="flex items-center gap-2">
          <FileText size={12} className="text-zinc-500" />
          <span className="text-[11px] font-mono text-zinc-300">{data.filename}</span>
        </div>
        <div className="flex items-center gap-2">
          {data.url && (
            <a
              href={safeHref(data.url)}
              download={data.downloadName ?? data.filename}
              className="inline-flex items-center gap-1 rounded-md border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-400 hover:border-zinc-700 hover:text-zinc-200"
            >
              <ExternalLink size={11} />
              開く
            </a>
          )}
          <span className="text-[10px] text-zinc-600">{data.size}</span>
        </div>
      </div>
      {data.path && (
        <div className="border-b border-zinc-800/60 bg-zinc-950/60 px-3 py-2 font-mono text-[10px] text-zinc-600">
          {data.path}
        </div>
      )}
      {looksLikeJson && (
        <div className="border-b border-zinc-800/60 bg-zinc-950/60 px-3 py-2 text-[10px] text-zinc-500">
          JSON の詳細です。必要なときだけ内容を確認してください。
        </div>
      )}
      <div className="flex-1 overflow-y-auto">
        {loaded.isLoading ? (
          <div className="flex h-full items-center justify-center text-[11px] text-zinc-600">内容を読み込んでいます</div>
        ) : loaded.error && !content ? (
          <ErrorNotice
            className="m-3 text-[11px]"
            copyLabel="ファイルプレビューエラーをコピー"
            message={`内容を読み込めませんでした: ${loaded.error}`}
          />
        ) : (
          <pre className="text-[11px] font-mono leading-[1.6]">
            {(content || '').split('\n').map((line, i) => (
              <div key={i} className="px-3 text-zinc-400 min-h-[1.6em]">
                <span className="inline-block w-7 text-right mr-3 text-zinc-700 select-none text-[10px]">
                  {i + 1}
                </span>
                {line}
              </div>
            ))}
          </pre>
        )}
      </div>
    </div>
  );
}

function MemoPreviewContent({
  value,
  onChange,
}: {
  value: string;
  onChange?: (value: string) => void;
}) {
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-zinc-800/60 bg-zinc-900 px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <NotebookPen size={12} className="flex-shrink-0 text-zinc-500" />
          <span className="truncate text-[11px] font-medium text-zinc-300">memo.md</span>
        </div>
        <span className="text-[10px] text-zinc-600">local</span>
      </div>
      <textarea
        value={value}
        onChange={(event) => onChange?.(event.target.value)}
        placeholder="ここに作業メモを書けます。AI が見ていた path、HTML preview、ブラウザ操作のスクショなどを開いた横で残しておけます。"
        className="h-full flex-1 resize-none border-none bg-[#0a0a0c] p-4 text-[13px] leading-6 text-zinc-200 outline-none placeholder:text-zinc-700"
      />
    </div>
  );
}

function ImagePreviewContent({ data }: { data: ImagePreview }) {
  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 px-3 py-2 bg-zinc-900 border-b border-zinc-800/60 flex-shrink-0">
        <Image size={12} className="text-zinc-500" />
        <span className="text-[11px] text-zinc-300">{data.alt}</span>
      </div>
      <div className="flex-1 p-4 flex items-center justify-center overflow-y-auto">
        {safePreviewImageUrl(data.url) ? (
          <img
            src={safePreviewImageUrl(data.url)}
            alt={data.alt}
            className="max-w-full max-h-full rounded-lg border border-zinc-800"
          />
        ) : (
          <div className="w-full aspect-square max-w-[200px] bg-zinc-800/30 rounded-lg border border-zinc-800 flex items-center justify-center">
            <Image size={32} className="text-zinc-700" />
          </div>
        )}
      </div>
      {data.prompt && (
        <div className="px-3 py-2 border-t border-zinc-800/60 flex-shrink-0">
          <p className="text-[10px] text-zinc-600">
            <span className="text-zinc-500">Prompt:</span> {data.prompt}
          </p>
        </div>
      )}
    </div>
  );
}

// ============================================================
// Canvas timeline
// ============================================================

function CanvasTimelineContent({
  items,
  onOpenPreview,
}: {
  items: ToolPreviewItem[];
  onOpenPreview: (item: ToolPreviewItem) => void;
}) {
  const timelineItems = useMemo(() => buildToolPreviewTimelineItems(items), [items]);

  return (
    <div className="h-full overflow-y-auto bg-[#0a0a0c] p-3">
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <Clock size={13} className="shrink-0 text-amber-300" />
          <h2 className="truncate text-[12px] font-semibold text-zinc-300">Timeline</h2>
        </div>
        <span className="shrink-0 rounded-md border border-zinc-800 bg-zinc-950 px-2 py-0.5 font-mono text-[10px] text-zinc-500">
          {timelineItems.length}
        </span>
      </div>
      <div className="grid gap-2">
        {timelineItems.map((item) => (
          <button
            key={item.id}
            type="button"
            onClick={() => onOpenPreview(item)}
            className="group flex min-w-0 items-start gap-3 rounded-lg border border-zinc-800/80 bg-zinc-950/45 px-3 py-2 text-left transition-colors hover:border-zinc-700 hover:bg-zinc-900/70 focus-visible:border-zinc-600 focus-visible:outline-none"
          >
            <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-zinc-900 text-zinc-400">
              {previewIcon(item.data, 13)}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[12px] font-medium text-zinc-300">{previewTitle(item.data)}</span>
              <span className="mt-0.5 block truncate text-[10px] text-zinc-600">{previewTypeLabel(item.data)} · {item.toolStepId}</span>
            </span>
          </button>
        ))}
        {timelineItems.length === 0 && (
          <div className="rounded-lg border border-zinc-800 bg-zinc-950/50 px-3 py-8 text-center text-[11px] text-zinc-600">
            Canvas に表示できる成果物はまだありません。
          </div>
        )}
      </div>
    </div>
  );
}

// ============================================================
// ToolPreviewPanel (main export)
// ============================================================

interface ToolPreviewPanelProps {
  previews: ToolPreviewItem[];
  isVisible: boolean;
  onClose: () => void;
  mode: ToolPreviewMode;
  onModeChange: (mode: ToolPreviewMode) => void;
  activePreviewId?: string | null;
  memo?: string;
  onMemoChange?: (value: string) => void;
}

export const CANVAS_CLOSE_LABEL = 'Canvasを閉じる';

export function CanvasCloseButton({ onClose }: { onClose: () => void }) {
  return (
    <button
      type="button"
      onClick={onClose}
      className="p-1 text-zinc-600 hover:text-zinc-300 transition-colors"
      title={CANVAS_CLOSE_LABEL}
      aria-label={CANVAS_CLOSE_LABEL}
    >
      <X size={14} />
    </button>
  );
}

export function ToolPreviewPanel({
  previews,
  isVisible,
  onClose,
  mode,
  onModeChange,
  activePreviewId,
  memo,
  onMemoChange,
}: ToolPreviewPanelProps) {
  const [foregroundPreview, setForegroundPreview] = useState<ArtifactPreviewDialogItem | null>(null);
  const [activeTabId, setActiveTabId] = useState(TIMELINE_TAB_ID);
  const [openPreviewIds, setOpenPreviewIds] = useState<string[]>([]);
  const [isPickerOpen, setIsPickerOpen] = useState(false);
  const [memoTabCreated, setMemoTabCreated] = useState(
    activePreviewId === MEMO_PREVIEW_ID || activePreviewId === 'memo',
  );
  const displayItems = useMemo(
    () => buildToolPreviewDisplayItems(
      previews,
      memo,
      memoTabCreated ? MEMO_PREVIEW_ID : activePreviewId,
    ),
    [activePreviewId, memo, memoTabCreated, previews],
  );
  const pickerItems = useMemo(
    () => buildCanvasTabPickerItems(displayItems, memo, Boolean(onMemoChange)),
    [displayItems, memo, onMemoChange],
  );
  const displayItemIdsKey = displayItems.map((item) => item.id).join('|');

  const openPreviewTab = (item: ToolPreviewItem) => {
    setOpenPreviewIds((ids) => selectCanvasTab(ids, item).openPreviewIds);
    setActiveTabId(item.id);
    if (item.id === MEMO_PREVIEW_ID) setMemoTabCreated(true);
    setIsPickerOpen(false);
  };

  const closePreviewTab = (previewId: string) => {
    setOpenPreviewIds((ids) => ids.filter((id) => id !== previewId));
    if (previewId === MEMO_PREVIEW_ID && !memo?.trim()) {
      setMemoTabCreated(false);
    }
    if (activeTabId === previewId) {
      setActiveTabId(TIMELINE_TAB_ID);
    }
  };

  useEffect(() => {
    setOpenPreviewIds((ids) => ids.filter((id) => displayItems.some((item) => item.id === id)));
    if (activeTabId !== TIMELINE_TAB_ID && !displayItems.some((item) => item.id === activeTabId)) {
      setActiveTabId(TIMELINE_TAB_ID);
    }
  }, [activeTabId, displayItemIdsKey, displayItems]);

  useEffect(() => {
    if (!activePreviewId) return;
    const item = displayItems.find((candidate) => matchesPreviewId(candidate, activePreviewId));
    if (item) {
      openPreviewTab(item);
    }
  }, [activePreviewId, mode, displayItemIdsKey]);

  useEffect(() => {
    if (!isVisible) setForegroundPreview(null);
  }, [isVisible]);

  if (!isVisible || displayItems.length === 0) return null;

  const openTabItems = openPreviewIds
    .map((id) => displayItems.find((item) => item.id === id))
    .filter((item): item is ToolPreviewItem => Boolean(item));
  const current = activeTabId === TIMELINE_TAB_ID
    ? null
    : displayItems.find((item) => item.id === activeTabId) ?? null;
  const isMemo = current?.id === MEMO_PREVIEW_ID;

  const renderContent = () => {
    if (!current) return <CanvasTimelineContent items={displayItems} onOpenPreview={openPreviewTab} />;
    if (isMemo) return <MemoPreviewContent value={memo ?? ''} onChange={onMemoChange} />;
    switch (current.data.type) {
      case 'web':
        return <WebPreviewContent data={current.data} />;
      case 'code':
        return <CodePreviewContent data={current.data} />;
      case 'file':
        return <FilePreviewContent data={current.data} />;
      case 'image':
        return <ImagePreviewContent data={current.data} />;
    }
  };

  return (
    <div className="flex flex-col h-full border-l border-zinc-800/60 bg-[#0a0a0c] w-full rumi-anim-fade-right">
      <div className="relative flex min-h-11 items-center gap-1.5 border-b border-zinc-800/60 bg-zinc-950/60 px-2">
        <div className="flex shrink-0 items-center gap-1.5 px-1 text-[11px] font-semibold text-zinc-300">
          <Layers size={13} className="text-zinc-500" />
          <span>Canvas</span>
        </div>
        <div className="flex min-w-0 flex-1 items-end gap-1 overflow-x-auto pt-2">
          <button
            type="button"
            onClick={() => setActiveTabId(TIMELINE_TAB_ID)}
            className={cn(
              'flex h-8 max-w-[132px] shrink-0 items-center gap-1.5 rounded-t-md border px-2 text-[11px] transition-colors',
              activeTabId === TIMELINE_TAB_ID
                ? 'border-zinc-800 border-b-[#0a0a0c] bg-[#0a0a0c] text-zinc-100'
                : 'border-transparent bg-zinc-900/45 text-zinc-500 hover:bg-zinc-900 hover:text-zinc-300'
            )}
            title="Timeline"
          >
            <Clock size={12} />
            <span className="truncate">Timeline</span>
          </button>
          {openTabItems.map((item) => (
            <div
              key={item.id}
              className={cn(
                'group flex h-8 max-w-[152px] shrink-0 items-center rounded-t-md border text-[11px] transition-colors',
                activeTabId === item.id
                  ? 'border-zinc-800 border-b-[#0a0a0c] bg-[#0a0a0c] text-zinc-100'
                  : 'border-transparent bg-zinc-900/45 text-zinc-500 hover:bg-zinc-900 hover:text-zinc-300'
              )}
              title={previewTitle(item.data)}
            >
              <button
                type="button"
                onClick={() => setActiveTabId(item.id)}
                className="flex h-full min-w-0 flex-1 items-center gap-1.5 px-2 text-left"
              >
                {item.id === MEMO_PREVIEW_ID ? <NotebookPen size={12} /> : previewIcon(item.data, 12)}
                <span className="min-w-0 truncate">{item.id === MEMO_PREVIEW_ID ? 'memo.md' : previewTitle(item.data)}</span>
              </button>
              <button
                type="button"
                aria-label={`${previewTitle(item.data)} を閉じる`}
                className="mr-1 flex h-4 w-4 shrink-0 items-center justify-center rounded text-zinc-600 hover:bg-zinc-800 hover:text-zinc-200"
                onClick={(event) => {
                  event.stopPropagation();
                  closePreviewTab(item.id);
                }}
              >
                <X size={10} />
              </button>
            </div>
          ))}
        </div>
        <div className="relative shrink-0">
          <button
            type="button"
            onClick={() => setIsPickerOpen((value) => !value)}
            className="mb-px flex h-7 w-7 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-900 hover:text-zinc-200"
            title="Canvas タブを追加"
            aria-label="Canvas タブを追加"
          >
            <Plus size={14} />
          </button>
          {isPickerOpen && (
            <div className="absolute right-0 top-8 rumi-layer-global-overlay w-64 overflow-hidden rounded-lg border border-zinc-800 bg-zinc-950 shadow-2xl">
              <div className="border-b border-zinc-800 px-3 py-2 text-[10px] font-medium uppercase tracking-wide text-zinc-500">
                新規タブ
              </div>
              <div className="max-h-72 overflow-y-auto p-1">
                {pickerItems.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => openPreviewTab(item)}
                    className="flex w-full min-w-0 items-center gap-2 rounded-md px-2 py-2 text-left text-[11px] text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
                  >
                    {item.id === MEMO_PREVIEW_ID ? <NotebookPen size={12} /> : previewIcon(item.data, 12)}
                    <span className="min-w-0 flex-1 truncate">{item.id === MEMO_PREVIEW_ID ? 'memo.md' : previewTitle(item.data)}</span>
                    <span className="shrink-0 text-[10px] text-zinc-600">{item.id === MEMO_PREVIEW_ID ? 'Memo' : previewTypeLabel(item.data)}</span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-0.5">
          <button
            onClick={() => onModeChange(mode === 'auto' ? 'manual' : 'auto')}
            className={cn(
              'flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-medium transition-colors border',
              mode === 'auto'
                ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
                : 'bg-zinc-800 text-zinc-500 border-zinc-800'
            )}
            title={
              mode === 'auto'
                ? 'Auto: ツール使用時に自動切替'
                : 'Manual: クリックで表示'
            }
          >
            {mode === 'auto' ? <Eye size={10} /> : <EyeOff size={10} />}
            {mode === 'auto' ? 'Auto' : 'Manual'}
          </button>

          <button
            onClick={() => current && setForegroundPreview(artifactDialogItemFromToolPreview(current))}
            disabled={!current}
            className="p-1 text-zinc-600 hover:text-zinc-300 transition-colors disabled:opacity-25"
            title="Foreground preview"
            aria-label="Open foreground preview"
          >
            <Maximize2 size={13} />
          </button>
          <CanvasCloseButton onClose={onClose} />
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-hidden">{renderContent()}</div>
      <ArtifactPreviewDialog item={foregroundPreview} onClose={() => setForegroundPreview(null)} />
    </div>
  );
}
