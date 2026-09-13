type TitleBarProps = {
  appName?: string;
  appIcon?: string;
};

const LEGACY_PRODUCT_NAMES = new Set([
  "console",
  "rumi console",
  "rumi dp",
  "rumi defaultspack",
  "rumi defaultspack v2",
]);

export function displayAppName(value: string | undefined): string {
  const displayName = String(value ?? "").trim();
  if (!displayName || LEGACY_PRODUCT_NAMES.has(displayName.toLowerCase())) {
    return "Tobkiri";
  }
  return displayName;
}

export function hasTauriNativeChrome(target: unknown = globalThis): boolean {
  return Boolean(
    target
      && typeof target === "object"
      && "__TAURI_INTERNALS__" in target,
  );
}

export function TitleBar({ appName = "Tobkiri", appIcon }: TitleBarProps) {
  // Tauri uses the operating system's decorated window. Rendering another web
  // title bar inside it duplicates the native controls (notably —, □, × on
  // macOS), so the web chrome is reserved for standalone browser sessions.
  if (hasTauriNativeChrome()) {
    return null;
  }

  return (
    <div className="rumi-ambient h-8 flex items-center bg-[#09090b] border-b border-zinc-800/60 select-none flex-shrink-0 cursor-default rumi-anim-fade-down">
      <div className="flex items-center gap-2 px-3 flex-1 pointer-events-none">
        {appIcon ? (
          <img src={appIcon} alt="" className="w-4 h-4 rounded object-cover flex-shrink-0" />
        ) : (
          <div className="w-4 h-4 rounded bg-zinc-800 border border-zinc-700/80 flex items-center justify-center flex-shrink-0">
            <span className="text-[9px] font-mono font-bold text-zinc-300">&gt;</span>
          </div>
        )}
        <span className="text-[11px] font-medium text-zinc-500">
          {displayAppName(appName)}
        </span>
      </div>
    </div>
  );
}
