"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  createReferenceListSession,
  readReferenceListSession,
  referenceSessionStorageKey,
  type ReferenceListSession,
} from "@/lib/reference-list-state";

type Router = {
  replace: (href: string, options: { scroll: boolean }) => void;
};

type ReferenceListState<T> = {
  total: number;
  loadedItems: T[];
  loadedOffsets: number[];
  requestedOffsets?: number[];
  loadedPages: Array<{ offset: number; items: T[] }>;
  visibleOffsets: number[];
};

export function useReferenceListState<T extends { id: string }>(initialState: ReferenceListState<T>) {
  const [listState, setListState] = useState<ReferenceListState<T>>(initialState);
  const [selection, setSelected] = useState<Set<string>>(new Set());
  const selected = useMemo(() => new Set(
    listState.loadedItems.filter((item) => selection.has(item.id)).map((item) => item.id),
  ), [listState.loadedItems, selection]);
  const onStateChange = useCallback((next: ReferenceListState<T>) => {
    setListState(next);
    // Partial or failed restoration is not evidence that a saved ID vanished.
    // Only confirmed batches can discard the remaining pending selection.
    if (!next.loadedOffsets.length || !next.requestedOffsets?.every((offset) => next.loadedOffsets.includes(offset))) return;
    const loadedIds = new Set(next.loadedItems.map((item) => item.id));
    setSelected((current) => {
      const pruned = new Set([...current].filter((id) => loadedIds.has(id)));
      return pruned.size === current.size ? current : pruned;
    });
  }, []);
  return { listState, setListState, selected, selection, setSelected, onStateChange };
}

type LegacyNavigationOptions = {
  legacyPage: string | null;
  legacyInitialIndex: number;
  total: number;
  listRef: { current: { scrollToIndex: (index: number) => void } | null };
  initialVirtualIndexRef: { current: number };
  pathname: string;
  paramsString: string;
  router: Router;
};

export function useReferenceListLegacyNavigation({
  legacyPage,
  legacyInitialIndex,
  total,
  listRef,
  initialVirtualIndexRef,
  pathname,
  paramsString,
  router,
}: LegacyNavigationOptions) {
  const pendingLegacyScrollRef = useRef<number | null>(legacyPage === null ? null : legacyInitialIndex);

  useEffect(() => {
    if (legacyPage === null || !total) return;
    let cleanupFrame = 0;
    const scrollFrame = window.requestAnimationFrame(() => {
      listRef.current?.scrollToIndex(legacyInitialIndex);
      cleanupFrame = window.requestAnimationFrame(() => {
        initialVirtualIndexRef.current = 0;
        const params = new URLSearchParams(paramsString);
        params.delete("p");
        const suffix = params.toString();
        router.replace(suffix ? `${pathname}?${suffix}` : pathname, { scroll: false });
      });
    });
    return () => {
      window.cancelAnimationFrame(scrollFrame);
      if (cleanupFrame) window.cancelAnimationFrame(cleanupFrame);
    };
  }, [legacyInitialIndex, legacyPage, listRef, pathname, paramsString, router, total]);

  useEffect(() => {
    if (legacyPage !== null || !total || pendingLegacyScrollRef.current === null) return;
    const index = pendingLegacyScrollRef.current;
    let settleFrame = 0;
    const scrollFrame = window.requestAnimationFrame(() => {
      settleFrame = window.requestAnimationFrame(() => {
        listRef.current?.scrollToIndex(index);
        pendingLegacyScrollRef.current = null;
      });
    });
    return () => {
      window.cancelAnimationFrame(scrollFrame);
      if (settleFrame) window.cancelAnimationFrame(settleFrame);
    };
  }, [legacyPage, listRef, total]);
}

type SessionOptions = {
  userId: string | number;
  pathname: string;
  queryFingerprint: string;
  legacyPage: string | null;
  total: number;
  loadedOffsets: readonly number[];
  selected: Set<string>;
  setSelected: (value: Set<string>) => void;
  onQueryChange: () => void;
};

export function useReferenceListSession({
  userId,
  pathname,
  queryFingerprint,
  legacyPage,
  total,
  loadedOffsets,
  selected,
  setSelected,
  onQueryChange,
}: SessionOptions) {
  const sessionKey = referenceSessionStorageKey(userId, pathname, queryFingerprint);
  const [storedSession, setStoredSession] = useState<{
    key: string;
    value: ReferenceListSession | null;
  } | null>(null);
  const restoredSession = storedSession?.key === sessionKey ? storedSession.value : null;
  const sessionReady = storedSession?.key === sessionKey;
  const previousQueryFingerprintRef = useRef(queryFingerprint);
  const onQueryChangeRef = useRef(onQueryChange);

  useEffect(() => {
    onQueryChangeRef.current = onQueryChange;
  }, [onQueryChange]);

  useLayoutEffect(() => {
    const queryChanged = previousQueryFingerprintRef.current !== queryFingerprint;
    previousQueryFingerprintRef.current = queryFingerprint;
    const saved = queryChanged
      ? null
      : readReferenceListSession(window.sessionStorage.getItem(sessionKey), queryFingerprint);
    setStoredSession({ key: sessionKey, value: saved });
    setSelected(new Set(saved?.selectedIds || []));
    if (queryChanged) {
      onQueryChangeRef.current();
      window.scrollTo({ top: 0 });
    }
  }, [queryFingerprint, sessionKey, setSelected]);

  const persistListSession = useCallback(() => {
    if (!sessionReady) return;
    window.sessionStorage.setItem(sessionKey, JSON.stringify(createReferenceListSession({
      queryFingerprint,
      scrollY: window.scrollY,
      loadedOffsets,
      selectedIds: [...selected],
    })));
  }, [loadedOffsets, queryFingerprint, selected, sessionKey, sessionReady]);

  useEffect(() => {
    window.addEventListener("pagehide", persistListSession);
    return () => window.removeEventListener("pagehide", persistListSession);
  }, [persistListSession]);

  const restoredScrollKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (!sessionReady || !total || legacyPage !== null || restoredScrollKeyRef.current === sessionKey) return;
    restoredScrollKeyRef.current = sessionKey;
    const frame = window.requestAnimationFrame(() => window.scrollTo({ top: restoredSession?.scrollY || 0 }));
    return () => window.cancelAnimationFrame(frame);
  }, [legacyPage, restoredSession?.scrollY, sessionKey, sessionReady, total]);

  return { sessionKey, restoredSession, sessionReady, persistListSession };
}

type NavigationOptions = {
  router: Router;
  pathname: string;
  paramsString: string;
  search: string;
  setSelected: (value: Set<string>) => void;
};

export function useReferenceListNavigation({
  router,
  pathname,
  paramsString,
  search,
  setSelected,
}: NavigationOptions) {
  const [inputVal, setInputVal] = useState(search);
  const navigationParamsRef = useRef(paramsString);
  const committedParamsRef = useRef(paramsString);
  const pendingParamsRef = useRef(new Set<string>());

  useEffect(() => {
    if (committedParamsRef.current === paramsString) return;
    committedParamsRef.current = paramsString;
    if (pendingParamsRef.current.delete(paramsString)) return;
    pendingParamsRef.current.clear();
    navigationParamsRef.current = paramsString;
    setInputVal(search);
  }, [paramsString, search]);

  useEffect(() => {
    const restoreHistoryQuery = () => {
      const params = new URLSearchParams(window.location.search);
      pendingParamsRef.current.clear();
      navigationParamsRef.current = params.toString();
      setInputVal(params.get("q") ?? "");
    };
    window.addEventListener("popstate", restoreHistoryQuery);
    return () => window.removeEventListener("popstate", restoreHistoryQuery);
  }, []);

  const updateParams = useCallback((updates: Record<string, string | null>, resetPage = true) => {
    const params = new URLSearchParams(navigationParamsRef.current);
    for (const [key, value] of Object.entries(updates)) {
      if (value === null || value === "") params.delete(key); else params.set(key, value);
    }
    if (resetPage) params.delete("p");
    const next = params.toString();
    navigationParamsRef.current = next;
    pendingParamsRef.current.add(next);
    router.replace(`${pathname}?${next}`, { scroll: false });
  }, [pathname, router]);

  useEffect(() => {
    if (inputVal === search && (new URLSearchParams(navigationParamsRef.current).get("q") ?? "") === search) return;
    const timer = setTimeout(() => {
      setSelected(new Set());
      window.scrollTo({ top: 0 });
      updateParams({ q: inputVal || null });
    }, 300);
    return () => clearTimeout(timer);
  }, [inputVal, search, setSelected, updateParams]);

  return { inputVal, setInputVal, updateParams };
}
