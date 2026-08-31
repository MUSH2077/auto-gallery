"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDownToLine,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Eye,
  Images,
  LoaderCircle,
} from "lucide-react";

import { Modal, StatusBadge, useToast } from "@/components";
import {
  ApiError,
  api,
  queryKeys,
  type DiscoveryCandidate,
  type RemoteWorkImportResult,
  type RemoteWorkPreview,
} from "@/lib/api";
import { adminRoutes } from "@/lib/adminRoutes";
import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";
import { runPrivateDiscoveryRequest } from "@/lib/remoteDiscoveryPrivateCache";

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
          className="max-h-[72vh] w-full rounded-md bg-black object-contain"
        />
      ) : (
        <p className="py-10 text-center text-sm text-muted">
          {t("discovery.preview_unavailable")}
        </p>
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
          {t("discovery.work_page_count", {
            current: page + 1,
            total: Math.max(images.length, 1),
          })}
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
      className="overflow-hidden rounded-xl border border-border bg-surface shadow-sm"
      style={{ contentVisibility: "auto", containIntrinsicSize: "360px" }}
    >
      <div className="relative aspect-[4/3] overflow-hidden bg-subtle">
        {work.thumbnail_url ? (
          <button
            type="button"
            className="h-full w-full"
            onClick={sensitive && !revealed ? onReveal : onPreview}
            aria-label={sensitive && !revealed
              ? t("discovery.reveal_sensitive")
              : t("discovery.preview_work", { title: work.title })}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={work.thumbnail_url}
              alt=""
              loading="lazy"
              decoding="async"
              className={`h-full w-full object-cover transition duration-300 ${
                sensitive && !revealed ? "scale-110 blur-xl" : "hover:scale-[1.03]"
              }`}
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
            <a
              href={work.work_url}
              target="_blank"
              rel="noreferrer"
              className="shrink-0 text-muted hover:text-accent"
              aria-label={t("discovery.open_work", { title: work.title })}
            >
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
            {importing ? (
              <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" />
            ) : (
              <ArrowDownToLine aria-hidden="true" className="h-4 w-4" />
            )}
            {t("discovery.import_this_work")}
          </button>
        )}
        {blockedCandidate ? (
          <p className="text-xs text-warning">
            {t(candidate.state === "dismissed"
              ? "discovery.restore_before_import"
              : "discovery.resolve_before_import")}
          </p>
        ) : null}
      </div>
    </article>
  );
}

export default function RemoteCreatorWorks({
  candidate,
  userId,
  works,
  onPrivateAccessError,
}: {
  candidate: DiscoveryCandidate;
  userId: number;
  works: RemoteWorkPreview[];
  onPrivateAccessError?: (error: unknown) => void;
}) {
  const t = useT();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [revealed, setRevealed] = useState<Set<string>>(new Set());
  const [lightboxWork, setLightboxWork] = useState<RemoteWorkPreview | null>(null);
  const [outcomes, setOutcomes] = useState<Record<string, RemoteWorkImportResult>>({});
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
      toast.success(t(
        result.status === "already_imported"
          ? "discovery.remote_work_already_imported"
          : result.status === "already_queued"
            ? "discovery.remote_work_already_queued"
            : "discovery.remote_work_import_queued",
      ));
    },
    onError: (error) => {
      onPrivateAccessError?.(error);
      toast.error(importErrorMessage(error, t));
    },
  });

  return (
    <>
      {works.length ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {works.map((work) => (
            <RemoteWorkCard
              key={work.source_work_id}
              candidate={candidate}
              work={work}
              revealed={revealed.has(work.source_work_id)}
              importing={importWork.isPending
                && importWork.variables?.source_work_id === work.source_work_id}
              outcome={outcomes[work.source_work_id]}
              onReveal={() => setRevealed((current) => (
                new Set(current).add(work.source_work_id)
              ))}
              onPreview={() => setLightboxWork(work)}
              onImport={() => importWork.mutate(work)}
            />
          ))}
        </div>
      ) : (
        <p className="rounded-xl border border-dashed border-border px-4 py-14 text-center text-sm text-muted">
          {t("discovery.no_remote_works")}
        </p>
      )}
      <WorkLightbox work={lightboxWork} onClose={() => setLightboxWork(null)} />
    </>
  );
}
