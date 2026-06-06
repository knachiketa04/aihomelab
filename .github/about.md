# .github
**Why:** Holds GitHub-native CI for this repo: the workflow that validates and tests the benchmark-harness on push and pull request.
**Solves:** Catches benchmark-harness regressions (scenario-schema validation, test suite) automatically in CI rather than at experiment time.
**Built for:** The benchmark-harness module (experiment 021 harness validation).
**Status:** ACTIVE
**How to use:** workflows/benchmark-harness-ci.yml runs on changes touching the harness. This directory is for CI only; agent infrastructure (skills, subagents, hooks) lives under .claude/, not here.
