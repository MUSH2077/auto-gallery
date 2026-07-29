"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { statusLabel } from "@/lib/i18n-format";

const SOURCES = [
  "", "pixiv", "twitter", "iwara", "danbooru", "weibo", "bilibili", "pinterest", "lofter",
];

const STATUSES = ["", "enqueued", "downloading", "downloaded", "importing", "failed", "stale", "paused"];

const ACTIONS = [
  "retry", "pause", "resume", "cancel", "delete",
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
      setResult(t("jobs.batch_filter_result", {
        succeeded: res.succeeded,
        failed: res.failed,
        matched: res.total_matched,
      }));
      onSuccess?.();
    } catch (e) {
      setResult(`${t("common.error")}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="flex items-center gap-2 flex-wrap">
      <select
        value={source}
        onChange={(e) => setSource(e.target.value)}
        className="text-xs px-2 py-1 rounded border bg-surface dark:border-border"
      >
        {SOURCES.map((sourceValue) => (
          <option key={sourceValue} value={sourceValue}>
            {sourceValue ? sourceValue : t("jobs.filter_all_source")}
          </option>
        ))}
      </select>
      <select
        value={status}
        onChange={(e) => setStatus(e.target.value)}
        className="text-xs px-2 py-1 rounded border bg-surface dark:border-border"
      >
        {STATUSES.map((s) => (
          <option key={s} value={s}>{s ? statusLabel(t, s) : t("jobs.filter_all_status")}</option>
        ))}
      </select>
      <select
        value={action}
        onChange={(e) => setAction(e.target.value)}
        className="text-xs px-2 py-1 rounded border bg-surface dark:border-border"
      >
        <option value="">{t("jobs.batch_action_placeholder")}</option>
        {ACTIONS.map((actionValue) => (
          <option key={actionValue} value={actionValue}>{t(`jobs.batch_action_${actionValue}`)}</option>
        ))}
      </select>
      {action === "pause" || action === "cancel" ? (
        <input
          type="text"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder={t("jobs.batch_reason_optional")}
          className="text-xs px-2 py-1 rounded border bg-surface dark:border-border w-32"
        />
      ) : null}
      <button
        onClick={handleSubmit}
        disabled={!action || running}
        className="text-xs px-3 py-1 rounded bg-blue-600 text-white disabled:opacity-50 hover:bg-blue-700"
      >
        {running ? t("jobs.batch_running") : t("jobs.batch_apply")}
      </button>
      {result && <span className="text-xs text-muted">{result}</span>}
    </div>
  );
}
