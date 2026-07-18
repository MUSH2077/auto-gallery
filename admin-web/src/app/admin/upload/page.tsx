"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { api, queryKeys, ApiError } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { usePermissions } from "@/lib/usePermissions";
import { useDebounce } from "@/lib/useDebounce";
import { formatBytes } from "@/lib/format";
import { usePresence, useEnterOnce } from "@/lib/motion";
import { PageHeader, PageShell, SectionPanel, Banner, EmptyState, PermissionGuard } from "@/components";
import { useToast } from "@/components/Toast";

// Mirrors ManualUploadService.ALLOWED_EXTENSIONS (backend/app/services/manual_upload.py).
const ACCEPT_EXTENSIONS = [".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".webm"];
const ACCEPT_ATTR = [
  ...ACCEPT_EXTENSIONS,
  "image/jpeg", "image/png", "image/webp", "image/gif", "video/mp4", "video/webm",
].join(",");
const MAX_FILE_BYTES = 500 * 1024 * 1024;

type RowStatus = "queued" | "uploading" | "done" | "error";

interface FileRow {
  id: string;
  file: File;
  status: RowStatus;
  progress: number;
  error?: string;
  workId?: string;
}

let _rowSeq = 0;

function extOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i).toLowerCase() : "";
}

// Transform-only scaleX smoothing — same pattern as RealProgressBar, just
// stripped down to a bare percent (no stage/message) for a compact row.
function MiniProgressBar({ pct }: { pct: number }) {
  return (
    <div className="h-1.5 w-24 shrink-0 overflow-hidden rounded-full bg-border dark:bg-border">
      <div
        className="h-full w-full rounded-full bg-accent transition-transform duration-slow ease-out"
        style={{ transform: `scaleX(${pct / 100})`, transformOrigin: "left" }}
      />
    </div>
  );
}

function StatusPill({ status }: { status: RowStatus }) {
  const t = useT();
  const toneClasses: Record<RowStatus, string> = {
    queued: "bg-subtle text-muted",
    uploading: "bg-accent-subtle text-accent",
    done: "bg-success-subtle text-success",
    error: "bg-danger-subtle text-danger",
  };
  const labels: Record<RowStatus, string> = {
    queued: t("upload.status_queued"),
    uploading: t("upload.status_uploading"),
    done: t("upload.status_done"),
    error: t("upload.status_error"),
  };
  return <span className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${toneClasses[status]}`}>{labels[status]}</span>;
}

// Searchable creator combobox — only rendered for users with `curation`
// permission (see ManualUploadService._resolve_identity: creator_id requires
// curation, else the backend 403s). Debounced search over api.listCreators.
function CreatorPicker({
  creatorId, creatorLabel, onSelect, onClear,
}: {
  creatorId: string | null;
  creatorLabel: string | null;
  onSelect: (c: { id: string; label: string }) => void;
  onClear: () => void;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const debouncedQuery = useDebounce(query, 300);
  const { mounted, closing } = usePresence(open);
  const ref = useRef<HTMLDivElement>(null);

  const results = useQuery({
    queryKey: queryKeys.creators.list(0, 20, { search: debouncedQuery || undefined }),
    queryFn: () => api.listCreators(0, 20, debouncedQuery ? { search: debouncedQuery } : undefined),
    enabled: open,
  });

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  return (
    <div ref={ref} className="relative">
      <input
        value={creatorId ? creatorLabel || "" : query}
        onChange={(e) => {
          if (creatorId) onClear();
          setQuery(e.target.value);
        }}
        onFocus={() => setOpen(true)}
        placeholder={t("upload.creator_search_placeholder")}
        className="input w-full pr-8"
      />
      {creatorId && (
        <button
          type="button"
          onClick={() => { onClear(); setQuery(""); }}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-fg"
          aria-label={t("common.clear")}
        >
          &times;
        </button>
      )}
      {mounted && (
        <div className={`popover ${closing ? "popover-exit" : ""} absolute z-10 mt-1 max-h-64 w-full overflow-auto rounded-md border border-border bg-surface shadow-overlay dark:shadow-overlay-dark`}>
          <button
            type="button"
            onClick={() => { onClear(); setQuery(""); setOpen(false); }}
            className="block w-full px-3 py-2 text-left text-sm hover:bg-subtle"
          >
            {t("upload.creator_use_personal_space")}
          </button>
          {results.isLoading && <div className="px-3 py-2 text-xs text-muted">{t("common.loading")}</div>}
          {results.data?.items.map((c) => (
            <button
              type="button"
              key={c.id}
              onClick={() => { onSelect({ id: c.id, label: c.display_name || c.name }); setOpen(false); }}
              className="block w-full truncate px-3 py-2 text-left text-sm hover:bg-subtle"
            >
              {c.display_name || c.name}
            </button>
          ))}
          {results.data && results.data.items.length === 0 && (
            <div className="px-3 py-2 text-xs text-muted">{t("common.no_data")}</div>
          )}
        </div>
      )}
    </div>
  );
}

function UploadPageContent() {
  const t = useT();
  const router = useRouter();
  const toast = useToast();
  const qc = useQueryClient();
  const { has } = usePermissions();
  const canCurate = has("curation");

  const me = useQuery({ queryKey: queryKeys.me, queryFn: api.getMe });

  const [title, setTitle] = useState("");
  const [tags, setTags] = useState("");
  const [isNsfw, setIsNsfw] = useState(false);
  const [creatorId, setCreatorId] = useState<string | null>(null);
  const [creatorLabel, setCreatorLabel] = useState<string | null>(null);

  const [rows, setRows] = useState<FileRow[]>([]);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const isNew = useEnterOnce(rows.map((r) => r.id));

  function addFiles(fileList: FileList | File[]) {
    const incoming: FileRow[] = [];
    for (const file of Array.from(fileList)) {
      const ext = extOf(file.name);
      let status: RowStatus = "queued";
      let error: string | undefined;
      if (!ACCEPT_EXTENSIONS.includes(ext)) {
        status = "error";
        error = t("upload.invalid_type");
      } else if (file.size > MAX_FILE_BYTES) {
        status = "error";
        error = t("upload.too_large");
      }
      incoming.push({ id: `f${++_rowSeq}`, file, status, progress: 0, error });
    }
    setRows((prev) => [...prev, ...incoming]);
  }

  function removeRow(id: string) {
    setRows((prev) => prev.filter((r) => r.id !== id));
  }

  function clearCompleted() {
    setRows((prev) => prev.filter((r) => r.status !== "done"));
  }

  const queuedRows = rows.filter((r) => r.status === "queued");
  const queuedBytes = queuedRows.reduce((sum, r) => sum + r.file.size, 0);
  const remainingQuota = me.data?.upload_quota_bytes != null ? me.data.upload_quota_bytes - me.data.upload_used_bytes : null;
  const overQuota = remainingQuota != null && queuedBytes > remainingQuota;
  const hasCompleted = rows.some((r) => r.status === "done");

  // Sequential per-work submit: one API call = one work (task-9-brief.md),
  // so each queued file is its own upload, processed one at a time (not in
  // parallel) so each row's progress bar reflects its own real XHR progress.
  async function handleSubmit() {
    if (isSubmitting) return;
    setIsSubmitting(true);
    const toUpload = rows.filter((r) => r.status === "queued");
    for (const row of toUpload) {
      setRows((prev) => prev.map((r) => (r.id === row.id ? { ...r, status: "uploading", progress: 0 } : r)));
      const fd = new FormData();
      fd.append("files", row.file);
      if (title.trim()) fd.append("title", title.trim());
      if (tags.trim()) fd.append("tags", tags.trim());
      fd.append("is_nsfw", String(isNsfw));
      if (creatorId) fd.append("creator_id", creatorId);
      try {
        const result = await api.uploadWorks(fd, (pct) => {
          setRows((prev) => prev.map((r) => (r.id === row.id ? { ...r, progress: pct } : r)));
        });
        setRows((prev) => prev.map((r) => (r.id === row.id ? { ...r, status: "done", progress: 100, workId: result.work_id } : r)));
        qc.invalidateQueries({ queryKey: queryKeys.me });
        toast.success({
          title: t("upload.upload_success"),
          message: row.file.name,
          action: { label: t("upload.view_work"), onClick: () => router.push(`/admin/works/${result.work_id}`) },
        });
      } catch (e) {
        const err = e as ApiError;
        setRows((prev) => prev.map((r) => (r.id === row.id ? { ...r, status: "error", error: err.message || t("common.unknown_error") } : r)));
        toast.error({ message: err.message || t("common.unknown_error") });
        if (err.status === 413) {
          // Quota is exhausted — every remaining file would fail the same
          // way, so stop instead of hammering the server.
          toast.warning({ message: t("upload.quota_exceeded_stop") });
          break;
        }
      }
    }
    setIsSubmitting(false);
  }

  function onDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragActive(false);
    if (e.dataTransfer.files?.length) addFiles(e.dataTransfer.files);
  }

  return (
    <PageShell size="normal">
      <PageHeader title={t("upload.title")} description={t("upload.description")} />

      {me.data && (
        <Banner tone={overQuota ? "warning" : "info"} title={t("upload.quota_title")} className="mb-4">
          <span className="font-mono">
            {formatBytes(me.data.upload_used_bytes)}
            {me.data.upload_quota_bytes != null ? ` / ${formatBytes(me.data.upload_quota_bytes)}` : ` (${t("upload.quota_unlimited")})`}
          </span>
          {overQuota && <span className="block mt-1">{t("upload.quota_warning")}</span>}
        </Banner>
      )}

      <div className="space-y-4">
        <SectionPanel title={t("upload.settings_title")}>
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="mb-1 block text-sm font-medium">{t("upload.title_label")}</label>
              <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder={t("upload.title_placeholder")} className="input w-full" disabled={isSubmitting} />
            </div>
            <div>
              <label className="mb-1 block text-sm font-medium">{t("upload.tags_label")}</label>
              <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder={t("upload.tags_placeholder")} className="input w-full" disabled={isSubmitting} />
            </div>
            <div className="sm:col-span-2">
              <label className="mb-1 block text-sm font-medium">{t("upload.creator_label")}</label>
              {canCurate ? (
                <CreatorPicker
                  creatorId={creatorId}
                  creatorLabel={creatorLabel}
                  onSelect={(c) => { setCreatorId(c.id); setCreatorLabel(c.label); }}
                  onClear={() => { setCreatorId(null); setCreatorLabel(null); }}
                />
              ) : (
                <>
                  <input value={t("upload.personal_space")} disabled className="input w-full" />
                  <p className="mt-1 text-xs text-muted">{t("upload.personal_space_locked_hint")}</p>
                </>
              )}
            </div>
            <label className="flex items-center gap-2 text-sm font-medium sm:col-span-2">
              <input type="checkbox" className="rounded" checked={isNsfw} onChange={(e) => setIsNsfw(e.target.checked)} disabled={isSubmitting} />
              {t("upload.nsfw_label")}
            </label>
          </div>
        </SectionPanel>

        <SectionPanel>
          <div
            onDrop={onDrop}
            onDragOver={(e) => { e.preventDefault(); setDragActive(true); }}
            onDragLeave={(e) => { e.preventDefault(); setDragActive(false); }}
            onClick={() => fileInputRef.current?.click()}
            className={`cursor-pointer rounded-md border-2 border-dashed p-8 text-center transition-colors ${isSubmitting ? "pointer-events-none opacity-60" : ""} ${dragActive ? "border-accent bg-accent-subtle" : "border-border hover:border-accent/50"}`}
          >
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={ACCEPT_ATTR}
              className="hidden"
              onChange={(e) => {
                if (e.target.files?.length) addFiles(e.target.files);
                e.target.value = "";
              }}
            />
            <p className="text-sm font-medium text-fg">{t("upload.dropzone_title")}</p>
            <p className="mt-1 text-xs text-muted">{t("upload.dropzone_hint")}</p>
          </div>
        </SectionPanel>

        <SectionPanel
          title={t("upload.file_list_title", { count: rows.length })}
          actions={hasCompleted ? <button onClick={clearCompleted} className="btn-ghost">{t("upload.clear_completed")}</button> : undefined}
        >
          {rows.length === 0 ? (
            <EmptyState title={t("upload.no_files")} />
          ) : (
            <div className="space-y-2">
              {rows.map((r) => (
                <div key={r.id} className={`flex items-center gap-3 rounded-md border border-border p-2 text-sm ${isNew(r.id) ? "fade-in" : ""}`}>
                  <span className="min-w-0 flex-1 truncate">{r.file.name}</span>
                  <span className="shrink-0 font-mono text-xs text-muted">{formatBytes(r.file.size)}</span>
                  {r.status === "uploading" && <MiniProgressBar pct={r.progress} />}
                  <StatusPill status={r.status} />
                  {r.status === "done" && r.workId && (
                    <Link href={`/admin/works/${r.workId}`} className="shrink-0 text-xs text-accent hover:underline">
                      {t("upload.view_work")}
                    </Link>
                  )}
                  {r.status === "error" && r.error && (
                    <span className="max-w-[160px] shrink-0 truncate text-xs text-danger" title={r.error}>{r.error}</span>
                  )}
                  {(r.status === "queued" || r.status === "error") && !isSubmitting && (
                    <button onClick={() => removeRow(r.id)} className="btn-icon shrink-0" aria-label={t("upload.remove")}>&times;</button>
                  )}
                </div>
              ))}
            </div>
          )}
        </SectionPanel>

        <div className="flex justify-end">
          <button onClick={handleSubmit} disabled={isSubmitting || queuedRows.length === 0} className="btn-primary">
            {isSubmitting ? t("upload.submitting") : t("upload.submit", { count: queuedRows.length })}
          </button>
        </div>
      </div>
    </PageShell>
  );
}

export default function UploadPage() {
  return (
    <PermissionGuard module="upload">
      <UploadPageContent />
    </PermissionGuard>
  );
}
