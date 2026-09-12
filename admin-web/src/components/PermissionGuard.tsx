"use client";
import { ReactNode } from "react";
import { usePathname } from "next/navigation";
import { LockKeyhole } from "lucide-react";
import EmptyState from "@/components/EmptyState";
import ErrorState from "@/components/ErrorState";
import PageHeader from "@/components/PageHeader";
import PageShell from "@/components/PageShell";
import { adminPageTitleKey } from "@/lib/adminRoutes";
import { usePermissions } from "@/lib/usePermissions";
import { useT } from "@/lib/i18n";

/**
 * Client-side gate for a module-scoped or administrator-only page. Mirrors
 * the backend permission dependency as a UX convenience; the API remains the
 * security boundary and rejects unauthorized calls regardless.
 *
 * While the `me` query is loading, renders nothing to avoid a 403 flash for
 * users who do have access.
 */
type PermissionGuardProps = {
  children: ReactNode;
} & (
  | { module: string; anyOf?: never; adminOnly?: never }
  | { module?: never; anyOf: readonly string[]; adminOnly?: never }
  | { module?: never; anyOf?: never; adminOnly: true }
);

export default function PermissionGuard({ module, anyOf, adminOnly, children }: PermissionGuardProps) {
  const t = useT();
  const pathname = usePathname();
  const { isAdmin, has, isLoading, error, refetch } = usePermissions();
  const requirements = anyOf || (module ? [module] : []);
  const titleKey = adminPageTitleKey(pathname);

  if (isLoading) {
    if (!titleKey) return null;
    return (
      <PageShell>
        <PageHeader title={t(titleKey)} />
        <div aria-hidden="true" className="h-32 animate-pulse rounded-md bg-subtle" />
      </PageShell>
    );
  }

  if (error) {
    return (
      <PageShell>
        {titleKey && <PageHeader title={t(titleKey)} />}
        <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />
      </PageShell>
    );
  }

  const allowed = adminOnly ? isAdmin : requirements.some((requirement) => has(requirement));
  if (!allowed) {
    return (
      <PageShell>
        {titleKey && <PageHeader title={t(titleKey)} />}
        <EmptyState
          icon={<LockKeyhole aria-hidden="true" className="h-8 w-8" />}
          title={t("common.forbidden")}
          description={t("common.forbidden_desc")}
        />
      </PageShell>
    );
  }

  return <>{children}</>;
}
