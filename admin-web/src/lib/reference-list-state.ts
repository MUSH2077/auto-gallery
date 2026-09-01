export const REFERENCE_BATCH_SIZE = 50;
export const REFERENCE_OVERSCAN = 8;
export const REFERENCE_PREFETCH_ROWS = 10;

export type ReferenceSortField = "name" | "created" | "updated";
export type ReferenceSortDirection = "asc" | "desc";

export interface ReferenceSort {
  field: ReferenceSortField;
  direction: ReferenceSortDirection;
}

interface ReferenceToken {
  kind: string;
  key?: string;
  value?: string;
  negated?: boolean;
}

export interface ReferenceListSession {
  version: 1;
  queryFingerprint: string;
  scrollY: number;
  loadedOffsets: number[];
  selectedIds: string[];
}

const SORT_VALUES = new Set([
  "name-asc",
  "name-desc",
  "created-asc",
  "created-desc",
  "updated-asc",
  "updated-desc",
]);

function sortValueFromTokens(tokens: readonly ReferenceToken[]): string | null {
  const token = tokens.find(
    (item) =>
      item.kind === "qualifier"
      && item.key === "sort"
      && !item.negated
      && typeof item.value === "string"
      && SORT_VALUES.has(item.value),
  );
  return token?.value || null;
}

export function referenceSortFromTokens(
  tokens: readonly ReferenceToken[],
): ReferenceSort {
  const value = sortValueFromTokens(tokens) || "name-asc";
  const [field, direction] = value.split("-") as [
    ReferenceSortField,
    ReferenceSortDirection,
  ];
  return { field, direction };
}

export function nextReferenceSortValue(
  current: ReferenceSort,
  field: ReferenceSortField,
): string {
  if (current.field === field) {
    return `${field}-${current.direction === "asc" ? "desc" : "asc"}`;
  }
  return `${field}-${field === "name" ? "asc" : "desc"}`;
}

export function referenceNameAnchorsAvailable(
  tokens: readonly ReferenceToken[],
): boolean {
  if (tokens.some((token) => token.kind === "text")) return false;
  const sortValue = sortValueFromTokens(tokens);
  return sortValue === null || sortValue.startsWith("name-");
}

export function legacyPageInitialIndex(value: string | null): number {
  if (!value) return 0;
  const page = Number(value);
  if (!Number.isInteger(page) || page < 0) return 0;
  return page * 25;
}

export function referenceBatchOffset(index: number): number {
  const safeIndex = Number.isFinite(index) ? Math.max(0, Math.floor(index)) : 0;
  return Math.floor(safeIndex / REFERENCE_BATCH_SIZE) * REFERENCE_BATCH_SIZE;
}

export function referenceSessionStorageKey(
  userId: string | number,
  pathname: string,
  queryFingerprint: string,
): string {
  return [
    "reference-list:v1",
    String(userId),
    encodeURIComponent(pathname),
    encodeURIComponent(queryFingerprint),
  ].join(":");
}

export function createReferenceListSession(input: {
  queryFingerprint: string;
  scrollY: number;
  loadedOffsets: readonly number[];
  selectedIds: readonly string[];
}): ReferenceListSession {
  const loadedOffsets = [...new Set(input.loadedOffsets)]
    .filter(
      (offset) =>
        Number.isInteger(offset)
        && offset >= 0
        && offset % REFERENCE_BATCH_SIZE === 0,
    )
    .sort((left, right) => left - right)
    .slice(0, 32);
  const selectedIds = [...new Set(input.selectedIds)]
    .filter(Boolean)
    .sort()
    .slice(0, 2_000);
  return {
    version: 1,
    queryFingerprint: input.queryFingerprint,
    scrollY: Number.isFinite(input.scrollY)
      ? Math.max(0, Math.round(input.scrollY))
      : 0,
    loadedOffsets,
    selectedIds,
  };
}

export function readReferenceListSession(
  raw: string | null,
  queryFingerprint: string,
): ReferenceListSession | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<ReferenceListSession>;
    if (
      value.version !== 1
      || value.queryFingerprint !== queryFingerprint
      || !Array.isArray(value.loadedOffsets)
      || !Array.isArray(value.selectedIds)
      || typeof value.scrollY !== "number"
    ) {
      return null;
    }
    return createReferenceListSession({
      queryFingerprint,
      scrollY: value.scrollY,
      loadedOffsets: value.loadedOffsets,
      selectedIds: value.selectedIds,
    });
  } catch {
    return null;
  }
}
