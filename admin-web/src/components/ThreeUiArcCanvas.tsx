"use client";

import { useEffect, useRef } from "react";

import { motionConfig } from "@/lib/motion/config";

type ArcCanvasProps = {
  className?: string;
  density?: "calm" | "regular";
};

type Rgb = [number, number, number];

function readRgb(host: HTMLElement, variable: string, fallback: Rgb): Rgb {
  const raw = getComputedStyle(host).getPropertyValue(variable).trim();
  const values = raw.split(/\s+/).map(Number);
  return values.length === 3 && values.every(Number.isFinite)
    ? values as Rgb
    : fallback;
}

/**
 * Token-aware Canvas2D adaptation of ThreeUI's Predictive Arc concept.
 * The renderer is intentionally local: it keeps Auto Gallery's colors,
 * reduced-motion behavior and runtime budget instead of importing ThreeUI's
 * global stylesheet or application shell.
 */
export default function ThreeUiArcCanvas({ className = "", density = "regular" }: ArcCanvasProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const host = hostRef.current;
    const canvas = canvasRef.current;
    const context = canvas?.getContext("2d", { alpha: true });
    if (!host || !canvas || !context) return;

    let width = 1;
    let height = 1;
    let frame = 0;
    let visible = true;
    let time = 0;
    let lastFrame = 0;
    const animate = motionConfig.shouldAnimate();

    const resize = () => {
      const bounds = host.getBoundingClientRect();
      width = Math.max(1, bounds.width);
      height = Math.max(1, bounds.height);
      const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.round(width * pixelRatio);
      canvas.height = Math.round(height * pixelRatio);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
      render();
    };

    const render = () => {
      context.clearRect(0, 0, width, height);
      const accent = readRgb(host, "--ag-accent", [29, 78, 216]);
      const muted = readRgb(host, "--ag-muted", [79, 91, 109]);
      const spacing = density === "calm" ? 10 : 8;
      const centerX = width * 0.55;
      const curveWidth = width * 1.38;
      const peak = height * 0.22;
      const arcHeight = height * 0.94;

      for (let x = 0; x <= width; x += spacing) {
        const normalizedX = (x - centerX) / (curveWidth / 2);
        const curveY = peak + normalizedX * normalizedX * arcHeight;
        for (let y = 0; y <= height; y += spacing) {
          const distance = Math.abs(y - curveY);
          const band = (density === "calm" ? 62 : 82) * Math.max(0.58, 1 - Math.abs(normalizedX) * 0.16);
          if (distance >= band) continue;
          const base = 1 - distance / band;
          const signal = animate ? (Math.sin(x * 0.017 + time) * Math.cos(y * 0.023 - time) + 1) / 2 : 0.5;
          const intensity = Math.max(0, base * (0.52 + signal * 0.48) * (1 - Math.min(0.72, Math.abs(normalizedX) * 0.38)));
          if (intensity < 0.08) continue;
          const mix = Math.min(1, intensity * 1.28);
          const red = muted[0] + (accent[0] - muted[0]) * mix;
          const green = muted[1] + (accent[1] - muted[1]) * mix;
          const blue = muted[2] + (accent[2] - muted[2]) * mix;
          context.fillStyle = `rgba(${Math.round(red)}, ${Math.round(green)}, ${Math.round(blue)}, ${0.08 + intensity * 0.52})`;
          const dot = 1 + intensity * (density === "calm" ? 1.25 : 1.75);
          context.fillRect(x, y, dot, dot);
        }
      }
    };

    const tick = (timestamp: number) => {
      if (timestamp - lastFrame >= 32) {
        time += 0.018;
        render();
        lastFrame = timestamp;
      }
      frame = visible && !document.hidden ? requestAnimationFrame(tick) : 0;
    };

    const resizeObserver = new ResizeObserver(resize);
    const intersectionObserver = new IntersectionObserver(([entry]) => {
      visible = entry?.isIntersecting ?? true;
      if (animate && visible && !document.hidden && !frame) frame = requestAnimationFrame(tick);
      if (!visible && frame) {
        cancelAnimationFrame(frame);
        frame = 0;
      }
    });
    const onVisibility = () => {
      if (document.hidden && frame) {
        cancelAnimationFrame(frame);
        frame = 0;
      } else if (animate && visible && !frame) {
        frame = requestAnimationFrame(tick);
      }
    };

    resizeObserver.observe(host);
    intersectionObserver.observe(host);
    document.addEventListener("visibilitychange", onVisibility);
    resize();
    if (animate) frame = requestAnimationFrame(tick);

    return () => {
      if (frame) cancelAnimationFrame(frame);
      resizeObserver.disconnect();
      intersectionObserver.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [density]);

  return (
    <div ref={hostRef} className={`pointer-events-none absolute inset-0 overflow-hidden ${className}`} aria-hidden="true">
      <canvas ref={canvasRef} data-threeui-inspired="predictive-arc" className="h-full w-full" />
    </div>
  );
}
