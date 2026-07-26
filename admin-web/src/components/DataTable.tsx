"use client";
import { ReactNode } from "react";
import { EmptyState } from "@/components";
import { useT } from "@/lib/i18n";
import { useStaggeredEntrance } from "@/lib/motion";

type Column<T> = { key: string; header: string; render: (row: T) => ReactNode; className?: string };

export default function DataTable<T>({ columns, data, keyField }: { columns: Column<T>[]; data: T[]; keyField: keyof T }) {
  const t = useT();
  const keys = data.map((row) => String(row[keyField]));
  const entrance = useStaggeredEntrance(keys);
  if (!data.length) return <EmptyState title={t("common.no_data")} />;
  return (
    <div className="table-shell">
      <table className="w-full text-sm">
        <thead>
          <tr className="table-head">
            {columns.map((c) => <th key={c.key} className={`text-left px-4 py-2.5 font-medium ${c.className || ""}`}>{c.header}</th>)}
          </tr>
        </thead>
        <tbody>
          {data.map((row, index) => {
            const key = String(row[keyField]);
            const entry = entrance(key, index);
            return (
              <tr key={key} className={`${entry.className} table-row`} style={entry.style}>
                {columns.map((c) => <td key={c.key} className={`px-4 py-2.5 ${c.className || ""}`}>{c.render(row)}</td>)}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
