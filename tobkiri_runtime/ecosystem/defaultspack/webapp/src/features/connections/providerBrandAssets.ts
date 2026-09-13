const BRAND_SVGS = {
  cloudflare: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><rect width="48" height="48" rx="11" fill="#fff7ed"/><path fill="#f38020" d="M29.8 32.4H8.9a5.9 5.9 0 0 1 .7-11.8 8.9 8.9 0 0 1 16.8-2.4 7.2 7.2 0 0 1 3.4 14.2Z"/><path fill="#faae40" d="M39.1 32.4H27.7a5.2 5.2 0 0 1 9.9-2.2 4.1 4.1 0 0 1 1.5 2.2Z"/></svg>`,
  google: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><rect width="48" height="48" rx="11" fill="#fff"/><path fill="#4285f4" d="M42.2 24.5c0-1.3-.1-2.6-.3-3.8H24v7.4h10.2a8.8 8.8 0 0 1-3.8 5.7v4.8h6.2c3.6-3.4 5.6-8.3 5.6-14.1Z"/><path fill="#34a853" d="M24 43c5.1 0 9.5-1.7 12.6-4.5l-6.2-4.8c-1.7 1.2-3.9 1.9-6.4 1.9-5 0-9.2-3.4-10.7-7.9H6.9v5c3.2 6.1 9.6 10.3 17.1 10.3Z"/><path fill="#fbbc05" d="M13.3 27.7a11.5 11.5 0 0 1 0-7.4v-5H6.9a19 19 0 0 0 0 17.4l6.4-5Z"/><path fill="#ea4335" d="M24 12.4c2.8 0 5.3 1 7.3 2.8l5.5-5.4A18.5 18.5 0 0 0 6.9 15.3l6.4 5c1.5-4.5 5.7-7.9 10.7-7.9Z"/></svg>`,
  github: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><rect width="48" height="48" rx="11" fill="#f4f4f5"/><path fill="#18181b" d="M24 7.2a17 17 0 0 0-5.4 33.1c.9.2 1.2-.4 1.2-.8v-3.3c-5 .9-6.1-2.1-6.1-2.1-.8-2.1-2-2.7-2-2.7-1.7-1.1.1-1.1.1-1.1 1.8.1 2.8 1.8 2.8 1.8 1.6 2.8 4.3 2 5.3 1.5.2-1.2.6-2 1.2-2.5-4-.5-8.2-2-8.2-8.4 0-1.9.7-3.4 1.8-4.6-.2-.5-.8-2.3.2-4.7 0 0 1.5-.5 4.8 1.8a16.7 16.7 0 0 1 8.8 0c3.3-2.3 4.8-1.8 4.8-1.8 1 2.4.4 4.2.2 4.7a6.6 6.6 0 0 1 1.8 4.6c0 6.5-4.2 7.9-8.2 8.4.7.6 1.2 1.7 1.2 3.4v5c0 .5.3 1 1.2.8A17 17 0 0 0 24 7.2Z"/></svg>`,
  codex: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><rect width="48" height="48" rx="11" fill="#111827"/><g fill="none" stroke="#f9fafb" stroke-width="2.6" stroke-linecap="round"><path d="M24 11.2a7.6 7.6 0 0 1 13.1 4.4v6.2"/><path d="M36.9 18.6a7.6 7.6 0 0 1 0 15.1l-5.4-3.1"/><path d="M34.7 32.5a7.6 7.6 0 0 1-13.1 4.4v-6.2"/><path d="M24 36.8a7.6 7.6 0 0 1-13.1-4.4v-6.2"/><path d="M11.1 29.4a7.6 7.6 0 0 1 0-15.1l5.4 3.1"/><path d="M13.3 15.5a7.6 7.6 0 0 1 13.1-4.4v6.2"/></g><circle cx="24" cy="24" r="4.2" fill="#10b981"/></svg>`,
} as const;

export type ProviderBrandId = keyof typeof BRAND_SVGS;

export function providerBrandAsset(providerId: string): string | null {
  const svg = BRAND_SVGS[providerId as ProviderBrandId];
  return svg ? `data:image/svg+xml,${encodeURIComponent(svg)}` : null;
}
