"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { CheckCircle2, CircleOff, Copy, RefreshCw, ShieldCheck } from "lucide-react";

import { ErrorState, PageHeader, PageShell, PermissionGuard } from "@/components";
import { useToast } from "@/components/Toast";
import { api, queryKeys, type GitllerySettings } from "@/lib/api";
import { useT } from "@/lib/i18n";

type CapabilityName = keyof GitllerySettings["capabilities"];

const CAPABILITY_NAMES: CapabilityName[] = [
  "automatic_projection",
  "reconcile",
  "backfill",
  "rebuild",
  "push",
  "pull",
  "verify",
  "commit",
];

const CLI_COMMANDS = ["config", "login", "status", "log", "verify", "commit"] as const;

function shortId(value?: string | null) {
  if (!value) return "—";
  return value.length > 18 ? `${value.slice(0, 10)}…${value.slice(-6)}` : value;
}

export default function GitllerySettingsPage() {
  const t = useT();
  const toast = useToast();
  const settings = useQuery({
    queryKey: queryKeys.gitllery.settings,
    queryFn: api.getGitllerySettings,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
  const verify = useMutation({
    mutationFn: (repositoryId: string) => api.gitlleryVerify(repositoryId),
    onSuccess: () => toast.success({ message: t("gitllery_settings.verify_queued") }),
    onError: (error: Error) => toast.error({ message: error.message }),
  });

  const copyCommand = async (command: string) => {
    try {
      await navigator.clipboard.writeText(command);
      toast.success({ message: t("common.copied") });
    } catch {
      toast.error({ message: t("gitllery_settings.copy_failed") });
    }
  };

  const data = settings.data;
  const repositories = data?.status.repositories ?? [];
  const integrityFailures = repositories.filter((repo) => !repo.object_integrity_ok).length;

  return (
    <PermissionGuard module="system">
      <PageShell>
        <PageHeader
          title={t("gitllery_settings.title")}
          description={t("gitllery_settings.desc")}
        >
          <button
            type="button"
            onClick={() => settings.refetch()}
            disabled={settings.isFetching}
            className="btn-ghost inline-flex min-h-11 items-center gap-2"
          >
            <RefreshCw aria-hidden="true" className={`h-4 w-4 ${settings.isFetching ? "animate-spin" : ""}`} />
            {settings.isFetching ? t("common.refreshing") : t("common.refresh")}
          </button>
        </PageHeader>

        {settings.isError && (
          <ErrorState
            message={(settings.error as Error).message}
            onRetry={() => settings.refetch()}
          />
        )}
        {!data && !settings.isError && (
          <div className="animate-pulse space-y-4" aria-label={t("common.loading")}>
            <div className="h-28 rounded-lg bg-subtle" />
            <div className="h-56 rounded-lg bg-subtle" />
          </div>
        )}

        {data && (
          <div data-page-primary-content className="space-y-6">
            <section className="rounded-lg border border-warning/30 bg-warning-subtle p-5 text-warning">
              <div className="flex items-start gap-3">
                <ShieldCheck aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0" />
                <div>
                  <h2 className="font-semibold">
                    {data.projection_mode === "shadow"
                      ? t("gitllery_settings.shadow_title")
                      : t("gitllery_settings.active_title")}
                  </h2>
                  <p className="mt-1 text-sm leading-6">
                    {data.projection_mode === "shadow"
                      ? t("gitllery_settings.shadow_desc")
                      : t("gitllery_settings.active_desc")}
                  </p>
                  <p className="mt-2 text-xs">{t("gitllery_settings.deployment_managed")}</p>
                </div>
              </div>
            </section>

            <section aria-labelledby="gitllery-build" className="space-y-3">
              <h2 id="gitllery-build" className="section-title">{t("gitllery_settings.build")}</h2>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                {([
                  [t("gitllery_settings.product"), `${data.product_name} ${data.product_version}`],
                  [t("gitllery_settings.format"), `${data.format_id} r${data.format_revision}`],
                  [t("gitllery_settings.mode"), data.projection_mode],
                  [t("gitllery_settings.generation"), data.build_generation],
                ] as const).map(([label, value]) => (
                  <div key={label} className="card p-4">
                    <div className="text-xs text-muted">{label}</div>
                    <div className="mt-1 break-all font-mono text-sm font-semibold text-fg">{value}</div>
                  </div>
                ))}
              </div>
            </section>

            <section aria-labelledby="gitllery-status" className="space-y-3">
              <h2 id="gitllery-status" className="section-title">{t("gitllery_settings.status")}</h2>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                {([
                  [t("gitllery_settings.repositories"), repositories.length],
                  [t("gitllery_settings.missing"), data.status.missing_repos],
                  [t("gitllery_settings.backlog"), data.status.behind_total],
                  [t("gitllery_settings.integrity_failures"), integrityFailures],
                ] as const).map(([label, value]) => (
                  <div key={label} className="card p-4">
                    <div className="text-xs text-muted">{label}</div>
                    <div className="mt-1 text-2xl font-semibold tabular-nums text-fg">{value}</div>
                  </div>
                ))}
              </div>

              <div className="table-shell max-h-[28rem] overflow-auto">
                <table className="w-full min-w-[760px] text-sm">
                  <thead>
                    <tr className="table-head">
                      <th className="px-4 py-3 text-left font-medium">{t("gitllery_settings.repository")}</th>
                      <th className="px-4 py-3 text-left font-medium">{t("gitllery_settings.integrity")}</th>
                      <th className="px-4 py-3 text-left font-medium">{t("gitllery_settings.head_segment")}</th>
                      <th className="px-4 py-3 text-left font-medium">{t("gitllery_settings.last_commit")}</th>
                      <th className="px-4 py-3 text-right font-medium">{t("gitllery_settings.safe_action")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {repositories.map((repo) => (
                      <tr key={repo.repository_id} className="table-row">
                        <td className="px-4 py-3">
                          <div className="font-medium text-fg">{repo.creator_dir}</div>
                          <div className="font-mono text-xs text-muted">{repo.source} · {repo.repository_id}</div>
                        </td>
                        <td className="px-4 py-3">
                          <span className={repo.object_integrity_ok ? "text-success" : "text-danger"}>
                            {repo.object_integrity_ok ? t("gitllery_settings.integrity_ok") : t("gitllery_settings.integrity_failed")}
                          </span>
                        </td>
                        <td className="px-4 py-3 font-mono text-xs" title={repo.head_segment ?? undefined}>{shortId(repo.head_segment)}</td>
                        <td className="px-4 py-3 font-mono text-xs" title={repo.last_complete_commit_id ?? undefined}>{shortId(repo.last_complete_commit_id)}</td>
                        <td className="px-4 py-3 text-right">
                          <button
                            type="button"
                            onClick={() => verify.mutate(repo.repository_id)}
                            disabled={!data.capabilities.verify.enabled || verify.isPending}
                            className="btn-ghost min-h-10 px-3 text-xs"
                          >
                            {verify.isPending && verify.variables === repo.repository_id
                              ? t("gitllery_settings.queueing")
                              : t("gitllery_settings.verify")}
                          </button>
                        </td>
                      </tr>
                    ))}
                    {repositories.length === 0 && (
                      <tr><td colSpan={5} className="px-4 py-8 text-center text-muted">{t("gitllery_settings.no_repositories")}</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            </section>

            <section aria-labelledby="gitllery-capabilities" className="space-y-3">
              <h2 id="gitllery-capabilities" className="section-title">{t("gitllery_settings.capabilities")}</h2>
              <div className="grid gap-3 md:grid-cols-2">
                {CAPABILITY_NAMES.map((name) => {
                  const capability = data.capabilities[name];
                  const reason = capability.reason === "gitllery_shadow_only"
                    ? t("gitllery_settings.reason.gitllery_shadow_only")
                    : capability.reason === "gitllery_transfer_not_implemented"
                      ? t("gitllery_settings.reason.gitllery_transfer_not_implemented")
                      : capability.reason;
                  return (
                    <div key={name} className="card flex items-center justify-between gap-4 p-4">
                      <div>
                        <div className="text-sm font-medium text-fg">{t(`gitllery_settings.capability.${name}`)}</div>
                        {!capability.enabled && reason && (
                          <div className="mt-1 text-xs text-muted">{reason}</div>
                        )}
                      </div>
                      {capability.enabled ? (
                        <span className="inline-flex items-center gap-1.5 text-xs font-medium text-success">
                          <CheckCircle2 aria-hidden="true" className="h-4 w-4" />
                          {t("gitllery_settings.available")}
                        </span>
                      ) : (
                        <button
                          type="button"
                          disabled
                          aria-label={`${t(`gitllery_settings.capability.${name}`)}: ${t("gitllery_settings.unavailable")}`}
                          className="inline-flex min-h-10 items-center gap-1.5 rounded-md px-3 text-xs font-medium text-muted disabled:cursor-not-allowed disabled:opacity-70"
                        >
                          <CircleOff aria-hidden="true" className="h-4 w-4" />
                          {t("gitllery_settings.unavailable")}
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            </section>

            <section aria-labelledby="gitllery-cli" className="space-y-3">
              <div>
                <h2 id="gitllery-cli" className="section-title">{t("gitllery_settings.cli")}</h2>
                <p className="mt-1 text-sm text-muted">
                  {t("gitllery_settings.cli_limits", {
                    works: data.cli.max_works_per_commit,
                    operations: data.cli.max_operations_per_commit,
                  })}
                </p>
                <p className="mt-1 text-xs text-muted">{t("gitllery_settings.token_notice")}</p>
              </div>
              <div className="space-y-2">
                {CLI_COMMANDS.map((name) => {
                  const command = data.cli.examples[name];
                  return (
                    <div key={name} className="card flex min-w-0 items-center gap-3 p-3">
                      <code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap text-xs text-fg">{command}</code>
                      <button
                        type="button"
                        onClick={() => copyCommand(command)}
                        className="btn-ghost inline-flex min-h-10 shrink-0 items-center gap-2 px-3 text-xs"
                        aria-label={t("gitllery_settings.copy_command", { command: name })}
                      >
                        <Copy aria-hidden="true" className="h-4 w-4" />
                        {t("common.copy")}
                      </button>
                    </div>
                  );
                })}
              </div>
            </section>

            <section aria-labelledby="gitllery-governance" className="card p-5">
              <h2 id="gitllery-governance" className="section-title">{t("gitllery_settings.governance")}</h2>
              <p className="mt-2 text-sm leading-6 text-muted">{t("gitllery_settings.governance_desc")}</p>
              <p className="mt-2 text-sm leading-6 text-muted">{t("gitllery_settings.swap_desc")}</p>
              <ul className="mt-3 grid gap-2 text-sm text-fg md:grid-cols-2">
                <li>• {t("gitllery_settings.observe_global")}</li>
                <li>• {t("gitllery_settings.enforce_project")}</li>
                <li>• {t("gitllery_settings.no_other_projects")}</li>
                <li>• {t("gitllery_settings.no_host_changes")}</li>
              </ul>
              <div className="mt-4 border-t border-border pt-4">
                <h3 className="text-sm font-semibold text-fg">{t("gitllery_settings.algorithm_title")}</h3>
                <ul className="mt-2 grid gap-2 text-sm text-muted md:grid-cols-2">
                  <li>• {t("gitllery_settings.algorithm_aimd")}</li>
                  <li>• {t("gitllery_settings.algorithm_incremental")}</li>
                  <li>• {t("gitllery_settings.algorithm_outbox")}</li>
                  <li>• {t("gitllery_settings.algorithm_workers")}</li>
                </ul>
              </div>
            </section>
          </div>
        )}
      </PageShell>
    </PermissionGuard>
  );
}
