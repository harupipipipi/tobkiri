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

export const DEVTOOLS_PANEL_ROUTE_KEYS = [
  'graph',
  'flow',
  'apiMap',
  'aiInput',
  'nodeManager',
  'profileFiles',
  'profileWiring',
] as const satisfies readonly PanelRouteKey[];

export type DevtoolsPanelRouteKey = (typeof DEVTOOLS_PANEL_ROUTE_KEYS)[number];

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

const primaryViewerNavGroups = [
  {
    id: 'workspace',
    labelKey: 'nav.group.workspace',
    routes: ['home', 'packs'] satisfies PanelRouteKey[],
  },
  {
    id: 'preferences',
    labelKey: 'nav.group.preferences',
    routes: ['profile', 'settings'] satisfies PanelRouteKey[],
  },
] as const;

const devtoolsViewerNavGroup = {
  id: 'devtools',
  labelKey: 'nav.group.devtools',
  routes: DEVTOOLS_PANEL_ROUTE_KEYS,
} as const;

export type ViewerNavGroup =
  | (typeof primaryViewerNavGroups)[number]
  | typeof devtoolsViewerNavGroup;

/** Keep Devtools out of default navigation without changing its stable routes. */
export function viewerNavGroups(devtoolsEnabled: boolean): ViewerNavGroup[] {
  return devtoolsEnabled
    ? [...primaryViewerNavGroups, devtoolsViewerNavGroup]
    : [...primaryViewerNavGroups];
}

export function isDevtoolsPanelRouteKey(
  route: PanelRouteKey,
): route is DevtoolsPanelRouteKey {
  return DEVTOOLS_PANEL_ROUTE_KEYS.some((candidate) => candidate === route);
}

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
