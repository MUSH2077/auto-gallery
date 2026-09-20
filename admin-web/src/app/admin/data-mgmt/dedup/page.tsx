"use client";

import { useEffect, useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
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
import { AdminOperationStatus } from "@/components/AdminOperationStatus";
import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";
import { secureRandomUuid } from "@/lib/random";
import { useAdminOperation } from "@/lib/useAdminOperation";

const PAGE_SIZE = 25;
const DEDUP_STATUSES = ["pending", "merged", "separate", "deferred"] as const;
type DedupStatus = typeof DEDUP_STATUSES[number];

type AssetScanResult = {
  scan_id: string;
  status: string;
  assets_scanned: number;
  candidates_evaluated: number;
  cases_created: number;
  assets_grouped: number;
  bytes_reclaimable: number;
};

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
  const fmt = useI18nFormat();
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
        {asset.posted_at ? ` · ${fmt.dateTime(asset.posted_at)}` : ""}
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
          <span className="badge">
            {t("asset_dedup.same_creator")} +{metadata.creator_bonus ?? 6}
          </span>
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
              ? `${metadata.min_posted_delta_hours.toFixed(1)}h${
                  metadata.time_bonus
                    ? ` (+${metadata.time_bonus.toFixed(0)})`
                    : ""
                }`
              : "—"
          }
        />
      </div>
    </div>
  );
}

function DedupContent() {
  const t = useT();
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const requestedStatus = searchParams.get("status");
  const status: DedupStatus = DEDUP_STATUSES.includes(requestedStatus as DedupStatus)
    ? requestedStatus as DedupStatus
    : "pending";
  const paramsKey = searchParams.toString();
  const [page, setPage] = useState(0);
  const [confirm, setConfirm] = useState<{
    item: AssetDedupCase;
    action: "merge" | "separate";
    representativeId?: string;
    requestId: string;
  } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    if (!requestedStatus || requestedStatus === status) return;
    const next = new URLSearchParams(paramsKey);
    next.set("status", status);
    router.replace(`${pathname}?${next.toString()}`, { scroll: false });
  }, [paramsKey, pathname, requestedStatus, router, status]);

  useEffect(() => {
    setPage(0);
  }, [status]);

  const selectStatus = (nextStatus: DedupStatus) => {
    const next = new URLSearchParams(paramsKey);
    next.set("status", nextStatus);
    router.push(`${pathname}?${next.toString()}`, { scroll: false });
  };

  const cases = useQuery({
    queryKey: queryKeys.dedup.cases(status, page),
    queryFn: () => api.listAssetDedupCases(status, page * PAGE_SIZE, PAGE_SIZE),
  });
  const scan = useAdminOperation<AssetScanResult>({
    operationType: "asset-dedup-scan",
    scope: "global",
    startOperation: () => api.startAssetDedupScan(true),
    loadLatest: () => api.getLatestAssetDedupScan<AssetScanResult>(),
    onCompleted: () => {
      void queryClient.invalidateQueries({ queryKey: ["dedup", "cases"] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.tasks.all });
    },
  });
  const decide = useMutation({
    mutationFn: ({
      item,
      action,
      representativeId,
      requestId,
    }: {
      item: AssetDedupCase;
      action: "merge" | "separate" | "defer";
      representativeId?: string;
      requestId: string;
    }) =>
      api.decideAssetDedupCase(item.id, {
        expected_revision: item.revision,
        action,
        representative_asset_id: representativeId,
        idempotency_key: requestId,
      }),
    onMutate: () => setNotice(null),
    onSuccess: (result) => {
      setConfirm(null);
      setNotice(t(`asset_dedup.${result.action}_complete`));
      void queryClient.invalidateQueries({ queryKey: ["dedup"] });
    },
  });

  const items = cases.data?.items || [];
  const entrances = useStaggeredEntrance(items.map((item) => item.id));
  const totalPages = Math.max(
    1,
    Math.ceil((cases.data?.total || 0) / PAGE_SIZE),
  );
  const scanLabel = useMemo(() => {
    if (scan.isStarting || scan.isRetrying) return t("asset_dedup.scan_starting");
    if (scan.canRetry) return t("asset_dedup.scan_retry");
    if (scan.isActive)
      return t("asset_dedup.scanning", {
        count: scan.task?.progress?.current ?? 0,
      });
    if (scan.snapshot) return t("asset_dedup.scan_again");
    return t("asset_dedup.scan");
  }, [scan.canRetry, scan.isActive, scan.isRetrying, scan.isStarting, scan.snapshot, scan.task?.progress?.current, t]);

  return (
      <PageShell>
        <PageHeader
          title={t("asset_dedup.title")}
          description={t("asset_dedup.desc")}
          meta={cases.data ? t("asset_dedup.filtered_count", { count: cases.data.total }) : undefined}
        >
          <button
            type="button"
            onClick={() => scan.canRetry ? scan.retry() : scan.start()}
            disabled={!scan.canStart && !scan.canRetry}
            className="btn-primary min-h-11"
          >
            {scanLabel}
          </button>
        </PageHeader>

        {(scan.taskId || scan.snapshot || scan.isStarting || scan.isLatestLoading || scan.latestError) && (
          <AdminOperationStatus controller={scan} />
        )}

        {notice && (
          <div role="status" className="mb-5 rounded-md border border-success/25 bg-success-subtle p-3 text-sm text-success">
            {notice}
          </div>
        )}

        <div className="mb-5 rounded-md border border-border bg-subtle/40 p-4 text-sm leading-6 text-muted">
          {t("asset_dedup.policy")}
        </div>

        <div
          className="mb-5 flex gap-2 overflow-x-auto"
          role="tablist"
          aria-label={t("asset_dedup.status_filter")}
        >
          {DEDUP_STATUSES.map(
            (value) => (
              <button
                id={`dedup-tab-${value}`}
                key={value}
                type="button"
                role="tab"
                aria-selected={status === value}
                aria-controls="dedup-cases-panel"
                onClick={() => {
                  setPage(0);
                  selectStatus(value);
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

        <div
          id="dedup-cases-panel"
          role="tabpanel"
          aria-labelledby={`dedup-tab-${status}`}
        >
        {(cases.error || decide.error) && (
          <ErrorState
            message={
              ((cases.error || decide.error) as Error).message
            }
            onRetry={cases.error ? () => cases.refetch() : undefined}
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
                    onMerge={() => {
                      decide.reset();
                      setConfirm({
                        item,
                        action: "merge",
                        representativeId: item.left.id,
                        requestId: secureRandomUuid(),
                      });
                    }}
                  />
                  <AssetPanel
                    asset={item.right}
                    suggested={
                      item.suggested_representative_asset_id === item.right.id
                    }
                    pending={decide.isPending}
                    actionable={actionable}
                    onMerge={() => {
                      decide.reset();
                      setConfirm({
                        item,
                        action: "merge",
                        representativeId: item.right.id,
                        requestId: secureRandomUuid(),
                      });
                    }}
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
                          decide.mutate({ item, action: "defer", requestId: secureRandomUuid() })
                        }
                      >
                        {t("asset_dedup.defer")}
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn-ghost min-h-11 text-danger"
                      disabled={decide.isPending}
                      onClick={() => {
                        decide.reset();
                        setConfirm({
                          item,
                          action: "separate",
                          requestId: secureRandomUuid(),
                        });
                      }}
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
        </div>

        <ConfirmDialog
          open={!!confirm}
          title={t(confirm?.action === "separate" ? "asset_dedup.separate_confirm_title" : "asset_dedup.confirm_title")}
          message={t(confirm?.action === "separate" ? "asset_dedup.separate_confirm_message" : "asset_dedup.confirm_message")}
          isPending={decide.isPending}
          error={(decide.error as Error)?.message}
          onCancel={() => { decide.reset(); setConfirm(null); }}
          onConfirm={() => {
            if (!confirm) return;
            decide.mutate({
              item: confirm.item,
              action: confirm.action,
              representativeId: confirm.representativeId,
              requestId: confirm.requestId,
            });
          }}
        />
      </PageShell>
  );
}

export default function DedupPage() {
  return (
    <PermissionGuard module="curation">
      <DedupContent />
    </PermissionGuard>
  );
}
