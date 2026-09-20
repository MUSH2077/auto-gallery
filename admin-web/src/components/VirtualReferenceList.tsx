"use client";

import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
  type RefAttributes,
} from "react";
import {
  useQueries,
  type QueryKey,
  type UseQueryResult,
} from "@tanstack/react-query";
import { useWindowVirtualizer } from "@tanstack/react-virtual";
import {
  REFERENCE_BATCH_SIZE,
  REFERENCE_OVERSCAN,
  REFERENCE_PREFETCH_ROWS,
  referenceBatchOffset,
} from "@/lib/reference-list-state";

export interface VirtualReferencePage<TItem, TMeta = unknown> {
  items: TItem[];
  total: number;
  meta?: TMeta;
}

export interface VirtualReferenceListState<TItem> {
  total: number;
  loadedItems: TItem[];
  loadedOffsets: number[];
  loadedPages: Array<{ offset: number; items: TItem[] }>;
  visibleOffsets: number[];
}

export interface VirtualReferenceListHandle {
  scrollToIndex: (index: number) => void;
  scrollToTop: () => void;
}

interface VirtualReferenceListProps<
  TItem extends { id: string },
  TMeta = unknown,
> {
  queryKey: QueryKey;
  loadPage: (
    offset: number,
    limit: number,
    signal?: AbortSignal,
  ) => Promise<VirtualReferencePage<TItem, TMeta>>;
  label: string;
  renderItem: (item: TItem, index: number, total: number) => ReactNode;
  renderInitialLoading: () => ReactNode;
  renderInitialError: (error: Error, retry: () => void) => ReactNode;
  renderPageError: (
    offset: number,
    error: Error,
    retry: () => void,
  ) => ReactNode;
  renderPlaceholder: (index: number) => ReactNode;
  renderEmpty: () => ReactNode;
  initialIndex?: number;
  initialOffsets?: readonly number[];
  estimateSize?: number;
  onStateChange?: (state: VirtualReferenceListState<TItem>) => void;
  onMetaChange?: (meta: TMeta) => void;
}

function alignedInitialOffsets(
  initialIndex: number,
  initialOffsets: readonly number[],
): number[] {
  const offsets = new Set<number>([
    referenceBatchOffset(initialIndex),
    ...initialOffsets.map(referenceBatchOffset),
  ]);
  return [...offsets].sort((left, right) => left - right);
}

function VirtualReferenceListInner<
  TItem extends { id: string },
  TMeta = unknown,
>(
  {
    queryKey,
    loadPage,
    label,
    renderItem,
    renderInitialLoading,
    renderInitialError,
    renderPageError,
    renderPlaceholder,
    renderEmpty,
    initialIndex = 0,
    initialOffsets = [],
    estimateSize = 112,
    onStateChange,
    onMetaChange,
  }: VirtualReferenceListProps<TItem, TMeta>,
  ref: React.ForwardedRef<VirtualReferenceListHandle>,
) {
  const listRef = useRef<HTMLDivElement>(null);
  const initialScrollDoneRef = useRef(false);
  const stateCallbackRef = useRef(onStateChange);
  const metaCallbackRef = useRef(onMetaChange);
  const stateSignatureRef = useRef("");
  stateCallbackRef.current = onStateChange;
  metaCallbackRef.current = onMetaChange;

  const [requestedOffsets, setRequestedOffsets] = useState<number[]>(() =>
    alignedInitialOffsets(initialIndex, initialOffsets),
  );
  const [pendingScrollIndex, setPendingScrollIndex] = useState<number | null>(null);
  const [scrollMargin, setScrollMargin] = useState(0);

  const pageQueries = useQueries({
    queries: requestedOffsets.map((offset) => ({
      queryKey: [...queryKey, "offset", offset, REFERENCE_BATCH_SIZE],
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        loadPage(offset, REFERENCE_BATCH_SIZE, signal),
      staleTime: 15_000,
      retry: false,
    })),
  }) as UseQueryResult<VirtualReferencePage<TItem, TMeta>, Error>[];

  const queryByOffset = useMemo(() => {
    const result = new Map<
      number,
      UseQueryResult<VirtualReferencePage<TItem, TMeta>, Error>
    >();
    requestedOffsets.forEach((offset, index) => {
      result.set(offset, pageQueries[index]);
    });
    return result;
  }, [pageQueries, requestedOffsets]);

  const successfulPages = useMemo(
    () => requestedOffsets.flatMap((offset) => {
      const data = queryByOffset.get(offset)?.data;
      return data ? [{ offset, data }] : [];
    }),
    [queryByOffset, requestedOffsets],
  );
  const total = successfulPages[0]?.data.total ?? 0;

  const itemAt = (index: number): TItem | undefined => {
    const offset = referenceBatchOffset(index);
    return queryByOffset.get(offset)?.data?.items[index - offset];
  };

  const virtualizer = useWindowVirtualizer({
    count: total,
    estimateSize: () => estimateSize,
    overscan: REFERENCE_OVERSCAN,
    scrollMargin,
    useFlushSync: false,
    useAnimationFrameWithResizeObserver: true,
    getItemKey: (index) => itemAt(index)?.id || index,
  });
  const virtualItems = virtualizer.getVirtualItems();

  useLayoutEffect(() => {
    const element = listRef.current;
    if (!element) return;
    const next = Math.round(element.getBoundingClientRect().top + window.scrollY);
    setScrollMargin((current) => current === next ? current : next);
  }, [total]);

  useEffect(() => {
    const updateMargin = () => {
      const element = listRef.current;
      if (!element) return;
      const next = Math.round(
        element.getBoundingClientRect().top + window.scrollY,
      );
      setScrollMargin((current) => current === next ? current : next);
    };
    window.addEventListener("resize", updateMargin);
    return () => window.removeEventListener("resize", updateMargin);
  }, []);

  useEffect(() => {
    const meta = successfulPages[0]?.data.meta;
    if (meta !== undefined) metaCallbackRef.current?.(meta);
  }, [successfulPages]);

  useEffect(() => {
    if (!total || virtualItems.length === 0) return;
    const first = virtualItems[0].index;
    const last = virtualItems[virtualItems.length - 1].index;
    const desired = new Set(requestedOffsets);
    const firstOffset = referenceBatchOffset(first);
    const lastOffset = referenceBatchOffset(last);
    for (
      let offset = firstOffset;
      offset <= lastOffset;
      offset += REFERENCE_BATCH_SIZE
    ) {
      desired.add(offset);
    }
    if (
      first - firstOffset <= REFERENCE_PREFETCH_ROWS
      && firstOffset >= REFERENCE_BATCH_SIZE
    ) {
      desired.add(firstOffset - REFERENCE_BATCH_SIZE);
    }
    const lastPageEnd = lastOffset + REFERENCE_BATCH_SIZE - 1;
    if (
      lastPageEnd - last <= REFERENCE_PREFETCH_ROWS
      && lastOffset + REFERENCE_BATCH_SIZE < total
    ) {
      desired.add(lastOffset + REFERENCE_BATCH_SIZE);
    }
    if (desired.size !== requestedOffsets.length) {
      setRequestedOffsets([...desired].sort((left, right) => left - right));
    }
  }, [requestedOffsets, total, virtualItems]);

  const loadedItems = useMemo(() => {
    const byId = new Map<string, TItem>();
    for (const { data } of successfulPages) {
      for (const item of data.items) byId.set(item.id, item);
    }
    return [...byId.values()];
  }, [successfulPages]);
  const loadedOffsets = useMemo(
    () => successfulPages.map((page) => page.offset),
    [successfulPages],
  );
  const loadedPages = useMemo(
    () => successfulPages.map((page) => ({
      offset: page.offset,
      items: page.data.items,
    })),
    [successfulPages],
  );
  const visibleOffsets = useMemo(
    () => [...new Set(virtualItems.map((item) => referenceBatchOffset(item.index)))],
    [virtualItems],
  );
  const callbackState = useMemo(
    () => ({ total, loadedItems, loadedOffsets, loadedPages, visibleOffsets }),
    [loadedItems, loadedOffsets, loadedPages, total, visibleOffsets],
  );
  const stateSignature = [
    total,
    loadedOffsets.join(","),
    loadedItems.map((item) => item.id).join(","),
    visibleOffsets.join(","),
  ].join("|");

  useEffect(() => {
    if (stateSignatureRef.current === stateSignature) return;
    stateSignatureRef.current = stateSignature;
    stateCallbackRef.current?.(callbackState);
  }, [callbackState, stateSignature]);

  const requestAndScroll = (rawIndex: number) => {
    if (!total) return;
    const index = Math.min(total - 1, Math.max(0, Math.floor(rawIndex)));
    const offset = referenceBatchOffset(index);
    setPendingScrollIndex(index);
    setRequestedOffsets((current) => current.includes(offset)
      ? current
      : [...current, offset].sort((left, right) => left - right));
    virtualizer.scrollToIndex(index, { align: "start", behavior: "auto" });
  };

  useEffect(() => {
    if (pendingScrollIndex === null) return;
    const offset = referenceBatchOffset(pendingScrollIndex);
    if (!queryByOffset.get(offset)?.data) return;
    let settleFrame = 0;
    const measureFrame = window.requestAnimationFrame(() => {
      virtualizer.measure();
      virtualizer.scrollToIndex(pendingScrollIndex, { align: "start", behavior: "auto" });
      settleFrame = window.requestAnimationFrame(() => {
        virtualizer.scrollToIndex(pendingScrollIndex, { align: "start", behavior: "auto" });
        setPendingScrollIndex(null);
      });
    });
    return () => {
      window.cancelAnimationFrame(measureFrame);
      if (settleFrame) window.cancelAnimationFrame(settleFrame);
    };
  }, [pendingScrollIndex, queryByOffset, virtualizer]);

  useImperativeHandle(ref, () => ({
    scrollToIndex: requestAndScroll,
    scrollToTop: () => requestAndScroll(0),
  }));

  useEffect(() => {
    if (!total || initialScrollDoneRef.current || initialIndex <= 0) return;
    initialScrollDoneRef.current = true;
    const frame = window.requestAnimationFrame(() => requestAndScroll(initialIndex));
    return () => window.cancelAnimationFrame(frame);
  }, [initialIndex, total]); // eslint-disable-line react-hooks/exhaustive-deps

  const initialOffset = referenceBatchOffset(initialIndex);
  const initialQuery = queryByOffset.get(initialOffset) || pageQueries[0];
  if (!successfulPages.length) {
    if (initialQuery?.isError) {
      return <>{renderInitialError(initialQuery.error, () => {
        void initialQuery.refetch();
      })}</>;
    }
    return <>{renderInitialLoading()}</>;
  }
  if (total === 0) return <>{renderEmpty()}</>;

  const firstVisibleInPage = new Map<number, number>();
  for (const item of virtualItems) {
    const offset = referenceBatchOffset(item.index);
    if (!firstVisibleInPage.has(offset)) firstVisibleInPage.set(offset, item.index);
  }

  return (
    <div
      ref={listRef}
      role="list"
      aria-label={label}
      data-virtual-reference-list
      className="entity-list relative"
      style={{ height: virtualizer.getTotalSize() }}
    >
      {virtualItems.map((virtualItem) => {
        const offset = referenceBatchOffset(virtualItem.index);
        const pageQuery = queryByOffset.get(offset);
        const item = pageQuery?.data?.items[virtualItem.index - offset];
        const showPageError = pageQuery?.isError
          && firstVisibleInPage.get(offset) === virtualItem.index;
        return (
          <div
            key={virtualItem.key}
            ref={virtualizer.measureElement}
            data-index={virtualItem.index}
            data-virtual-index={virtualItem.index}
            className="virtual-reference-row absolute left-0 top-0 w-full"
            style={{
              transform: `translateY(${virtualItem.start - scrollMargin}px)`,
            }}
          >
            {showPageError && pageQuery?.error
              ? renderPageError(offset, pageQuery.error, () => {
                  void pageQuery.refetch();
                })
              : item
                ? renderItem(item, virtualItem.index, total)
                : renderPlaceholder(virtualItem.index)}
          </div>
        );
      })}
    </div>
  );
}

const VirtualReferenceList = forwardRef(VirtualReferenceListInner) as <
  TItem extends { id: string },
  TMeta = unknown,
>(
  props: VirtualReferenceListProps<TItem, TMeta>
    & RefAttributes<VirtualReferenceListHandle>,
) => ReactElement;

export default VirtualReferenceList;
