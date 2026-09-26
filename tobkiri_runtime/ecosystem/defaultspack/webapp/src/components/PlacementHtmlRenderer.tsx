import type { PlacementManifest } from "../lib/placement";
import { resolvePlacementHtmlRendering } from "../lib/placement";

export function PlacementHtmlRenderer({
  manifest,
  onDisable,
}: {
  manifest: PlacementManifest;
  onDisable?: () => void;
}) {
  const rendering = resolvePlacementHtmlRendering(manifest);
  if (rendering.kind !== "blocked_html") return null;
  return (
    <section
      role="status"
      aria-label={`${manifest.label} extension content blocked`}
      data-placement-renderer="blocked-html"
      data-placement-id={rendering.manifestId}
      className="h-full w-full rounded-lg border border-amber-500/40 bg-amber-500/5 p-4 text-sm text-zinc-200"
    >
      <p className="font-semibold text-amber-200">Untrusted HTML blocked</p>
      <p className="mt-2 font-medium text-zinc-100">{manifest.label}</p>
      <p className="mt-1 break-all text-xs text-zinc-400">Source: {rendering.sourceLabel}</p>
      <p className="mt-1 break-all text-xs text-zinc-500">
        Placement: {rendering.manifestId} · {rendering.byteLength} bytes requested
      </p>
      <p className="mt-3 leading-5 text-zinc-300">{rendering.message}</p>
      <p className="mt-3 text-xs text-zinc-500">
        Disable this placement or ask the extension author to provide a verified component or template.
      </p>
      {onDisable && (
        <button
          type="button"
          onClick={onDisable}
          aria-label={`Disable ${manifest.label} placement`}
          className="mt-3 rounded-md border border-amber-500/50 px-2.5 py-1 text-xs font-medium text-amber-100 hover:border-amber-400 hover:bg-amber-500/10"
        >
          Disable placement
        </button>
      )}
    </section>
  );
}
