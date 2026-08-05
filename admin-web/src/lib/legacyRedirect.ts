export type LegacySearchParams = Record<string, string | string[] | undefined>;

/**
 * Preserve legacy deep-link state while allowing the canonical route to lock
 * required query values (for example, the pending dedup status).
 */
export function canonicalRedirectUrl(
  pathname: string,
  searchParams: LegacySearchParams = {},
  overrides: Record<string, string | null> = {},
): string {
  const query = new URLSearchParams();
  for (const [key, rawValue] of Object.entries(searchParams)) {
    if (Array.isArray(rawValue)) {
      for (const value of rawValue) query.append(key, value);
    } else if (rawValue !== undefined) {
      query.set(key, rawValue);
    }
  }
  for (const [key, value] of Object.entries(overrides)) {
    if (value === null) query.delete(key);
    else query.set(key, value);
  }
  const suffix = query.toString();
  return suffix ? `${pathname}?${suffix}` : pathname;
}
