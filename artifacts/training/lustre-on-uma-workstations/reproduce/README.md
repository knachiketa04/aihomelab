# Lustre on UMA workstations — reproduce kit

Reproduces a three-pillar storage characterization of minimum-viable distributed Lustre on two UMA workstations with stock single-NVMe layouts:

1. **Three-knob ZFS+Lustre trio** (`primarycache=metadata`, `atime=off`, `obdfilter.brw_size=4`) lifts default 5× / 32× / 400× on bulk / small-block / random IO. Default Lustre+ZFS on stock-UMA is unusable; tuned recovers ~85% of loopback ceiling.
2. **Cross-node Lustre is ~6× slower than NFSoRDMA on cached reads** on identical hardware (architectural — RPC framing + LDLM + osd-zfs + ko2iblnd stack depth, not pipeline depth).
3. **2-node distributed Lustre + concurrent multi-client** delivers 1.35 GB/s aggregate writes / 9.32 GB/s aggregate reads — within 60% / 85% of single-node loopback ceiling. Distributed-FS win condition is concurrent multi-client (FSDP / multi-rank), not single-client.

Numbers in [`expected-output.md`](expected-output.md).

## Environment

- 2× UMA workstations, RDMA fabric pair (RoCE/IB) cross-connected, ≥121 GiB unified memory each, ≥800 GB free local NVMe each.
- Linux ARM64 kernel 6.x; UEFI Secure Boot off (kit defaults — see [`setup-secure-boot-trip.md`](setup-secure-boot-trip.md)).
- `sudo` on both hosts; passwordless SSH between them; build deps installed by `setup-zfs-build.sh` / `setup-lustre-build.sh`.

## Files

| File | Purpose | Runtime |
| --- | --- | --- |
| `setup-zfs-build.sh` | OpenZFS 2.4.1 from source + `Module.symvers` symlink workaround. **Both hosts.** | ~30–45 min |
| `setup-lustre-build.sh` | Lustre master tip from source (server+client). **Both hosts.** | ~25–45 min |
| `setup-secure-boot-trip.md` | Doc-only. Physical UEFI procedure if kernel is signed. | ~10 min/host |
| `verify-modules-loaded.sh` | Post-trip module load + version check. **Both hosts.** | <1 min |
| `setup-lnet.sh` | Configure `o2ib0` net on the RDMA NIC. **Both hosts.** | <1 min/host |
| `setup-host1-osts.sh` | MGS + MDT0000 + OST0000 (host 1). | ~5 min |
| `setup-host2-ost.sh` | OST0001 (host 2). Registers with host 1 MGS. | ~3 min |
| `setup-runtime-tunables.sh` | Knob trio + `lctl set_param -P` for persistence. **Both hosts.** | <1 min |
| `run-single-node-tuning.sh` | Pillar 1 — fio battery on host 1 local. | ~14 min |
| `run-cross-node-baseline.sh` | Pillar 2 — fio battery on host 2 over o2ib. | ~14 min |
| `run-distributed-concurrent.sh` | Pillar 3 — concurrent battery, both clients, 2-way striped. | ~14 min |
| `analyze-fio.py` | Parse fio outputs → comparison table. | <30 sec |
| `expected-output.md` | Reference numbers + pass criteria. | — |

## Run order

1. `setup-zfs-build.sh` (both hosts, parallel).
2. `setup-lustre-build.sh` (both hosts, after Step 1).
3. Secure Boot trip per `setup-secure-boot-trip.md` (physical access).
4. `verify-modules-loaded.sh` (both hosts).
5. `setup-lnet.sh` on both hosts. Verify with `lctl ping <peer-NID>@o2ib0`.
6. `setup-host1-osts.sh` → `setup-host2-ost.sh`. Confirm `lfs osts` shows both OSTs ACTIVE.
7. `setup-runtime-tunables.sh` (both hosts). **Skip and Pillar 3 collapses to ~0.49 GB/s.**
8. `run-single-node-tuning.sh` (host 1) → `run-cross-node-baseline.sh` (host 2) → `run-distributed-concurrent.sh` (control).
9. `analyze-fio.py`.

## Tunables (env vars)

| Var | Default | Notes |
| --- | --- | --- |
| `EXP_ROOT` | `/home/$USER/lustre-on-uma-reproduce` | Working dir |
| `BUILD_ROOT` | `${EXP_ROOT}/build` | Source trees |
| `LUSTRE_COMMIT` | `805cece6747f442449f32a1d25a8b8a03b230875` | Master tip validated against kernel 6.17 |
| `ZFS_TAG` | `zfs-2.4.1` | OpenZFS git tag |
| `HOST1_QSFP_IP` / `HOST2_QSFP_IP` | `169.254.188.115` / `169.254.10.122` | Link-local |
| `RDMA_IFACE` | `enp1s0f0np0` | RDMA NIC name |
| `LUSTRE_FSNAME` | `lustrefs` | FS name (any 1–8 chars) |
| `OST_IMG_SIZE` / `MDT_IMG_SIZE` / `MGS_IMG_SIZE` | `600G` / `50G` / `2G` | Image-file pre-allocation |
| `ZFS_ARC_MAX_BYTES` | `8589934592` (8 GiB) | **Load-bearing** for knob-trio findings |
| `HOST1_SSH` / `HOST2_SSH` | `host1` / `host2` | Passwordless-SSH targets used by `run-distributed-concurrent.sh` (override per your setup) |
| `NVME_DEVICE` | `nvme0n1` | Local NVMe |

## Cherry-picking

- Build recipe (`setup-zfs-build.sh` + `setup-lustre-build.sh` + `verify-modules-loaded.sh`) — full Lustre-on-modern-kernel build with all six obstacle workarounds, reusable standalone.
- `setup-runtime-tunables.sh` — knob trio + `set_param -P` recipe, reusable for any Lustre+ZFS deployment.
- `analyze-fio.py` — fio output → tabular comparison, reusable for any multi-config fio characterization.

`setup-host*-ost*.sh` and `run-*.sh` are kit-specific.

## Verification (pass criteria, ±10–15% drift OK)

- **Pillar 1**: 1 MiB seq write ≥ 2.0 GB/s; 64 KiB write ≥ 1.8 GB/s; 4 KiB random ≥ 150K IOPS each direction.
- **Pillar 2**: 1 MiB seq write 0.4–0.6 GB/s; 1 MiB seq read 2.0–2.5 GB/s. Slower than Pillar 1 by design.
- **Pillar 3 (aggregate)**: 1 MiB write ≥ 1.2 GB/s; 1 MiB read ≥ 8.5 GB/s; per-client symmetry within ~5%.

Pillar 1 below threshold → knob trio didn't apply. Pillar 3 below threshold while 1+2 are fine → runtime tunables didn't survive remount; re-run Step 7.

## Post-reboot recovery (kit gotcha)

This kit does NOT install boot persistence. After any host reboot, Lustre must be brought back up manually. On each affected host (`-d` arg points to file-backed-vdev directory; required because file-vdev pools aren't in `/etc/zfs/zpool.cache` by default):

```bash
sudo zpool import -d /var/lib/lustre-pools -a
sudo modprobe zfs lnet lustre osd_zfs
sudo lnetctl lnet configure
sudo lnetctl net add --net o2ib0 --if "$RDMA_IFACE"

# host 1 (full server):
sudo mount -t lustre mgs-pool/mgs   /mnt/mgt
sudo mount -t lustre mdt0-pool/mdt0 /mnt/mdt0
sudo mount -t lustre ost0-pool/ost0 /mnt/ost0
sudo mount -t lustre "${HOST1_QSFP_IP}@o2ib0:/${LUSTRE_FSNAME}" /mnt/lustre

# host 2 (OST + client):
sudo mount -t lustre ost1-pool/ost1 /mnt/ost1
sudo mount -t lustre "${HOST1_QSFP_IP}@o2ib0:/${LUSTRE_FSNAME}" /mnt/lustre
```

Runtime tunables (`brw_size`, `max_rpcs_in_flight`, etc.) **persist** via the MGS config log written by `setup-runtime-tunables.sh`'s `lctl set_param -P` — empirically validated through reboot. Re-running `setup-runtime-tunables.sh` after reboot is idempotent but not strictly needed.

## Out of scope

- Realistic AI training workloads (no `--direct=1`, `primarycache=all`, larger ARC).
- Multi-rail LNet (blocked by upstream `lnetctl` ARM64 bug at kit-build time).
- ZFS Direct IO (`direct=always`) — tested, -5 to -8% regression on file-backed substrate.
- Raw-partition zpools — would require destructive root-FS shrink.
