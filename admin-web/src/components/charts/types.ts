import type { ReactNode } from "react";

export type ChartColorRole =
  | "neutral"
  | "accent"
  | "success"
  | "warning"
  | "danger"
  | `source:${string}`;

export interface ChartDatum {
  id: string;
  label: string;
  value: number;
  colorRole?: ChartColorRole;
  href?: string;
  description?: string;
}

export interface ChartTableColumn {
  key: string;
  label: string;
  align?: "left" | "right";
}

export interface ChartTableRow {
  id: string;
  cells: Record<string, ReactNode>;
}

export interface ChartTableModel {
  caption: string;
  columns: ChartTableColumn[];
  rows: ChartTableRow[];
}

export interface ChartSeriesPoint {
  id: string;
  label: string;
  value: number;
  description?: string;
}
