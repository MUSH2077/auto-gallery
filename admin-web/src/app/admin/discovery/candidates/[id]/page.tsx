"use client";

import { Suspense } from "react";

import { PageShell, PermissionGuard } from "@/components";
import { useT } from "@/lib/i18n";
import RemoteCreatorDetailPage from "./RemoteCreatorDetailPage";

function DetailFallback() {
  const t = useT();
  return (
    <PageShell className="max-w-[96rem]">
      <div className="h-72 animate-pulse rounded-2xl bg-subtle" aria-label={t("common.loading")} />
    </PageShell>
  );
}

export default function DiscoveryCandidateDetailRoute() {
  return (
    <PermissionGuard module="subscriptions">
      <Suspense fallback={<DetailFallback />}>
        <RemoteCreatorDetailPage />
      </Suspense>
    </PermissionGuard>
  );
}
