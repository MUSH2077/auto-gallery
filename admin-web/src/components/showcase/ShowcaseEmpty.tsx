"use client";

import Link from "next/link";

import { EmptyState } from "@/components";
import { adminRoutes } from "@/lib/adminRoutes";
import { useT } from "@/lib/i18n";
import { usePermissions } from "@/lib/usePermissions";

export default function ShowcaseEmpty({ variant = "library" }: { variant?: "library" | "filtered" }) {
  const t = useT();
  const { has } = usePermissions();

  if (variant === "filtered") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-subtle px-4 dark:bg-canvas">
        <div className="w-full max-w-md">
          <EmptyState
            title={t("showcase.empty_filtered_title")}
            description={t("showcase.empty_filtered_desc")}
            action={
              <div className="flex flex-wrap items-center justify-center gap-3">
                <Link href={adminRoutes.profileTab("showcase")} className="btn-primary">
                  {t("showcase_settings.title")}
                </Link>
              </div>
            }
          />
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-subtle px-4 dark:bg-canvas">
      <div className="w-full max-w-md">
        <EmptyState
          title={t("showcase.empty_title")}
          description={t("showcase.empty_desc")}
          action={
            <div className="flex flex-wrap items-center justify-center gap-3">
              <Link href="/admin/subscriptions" className="btn-primary">
                {t("nav.subscriptions")}
              </Link>
              {has("upload") && (
                <Link href="/admin/upload" className="btn-ghost">
                  {t("nav.upload")}
                </Link>
              )}
            </div>
          }
        />
      </div>
    </div>
  );
}
