"use client";

import { Suspense } from "react";

import { PageHeader, PageShell, PermissionGuard, TableSkeleton } from "@/components";
import { useT } from "@/lib/i18n";
import RemoteDiscoveryPage from "./RemoteDiscoveryPage";

function DiscoveryFallback() {
  const t = useT();
  return (
    <PageShell>
      <PageHeader title={t("discovery.title")} description={t("discovery.desc")} />
      <TableSkeleton rows={6} />
    </PageShell>
  );
}

export default function DiscoveryRoute() {
  return (
    <PermissionGuard module="subscriptions">
      <Suspense fallback={<DiscoveryFallback />}>
        <RemoteDiscoveryPage />
      </Suspense>
    </PermissionGuard>
  );
}
