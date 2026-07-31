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

export interface ChartSeriesPoint {
  id: string;
  label: string;
  value: number;
  description?: string;
}
