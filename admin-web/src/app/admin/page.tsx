"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {BusinessPage, BusinessConfirm as ConfirmDialog} from "@/features/gallery-ui/BusinessPage";
import ErrorState from "@/components/ErrorState";
import {BusinessPermissionGuard as PermissionGuard} from "@/features/gallery-ui/BusinessPage";
import { useToast } from "@/components/Toast";
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
import { secureRandomUuid } from "@/lib/random";
import { actionErrorReason, clearRepeatSyncIntent, createRepeatSyncIntent, readRepeatSyncIntent, repeatSyncConflict, storeRepeatSyncIntent, validateRepeatSyncAcceptance } from "@/lib/task-actions";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

function DashboardSkeleton() {const t=useT();return <p className="state-view" role="status">{t("common.loading")}</p>;}

function DashboardContent() {
  const t = useT();
  const toast = useToast();
  const queryClient = useQueryClient();
  const router = useRouter();
  const [repeatActivity, setRepeatActivity] = useState<DashboardActivity | null>(null);
  const [repeatConflictState, setRepeatConflictState] = useState<ReturnType<typeof repeatSyncConflict> | null>(null);
  const { has, user } = usePermissions();
  const canRetry = has("tasks");
  const repeatSequence=useRef(0),active=useRef(true);
  useEffect(()=>{active.current=true;return()=>{active.current=false;repeatSequence.current++;};},[]);
  const retryLocks=useRef(new Set<string>());
  const [retryKeys,setRetryKeys]=useState(new Set<string>());

  const workbench = useQuery({
    queryKey: queryKeys.workbench,
    queryFn: api.workbench,
    refetchInterval: false,
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
    onSuccess: () => {
      void queryClient.invalidateQueries({queryKey:queryKeys.workbench});
      toast.success(t("dashboard.retry_started"));
    },
    onSettled: (_data,_error,activity) => {retryLocks.current.delete(activity.key);setRetryKeys(new Set(retryLocks.current));},
    onError: (error: Error) => {
      toast.error({
        title: t("dashboard.retry_failed"),
        message: error.message,
      });
    },
  });

  const retryAll = useMutation({
    mutationFn: api.retryAllFailedJobs,
    onSuccess: (result) => {
      if (result.failed > 0) {
        toast.warning({ message: `${t("jobs.batch_result", { succeeded: result.succeeded, failed: result.failed })}: ${result.errors.map((entry) => `${entry.id.slice(0, 8)} ${actionErrorReason(entry.error)}`).join("; ")}` });
      } else {
        toast.success(t("dashboard.retry_all_started", { count: result.succeeded }));
      }
    },
    onError: (error: Error) => {
      toast.error({
        title: t("dashboard.retry_failed"),
        message: error.message,
      });
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
      void queryClient.invalidateQueries({ queryKey: queryKeys.tasks.all });
      void queryClient.invalidateQueries({ queryKey: queryKeys.workbench });
    },
  });

  const repeatOne = useMutation({
    mutationFn: async (activity: DashboardActivity & {intentToken:number}) => {
      if (activity.kind !== "download") throw new Error(t("jobs.repeat_identity_invalid"));
      if (!user?.id) throw new Error(t("jobs.repeat_original_unavailable"));
      const intent = readRepeatSyncIntent(user.id, activity.id) || createRepeatSyncIntent(user.id, activity.id, activity.title, secureRandomUuid);
      storeRepeatSyncIntent(intent);
      const accepted = validateRepeatSyncAcceptance(intent, await api.repeatDownloadJob(activity.id, intent.requestId));
      clearRepeatSyncIntent(intent);
      return accepted;
    },
    onSuccess: (accepted,activity) => {
      void queryClient.invalidateQueries({queryKey:queryKeys.workbench});
      if(!active.current||activity.intentToken!==repeatSequence.current)return;
      setRepeatActivity(null);
      setRepeatConflictState(null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.workbench });
      router.push(`/admin/jobs?tab=downloads&job=${encodeURIComponent(accepted.job_id)}`);
    },
    onError: (error,activity) => {if(active.current&&activity.intentToken===repeatSequence.current)setRepeatConflictState(repeatSyncConflict(error));},
  });

  if (workbench.error && !workbench.data) {
    return (
      <BusinessPage title={t("dashboard.title")}>
        <ErrorState
          message={(workbench.error as Error).message}
          onRetry={() => workbench.refetch()}
        />
      </BusinessPage>
    );
  }

  return (
    <BusinessPage title={t("dashboard.title")}>


      {!workbench.data ? (
        <DashboardSkeleton />
      ) : (
        <div className="space-y-5">
          <DashboardStatusStrip
            data={workbench.data}
            refreshing={refresh.isPending}
            onRefresh={() => refresh.mutate()}
          />

          <div className="business-dashboard-main">
            <RecentWorksPanel data={workbench.data} />
            <ActivityPanel
              data={workbench.data}
              canRetry={canRetry}
              retryingKeys={retryKeys}
              onRetry={activity => {if(retryLocks.current.has(activity.key))return;retryLocks.current.add(activity.key);setRetryKeys(new Set(retryLocks.current));retryOne.mutate(activity);}}
              onRepeat={activity=>{repeatSequence.current++;repeatOne.reset();setRepeatConflictState(null);setRepeatActivity(activity);}}
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
          {repeatActivity && (
            <ConfirmDialog
              open
              title={t("jobs.repeat_sync_title")}
              message={t("jobs.repeat_sync_confirm")}
              onConfirm={() => repeatOne.mutate({...repeatActivity,intentToken:repeatSequence.current})}
              onCancel={() => {
                repeatSequence.current++;
                setRepeatActivity(null);
              }}
              isPending={repeatOne.isPending}
              error={repeatOne.error ? actionErrorReason(repeatOne.error) : undefined}
            >
              {repeatConflictState?.kind === "existing" && <button type="button" className="btn-ghost mb-3" onClick={() => router.push(`/admin/jobs?tab=downloads&job=${encodeURIComponent(repeatConflictState.existingJobId)}`)}>{t("jobs.open_existing_download")}</button>}
              {repeatConflictState?.kind === "identity" && <button type="button" className="btn-ghost mb-3" onClick={() => {
                if (user?.id) localStorage.removeItem(`auto-gallery-repeat-sync:${user.id}:${repeatActivity.id}:repeat_sync`);
                repeatOne.reset(); setRepeatConflictState(null);
              }}>{t("jobs.repeat_new_intent")}</button>}
            </ConfirmDialog>
          )}
        </div>
      )}
    </BusinessPage>
  );
}

export default function Dashboard(){return <PermissionGuard module="system"><DashboardContent/></PermissionGuard>;}
