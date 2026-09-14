"use client";

import { useMemo, useRef } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useT } from "@/lib/i18n";

const PAGE_SIZE = 50;
const QUERY_KEY = ["creators", "paginated-native-selector", PAGE_SIZE] as const;

export default function PaginatedCreatorSelect({
  id,
  label,
  ariaLabel,
  value,
  onChange,
  placeholder,
  disabled = false,
  selectClassName = "select w-full",
}: {
  id: string;
  label?: string;
  ariaLabel?: string;
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  disabled?: boolean;
  selectClassName?: string;
}) {
  const t = useT();
  const nextPageLock = useRef(false);
  const creators = useInfiniteQuery({
    queryKey: QUERY_KEY,
    queryFn: ({ pageParam }) => api.listCreators(pageParam, PAGE_SIZE),
    initialPageParam: 0,
    getNextPageParam: (lastPage, pages) => {
      const loaded = pages.reduce((count, page) => count + page.items.length, 0);
      return lastPage.items.length > 0 && loaded < lastPage.total ? loaded : undefined;
    },
    retry: false,
  });
  const options = useMemo(() => {
    const byId = new Map<string, { id: string; name: string; display_name?: string | null }>();
    for (const page of creators.data?.pages || []) {
      for (const creator of page.items) byId.set(creator.id, creator);
    }
    return [...byId.values()];
  }, [creators.data?.pages]);
  const initialLoading = creators.isFetching && options.length === 0;
  const initialError = creators.isError && !creators.isFetching && options.length === 0;
  const pageError = creators.isFetchNextPageError && !creators.isFetchingNextPage && options.length > 0;

  const loadNextPage = async () => {
    if (nextPageLock.current || creators.isFetchingNextPage || !creators.hasNextPage) return;
    nextPageLock.current = true;
    try { await creators.fetchNextPage({ cancelRefetch: false }); }
    finally { nextPageLock.current = false; }
  };

  return (
    <div className="space-y-2">
      {label && <label htmlFor={id} className="block text-sm font-medium">{label}</label>}
      <select
        id={id}
        aria-label={ariaLabel}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled || initialLoading || initialError}
        className={selectClassName}
      >
        <option value="">{placeholder}</option>
        {options.map((creator) => (
          <option key={creator.id} value={creator.id}>{creator.display_name || creator.name}</option>
        ))}
      </select>
      {initialLoading && <p role="status" className="text-xs text-muted">{t("creator_picker.loading")}</p>}
      {initialError && (
        <div role="alert" className="flex items-center gap-2 text-xs text-danger">
          <span>{t("creator_picker.load_error")}</span>
          <button type="button" className="btn-ghost" onClick={() => void creators.refetch()}>{t("common.retry")}</button>
        </div>
      )}
      {pageError ? (
        <div role="alert" className="flex items-center gap-2 text-xs text-danger">
          <span>{t("creator_picker.load_more_error")}</span>
          <button type="button" className="btn-ghost" onClick={loadNextPage}>{t("common.retry")}</button>
        </div>
      ) : creators.hasNextPage ? (
        <button type="button" className="btn-ghost" disabled={creators.isFetchingNextPage} onClick={loadNextPage}>
          {creators.isFetchingNextPage ? t("creator_picker.loading_more") : t("common.load_more")}
        </button>
      ) : null}
      {!initialLoading && !initialError && options.length === 0 && (
        <p role="status" className="text-xs text-muted">{t("creator_picker.empty")}</p>
      )}
    </div>
  );
}
