import type { Metadata } from "next";
import Providers from "./providers";
import "./globals.css";

export const dynamic = 'force-dynamic';

export const metadata: Metadata = {
  title: "auto-gallery Admin",
  description: "auto-gallery media archive administration",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body className="bg-subtle dark:bg-canvas text-fg antialiased min-h-screen">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
