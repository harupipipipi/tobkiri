import { RollingEditCount } from "./RollingEditCount";

export type FileEditTimelineEntry = {
  eventId: string;
  fileId: string;
  path: string;
  timestamp: number;
  order: number;
  addedLines: number | null;
  deletedLines: number | null;
};

export type FileEditTimelineProps = {
  entries: readonly FileEditTimelineEntry[];
  onOpen?: (entry: FileEditTimelineEntry) => void;
};

function editTime(timestamp: number): { iso: string; label: string } | null {
  const date = new Date(timestamp);
  if (!Number.isFinite(date.getTime())) return null;
  const iso = date.toISOString();
  return { iso, label: `${iso.slice(0, 10)} ${iso.slice(11, 19)} UTC` };
}

/** Show confirmed file edits chronologically with counts visible at narrow widths. */
export function FileEditTimeline({ entries, onOpen }: FileEditTimelineProps) {
  const chronological = [...entries].sort((a, b) => a.order - b.order || a.timestamp - b.timestamp || a.eventId.localeCompare(b.eventId));
  return <section aria-label="ファイル編集履歴" className="min-w-0 w-full">
    {chronological.length === 0 ? <p className="text-sm text-zinc-500">確認済みのファイル編集はありません。</p> : <ol className="m-0 grid min-w-0 list-none gap-3 p-0">
      {chronological.map((entry, index) => {
        const parts = entry.path.split(/[\\/]/).filter(Boolean);
        const filename = parts[parts.length - 1] ?? entry.path;
        const time = editTime(entry.timestamp);
        return <li key={entry.eventId} data-event-id={entry.eventId} data-edit-order={entry.order} className="grid min-w-0 gap-2 rounded-lg border border-zinc-700 p-3">
          <div className="min-w-0">
            {onOpen ? <button type="button" className="max-w-full whitespace-normal break-all text-left font-medium underline underline-offset-2" onClick={() => onOpen(entry)}>{filename}</button> : <span className="block break-all font-medium">{filename}</span>}
            <p className="m-0 mt-1 break-all text-xs text-zinc-400">{entry.path}</p>
          </div>
          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-zinc-500">
            {time ? <time dateTime={time.iso}>{time.label}</time> : <span>時刻未確認</span>}
            <span title={`順序 ${entry.order}`}>編集 #{index + 1}</span>
          </div>
          <div className="flex min-w-0 flex-wrap gap-x-4 gap-y-2 text-sm tabular-nums" aria-label="編集行数">
            <span className="inline-flex shrink-0 items-baseline gap-1 text-red-400" aria-label="削除行数"><span aria-hidden="true">--</span><RollingEditCount value={entry.deletedLines} animateInitial /></span>
            <span className="inline-flex shrink-0 items-baseline gap-1 text-blue-400" aria-label="追加行数"><span aria-hidden="true">++</span><RollingEditCount value={entry.addedLines} animateInitial /></span>
          </div>
        </li>;
      })}
    </ol>}
  </section>;
}
