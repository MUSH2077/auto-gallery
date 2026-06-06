# Frontend Discipline Constraint

## Rule

Frontend code must follow the same strict patterns as backend code. No duplicated constants, no orphaned API types, no untranslated text.

## Shared modules (single source of truth)

| Module | Purpose | Must be imported, never duplicated |
|--------|---------|-------------------------------------|
| `@/lib/api.ts` | API client, types, query keys | All API calls and types |
| `@/lib/i18n.tsx` | i18n keys, `useT()` hook | All user-facing text |
| `@/lib/sourceColors.ts` | Source color hex + badge classes | All source-colored UI elements |

## Component rules

### 1. Import shared constants, never redefine

BAD:
```typescript
// ❌ Defining SOURCE_COLORS in a page component
const SOURCE_COLORS: Record<string, string> = { pixiv: "#0066FF", ... };
```

GOOD:
```typescript
// ✓ Import from canonical module
import { SOURCE_COLORS, getSourceColor } from "@/lib/sourceColors";
```

### 2. Every visible string must use i18n

BAD:
```tsx
<span>Source Accounts</span>
<button>Create Backup</button>
```

GOOD:
```tsx
<span>{t("creator_detail.source_accounts")}</span>
<button>{t("backup.create")}</button>
```

When adding new text:
1. Add the key to `i18n.tsx` in BOTH `zh` and `en` dictionaries
2. Use `namespace.natural_name` key format
3. Use the `t()` function in components

### 3. API types must match backend schemas

When a backend endpoint adds/removes/renames a field:
1. Update the TypeScript type in `api.ts`
2. Update all components that use that type
3. Run `npx next build` to catch type errors

### 4. Thin pages, reusable components

- Pages under `app/admin/` should be thin orchestration layers
- Extract reusable UI patterns to `components/`
- Common patterns already available: `PageHeader`, `StatusBadge`, `SourceBadge`, `EmptyState`, `ErrorState`, `ConfirmDialog`, `WorkGrid`

### 5. TanStack Query conventions

- Query keys: use `queryKeys.*` from `api.ts`
- Mutations: invalidate affected queries on success
- Loading state: always handle, use skeleton components not spinners
- Error state: always handle, use `ErrorState` component with retry
- Empty state: always handle, use `EmptyState` component with action

### 6. No new navigation items without explicit instruction

Per CLAUDE.md Frontend Modification Discipline:
- Don't add new pages, links, buttons, or cards unless explicitly asked
- When unsure which page to modify, ASK the user

### 7. Import order convention

```typescript
// 1. React/Next.js imports
"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";

// 2. Third-party imports
import { useQuery, useMutation } from "@tanstack/react-query";

// 3. Local imports
import { api, queryKeys } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { getSourceColor } from "@/lib/sourceColors";
import { PageHeader, StatusBadge } from "@/components";
```

### 8. Dark mode

Every UI component must render correctly in both light and dark modes:
- Use `dark:` Tailwind prefixes for all colors, backgrounds, borders
- Test in both modes before committing

## Anti-patterns

- ❌ `const SOURCE_COLORS = { pixiv: "#0066FF", ... }` in a page file
- ❌ Hardcoded Chinese/English strings instead of `t("key")`
- ❌ i18n key added to `zh` but not `en` (or vice versa)
- ❌ API type drift — changing backend without updating frontend types
- ❌ `import { useRouter } from "next/navigation"` placed after type definitions
- ❌ Inline styles for colors that should use canonical source colors
