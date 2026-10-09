/** Optional browser-session layout. Coordinates are never workflow authority. */
export interface WorkflowPoint { x: number; y: number }
const storageKey = (key: string) => `tobkiri-workflow-layout-v1:${encodeURIComponent(key)}`;

function sanitize(value: unknown, ids: ReadonlySet<string>): Record<string, WorkflowPoint> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const entries: Array<[string, WorkflowPoint]> = [];
  for (const [id, raw] of Object.entries(value).slice(0, 256)) {
    if (!ids.has(id) || !raw || typeof raw !== 'object' || Array.isArray(raw)) continue;
    const point = raw as Record<string, unknown>;
    if (Object.keys(point).sort().join(',') !== 'x,y'
      || typeof point.x !== 'number' || typeof point.y !== 'number'
      || !Number.isFinite(point.x) || !Number.isFinite(point.y)
      || point.x < 0 || point.y < 0 || point.x > 20000 || point.y > 20000) continue;
    entries.push([id, {x: point.x, y: point.y}]);
  }
  return Object.fromEntries(entries);
}

export function readWorkflowLayout(
  storage: Pick<Storage, 'getItem'> | null,
  key: string | undefined,
  ids: ReadonlySet<string>,
): Record<string, WorkflowPoint> {
  if (!storage || !key || key.length > 1024) return {};
  try {
    const raw = storage.getItem(storageKey(key));
    return raw && raw.length <= 32768 ? sanitize(JSON.parse(raw), ids) : {};
  } catch { return {}; }
}

export function saveWorkflowLayout(
  storage: Pick<Storage, 'setItem'> | null,
  key: string | undefined,
  positions: Record<string, WorkflowPoint>,
  ids: ReadonlySet<string>,
): boolean {
  if (!storage || !key || key.length > 1024) return false;
  try {
    storage.setItem(storageKey(key), JSON.stringify(sanitize(positions, ids)));
    return true;
  } catch { return false; }
}
