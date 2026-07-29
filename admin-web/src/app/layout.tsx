import type { Metadata } from "next";
import Providers from "./providers";
import { ADMIN_SIDEBAR_BOOTSTRAP_SCRIPT } from "@/lib/adminSidebar";
import "./globals.css";

export const dynamic = 'force-dynamic';

export const metadata: Metadata = {
  title: "auto-gallery Admin",
  description: "auto-gallery media archive administration",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <script
          id="admin-sidebar-bootstrap"
          dangerouslySetInnerHTML={{ __html: ADMIN_SIDEBAR_BOOTSTRAP_SCRIPT }}
        />
      </head>
      <body className="bg-subtle dark:bg-canvas text-fg antialiased min-h-screen">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
