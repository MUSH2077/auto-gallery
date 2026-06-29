import type { ReactNode } from "react";

type Tone = "info" | "success" | "warning" | "danger";

// Token-based tinted banner. Reskins with the active palette automatically.
const toneClasses: Record<Tone, string> = {
  info: "border-accent/30 bg-accent-subtle text-accent",
  success: "border-success/30 bg-success-subtle text-success",
  warning: "border-warning/30 bg-warning-subtle text-warning",
  danger: "border-danger/30 bg-danger-subtle text-danger",
};

export default function Banner({
  tone = "info",
  title,
  children,
  className = "",
}: {
  tone?: Tone;
  title?: string;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <div className={`rounded-md border px-3 py-2 text-sm ${toneClasses[tone]} ${className}`} role="status">
      {title && <div className="font-medium">{title}</div>}
      {children != null && <div className={title ? "mt-0.5" : ""}>{children}</div>}
    </div>
  );
}
