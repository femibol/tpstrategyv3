---
description: Update HANDOFF.md with current branch state — commits, open work, gotchas — then commit + push
allowed-tools: Bash(git status:*), Bash(git log:*), Bash(git diff:*), Bash(git branch:*), Bash(git add:*), Bash(git commit:*), Bash(git push:*), Read, Edit, Write
---

Update `HANDOFF.md` so the next Claude Code session (local or web) can pick up without re-deriving context. Follow the file's existing structure exactly — match the heading style, dating convention, and tone of prior entries.

## Steps

1. **Survey the current state** by running these in parallel:
   - `git status --short` — uncommitted changes
   - `git branch --show-current` — current branch name
   - `git log main..HEAD --oneline` — commits on this branch not yet on main (if `main` doesn't exist as a ref, fall back to `origin/main`)
   - `git log -10 --oneline` — recent commit history for style reference
   - Read the current `HANDOFF.md`

2. **Compose the update.** Move any "Current In-Progress" section that's now shipped into "Recently Shipped" (create the section if missing). Write a fresh "Current In-Progress" section for the active branch covering:
   - What this branch does (1–3 sentences, why-focused not what-focused)
   - Specific files / line numbers worth knowing about
   - Any deploy steps the next session needs to run
   - Known gotchas, ruled-out paths, or follow-up work

3. **Update the "Last Updated" line** with today's date in the same format prior entries use (e.g. `2026-04-27 — <one-line summary>`). If multiple updates have happened today, append `(2)`, `(3)`, etc., matching the file's convention.

4. **Show the diff** with `git diff HANDOFF.md` and ask the user to confirm before committing. If they approve, commit with a message like `Update HANDOFF.md — <one-line summary>` and push to the current branch with `git push -u origin <branch>`.

## Notes

- Do NOT invent work that isn't reflected in commits or current diff. If `git log main..HEAD` is empty, say so and ask whether to skip or write a "no work yet" placeholder.
- Do NOT remove historical entries from "Recently Shipped" — only append.
- If on `main`, ask the user which branch they actually want to document before doing anything.
- Argument: $ARGUMENTS — optional one-line summary the user wants in the "Last Updated" header. If empty, generate one from the commits.
