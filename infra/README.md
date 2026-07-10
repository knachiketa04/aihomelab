# infra
**Why:** Operational automation for the cluster: ordered, idempotent scripts that bring up, tear down, and check the Spark nodes and the Lustre filesystem.
**Solves:** Replaces ad-hoc multi-host SSH (zpool, modprobe, LNet, mounts, readiness probes) with repeatable scripts that enforce cross-host ordering.
**Built for:** The published Lustre-on-Spark cluster work and pre-experiment readiness checks.
**Status:** Split: readiness checks ACTIVE; Lustre orchestration DORMANT since 2026-07-10, retained as the revival path for the dormant Lustre pools.
**How to use:** Driven by skills: aihomelab-lustre-cluster runs scripts/lustre-cluster.sh (bringup | teardown | status); aihomelab-cluster-ready runs scripts/check-cluster-readiness.sh.
