# infra
**Why:** Operational automation for the cluster: ordered, idempotent scripts that bring up, tear down, and check the Spark nodes and the Lustre filesystem.
**Solves:** Replaces ad-hoc multi-host SSH (zpool, modprobe, LNet, mounts, readiness probes) with repeatable scripts that enforce cross-host ordering.
**Built for:** The Lustre-on-Spark work (experiment 014) and pre-experiment readiness checks.
**Status:** ACTIVE
**How to use:** Driven by skills: aihomelab-lustre-cluster runs scripts/lustre-cluster.sh (bringup | teardown | status); aihomelab-cluster-ready runs scripts/check-cluster-readiness.sh. See scripts/script-inventory.md for the catalog.
