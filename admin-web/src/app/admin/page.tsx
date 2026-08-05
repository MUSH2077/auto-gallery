"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ErrorState, PageShell, useToast } from "@/components";
import {
  ActivityPanel,
  AttentionBanner,
  DashboardStatusStrip,
  RecentWorksPanel,
  ServicesPanel,
  type DashboardActivity,
} from "@/components/dashboard/DashboardWorkbench";
import { api, queryKeys } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { usePermissions } from "@/lib/usePermissions";

function DashboardSkeleton() {
  return (
    <div className="space-y-5" aria-hidden>
      <div className="h-56 animate-pulse rounded-lg border border-border bg-surface md:h-64 xl:h-28" />
      <div className="grid gap-5 xl:grid-cols-[minmax(0,1.05fr)_minmax(0,.95fr)]">
        <div className="h-[26rem] animate-pulse rounded-lg border border-border bg-surface" />
        <div className="h-[26rem] animate-pulse rounded-lg border border-border bg-surface" />
      </div>
      <div className="h-28 animate-pulse rounded-lg border border-border bg-surface" />
    </div>
  );
}

export default function Dashboard() {
  const t = useT();
  const toast = useToast();
  const queryClient = useQueryClient();
  const { has } = usePermissions();
  const canRetry = has("tasks");

  const workbench = useQuery({
    queryKey: queryKeys.workbench,
    queryFn: api.workbench,
    refetchInterval: (query) => {
      const data = query.state.data;
      const active = (data?.queue.active_download_count || 0)
        + (data?.queue.active_import_count || 0);
      return active > 0 ? 5000 : 15000;
    },
  });

  const refresh = useMutation({
    mutationFn: api.refreshWorkbench,
    onSuccess: (data) => {
      queryClient.setQueryData(queryKeys.workbench, data);
      toast.success(t("dashboard.refresh_success"));
    },
    onError: (error: Error) => {
      toast.error({
        title: t("dashboard.refresh_failed"),
        message: error.message,
      });
    },
  });

  const retryOne = useMutation({
    mutationFn: async (activity: DashboardActivity) => {
      if (activity.kind === "download") {
        await api.retryDownloadJob(activity.id);
      } else if (activity.kind === "import") {
        await api.retryImportJob(activity.id);
      }
      return activity;
    },
    onSuccess: async () => {
      const data = await api.refreshWorkbench();
      queryClient.setQueryData(queryKeys.workbench, data);
      toast.success(t("dashboard.retry_started"));
    },
    onError: (error: Error) => {
      toast.error({
        title: t("dashboard.retry_failed"),
        message: error.message,
      });
    },
  });

  const retryAll = useMutation({
    mutationFn: api.retryAllFailedJobs,
    onSuccess: async (result) => {
      const data = await api.refreshWorkbench();
      queryClient.setQueryData(queryKeys.workbench, data);
      toast.success(t("dashboard.retry_all_started", { count: result.succeeded }));
    },
    onError: (error: Error) => {
      toast.error({
        title: t("dashboard.retry_failed"),
        message: error.message,
      });
    },
  });

  if (workbench.error && !workbench.data) {
    return (
      <PageShell>
        <ErrorState
          message={(workbench.error as Error).message}
          onRetry={() => workbench.refetch()}
        />
      </PageShell>
    );
  }

  return (
    <PageShell>
      <h1 className="sr-only">{t("dashboard.title")}</h1>

      {!workbench.data ? (
        <DashboardSkeleton />
      ) : (
        <div className="space-y-5">
          <DashboardStatusStrip
            data={workbench.data}
            refreshing={refresh.isPending}
            onRefresh={() => refresh.mutate()}
          />

          <div className="grid min-w-0 gap-5 xl:grid-cols-[minmax(0,1.05fr)_minmax(0,.95fr)]">
            <RecentWorksPanel data={workbench.data} />
            <ActivityPanel
              data={workbench.data}
              canRetry={canRetry}
              retryingKey={retryOne.variables?.key}
              onRetry={(activity) => retryOne.mutate(activity)}
            />
          </div>

          <AttentionBanner
            data={workbench.data}
            canRetry={canRetry}
            retrying={retryAll.isPending}
            onRetryFailedDownloads={() => retryAll.mutate()}
          />

          <ServicesPanel health={workbench.data.health} />

          <p className="sr-only" role="status" aria-live="polite">
            {refresh.isPending
              ? t("dashboard.refreshing")
              : t("dashboard.updated", { time: workbench.data.updated_at })}
          </p>
        </div>
      )}
    </PageShell>
  );
}
