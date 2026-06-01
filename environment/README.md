# environment
**Why:** Holds the lab's hardware and network ground truth: node inventory, topology, and the validation probes that confirm the cluster matches that spec.
**Solves:** Keeps cluster facts (node names, addresses, fabric, disk layout) in one referenced place so experiments and skills don't guess or hardcode them.
**Built for:** The two-node DGX Spark cluster (spark01/spark02); referenced from CLAUDE.md "Start Here".
**Status:** ACTIVE
**How to use:** Read cluster-env.md for node and network details, cluster-validation.md for the probe list and bootstrap path, cluster-topology.svg for the wiring diagram. The aihomelab-cluster-ready skill and infra/scripts/check-cluster-readiness.sh build on these facts.
