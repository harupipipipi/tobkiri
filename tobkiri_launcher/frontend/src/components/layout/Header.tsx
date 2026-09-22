import { Link, useLocation } from 'react-router';
import { Menu } from 'lucide-react';
import { TobkiriLoadingMark } from '@/src/components/ui/TobkiriLoader';
import { useAppStore } from '@/src/store';
import { useT } from '@/src/lib/i18n';
import { cn } from '@/src/lib/utils';
import { describeRuntimeBadge } from '@/src/lib/runtimeHealth';
import { isPanelRouteActive, panelRouteMeta, panelRouteTitleKey, panelRoutes, viewerNavGroups } from '@/src/lib/routes';
import { preloadPanelRoute } from '@/src/lib/routeModules';
import { Avatar } from '@/src/components/ui/Avatar';
import { Popover, PopoverContent, PopoverTrigger } from '@/src/components/ui/Popover';

export function Header() {
  const t = useT();
  const profile = useAppStore(state => state.profile);
  const runtimeReady = useAppStore(state => state.runtimeReady);
  const runtimeStatus = useAppStore(state => state.runtimeStatus);
  const runtimeError = useAppStore(state => state.runtimeError);
  const runtimeDisconnected = useAppStore(state => state.runtimeDisconnected);
  const lastRuntimeHealthyAt = useAppStore(state => state.lastRuntimeHealthyAt);
  const location = useLocation();
  const runtimeBadge = describeRuntimeBadge({
    runtimeReady,
    runtimeStatus,
    runtimeError,
    runtimeDisconnected,
    lastRuntimeHealthyAt,
  });
  const profileReconfirmationRequired = runtimeStatus === 'profile_reconfirmation_required';

  const pageTitle = t(panelRouteTitleKey(location.pathname));

  const runtimePill = (() => {
    if (profileReconfirmationRequired) {
      return {
        label: 'Profile reconfirmation required',
        dotClass: 'bg-amber-500',
        textClass: 'text-amber-600 dark:text-amber-400',
      };
    }
    if (runtimeStatus === 'error') {
      return {
        label: 'Runtime error',
        dotClass: 'bg-red-500',
        textClass: 'text-red-600 dark:text-red-400',
      };
    }
    if (!runtimeReady) {
      return {
        label: 'Warming up',
        dotClass: 'bg-amber-500 animate-pulse',
        textClass: 'text-amber-600 dark:text-amber-400',
      };
    }
    return {
      label: 'Runtime ready',
      dotClass: 'bg-emerald-500',
      textClass: 'text-emerald-600 dark:text-emerald-400',
    };
  })();

  return (
    <header
      data-tauri-drag-region
      className="z-40 flex h-14 shrink-0 items-center justify-between border-b border-border bg-bg-header px-6 transition-colors duration-[var(--transition-base)]"
    >
      <div className="flex min-w-0 items-center gap-3">
        <div className="md:hidden">
          <Popover>
            <PopoverTrigger className="rounded-md p-2 text-text-muted transition hover:bg-bg-hover hover:text-text-main" aria-label={t('nav.open_menu')} aria-haspopup="dialog">
              <Menu className="h-4 w-4" />
            </PopoverTrigger>
            <PopoverContent align="left" className="w-64" role="dialog" aria-label={t('nav.mobile_navigation')}>
              <nav aria-label={t('nav.mobile_navigation')} className="max-h-[70vh] overflow-y-auto p-1">
                {viewerNavGroups.map((group) => (
                  <div key={group.id} className="py-1">
                    <div className="px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-text-muted/70">
                      {t(group.labelKey)}
                    </div>
                    <div className="flex flex-col gap-1">
                      {group.routes.map((route) => {
                        const meta = panelRouteMeta[route];
                        const isActive = isPanelRouteActive(location.pathname, meta.path);
                        return (
                          <Link
                            key={route}
                            to={meta.path}
                            aria-current={isActive ? 'page' : undefined}
                            className={cn(
                              "min-h-11 rounded-md px-3 py-2 text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]",
                              isActive ? "bg-accent/8 text-accent" : "text-text-muted hover:bg-bg-hover hover:text-text-main",
                            )}
                          >
                            {t(meta.navKey || meta.titleKey)}
                          </Link>
                        );
                      })}
                    </div>
                  </div>
                ))}
              </nav>
            </PopoverContent>
          </Popover>
        </div>
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-2">
            <h1 className="truncate text-sm font-medium text-text-main">{pageTitle}</h1>
            <span
              className={`hidden rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.16em] sm:inline-flex ${
                runtimeBadge.tone === 'success'
                  ? 'bg-emerald-500/12 text-emerald-600 dark:text-emerald-300'
                  : runtimeBadge.tone === 'danger'
                    ? 'bg-red-500/12 text-red-600 dark:text-red-300'
                    : 'bg-amber-500/12 text-amber-600 dark:text-amber-300'
              }`}
            >
              {runtimeBadge.label}
            </span>
          </div>
          <p className="hidden truncate text-[11px] text-text-muted sm:block">{runtimeBadge.detail}</p>
        </div>
      </div>

      <div className="flex items-center gap-3">
        {profileReconfirmationRequired ? (
          <Link
            to={panelRoutes.setup}
            className={cn(
              "rumi-control-pill inline-flex min-h-11 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]",
              runtimePill.textClass,
            )}
            aria-label="Profile reconfirmation required. Open Setup to review and activate the Profile."
            aria-live="polite"
            title={runtimePill.label}
          >
            <span className={cn("rumi-control-pill-dot", runtimePill.dotClass)} />
            <span>{runtimePill.label}</span>
          </Link>
        ) : (
          <div
            className={cn(
              "rumi-control-pill hidden md:inline-flex",
              runtimePill.textClass,
            )}
            role="status"
            aria-live="polite"
            title={runtimePill.label}
          >
            {!runtimeReady && runtimeStatus !== 'error' ? (
              <TobkiriLoadingMark className="h-3 w-6" />
            ) : (
              <span className={cn("rumi-control-pill-dot", runtimePill.dotClass)} />
            )}
            <span>{runtimePill.label}</span>
          </div>
        )}
        <Popover>
          <PopoverTrigger
            className="flex min-h-11 items-center gap-2 rounded-lg px-2 text-left transition hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
            aria-label={`${profile.username} profile and settings`}
            aria-haspopup="dialog"
          >
            <span className="text-xs text-text-muted hidden sm:block">{profile.username}</span>
            <Avatar
              src={profile.avatar}
              username={profile.username}
              alt={`${profile.username} avatar`}
              className="size-7 text-xs"
            />
          </PopoverTrigger>
          <PopoverContent align="right" className="w-64" role="dialog" aria-label="Profile menu">
            <div className="border-b border-border px-3 py-2">
              <p className="truncate text-sm font-semibold text-text-main">{profile.username}</p>
              <p className="text-xs text-text-muted">Launcher-local profile</p>
            </div>
            <nav className="flex flex-col gap-1 p-1" aria-label="Profile and settings">
              {(['profile', 'settings'] as const).map((route) => {
                const meta = panelRouteMeta[route];
                const isActive = location.pathname === meta.path;
                return (
                  <Link
                    key={route}
                    to={meta.path}
                    aria-current={isActive ? 'page' : undefined}
                    onFocus={() => { void preloadPanelRoute(route); }}
                    onPointerEnter={() => { void preloadPanelRoute(route); }}
                    className={cn(
                      "flex min-h-11 items-center rounded-md px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]",
                      isActive ? "bg-accent/8 text-accent" : "text-text-muted hover:bg-bg-hover hover:text-text-main",
                    )}
                  >
                    {t(meta.navKey || meta.titleKey)}
                  </Link>
                );
              })}
            </nav>
          </PopoverContent>
        </Popover>
      </div>
    </header>
  );
}
