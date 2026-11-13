import type { Metadata } from "next";
import { THEME_BOOTSTRAP_SCRIPT } from "@/lib/themePreference";
import { X_OAUTH_CALLBACK_BOOTSTRAP_SCRIPT } from "@/lib/xOAuthCallbackBootstrap";
import "./globals.css";

export const dynamic = 'force-dynamic';

export const metadata: Metadata = {
  title: "Auto Gallery",
  description: "Auto Gallery media archive administration",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <script id="theme-bootstrap" dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP_SCRIPT }} />
        <script
          id="x-oauth-callback-bootstrap"
          dangerouslySetInnerHTML={{ __html: X_OAUTH_CALLBACK_BOOTSTRAP_SCRIPT }}
        />
      </head>
      <body className="bg-subtle dark:bg-canvas text-fg antialiased min-h-screen">
        {children}
      </body>
    </html>
  );
}
