# Source Color Consistency Constraint

## Rule

Every source provider has a single canonical color used consistently across the entire admin system. Colors must match the gallery-dl settings page tabs at `/admin/settings/gallerydl`.

## Canonical color palette

Stored in `admin-web/src/lib/sourceColors.ts` — the single source of truth:

| Source | Hex | Tailwind | Rationale |
|--------|-----|----------|-----------|
| pixiv | `#0066FF` | blue-500 | Pixiv brand blue |
| iwara | `#EC4899` | pink-500 | Iwara brand pink |
| x / twitter | `#6B7280` | gray-500 | Neutral — X has no single brand color |
| danbooru | `#B45309` | amber-700 | Danbooru warm amber theme |
| pinterest | `#EF4444` | red-500 | Pinterest brand red |
| lofter | `#14B8A6` | teal-500 | LOFTER brand teal |
| weibo | `#F97316` | orange-500 | Weibo brand orange |
| bilibili | `#06B6D4` | cyan-500 | Bilibili brand cyan |

## Where colors are used

Every UI element representing a source must use the canonical color:

- **SourceBadge** component — Tailwind classes from `getSourceBadgeColor()`
- **WorkGrid** (GitHub-style heatmap) — hex from `SOURCE_COLORS`
- **Creator detail charts** — hex from `SOURCE_COLORS` and `CHART_COLORS`
- **Data center storage bars** — hex from `getSourceColor()`
- **Gallery-dl settings tabs** — Tailwind border colors matching hex
- **Sources page** — color dot next to provider name

## Adding a new source

When adding a new source provider:
1. Choose a color that matches the source's brand identity
2. Add it to `admin-web/src/lib/sourceColors.ts` (both `SOURCE_COLORS` and `SOURCE_BADGE_COLORS`)
3. Update the gallery-dl settings tab color to match
4. Do NOT define local `SOURCE_COLORS` in any page or component — always import from `@/lib/sourceColors`

## Anti-patterns

- ❌ Defining `const SOURCE_COLORS = {...}` in individual pages
- ❌ Using different hex codes for the same source in different pages
- ❌ Using Tailwind color classes for charts and hex colors for badges (or vice versa) — badges use Tailwind, charts use hex, but both come from the same canonical module
- ❌ Changing a source's color in one place without updating `sourceColors.ts`
