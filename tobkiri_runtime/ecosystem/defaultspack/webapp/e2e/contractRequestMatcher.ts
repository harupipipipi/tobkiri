import { DEFAULTSPACK_CONTRACT_ENDPOINT, defaultspackContractRoute, defaultspackContractUrl } from "../src/lib/api";

/** Match the exact canonical transport and method, retaining dynamic query data. */
export function canonicalRequestQuery(request: {url(): string; method(): string}, apiPath: string, method: string): URLSearchParams | null {
  if (request.method() !== method) return null;
  try {
    const url = new URL(request.url(), "https://fixture.test");
    if (!url.pathname.startsWith(DEFAULTSPACK_CONTRACT_ENDPOINT)) return null;
    const operation = decodeURIComponent(url.pathname.slice(DEFAULTSPACK_CONTRACT_ENDPOINT.length));
    const queryAt = operation.indexOf("?");
    const suffix = queryAt < 0 ? "" : operation.slice(queryAt);
    const expected = defaultspackContractUrl(defaultspackContractRoute(apiPath + suffix), method);
    if (url.pathname !== expected) return null;
    return new URLSearchParams(suffix);
  } catch { return null; }
}
