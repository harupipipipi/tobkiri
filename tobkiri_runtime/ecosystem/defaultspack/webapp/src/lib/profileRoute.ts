export type ProfileScreenRoute = {
  profileId: string;
  applicationRoute: string | null;
};

function validProfileId(value: string): boolean {
  return value.length > 0
    && new TextEncoder().encode(value).length <= 128
    && value.trim() === value
    && !value.includes("/")
    && !value.includes("\\")
    && !value.includes("..")
    && !Array.from(value).some((character) => /[\u0000-\u001f\u007f]/.test(character));
}

function encodeProfileId(value: string): string {
  return encodeURIComponent(value).replace(
    /[!'()*]/g,
    (character) => `%${character.charCodeAt(0).toString(16).toUpperCase()}`,
  );
}

function validApplicationRoute(value: string): boolean {
  return value === "/" || /^\/(?:[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_-]+)*)$/.test(value);
}

/** Parse one identity-bearing screen URL without treating it as authority. */
export function parseProfileScreenPath(pathname: string): ProfileScreenRoute | null {
  if (!pathname.startsWith("/p/")) return null;
  const remainder = pathname.slice(3);
  const separator = remainder.indexOf("/");
  const encodedProfile = separator < 0 ? remainder : remainder.slice(0, separator);
  let profileId: string;
  try {
    profileId = decodeURIComponent(encodedProfile);
  } catch {
    return null;
  }
  if (!validProfileId(profileId) || encodeProfileId(profileId) !== encodedProfile) {
    return null;
  }
  if (separator < 0) return { profileId, applicationRoute: null };
  const applicationRoute = remainder.slice(separator);
  return validApplicationRoute(applicationRoute)
    ? { profileId, applicationRoute }
    : null;
}

/** Qualify a verified Application route with its persistent Runtime Profile ID. */
export function profileScreenPath(profileId: string, applicationRoute: string): string {
  if (!validProfileId(profileId) || !validApplicationRoute(applicationRoute)) {
    throw new Error("profile_screen_route_invalid");
  }
  return `/p/${encodeProfileId(profileId)}${applicationRoute}`;
}

/** Preserve the current URL's captured Profile when navigating inside its Application. */
export function profileScreenPathFromLocation(
  applicationRoute: string,
  pathname = window.location.pathname,
): string {
  const current = parseProfileScreenPath(pathname);
  if (!current) throw new Error("profile_screen_identity_unavailable");
  return profileScreenPath(current.profileId, applicationRoute);
}

export function profileScreenUrlFromLocation(
  applicationUrl: string,
  href = window.location.href,
): string {
  const currentUrl = new URL(href);
  const current = parseProfileScreenPath(currentUrl.pathname);
  const target = new URL(applicationUrl, currentUrl.origin);
  if (!current || target.origin !== currentUrl.origin || !validApplicationRoute(target.pathname)) {
    throw new Error("profile_screen_identity_unavailable");
  }
  target.pathname = profileScreenPath(current.profileId, target.pathname);
  return `${target.pathname}${target.search}${target.hash}`;
}

export function applicationPathname(pathname: string): string | null {
  return parseProfileScreenPath(pathname)?.applicationRoute ?? null;
}
