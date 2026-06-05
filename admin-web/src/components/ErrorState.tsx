import { useT } from "@/lib/i18n";

export default function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  const t = useT();
  return (
    <div className="rounded-md border border-[#cf222e]/30 bg-[#ffebe9] p-6 text-center dark:border-[#f85149]/30 dark:bg-[#f8514926]">
      <p className="mb-3 text-sm text-[#cf222e] dark:text-[#f85149]">{message}</p>
      {onRetry && <button onClick={onRetry} className="btn-danger">{t("common.retry")}</button>}
    </div>
  );
}
