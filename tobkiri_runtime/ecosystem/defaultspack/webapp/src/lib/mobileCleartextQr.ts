export const RUMI_MOBILE_ALLOW_CLEARTEXT_QR_ENV = "VITE_RUMI_MOBILE_ALLOW_CLEARTEXT_QR";

function viteEnv(): Record<string, string | undefined> {
  return ((import.meta as ImportMeta & { env?: Record<string, string | undefined> }).env) ?? {};
}

export function allowCleartextMobileQr(env: Record<string, string | undefined> = viteEnv()): boolean {
  return env[RUMI_MOBILE_ALLOW_CLEARTEXT_QR_ENV] === "1";
}
