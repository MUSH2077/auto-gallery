# Design Tokens — admin-web

GitHub-grade, **palette-switchable** system. Source of truth: `--ag-*` CSS vars in
`src/app/globals.css`, surfaced as Tailwind utilities in `tailwind.config.ts` and
driven by `src/lib/theme.tsx`. Each var is an **RGB triplet** ("R G B"), consumed as
`rgb(var(--ag-*) / <alpha-value>)` so Tailwind opacity modifiers (`bg-accent/50`)
work. Prefer semantic tokens over hardcoded hex in new/edited code. Tokens are
additive — existing hex still works; migrate opportunistically.

## Palettes (`data-theme`)

Six palettes share the same token names; only the `--ag-*` values differ, so any
component built on tokens reskins for free. The active palette is set as
`data-theme` on `<html>` by `theme.tsx`; **light/dark is orthogonal** — `html.dark`
combines with `[data-theme=...]` (e.g. `html.dark[data-theme="nord"]`).

| `data-theme` | Label | Notes |
|--------------|-------|-------|
| `github` (default) | GitHub | The original look — unchanged. `:root` == `[data-theme="github"]`. |
| `nord` | Nord | Cool arctic blues |
| `rose` | Rosé | Rosé Pine |
| `solarized` | Solarized | Ethan Schoonover palette |
| `gruvbox` | Gruvbox | Warm retro |
| `catppuccin` | Catppuccin | Mocha/Latte pastels |

Palette list + labels live in `theme.tsx` (`PALETTES`, `PALETTE_LABELS`). Adding a
palette = one `[data-theme="x"]` + `html.dark[data-theme="x"]` block in `globals.css`
and one entry in those two maps. Never hardcode a palette's hex in a component.

## Color (auto dark-mode + palette-aware — no `dark:` needed)

Hex columns below are the **GitHub** palette (light/dark); other palettes override the
same vars. Read the live values from `globals.css`.

| Token (utility)            | CSS var          | Light     | Dark      | Use |
|----------------------------|------------------|-----------|-----------|-----|
| `bg-canvas`                | `--ag-bg`        | `#f6f8fa` | `#0d1117` | Page background |
| `bg-surface`               | `--ag-surface`   | `#ffffff` | `#161b22` | Cards, panels |
| `bg-subtle`                | `--ag-subtle`    | `#f6f8fa` | `#21262d` | Hover / inset surfaces |
| `bg-nav` / `text-nav-fg`   | `--ag-nav` / `--ag-nav-fg` | `#24292f` / `#fff` | `#010409` / `#fff` | Top navigation bar bg + its text |
| `border-border`            | `--ag-border`    | `#d8dee4` | `#30363d` | Hairline borders |
| `text-fg`                  | `--ag-text`      | `#24292f` | `#e6edf3` | Primary text |
| `text-muted`               | `--ag-muted`     | `#57606a` | `#8b949e` | Secondary text |
| `text-accent` / `bg-accent`| `--ag-accent`    | `#0969da` | `#58a6ff` | Links, focus, accents |
| `*-success`                | `--ag-success`   | `#1a7f37` | `#3fb950` | OK / up |
| `*-warning`                | `--ag-warning`   | `#9a6700` | `#d29922` | Caution |
| `*-danger`                 | `--ag-danger`    | `#cf222e` | `#f85149` | Errors / destructive |

### Subtle semantic tints (`*-subtle`)

Tinted backgrounds for badges/banners that pair with the solid semantic color for
text/border. Use these instead of `bg-accent/10` guesses so the tint tracks the palette.

| Token | CSS var | Use |
|-------|---------|-----|
| `bg-accent-subtle`  | `--ag-accent-subtle`  | Info banners, selected rows |
| `bg-success-subtle` | `--ag-success-subtle` | Success badges |
| `bg-warning-subtle` | `--ag-warning-subtle` | Caution badges |
| `bg-danger-subtle`  | `--ag-danger-subtle`  | Error/destructive badges |

> There are also `primary` / `primary-hover` / `on-primary` (button fills) and
> `input` / `placeholder` (form fields) tokens — see `tailwind.config.ts` for the full map.

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
