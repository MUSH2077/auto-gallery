# Design Tokens — admin-web

GitHub-grade system. Source of truth: `--ag-*` CSS vars in `src/app/globals.css`
(they flip in dark mode), surfaced as Tailwind utilities in `tailwind.config.ts`.
Prefer semantic tokens over hardcoded hex in new/edited code. Tokens are additive
— existing hex still works; migrate opportunistically.

## Color (auto dark-mode — no `dark:` needed)

| Token (utility)            | CSS var          | Light     | Dark      | Use |
|----------------------------|------------------|-----------|-----------|-----|
| `bg-canvas`                | `--ag-bg`        | `#f6f8fa` | `#0d1117` | Page background |
| `bg-surface`               | `--ag-surface`   | `#ffffff` | `#161b22` | Cards, panels |
| `bg-subtle`                | `--ag-subtle`    | `#f6f8fa` | `#21262d` | Hover / inset surfaces |
| `border-border`            | `--ag-border`    | `#d8dee4` | `#30363d` | Hairline borders |
| `text-fg`                  | `--ag-text`      | `#24292f` | `#e6edf3` | Primary text |
| `text-muted`               | `--ag-muted`     | `#57606a` | `#8b949e` | Secondary text |
| `text-accent` / `bg-accent`| `--ag-accent`    | `#0969da` | `#58a6ff` | Links, focus, accents |
| `*-success`                | `--ag-success`   | `#1a7f37` | `#3fb950` | OK / up |
| `*-warning`                | `--ag-warning`   | `#9a6700` | `#d29922` | Caution |
| `*-danger`                 | `--ag-danger`    | `#cf222e` | `#f85149` | Errors / destructive |

## Motion

| Token | Value | Use |
|-------|-------|-----|
| `ease-expo` | `cubic-bezier(0.16,1,0.3,1)` | Entrances / orchestrated reveals |
| `duration-fast` | 120ms | Press, icon swaps, exits |
| `duration-base` | 150ms | Default state transitions (hover) |
| `duration-slow` | 240ms | Larger reveals / page transitions |

Rules: never `transition: all` (scope the properties); split enter (opacity +
small translateY) from exit (shorter, quieter); `prefers-reduced-motion` is reset
globally in `globals.css`.

## Shadow / radius / type

- `shadow-overlay` / `shadow-overlay-dark` — layered transparent shadow for dropdowns, dialogs, popovers (use instead of flat `shadow-sm` on overlays).
- `shadow-sm` — resting card depth (keep).
- Radius: keep `rounded-md` (6px). Concentric rule for nested surfaces: `outer = inner + padding`.
- Fluid display sizes: `text-fluid-lg` / `text-fluid-xl` / `text-fluid-2xl` (clamp-based) for headings that should scale mobile→desktop. Body text keeps the default scale.
- Numbers that update (timers, counters, table figures): add `.tabular` (`tabular-nums`).

## Hex → token cheat-sheet (migrate opportunistically)

| Hardcoded | Replace with |
|-----------|--------------|
| `bg-white dark:bg-[#161b22]` | `bg-surface` |
| `text-[#24292f] dark:text-[#e6edf3]` | `text-fg` |
| `text-[#57606a] dark:text-[#8b949e]` | `text-muted` |
| `border-[#d8dee4] dark:border-[#30363d]` | `border-border` |
| `bg-[#f6f8fa] dark:bg-[#21262d]` | `bg-subtle` |
| `text-[#0969da] dark:text-[#58a6ff]` | `text-accent` |

Do NOT mass-rename — swap during edits you're already making to a file, verifying no visual change.
