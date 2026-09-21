import {Plus, Search} from 'lucide-react';

import {Badge} from '@/src/components/ui/Badge';
import {Button} from '@/src/components/ui/Button';
import {Input} from '@/src/components/ui/Input';
import type {ApiProfileGraphAvailableItem} from '@/src/lib/apiTypes';
import type {ProfileGraphCategory} from '@/src/lib/profileGraph';
import {PROFILE_GRAPH_CATEGORY_LABELS} from '@/src/lib/profileGraph';
import {cn} from '@/src/lib/utils';

interface ProfileGraphPaletteProps {
  activeCategory: ProfileGraphCategory;
  available: Record<ProfileGraphCategory, ApiProfileGraphAvailableItem[]>;
  selectedValues: string[];
  search: string;
  onSearchChange: (value: string) => void;
  onCategoryChange: (category: ProfileGraphCategory) => void;
  onAdd: (category: ProfileGraphCategory, item: ApiProfileGraphAvailableItem) => void;
}

export function ProfileGraphPalette({
  activeCategory,
  available,
  selectedValues,
  search,
  onSearchChange,
  onCategoryChange,
  onAdd,
}: ProfileGraphPaletteProps) {
  const searchValue = search.trim().toLowerCase();
  const items = (available[activeCategory] || []).filter((item) => {
    if (!searchValue) {
      return true;
    }
    return [item.id, item.label, item.kind]
      .filter(Boolean)
      .join(' ')
      .toLowerCase()
      .includes(searchValue);
  });

  return (
    <section className="flex min-h-0 flex-1 flex-col rounded-xl border border-border bg-bg-card p-3">
      <div className="mb-3 grid grid-cols-2 gap-1.5">
        {(Object.keys(PROFILE_GRAPH_CATEGORY_LABELS) as ProfileGraphCategory[]).map((category) => (
          <Button
            key={category}
            type="button"
            size="sm"
            variant={category === activeCategory ? 'default' : 'outline'}
            onClick={() => onCategoryChange(category)}
            className="w-full justify-start"
          >
            {PROFILE_GRAPH_CATEGORY_LABELS[category]}
          </Button>
        ))}
      </div>

      <div className="relative mb-3">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-text-muted" />
        <Input
          value={search}
          onChange={(event) => onSearchChange(event.target.value)}
          placeholder={`Search ${activeCategory.replace('_', ' ')}`}
          className="pl-9"
        />
      </div>

      <div className="mb-3 flex items-center justify-between text-xs text-text-muted">
        <span>{items.length} candidates</span>
        <Badge variant="outline">{selectedValues.length} selected</Badge>
      </div>

      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-1">
        {items.length ? items.map((item) => {
          const isSelected = selectedValues.includes(item.id);
          return (
            <div
              key={item.id}
              className={cn(
                'rounded-lg border p-2.5 transition-colors',
                isSelected ? 'border-accent bg-accent/10' : 'border-border hover:bg-bg-hover',
              )}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium text-text-main">{item.label || item.id}</div>
                  <div className="truncate text-xs text-text-muted">{item.id}</div>
                </div>
                <Button
                  type="button"
                  size="sm"
                  variant={isSelected ? 'secondary' : 'outline'}
                  onClick={() => onAdd(activeCategory, item)}
                >
                  <Plus className="h-3.5 w-3.5" />
                  {isSelected ? 'Again' : 'Add'}
                </Button>
              </div>
              <div className="mt-2 flex flex-wrap gap-2">
                <Badge variant="outline">{item.kind}</Badge>
                {isLaunchSurfaceCandidate(item) ? (
                  <Badge variant="default">launch surface</Badge>
                ) : null}
                {typeof item.path === 'string' && item.path ? (
                  <Badge variant="secondary">path</Badge>
                ) : null}
                {typeof item.method === 'string' && item.method ? (
                  <Badge variant="secondary">{item.method}</Badge>
                ) : null}
              </div>
            </div>
          );
        }) : (
          <div className="rounded-xl border border-dashed border-border px-4 py-8 text-center text-sm text-text-muted">
            No matching candidates in this category.
          </div>
        )}
      </div>
    </section>
  );
}

function isLaunchSurfaceCandidate(item: ApiProfileGraphAvailableItem): boolean {
  const componentType = String(item.component_type || '').toLowerCase();
  const launch = item.launch;
  const ports = Array.isArray(item.ports) ? item.ports : [];
  const hasSurfacePort = ports.some((port) => {
    if (!port || typeof port !== 'object') {
      return false;
    }
    const direction = String((port as {direction?: unknown}).direction || '').toLowerCase();
    const standards = Array.isArray((port as {standards?: unknown[]}).standards) ? (port as {standards: unknown[]}).standards : [];
    return direction === 'output' && standards.some((value) => String(value) === 'rumi.surface');
  });
  return (
    componentType === 'frontend' &&
    !!launch &&
    typeof launch === 'object' &&
    String((launch as {kind?: unknown}).kind || '').toLowerCase() === 'desktop_app' &&
    hasSurfacePort
  );
}
