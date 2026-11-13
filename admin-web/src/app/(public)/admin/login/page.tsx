import { Suspense } from "react";
import Providers from "@/app/providers";
import GalleryRoot from "@/features/gallery/GalleryRoot";
import LoginPage from "@/features/gallery/LoginPage";
import GallerySessionState from "@/features/gallery/GallerySessionState";

export default function LoginRoute() {
  return <Providers><Suspense fallback={<GallerySessionState />}>
    <GalleryRoot><LoginPage /></GalleryRoot>
  </Suspense></Providers>;
}
