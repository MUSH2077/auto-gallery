/** Serialize a value for the canonical backend search language.
 *
 * This is intentionally only a serializer for links and fixed contextual
 * tokens. Parsing, validation and normalization remain server-owned.
 */
export function quoteSearchValue(value: string): string {
  return `"${value.replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`;
}

export function searchUrl(pathname: string, query: string, extra?: Record<string, string>): string {
  const params = new URLSearchParams({ q: query, ...extra });
  return `${pathname}?${params.toString()}`;
}
