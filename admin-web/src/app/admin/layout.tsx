"use client";
import { Suspense, type ReactNode } from "react";
import Providers from "@/app/providers";
import AuthGuard from "@/components/AuthGuard";
import GalleryShell from "@/features/gallery/GalleryShell";
import GallerySessionState from "@/features/gallery/GallerySessionState";

export default function AdminLayout({ children }: { children: ReactNode }) {
  return <Providers>
    <Suspense fallback={<GallerySessionState />}>
      <AuthGuard loadingFallback={<GallerySessionState />} unavailableFallback={<GallerySessionState unavailable />}>
        <GalleryShell>{children}</GalleryShell>
      </AuthGuard>
    </Suspense>
  </Providers>;
}
