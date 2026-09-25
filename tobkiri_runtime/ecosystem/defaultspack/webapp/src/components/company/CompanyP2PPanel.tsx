import { Link2, Radio, Send, ShieldCheck, ShieldX } from "lucide-react";
import { useState } from "react";

import type { CompanyMutationReceipt } from "../../features/company/companyWorkspaceState";
import { useCompanyMutation } from "../../features/company/useCompanyMutation";
import type { P2PIdentity, P2PPairing, P2PPeer, P2PStatusResponse } from "../../lib/api";

export function CompanyP2PPanel({
  status,
  identity,
  peers,
  busy = false,
  onStartPairing,
  onSendMessage,
}: {
  status: P2PStatusResponse | null;
  identity?: P2PIdentity | null;
  peers: P2PPeer[];
  busy?: boolean;
  onStartPairing?: (peerLabel: string | undefined, operationId: string) => Promise<CompanyMutationReceipt<{ pairing: P2PPairing }>>;
  onSendMessage?: (peerId: string, text: string, operationId: string) => Promise<CompanyMutationReceipt<{ envelope: Record<string, unknown>; peer: P2PPeer }>>;
}) {
  const [pairLabel, setPairLabel] = useState("");
  const [messagePeerId, setMessagePeerId] = useState("");
  const [message, setMessage] = useState("");
  const pairingMutation = useCompanyMutation("p2p-pairing", (
    peerLabel: string | undefined,
    operationId,
  ) => onStartPairing!(peerLabel, operationId));
  const messageMutation = useCompanyMutation("p2p-message", (
    payload: { peerId: string; text: string },
    operationId,
  ) => onSendMessage!(payload.peerId, payload.text, operationId));
  const enabled = Boolean(status?.p2p?.enabled);
  const controlsDisabled = busy || !enabled;

  return (
    <section className="space-y-2 p-2">
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-[11px] font-semibold uppercase tracking-wide text-zinc-500">P2P</h4>
        <span className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] ${
          enabled ? "border-emerald-500/30 text-emerald-300" : "border-zinc-800 text-zinc-500"
        }`}>
          {enabled ? <ShieldCheck size={10} /> : <ShieldX size={10} />}
          {enabled ? "enabled" : "disabled"}
        </span>
      </div>

      {!enabled && (
        <p role="status" className="rounded-md border border-amber-500/20 bg-amber-500/5 px-2 py-1.5 text-[10px] text-amber-200/80">
          {status
            ? "P2P is disabled. Set RUMI_DEFAULTSPACK_P2P_ENABLED=1 and restart the backend before creating pairing state or sending messages."
            : "P2P status is unavailable. Pairing and messaging stay disabled until status loads."}
        </p>
      )}

      <div className="rounded-md border border-zinc-800/70 bg-zinc-950/40 px-2 py-1.5">
        <div className="flex items-center gap-1.5 text-[10px] text-zinc-500">
          <Radio size={10} />
          <span>{status?.approved_peer_count ?? 0} approved / {status?.peer_count ?? 0} peers</span>
        </div>
        {identity && (
          <p className="mt-1 truncate font-mono text-[10px] text-zinc-400" title={identity.fingerprint}>
            {identity.node_id}
          </p>
        )}
      </div>

      {onStartPairing && (
        <form
          className="grid grid-cols-[minmax(0,1fr)_28px] gap-1.5"
          onSubmit={(event) => {
            event.preventDefault();
            if (!enabled) return;
            const cleanLabel = pairLabel.trim();
            void pairingMutation.submit(cleanLabel || undefined).then((receipt) => {
              if (receipt.phase === "committed") {
                setPairLabel((current) => current.trim() === cleanLabel ? "" : current);
              }
            });
          }}
        >
          <input
            value={pairLabel}
            onChange={(event) => setPairLabel(event.target.value)}
            disabled={controlsDisabled || pairingMutation.pending}
            placeholder="peer label"
            className="h-8 min-w-0 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-[12px] text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
          />
          <button
            type="submit"
            disabled={controlsDisabled || pairingMutation.pending}
            aria-busy={pairingMutation.pending}
            className="flex h-8 w-8 items-center justify-center rounded-md border border-zinc-800 bg-zinc-900 text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
            title="Start pairing"
          >
            <Link2 size={13} />
          </button>
        </form>
      )}
      {pairingMutation.state.phase !== "idle" && (
        <p role={pairingMutation.state.phase === "rejected" ? "alert" : "status"} className={pairingMutation.state.phase === "rejected" ? "text-[11px] text-amber-200" : "text-[11px] text-emerald-300"}>
          {pairingMutation.state.message}
          {pairingMutation.canRetry && <button type="button" className="ml-2 underline" onClick={() => void pairingMutation.retry()}>Retry</button>}
        </p>
      )}

      <div className="space-y-1">
        {peers.map((peer) => (
          <div key={peer.peer_id} className="rounded-md border border-zinc-800/70 bg-zinc-950/40 px-2 py-1.5">
            <div className="flex items-center justify-between gap-2">
              <span className="truncate text-[12px] text-zinc-200">{peer.label || peer.peer_id}</span>
              <span className="rounded border border-zinc-800 px-1.5 py-0.5 text-[9px] text-zinc-500">{peer.status ?? "pending"}</span>
            </div>
            <p className="mt-1 truncate font-mono text-[10px] text-zinc-500">{peer.peer_id}</p>
          </div>
        ))}
        {peers.length === 0 && (
          <div className="rounded-md border border-zinc-800/70 bg-zinc-950/40 px-2 py-2 text-[11px] text-zinc-500">
            No peers paired.
          </div>
        )}
      </div>

      {onSendMessage && peers.length > 0 && (
        <form
          className="grid grid-cols-[90px_minmax(0,1fr)_28px] gap-1.5"
          onSubmit={(event) => {
            event.preventDefault();
            if (!enabled) return;
            const text = message.trim();
            const peerId = messagePeerId || peers[0]?.peer_id;
            if (!text || !peerId) return;
            void messageMutation.submit({ peerId, text }).then((receipt) => {
              if (receipt.phase === "committed") {
                setMessage((current) => current.trim() === text ? "" : current);
              }
            });
          }}
        >
          <select
            value={messagePeerId || peers[0]?.peer_id || ""}
            onChange={(event) => setMessagePeerId(event.target.value)}
            disabled={controlsDisabled || messageMutation.pending}
            className="h-8 rounded-md border border-zinc-800 bg-zinc-950 px-1.5 text-[11px] text-zinc-300 outline-none"
          >
            {peers.map((peer) => (
              <option key={peer.peer_id} value={peer.peer_id}>{peer.label || peer.peer_id}</option>
            ))}
          </select>
          <input
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            disabled={controlsDisabled || messageMutation.pending}
            placeholder="message"
            className="h-8 min-w-0 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-[12px] text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
          />
          <button
            type="submit"
            disabled={controlsDisabled || messageMutation.pending || !message.trim()}
            aria-busy={messageMutation.pending}
            className="flex h-8 w-8 items-center justify-center rounded-md bg-zinc-100 text-zinc-950 hover:bg-white disabled:opacity-30"
            title="Send P2P message"
          >
            <Send size={13} />
          </button>
        </form>
      )}
      {messageMutation.state.phase !== "idle" && (
        <p role={messageMutation.state.phase === "rejected" ? "alert" : "status"} className={messageMutation.state.phase === "rejected" ? "text-[11px] text-amber-200" : "text-[11px] text-emerald-300"}>
          {messageMutation.state.message}
          {messageMutation.canRetry && <button type="button" className="ml-2 underline" onClick={() => void messageMutation.retry()}>Retry</button>}
        </p>
      )}
    </section>
  );
}
