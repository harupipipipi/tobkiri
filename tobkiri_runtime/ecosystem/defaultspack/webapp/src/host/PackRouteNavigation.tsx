import { profileScreenPath } from "../lib/profileRoute";
import { contributionsForRoute } from "./DynamicFrontendHost";
import type { FrontendCatalog } from "./frontendContracts";

export type PackScreenLink = {
  contributionId: string;
  href: string;
  label: string;
  route: string;
};

/** Offer only unique, active, Profile-qualified screen routes from the captured catalog. */
export function packScreenLinks(
  catalog: FrontendCatalog,
  activePlanHash: string,
): PackScreenLink[] {
  if (catalog.plan_hash !== activePlanHash) return [];
  return catalog.contributions.flatMap((item) => {
    if (item.kind !== "route" || item.mode === "application_builtin" || !item.route) {
      return [];
    }
    if (contributionsForRoute(catalog, item.route, activePlanHash)[0] !== item) {
      return [];
    }
    try {
      return [{
        contributionId: item.contribution_id,
        href: profileScreenPath(catalog.profile_id, item.route),
        label: item.label,
        route: item.route,
      }];
    } catch {
      return [];
    }
  });
}

export function PackRouteNavigation({
  catalog,
  route,
  activePlanHash,
}: {
  catalog: FrontendCatalog;
  route: string;
  activePlanHash: string;
}) {
  const links = packScreenLinks(catalog, activePlanHash);
  if (links.length === 0) return null;
  const home = contributionsForRoute(
    catalog,
    catalog.selected_entry_route,
    activePlanHash,
  );
  let homeHref: string | null = null;
  if (home.length === 1 && route !== catalog.selected_entry_route) {
    try {
      homeHref = profileScreenPath(catalog.profile_id, catalog.selected_entry_route);
    } catch {
      // An invalid selected entry never becomes a navigation target.
    }
  }
  return (
    <nav aria-label="Pack screens" data-pack-route-navigation
      className="rumi-layer-global-overlay fixed right-3 top-14 max-w-[min(20rem,calc(100vw-1.5rem))] text-sm">
      <details className="rounded-lg border border-zinc-700 bg-zinc-950/95 text-zinc-100 shadow-lg">
        <summary className="cursor-pointer px-3 py-2 font-medium">Pack screens</summary>
        <div className="flex max-h-[50vh] flex-col gap-1 overflow-y-auto border-t border-zinc-700 p-2">
          {homeHref && <a href={homeHref} className="rounded px-2 py-1 hover:bg-zinc-800">Home</a>}
          {links.map((link) => (
            <a key={link.contributionId} href={link.href}
              aria-current={link.route === route ? "page" : undefined}
              className="truncate rounded px-2 py-1 hover:bg-zinc-800"
              title={link.label}>{link.label}</a>
          ))}
        </div>
      </details>
    </nav>
  );
}
