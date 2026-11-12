import type { QueryClient } from "@tanstack/react-query";
import { queryKeys } from "@/lib/api";

// Visibility changes affect membership and history, not the asset/source payloads.
export function refreshWorkVisibility(qc: QueryClient, ids: readonly string[]) {
  return Promise.all([
    qc.invalidateQueries({ queryKey: queryKeys.works.all, predicate: ({ queryKey }) =>
      queryKey[1] === "compound-search" || queryKey[1] === "trash-preview"
      || (queryKey.length === 2 && ids.includes(queryKey[1] as string)) }),
    qc.invalidateQueries({ queryKey: queryKeys.curation.all }),
  ]);
}
