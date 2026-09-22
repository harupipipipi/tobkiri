import {lazy, type ComponentType, type LazyExoticComponent} from 'react';

import type {PanelRouteKey} from './routes';

export type RouteModuleKey =
  | 'packs'
  | 'packDetail'
  | 'profile'
  | 'settings'
  | 'profileWiring'
  | 'profileFiles'
  | 'flow'
  | 'graph'
  | 'aiInput'
  | 'apiMap'
  | 'nodeManager';

type RouteModuleLoader = () => Promise<unknown>;

const rawRouteModuleLoaders: Record<RouteModuleKey, RouteModuleLoader> = {
  packs: () => import('../pages/Packs'),
  packDetail: () => import('../pages/PackDetail'),
  profile: () => import('../pages/Profile'),
  settings: () => import('../pages/Settings'),
  profileWiring: () => import('../pages/ProfileWiring'),
  profileFiles: () => import('../pages/ProfileFiles'),
  flow: () => import('../pages/Flow'),
  graph: () => import('../pages/Graph'),
  aiInput: () => import('../pages/AiInput'),
  apiMap: () => import('../pages/ApiMap'),
  nodeManager: () => import('../pages/NodeManager'),
};

export const routeModuleSources: Record<RouteModuleKey, string> = {
  packs: 'src/pages/Packs.tsx',
  packDetail: 'src/pages/PackDetail.tsx',
  profile: 'src/pages/Profile.tsx',
  settings: 'src/pages/Settings.tsx',
  profileWiring: 'src/pages/ProfileWiring.tsx',
  profileFiles: 'src/pages/ProfileFiles.tsx',
  flow: 'src/pages/Flow.tsx',
  graph: 'src/pages/Graph.tsx',
  aiInput: 'src/pages/AiInput.tsx',
  apiMap: 'src/pages/ApiMap.tsx',
  nodeManager: 'src/pages/NodeManager.tsx',
};

const routeModulePromises = new Map<RouteModuleKey, Promise<unknown>>();

export function preloadRouteModule(key: RouteModuleKey): Promise<unknown> {
  const existing = routeModulePromises.get(key);
  if (existing) return existing;

  const promise = rawRouteModuleLoaders[key]().catch((error) => {
    routeModulePromises.delete(key);
    throw error;
  });
  routeModulePromises.set(key, promise);
  return promise;
}

const panelRouteToModule: Partial<Record<PanelRouteKey, RouteModuleKey>> = {
  packs: 'packs',
  profile: 'profile',
  settings: 'settings',
  profileWiring: 'profileWiring',
  profileFiles: 'profileFiles',
  flow: 'flow',
  graph: 'graph',
  aiInput: 'aiInput',
  apiMap: 'apiMap',
  nodeManager: 'nodeManager',
};

export function preloadPanelRoute(route: PanelRouteKey): Promise<unknown> | null {
  const key = panelRouteToModule[route];
  return key ? preloadRouteModule(key) : null;
}

function lazyNamedRoute(
  key: RouteModuleKey,
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
export const LazyProfile = lazyNamedRoute('profile', 'Profile');
export const LazySettings = lazyNamedRoute('settings', 'Settings');
export const LazyProfileWiring = lazyNamedRoute('profileWiring', 'ProfileWiring');
export const LazyProfileFiles = lazyNamedRoute('profileFiles', 'ProfileFiles');
export const LazyFlow = lazyNamedRoute('flow', 'Flow');
export const LazyGraph = lazyNamedRoute('graph', 'Graph');
export const LazyAiInput = lazyNamedRoute('aiInput', 'AiInput');
export const LazyApiMap = lazyNamedRoute('apiMap', 'ApiMap');
export const LazyNodeManager = lazyNamedRoute('nodeManager', 'NodeManager');
