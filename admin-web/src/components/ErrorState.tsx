import { useT } from "@/lib/i18n";

export default function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  const t = useT();
  return (
    <div className="rounded-md border border-danger/30 bg-danger-subtle p-6 text-center dark:border-danger/30 dark:bg-danger-subtle">
      <p className="mb-3 text-sm text-danger dark:text-danger">{message}</p>
      {onRetry && <button onClick={onRetry} className="btn-danger">{t("common.retry")}</button>}
    </div>
  );
}
