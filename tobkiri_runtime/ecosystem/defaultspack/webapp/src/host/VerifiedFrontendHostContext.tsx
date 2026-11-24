import { createContext, useContext, type ReactNode } from "react";
import type { FrontendCapabilityInvoker, FrontendCatalog } from "./frontendContracts";

export type VerifiedFrontendHost = {
  catalog: FrontendCatalog;
  activePlanHash: string;
  capabilities: FrontendCapabilityInvoker;
};
const context = createContext<VerifiedFrontendHost | null>(null);

/** The captured Host supplies identity and invocation; optional consumers fail closed. */
export function VerifiedFrontendHostProvider({
  value, children,
}: { value: VerifiedFrontendHost; children: ReactNode }) {
  return <context.Provider value={value}>{children}</context.Provider>;
}

export function useVerifiedFrontendHost(): VerifiedFrontendHost | null {
  return useContext(context);
}
