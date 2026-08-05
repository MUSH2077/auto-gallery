import { type ReactNode } from "react";

export default function PageShell({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      data-page-shell
      className={`mx-auto w-full min-w-0 max-w-7xl px-3 py-5 sm:px-5 sm:py-6 xl:px-6 ${className}`}
    >
      {children}
    </div>
  );
}
