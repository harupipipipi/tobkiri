import {
  LAUNCHER_VERSION_ACCESSIBLE_LABEL,
  LAUNCHER_VERSION_LABEL,
} from '@/src/lib/launcherMetadata';

export function ViewerVersionLabel() {
  return (
    <div
      className="pointer-events-none flex min-w-0 shrink-0 select-none justify-end bg-bg-main px-3 pb-2 pt-1 text-[10px] leading-none text-text-muted opacity-45 sm:px-4"
      aria-label={LAUNCHER_VERSION_ACCESSIBLE_LABEL}
    >
      <span className="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap">
        {LAUNCHER_VERSION_LABEL}
      </span>
    </div>
  );
}
