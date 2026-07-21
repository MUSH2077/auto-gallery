"use client";

import Link from "next/link";

import { useT } from "@/lib/i18n";
import { usePermissions } from "@/lib/usePermissions";
import type { ShowcaseConfig } from "@/lib/showcase/config";

export default function ShowcaseHero({ config, itemCount }: { config: ShowcaseConfig; itemCount: number }) {
  const t = useT();
  const { has } = usePermissions();

  const headline = config.headline.trim() ? config.headline : t("showcase.headline_default");

  return (
    // pointer-events-none so this full-viewport overlay lets pointer input
    // fall through to the trail layer beneath it (WebGL canvas or DOM
    // fallback) — only the interactive nav below opts back in.
    <div className="pointer-events-none relative z-10 flex min-h-screen flex-col items-center justify-center gap-6 px-4 text-center">
      <h1 className="max-w-3xl text-fluid-2xl font-semibold tracking-tight text-fg">{headline}</h1>
      {config.showStats && (
        <p className="text-fluid-lg text-muted">{t("showcase.stats", { count: itemCount })}</p>
      )}
      <nav className="pointer-events-auto flex flex-wrap items-center justify-center gap-3">
        <Link href="/admin" className="btn-primary">
          {t("showcase.enter_dashboard")}
        </Link>
        <Link href="/admin/works" className="btn-ghost">
          {t("showcase.enter_gallery")}
        </Link>
        {has("upload") && (
          <Link href="/admin/upload" className="btn-ghost">
            {t("showcase.enter_upload")}
          </Link>
        )}
      </nav>
    </div>
  );
}
