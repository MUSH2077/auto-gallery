"use client";
import { ReactNode } from "react";
import { EmptyState } from "@/components";
import { useT } from "@/lib/i18n";

type Column<T> = { key: string; header: string; render: (row: T) => ReactNode; className?: string };

export default function DataTable<T>({ columns, data, keyField }: { columns: Column<T>[]; data: T[]; keyField: keyof T }) {
  const t = useT();
  if (!data.length) return <EmptyState title={t("common.no_data")} />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b bg-gray-50">
            {columns.map((c) => <th key={c.key} className={`text-left px-4 py-3 font-medium text-gray-600 ${c.className || ""}`}>{c.header}</th>)}
          </tr>
        </thead>
        <tbody>
          {data.map((row) => (
            <tr key={String(row[keyField])} className="border-b hover:bg-gray-50">
              {columns.map((c) => <td key={c.key} className={`px-4 py-3 ${c.className || ""}`}>{c.render(row)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
