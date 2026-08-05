const HEX_COLOR = /^#?([0-9a-f]{6})$/i;

export interface RgbColor {
  r: number;
  g: number;
  b: number;
}

function clampChannel(value: number): number {
  return Math.max(0, Math.min(255, Math.round(value)));
}

export function parseHexColor(value: string): RgbColor | null {
  const match = value.trim().match(HEX_COLOR);
  if (!match) return null;
  const hex = match[1];
  return {
    r: Number.parseInt(hex.slice(0, 2), 16),
    g: Number.parseInt(hex.slice(2, 4), 16),
    b: Number.parseInt(hex.slice(4, 6), 16),
  };
}

export function parseRgbTriplet(value: string): RgbColor | null {
  const channels = value.trim().split(/\s+/).map(Number);
  if (channels.length !== 3 || channels.some((channel) => !Number.isFinite(channel))) {
    return null;
  }
  return { r: channels[0], g: channels[1], b: channels[2] };
}

export function rgbCss(color: RgbColor): string {
  return `rgb(${clampChannel(color.r)} ${clampChannel(color.g)} ${clampChannel(color.b)})`;
}

function linearChannel(value: number): number {
  const normalized = value / 255;
  return normalized <= 0.04045
    ? normalized / 12.92
    : ((normalized + 0.055) / 1.055) ** 2.4;
}

export function contrastRatio(left: RgbColor, right: RgbColor): number {
  const luminance = (color: RgbColor) => (
    0.2126 * linearChannel(color.r)
    + 0.7152 * linearChannel(color.g)
    + 0.0722 * linearChannel(color.b)
  );
  const a = luminance(left);
  const b = luminance(right);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

function mix(left: RgbColor, right: RgbColor, ratio: number): RgbColor {
  return {
    r: left.r + (right.r - left.r) * ratio,
    g: left.g + (right.g - left.g) * ratio,
    b: left.b + (right.b - left.b) * ratio,
  };
}

export function ensureContrast(
  color: RgbColor,
  surface: RgbColor,
  foreground: RgbColor,
  minimum = 3,
): RgbColor {
  if (contrastRatio(color, surface) >= minimum) return color;
  for (let step = 1; step <= 20; step += 1) {
    const candidate = mix(color, foreground, step / 20);
    if (contrastRatio(candidate, surface) >= minimum) return candidate;
  }
  return foreground;
}

export function niceUnit(maximum: number, targetTicks = 24): number {
  if (!Number.isFinite(maximum) || maximum <= 0) return 1;
  const rough = maximum / Math.max(1, targetTicks);
  const power = 10 ** Math.floor(Math.log10(rough));
  const normalized = rough / power;
  const step = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return Math.max(1, step * power);
}

export function radiusForValue(value: number, maximum: number, minimum: number, maximumRadius: number): number {
  if (value <= 0 || maximum <= 0) return minimum;
  const ratio = Math.sqrt(value) / Math.sqrt(maximum);
  return minimum + ratio * (maximumRadius - minimum);
}

export function stableTopWithOther<T>(
  values: T[],
  readValue: (item: T) => number,
  limit: number,
): { top: T[]; remainder: T[] } {
  const sorted = [...values].sort((left, right) => readValue(right) - readValue(left));
  if (sorted.length <= limit) return { top: sorted, remainder: [] };
  return { top: sorted.slice(0, limit), remainder: sorted.slice(limit) };
}
