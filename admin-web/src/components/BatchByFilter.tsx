"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { actionErrorReason } from "@/lib/task-actions";

const ACTIONS = [
  "retry", "pause", "resume", "cancel", "delete",
];

export function BatchByFilter({ filters, onSuccess }: { filters: Record<string, string>; onSuccess?: () => void }) {
  const t = useT();
  const [action, setAction] = useState("");
  const [note, setNote] = useState("");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const handleSubmit = async () => {
    if (!action) return;
    if (!window.confirm(t("jobs.batch_filter_confirm", { filters: Object.entries(filters).map(([key, value]) => `${key}=${value}`).join(", ") }))) return;
    setRunning(true);
    setResult(null);
    try {
      const res = await api.batchDownloadJobsByFilter(filters, action, note || undefined);
      const summary = t("jobs.batch_filter_result", {
        succeeded: res.succeeded,
        failed: res.failed,
        matched: res.total_matched,
      });
      setResult(res.errors.length ? `${summary}: ${res.errors.map((entry) => `${entry.id.slice(0, 8)} ${actionErrorReason(entry.error)}`).join("; ")}` : summary);
      onSuccess?.();
    } catch (e) {
      setResult(`${t("common.error")}: ${actionErrorReason(e)}`);
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span className="text-xs text-muted">{Object.entries(filters).map(([key, value]) => `${key}=${value}`).join(" · ")}</span>
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
      {result && <span role="status" className="text-xs text-muted">{result}</span>}
    </div>
  );
}
