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
  const maxWidth = size === "full" ? "max-w-none" : size === "normal" ? "max-w-6xl" : "max-w-7xl";
  return (
    <main className={`mx-auto w-full ${maxWidth} px-4 py-6 sm:px-6 ${className}`}>
      {children}
    </main>
  );
}
