# .githooks/

Tracked git hooks for this repo. One file today — `pre-commit` — runs `gitleaks` on staged content and blocks the commit on any finding.

## One-time setup (per clone)

```bash
git config core.hooksPath .githooks
```

This points git at this directory instead of the default `.git/hooks/`. After running it once on a clone, every `git commit` runs `pre-commit` automatically.

To verify the hook is active:

```bash
git config --get core.hooksPath   # should print: .githooks
```

## What `pre-commit` does

- Runs `gitleaks git --staged --redact` over the staged diff before the commit lands.
- Exits non-zero (blocking the commit) on any secret pattern hit.
- Prints redacted findings — never the secret value itself.

The brand-boundary check (employer names, marketing language) is **not** done here — that lives in `aihomelab-publication-gate`'s Phase 1 scan, run when an experiment is being considered for publication.

## Bypass

```bash
git commit --no-verify
```

Use sparingly and document why in the commit message. If the same false positive recurs, add a `gitleaks:allow` comment on the matching line, or tune `.gitleaks.toml` (not currently in repo).

## Dependencies

- `gitleaks` 8+ on PATH. macOS: `brew install gitleaks`.
- Hook fails fast with an install hint if gitleaks isn't found.
