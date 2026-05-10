#!/usr/bin/env bash
# analyze.sh — extract the 8 canonical numbers from FIO JSON and print
# the Measured table in markdown, with a comparison-to-spec column
# computed from SPEC_* env vars.
#
# Reads from out/*.json (relative to script dir). Output goes to stdout.
#
# CONFIG (defaults match the worked-example drive; override via env vars):
#   SPEC_SEQWRITE_MBS=13400
#   SPEC_SEQREAD_MBS=14800
#   SPEC_RANDREAD_IOPS=2200000
#   SPEC_RANDWRITE_IOPS=2600000

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="$SCRIPT_DIR/out"

: "${SPEC_SEQWRITE_MBS:=13400}"
: "${SPEC_SEQREAD_MBS:=14800}"
: "${SPEC_RANDREAD_IOPS:=2200000}"
: "${SPEC_RANDWRITE_IOPS:=2600000}"

# Helpers
bw_mbs() {  # bw_mbs <json> <read|write>
    jq -r ".jobs[0].$2.bw_bytes // 0 | . / 1e6 | round" "$1"
}
iops() {
    jq -r ".jobs[0].$2.iops // 0 | round" "$1"
}
p99_us() {
    jq -r ".jobs[0].$2.clat_ns.percentile.\"99.000000\" // 0 | . / 1000 | round" "$1"
}
pct_of() {  # pct_of <actual> <target>
    awk -v a="$1" -v t="$2" 'BEGIN { if (t==0) print "—"; else printf "%d%%\n", a/t*100 }'
}

j() { echo "$OUT_DIR/$1.json"; }

# --- Print Measured table -------------------------------------------------
cat <<'EOF'
| Job | Throughput / IOPS | p99 lat | % of spec |
| --- | --- | --- | --- |
EOF

v=$(bw_mbs "$(j 01-seqwrite-1t)" write)
echo "| seqwrite 1t 1MB | ${v} MB/s | — | $(pct_of "$v" "$SPEC_SEQWRITE_MBS") |"

v=$(bw_mbs "$(j 02-seqwrite-16t)" write)
echo "| seqwrite 16t 1MB | ${v} MB/s | — | $(pct_of "$v" "$SPEC_SEQWRITE_MBS") |"

v=$(bw_mbs "$(j 03-seqread-1t)" read)
echo "| seqread 1t 1MB | ${v} MB/s | — | $(pct_of "$v" "$SPEC_SEQREAD_MBS") |"

v=$(bw_mbs "$(j 04-seqread-16t)" read)
echo "| seqread 16t 1MB | ${v} MB/s | — | $(pct_of "$v" "$SPEC_SEQREAD_MBS") |"

i=$(iops   "$(j 05-randread-4k-qd64)" read)
p=$(p99_us "$(j 05-randread-4k-qd64)" read)
echo "| randread 4k QD64 | ${i} IOPS | ${p} µs | $(pct_of "$i" "$SPEC_RANDREAD_IOPS") |"

i=$(iops   "$(j 06-randwrite-4k-qd64)" write)
p=$(p99_us "$(j 06-randwrite-4k-qd64)" write)
echo "| randwrite 4k QD64 | ${i} IOPS | ${p} µs | $(pct_of "$i" "$SPEC_RANDWRITE_IOPS") |"

i=$(iops   "$(j 07-randread-4k-qd1)" read)
p=$(p99_us "$(j 07-randread-4k-qd1)" read)
echo "| randread 4k QD1 (latency floor) | ${i} IOPS | **${p} µs** | — |"

r=$(bw_mbs "$(j 08-rw7030)" read)
w=$(bw_mbs "$(j 08-rw7030)" write)
agg=$((r+w))
echo "| mixed 70/30 1MB 8t | ${r} R + ${w} W = ${agg} MB/s | — | — |"

echo
fio_ver=$(fio --version 2>/dev/null || echo "unknown")
drv=$(cat /sys/block/nvme0n1/device/model 2>/dev/null || echo "unknown")
fw=$(cat /sys/block/nvme0n1/device/firmware_rev 2>/dev/null || echo "unknown")
echo "_${fio_ver}, drive ${drv}, firmware ${fw}_"

# --- Per-second bandwidth log hint ---------------------------------------
cat <<EOF

Per-second bandwidth logs (write jobs only) are in $OUT_DIR/*_bw.*.log.
Plot any of them to see the SLC fall-off curve. Example one-liner:

    awk -F, '{print \$1/1000, \$2/1024}' $OUT_DIR/02-seqwrite-16t_bw.0.log | head -200

(column 1 = seconds, column 2 = MiB/s)
EOF
