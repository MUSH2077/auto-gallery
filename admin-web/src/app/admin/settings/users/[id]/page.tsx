"use client";
import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, PageShell, SectionPanel, ConfirmDialog, Modal, ErrorState, StatusBadge } from "@/components";
import { useToast } from "@/components/Toast";
import { useT } from "@/lib/i18n";
import { formatBytes } from "@/lib/format";
import { userModuleLabel } from "@/lib/i18n-format";
import { adminRoutes } from "@/lib/adminRoutes";

function initials(name: string) {
  return name.trim().slice(0, 2).toUpperCase();
}

function ToggleSwitch({ checked, onChange, label, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <label className={`relative inline-flex min-h-11 min-w-11 items-center justify-center ${disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer"}`}>
      <input type="checkbox" aria-label={label} checked={checked} disabled={disabled}
        onChange={(e) => onChange(e.target.checked)} className="sr-only peer" />
      <div className="w-9 h-5 bg-subtle peer-focus:outline-none rounded-full peer dark:bg-subtle peer-checked:after:translate-x-full rtl:peer-checked:after:-translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:border-border after:border after:rounded-full after:h-4 after:w-4 after:transition-all dark:border-border peer-checked:bg-accent dark:peer-checked:bg-accent"></div>
    </label>
  );
}

export default function UserDetailPage() {
  const t = useT();
  const toast = useToast();
  const router = useRouter();
  const params = useParams();
  const qc = useQueryClient();
  const id = Number(params.id);

  const user = useQuery({ queryKey: queryKeys.users.detail(id), queryFn: () => api.getUser(id) });
  const me = useQuery({ queryKey: queryKeys.me, queryFn: api.getMe });

  const [displayName, setDisplayName] = useState("");
  const [quotaMb, setQuotaMb] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const [resetResult, setResetResult] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!user.data) return;
    setDisplayName(user.data.display_name || "");
    setQuotaMb(user.data.upload_quota_bytes != null ? String(Math.round(user.data.upload_quota_bytes / (1024 * 1024))) : "");
  }, [user.data]);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: queryKeys.users.all });
    qc.invalidateQueries({ queryKey: queryKeys.users.detail(id) });
  };

  const update = useMutation({
    mutationFn: (data: Record<string, unknown>) => api.updateUser(id, data),
    onSuccess: () => { invalidate(); toast.success({ message: t("common.saved") }); },
    onError: (e: Error) => toast.error({ message: e.message }),
  });

  const del = useMutation({
    mutationFn: () => api.deleteUser(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.users.all });
      toast.success({ message: t("notification.deleted") });
      router.push(adminRoutes.users);
    },
    onError: (e: Error) => toast.error({ message: e.message }),
  });

  const resetPw = useMutation({
    mutationFn: () => api.resetUserPassword(id),
    onSuccess: (res) => {
      setConfirmReset(false);
      setResetResult(res.password);
      invalidate();
    },
    onError: (e: Error) => toast.error({ message: e.message }),
  });

  const togglePermission = (key: string) => {
    if (!user.data) return;
    const has = user.data.permissions.includes(key);
    const next = has ? user.data.permissions.filter((p) => p !== key) : [...user.data.permissions, key];
    update.mutate({ permissions: next });
  };

  if (user.isLoading) {
    return (
      <PageShell>
        <PageHeader title={t("user_detail.title")} />
        <div className="space-y-4">{Array.from({ length: 4 }).map((_, i) => <div key={i} className="h-24 animate-pulse rounded-md bg-subtle dark:bg-subtle" />)}</div>
      </PageShell>
    );
  }
  if (user.error) {
    return (
      <PageShell>
        <PageHeader title={t("user_detail.title")} />
        <ErrorState message={(user.error as Error).message} onRetry={() => user.refetch()} />
      </PageShell>
    );
  }
  const u = user.data;
  if (!u) {
    return (
      <PageShell>
        <PageHeader title={t("user_detail.title")} />
        <ErrorState message={t("common.no_data")} onRetry={() => user.refetch()} />
      </PageShell>
    );
  }

  const modules = me.data?.modules || {};
  const displayNameDirty = displayName !== (u.display_name || "");
  const quotaMbTrimmed = quotaMb.trim();
  const parsedQuotaMb = quotaMbTrimmed === "" ? null : parseFloat(quotaMbTrimmed);
  const quotaInvalid = quotaMbTrimmed !== "" && (parsedQuotaMb === null || Number.isNaN(parsedQuotaMb) || parsedQuotaMb < 0);
  const quotaBytes = parsedQuotaMb === null ? null : Math.round(parsedQuotaMb * 1024 * 1024);
  const quotaDirty = !quotaInvalid && quotaBytes !== (u.upload_quota_bytes ?? null);

  return (
    <PageShell>
      <PageHeader
        title={u.display_name || u.username}
        description={
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono">{u.username}</span>
            <StatusBadge status={u.is_active ? "up" : "down"} />
            {u.is_admin && <span className="rounded-full bg-accent-subtle px-2 py-0.5 text-[10px] text-accent dark:bg-accent-subtle dark:text-accent">{t("users.admin_badge")}</span>}
          </div>
        }
      >
        <div className="flex items-center gap-3">
          <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full border border-border bg-gradient-to-br from-accent to-[#8250df] text-base font-semibold text-white dark:border-border">
            {initials(u.display_name || u.username)}
          </div>
        </div>
      </PageHeader>

      <div className="space-y-5">
        <SectionPanel title={t("user_detail.account_section")}>
          <div className="space-y-4">
            <div>
              <label htmlFor="user-display-name" className="mb-1 block text-sm font-medium">{t("user_detail.display_name_field")}</label>
              <div className="flex gap-2">
                <input id="user-display-name" value={displayName} onChange={(e) => setDisplayName(e.target.value)} className="input flex-1" />
                <button onClick={() => update.mutate({ display_name: displayName.trim() === "" ? null : displayName.trim() })} disabled={!displayNameDirty || update.isPending} className="btn-primary shrink-0">
                  {update.isPending ? t("common.saving") : t("common.save")}
                </button>
              </div>
            </div>
            <div className="flex items-center justify-between border-t border-border pt-4 dark:border-border">
              <span className="text-sm font-medium">{t("user_detail.active_toggle")}</span>
              <ToggleSwitch label={t("user_detail.active_toggle")} checked={u.is_active} onChange={(v) => update.mutate({ is_active: v })} disabled={update.isPending} />
            </div>
            <div className="flex items-center justify-between border-t border-border pt-4 dark:border-border">
              <div>
                <span className="text-sm font-medium">{t("user_detail.admin_toggle")}</span>
                <p className="mt-1 text-xs text-muted">{t("user_detail.admin_hint")}</p>
              </div>
              <ToggleSwitch label={t("user_detail.admin_toggle")} checked={u.is_admin} onChange={(v) => update.mutate({ is_admin: v })} disabled={update.isPending} />
            </div>
            {u.must_change_password && <p className="text-xs text-warning dark:text-warning">{t("user_detail.must_change_password")}</p>}
          </div>
        </SectionPanel>

        <SectionPanel title={t("user_detail.permissions_section")} description={u.is_admin ? t("user_detail.permissions_hint_admin") : undefined}>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {Object.keys(modules).map((key) => (
              <label key={key} className={`flex items-center gap-2 text-sm ${u.is_admin ? "opacity-50" : ""}`}>
                <input type="checkbox" className="rounded" disabled={u.is_admin || update.isPending}
                  checked={u.is_admin || u.permissions.includes(key)}
                  onChange={() => togglePermission(key)} />
                {userModuleLabel(t, key)}
              </label>
            ))}
          </div>
        </SectionPanel>

        <SectionPanel title={t("user_detail.quota_section")}>
          <div className="space-y-3">
            <div>
              <label htmlFor="user-upload-quota" className="mb-1 block text-sm font-medium">{t("user_detail.quota_mb_label")}</label>
              <div className="flex gap-2">
                <input id="user-upload-quota" type="number" min={0} value={quotaMb} onChange={(e) => setQuotaMb(e.target.value)} placeholder={t("user_detail.quota_no_limit")} className="input w-40" />
                <button onClick={() => update.mutate({ upload_quota_bytes: quotaBytes })} disabled={!quotaDirty || update.isPending} className="btn-primary shrink-0">
                  {update.isPending ? t("common.saving") : t("common.save")}
                </button>
              </div>
              <p className="mt-1 text-xs text-muted">{t("user_detail.quota_unlimited_hint")}</p>
            </div>
            <div className="flex items-center justify-between border-t border-border pt-3 text-sm dark:border-border">
              <span className="text-muted">{t("user_detail.quota_used_label")}</span>
              <span className="font-mono">
                {formatBytes(u.upload_used_bytes)}
                {u.upload_quota_bytes != null ? ` / ${formatBytes(u.upload_quota_bytes)}` : ` (${t("user_detail.quota_no_limit")})`}
              </span>
            </div>
          </div>
        </SectionPanel>

        <SectionPanel title={t("user_detail.content_section")}>
          <div className="flex items-center justify-between">
            <div>
              <span className="text-sm font-medium">{t("user_detail.nsfw_toggle")}</span>
              <p className="mt-1 text-xs text-muted">{t("user_detail.nsfw_hint")}</p>
            </div>
            <ToggleSwitch label={t("user_detail.nsfw_toggle")} checked={u.nsfw_visible} onChange={(v) => update.mutate({ nsfw_visible: v })} disabled={update.isPending} />
          </div>
        </SectionPanel>

        <SectionPanel title={t("user_detail.actions_section")}>
          <div className="flex flex-wrap gap-3">
            <button onClick={() => setConfirmReset(true)} className="btn-ghost">{t("user_detail.reset_password")}</button>
            <button onClick={() => setConfirmDelete(true)} className="btn-danger">{t("user_detail.delete_user")}</button>
          </div>
        </SectionPanel>
      </div>

      <ConfirmDialog open={confirmReset} title={t("user_detail.reset_password")} message={t("user_detail.reset_password_confirm_msg")}
        onConfirm={() => resetPw.mutate()} onCancel={() => setConfirmReset(false)} isPending={resetPw.isPending} error={(resetPw.error as Error)?.message} />

      <ConfirmDialog open={confirmDelete} title={t("users.delete_title")} message={t("users.delete_msg")}
        onConfirm={() => del.mutate()} onCancel={() => setConfirmDelete(false)} isPending={del.isPending} error={(del.error as Error)?.message} />

      <Modal open={!!resetResult} onClose={() => { setResetResult(null); setCopied(false); }} title={t("user_detail.reset_password_result_title")}>
        <div className="space-y-4">
          <p className="text-sm text-muted">{t("user_detail.reset_password_result_hint")}</p>
          <div className="flex items-center gap-2 rounded-md border border-border bg-subtle p-3 dark:border-border dark:bg-subtle">
            <code className="flex-1 select-all break-all font-mono text-sm">{resetResult}</code>
            <button
              onClick={() => { if (resetResult) { navigator.clipboard?.writeText(resetResult); setCopied(true); } }}
              className="btn-ghost shrink-0">
              {copied ? t("user_detail.copied") : t("user_detail.copy")}
            </button>
          </div>
          <div className="flex justify-end pt-2">
            <button onClick={() => { setResetResult(null); setCopied(false); }} className="btn-primary">{t("common.close")}</button>
          </div>
        </div>
      </Modal>
    </PageShell>
  );
}
