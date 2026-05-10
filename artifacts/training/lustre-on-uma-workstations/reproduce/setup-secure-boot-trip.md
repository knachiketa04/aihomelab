# Secure Boot disable — physical-access procedure

If the host runs a vendor-signed kernel with `lockdown=integrity` (or stricter), locally-built ZFS / Lustre modules will fail to load with `Key was rejected by service`. The kit defaults to **disabling Secure Boot** in firmware. Production-style alternative (MOK enrollment + module signing) is briefly noted at the end.

**Required for**: any host where `cat /sys/kernel/security/lockdown` does NOT show `[none]` after `setup-zfs-build.sh` finishes. The script's `modprobe zfs` failure path prints the same hint.

**Note on hardware without a BMC** (e.g., DGX Spark): UEFI menu interaction requires **physical** keyboard + monitor at the box. Plan accordingly. Other UMA platforms with iLO / iDRAC / Redfish can drive the same procedure remotely.

## Procedure (per host)

### 1. Capture pre-trip state (over SSH)

```bash
mkdir -p /home/$USER/lustre-on-uma-reproduce/logs
sudo mokutil --sb-state          | tee /home/$USER/lustre-on-uma-reproduce/logs/sb-state-before.txt
cat /sys/kernel/security/lockdown | tee /home/$USER/lustre-on-uma-reproduce/logs/lockdown-before.txt
```

Expect: `SecureBoot enabled` and `[integrity]` (or stricter).

### 2. Reboot directly into UEFI firmware menu (over SSH)

```bash
sudo systemctl reboot --firmware-setup
```

The host reboots and lands in the UEFI menu — no POST keypress timing required. SSH session drops.

### 3. At the keyboard / monitor

1. Navigate to **Security** (or **Boot** depending on the platform's UEFI layout — try Advanced or System Configuration if not under Security).
2. Set **Secure Boot** to **Disabled**.
3. Save & Exit (typically F10, follow the on-screen legend).
4. Host reboots normally.

If the firmware presents a blue **MOK Manager** screen during the disable sequence, **accept** the Secure Boot change confirmation — don't dismiss.

### 4. Verify post-trip (SSH back in)

```bash
ssh <user>@<host> 'sudo mokutil --sb-state ; cat /sys/kernel/security/lockdown'
```

Expect: `SecureBoot disabled` and `[none] integrity confidentiality` (the brackets around `none` indicate the active value).

### 5. Re-run `setup-zfs-build.sh`

The script's `modprobe zfs` step should now succeed. Re-running is idempotent — most steps are no-ops; only the `modprobe` proceeds further than before.

## If `lockdown` still shows `[integrity]` after Secure Boot is disabled

Some vendor kernels enforce lockdown independently of Secure Boot. Apply the GRUB cmdline override:

```bash
sudo cp /etc/default/grub /etc/default/grub.bak
sudo sed -i 's/^GRUB_CMDLINE_LINUX="/GRUB_CMDLINE_LINUX="lockdown=none /' /etc/default/grub
grep '^GRUB_CMDLINE_LINUX' /etc/default/grub   # verify the edit
sudo update-grub
sudo reboot
```

After reboot:

```bash
cat /proc/cmdline | grep -o 'lockdown=[a-z]*'   # expect: lockdown=none
cat /sys/kernel/security/lockdown               # expect: [none] integrity confidentiality
```

## Alternative: MOK enrollment + module signing (production-style)

Preserves Secure Boot integrity guarantees but adds operational overhead. Outline:

1. Generate a Machine Owner Key pair locally.
2. `sudo mokutil --import <pubkey>` → enrolls on next boot via MOK Manager (also requires physical access for the boot-time confirmation prompt).
3. Sign every newly-built `.ko` with the private key in your build pipeline.
4. Re-sign after every kernel module rebuild (every Lustre/ZFS source update).

Out of scope for this kit; document for production deployments.

## Revert (when tearing down)

```bash
# If lockdown=none cmdline was added:
sudo cp /etc/default/grub.bak /etc/default/grub
sudo update-grub

# Re-enable Secure Boot via the same firmware-setup procedure (steps 2 + 3).
sudo systemctl reboot --firmware-setup
# In firmware menu: Security → Secure Boot → Enabled → Save & Exit.
```

After: `mokutil --sb-state` reports `SecureBoot enabled`, `lockdown` returns to `[integrity]`. The vendor kernel + signed in-tree modules continue to work; only locally-built unsigned modules become unloadable again.
