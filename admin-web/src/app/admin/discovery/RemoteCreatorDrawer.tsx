"use client";

import Link from "next/link";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDownToLine,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Eye,
  Images,
  LoaderCircle,
  X,
} from "lucide-react";

import { ErrorState, Modal, StatusBadge, useToast } from "@/components";
import {
  ApiError,
  api,
  queryKeys,
  type DiscoveryCandidate,
  type RemoteCreatorProfile,
  type RemoteWorkImportResult,
  type RemoteWorkPage,
  type RemoteWorkPreview,
} from "@/lib/api";
import { adminRoutes } from "@/lib/adminRoutes";
import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";
import { runPrivateDiscoveryRequest } from "@/lib/remoteDiscoveryPrivateCache";

type CreatorPage = { profile?: RemoteCreatorProfile; works: RemoteWorkPage };

function importErrorMessage(error: unknown, t: ReturnType<typeof useT>) {
  if (error instanceof ApiError) {
    if (error.code === "source_busy") return t("discovery.remote_source_busy");
    if (error.code === "sensitive_content_confirmation_required") {
      return t("discovery.reveal_before_import");
    }
    if (error.code === "candidate_dismissed") return t("discovery.restore_before_import");
    if (error.code === "candidate_conflict") return t("discovery.resolve_before_import");
  }
  return t("discovery.remote_work_import_failed");
}

function WorkLightbox({
  work,
  onClose,
}: {
  work: RemoteWorkPreview | null;
  onClose: () => void;
}) {
  const t = useT();
  const [page, setPage] = useState(0);
  useEffect(() => setPage(0), [work?.source_work_id]);
  if (!work) return null;
  const images = work.preview_urls.length
    ? work.preview_urls
    : work.thumbnail_url
      ? [work.thumbnail_url]
      : [];
  return (
    <Modal open={!!work} onClose={onClose} title={work.title}>
      {images[page] ? (
        // Signed provider media is intentionally served by the backend proxy.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={images[page]}
          alt={t("discovery.work_page_alt", { title: work.title, page: page + 1 })}
          className="max-h-[64vh] w-full rounded-md bg-black object-contain"
        />
      ) : (
        <p className="py-10 text-center text-sm text-muted">{t("discovery.preview_unavailable")}</p>
      )}
      <div className="mt-3 flex items-center justify-between gap-3">
        <button
          type="button"
          className="btn-ghost"
          disabled={page === 0}
          onClick={() => setPage((current) => Math.max(0, current - 1))}
        >
          <ChevronLeft aria-hidden="true" className="h-4 w-4" />
          {t("common.prev")}
        </button>
        <span className="text-xs tabular-nums text-muted">
          {t("discovery.work_page_count", { current: page + 1, total: Math.max(images.length, 1) })}
        </span>
        <button
          type="button"
          className="btn-ghost"
          disabled={page >= images.length - 1}
          onClick={() => setPage((current) => Math.min(images.length - 1, current + 1))}
        >
          {t("common.next")}
          <ChevronRight aria-hidden="true" className="h-4 w-4" />
        </button>
      </div>
    </Modal>
  );
}

function RemoteWorkCard({
  candidate,
  work,
  revealed,
  importing,
  outcome,
  onReveal,
  onPreview,
  onImport,
}: {
  candidate: DiscoveryCandidate;
  work: RemoteWorkPreview;
  revealed: boolean;
  importing: boolean;
  outcome?: RemoteWorkImportResult;
  onReveal: () => void;
  onPreview: () => void;
  onImport: () => void;
}) {
  const t = useT();
  const fmt = useI18nFormat();
  const sensitive = work.x_restrict > 0;
  const localWorkId = outcome?.local_work_id || work.local_work_id;
  const queued = outcome?.download_job_id || work.download_job_id;
  const blockedCandidate = candidate.state === "dismissed" || candidate.state === "conflict";
  return (
    <article
      className="overflow-hidden rounded-lg border border-border bg-surface"
      style={{ contentVisibility: "auto", containIntrinsicSize: "320px" }}
    >
      <div className="relative aspect-[4/3] overflow-hidden bg-subtle">
        {work.thumbnail_url ? (
          <button
            type="button"
            className="h-full w-full"
            onClick={sensitive && !revealed ? onReveal : onPreview}
            aria-label={sensitive && !revealed ? t("discovery.reveal_sensitive") : t("discovery.preview_work", { title: work.title })}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={work.thumbnail_url}
              alt=""
              className={`h-full w-full object-cover transition ${sensitive && !revealed ? "scale-110 blur-xl" : "hover:scale-[1.02]"}`}
            />
          </button>
        ) : (
          <div className="flex h-full items-center justify-center text-muted">
            <Images aria-hidden="true" className="h-8 w-8" />
          </div>
        )}
        <div className="absolute left-2 top-2 flex gap-1">
          <span className="rounded bg-black/75 px-2 py-1 text-[11px] font-medium text-white">
            {t(`discovery.work_type_${work.work_type}`)}
          </span>
          {work.page_count > 1 ? (
            <span className="rounded bg-black/75 px-2 py-1 text-[11px] text-white">
              {t("discovery.work_pages", { count: work.page_count })}
            </span>
          ) : null}
        </div>
        {sensitive && !revealed ? (
          <button
            type="button"
            className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/35 text-sm font-medium text-white"
            onClick={onReveal}
          >
            <Eye aria-hidden="true" className="h-6 w-6" />
            {t(work.x_restrict === 2 ? "discovery.reveal_r18g" : "discovery.reveal_r18")}
          </button>
        ) : null}
      </div>
      <div className="space-y-3 p-3">
        <div>
          <div className="flex items-start justify-between gap-2">
            <h3 className="line-clamp-2 text-sm font-medium text-fg">{work.title}</h3>
            <a href={work.work_url} target="_blank" rel="noreferrer" className="shrink-0 text-muted hover:text-accent" aria-label={t("discovery.open_work", { title: work.title })}>
              <ExternalLink aria-hidden="true" className="h-4 w-4" />
            </a>
          </div>
          <p className="mt-1 text-xs text-muted">{fmt.dateTime(work.created_at)}</p>
        </div>
        {localWorkId ? (
          <Link href={adminRoutes.work(localWorkId)} className="btn-ghost w-full justify-center">
            {t("discovery.open_local_work")}
          </Link>
        ) : queued ? (
          <StatusBadge status="pending" label={t("discovery.remote_work_queued")} />
        ) : (
          <button
            type="button"
            className="btn-primary w-full justify-center"
            disabled={importing || blockedCandidate || (sensitive && !revealed)}
            onClick={onImport}
          >
            {importing ? <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" /> : <ArrowDownToLine aria-hidden="true" className="h-4 w-4" />}
            {t("discovery.import_this_work")}
          </button>
        )}
        {blockedCandidate ? (
          <p className="text-xs text-warning">
            {t(candidate.state === "dismissed" ? "discovery.restore_before_import" : "discovery.resolve_before_import")}
          </p>
        ) : null}
      </div>
    </article>
  );
}

export default function RemoteCreatorDrawer({
  candidate,
  userId,
  onClose,
  onPrivateAccessError,
}: {
  candidate: DiscoveryCandidate;
  userId: number;
  onClose: () => void;
  onPrivateAccessError?: (error: unknown) => void;
}) {
  const t = useT();
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);
  const closeRef = useRef(onClose);
  const lightboxOpenRef = useRef(false);
  const toast = useToast();
  const queryClient = useQueryClient();
  const [revealed, setRevealed] = useState<Set<string>>(new Set());
  const [lightboxWork, setLightboxWork] = useState<RemoteWorkPreview | null>(null);
  const [outcomes, setOutcomes] = useState<Record<string, RemoteWorkImportResult>>({});

  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    lightboxOpenRef.current = !!lightboxWork;
  }, [lightboxWork]);

  useEffect(() => {
    previousFocus.current = document.activeElement as HTMLElement;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const panel = panelRef.current;
    window.requestAnimationFrame(() => panel?.querySelector<HTMLElement>("button:not([disabled]), a[href]")?.focus());
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !lightboxOpenRef.current) {
        event.preventDefault();
        closeRef.current();
        return;
      }
      if (event.key !== "Tab" || !panel) return;
      const controls = Array.from(panel.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
      ));
      if (!controls.length) return;
      if (event.shiftKey && document.activeElement === controls[0]) {
        event.preventDefault();
        controls.at(-1)?.focus();
      } else if (!event.shiftKey && document.activeElement === controls.at(-1)) {
        event.preventDefault();
        controls[0].focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      previousFocus.current?.focus();
    };
  }, []);

  const detail = useInfiniteQuery({
    queryKey: [
      "remote-discovery-private",
      userId,
      "discovery",
      "candidate-detail",
      candidate.id,
    ] as const,
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam, signal }): Promise<CreatorPage> => {
      if (pageParam) {
        return {
          works: await api.getDiscoveryCandidateRemoteWorks(candidate.id, {
            cursor: pageParam,
            limit: 20,
            signal,
          }),
        };
      }
      const first = await api.getDiscoveryCandidateRemoteDetail(candidate.id, {
        limit: 20,
        signal,
      });
      return { profile: first.profile, works: first.works };
    },
    getNextPageParam: (lastPage) => lastPage.works.next_cursor || undefined,
    retry: false,
  });

  useEffect(() => {
    if (detail.error) onPrivateAccessError?.(detail.error);
  }, [detail.error, onPrivateAccessError]);

  const profile = detail.data?.pages.find((page) => page.profile)?.profile;
  const works = useMemo(
    () => detail.data?.pages.flatMap((page) => page.works.items) || [],
    [detail.data?.pages],
  );
  const importWork = useMutation({
    mutationKey: queryKeys.discovery.mutation(userId, "remote-work-import"),
    mutationFn: (work: RemoteWorkPreview) => runPrivateDiscoveryRequest(
      userId,
      (signal) => api.importDiscoveryCandidateRemoteWork(
        candidate.id,
        work.work_token,
        work.x_restrict === 0 || revealed.has(work.source_work_id),
        signal,
      ),
    ),
    onSuccess: async (result, work) => {
      setOutcomes((current) => ({ ...current, [work.source_work_id]: result }));
      await queryClient.invalidateQueries({ queryKey: queryKeys.discovery.all(userId) });
      toast.success(t(result.status === "already_imported" ? "discovery.remote_work_already_imported" : result.status === "already_queued" ? "discovery.remote_work_already_queued" : "discovery.remote_work_import_queued"));
    },
    onError: (error) => {
      onPrivateAccessError?.(error);
      toast.error(importErrorMessage(error, t));
    },
  });

  const name = profile?.display_name || candidate.display_name || candidate.source_creator_id;
  const username = profile?.username;
  const avatar = profile?.avatar_url || candidate.avatar_url;
  return (
    <>
      <div
        className="fixed inset-0 z-40 bg-black/45"
        onMouseDown={(event) => {
          if (event.target === event.currentTarget) onClose();
        }}
      >
        <div
          ref={panelRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          className="absolute inset-y-0 right-0 flex w-full max-w-3xl flex-col border-l border-border bg-surface shadow-2xl"
        >
          <header className="flex shrink-0 items-center justify-between border-b border-border px-4 py-3 sm:px-6">
            <h2 id={titleId} className="text-base font-semibold text-fg">{t("discovery.creator_details")}</h2>
            <button type="button" className="btn-icon" onClick={onClose} aria-label={t("common.close_dialog")}>
              <X aria-hidden="true" className="h-5 w-5" />
            </button>
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
            {detail.isLoading ? (
              <div className="flex min-h-52 items-center justify-center gap-2 text-sm text-muted" role="status">
                <LoaderCircle aria-hidden="true" className="h-5 w-5 animate-spin" />
                {t("discovery.creator_details_loading")}
              </div>
            ) : null}
            {detail.error ? (
              <ErrorState message={t("discovery.creator_details_failed")} onRetry={() => detail.refetch()} />
            ) : null}
            {detail.data ? (
              <div className="space-y-6">
                <section className="flex items-start gap-4">
                  {avatar ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={avatar} alt={t("discovery.avatar_alt", { name })} className="h-20 w-20 shrink-0 rounded-xl object-cover" />
                  ) : (
                    <div className="h-20 w-20 shrink-0 rounded-xl bg-subtle" />
                  )}
                  <div className="min-w-0 flex-1">
                    <h3 className="truncate text-xl font-semibold text-fg">{name}</h3>
                    <p className="text-sm text-muted">{username ? `@${username}` : candidate.source_creator_id}</p>
                    {profile?.comment ? <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-muted">{profile.comment}</p> : null}
                    <a
                      href={profile?.profile_url || candidate.remote_url || "#"}
                      target="_blank"
                      rel="noreferrer"
                      className="mt-3 inline-flex items-center gap-1 text-sm text-accent hover:underline"
                    >
                      {t("discovery.open_pixiv_profile")}
                      <ExternalLink aria-hidden="true" className="h-4 w-4" />
                    </a>
                  </div>
                </section>
                <section>
                  <div className="mb-3 flex items-center justify-between gap-3">
                    <h3 className="text-base font-semibold text-fg">{t("discovery.recent_works")}</h3>
                    <span className="text-xs tabular-nums text-muted">{t("discovery.loaded_works", { count: works.length })}</span>
                  </div>
                  {works.length ? (
                    <div className="grid gap-4 sm:grid-cols-2">
                      {works.map((work) => (
                        <RemoteWorkCard
                          key={work.source_work_id}
                          candidate={candidate}
                          work={work}
                          revealed={revealed.has(work.source_work_id)}
                          importing={importWork.isPending && importWork.variables?.source_work_id === work.source_work_id}
                          outcome={outcomes[work.source_work_id]}
                          onReveal={() => setRevealed((current) => new Set(current).add(work.source_work_id))}
                          onPreview={() => setLightboxWork(work)}
                          onImport={() => importWork.mutate(work)}
                        />
                      ))}
                    </div>
                  ) : (
                    <p className="rounded-lg border border-dashed border-border px-4 py-10 text-center text-sm text-muted">
                      {t("discovery.no_remote_works")}
                    </p>
                  )}
                  {detail.hasNextPage ? (
                    <button
                      type="button"
                      className="btn-ghost mt-4 w-full justify-center"
                      disabled={detail.isFetchingNextPage}
                      onClick={() => detail.fetchNextPage()}
                    >
                      {detail.isFetchingNextPage ? <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" /> : null}
                      {t(detail.isFetchingNextPage ? "discovery.loading_more_works" : "discovery.load_more_works")}
                    </button>
                  ) : null}
                </section>
              </div>
            ) : null}
          </div>
        </div>
      </div>
      <WorkLightbox work={lightboxWork} onClose={() => setLightboxWork(null)} />
    </>
  );
}
