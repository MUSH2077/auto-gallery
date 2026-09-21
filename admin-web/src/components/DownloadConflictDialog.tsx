"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, type DownloadConflictItem, type DownloadConflictWinner } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { usePermissions } from "@/lib/usePermissions";
import { useToast } from "./Toast";
import ErrorState from "./ErrorState";
import { DirectVideoPlayer } from "./MediaAssetRenderer";

const NEEDS_RETRY_STATUS = ["needs", "retry"].join("_");

function compactHash(value?: string | null) {
  return value ? `${value.slice(0, 12)}…${value.slice(-8)}` : "—";
}

function MediaSide({
  taskId,
  item,
  side,
  zoom,
  onZoom,
}: {
  taskId: string;
  item: DownloadConflictItem;
  side: DownloadConflictWinner;
  zoom: number;
  onZoom: (value: number) => void;
}) {
  const t = useT();
  const isCanonical = side === "canonical";
  const viewportRef = useRef<HTMLDivElement>(null);
  const media = useQuery({
    queryKey: ["download-conflict-media", taskId, item.relative_path, side],
    queryFn: () => api.downloadConflictMedia(taskId, item.relative_path, side),
    staleTime: Infinity,
  });
  const url = useMemo(
    () => media.data ? URL.createObjectURL(media.data.blob) : null,
    [media.data],
  );
  useEffect(() => () => {
    if (url) URL.revokeObjectURL(url);
  }, [url]);
  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const handleWheel = (event: WheelEvent) => {
      if (!event.ctrlKey) return;
      event.preventDefault();
      onZoom(Math.max(0.25, Math.min(4, zoom + (event.deltaY < 0 ? 0.15 : -0.15))));
    };
    viewport.addEventListener("wheel", handleWheel, { passive: false });
    return () => viewport.removeEventListener("wheel", handleWheel);
  }, [onZoom, zoom]);
  const size = isCanonical ? item.canonical_size : item.staged_size;
  const sha = isCanonical ? item.canonical_sha256 : item.staged_sha256;
  const label = t(`jobs.conflict.${side}`);
  return (
    <section className="min-w-0 rounded-md border border-border bg-surface">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-2 text-xs">
        <strong>{label}</strong>
        <span className="font-mono text-muted" title={sha || undefined}>{compactHash(sha)}</span>
        <span className="text-muted">{typeof size === "number" ? `${(size / 1024).toFixed(1)} KiB` : "—"}</span>
      </div>
      <div
        ref={viewportRef}
        className="h-[44vh] overflow-auto bg-black/90 p-3"
      >
        {media.isLoading ? (
          <div className="h-full animate-pulse rounded bg-subtle" />
        ) : media.error ? (
          <p role="alert" className="text-sm text-danger">{media.error.message}</p>
        ) : item.mime_type.startsWith("image/") && url ? (
          <img
            src={url}
            alt={t("jobs.conflict.preview_alt", { side: label, path: item.relative_path })}
            className="max-w-none origin-top-left select-none"
            style={{ transform: `scale(${zoom})` }}
            draggable={false}
          />
        ) : item.mime_type.startsWith("video/") && url ? (
          <DirectVideoPlayer
            src={url}
            label={t("jobs.conflict.preview_alt", { side: label, path: item.relative_path })}
            className="max-h-full max-w-full"
          />
        ) : (
          <pre className="whitespace-pre-wrap break-all text-xs text-white">{t("jobs.conflict.nonvisual")}</pre>
        )}
      </div>
    </section>
  );
}

export default function DownloadConflictDialog({
  open,
  taskId,
  existingResolution,
  onClose,
}: {
  open: boolean;
  taskId: string;
  existingResolution?: { resolution_id: string; expires_at?: string } | null;
  onClose: () => void;
}) {
  const t = useT();
  const toast = useToast();
  const qc = useQueryClient();
  const { isAdmin } = usePermissions();
  const dialogRef = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState(1);
  const [decisions, setDecisions] = useState<Record<string, DownloadConflictWinner>>({});
  const conflicts = useQuery({
    queryKey: ["download-conflicts", taskId],
    queryFn: () => api.getDownloadConflicts(taskId),
    enabled: open && !existingResolution,
  });

  useEffect(() => {
    if (!open) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", closeOnEscape);
    dialogRef.current?.focus();
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [onClose, open]);

  useEffect(() => {
    if (!conflicts.data?.items) return;
    setDecisions(Object.fromEntries(conflicts.data.items.map((item) => [
      item.relative_path,
      item.evidence.recommended_winner,
    ])));
  }, [conflicts.data?.items]);

  const resolution = existingResolution || conflicts.data?.resolution || null;
  const complete = useMemo(
    () => !!conflicts.data?.items.length && conflicts.data.items.every((item) => decisions[item.relative_path]),
    [conflicts.data?.items, decisions],
  );
  const refresh = async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: queryKeys.tasks.all }),
      qc.invalidateQueries({ queryKey: queryKeys.tasks.detail(taskId) }),
      qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }),
      qc.invalidateQueries({ queryKey: ["download-conflicts", taskId] }),
    ]);
  };
  const resolve = useMutation({
    mutationFn: () => api.resolveDownloadConflicts(
      taskId,
      (conflicts.data?.items || []).map((item) => ({
        relative_path: item.relative_path,
        winner: decisions[item.relative_path],
      })),
    ),
    onSuccess: async (result) => {
      await refresh();
      toast.success(result.retry?.status === NEEDS_RETRY_STATUS
        ? t("jobs.conflict.resolved_needs_retry")
        : t("jobs.conflict.resolved"));
      onClose();
    },
    onError: (error: Error) => toast.error(error.message),
  });
  const rollback = useMutation({
    mutationFn: () => api.rollbackDownloadConflictResolution(taskId, resolution!.resolution_id),
    onSuccess: async () => {
      await refresh();
      toast.success(t("jobs.conflict.rolled_back"));
      onClose();
    },
    onError: (error: Error) => toast.error(error.message),
  });

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[70] flex items-start justify-center bg-black/60 p-3 sm:p-6" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div ref={dialogRef} tabIndex={-1} role="dialog" aria-modal="true" aria-label={t("jobs.conflict.title")} className="max-h-[94vh] w-full max-w-7xl overflow-y-auto rounded-lg border border-border bg-canvas shadow-2xl">
        <header className="sticky top-0 z-10 flex flex-wrap items-center justify-between gap-3 border-b border-border bg-canvas/95 px-4 py-3 backdrop-blur">
          <div>
            <h2 className="font-semibold">{t("jobs.conflict.title")}</h2>
            <p className="mt-1 max-w-3xl text-xs text-muted">{t("jobs.conflict.desc")}</p>
          </div>
          <div className="flex items-center gap-2">
            <button type="button" className="btn-ghost text-xs" onClick={() => setZoom(Math.max(0.25, zoom - 0.25))} aria-label={t("jobs.conflict.zoom_out")}>−</button>
            <span className="w-14 text-center font-mono text-xs">{Math.round(zoom * 100)}%</span>
            <button type="button" className="btn-ghost text-xs" onClick={() => setZoom(Math.min(4, zoom + 0.25))} aria-label={t("jobs.conflict.zoom_in")}>+</button>
            <button type="button" className="btn-ghost" onClick={onClose}>{t("common.close")}</button>
          </div>
        </header>

        <div className="space-y-6 p-4">
          {resolution && (
            <section className="rounded-md border border-success/30 bg-success-subtle p-4 text-sm text-success">
              <strong>{t("jobs.conflict.resolution_applied")}</strong>
              <p className="mt-1 text-xs">{t("jobs.conflict.rollback_until", { time: resolution.expires_at || "—" })}</p>
              {isAdmin && <button type="button" className="btn-danger mt-3" disabled={rollback.isPending} onClick={() => rollback.mutate()}>{t("jobs.conflict.rollback")}</button>}
            </section>
          )}
          {conflicts.isLoading && <div className="h-96 animate-pulse rounded-md bg-subtle" />}
          {conflicts.error && !resolution && <ErrorState message={(conflicts.error as Error).message} onRetry={() => conflicts.refetch()} />}
          {conflicts.data?.items.map((item, index) => (
            <article key={item.relative_path} className="rounded-lg border border-border bg-surface p-3">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="font-mono text-sm break-all">{index + 1}. {item.relative_path}</h3>
                  <p className={`mt-1 text-xs ${item.evidence.auto_eligible ? "text-success" : "text-warning"}`}>
                    {item.evidence.auto_eligible ? t("jobs.conflict.evidence_complete") : t("jobs.conflict.evidence_incomplete")}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2" role="radiogroup" aria-label={t("jobs.conflict.choose_winner")}>
                  {(["canonical", "staged"] as const).map((winner) => (
                    <label key={winner} className={`flex min-h-11 cursor-pointer items-center gap-2 rounded-md border px-3 text-xs ${decisions[item.relative_path] === winner ? "border-accent bg-accent-subtle text-accent" : "border-border"}`}>
                      <input type="radio" name={`winner-${index}`} value={winner} checked={decisions[item.relative_path] === winner} onChange={() => setDecisions((current) => ({ ...current, [item.relative_path]: winner }))} />
                      {t(`jobs.conflict.keep_${winner}`)}
                    </label>
                  ))}
                </div>
              </div>
              <div className="grid gap-3 lg:grid-cols-2">
                <MediaSide taskId={taskId} item={item} side="canonical" zoom={zoom} onZoom={setZoom} />
                <MediaSide taskId={taskId} item={item} side="staged" zoom={zoom} onZoom={setZoom} />
              </div>
              <details className="mt-3 text-xs">
                <summary className="cursor-pointer font-medium">{t("jobs.conflict.evidence")}</summary>
                <pre className="mt-2 overflow-auto rounded bg-subtle p-3">{JSON.stringify(item.evidence, null, 2)}</pre>
              </details>
            </article>
          ))}
        </div>
        {isAdmin && conflicts.data?.items.length ? (
          <footer className="sticky bottom-0 flex flex-wrap items-center justify-between gap-3 border-t border-border bg-canvas/95 px-4 py-3 backdrop-blur">
            <p className="text-xs text-muted">{t("jobs.conflict.full_batch_required")}</p>
            <button type="button" className="btn-primary" disabled={!complete || resolve.isPending} onClick={() => resolve.mutate()}>
              {resolve.isPending ? t("common.saving") : t("jobs.conflict.apply")}
            </button>
          </footer>
        ) : null}
      </div>
    </div>
  );
}
