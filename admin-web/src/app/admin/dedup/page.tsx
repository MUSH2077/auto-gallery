"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, AssetDedupCase, queryKeys } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import { useStaggeredEntrance } from "@/lib/motion";
import {
  ConfirmDialog,
  EmptyState,
  ErrorState,
  PageHeader,
  PageShell,
  PermissionGuard,
  SourceBadge,
} from "@/components";
import { useT } from "@/lib/i18n";

const PAGE_SIZE = 25;

function Metric({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div className="min-w-0">
      <div className="text-xs text-muted">{label}</div>
      <div className="truncate font-mono text-sm text-fg">{value}</div>
    </div>
  );
}

function AssetPanel({
  asset,
  suggested,
  onMerge,
  pending,
  actionable,
}: {
  asset: AssetDedupCase["left"];
  suggested: boolean;
  onMerge: () => void;
  pending: boolean;
  actionable: boolean;
}) {
  const t = useT();
  return (
    <section className="min-w-0 rounded-md border border-border bg-subtle/40 p-3">
      <div className="relative aspect-square overflow-hidden rounded-md bg-black/5 dark:bg-black/20">
        <img
          src={asset.preview_url}
          alt={asset.file_name}
          className="h-full w-full object-contain"
          loading="lazy"
          decoding="async"
        />
        {suggested && (
          <span className="absolute left-2 top-2 rounded-full bg-success px-2 py-1 text-xs font-medium text-white shadow">
            {t("asset_dedup.recommended")}
          </span>
        )}
      </div>
      <div className="mt-3 flex min-w-0 items-center gap-2">
        {asset.source && <SourceBadge source={asset.source} />}
        <span className="truncate text-sm font-medium text-fg">
          {asset.work_title || asset.file_name}
        </span>
      </div>
      <div className="mt-1 truncate text-xs text-muted">
        {asset.creator_name || t("asset_dedup.unknown_creator")}
        {asset.posted_at ? ` · ${new Date(asset.posted_at).toLocaleString()}` : ""}
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2">
        <Metric
          label={t("asset_dedup.resolution")}
          value={
            asset.width && asset.height
              ? `${asset.width}×${asset.height}`
              : "—"
          }
        />
        <Metric
          label={t("asset_dedup.file_size")}
          value={asset.file_size != null ? formatBytes(asset.file_size) : "—"}
        />
        <Metric
          label={t("asset_dedup.mime")}
          value={asset.mime_type || "—"}
        />
        <Metric
          label={t("asset_dedup.source_id")}
          value={asset.source_work_id || "—"}
        />
      </div>
      {actionable && (
        <button
          type="button"
          onClick={onMerge}
          disabled={pending}
          className="btn-primary mt-4 min-h-11 w-full"
        >
          {t("asset_dedup.keep_this")}
        </button>
      )}
    </section>
  );
}

function EvidencePanel({ item }: { item: AssetDedupCase }) {
  const t = useT();
  const metadata = item.evidence.facts?.metadata;
  const score = Math.round(item.evidence.total_score);
  return (
    <div className="mt-4 border-t border-border pt-4">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium text-fg">
          {t("asset_dedup.evidence")}
        </span>
        <span
          className={`badge ${
            score >= 95
              ? "border-success/30 bg-success-subtle text-success"
              : "border-warning/30 bg-warning-subtle text-warning"
          }`}
        >
          {t("asset_dedup.score")} {score}
        </span>
        {item.evidence.sha256_equal && (
          <span className="badge border-success/30 bg-success-subtle text-success">
            SHA-256
          </span>
        )}
        {metadata?.same_canonical_creator && (
          <span className="badge">{t("asset_dedup.same_creator")}</span>
        )}
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Metric
          label="pHash"
          value={
            item.evidence.phash_distance != null
              ? String(item.evidence.phash_distance)
              : "—"
          }
        />
        <Metric
          label="SSIM"
          value={
            item.evidence.ssim_score != null
              ? item.evidence.ssim_score.toFixed(4)
              : "—"
          }
        />
        <Metric
          label={t("asset_dedup.aspect_delta")}
          value={
            item.evidence.aspect_ratio_delta != null
              ? `${(item.evidence.aspect_ratio_delta * 100).toFixed(2)}%`
              : "—"
          }
        />
        <Metric
          label={t("asset_dedup.visual_score")}
          value={item.evidence.visual_score.toFixed(1)}
        />
        <Metric
          label={t("asset_dedup.metadata_score")}
          value={`+${item.evidence.metadata_score.toFixed(1)}`}
        />
        <Metric
          label={t("asset_dedup.time_delta")}
          value={
            metadata?.min_posted_delta_hours != null
              ? `${metadata.min_posted_delta_hours.toFixed(1)}h`
              : "—"
          }
        />
      </div>
    </div>
  );
}

export default function DedupPage() {
  const t = useT();
  const queryClient = useQueryClient();
  const [status, setStatus] = useState("pending");
  const [page, setPage] = useState(0);
  const [confirm, setConfirm] = useState<{
    item: AssetDedupCase;
    representativeId: string;
  } | null>(null);
  const [scanId, setScanId] = useState<string | null>(null);

  const cases = useQuery({
    queryKey: queryKeys.dedup.cases(status, page),
    queryFn: () => api.listAssetDedupCases(status, page * PAGE_SIZE, PAGE_SIZE),
  });
  const scanStatus = useQuery({
    queryKey: queryKeys.dedup.scan(scanId || ""),
    queryFn: () => api.getAssetDedupScan(scanId!),
    enabled: !!scanId,
    refetchInterval: (query) => {
      const current = query.state.data?.status;
      return current && ["complete", "failed"].includes(current) ? false : 2000;
    },
  });
  const scan = useMutation({
    mutationFn: () => api.startAssetDedupScan(true),
    onSuccess: (result) => setScanId(result.scan_id),
  });
  const decide = useMutation({
    mutationFn: ({
      item,
      action,
      representativeId,
    }: {
      item: AssetDedupCase;
      action: "merge" | "separate" | "defer";
      representativeId?: string;
    }) =>
      api.decideAssetDedupCase(item.id, {
        expected_revision: item.revision,
        action,
        representative_asset_id: representativeId,
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: () => {
      setConfirm(null);
      queryClient.invalidateQueries({ queryKey: ["dedup", "cases"] });
    },
  });

  const items = cases.data?.items || [];
  const entrances = useStaggeredEntrance(items.map((item) => item.id));
  const totalPages = Math.max(
    1,
    Math.ceil((cases.data?.total || 0) / PAGE_SIZE),
  );
  const scanLabel = useMemo(() => {
    if (scan.isPending) return t("asset_dedup.scan_starting");
    if (!scanStatus.data) return t("asset_dedup.scan");
    if (scanStatus.data.status === "complete")
      return t("asset_dedup.scan_again");
    if (scanStatus.data.status === "failed")
      return t("asset_dedup.scan_retry");
    return t("asset_dedup.scanning", {
      count: scanStatus.data.assets_scanned,
    });
  }, [scan.isPending, scanStatus.data, t]);

  return (
    <PermissionGuard module="curation">
      <PageShell size="wide">
        <PageHeader
          title={t("asset_dedup.title")}
          description={t("asset_dedup.desc")}
        >
          <button
            type="button"
            onClick={() => scan.mutate()}
            disabled={
              scan.isPending ||
              (!!scanStatus.data &&
                !["complete", "failed"].includes(scanStatus.data.status))
            }
            className="btn-primary min-h-11"
          >
            {scanLabel}
          </button>
        </PageHeader>

        <div className="mb-5 rounded-md border border-border bg-subtle/40 p-4 text-sm leading-6 text-muted">
          {t("asset_dedup.policy")}
        </div>

        <div
          className="mb-5 flex gap-2 overflow-x-auto"
          role="tablist"
          aria-label={t("asset_dedup.status_filter")}
        >
          {(["pending", "merged", "separate", "deferred"] as const).map(
            (value) => (
              <button
                key={value}
                type="button"
                role="tab"
                aria-selected={status === value}
                onClick={() => {
                  setStatus(value);
                  setPage(0);
                }}
                className={
                  status === value ? "btn-primary min-h-11" : "btn-ghost min-h-11"
                }
              >
                {t(`asset_dedup.status_${value}`)}
              </button>
            ),
          )}
        </div>

        {(cases.error || scan.error || scanStatus.error) && (
          <ErrorState
            message={
              ((cases.error || scan.error || scanStatus.error) as Error).message
            }
            onRetry={() => cases.refetch()}
          />
        )}

        {cases.isLoading && (
          <div className="space-y-4">
            {Array.from({ length: 2 }).map((_, index) => (
              <div
                key={index}
                className="h-[34rem] animate-pulse rounded-md bg-subtle"
              />
            ))}
          </div>
        )}

        {cases.data && items.length === 0 && (
          <EmptyState
            title={t("asset_dedup.empty")}
            description={t("asset_dedup.empty_desc")}
          />
        )}

        <div className="space-y-5">
          {items.map((item, index) => {
            const entrance = entrances(item.id, index);
            const actionable =
              item.status === "pending" || item.status === "deferred";
            return (
              <article
                key={item.id}
                className={`card p-4 sm:p-5 ${entrance.className}`}
                style={entrance.style}
              >
                <div className="grid gap-4 lg:grid-cols-2">
                  <AssetPanel
                    asset={item.left}
                    suggested={
                      item.suggested_representative_asset_id === item.left.id
                    }
                    pending={decide.isPending}
                    actionable={actionable}
                    onMerge={() =>
                      setConfirm({
                        item,
                        representativeId: item.left.id,
                      })
                    }
                  />
                  <AssetPanel
                    asset={item.right}
                    suggested={
                      item.suggested_representative_asset_id === item.right.id
                    }
                    pending={decide.isPending}
                    actionable={actionable}
                    onMerge={() =>
                      setConfirm({
                        item,
                        representativeId: item.right.id,
                      })
                    }
                  />
                </div>
                <EvidencePanel item={item} />
                {actionable && (
                  <div className="mt-4 flex flex-wrap justify-end gap-2 border-t border-border pt-4">
                    {item.status === "pending" && (
                      <button
                        type="button"
                        className="btn-ghost min-h-11"
                        disabled={decide.isPending}
                        onClick={() =>
                          decide.mutate({ item, action: "defer" })
                        }
                      >
                        {t("asset_dedup.defer")}
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn-ghost min-h-11 text-danger"
                      disabled={decide.isPending}
                      onClick={() =>
                        decide.mutate({ item, action: "separate" })
                      }
                    >
                      {t("asset_dedup.separate")}
                    </button>
                  </div>
                )}
              </article>
            );
          })}
        </div>

        {cases.data && cases.data.total > PAGE_SIZE && (
          <div className="mt-6 flex items-center justify-between">
            <button
              type="button"
              className="btn-ghost min-h-11"
              disabled={page === 0}
              onClick={() => setPage((value) => Math.max(0, value - 1))}
            >
              {t("common.prev")}
            </button>
            <span className="text-sm text-muted">
              {page + 1} / {totalPages}
            </span>
            <button
              type="button"
              className="btn-ghost min-h-11"
              disabled={page + 1 >= totalPages}
              onClick={() => setPage((value) => value + 1)}
            >
              {t("common.next")}
            </button>
          </div>
        )}

        <ConfirmDialog
          open={!!confirm}
          title={t("asset_dedup.confirm_title")}
          message={t("asset_dedup.confirm_message")}
          isPending={decide.isPending}
          error={(decide.error as Error)?.message}
          onCancel={() => setConfirm(null)}
          onConfirm={() => {
            if (!confirm) return;
            decide.mutate({
              item: confirm.item,
              action: "merge",
              representativeId: confirm.representativeId,
            });
          }}
        />
      </PageShell>
    </PermissionGuard>
  );
}
