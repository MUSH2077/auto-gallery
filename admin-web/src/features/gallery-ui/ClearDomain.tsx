"use client";

import { useId, useRef, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, type ClearEntity } from "@/lib/api";
import { usePermissions } from "@/lib/usePermissions";
import { useT } from "@/lib/i18n";
import { Modal } from "@/features/gallery-ui/Modal";
import ErrorState from "@/components/ErrorState";

type Props = {
  entity: ClearEntity;
  title: string;
  description: string;
  embedded?: boolean;
};

export default function ClearDomain({ entity, title, description, embedded = false }: Props) {
  const t = useT();
  const { has } = usePermissions();
  const qc = useQueryClient();
  const inputId = useId();
  const [open, setOpen] = useState(false);
  const [confirmation, setConfirmation] = useState("");
  const [accepted, setAccepted] = useState<string | null>(null);
  const pending = useRef(false);

  const preview = useQuery({
    queryKey: ["clear-impact-preview", entity],
    queryFn: () => api.previewClearEntity(entity),
    enabled: open && has("system"),
    staleTime: 0,
  });

  const clear = useMutation({
    mutationFn: async () => {
      if (!preview.data || confirmation !== preview.data.confirmation_phrase) {
        throw new Error(t("gallery.confirmationRequired"));
      }
      return api.startClearOperation(entity, confirmation);
    },
    onSuccess: (result) => {
      setAccepted(result.task_id);
      setOpen(false);
      void qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
    },
  });

  if (!has("system")) return null;

  const labels: Record<string, string> = {
    works: t("works.title"),
    assets: t("gallery.resources"),
    creators: t("nav.creators"),
    tags: t("nav.tags"),
    subscriptions: t("nav.subscriptions"),
    download_jobs: t("jobs.tab_downloads"),
    import_jobs: t("jobs.imports"),
  };

  async function submit() {
    if (pending.current || !preview.data || confirmation !== preview.data.confirmation_phrase) return;
    pending.current = true;
    try {
      await clear.mutateAsync();
    } catch {
      // The mutation keeps the error visible so the user can retry.
    } finally {
      pending.current = false;
    }
  }

  const modalContent = preview.isPending ? (
    <p role="status">{t("common.loading")}</p>
  ) : preview.isError ? (
    <ErrorState error={preview.error} onRetry={() => void preview.refetch()} />
  ) : preview.data ? (
    <form
      className="gallery-filter-dialog"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <p className="confirm-message">{description}</p>
      <dl className="gallery-impact-counts">
        {Object.entries(preview.data.counts).map(([name, count]) => (
          <div key={name}>
            <dt>{labels[name] || name}</dt>
            <dd>{count}</dd>
          </div>
        ))}
      </dl>
      <label htmlFor={inputId}>
        {t("common.type_to_confirm", { phrase: preview.data.confirmation_phrase })}
      </label>
      <input
        id={inputId}
        autoComplete="off"
        spellCheck={false}
        value={confirmation}
        onChange={(event) => setConfirmation(event.target.value)}
        disabled={clear.isPending}
      />
      {clear.isPending ? <p role="status">{t("jobs.action_submitted_close")}</p> : null}
      {clear.error ? <p role="alert" className="confirm-error">{clear.error.message}</p> : null}
      <div className="confirm-actions">
        <button type="button" className="button" onClick={() => setOpen(false)}>
          {t(clear.isPending ? "common.close" : "common.cancel")}
        </button>
        <button
          type="submit"
          className="button danger-button"
          disabled={clear.isPending || confirmation !== preview.data.confirmation_phrase}
          aria-busy={clear.isPending || undefined}
        >
          {t("common.confirm")}
        </button>
      </div>
    </form>
  ) : null;

  const body = (
    <>
      <p>{description}</p>
      <button
        type="button"
        className="text-button danger-text"
        disabled={clear.isPending}
        onClick={() => {
          clear.reset();
          setConfirmation("");
          setOpen(true);
        }}
      >
        {title}
      </button>
      {accepted ? (
        <p className="operation-feedback" role="status">
          {t("jobs.action_accepted")}{" "}
          <Link href={"/admin/jobs?task=" + encodeURIComponent(accepted)}>
            {t("jobs.task_detail")}
          </Link>
        </p>
      ) : null}
      {open ? (
        <Modal title={title} onClose={() => setOpen(false)}>
          {embedded ? modalContent : <div className="gallery-business business-modal">{modalContent}</div>}
        </Modal>
      ) : null}
    </>
  );

  if (embedded) {
    return <section className="gallery-library-maintenance">{body}</section>;
  }

  return (
    <details className="gallery-library-maintenance business-domain-clear">
      <summary className="danger-text">{title}</summary>
      {body}
    </details>
  );
}
