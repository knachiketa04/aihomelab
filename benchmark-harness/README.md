# benchmark-harness

Reusable, declarative harness for unattended storage benchmarking across multi-node clusters. Define scenarios in YAML, walk away for two days, return to a SQLite database of normalized metrics and a markdown comparison report.

**Status:** v0 in progress. This README is a stub — full architecture, install, and usage land at step 9 of the implementation plan. The project plan is at [.claude/plans/context-background-so-sleepy-peach.md](../.claude/plans/context-background-so-sleepy-peach.md) (local-only).

## What v0 covers

- **fio** runner, parser, and SQLite store, end-to-end.
- Declarative scenario library in `scenarios/`.
- Crash-resume for long unattended campaigns.
- Markdown comparison reports.

MLPerf Storage support is stubbed at the runner interface and lands in v0.1.

## License

Apache-2.0. See [LICENSE-CODE](../LICENSE-CODE) at the repository root.
