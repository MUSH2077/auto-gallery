"use client";

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import { useT } from "@/lib/i18n";
import { adminPageTitleKey } from "@/lib/adminRoutes";
import { usePermissions } from "@/lib/usePermissions";
import ErrorState from "@/components/ErrorState";
import { Modal } from "./Modal";
import ClearDomain from "./ClearDomain";

export function BusinessPage({
  children,
  title,
  description,
  actions,
  className = "",
}: {
  children: ReactNode;
  title?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div className={"gallery-business " + className}>
      {title || actions ? (
        <header className="business-heading">
          {title ? <h1>{title}</h1> : null}
          {description ? <p>{description}</p> : null}
          {actions ? <div className="business-heading-actions">{actions}</div> : null}
        </header>
      ) : null}
      {children}
    </div>
  );
}

export function BusinessModal({
  open,
  onClose,
  title,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
}) {
  return open ? (
    <Modal title={title} onClose={onClose}>
      <div className="gallery-business business-modal">{children}</div>
    </Modal>
  ) : null;
}

export function BusinessConfirm({
  open,
  title,
  message,
  onConfirm,
  onCancel,
  isPending,
  error,
  confirmationPhrase,
  confirmDisabled,
  children,
}: {
  open: boolean;
  title: string;
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
  isPending?: boolean;
  error?: string | null;
  confirmationPhrase?: string;
  confirmDisabled?: boolean;
  children?: ReactNode;
}) {
  const t = useT();
  const id = useId();
  const pending = useRef(false);
  const [value, setValue] = useState("");

  useEffect(() => {
    setValue("");
  }, [open, confirmationPhrase]);

  useEffect(() => {
    if (!isPending) pending.current = false;
  }, [isPending]);

  const disabled = Boolean(
    isPending ||
      confirmDisabled ||
      (confirmationPhrase && value !== confirmationPhrase),
  );

  function confirm() {
    if (disabled || pending.current) return;
    pending.current = true;
    onConfirm();
  }

  return (
    <BusinessModal open={open} onClose={onCancel} title={title}>
      <p className="confirm-message">{message}</p>
      {confirmationPhrase ? (
        <label className="business-field" htmlFor={id}>
          {t("common.type_to_confirm", { phrase: confirmationPhrase })}
          <input
            id={id}
            name="confirmation"
            autoComplete="off"
            spellCheck={false}
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
        </label>
      ) : null}
      {children}
      {error ? <p role="alert" className="confirm-error">{error}</p> : null}
      {isPending ? (
        <p role="status">{t("jobs.action_submitted_close")}</p>
      ) : null}
      <div className="confirm-actions">
        <button type="button" className="button" onClick={onCancel}>
          {t(isPending ? "common.close" : "common.cancel")}
        </button>
        <button
          type="button"
          className="button primary"
          disabled={disabled}
          aria-busy={isPending || undefined}
          onClick={confirm}
        >
          {t("common.confirm")}
        </button>
      </div>
    </BusinessModal>
  );
}

export const BusinessDangerZone = ClearDomain;

export function BusinessPermissionGuard({
  children,
  module,
  anyOf,
  adminOnly,
}: {
  children: ReactNode;
  module?: string;
  anyOf?: readonly string[];
  adminOnly?: boolean;
}) {
  const t = useT();
  const pathname = usePathname();
  const { isAdmin, has, isLoading, error, refetch } = usePermissions();
  const key = adminPageTitleKey(pathname);
  const title = key ? t(key) : undefined;

  if (isLoading) {
    return (
      <BusinessPage title={title}>
        <p className="state-view" role="status">{t("common.loading")}</p>
      </BusinessPage>
    );
  }

  if (error) {
    return (
      <BusinessPage title={title}>
        <ErrorState error={error} onRetry={() => void refetch()} />
      </BusinessPage>
    );
  }

  const allowedPermissions = anyOf || (module ? [module] : []);
  const allowed = adminOnly ? isAdmin : allowedPermissions.some(has);
  if (!allowed) {
    return (
      <BusinessPage title={title}>
        <div className="state-view" role="alert">
          <h2>{t("common.forbidden")}</h2>
          <p>{t("common.forbidden_desc")}</p>
        </div>
      </BusinessPage>
    );
  }

  return <>{children}</>;
}
