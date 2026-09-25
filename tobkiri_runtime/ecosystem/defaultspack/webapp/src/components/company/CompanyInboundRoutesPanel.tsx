import { Plus, Route, Trash2 } from "lucide-react";
import { useState } from "react";

import type { CompanyMutationReceipt } from "../../features/company/companyWorkspaceState";
import { useCompanyMutation } from "../../features/company/useCompanyMutation";
import type { CompanyInboundRoute } from "../../lib/api";

export function CompanyInboundRoutesPanel({
  routes,
  busy = false,
  onUpsertRoute,
  onDeleteRoute,
}: {
  routes: CompanyInboundRoute[];
  busy?: boolean;
  onUpsertRoute?: (route: Partial<CompanyInboundRoute>, operationId: string) => Promise<CompanyMutationReceipt<CompanyInboundRoute>>;
  onDeleteRoute?: (routeId: string, operationId: string) => Promise<CompanyMutationReceipt<{ deleted: boolean; route_id: string }>>;
}) {
  const [provider, setProvider] = useState("local");
  const [source, setSource] = useState("");
  const upsertMutation = useCompanyMutation("company-route", onUpsertRoute);
  const deleteMutation = useCompanyMutation("company-route-delete", onDeleteRoute);

  return (
    <section className="space-y-2 p-2">
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-[11px] font-semibold uppercase tracking-wide text-zinc-500">Inbound Routes</h4>
        <span className="text-[10px] text-zinc-600">{routes.length}</span>
      </div>

      {onUpsertRoute && (
        <form
          className="grid grid-cols-[74px_minmax(0,1fr)_28px] gap-1.5"
          onSubmit={(event) => {
            event.preventDefault();
            const cleanSource = source.trim();
            if (!cleanSource) return;
            void upsertMutation.submit({ provider, source: cleanSource, channel_id: "ops-company", enabled: true })
              .then((receipt) => {
                if (receipt.phase === "committed") {
                  setSource((current) => current.trim() === cleanSource ? "" : current);
                }
              });
          }}
        >
          <select
            value={provider}
            onChange={(event) => setProvider(event.target.value)}
            disabled={busy || upsertMutation.pending}
            className="h-8 rounded-md border border-zinc-800 bg-zinc-950 px-1.5 text-[11px] text-zinc-300 outline-none"
          >
            <option value="local">local</option>
            <option value="slack">slack</option>
            <option value="discord">discord</option>
            <option value="p2p">p2p</option>
          </select>
          <input
            value={source}
            onChange={(event) => setSource(event.target.value)}
            disabled={busy || upsertMutation.pending}
            placeholder="source id"
            className="h-8 min-w-0 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-[12px] text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
          />
          <button
            type="submit"
            disabled={busy || upsertMutation.pending || !source.trim()}
            aria-busy={upsertMutation.pending}
            className="flex h-8 w-8 items-center justify-center rounded-md bg-zinc-100 text-zinc-950 hover:bg-white disabled:opacity-30"
            title="Add route"
          >
            <Plus size={13} />
          </button>
        </form>
      )}
      {upsertMutation.state.phase !== "idle" && (
        <p role={upsertMutation.state.phase === "rejected" ? "alert" : "status"} className={upsertMutation.state.phase === "rejected" ? "text-[11px] text-amber-200" : "text-[11px] text-emerald-300"}>
          {upsertMutation.state.message}
          {upsertMutation.canRetry && <button type="button" className="ml-2 underline" onClick={() => void upsertMutation.retry()}>Retry</button>}
        </p>
      )}

      <div className="space-y-1">
        {routes.map((route) => (
          <div key={route.id} className="flex items-center gap-2 rounded-md border border-zinc-800/70 bg-zinc-950/40 px-2 py-1.5">
            <Route size={13} className={route.enabled === false ? "text-zinc-700" : "text-zinc-500"} />
            <div className="min-w-0 flex-1">
              <p className="truncate text-[12px] text-zinc-200">{route.provider || "local"} / {route.source || route.id}</p>
              <p className="truncate text-[10px] text-zinc-500">{route.channel_id || "ops-company"}</p>
            </div>
            {onDeleteRoute && (
              <button
                type="button"
                onClick={() => void deleteMutation.submit(route.id)}
                disabled={busy || deleteMutation.pending}
                className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-md text-zinc-600 hover:bg-zinc-800 hover:text-zinc-200 disabled:opacity-40"
                title="Delete route"
              >
                <Trash2 size={12} />
              </button>
            )}
          </div>
        ))}
        {routes.length === 0 && (
          <div className="rounded-md border border-zinc-800/70 bg-zinc-950/40 px-2 py-2 text-[11px] text-zinc-500">
            No inbound routes.
          </div>
        )}
      </div>
      {deleteMutation.state.phase !== "idle" && (
        <p role={deleteMutation.state.phase === "rejected" ? "alert" : "status"} className={deleteMutation.state.phase === "rejected" ? "text-[11px] text-amber-200" : "text-[11px] text-emerald-300"}>
          {deleteMutation.state.message}
          {deleteMutation.canRetry && <button type="button" className="ml-2 underline" onClick={() => void deleteMutation.retry()}>Retry</button>}
        </p>
      )}
    </section>
  );
}
