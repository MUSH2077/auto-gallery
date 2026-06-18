import { type ReactNode } from "react";

type Tone = "neutral" | "active" | "success" | "danger" | "warning";

const toneClasses: Record<Tone, string> = {
  neutral: "text-[#24292f] dark:text-[#e6edf3]",
  active: "text-[#0969da] dark:text-[#58a6ff]",
  success: "text-[#1a7f37] dark:text-[#3fb950]",
  danger: "text-[#cf222e] dark:text-[#f85149]",
  warning: "text-[#9a6700] dark:text-[#d29922]",
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
      <div className="mt-1 text-xs font-medium uppercase text-[#57606a] dark:text-[#8b949e]">{label}</div>
      {sub && <div className="mt-1 text-xs text-[#8c959f] dark:text-[#6e7681]">{sub}</div>}
    </div>
  );
}
