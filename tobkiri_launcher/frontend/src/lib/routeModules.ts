import {lazy, type ComponentType, type LazyExoticComponent} from 'react';

import type {PanelRouteKey} from './routes';

export type AdvancedRouteModuleKey =
  | 'packs'
  | 'packDetail'
  | 'nodes'
  | 'graphEditor'
  | 'profileGraph'
  | 'aiInput'
  | 'apiMap'
  | 'profileWorkspace'
  | 'startup'
  | 'flows'
  | 'settings';

type RouteModuleLoader = () => Promise<unknown>;

const rawRouteModuleLoaders: Record<AdvancedRouteModuleKey, RouteModuleLoader> = {
  packs: () => import('../pages/Packs'),
  packDetail: () => import('../pages/PackDetail'),
  nodes: () => import('../pages/NodeManager'),
  graphEditor: () => Promise.all([
    import('@xyflow/react/dist/style.css'),
    import('../pages/GraphEditor'),
  ]).then(([, routeModule]) => routeModule),
  profileGraph: () => Promise.all([
    import('@xyflow/react/dist/style.css'),
    import('../pages/ProfileGraphEditor'),
  ]).then(([, routeModule]) => routeModule),
  aiInput: () => import('../pages/AiInputInspector'),
  apiMap: () => import('../pages/ApiMap'),
  profileWorkspace: () => import('../pages/ProfileWorkspace'),
  startup: () => import('../pages/StartupProfiles'),
  flows: () => Promise.all([
    import('@xyflow/react/dist/style.css'),
    import('../pages/Flows'),
  ]).then(([, routeModule]) => routeModule),
  settings: () => import('../pages/Settings'),
};

export const advancedRouteModuleSources: Record<AdvancedRouteModuleKey, string> = {
  packs: 'src/pages/Packs.tsx',
  packDetail: 'src/pages/PackDetail.tsx',
  nodes: 'src/pages/NodeManager.tsx',
  graphEditor: 'src/pages/GraphEditor.tsx',
  profileGraph: 'src/pages/ProfileGraphEditor.tsx',
  aiInput: 'src/pages/AiInputInspector.tsx',
  apiMap: 'src/pages/ApiMap.tsx',
  profileWorkspace: 'src/pages/ProfileWorkspace.tsx',
  startup: 'src/pages/StartupProfiles.tsx',
  flows: 'src/pages/Flows.tsx',
  settings: 'src/pages/Settings.tsx',
};

const routeModulePromises = new Map<AdvancedRouteModuleKey, Promise<unknown>>();

export function preloadRouteModule(key: AdvancedRouteModuleKey): Promise<unknown> {
  const existing = routeModulePromises.get(key);
  if (existing) return existing;

  const promise = rawRouteModuleLoaders[key]().catch((error) => {
    routeModulePromises.delete(key);
    throw error;
  });
  routeModulePromises.set(key, promise);
  return promise;
}

const panelRouteToModule: Partial<Record<PanelRouteKey, AdvancedRouteModuleKey>> = {
  packs: 'packs',
  nodes: 'nodes',
  graphEditor: 'graphEditor',
  profileGraph: 'profileGraph',
  aiInput: 'aiInput',
  apiMap: 'apiMap',
  profileWorkspace: 'profileWorkspace',
  startup: 'startup',
  flows: 'flows',
  settings: 'settings',
};

export function preloadPanelRoute(route: PanelRouteKey): Promise<unknown> | null {
  const key = panelRouteToModule[route];
  return key ? preloadRouteModule(key) : null;
}

function lazyNamedRoute(
  key: AdvancedRouteModuleKey,
  exportName: string,
): LazyExoticComponent<ComponentType> {
  return lazy(async () => {
    const routeModule = await preloadRouteModule(key) as Record<string, unknown>;
    const component = routeModule[exportName];
    if (!component || (typeof component !== 'function' && typeof component !== 'object')) {
      throw new Error(`Route module ${key} did not export ${exportName}`);
    }
    return {default: component as ComponentType};
  });
}

export const LazyPacks = lazyNamedRoute('packs', 'Packs');
export const LazyPackDetail = lazyNamedRoute('packDetail', 'PackDetail');
export const LazyNodeManager = lazyNamedRoute('nodes', 'NodeManager');
export const LazyGraphEditor = lazyNamedRoute('graphEditor', 'GraphEditor');
export const LazyProfileGraphEditor = lazyNamedRoute('profileGraph', 'ProfileGraphEditor');
export const LazyAiInputInspector = lazyNamedRoute('aiInput', 'AiInputInspector');
export const LazyApiMap = lazyNamedRoute('apiMap', 'ApiMap');
export const LazyProfileWorkspace = lazyNamedRoute('profileWorkspace', 'ProfileWorkspace');
export const LazyStartupProfiles = lazyNamedRoute('startup', 'StartupProfiles');
export const LazyFlows = lazyNamedRoute('flows', 'Flows');
export const LazySettings = lazyNamedRoute('settings', 'Settings');
