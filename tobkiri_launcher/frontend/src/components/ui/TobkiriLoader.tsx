import { useAppStore } from '@/src/store';
import { viewerLayers } from '@/src/lib/layers';

interface TobkiriLoaderProps {
  label?: string;
  className?: string;
  scene?: 'startup' | 'transition';
  scope?: 'panel' | 'screen' | 'inline';
}

const startupAnimationUrl = '/panel/assets/tobkiri-startup-blade-cut.svg';
const transitionAnimationUrl = '/panel/assets/tobkiri-startup-blade-cut.svg';

export function TobkiriLoadingMark({
  className = '',
  scene = 'transition',
}: Pick<TobkiriLoaderProps, 'className' | 'scene'>) {
  return (
    <svg
      aria-hidden="true"
      className={`h-4 w-4 shrink-0 animate-spin motion-reduce:animate-none ${className}`}
      data-loading-indicator="spinner"
      data-loading-scene={scene}
      fill="none"
      viewBox="0 0 24 24"
    >
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  );
}

/** Full-page Tobkiri startup animation used while an entire surface loads. */
export function TobkiriLoader({
  label = 'Loading Tobkiri…',
  className = '',
  scene = 'transition',
  scope = 'panel',
}: TobkiriLoaderProps) {
  const isSidebarOpen = useAppStore((state) => state.isSidebarOpen);
  const source = scene === 'startup' ? startupAnimationUrl : transitionAnimationUrl;
  if (scope === 'inline') {
    return (
      <div
        className={`inline-flex items-center gap-2 text-sm text-text-muted ${className}`}
        data-loading-scope={scope}
        role="status"
        aria-live="polite"
      >
        <TobkiriLoadingMark scene={scene} />
        <span>{label}</span>
      </div>
    );
  }
  const positionClass = scope === 'screen'
    ? `fixed inset-0 ${viewerLayers.loading}`
    : `fixed inset-y-0 right-0 ${viewerLayers.loading} left-0 transition-[left] duration-300 ${
      isSidebarOpen ? 'md:left-[240px]' : 'md:left-[56px]'
    }`;
  return (
    <div
      className={`flex items-center justify-center bg-bg-main px-6 py-12 ${positionClass} ${className}`}
      data-loading-scope={scope}
      role="status"
      aria-live="polite"
    >
      <div className="flex w-full max-w-xs flex-col items-center gap-4 text-center">
        <img
          alt=""
          aria-hidden="true"
          className="aspect-[2/1] w-full animate-pulse object-contain mix-blend-multiply dark:mix-blend-screen dark:invert"
          data-loading-scene={scene}
          src={source}
        />
        <span className="text-sm text-text-muted">{label}</span>
      </div>
    </div>
  );
}
