"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";

import { ErrorState, PageHeader, PageShell, TableSkeleton, useToast } from "@/components";
import { ApiError, api, queryKeys, type RemoteAccountRead } from "@/lib/api";
import { adminRoutes } from "@/lib/adminRoutes";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { clearPrivateDiscoveryCache, runPrivateDiscoveryRequest } from "@/lib/remoteDiscoveryPrivateCache";
import CandidateWorkbench from "./CandidateWorkbench";
import RemoteAccountPanel from "./RemoteAccountPanel";
import { safeDiscoveryError } from "./discoveryPresentation";

const ACTIVE_SCAN_STATES = new Set(["enqueued", "running", "recovering", "waiting"]);

function consumeOAuthSecretsFromDocument(state: string, code: string) {
  window.history.replaceState(null, "", adminRoutes.discovery);
  for (const script of Array.from(document.scripts)) {
    const source = script.textContent || "";
    if (source.includes(state) || source.includes(code)) script.remove();
  }
}

export default function RemoteDiscoveryPage() {
  const router = useRouter();
  const t = useT();
  const toast = useToast();
  const qc = useQueryClient();
  const { user } = useAuth();
  const userId = user?.id || 0;
  const oauthAttempted = useRef(false);
  const [pendingScanId, setPendingScanId] = useState<string | null>(null);
  const [privateAccessError, setPrivateAccessError] = useState<ApiError | null>(null);

  const handlePrivateAccessError = useCallback((error: unknown) => {
    if (!(error instanceof ApiError) || (error.status !== 401 && error.status !== 403)) return;
    clearPrivateDiscoveryCache(qc, userId);
    setPrivateAccessError(error);
  }, [qc, userId]);

  const accounts = useQuery({
    queryKey: queryKeys.remoteAccounts.all(userId),
    queryFn: ({ signal }) => api.listRemoteAccounts(0, 50, signal),
    enabled: userId > 0 && !privateAccessError,
    retry: false,
  });
  const providers = useQuery({ queryKey: queryKeys.sources, queryFn: api.sources, retry: false });
  const scans = useQuery({
    queryKey: queryKeys.discovery.scans(userId),
    queryFn: ({ signal }) => api.listDiscoveryScans(undefined, 0, 50, signal),
    enabled: userId > 0 && !privateAccessError,
    retry: false,
    refetchInterval: (query) => query.state.data?.items.some((scan) => ACTIVE_SCAN_STATES.has(scan.status)) ? 2000 : false,
  });

  useLayoutEffect(() => {
    handlePrivateAccessError(accounts.error);
    handlePrivateAccessError(scans.error);
  }, [accounts.error, handlePrivateAccessError, scans.error]);

  useLayoutEffect(() => {
    if (oauthAttempted.current || userId <= 0) return;
    const callbackUrl = new URL(window.location.href);
    const outcome = callbackUrl.searchParams.get("oauth");
    let callbackState = callbackUrl.searchParams.get("state");
    let callbackCode = callbackUrl.searchParams.get("code");
    if (!outcome && (!callbackState || !callbackCode)) return;
    oauthAttempted.current = true;
    if (callbackState && callbackCode) consumeOAuthSecretsFromDocument(callbackState, callbackCode);
    else window.history.replaceState(null, "", adminRoutes.discovery);
    // replaceState removes the public URL synchronously. The router replacement also
    // rebuilds Next's private history tree so it cannot retain the callback secrets.
    router.replace(adminRoutes.discovery, { scroll: false });
    if (outcome) {
      if (outcome === "success") toast.success(t("discovery.oauth_succeeded"));
      if (outcome === "error") toast.error(t("discovery.oauth_failed"));
      callbackState = null;
      callbackCode = null;
      return;
    }
    const callbackPromise = runPrivateDiscoveryRequest(userId, (signal) => (
      api.completeXOAuth(callbackState!, callbackCode!, signal)
    ));
    callbackState = null;
    callbackCode = null;
    void callbackPromise.then(async () => {
      await qc.invalidateQueries({ queryKey: queryKeys.remoteAccounts.all(userId) });
      toast.success(t("discovery.oauth_succeeded"));
    }).catch(() => toast.error(t("discovery.oauth_failed")));
  }, [qc, router, t, toast, userId]);

  const scan = useMutation({
    mutationKey: queryKeys.discovery.mutation(userId, "scan"),
    mutationFn: (account: RemoteAccountRead) => runPrivateDiscoveryRequest(userId, (signal) => api.createDiscoveryScan(account.id, signal)),
    onSuccess: async (task) => {
      setPendingScanId(task.id);
      await qc.invalidateQueries({ queryKey: queryKeys.discovery.scans(userId) });
      toast.info(t("discovery.scan_queued"));
    },
    onError: (error) => {
      handlePrivateAccessError(error);
      toast.error(safeDiscoveryError(t, error, t("discovery.scan_action_failed")));
    },
  });

  useEffect(() => {
    if (!pendingScanId || !scans.data) return;
    const task = scans.data.items.find((item) => item.id === pendingScanId);
    if (!task || ACTIVE_SCAN_STATES.has(task.status)) return;
    setPendingScanId(null);
    qc.invalidateQueries({ queryKey: queryKeys.discovery.all(userId) });
    if (task.status === "complete") toast.success(t("discovery.scan_complete"));
    else toast.error(t("discovery.scan_failed"));
  }, [pendingScanId, qc, scans.data, t, toast]);

  const accountError = privateAccessError || accounts.error || providers.error || scans.error;
  const previewEnabledAccountIds = useMemo(() => {
    const enabledSources = new Set((providers.data?.sources || [])
      .filter((provider) => provider.capabilities.remote_discovery_rollout?.manual_preview === true)
      .map((provider) => provider.source_name));
    return new Set((accounts.data || [])
      .filter((account) => enabledSources.has(account.source))
      .map((account) => account.id));
  }, [accounts.data, providers.data?.sources]);
  return (
    <PageShell className="max-w-[96rem]">
      <PageHeader title={t("discovery.title")} description={t("discovery.desc")} />
      {accounts.isLoading || providers.isLoading ? <TableSkeleton rows={3} /> : null}
      {accountError ? (
        <ErrorState
          message={safeDiscoveryError(t, accountError, t("discovery.load_failed"))}
          onRetry={() => {
            if (privateAccessError) {
              setPrivateAccessError(null);
              return;
            }
            accounts.refetch();
            providers.refetch();
            scans.refetch();
          }}
        />
      ) : null}
      {!accounts.isLoading && !providers.isLoading && !accountError ? (
        <RemoteAccountPanel
          accounts={accounts.data || []}
          userId={userId}
          providers={providers.data?.sources || []}
          scans={scans.data?.items || []}
          onScan={(account) => scan.mutate(account)}
          scanPending={scan.isPending}
          onPrivateAccessError={handlePrivateAccessError}
        />
      ) : null}
      <CandidateWorkbench
        accounts={accounts.data || []}
        previewEnabledAccountIds={previewEnabledAccountIds}
        userId={userId}
        enabled={!privateAccessError}
        onPrivateAccessError={handlePrivateAccessError}
      />
    </PageShell>
  );
}
