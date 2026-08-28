"use client";

import { useEffect, useLayoutEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDownToLine, CircleAlert, ExternalLink, RotateCcw, UserRound, XCircle } from "lucide-react";

import { EmptyState, ErrorState, FilterBar, Modal, Pagination, SectionPanel, SelectionBar, StatusBadge, TableSkeleton, useToast } from "@/components";
import {
  ApiError,
  api,
  queryKeys,
  type DiscoveryCandidate,
  type DiscoveryCandidateState,
  type DiscoveryConfidence,
  type RemoteAccountRead,
  type RemoteDiscoverySource,
} from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";
import { runPrivateDiscoveryRequest } from "@/lib/remoteDiscoveryPrivateCache";
import ConflictResolutionDialog, { type ConflictResolutionValue } from "./ConflictResolutionDialog";
import {
  candidateAvatar,
  candidateUsername,
  confidenceReasonLabel,
  localCreatorIds,
  providerLabel,
  safeDiscoveryError,
} from "./discoveryPresentation";

const PAGE_SIZE = 25;
type LocalFilter = "" | "matched" | "unmatched" | "conflict";

function Avatar({ candidate }: { candidate: DiscoveryCandidate }) {
  const t = useT();
  const [failed, setFailed] = useState(false);
  const name = candidate.display_name || candidate.source_creator_id;
  const src = candidateAvatar(candidate);
  if (!src || failed) {
    return (
      <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-border bg-subtle text-muted">
        <UserRound aria-hidden="true" className="h-5 w-5" />
      </span>
    );
  }
  return (
    // Provider avatar URLs are immutable snapshots, not application assets.
    // eslint-disable-next-line @next/next/no-img-element
    <img src={src} alt={t("discovery.avatar_alt", { name })} className="h-10 w-10 shrink-0 rounded-lg object-cover" onError={() => setFailed(true)} />
  );
}

function ConfidenceDetails({ candidate }: { candidate: DiscoveryCandidate }) {
  const t = useT();
  const tone = candidate.confidence === "high" ? "up" : candidate.confidence === "medium" ? "warning" : "unknown";
  return (
    <div className="min-w-[10rem]">
      <StatusBadge status={tone} label={t(`discovery.confidence_${candidate.confidence}`)} />
      <ul className="mt-1.5 space-y-0.5 text-xs leading-4 text-muted">
        {(candidate.confidence_reasons || []).slice(0, 3).map((reason, index) => (
          <li key={`${typeof reason === "string" ? reason : "reason"}-${index}`}>• {confidenceReasonLabel(t, reason)}</li>
        ))}
      </ul>
    </div>
  );
}

function CandidateIdentity({ candidate }: { candidate: DiscoveryCandidate }) {
  const t = useT();
  const name = candidate.display_name || candidate.source_creator_id;
  const username = candidateUsername(candidate);
  return (
    <div className="flex min-w-[12rem] items-center gap-3">
      <Avatar candidate={candidate} />
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-1.5">
          <span className="truncate font-medium text-fg">{name}</span>
          {candidate.remote_url ? (
            <a href={candidate.remote_url} target="_blank" rel="noreferrer" aria-label={t("discovery.open_profile", { name })} className="shrink-0 text-muted hover:text-accent">
              <ExternalLink aria-hidden="true" className="h-3.5 w-3.5" />
            </a>
          ) : null}
        </div>
        <p className="truncate text-xs text-muted">{username ? `@${username}` : candidate.source_creator_id}</p>
      </div>
    </div>
  );
}

function LocalMatch({ candidate }: { candidate: DiscoveryCandidate }) {
  const t = useT();
  const ids = localCreatorIds(candidate);
  if (candidate.state === "conflict" || ids.length > 1) {
    return <StatusBadge status="failed" label={t("discovery.local_match_count", { count: ids.length })} />;
  }
  if (ids.length === 1 || candidate.subscription_id) return <StatusBadge status="up" label={t("discovery.local_matched")} />;
  return <span className="text-xs text-muted">{t("discovery.no_local_match")}</span>;
}

function RowActions({
  candidate,
  onImport,
  onDismiss,
  onRestore,
  onResolve,
  pending,
}: {
  candidate: DiscoveryCandidate;
  onImport: () => void;
  onDismiss: () => void;
  onRestore: () => void;
  onResolve: () => void;
  pending: boolean;
}) {
  const t = useT();
  const name = candidate.display_name || candidate.source_creator_id;
  if (candidate.state === "conflict") {
    return (
      <div className="flex flex-wrap gap-1">
        <button type="button" className="btn-primary whitespace-nowrap" disabled={pending} onClick={onResolve} aria-label={t("discovery.resolve_candidate", { name })}>
          <CircleAlert aria-hidden="true" className="h-4 w-4" />
          {t("discovery.resolve_conflict")}
        </button>
        <button type="button" className="btn-ghost" disabled={pending} onClick={onDismiss} aria-label={t("discovery.dismiss_candidate", { name })}>
          <XCircle aria-hidden="true" className="h-4 w-4" />
        </button>
      </div>
    );
  }
  if (candidate.state === "dismissed") {
    return (
      <button type="button" className="btn-ghost whitespace-nowrap" disabled={pending} onClick={onRestore} aria-label={t("discovery.restore_candidate", { name })}>
        <RotateCcw aria-hidden="true" className="h-4 w-4" />
        {t("discovery.restore")}
      </button>
    );
  }
  if (candidate.state === "pending") {
    return (
      <div className="flex flex-wrap gap-1">
        <button type="button" className="btn-primary whitespace-nowrap" disabled={pending} onClick={onImport} aria-label={t("discovery.import_candidate", { name })}>
          <ArrowDownToLine aria-hidden="true" className="h-4 w-4" />
          {t("discovery.import")}
        </button>
        <button type="button" className="btn-ghost" disabled={pending} onClick={onDismiss} aria-label={t("discovery.dismiss_candidate", { name })}>
          <XCircle aria-hidden="true" className="h-4 w-4" />
        </button>
      </div>
    );
  }
  return <StatusBadge status="complete" label={t("discovery.status_imported")} />;
}

function ImportDialog({
  count,
  open,
  pending,
  onClose,
  onConfirm,
}: {
  count: number;
  open: boolean;
  pending: boolean;
  onClose: () => void;
  onConfirm: (syncNow: boolean) => void;
}) {
  const t = useT();
  const [syncNow, setSyncNow] = useState(false);
  useEffect(() => {
    if (open) setSyncNow(false);
  }, [open]);
  return (
    <Modal open={open} onClose={onClose} title={t("discovery.import_title")}>
      <p className="text-sm leading-5 text-muted">{t("discovery.import_message", { count })}</p>
      <label className="mt-4 flex min-h-11 cursor-pointer items-center gap-2 rounded-md border border-border px-3 text-sm text-fg">
        <input type="checkbox" className="rounded" checked={syncNow} onChange={(event) => setSyncNow(event.target.checked)} />
        {t("discovery.sync_immediately")}
      </label>
      <div className="mt-5 flex flex-wrap justify-end gap-2">
        <button type="button" className="btn-ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button type="button" className="btn-primary" disabled={pending} onClick={() => onConfirm(syncNow)}>{t("discovery.confirm_import")}</button>
      </div>
    </Modal>
  );
}

export default function CandidateWorkbench({
  accounts,
  userId,
  enabled = true,
  onPrivateAccessError,
}: {
  accounts: RemoteAccountRead[];
  userId: number;
  enabled?: boolean;
  onPrivateAccessError?: (error: unknown) => void;
}) {
  const t = useT();
  const fmt = useI18nFormat();
  const toast = useToast();
  const qc = useQueryClient();
  const [provider, setProvider] = useState<RemoteDiscoverySource | "">("");
  const [confidence, setConfidence] = useState<DiscoveryConfidence | "">("");
  const [status, setStatus] = useState<DiscoveryCandidateState | "">("");
  const [following, setFollowing] = useState<"" | "true" | "false">("");
  const [local, setLocal] = useState<LocalFilter>("");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [importIds, setImportIds] = useState<string[]>([]);
  const [resolveCandidate, setResolveCandidate] = useState<DiscoveryCandidate | null>(null);
  const [resolveError, setResolveError] = useState<string | null>(null);
  const accountBySource = useMemo(() => new Map(accounts.map((account) => [account.source, account])), [accounts]);
  const accountId = provider ? accountBySource.get(provider)?.id : undefined;
  const effectiveState = status || (local === "conflict" ? "conflict" : undefined);
  const filters = {
    accountId,
    state: effectiveState || undefined,
    confidence: confidence || undefined,
    isFollowing: following === "" ? undefined : following === "true",
    offset: (page - 1) * PAGE_SIZE,
    limit: PAGE_SIZE,
  };
  const candidates = useQuery({
    queryKey: queryKeys.discovery.candidates(userId, filters),
    queryFn: ({ signal }) => api.listDiscoveryCandidates(filters, signal),
    enabled: userId > 0 && enabled,
    placeholderData: (previous) => previous,
    retry: false,
  });

  useLayoutEffect(() => {
    if (candidates.error instanceof ApiError && [401, 403].includes(candidates.error.status)) {
      onPrivateAccessError?.(candidates.error);
    }
  }, [candidates.error, onPrivateAccessError]);

  const rowsInert = candidates.isFetching || candidates.isPlaceholderData || !!candidates.error;
  const visible = useMemo(() => (candidates.error ? [] : candidates.data?.items || []).filter((candidate) => {
    if (provider && candidate.remote_account_id !== accountId) return false;
    const matches = localCreatorIds(candidate).length > 0 || !!candidate.subscription_id;
    if (local === "matched" && !matches) return false;
    if (local === "unmatched" && matches) return false;
    return true;
  }), [accountId, candidates.data?.items, candidates.error, local, provider]);
  const visibleIds = useMemo(() => new Set(visible.map((candidate) => candidate.id)), [visible]);

  useEffect(() => {
    setSelected((current) => new Set([...current].filter((id) => visibleIds.has(id))));
  }, [visibleIds]);

  const resetFilters = () => {
    setPage(1);
    setSelected(new Set());
  };
  const refresh = async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: queryKeys.discovery.all(userId) }),
      qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all }),
    ]);
  };
  const batch = useMutation({
    mutationKey: queryKeys.discovery.mutation(userId, "candidate-batch"),
    mutationFn: (input: { ids: string[]; action: "import" | "dismiss" | "restore"; syncNow?: boolean }) => (
      runPrivateDiscoveryRequest(userId, (signal) => api.batchDiscoveryCandidates(input, signal))
    ),
    onSuccess: async () => {
      setSelected(new Set());
      setImportIds([]);
      await refresh();
      toast.success(t("discovery.batch_succeeded"));
    },
    onError: (error) => {
      onPrivateAccessError?.(error);
      toast.error(safeDiscoveryError(t, error, t("discovery.batch_failed")));
    },
  });
  const resolve = useMutation({
    mutationKey: queryKeys.discovery.mutation(userId, "candidate-resolve"),
    mutationFn: (value: ConflictResolutionValue) => runPrivateDiscoveryRequest(
      userId,
      (signal) => api.resolveDiscoveryCandidate(resolveCandidate!.id, value, signal),
    ),
    onSuccess: async () => {
      setResolveCandidate(null);
      setResolveError(null);
      await refresh();
      toast.success(t("discovery.resolve_succeeded"));
    },
    onError: (error) => {
      onPrivateAccessError?.(error);
      setResolveError(safeDiscoveryError(t, error, t("discovery.resolve_failed")));
    },
  });

  const allVisibleSelected = visible.length > 0 && visible.every((candidate) => selected.has(candidate.id));
  const selectedRows = visible.filter((candidate) => selected.has(candidate.id));
  const selectedPending = selectedRows.filter((candidate) => candidate.state === "pending").map((candidate) => candidate.id);
  const selectedDismissable = selectedRows.filter((candidate) => candidate.state === "pending" || candidate.state === "conflict").map((candidate) => candidate.id);
  const selectedDismissed = selectedRows.filter((candidate) => candidate.state === "dismissed").map((candidate) => candidate.id);

  if (!enabled) return null;

  return (
    <>
      <SectionPanel title={t("discovery.workbench_title")} description={t("discovery.workbench_desc")} className="mt-5">
        <FilterBar
          className="flex-col items-stretch sm:flex-row sm:items-center"
          meta={candidates.data && !candidates.error ? <span className="block w-full text-right text-xs tabular-nums text-muted">{t("common.total")}: {fmt.number(candidates.data.total)}</span> : undefined}
        >
          <label className="grid w-full gap-1 text-xs font-medium text-muted sm:w-auto">
            <span>{t("discovery.filter_provider")}</span>
            <select className="select w-full sm:min-w-36" value={provider} onChange={(event) => { setProvider(event.target.value as typeof provider); resetFilters(); }}>
              <option value="">{t("discovery.filter_all")}</option>
              {accounts.map((account) => <option key={account.id} value={account.source}>{providerLabel(t, account.source)}</option>)}
            </select>
          </label>
          <label className="grid w-full gap-1 text-xs font-medium text-muted sm:w-auto">
            <span>{t("discovery.filter_confidence")}</span>
            <select className="select w-full sm:min-w-32" aria-label={t("discovery.filter_confidence")} value={confidence} onChange={(event) => { setConfidence(event.target.value as typeof confidence); resetFilters(); }}>
              <option value="">{t("discovery.filter_all")}</option>
              <option value="high">{t("discovery.confidence_high")}</option>
              <option value="medium">{t("discovery.confidence_medium")}</option>
              <option value="low">{t("discovery.confidence_low")}</option>
            </select>
          </label>
          <label className="grid w-full gap-1 text-xs font-medium text-muted sm:w-auto">
            <span>{t("discovery.filter_status")}</span>
            <select className="select w-full sm:min-w-32" aria-label={t("discovery.filter_status")} value={status} onChange={(event) => { setStatus(event.target.value as typeof status); resetFilters(); }}>
              <option value="">{t("discovery.filter_all")}</option>
              <option value="pending">{t("discovery.status_pending")}</option>
              <option value="imported">{t("discovery.status_imported")}</option>
              <option value="dismissed">{t("discovery.status_dismissed")}</option>
              <option value="conflict">{t("discovery.status_conflict")}</option>
            </select>
          </label>
          <label className="grid w-full gap-1 text-xs font-medium text-muted sm:w-auto">
            <span>{t("discovery.filter_following")}</span>
            <select className="select w-full sm:min-w-36" value={following} onChange={(event) => { setFollowing(event.target.value as typeof following); resetFilters(); }}>
              <option value="">{t("discovery.filter_all")}</option>
              <option value="true">{t("discovery.filter_following_yes")}</option>
              <option value="false">{t("discovery.filter_following_no")}</option>
            </select>
          </label>
          <label className="grid w-full gap-1 text-xs font-medium text-muted sm:w-auto">
            <span>{t("discovery.filter_local")}</span>
            <select className="select w-full sm:min-w-36" value={local} onChange={(event) => { setLocal(event.target.value as LocalFilter); resetFilters(); }}>
              <option value="">{t("discovery.filter_all")}</option>
              <option value="matched">{t("discovery.filter_local_matched")}</option>
              <option value="unmatched">{t("discovery.filter_local_unmatched")}</option>
              <option value="conflict">{t("discovery.filter_local_conflict")}</option>
            </select>
          </label>
        </FilterBar>

        <SelectionBar
          count={selected.size}
          label={t("discovery.selected_count", { count: selected.size })}
          clearLabel={t("common.clear")}
          onClear={() => setSelected(new Set())}
        >
          {selectedPending.length ? (
            <>
              <button type="button" className="btn-primary" disabled={rowsInert} onClick={() => setImportIds(selectedPending)}>{t("discovery.import_selected")}</button>
              {selectedDismissable.length ? <button type="button" className="btn-ghost" disabled={batch.isPending || rowsInert} onClick={() => batch.mutate({ ids: selectedDismissable, action: "dismiss" })}>{t("discovery.dismiss_selected")}</button> : null}
            </>
          ) : null}
          {!selectedPending.length && selectedDismissable.length ? (
            <button type="button" className="btn-primary" disabled={batch.isPending || rowsInert} onClick={() => batch.mutate({ ids: selectedDismissable, action: "dismiss" })}>{t("discovery.dismiss_selected")}</button>
          ) : null}
          {selectedDismissed.length ? (
            <button type="button" className="btn-primary" disabled={batch.isPending || rowsInert} onClick={() => batch.mutate({ ids: selectedDismissed, action: "restore" })}>{t("discovery.restore_selected")}</button>
          ) : null}
        </SelectionBar>

        {candidates.isLoading ? <TableSkeleton rows={6} /> : null}
        {candidates.error ? (
          <ErrorState message={safeDiscoveryError(t, candidates.error, t("discovery.load_failed"))} onRetry={() => candidates.refetch()} />
        ) : null}
        {candidates.isFetching && !candidates.isLoading ? (
          <p className="mb-3 text-xs font-medium text-accent" role="status">{t("discovery.updating_candidates")}</p>
        ) : null}
        {!candidates.isLoading && !candidates.error && visible.length === 0 ? (
          <EmptyState
            title={t((candidates.data?.items.length || 0) > 0 ? "discovery.no_candidates_page" : "discovery.no_candidates")}
            description={t((candidates.data?.items.length || 0) > 0 ? "discovery.no_candidates_page_desc" : "discovery.no_candidates_desc")}
          />
        ) : null}

        {visible.length ? (
          <>
            <div className="hidden overflow-x-auto rounded-lg border border-border lg:block">
              <table className="w-full min-w-[56rem] text-sm" aria-busy={rowsInert}>
                <thead className="bg-subtle text-xs font-medium text-muted">
                  <tr>
                    <th className="w-12 px-3 py-3 text-left">
                      <input
                        type="checkbox"
                        className="rounded"
                        aria-label={t("discovery.select_visible")}
                        disabled={rowsInert}
                        checked={allVisibleSelected}
                        onChange={(event) => setSelected(event.target.checked ? new Set(visible.map((candidate) => candidate.id)) : new Set())}
                      />
                    </th>
                    <th className="px-3 py-3 text-left">{t("discovery.candidate_identity")}</th>
                    <th className="px-3 py-3 text-left">{t("discovery.provider")}</th>
                    <th className="px-3 py-3 text-left">{t("discovery.confidence")}</th>
                    <th className="px-3 py-3 text-left">{t("discovery.local_match")}</th>
                    <th className="px-3 py-3 text-left">{t("discovery.remote_status")}</th>
                    <th className="px-3 py-3 text-left">{t("discovery.updated")}</th>
                    <th className="w-28 min-w-28 px-3 py-3 text-left">{t("discovery.actions")}</th>
                  </tr>
                </thead>
                <tbody className={`divide-y divide-border ${rowsInert ? "pointer-events-none opacity-60" : ""}`}>
                  {visible.map((candidate) => {
                    const name = candidate.display_name || candidate.source_creator_id;
                    const source = accounts.find((account) => account.id === candidate.remote_account_id)?.source || "pixiv";
                    return (
                      <tr key={candidate.id} className="bg-surface align-top hover:bg-subtle/60">
                        <td className="px-3 py-3"><input type="checkbox" className="rounded" disabled={rowsInert} aria-label={t("discovery.select_candidate", { name })} checked={selected.has(candidate.id)} onChange={() => setSelected((current) => { const next = new Set(current); if (next.has(candidate.id)) next.delete(candidate.id); else next.add(candidate.id); return next; })} /></td>
                        <td className="px-3 py-3"><CandidateIdentity candidate={candidate} /></td>
                        <td className="px-3 py-3"><span className="rounded-md border border-border bg-subtle px-2 py-1 text-xs font-medium text-fg">{providerLabel(t, source)}</span></td>
                        <td className="px-3 py-3"><ConfidenceDetails candidate={candidate} /></td>
                        <td className="px-3 py-3"><LocalMatch candidate={candidate} /></td>
                        <td className="px-3 py-3"><StatusBadge status={candidate.is_following ? "up" : "warning"} label={t(candidate.is_following ? "discovery.following" : "discovery.unfollowed")} /></td>
                        <td className="px-3 py-3 text-xs text-muted"><span className="whitespace-nowrap">{fmt.dateTime(candidate.updated_at)}</span></td>
                        <td className="w-28 min-w-28 px-3 py-3"><RowActions candidate={candidate} pending={rowsInert || batch.isPending || resolve.isPending} onImport={() => setImportIds([candidate.id])} onDismiss={() => batch.mutate({ ids: [candidate.id], action: "dismiss" })} onRestore={() => batch.mutate({ ids: [candidate.id], action: "restore" })} onResolve={() => { setResolveError(null); setResolveCandidate(candidate); }} /></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <div className={`grid gap-3 lg:hidden ${rowsInert ? "pointer-events-none opacity-60" : ""}`} aria-busy={rowsInert}>
              {visible.map((candidate) => {
                const name = candidate.display_name || candidate.source_creator_id;
                const source = accounts.find((account) => account.id === candidate.remote_account_id)?.source || "pixiv";
                return (
                  <article key={candidate.id} className="rounded-lg border border-border bg-surface p-3">
                    <div className="flex items-start gap-3">
                      <input type="checkbox" className="mt-2 rounded" disabled={rowsInert} aria-label={t("discovery.select_candidate", { name })} checked={selected.has(candidate.id)} onChange={() => setSelected((current) => { const next = new Set(current); if (next.has(candidate.id)) next.delete(candidate.id); else next.add(candidate.id); return next; })} />
                      <div className="min-w-0 flex-1"><CandidateIdentity candidate={candidate} /></div>
                    </div>
                    <div className="mt-3 grid grid-cols-2 gap-3 border-y border-border py-3 text-xs">
                      <div><p className="mb-1 text-muted">{t("discovery.provider")}</p><p className="font-medium text-fg">{providerLabel(t, source)}</p></div>
                      <div><p className="mb-1 text-muted">{t("discovery.remote_status")}</p><p className="font-medium text-fg">{t(candidate.is_following ? "discovery.following" : "discovery.unfollowed")}</p></div>
                      <div className="col-span-2"><ConfidenceDetails candidate={candidate} /></div>
                      <div className="col-span-2"><LocalMatch candidate={candidate} /></div>
                    </div>
                    <div className="mt-3 flex min-w-0 flex-wrap items-center justify-between gap-2">
                      <span className="text-xs text-muted">{fmt.dateTime(candidate.updated_at)}</span>
                      <RowActions candidate={candidate} pending={rowsInert || batch.isPending || resolve.isPending} onImport={() => setImportIds([candidate.id])} onDismiss={() => batch.mutate({ ids: [candidate.id], action: "dismiss" })} onRestore={() => batch.mutate({ ids: [candidate.id], action: "restore" })} onResolve={() => { setResolveError(null); setResolveCandidate(candidate); }} />
                    </div>
                  </article>
                );
              })}
            </div>
          </>
        ) : null}
        {!candidates.error && (candidates.data?.total || 0) > PAGE_SIZE ? (
          <Pagination page={page} pageSize={PAGE_SIZE} total={candidates.data?.total || 0} onPageChange={(next) => { setPage(next); setSelected(new Set()); }} />
        ) : null}
      </SectionPanel>

      <ImportDialog count={importIds.length} open={importIds.length > 0} pending={batch.isPending} onClose={() => setImportIds([])} onConfirm={(syncNow) => batch.mutate({ ids: importIds, action: "import", syncNow })} />
      <ConflictResolutionDialog candidate={resolveCandidate} open={!!resolveCandidate} pending={resolve.isPending} error={resolveError} onClose={() => { setResolveCandidate(null); setResolveError(null); }} onResolve={(value) => resolve.mutate(value)} />
    </>
  );
}
