import { Link, useLocation } from 'react-router';
import { useAppStore } from '@/src/store';
import { useT } from '@/src/lib/i18n';
import { cn } from '@/src/lib/utils';
import { isPanelRouteActive, panelRouteMeta, viewerNavGroups, type PanelRouteKey } from '@/src/lib/routes';
import { Avatar } from '@/src/components/ui/Avatar';
import { LAUNCHER_DISPLAY_NAME } from '@/src/lib/launcherBrand';
import { preloadPanelRoute } from '@/src/lib/routeModules';
import { Popover, PopoverContent, PopoverTrigger } from '@/src/components/ui/Popover';
import {
  BrainCircuit,
  Folder,
  FolderOpen,
  GitBranch,
  Home,
  Network,
  PanelLeft,
  Route,
  Settings,
  Share2,
  UserRound,
  Workflow,
  type LucideIcon,
} from 'lucide-react';

type NavGroup = {
  id: 'workspace' | 'advanced';
  label: string;
  items: { to: string; icon: LucideIcon; label: string; route: PanelRouteKey }[];
};

const sidebarAnimation = 'duration-300 ease-[cubic-bezier(0.4,0,0.2,1)]';

const routeIcons: Record<PanelRouteKey, LucideIcon> = {
  home: Home,
  setup: Home,
  packs: Folder,
  profile: UserRound,
  settings: Settings,
  profileWiring: Share2,
  profileFiles: FolderOpen,
  flow: Workflow,
  graph: GitBranch,
  aiInput: BrainCircuit,
  apiMap: Route,
  nodeManager: Network,
};

export function Sidebar() {
  const t = useT();
  const location = useLocation();
  const profile = useAppStore(state => state.profile);
  const isSidebarOpen = useAppStore(state => state.isSidebarOpen);
  const setSidebarOpen = useAppStore(state => state.setSidebarOpen);

  const navGroups: NavGroup[] = viewerNavGroups.map((group) => ({
    id: group.id,
    label: t(group.labelKey),
    items: group.routes.map((route) => {
      const meta = panelRouteMeta[route];
      return {
        to: meta.path,
        icon: routeIcons[route],
        label: t(meta.navKey || meta.titleKey),
        route,
      };
    }),
  }));

  return (
    <aside
      className={cn(
        "hidden flex-shrink-0 flex-col bg-bg-sidebar border-r border-border transition-[width] overflow-hidden will-change-[width] md:flex",
        sidebarAnimation,
        isSidebarOpen ? "w-[240px]" : "w-[56px]"
      )}
    >
      {/* Brand + Toggle */}
      <div
        className={cn(
          "grid h-14 grid-cols-[minmax(0,1fr)_32px] items-center overflow-hidden border-b border-border transition-[padding]",
          sidebarAnimation,
          isSidebarOpen ? "px-4" : "px-3",
        )}
      >
        <span
          className={cn(
            "block min-w-0 overflow-hidden whitespace-nowrap text-base font-semibold tracking-tight text-text-main transition-[max-width,opacity,transform]",
            sidebarAnimation,
            isSidebarOpen ? "max-w-32 translate-x-0 opacity-100" : "max-w-0 -translate-x-2 opacity-0",
          )}
          aria-hidden={!isSidebarOpen}
        >
          {LAUNCHER_DISPLAY_NAME}
        </span>
        <button
          onClick={() => setSidebarOpen(!isSidebarOpen)}
          className={cn(
            "justify-self-center rounded-md p-1.5 text-text-muted transition-[background-color,color,transform] hover:bg-bg-hover hover:text-text-main focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]",
            sidebarAnimation,
            !isSidebarOpen && "scale-105",
          )}
          aria-label={isSidebarOpen ? "Collapse sidebar" : "Expand sidebar"}
        >
          <PanelLeft className={cn("w-4 h-4 transition-transform", sidebarAnimation, !isSidebarOpen && "rotate-180")} />
        </button>
      </div>

      {/* Navigation */}
      <nav
        className={cn(
          "flex-1 overflow-y-auto py-3 transition-[padding]",
          sidebarAnimation,
          isSidebarOpen ? "px-3" : "px-1.5",
        )}
        aria-label="Main navigation"
      >
        <ul
          className={cn(
            "flex flex-col transition-[gap]",
            sidebarAnimation,
            isSidebarOpen ? "gap-4" : "gap-2",
          )}
        >
          {navGroups.map((group, groupIndex) => (
            <li key={group.id} className="flex flex-col gap-1">
              {groupIndex > 0 && (
                <div
                  className={cn(
                    "mx-2 h-px bg-border/60 transition-[margin,opacity]",
                    sidebarAnimation,
                    isSidebarOpen ? "my-0 opacity-0" : "my-1 opacity-100",
                  )}
                  aria-hidden="true"
                />
              )}
              <div
                className={cn(
                  "overflow-hidden px-3 text-[10px] font-semibold uppercase tracking-[0.18em] text-text-muted/70 transition-[max-height,padding,opacity,transform,border-color]",
                  sidebarAnimation,
                  groupIndex > 0 && "border-t border-border/60",
                  isSidebarOpen
                    ? cn("max-h-10 translate-x-0 pt-1 opacity-100", groupIndex > 0 && "pt-3")
                    : "max-h-0 -translate-x-1 pt-0 opacity-0 border-transparent",
                )}
                aria-hidden={!isSidebarOpen}
              >
                {group.label}
              </div>
              <ul
                className={cn(
                  "flex flex-col transition-[gap]",
                  sidebarAnimation,
                  isSidebarOpen ? "gap-1" : "gap-1.5",
                )}
              >
                {group.items.map((link) => {
                  const isActive = isPanelRouteActive(location.pathname, link.to);
                  return (
                    <li key={link.to}>
                      <Link
                        to={link.to}
                        title={!isSidebarOpen ? link.label : undefined}
                        aria-label={link.label}
                        aria-current={isActive ? 'page' : undefined}
                        onFocus={() => { void preloadPanelRoute(link.route); }}
                        onPointerEnter={() => { void preloadPanelRoute(link.route); }}
                        onTouchStart={() => { void preloadPanelRoute(link.route); }}
                        className={cn(
                          "group relative flex items-center rounded-lg text-sm font-medium transition-[gap,padding,background-color,color]",
                          sidebarAnimation,
                          isSidebarOpen ? "gap-3 px-3 py-2" : "justify-center gap-0 p-2.5",
                          isActive
                            ? "bg-accent/8 text-accent"
                            : "text-text-muted hover:bg-bg-hover hover:text-text-main"
                        )}
                      >
                        <div
                          className={cn(
                            "absolute left-0 top-1/2 w-[3px] -translate-y-1/2 rounded-r-full bg-accent transition-[height,opacity]",
                            sidebarAnimation,
                            isActive && isSidebarOpen ? "h-4 opacity-100" : "h-2 opacity-0",
                          )}
                          aria-hidden="true"
                        />
                        <link.icon className={cn("w-[18px] h-[18px] shrink-0", isActive ? "text-accent" : "text-text-muted group-hover:text-text-main")} />
                        <span
                          className={cn(
                            "block min-w-0 overflow-hidden whitespace-nowrap transition-[max-width,opacity,transform]",
                            sidebarAnimation,
                            isSidebarOpen ? "max-w-40 translate-x-0 opacity-100" : "max-w-0 -translate-x-2 opacity-0",
                          )}
                          aria-hidden={!isSidebarOpen}
                        >
                          {link.label}
                        </span>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </li>
          ))}
        </ul>
      </nav>

      {/* User section */}
      <div className="border-t border-border">
        <div
          className={cn(
            "transition-[padding]",
            sidebarAnimation,
            isSidebarOpen ? "p-3" : "flex justify-center p-1.5",
          )}
          >
          <Popover>
            <PopoverTrigger
              className={cn(
                "flex min-h-11 items-center rounded-lg text-left transition-[gap,padding,background-color] hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]",
                sidebarAnimation,
                isSidebarOpen ? "w-full gap-3 p-2" : "justify-center gap-0 p-2",
              )}
              aria-label={`${profile.username} profile and settings`}
              aria-haspopup="dialog"
              title={!isSidebarOpen ? `${profile.username} profile and settings` : undefined}
            >
              <Avatar src={profile.avatar} username={profile.username} className="size-7 text-xs" />
              <span
                className={cn(
                  "min-w-0 flex-1 overflow-hidden transition-[max-width,opacity,transform]",
                  sidebarAnimation,
                  isSidebarOpen ? "max-w-32 translate-x-0 opacity-100" : "max-w-0 -translate-x-2 opacity-0",
                )}
                aria-hidden={!isSidebarOpen}
              >
                <span className="block truncate text-sm font-medium text-text-main">{profile.username}</span>
              </span>
            </PopoverTrigger>
            <PopoverContent align="right" className="w-64" role="dialog" aria-label="Profile menu">
              <div className="border-b border-border px-3 py-2">
                <p className="truncate text-sm font-semibold text-text-main">{profile.username}</p>
                <p className="text-xs text-text-muted">Launcher-local profile</p>
              </div>
              <nav className="flex flex-col gap-1 p-1" aria-label="Profile and settings">
                {(['profile', 'settings'] as const).map((route) => {
                  const meta = panelRouteMeta[route];
                  const Icon = routeIcons[route];
                  const isActive = location.pathname === meta.path;
                  return (
                    <Link
                      key={route}
                      to={meta.path}
                      aria-current={isActive ? 'page' : undefined}
                      onFocus={() => { void preloadPanelRoute(route); }}
                      onPointerEnter={() => { void preloadPanelRoute(route); }}
                      className={cn(
                        "flex min-h-11 items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]",
                        isActive ? "bg-accent/8 text-accent" : "text-text-muted hover:bg-bg-hover hover:text-text-main",
                      )}
                    >
                      <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                      <span>{t(meta.navKey || meta.titleKey)}</span>
                    </Link>
                  );
                })}
              </nav>
            </PopoverContent>
          </Popover>
        </div>
      </div>
    </aside>
  );
}
