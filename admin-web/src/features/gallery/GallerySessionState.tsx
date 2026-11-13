"use client";
import GalleryRoot from "./GalleryRoot";
import { BrandNavigation } from "@/features/gallery-ui/Shell";
import { useT } from "@/lib/i18n";

export default function GallerySessionState({ unavailable = false }: { unavailable?: boolean }) {
  const t = useT();
  return <GalleryRoot><div className="app-shell">
    <BrandNavigation href="/admin/login" />
    <main className="state-view" id="main-content" tabIndex={-1} role={unavailable ? "alert" : "status"} aria-busy={!unavailable || undefined}>
      <p>{t(unavailable ? "auth.session_unavailable" : "common.loading")}</p>
      {unavailable && <button type="button" className="button primary" onClick={() => window.location.reload()}>{t("auth.retry")}</button>}
    </main>
  </div></GalleryRoot>;
}
