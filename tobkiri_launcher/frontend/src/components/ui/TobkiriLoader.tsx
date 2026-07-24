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
  const source = scene === 'startup' ? startupAnimationUrl : transitionAnimationUrl;
  return (
    <img
      alt=""
      aria-hidden="true"
      className={`h-4 w-8 shrink-0 rounded-sm object-contain dark:invert ${className}`}
      data-loading-scene={scene}
      src={source}
    />
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
  const positionClass = scope === 'inline'
    ? 'relative min-h-0 flex-1'
    : scope === 'screen'
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
