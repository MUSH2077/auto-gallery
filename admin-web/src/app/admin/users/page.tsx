"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { useStaggeredEntrance } from "@/lib/motion";
import { PageHeader, PageShell, EmptyState, ErrorState, ConfirmDialog, Modal, EntityList, EntityRow, RowActionMenu } from "@/components";
import { useToast } from "@/components/Toast";
import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";

function CreateForm({ isPending, error, modules, onSubmit, onClose }: {
  isPending: boolean;
  error: Error | null;
  modules: Record<string, string>;
  onSubmit: (data: { username: string; password: string; display_name?: string; is_admin: boolean; permissions: string[] }) => void;
  onClose: () => void;
}) {
  const t = useT();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [permissions, setPermissions] = useState<string[]>([]);

  const togglePermission = (key: string) => {
    setPermissions((prev) => (prev.includes(key) ? prev.filter((p) => p !== key) : [...prev, key]));
  };

  return (
    <div className="space-y-4">
      <div>
        <label className="block text-sm font-medium mb-1">{t("users.username_label")}</label>
        <input value={username} onChange={(e) => setUsername(e.target.value)} className="input w-full" placeholder={t("users.username_placeholder")} />
      </div>
      <div>
        <label className="block text-sm font-medium mb-1">{t("users.password_label")}</label>
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} className="input w-full" placeholder={t("users.password_placeholder")} />
      </div>
      <div>
        <label className="block text-sm font-medium mb-1">{t("users.display_name_label")}</label>
        <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} className="input w-full" />
      </div>
      <label className="flex items-center gap-2 text-sm font-medium">
        <input type="checkbox" className="rounded" checked={isAdmin}
          onChange={(e) => { setIsAdmin(e.target.checked); if (e.target.checked) setPermissions([]); }} />
        {t("users.is_admin_label")}
      </label>
      <div>
        <div className="mb-1 text-sm font-medium">{t("users.permissions_label")}</div>
        {isAdmin && <p className="mb-2 text-xs text-muted">{t("users.permissions_disabled_admin_hint")}</p>}
        <div className="grid grid-cols-2 gap-2">
          {Object.keys(modules).map((key) => (
            <label key={key} className={`flex items-center gap-2 text-sm ${isAdmin ? "opacity-50" : ""}`}>
              <input type="checkbox" className="rounded" disabled={isAdmin}
                checked={permissions.includes(key)} onChange={() => togglePermission(key)} />
              {t(`users.module_${key}`, modules[key])}
            </label>
          ))}
        </div>
      </div>
      <div className="flex justify-end gap-3 pt-2">
        <button onClick={onClose} className="btn-ghost">{t("users.cancel")}</button>
        <button
          onClick={() => onSubmit({ username, password, display_name: displayName || undefined, is_admin: isAdmin, permissions: isAdmin ? [] : permissions })}
          disabled={!username || !password || isPending}
          className="btn-primary">
          {isPending ? t("users.creating") : t("users.create")}
        </button>
      </div>
      {error && <p className="text-sm text-danger dark:text-danger">{error.message}</p>}
    </div>
  );
}

export default function UsersPage() {
  const t = useT();
  const fmt = useI18nFormat();
  const toast = useToast();
  const router = useRouter();
  const qc = useQueryClient();

  const [showCreate, setShowCreate] = useState(false);
  const [deleteId, setDeleteId] = useState<number | null>(null);

  const users = useQuery({ queryKey: queryKeys.users.all, queryFn: api.listUsers });
  const me = useQuery({ queryKey: queryKeys.me, queryFn: api.getMe });

  const userItems = users.data || [];
  const userEntrance = useStaggeredEntrance(userItems.map((user) => user.id));

  const create = useMutation({
    mutationFn: (data: { username: string; password: string; display_name?: string; is_admin: boolean; permissions: string[] }) => api.createUser(data),
    onSuccess: () => {
      setShowCreate(false);
      qc.invalidateQueries({ queryKey: queryKeys.users.all });
      toast.success({ message: t("notification.created") });
    },
    onError: (e: Error) => toast.error({ message: e.message }),
  });

  const del = useMutation({
    mutationFn: (id: number) => api.deleteUser(id),
    onSuccess: () => {
      setDeleteId(null);
      qc.invalidateQueries({ queryKey: queryKeys.users.all });
      toast.success({ message: t("notification.deleted") });
    },
    onError: (e: Error) => toast.error({ message: e.message }),
  });

  return (
    <PageShell size="normal">
      <PageHeader title={t("users.title")} description={t("users.count", { count: users.data?.length ?? 0 })}>
        <button onClick={() => setShowCreate(true)} className="btn-primary">{t("users.new")}</button>
      </PageHeader>

      {users.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-16 rounded-md bg-subtle dark:bg-subtle animate-pulse" />)}</div>}
      {users.error && <ErrorState message={(users.error as Error).message} onRetry={() => users.refetch()} />}
      {users.data && users.data.length === 0 && (
        <EmptyState
          title={t("users.no_users")}
          description={t("users.no_users_desc")}
          action={<button onClick={() => setShowCreate(true)} className="btn-primary">{t("users.create_user")}</button>}
        />
      )}

      {users.data && users.data.length > 0 && (
        <EntityList label={t("users.title")}>
          {users.data.map((u, i) => (
            <EntityRow
              key={u.id}
              label={t("common.open_item", { name: u.display_name || u.username })}
              entrance={userEntrance(u.id, i)}
              onOpen={() => router.push(`/admin/users/${u.id}`)}
            >
              <div className="entity-avatar">
                {(u.display_name || u.username).trim().slice(0, 2).toUpperCase()}
              </div>
              <div className="entity-main">
                <div className="entity-title-line">
                  <span className="entity-title">{u.display_name || u.username}</span>
                  {u.display_name && <span className="truncate font-mono text-xs text-muted">{u.username}</span>}
                  <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${u.is_active ? "bg-success" : "bg-border"}`} />
                  {u.is_admin && <span className="rounded-full bg-accent-subtle px-2 py-0.5 text-[10px] text-accent dark:bg-accent-subtle dark:text-accent">{t("users.admin_badge")}</span>}
                </div>
                <div className="entity-meta">
                  <span>{u.is_admin ? t("users.all_permissions") : t("users.permission_count", { count: u.permissions.length })}</span>
                  <span>{t("users.last_login", { time: u.last_login_at ? fmt.dateTime(u.last_login_at) : t("common.never") })}</span>
                </div>
              </div>
              <div className="entity-actions" onClick={(event) => event.stopPropagation()}>
                <RowActionMenu
                  label={t("common.more_actions")}
                  items={[{
                    label: t("users.del"),
                    tone: "danger",
                    disabled: u.id === me.data?.id,
                    onSelect: () => setDeleteId(u.id),
                  }]}
                />
              </div>
            </EntityRow>
          ))}
        </EntityList>
      )}

      <Modal open={showCreate} onClose={() => setShowCreate(false)} title={t("users.new_user_title")}>
        <CreateForm isPending={create.isPending} error={create.error as Error | null} modules={me.data?.modules || {}}
          onSubmit={(data) => create.mutate(data)} onClose={() => setShowCreate(false)} />
      </Modal>
      {deleteId !== null && (
        <ConfirmDialog open title={t("users.delete_title")} message={t("users.delete_msg")}
          onConfirm={() => del.mutate(deleteId)} onCancel={() => setDeleteId(null)} isPending={del.isPending} error={(del.error as Error)?.message} />
      )}
    </PageShell>
  );
}
