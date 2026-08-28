"use client";

import { useEffect, useId, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, FlaskConical, KeyRound, Radar, Settings2, ShieldCheck, Trash2 } from "lucide-react";

import { ConfirmDialog, Modal, SectionPanel, StatusBadge, useToast } from "@/components";
import {
  api,
  queryKeys,
  type ProviderInfo,
  type RemoteAccountRead,
  type RemoteAuthMethod,
  type RemoteCollection,
  type RemoteDiscoverySource,
  type TaskRun,
} from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";
import { DISCOVERY_SOURCES, providerLabel, safeDiscoveryError } from "./discoveryPresentation";

type DialogKind = "connect" | "reconnect" | "settings" | null;

function validXAuthorizationUrl(value: string) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && (url.hostname === "x.com" || url.hostname === "twitter.com");
  } catch {
    return false;
  }
}

function CredentialsDialog({
  open,
  source,
  account,
  onClose,
}: {
  open: boolean;
  source: RemoteDiscoverySource;
  account?: RemoteAccountRead;
  onClose: () => void;
}) {
  const t = useT();
  const toast = useToast();
  const qc = useQueryClient();
  const fieldId = useId();
  const methodId = useId();
  const [credential, setCredential] = useState("");
  const [xMethod, setXMethod] = useState<"oauth2" | "cookie">("oauth2");
  const [feedback, setFeedback] = useState<string | null>(null);
  const reconnecting = !!account;

  useEffect(() => {
    if (!open) {
      setCredential("");
      setFeedback(null);
      return;
    }
    setCredential("");
    setFeedback(null);
    setXMethod(account?.auth_method === "cookie" ? "cookie" : "oauth2");
  }, [account?.auth_method, open]);

  const connect = useMutation({
    mutationFn: async () => {
      const authMethod: RemoteAuthMethod = source === "pixiv"
        ? "refresh_token"
        : source === "bilibili"
          ? "sessdata"
          : "cookie";
      const credentials: Record<string, string> = source === "pixiv"
        ? { refresh_token: credential }
        : source === "bilibili"
          ? { SESSDATA: credential }
          : { cookie: credential };
      if (account) {
        return api.updateRemoteAccount(account.id, { auth_method: authMethod, credentials });
      }
      return api.createRemoteAccount({ source, auth_method: authMethod, credentials });
    },
    onSuccess: async () => {
      setCredential("");
      await qc.invalidateQueries({ queryKey: queryKeys.remoteAccounts.all });
      toast.success(t("discovery.account_connected"));
      onClose();
    },
    onError: (error) => setFeedback(safeDiscoveryError(t, error, t("discovery.connection_failed"))),
  });

  const oauth = useMutation({
    mutationFn: () => api.authorizeXOAuth(account?.id),
    onSuccess: (result) => {
      if (!validXAuthorizationUrl(result.authorization_url)) {
        setFeedback(t("discovery.invalid_oauth_redirect"));
        return;
      }
      setCredential("");
      window.location.assign(result.authorization_url);
    },
    onError: (error) => setFeedback(safeDiscoveryError(t, error, t("discovery.oauth_failed"))),
  });

  const provider = providerLabel(t, source);
  const isOAuth = source === "x" && xMethod === "oauth2";
  const label = source === "pixiv"
    ? t("discovery.pixiv_refresh_token")
    : source === "bilibili"
      ? t("discovery.bilibili_sessdata")
      : t("discovery.x_cookie_label");

  return (
    <Modal
      open={open}
      onClose={() => {
        setCredential("");
        onClose();
      }}
      title={t(reconnecting ? "discovery.reconnect_title" : "discovery.connect_title", { provider })}
    >
      <div className="space-y-4">
        {source === "x" ? (
          <div>
            <label htmlFor={methodId} className="mb-1.5 block text-sm font-medium text-fg">
              {t("discovery.x_auth_method")}
            </label>
            <select
              id={methodId}
              className="select w-full"
              value={xMethod}
              onChange={(event) => {
                setCredential("");
                setXMethod(event.target.value as "oauth2" | "cookie");
              }}
            >
              <option value="oauth2">{t("discovery.x_oauth")}</option>
              <option value="cookie">{t("discovery.x_cookie")}</option>
            </select>
          </div>
        ) : null}

        {isOAuth ? (
          <div className="rounded-lg border border-border bg-subtle p-4">
            <p className="text-sm leading-5 text-muted">{t("discovery.oauth_help")}</p>
            <button
              type="button"
              className="btn-primary mt-4 w-full justify-center"
              onClick={() => oauth.mutate()}
              disabled={oauth.isPending}
            >
              <ExternalLink aria-hidden="true" className="h-4 w-4" />
              {t("discovery.start_oauth")}
            </button>
          </div>
        ) : (
          <div>
            <label htmlFor={fieldId} className="mb-1.5 block text-sm font-medium text-fg">{label}</label>
            <input
              id={fieldId}
              className="input w-full font-mono"
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={credential}
              onChange={(event) => setCredential(event.target.value)}
            />
            <p className="mt-1.5 text-xs text-muted">{t("discovery.credential_help")}</p>
          </div>
        )}

        {feedback ? <p role="alert" className="rounded-md border border-danger/30 bg-danger-subtle p-3 text-sm text-danger">{feedback}</p> : null}
        {!isOAuth ? (
          <div className="flex flex-wrap justify-end gap-2 pt-1">
            <button type="button" className="btn-ghost" onClick={onClose}>{t("common.cancel")}</button>
            <button
              type="button"
              className="btn-primary"
              disabled={!credential.trim() || connect.isPending}
              onClick={() => connect.mutate()}
            >
              <KeyRound aria-hidden="true" className="h-4 w-4" />
              {t(reconnecting ? "discovery.reconnect_account" : "discovery.connect_account")}
            </button>
          </div>
        ) : null}
      </div>
    </Modal>
  );
}

function sameSelector(left: Record<string, unknown>, right: Record<string, unknown>) {
  return JSON.stringify(left, Object.keys(left).sort()) === JSON.stringify(right, Object.keys(right).sort());
}

function collectionLabel(t: ReturnType<typeof useT>, collection: RemoteCollection) {
  if (["public", "private", "all"].includes(collection.id)) {
    return t(`discovery.collection_${collection.id}`);
  }
  return collection.name;
}

function AccountSettingsDialog({
  open,
  account,
  onClose,
}: {
  open: boolean;
  account: RemoteAccountRead;
  onClose: () => void;
}) {
  const t = useT();
  const toast = useToast();
  const qc = useQueryClient();
  const [interval, setInterval] = useState(account.scan_interval_hours);
  const [autoImport, setAutoImport] = useState(account.auto_import_enabled);
  const [threshold, setThreshold] = useState(account.auto_import_min_confidence);
  const [limit, setLimit] = useState(account.auto_import_limit);
  const [selectedCollections, setSelectedCollections] = useState<Set<string>>(new Set());
  const [feedback, setFeedback] = useState<string | null>(null);

  const collections = useQuery({
    queryKey: queryKeys.remoteAccounts.collections(account.id),
    queryFn: () => api.listRemoteCollections(account.id),
    enabled: open && account.has_credentials,
    retry: false,
  });

  useEffect(() => {
    if (!open) return;
    setInterval(account.scan_interval_hours);
    setAutoImport(account.auto_import_enabled);
    setThreshold(account.auto_import_min_confidence);
    setLimit(account.auto_import_limit);
    setFeedback(null);
  }, [account, open]);

  useEffect(() => {
    if (!open || !collections.data) return;
    setSelectedCollections(new Set(collections.data
      .filter((collection) => account.collection_selectors.some((selector) => sameSelector(collection.selector, selector)))
      .map((collection) => collection.id)));
  }, [account.collection_selectors, collections.data, open]);

  const save = useMutation({
    mutationFn: () => api.updateRemoteAccount(account.id, {
      collection_selectors: (collections.data || [])
        .filter((collection) => selectedCollections.has(collection.id))
        .map((collection) => collection.selector),
      scan_interval_hours: Math.max(1, Math.floor(interval)),
      auto_import_enabled: autoImport,
      auto_import_min_confidence: threshold,
      auto_import_limit: Math.min(200, Math.max(1, Math.floor(limit))),
    }),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: queryKeys.remoteAccounts.all });
      toast.success(t("discovery.settings_saved"));
      onClose();
    },
    onError: (error) => setFeedback(safeDiscoveryError(t, error, t("discovery.settings_failed"))),
  });

  const provider = providerLabel(t, account.source);
  return (
    <Modal open={open} onClose={onClose} title={t("discovery.settings_title", { provider })}>
      <div className="space-y-5">
        <fieldset>
          <legend className="mb-2 text-sm font-medium text-fg">{t("discovery.collections")}</legend>
          {collections.isLoading ? <p className="text-sm text-muted">{t("discovery.collections_loading")}</p> : null}
          {!collections.isLoading && (collections.data?.length || 0) === 0 ? (
            <p className="text-sm text-muted">{t("discovery.collections_empty")}</p>
          ) : null}
          <div className="grid gap-2 sm:grid-cols-2">
            {(collections.data || []).map((collection: RemoteCollection) => (
              <label key={collection.id} className="flex min-h-11 cursor-pointer items-center gap-2 rounded-md border border-border px-3 text-sm text-fg hover:bg-subtle">
                <input
                  type="checkbox"
                  className="rounded"
                  checked={selectedCollections.has(collection.id)}
                  onChange={(event) => setSelectedCollections((current) => {
                    const next = new Set(current);
                    if (event.target.checked) next.add(collection.id); else next.delete(collection.id);
                    return next;
                  })}
                />
                <span>{collectionLabel(t, collection)}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <div className="grid gap-4 sm:grid-cols-2">
          <label className="text-sm font-medium text-fg">
            <span className="mb-1.5 block">{t("discovery.scan_interval")}</span>
            <input className="input w-full" type="number" min={1} value={interval} onChange={(event) => setInterval(Number(event.target.value))} />
          </label>
          <label className="text-sm font-medium text-fg">
            <span className="mb-1.5 block">{t("discovery.max_imports")}</span>
            <input className="input w-full" type="number" min={1} max={200} value={limit} onChange={(event) => setLimit(Number(event.target.value))} />
          </label>
          <label className="text-sm font-medium text-fg">
            <span className="mb-1.5 block">{t("discovery.minimum_confidence")}</span>
            <select className="select w-full" value={threshold} onChange={(event) => setThreshold(event.target.value as typeof threshold)}>
              <option value="high">{t("discovery.confidence_high_only")}</option>
              <option value="medium">{t("discovery.confidence_high_medium")}</option>
              <option value="low">{t("discovery.confidence_all")}</option>
            </select>
          </label>
          <label className="flex min-h-11 cursor-pointer items-center gap-2 self-end rounded-md border border-border px-3 text-sm font-medium text-fg">
            <input type="checkbox" className="rounded" checked={autoImport} onChange={(event) => setAutoImport(event.target.checked)} />
            <span>{t("discovery.auto_import")}</span>
          </label>
        </div>
        {feedback ? <p role="alert" className="rounded-md border border-danger/30 bg-danger-subtle p-3 text-sm text-danger">{feedback}</p> : null}
        <div className="flex flex-wrap justify-end gap-2">
          <button type="button" className="btn-ghost" onClick={onClose}>{t("common.cancel")}</button>
          <button type="button" className="btn-primary" disabled={save.isPending} onClick={() => save.mutate()}>
            {t("discovery.save_settings")}
          </button>
        </div>
      </div>
    </Modal>
  );
}

function AccountCard({
  source,
  provider,
  account,
  scan,
  onDialog,
  onTest,
  onScan,
  onDelete,
  pending,
}: {
  source: RemoteDiscoverySource;
  provider?: ProviderInfo;
  account?: RemoteAccountRead;
  scan?: TaskRun;
  onDialog: (kind: Exclude<DialogKind, null>) => void;
  onTest: () => void;
  onScan: () => void;
  onDelete: () => void;
  pending: boolean;
}) {
  const t = useT();
  const fmt = useI18nFormat();
  const label = providerLabel(t, source);
  const experimental = source !== "x" && provider?.capabilities.supports_remote_discovery;
  const activeScan = scan && ["enqueued", "running", "recovering", "waiting"].includes(scan.status);
  const scanState = scan?.status === "complete"
    ? t("discovery.scan_complete")
    : scan?.status === "failed"
      ? t("discovery.scan_failed")
      : activeScan
        ? t("discovery.scan_in_progress")
        : null;
  const authTone = account?.auth_status === "healthy" ? "up" : account?.auth_status ? "warning" : "unknown";
  const authLabel = account?.auth_status === "healthy"
    ? t("discovery.auth_healthy")
    : account?.auth_status
      ? t("discovery.auth_requires_attention")
      : t("discovery.auth_untested");
  const progressCurrent = typeof scan?.progress_current === "number"
    ? scan.progress_current
    : typeof scan?.progress_data?.current === "number"
      ? scan.progress_data.current
      : typeof scan?.progress_data?.selector_index === "number"
        ? scan.progress_data.selector_index
        : null;
  const progressTotal = typeof scan?.progress_total === "number"
    ? scan.progress_total
    : typeof scan?.progress_data?.total === "number"
      ? scan.progress_data.total
      : typeof scan?.progress_data?.selector_count === "number"
        ? scan.progress_data.selector_count
        : null;
  const candidatesSeen = typeof scan?.progress_data?.candidates_seen === "number" ? scan.progress_data.candidates_seen : 0;
  const progressPercent = progressCurrent !== null && progressTotal && progressTotal > 0
    ? Math.min(100, Math.max(0, (progressCurrent / progressTotal) * 100))
    : null;

  return (
    <article aria-label={t("discovery.provider_account", { provider: label })} className="rounded-lg border border-border bg-surface p-4">
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-border bg-subtle text-sm font-semibold text-accent">
            {source === "bilibili" ? "B" : source === "pixiv" ? "P" : "X"}
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-base font-semibold text-fg">{label}</h3>
              {experimental ? (
                <span className="inline-flex items-center gap-1 rounded-full border border-accent/30 bg-accent-subtle px-2 py-0.5 text-xs font-medium text-accent">
                  <FlaskConical aria-hidden="true" className="h-3 w-3" />
                  {t("discovery.experimental")}
                </span>
              ) : null}
            </div>
            <p className="mt-0.5 truncate text-sm text-muted">
              {account ? (account.remote_username || account.remote_user_id || t("discovery.credentials_masked")) : t("discovery.not_connected")}
            </p>
          </div>
        </div>
        {account ? <StatusBadge status={authTone} label={authLabel} /> : null}
      </div>

      {!account ? (
        <div className="mt-5">
          <p className="text-sm leading-5 text-muted">{t("discovery.not_connected_desc")}</p>
          <button type="button" className="btn-primary mt-4" onClick={() => onDialog("connect")}>
            <KeyRound aria-hidden="true" className="h-4 w-4" />
            {t("discovery.connect_provider", { provider: label })}
          </button>
        </div>
      ) : (
        <>
          <dl className="mt-5 grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-2 border-y border-border py-3 text-xs">
            <dt className="text-muted">{t("discovery.credentials")}</dt>
            <dd className="text-right font-medium text-fg">{t("discovery.credentials_masked")}</dd>
            <dt className="text-muted">{t("discovery.last_scan")}</dt>
            <dd className="text-right text-fg">{account.last_scan_completed_at ? fmt.relative(account.last_scan_completed_at) : t("discovery.never_scanned")}</dd>
            <dt className="text-muted">{t("discovery.next_scan")}</dt>
            <dd className="text-right text-fg">{fmt.relative(account.next_scan_at)}</dd>
          </dl>
          <p className="mt-3 text-xs text-muted">
            {account.auto_import_enabled
              ? t("discovery.auto_import_summary", {
                  threshold: t(`discovery.confidence_${account.auto_import_min_confidence}`),
                  limit: account.auto_import_limit,
                })
              : t("discovery.auto_import_off")}
          </p>
          {scanState ? (
            <div className="mt-3 text-xs font-medium text-accent" role="status">
              <div className="flex items-center gap-2">
                <Radar aria-hidden="true" className={`h-4 w-4 ${activeScan ? "animate-pulse" : ""}`} />
                {scanState}
              </div>
              {activeScan ? (
                <div className="mt-2">
                  <div
                    className="h-1.5 overflow-hidden rounded-full bg-border"
                    role={progressPercent !== null ? "progressbar" : undefined}
                    aria-label={progressPercent !== null ? scanState : undefined}
                    aria-valuemin={progressPercent !== null ? 0 : undefined}
                    aria-valuemax={progressPercent !== null ? 100 : undefined}
                    aria-valuenow={progressPercent !== null ? Math.round(progressPercent) : undefined}
                  >
                    <div
                      className={`h-full w-full rounded-full bg-accent ${progressPercent === null ? "animate-pulse" : "transition-transform duration-slow ease-out"}`}
                      style={{ transform: `scaleX(${progressPercent === null ? 0.6 : progressPercent / 100})`, transformOrigin: "left" }}
                    />
                  </div>
                  {progressCurrent !== null && progressTotal !== null ? (
                    <p className="mt-1 text-[11px] font-normal text-muted">
                      {t("discovery.scan_progress", { current: progressCurrent, total: progressTotal, candidates: candidatesSeen })}
                    </p>
                  ) : null}
                </div>
              ) : null}
            </div>
          ) : null}
          <div className="mt-4 flex flex-wrap gap-2">
            <button type="button" className="btn-primary" disabled={pending || !!activeScan || account.auth_status !== "healthy"} onClick={onScan} aria-label={t("discovery.scan_provider", { provider: label })}>
              <Radar aria-hidden="true" className="h-4 w-4" />
              {t("discovery.scan_provider", { provider: label })}
            </button>
            <button type="button" className="btn-ghost" disabled={pending} onClick={() => onDialog("settings")} aria-label={t("discovery.configure_provider", { provider: label })}>
              <Settings2 aria-hidden="true" className="h-4 w-4" />
              {t("discovery.configure_provider", { provider: label })}
            </button>
            <button type="button" className="btn-ghost" disabled={pending} onClick={onTest} aria-label={t("discovery.test_provider", { provider: label })} title={t("discovery.test_provider", { provider: label })}>
              <ShieldCheck aria-hidden="true" className="h-4 w-4" />
            </button>
            <button type="button" className="btn-ghost" disabled={pending} onClick={() => onDialog("reconnect")} aria-label={t("discovery.reconnect_provider", { provider: label })} title={t("discovery.reconnect_provider", { provider: label })}>
              <KeyRound aria-hidden="true" className="h-4 w-4" />
            </button>
            <button type="button" className="btn-ghost text-danger" disabled={pending} onClick={onDelete} aria-label={t("discovery.delete_provider", { provider: label })} title={t("discovery.delete_provider", { provider: label })}>
              <Trash2 aria-hidden="true" className="h-4 w-4" />
            </button>
          </div>
        </>
      )}
    </article>
  );
}

export default function RemoteAccountPanel({
  accounts,
  providers,
  scans,
  onScan,
  scanPending,
}: {
  accounts: RemoteAccountRead[];
  providers: ProviderInfo[];
  scans: TaskRun[];
  onScan: (account: RemoteAccountRead) => void;
  scanPending: boolean;
}) {
  const t = useT();
  const toast = useToast();
  const qc = useQueryClient();
  const [dialog, setDialog] = useState<{ source: RemoteDiscoverySource; kind: DialogKind } | null>(null);
  const [deleteAccount, setDeleteAccount] = useState<RemoteAccountRead | null>(null);
  const providersBySource = useMemo(() => new Map(providers.map((provider) => [provider.source_name, provider])), [providers]);
  const accountsBySource = useMemo(() => new Map(accounts.map((account) => [account.source, account])), [accounts]);
  const latestScanByAccount = useMemo(() => {
    const result = new Map<string, TaskRun>();
    for (const scan of scans) {
      const accountId = (scan as TaskRun & { triggering_remote_account_id?: string | null }).triggering_remote_account_id;
      if (accountId && !result.has(accountId)) result.set(accountId, scan);
    }
    return result;
  }, [scans]);

  const testAccount = useMutation({
    mutationFn: (account: RemoteAccountRead) => api.testRemoteAccount(account.id),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: queryKeys.remoteAccounts.all });
      toast.success(t("discovery.test_succeeded"));
    },
    onError: (error) => toast.error(safeDiscoveryError(t, error, t("discovery.test_failed"))),
  });
  const removeAccount = useMutation({
    mutationFn: (account: RemoteAccountRead) => api.deleteRemoteAccount(account.id),
    onSuccess: async () => {
      setDeleteAccount(null);
      await Promise.all([
        qc.invalidateQueries({ queryKey: queryKeys.remoteAccounts.all }),
        qc.invalidateQueries({ queryKey: queryKeys.discovery.all }),
      ]);
      toast.success(t("discovery.account_deleted"));
    },
    onError: (error) => toast.error(safeDiscoveryError(t, error, t("discovery.delete_failed"))),
  });

  const activeSource = dialog?.source;
  const activeAccount = activeSource ? accountsBySource.get(activeSource) : undefined;
  return (
    <>
      <SectionPanel title={t("discovery.accounts_title")} description={t("discovery.accounts_desc")}>
        <div className="grid gap-4 xl:grid-cols-3">
          {DISCOVERY_SOURCES.map((source) => {
            const account = accountsBySource.get(source);
            return (
              <AccountCard
                key={source}
                source={source}
                provider={providersBySource.get(source)}
                account={account}
                scan={account ? latestScanByAccount.get(account.id) : undefined}
                onDialog={(kind) => setDialog({ source, kind })}
                onTest={() => account && testAccount.mutate(account)}
                onScan={() => account && onScan(account)}
                onDelete={() => account && setDeleteAccount(account)}
                pending={testAccount.isPending || removeAccount.isPending || scanPending}
              />
            );
          })}
        </div>
      </SectionPanel>

      {activeSource && (dialog?.kind === "connect" || dialog?.kind === "reconnect") ? (
        <CredentialsDialog
          open
          source={activeSource}
          account={dialog.kind === "reconnect" ? activeAccount : undefined}
          onClose={() => setDialog(null)}
        />
      ) : null}
      {activeAccount && dialog?.kind === "settings" ? (
        <AccountSettingsDialog open account={activeAccount} onClose={() => setDialog(null)} />
      ) : null}
      <ConfirmDialog
        open={!!deleteAccount}
        title={t("discovery.delete_title", { provider: deleteAccount ? providerLabel(t, deleteAccount.source) : "" })}
        message={t("discovery.delete_message")}
        onCancel={() => setDeleteAccount(null)}
        onConfirm={() => deleteAccount && removeAccount.mutate(deleteAccount)}
        isPending={removeAccount.isPending}
      />
    </>
  );
}
