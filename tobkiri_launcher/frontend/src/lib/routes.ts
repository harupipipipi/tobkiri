export const panelRoutes = {
  home: '/',
  setup: '/setup',
  packs: '/packs',
  packDetail: (id: string) => `/packs/${id}`,
  nodes: '/nodes',
  graphEditor: '/graphs',
  profileGraph: '/profile-graph',
  aiInput: '/ai-input',
  apiMap: '/api-map',
  profileWorkspace: '/profile-workspace',
  startup: '/startup',
  flows: '/flows',
  settings: '/settings',
} as const;

export type PanelRouteKey = Exclude<keyof typeof panelRoutes, 'packDetail'>;

export type PanelRouteMeta = {
  path: string;
  titleKey: string;
  navKey?: string;
};

export const panelRouteMeta: Record<PanelRouteKey, PanelRouteMeta> = {
  home: { path: panelRoutes.home, titleKey: 'nav.home', navKey: 'nav.home' },
  setup: { path: panelRoutes.setup, titleKey: 'nav.setup' },
  packs: { path: panelRoutes.packs, titleKey: 'nav.packs', navKey: 'nav.packs' },
  nodes: { path: panelRoutes.nodes, titleKey: 'nav.nodes', navKey: 'nav.nodes' },
  graphEditor: { path: panelRoutes.graphEditor, titleKey: 'nav.graphs', navKey: 'nav.graphs' },
  profileGraph: { path: panelRoutes.profileGraph, titleKey: 'nav.profile_graph', navKey: 'nav.profile_graph' },
  aiInput: { path: panelRoutes.aiInput, titleKey: 'nav.ai_input', navKey: 'nav.ai_input' },
  apiMap: { path: panelRoutes.apiMap, titleKey: 'nav.api_map', navKey: 'nav.api_map' },
  profileWorkspace: { path: panelRoutes.profileWorkspace, titleKey: 'nav.profile_workspace', navKey: 'nav.profile_workspace' },
  startup: { path: panelRoutes.startup, titleKey: 'nav.startup', navKey: 'nav.startup' },
  flows: { path: panelRoutes.flows, titleKey: 'nav.flows', navKey: 'nav.flows' },
  settings: { path: panelRoutes.settings, titleKey: 'nav.settings', navKey: 'nav.settings' },
};

export const viewerNavGroups = [
  {
    id: 'workspace',
    labelKey: 'nav.group.workspace',
    routes: ['home', 'packs', 'flows', 'nodes'] satisfies PanelRouteKey[],
  },
  {
    id: 'advanced',
    labelKey: 'nav.group.advanced',
    routes: ['graphEditor', 'profileGraph', 'aiInput', 'apiMap', 'profileWorkspace', 'settings'] satisfies PanelRouteKey[],
  },
] as const;

export function panelRouteTitleKey(pathname: string): string {
  if (pathname === panelRoutes.packs || pathname.startsWith(`${panelRoutes.packs}/`)) {
    return panelRouteMeta.packs.titleKey;
  }

  const match = Object.values(panelRouteMeta).find((meta) => meta.path === pathname);
  return match?.titleKey ?? 'nav.unknown';
}

function withQuery(path: string, params: Record<string, string | null | undefined>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    const normalized = String(value || '').trim();
    if (normalized) {
      search.set(key, normalized);
    }
  });
  const query = search.toString();
  return query ? `${path}?${query}` : path;
}

export function profileGraphRoute(profileId?: string | null): string {
  return withQuery(panelRoutes.profileGraph, {profile: profileId});
}

export function apiMapRoute(options?: {profileId?: string | null; focus?: string | null}): string {
  return withQuery(panelRoutes.apiMap, {
    profile_id: options?.profileId,
    focus: options?.focus,
  });
}

export function aiInputRoute(profileId?: string | null): string {
  return withQuery(panelRoutes.aiInput, {profile: profileId});
}
