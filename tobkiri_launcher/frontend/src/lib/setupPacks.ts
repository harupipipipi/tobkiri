import { apiFetch } from './api';
import { panelRoutes } from './routes';

type SetupPacksPayload = {
  selected_setup_pack_ids?: unknown;
  active_target_pack_id?: unknown;
};

const PANEL_BASE_PATH = '/panel';
export const SETUP_PACK_RETURN_PARAM = 'setup_pack_done';

export function selectedSetupPackIds(payload: unknown): string[] {
  if (!payload || typeof payload !== 'object') return [];
  const selected = (payload as SetupPacksPayload).selected_setup_pack_ids;
  if (!Array.isArray(selected)) return [];
  return selected
    .map(item => String(item || '').trim())
    .filter(Boolean);
}

export function activeTargetPackId(payload: unknown): string {
  if (!payload || typeof payload !== 'object') return '';
  return String((payload as SetupPacksPayload).active_target_pack_id || '').trim();
}

export function hasActiveSetupPackSelection(payload: unknown): boolean {
  return selectedSetupPackIds(payload).length > 0 && Boolean(activeTargetPackId(payload));
}

export async function hasSelectedSetupPack(): Promise<boolean> {
  const packs = await apiFetch<SetupPacksPayload>('/api/setup/packs');
  return hasActiveSetupPackSelection(packs);
}

export function setupPackSelectionUrl(
  returnTo = `${PANEL_BASE_PATH}${panelRoutes.setup}?${SETUP_PACK_RETURN_PARAM}=1`,
  colorMode = 'dark',
): string {
  const normalizedColorMode = colorMode === 'light' ? 'light' : 'dark';
  return `/setup?return_to=${encodeURIComponent(returnTo)}&color_mode=${normalizedColorMode}`;
}
