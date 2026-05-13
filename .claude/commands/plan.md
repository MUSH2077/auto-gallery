# Plan Command

Invoke when the user asks to plan implementation work. This command reads all constraints and produces a step-by-step implementation plan.

## What this command does

1. Reads `.claude/constraints/*` to load all non-negotiable rules
2. Reads `CLAUDE.md` for architecture and phase context
3. Identifies which constraint files apply to the requested work
4. Produces a numbered implementation plan with specific files to create/modify
5. Flags any constraint conflicts before implementation begins

## Usage

```
/plan <feature description>
```

## Output format

- **Files to create** — with paths
- **Files to modify** — with paths and specific sections
- **Constraint check** — explicit confirmation that no constraints are violated
- **Step order** — numbered, dependency-aware sequence
- **Verification** — how to confirm the implementation works

## Constraint checklist (must pass before plan is final)

- [ ] No Pixiv-specific model names in core code
- [ ] Danbooru treated as reference only (unless Danbooru posts phase)
- [ ] Dedup defaults to OFF, no auto-delete/auto-merge
- [ ] NAS paths absent from application code
- [ ] `shell=False` for all subprocess calls
- [ ] URL validation at API boundary
- [ ] Admin routes behind auth
- [ ] No filesystem paths in API responses
- [ ] Docker Compose remains runnable
