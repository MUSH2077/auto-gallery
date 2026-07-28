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
      <body className="bg-[#f6f8fa] dark:bg-[#0d1117] text-[#24292f] dark:text-[#e6edf3] antialiased min-h-screen">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
