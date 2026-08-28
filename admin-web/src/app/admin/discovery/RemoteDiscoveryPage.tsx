"use client";

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";

import { ErrorState, PageHeader, PageShell, TableSkeleton, useToast } from "@/components";
import { api, queryKeys, type RemoteAccountRead } from "@/lib/api";
import { adminRoutes } from "@/lib/adminRoutes";
import { useT } from "@/lib/i18n";
import CandidateWorkbench from "./CandidateWorkbench";
import RemoteAccountPanel from "./RemoteAccountPanel";
import { safeDiscoveryError } from "./discoveryPresentation";

const ACTIVE_SCAN_STATES = new Set(["enqueued", "running", "recovering", "waiting"]);

export default function RemoteDiscoveryPage() {
  const t = useT();
  const toast = useToast();
  const qc = useQueryClient();
  const router = useRouter();
  const searchParams = useSearchParams();
  const oauthAttempt = useRef<string | null>(null);
  const oauthNotice = useRef<string | null>(null);
  const [pendingScanId, setPendingScanId] = useState<string | null>(null);

  const accounts = useQuery({
    queryKey: queryKeys.remoteAccounts.all,
    queryFn: () => api.listRemoteAccounts(),
    retry: false,
  });
  const providers = useQuery({ queryKey: queryKeys.sources, queryFn: api.sources, retry: false });
  const scans = useQuery({
    queryKey: queryKeys.discovery.scans(),
    queryFn: () => api.listDiscoveryScans(),
    retry: false,
    refetchInterval: (query) => query.state.data?.items.some((scan) => ACTIVE_SCAN_STATES.has(scan.status)) ? 2000 : false,
  });

  const oauthCallback = useMutation({
    mutationFn: ({ state, code }: { state: string; code: string }) => api.completeXOAuth(state, code),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: queryKeys.remoteAccounts.all });
      toast.success(t("discovery.oauth_succeeded"));
      router.replace(adminRoutes.discovery, { scroll: false });
    },
    onError: () => {
      toast.error(t("discovery.oauth_failed"));
      router.replace(adminRoutes.discovery, { scroll: false });
    },
  });

  useEffect(() => {
    const state = searchParams.get("state");
    const code = searchParams.get("code");
    if (!state || !code) return;
    const marker = `${state}:${code}`;
    if (oauthAttempt.current === marker) return;
    oauthAttempt.current = marker;
    oauthCallback.mutate({ state, code });
  }, [searchParams]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const outcome = searchParams.get("oauth");
    if (!outcome || oauthNotice.current === outcome) return;
    oauthNotice.current = outcome;
    if (outcome === "success") toast.success(t("discovery.oauth_succeeded"));
    if (outcome === "error") toast.error(t("discovery.oauth_failed"));
    router.replace(adminRoutes.discovery, { scroll: false });
  }, [router, searchParams, t, toast]);

  const scan = useMutation({
    mutationFn: (account: RemoteAccountRead) => api.createDiscoveryScan(account.id),
    onSuccess: async (task) => {
      setPendingScanId(task.id);
      await qc.invalidateQueries({ queryKey: queryKeys.discovery.scans() });
      toast.info(t("discovery.scan_queued"));
    },
    onError: (error) => toast.error(safeDiscoveryError(t, error, t("discovery.scan_action_failed"))),
  });

  useEffect(() => {
    if (!pendingScanId || !scans.data) return;
    const task = scans.data.items.find((item) => item.id === pendingScanId);
    if (!task || ACTIVE_SCAN_STATES.has(task.status)) return;
    setPendingScanId(null);
    qc.invalidateQueries({ queryKey: queryKeys.discovery.all });
    if (task.status === "complete") toast.success(t("discovery.scan_complete"));
    else toast.error(t("discovery.scan_failed"));
  }, [pendingScanId, qc, scans.data, t, toast]);

  const accountError = accounts.error || providers.error || scans.error;
  return (
    <PageShell className="max-w-[96rem]">
      <PageHeader title={t("discovery.title")} description={t("discovery.desc")} />
      {accounts.isLoading || providers.isLoading ? <TableSkeleton rows={3} /> : null}
      {accountError ? (
        <ErrorState
          message={safeDiscoveryError(t, accountError, t("discovery.load_failed"))}
          onRetry={() => {
            accounts.refetch();
            providers.refetch();
            scans.refetch();
          }}
        />
      ) : null}
      {!accounts.isLoading && !providers.isLoading && !accountError ? (
        <RemoteAccountPanel
          accounts={accounts.data || []}
          providers={providers.data?.sources || []}
          scans={scans.data?.items || []}
          onScan={(account) => scan.mutate(account)}
          scanPending={scan.isPending}
        />
      ) : null}
      <CandidateWorkbench accounts={accounts.data || []} />
    </PageShell>
  );
}
