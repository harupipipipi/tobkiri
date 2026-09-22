export const panelRoutes = {
  home: '/',
  setup: '/setup',
  packs: '/packs',
  profile: '/profile',
  settings: '/settings',
  profileWiring: '/profile-graph',
  profileFiles: '/profile-workspace',
  flow: '/flows',
  graph: '/graphs',
  aiInput: '/ai-input',
  apiMap: '/api-map',
  nodeManager: '/nodes',
  packDetail: (id: string) => `/packs/${id}`,
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
  profile: { path: panelRoutes.profile, titleKey: 'nav.profile', navKey: 'nav.profile' },
  settings: { path: panelRoutes.settings, titleKey: 'nav.settings', navKey: 'nav.settings' },
  profileWiring: {
    path: panelRoutes.profileWiring,
    titleKey: 'nav.profile_wiring',
    navKey: 'nav.profile_wiring',
  },
  profileFiles: {
    path: panelRoutes.profileFiles,
    titleKey: 'nav.profile_files',
    navKey: 'nav.profile_files',
  },
  flow: { path: panelRoutes.flow, titleKey: 'nav.flow', navKey: 'nav.flow' },
  graph: { path: panelRoutes.graph, titleKey: 'nav.graph', navKey: 'nav.graph' },
  aiInput: { path: panelRoutes.aiInput, titleKey: 'nav.ai_input', navKey: 'nav.ai_input' },
  apiMap: { path: panelRoutes.apiMap, titleKey: 'nav.api_map', navKey: 'nav.api_map' },
  nodeManager: {
    path: panelRoutes.nodeManager,
    titleKey: 'nav.node_manager',
    navKey: 'nav.node_manager',
  },
};

export const viewerNavGroups = [
  {
    id: 'workspace',
    labelKey: 'nav.group.workspace',
    routes: ['home', 'packs'] satisfies PanelRouteKey[],
  },
  {
    id: 'advanced',
    labelKey: 'nav.group.advanced',
    routes: [
      'profile',
      'settings',
      'profileWiring',
      'profileFiles',
      'flow',
      'graph',
      'aiInput',
      'apiMap',
      'nodeManager',
    ] satisfies PanelRouteKey[],
  },
] as const;

export function panelRouteTitleKey(pathname: string): string {
  if (pathname === panelRoutes.packs || pathname.startsWith(`${panelRoutes.packs}/`)) {
    return panelRouteMeta.packs.titleKey;
  }

  const match = Object.values(panelRouteMeta).find((meta) => meta.path === pathname);
  return match?.titleKey ?? 'nav.unknown';
}

/** Match stable panel routes without treating Profile Files/Wiring as Profile. */
export function isPanelRouteActive(pathname: string, routePath: string): boolean {
  if (routePath === panelRoutes.packs) {
    return pathname === routePath || pathname.startsWith(`${routePath}/`);
  }
  return pathname === routePath;
}
