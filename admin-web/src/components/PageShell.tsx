import { type ReactNode } from "react";

export default function PageShell({
  children,
  size = "wide",
  className = "",
}: {
  children: ReactNode;
  size?: "normal" | "wide" | "full";
  className?: string;
}) {
  // Keep every standard admin page aligned to the task page. The former
  // max-w-6xl "normal" shell shifted headers and page actions inward.
  const maxWidth = size === "full" ? "max-w-none" : "max-w-7xl";
  return (
    <main className={`mx-auto w-full min-w-0 ${maxWidth} px-3 py-5 sm:px-5 sm:py-6 xl:px-6 ${className}`}>
      {children}
    </main>
  );
}
