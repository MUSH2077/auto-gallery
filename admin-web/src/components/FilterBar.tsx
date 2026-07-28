import { type ReactNode } from "react";
import AdaptiveToolbar from "@/components/AdaptiveToolbar";

export default function FilterBar({
  children,
  meta,
  className = "",
}: {
  children: ReactNode;
  meta?: ReactNode;
  className?: string;
}) {
  return (
    <AdaptiveToolbar leading={children} meta={meta} className={`mb-4 ${className}`} />
  );
}
