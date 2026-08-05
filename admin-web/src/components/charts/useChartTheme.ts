"use client";

import { useEffect, useMemo, useState } from "react";

import { getSourceColor } from "@/lib/sourceColors";
import { useTheme } from "@/lib/theme";

import { ensureContrast, parseHexColor, parseRgbTriplet, rgbCss, type RgbColor } from "./chartMath";
import type { ChartColorRole } from "./types";

interface ChartTheme {
  surface: string;
  text: string;
  muted: string;
  border: string;
  subtle: string;
  accent: string;
  success: string;
  warning: string;
  danger: string;
  colorFor: (role?: ChartColorRole) => string;
}

const FALLBACK_LIGHT: Record<string, RgbColor> = {
  surface: { r: 255, g: 255, b: 255 },
  text: { r: 23, g: 33, b: 48 },
  muted: { r: 79, g: 91, b: 109 },
  border: { r: 207, g: 216, b: 227 },
  subtle: { r: 238, g: 242, b: 247 },
  accent: { r: 29, g: 78, b: 216 },
  success: { r: 15, g: 105, b: 55 },
  warning: { r: 139, g: 79, b: 0 },
  danger: { r: 185, g: 28, b: 48 },
};

const FALLBACK_DARK: Record<string, RgbColor> = {
  surface: { r: 15, g: 24, b: 39 },
  text: { r: 229, g: 235, b: 244 },
  muted: { r: 147, g: 160, b: 180 },
  border: { r: 48, g: 62, b: 83 },
  subtle: { r: 24, g: 35, b: 53 },
  accent: { r: 96, g: 165, b: 250 },
  success: { r: 74, g: 222, b: 128 },
  warning: { r: 251, g: 191, b: 36 },
  danger: { r: 251, g: 113, b: 133 },
};

const TOKEN_NAMES: Record<string, string> = {
  surface: "--ag-surface",
  text: "--ag-text",
  muted: "--ag-muted",
  border: "--ag-border",
  subtle: "--ag-subtle",
  accent: "--ag-accent",
  success: "--ag-success",
  warning: "--ag-warning",
  danger: "--ag-danger",
};

function readTheme(fallback: Record<string, RgbColor>): Record<string, RgbColor> {
  if (typeof window === "undefined") return fallback;
  const style = window.getComputedStyle(document.documentElement);
  return Object.fromEntries(
    Object.entries(TOKEN_NAMES).map(([key, token]) => [
      key,
      parseRgbTriplet(style.getPropertyValue(token)) || fallback[key],
    ]),
  );
}

export function useChartTheme(): ChartTheme {
  const { resolved } = useTheme();
  const fallback = resolved === "dark" ? FALLBACK_DARK : FALLBACK_LIGHT;
  const [palette, setPalette] = useState<Record<string, RgbColor>>(() => fallback);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => setPalette(readTheme(fallback)));
    return () => window.cancelAnimationFrame(frame);
  }, [resolved]);

  return useMemo(() => {
    const semantic = (key: string) => rgbCss(palette[key] || fallback[key]);
    const colorFor = (role: ChartColorRole = "neutral") => {
      if (role.startsWith("source:")) {
        const base = parseHexColor(getSourceColor(role.slice("source:".length)));
        if (base) {
          return rgbCss(ensureContrast(base, palette.surface, palette.text));
        }
      }
      if (role === "accent" || role === "success" || role === "warning" || role === "danger") {
        return semantic(role);
      }
      return semantic("text");
    };
    return {
      surface: semantic("surface"),
      text: semantic("text"),
      muted: semantic("muted"),
      border: semantic("border"),
      subtle: semantic("subtle"),
      accent: semantic("accent"),
      success: semantic("success"),
      warning: semantic("warning"),
      danger: semantic("danger"),
      colorFor,
    };
  }, [fallback, palette]);
}
