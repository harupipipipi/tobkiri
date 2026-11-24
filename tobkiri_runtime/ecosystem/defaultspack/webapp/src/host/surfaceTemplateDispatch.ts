import type { FrontendCapabilityInvoker, FrontendCatalog } from "./frontendContracts";
import { viewOperationRequest, type RegisteredCatalogView, type ViewInputContext } from "./catalogViewRegistry";
import type { SurfaceRequest } from "./surfaceTemplateContract";
import { surfaceRequestPayload } from "./surfaceTemplateState";
import { viewOperationOutcome } from "./viewControlState";

/** Explicit Surface actions use the ordinary captured capability/approval path. */
export async function dispatchSurfaceRequest(
  catalog: FrontendCatalog, registered: RegisteredCatalogView, capabilities: FrontendCapabilityInvoker,
  request: SurfaceRequest, snapshot: unknown, context: ViewInputContext,
  additions: Record<string, unknown>, isCurrent: () => boolean,
): Promise<{ kind: "approval" } | { kind: "completed"; result: unknown } | null> {
  const payload = surfaceRequestPayload(request, snapshot, context, additions);
  const invocation = payload && isCurrent() ? viewOperationRequest(catalog, registered, request.operation, payload) : null;
  if (!invocation) return null;
  const result = await capabilities.invokeAction(invocation);
  if (!isCurrent() || !viewOperationRequest(catalog, registered, request.operation, payload!)) return null;
  const outcome = viewOperationOutcome(result);
  if (outcome === "approval") return { kind: "approval" };
  if (outcome === "failed") throw new Error("surface_operation_failed");
  return { kind: "completed", result };
}
