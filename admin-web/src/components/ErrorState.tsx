import { useT } from "@/lib/i18n";
import { ApiError } from "@/lib/api";

export default function ErrorState({ message, error, onRetry }: { message?: string; error?: unknown; onRetry?: () => void }) {
  const t = useT();
  const detail = error instanceof ApiError && error.kind === "network"
    ? t("common.network_error")
    : message || (error instanceof Error ? error.message : t("common.error"));
  return (
    <div className="rounded-md border border-danger/30 bg-danger-subtle p-6 text-center dark:border-danger/30 dark:bg-danger-subtle">
      <p className="mb-3 text-sm text-danger dark:text-danger">{detail}</p>
      {onRetry && <button onClick={onRetry} className="btn-danger">{t("common.retry")}</button>}
    </div>
  );
}
