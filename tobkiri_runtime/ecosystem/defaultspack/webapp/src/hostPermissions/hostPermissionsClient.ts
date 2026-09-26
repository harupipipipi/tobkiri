import { api, type AuthorityRequest } from "../lib/api";
import { redactDiagnosticText } from "../lib/clientDiagnostics";
import { fetchDesktopSystemInfo, type DesktopSystemInfo } from "../lib/desktopSystemInfo";
import { buildHostPermissionRows, hostPermissionSummary, type HostPermissionRow } from "./hostPermissions";

export type HostPermissionsSnapshot = {
  info: DesktopSystemInfo | null;
  authorityRequests: AuthorityRequest[];
  rows: HostPermissionRow[];
  summary: ReturnType<typeof hostPermissionSummary>;
  authorityUnavailable?: boolean;
  authorityDiagnostic?: string;
};

export async function fetchHostPermissionsSnapshot(): Promise<HostPermissionsSnapshot> {
  const [info, authorityResult] = await Promise.all([
    fetchDesktopSystemInfo(),
    api.listAuthorityRequests({ status: "all" })
      .then((result) => ({ result, error: "" }))
      .catch((error) => ({
        result: { requests: [], pending: [], count: 0 },
        error: redactDiagnosticText(
          error instanceof Error ? `${error.name}: ${error.message}` : error,
          480,
        ) || "Authority request lookup failed.",
      })),
  ]);
  const authorityRequests = authorityResult.result.requests;
  const rows = buildHostPermissionRows(info, authorityRequests);
  return {
    info,
    authorityRequests,
    rows,
    summary: hostPermissionSummary(rows),
    ...(authorityResult.error
      ? {
          authorityUnavailable: true,
          authorityDiagnostic: authorityResult.error,
        }
      : {}),
  };
}
