---
name: ship-release
description: The release discipline for this repository — lint, tests, version bump, house-style CHANGELOG entry, docs sync, commit, push, CI check. Use this EVERY time you finish a code or docs change in this repo and are about to commit, when asked to "поднять версию", "оформи ченжлог", "release", or "ship it" — even for a one-line fix. Do not commit here without following it.
---

# Shipping a change

Every merged change in this repo is a numbered release with a changelog entry.
That is the product's public history and what Local Mode users read after
`git pull`, so the discipline is not optional ceremony.

## Checklist (in order)

1. **Lint and tests** — both must be clean before anything else:

   ```bash
   .venv/bin/ruff check .
   .venv/bin/pytest -q
   ```

   Use the `pytest` console script, not only `python -m pytest`: that is what
   CI runs (`pythonpath` is configured in pyproject.toml so both work — if
   `pytest` fails with `No module named 'app'`, that config was broken).

2. **Version bump** — `app_version` in `app/config.py`. Minor bump (0.X.0)
   for new features or behavior changes; patch (0.X.Y) for docs, process, and
   fixes. Every shipped change bumps the version — no "silent" commits.

3. **CHANGELOG.txt entry** — prepend above the previous version, matching the
   house style exactly:

   ```text
   Version 0.6.0 - 2026-07-10

   Group name:
   - Past-tense bullet explaining WHAT changed and WHY it matters to a user
     or an agent, with concrete file/endpoint names.
   - ...

   - Bumped the app version from 0.5.2 to 0.6.0.
   ```

   Bullets are detailed and self-contained — a reader should understand the
   release without opening the diff. Group related bullets under short
   headings when the release has more than ~5 bullets.

4. **Docs sync** — if the change touched any public surface, update the
   matching docs in the same commit: API endpoints → `README.md` +
   `docs/API.md`; MCP tools → `docs/MCP.md`; adapters → `adapters/README.md`;
   eval process → `evals/README.md`.

5. **Commit** — message starts with `Version X.Y.Z <short imperative summary>`
   (see `git log --oneline` for the established pattern), then bullet details.

6. **Push and verify CI** — after pushing to `main`, confirm the GitHub
   Actions run is green (it lints, tests, builds the Docker image, and smoke
   tests it):

   ```bash
   git push origin main
   gh run list --limit 1
   ```

   If CI fails, fixing it is part of the same release — do not leave `main`
   red.

7. **Tag the release** — once CI is green, tag so the Release workflow
   publishes a GitHub Release with notes extracted from CHANGELOG.txt
   (`scripts/release_notes.py`; a tag without a matching changelog section
   fails loudly):

   ```bash
   git tag vX.Y.Z && git push origin vX.Y.Z
   gh run list --workflow Release --limit 1
   ```

## Extra gates for specific surfaces

- Database schema touched → follow the `db-schema-change` skill first.
- MCP tool surface touched → follow the `mcp-tool-change` skill first
  (its evaluation step is part of the release).
