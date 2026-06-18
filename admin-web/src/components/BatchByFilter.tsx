"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { statusLabel } from "@/lib/i18n-format";

const SOURCES = [
  { value: "", label: "All sources" },
  { value: "pixiv", label: "Pixiv" },
  { value: "twitter", label: "Twitter" },
  { value: "iwara", label: "Iwara" },
  { value: "danbooru", label: "Danbooru" },
  { value: "weibo", label: "Weibo" },
  { value: "bilibili", label: "Bilibili" },
  { value: "pinterest", label: "Pinterest" },
  { value: "lofter", label: "LOFTER" },
];

const STATUSES = ["", "enqueued", "downloading", "downloaded", "importing", "failed", "stale", "paused"];

const ACTIONS = [
  { value: "retry", label: "Retry All" },
  { value: "pause", label: "Pause All" },
  { value: "resume", label: "Resume All" },
  { value: "cancel", label: "Cancel All" },
  { value: "delete", label: "Delete All" },
];

export function BatchByFilter({ onSuccess }: { onSuccess?: () => void }) {
  const t = useT();
  const [source, setSource] = useState("");
  const [status, setStatus] = useState("");
  const [action, setAction] = useState("");
  const [note, setNote] = useState("");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const handleSubmit = async () => {
    if (!action) return;
    setRunning(true);
    setResult(null);
    try {
      const filters: Record<string, string> = {};
      if (source) filters.source = source;
      if (status) filters.status = status;

      const res = await api.batchDownloadJobsByFilter(filters, action, note || undefined);
      setResult(`${res.succeeded} succeeded, ${res.failed} failed (${res.total_matched} matched)`); onSuccess?.();
    } catch (e) {
      setResult(`Error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="flex items-center gap-2 flex-wrap">
      <select
        value={source}
        onChange={(e) => setSource(e.target.value)}
        className="text-xs px-2 py-1 rounded border bg-white dark:bg-slate-700 dark:border-slate-600"
      >
        {SOURCES.map((s) => (
          <option key={s.value} value={s.value}>{s.label}</option>
        ))}
      </select>
      <select
        value={status}
        onChange={(e) => setStatus(e.target.value)}
        className="text-xs px-2 py-1 rounded border bg-white dark:bg-slate-700 dark:border-slate-600"
      >
        {STATUSES.map((s) => (
          <option key={s} value={s}>{s ? statusLabel(t, s) : t("jobs.filter_all_status")}</option>
        ))}
      </select>
      <select
        value={action}
        onChange={(e) => setAction(e.target.value)}
        className="text-xs px-2 py-1 rounded border bg-white dark:bg-slate-700 dark:border-slate-600"
      >
        <option value="">Action...</option>
        {ACTIONS.map((a) => (
          <option key={a.value} value={a.value}>{a.label}</option>
        ))}
      </select>
      {action === "pause" || action === "cancel" ? (
        <input
          type="text"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Reason (optional)"
          className="text-xs px-2 py-1 rounded border bg-white dark:bg-slate-700 dark:border-slate-600 w-32"
        />
      ) : null}
      <button
        onClick={handleSubmit}
        disabled={!action || running}
        className="text-xs px-3 py-1 rounded bg-blue-600 text-white disabled:opacity-50 hover:bg-blue-700"
      >
        {running ? "Running..." : "Apply"}
      </button>
      {result && <span className="text-xs text-gray-500">{result}</span>}
    </div>
  );
}
