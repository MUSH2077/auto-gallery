# Gallery design tokens

The gallery presentation is shared by login, the library, tasks, details and
management pages. Preserve its typography, centered layout, palette and
interaction states when changing business behavior.

## Sources

- `src/features/gallery-ui/gallery-ui.css`: shared controls and layouts.
- `src/features/gallery-ui/app.css`: application styles, semantic color tokens
  and the dark theme.
- `src/features/gallery-ui/typography.css`: active text roles, sizes and weights.
- `src/features/gallery-ui/latin-fonts.css` and `fonts.css`: local font assets.
- `src/lib/themePreference.ts` and `src/lib/theme.tsx`: defaults and persistence.
- `tailwind.config.ts`: utilities backed by `--ag-*` semantic tokens.

Apply the application and typography stylesheets together. The base stylesheet
alone does not describe the effective typography or color hierarchy.

## Color and themes

Light is the default. Users may explicitly choose dark or system mode. There
is one gallery palette with light and dark variants; named palette switching
through `data-theme` has been removed.

| Role | Light | Dark |
|---|---|---|
| Canvas | `#edf1ed` | `#29322c` |
| Surface | `#f8faf7` | `#333e36` |
| Subtle surface | `#e0e7df` | `#3c493f` |
| Text | `#26382e` | `#edf2e9` |
| Border | `#c6d1c6` | `#596b5c` |
| Accent | `#45644e` | `#bed1b6` |
| Danger | `#923b31` | `#f1b5a5` |

Use semantic tokens for business components: `bg-canvas`, `bg-surface`,
`bg-subtle`, `text-fg`, `text-muted`, `border-border`, `text-accent`,
`text-success`, `text-warning` and `text-danger`. The gallery maps these
utilities to its own `--ag-*` RGB triplets. Secondary text uses the same readable
ink; spacing, size and placement provide hierarchy instead of low contrast.

## Typography

Keep the Italianno wordmark independent of content typography. English and
numbers use Fraunces; Chinese content headings use Noto Serif SC and functional
UI uses LXGW WenKai. Keep the local font assets and their licenses together.

| Role | Size / line height |
|---|---|
| Auxiliary information | 14px / 22px |
| Controls and detail facts | 16px / 26px |
| Body and task names | 18px / 28px |
| Work titles | 22px / 32px |
| Page headings | 28px / 38px |
| Login heading | 33px / 44px |

Functional UI uses the established 600 weight. Preserve role-specific overrides
in `typography.css`, including task names and the wordmark. Detail labels and
values share one text role and align on their first baseline. Updating numbers
and comparison columns use tabular numerals.

## Layout and interaction

Keep the centered brand and content composition. The bottom bar switches
top-level pages; search, sort, filters and display controls remain next to their
content. Reuse shared gallery controls and business page wrappers.

Actions use buttons and navigation uses links. Preserve visible keyboard focus,
native dialogs, Escape and outside dismissal where applicable, focus restoration
and confirmation for destructive actions. Loading, empty and error states
remain distinct, with retry where recovery is possible.

Scope transitions to their actual properties and honor reduced motion. Wait
for stable rendered states before visual or contrast assertions. Preserve the
established radii, overlay shadows and state colors in the shared styles.
