# Contributing to seqledger

Thanks for helping. This page covers the branch model, tests, code rules, and
commit style.

## Branch model

- `main` holds releases. `devel` is the staging branch.
- Start every feature or fix on a new working branch off `devel`:
  `git switch devel && git switch -c <name>`.
- When the work is done and tested, merge it into `devel` with a plain merge:
  `git switch devel && git merge --no-ff <name>`, then delete the branch.
- Only `devel` is pushed. Working branches stay local (or go up as a PR
  against `devel` if you are contributing from a fork).
- A release is a PR from `devel` to `main`. The maintainer decides when to
  release and bumps the version in `pyproject.toml`. After it merges, `main`
  is merged back into `devel`.

## Running tests

Set up an environment with either:

    pip install -e .[test]

or

    conda env create -f environment.yml

Then run, from the repository root:

    python -m pytest -q

All tests must pass before a merge into `devel`.

## Code rules

- Core modules (everything except the GUI) use the Python standard library
  only. GUI dependencies belong in the `gui` extra.
- ASCII only in code and docs. No em dashes.
- Keep comments minimal. Say what is not obvious; do not narrate a bug fix.
- Keep changes narrow: fix the problem at hand, avoid drive-by refactors.

## Changelog

Every user-visible change gets a bullet in `CHANGELOG.md` under
`## [Unreleased]`, in the Added, Changed, Removed, or Fixed subsection.

## Schema changes

- The catalog schema lives in `seqledger/schema.sql`. Edit it there.
- There are no in-place migrations: add a CHANGELOG bullet that describes the
  change and tells users to back up the catalog before running `init-db`.

## Commit messages

- Short title, for example `ingest: skip empty mapfile rows`.
- Add a body only when the reason is not obvious. Describe the result, not
  the debugging.
- No attribution or co-author lines.
