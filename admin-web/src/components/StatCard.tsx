import { type ReactNode } from "react";

type Tone = "neutral" | "active" | "success" | "danger" | "warning";

const toneClasses: Record<Tone, string> = {
  neutral: "text-fg",
  active: "text-accent",
  success: "text-success dark:text-success",
  danger: "text-danger dark:text-danger",
  warning: "text-warning dark:text-warning",
};

export default function StatCard({
  label,
  value,
  sub,
  tone = "neutral",
  className = "",
}: {
  label: ReactNode;
  value: ReactNode;
  sub?: ReactNode;
  tone?: Tone;
  className?: string;
}) {
  return (
    <div className={`card p-4 ${className}`}>
      <div className={`tabular-nums text-2xl font-semibold ${toneClasses[tone]}`}>{value}</div>
      <div className="mt-1 text-xs font-medium uppercase text-muted">{label}</div>
      {sub && <div className="mt-1 text-xs text-[#8c959f] dark:text-muted">{sub}</div>}
    </div>
  );
}
