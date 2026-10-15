# seqledger project instructions

## Git workflow (devel staging branch)

- `devel` is the staging branch. All features and bug fixes start on a new working branch off `devel` (`git switch devel && git switch -c <name>`), never off `main`.
- When the work is done, tested, and the user agrees, merge it into `devel` with a plain merge: `git switch devel && git merge --no-ff <name>`. Then delete the local working branch (`git branch -d <name>`); no need to ask.
- Only `devel` is ever pushed. Never push working branches. Push `devel` only when the user asks.
- Release: only when the user decides. Bump `version` in `pyproject.toml`, turn the CHANGELOG.md `## [Unreleased]` heading into the release heading (`## [x.y.z] - YYYY-MM-DD`), then open a PR from `devel` to `main`. After it merges, bring `main` back into `devel` (`git switch devel && git merge main`) so they don't drift.
- Keep `devel` current: after anything lands on `main` (release PR, direct fix), merge `main` into `devel`.
- New CHANGELOG.md entries go under `## [Unreleased]` at the top, in the Added / Changed / Fixed subsections.
- Override for small direct-to-main changes: only when the user explicitly says so in the current request. Prefix the git command with `ALLOW_MAIN=1` (e.g. `ALLOW_MAIN=1 git commit ...`, `ALLOW_MAIN=1 git push origin main`). Afterwards, merge `main` into `devel`.
- A PreToolUse hook (`.claude/hooks/git_guard.sh`, registered in `.claude/settings.json`) blocks pushes of anything but `devel`, tag pushes, and commit/merge/rebase/cherry-pick on `main`, unless `ALLOW_MAIN=1` is in the command.
- Branches that predate this workflow (`fix/*`, `gui-*`, `add-worms-taxonomy`) are merged or archived; don't build on them.

## Database schema changes

- The schema lives in `seqledger/schema.sql`; `init-db` applies it and runs in-place migrations in `seqledger/db.py`. Any table or column change edits both, and the migration must be idempotent (safe to re-run on an already-migrated catalog).
- Document every schema change in CHANGELOG.md and remind users to back up the catalog before `init-db`.

## Testing

- Run `python -m pytest -q` before merging into `devel`. Core modules are stdlib-only; the GUI and tests need the `seqledger` conda env (`environment.yml`).
