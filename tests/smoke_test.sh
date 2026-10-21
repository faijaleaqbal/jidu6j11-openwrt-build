#!/bin/sh
# End-to-end smoke test: simulate a fresh first boot of the firmware on a
# fake router, then walk the user through the ISP switch and the identity
# report, and check the U-Boot environment ends up in the right state.
#
# This is the "does the whole thing hang together" check, on top of the
# per-script unit tests.
set -e

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/bin"
mkdir -p "$BIN"

# --- stubs -----------------------------------------------------------------
cat > "$BIN/uci" <<'EOF'
#!/bin/sh
STATE="${FAKE_UCI_STATE:?}"
args=""
for a in "$@"; do case "$a" in -*) ;; *) args="$args|$a";; esac; done
set -- $(printf '%s' "$args" | tr '|' ' ')
case "${1:-}" in
  set)   shift; K=""; for a in "$@"; do K="$a"; done
         printf '%s\n' "${K%%=*}=${K#*=}" >> "$STATE";;
  delete) shift; for a in "$@"; do K="$a"; done
         grep -v "^${K}=" "$STATE" > "$STATE.t" 2>/dev/null || true; mv "$STATE.t" "$STATE";;
  get)   shift; K=""; for a in "$@"; do K="$a"; done
         v=$(grep -F "${K}=" "$STATE" 2>/dev/null | tail -1 | cut -d= -f2-); [ -n "$v" ] || exit 1; echo "$v";;
  show)  shift; S=""; for a in "$@"; do S="$a"; done
         if [ -n "$S" ]; then grep "^${S}\." "$STATE" 2>/dev/null || true; else cat "$STATE"; fi;;
  commit) ;;
esac
EOF

cat > "$BIN/fw_printenv" <<'EOF'
#!/bin/sh
STATE="${FAKE_FW_STATE:?}"
if [ "${1:-}" = "-n" ]; then
  v=$(grep "^${2:-}=" "$STATE" 2>/dev/null | tail -1 | cut -d= -f2-); [ -n "$v" ] || exit 1; echo "$v"; exit 0
fi
if [ -n "${1:-}" ]; then grep "^${1}=" "$STATE" 2>/dev/null || exit 1; else cat "$STATE"; fi
EOF

cat > "$BIN/fw_setenv" <<'EOF'
#!/bin/sh
STATE="${FAKE_FW_STATE:?}"
printf '%s=%s\n' "$1" "${2:-}" >> "$STATE"
EOF

cat > "$BIN/reboot" <<'EOF'
#!/bin/sh
echo REBOOT >> "$REBOOT_LOG"
EOF

chmod +x "$BIN"/*
export PATH="$BIN:$PATH"
export FAKE_UCI_STATE="$TMP/uci.state"
export FAKE_FW_STATE="$TMP/fw.state"
export REBOOT_LOG="$TMP/reboot.log"

# --- fake router state -----------------------------------------------------
cat > "$FAKE_UCI_STATE" <<'EOF'
network.lan.proto=static
network.lan.ipaddr=192.168.1.1
network.lan.netmask=255.255.255.0
network.wan.proto=dhcp
EOF

cat > "$FAKE_FW_STATE" <<'EOF'
bootcmd=ubi part ubi; ubi read 0x44000000 kernel; bootm 0x44000000
mtdparts=mtdparts=nand0:0xe100000@0x1700000(ubi)
jidu_boot_attempts=0
EOF

# --- fake /proc ------------------------------------------------------------
PROC="$TMP/proc"
mkdir -p "$PROC/device-tree"
printf 'JioRouter AX6000 (JIDU6J11-6111)\0' > "$PROC/device-tree/model"
printf 'jiorouter,ax6000-jidu6j11-6111\0jiorouter,ax6000-jidu6j11\0qcom,ipq9574\0' > "$PROC/device-tree/compatible"
printf 'processor\t: 0\nmodel name\t: Qualcomm IPQ9574\n' > "$PROC/cpuinfo"
printf 'MemTotal:       512000 kB\n' > "$PROC/meminfo"
cat > "$PROC/mtd" <<'EOF'
mtd0: 01700000 0e100000 "ubi"
mtd1: 00100000 00100000 "0:ART"
mtd2: 00200000 00100000 "0:BDF"
mtd3: 00300000 00100000 "MFG"
mtd4: 00400000 00100000 "Jio-Reserved"
EOF
printf 'console=ttyMSM0,115200n8 ubi.mtd=ubi root=/dev/ubiblock0_1 rootfstype=squashfs\n' > "$PROC/cmdline"

# --- fake firmware tree ----------------------------------------------------
FW="$TMP/fwroot"
for c in IPQ9574 QCN9074; do
  mkdir -p "$FW/$c/hw1.0"
  dd if=/dev/zero of="$FW/$c/hw1.0/board-2.bin" bs=1024 count=4 2>/dev/null
done

export JIDU_PROC="$PROC"
export JIDU_SYS="$TMP/sys"
export JIDU_FIRMWARE="$FW"

FILES="$(dirname "$0")/../files"
pass=0; fail=0
ok()   { echo "  ok   $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL $1"; fail=$((fail+1)); }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (got '$2', want '$3')"; fi; }
# has() - assert that a string contains a pattern
has() {
  case "$2" in
    *"$3"*) ok "$1" ;;
    *)      bad "$1 (missing '$3')" ;;
  esac
}

echo "=== 1. identity report ==="
OUT=$(sh "$FILES/usr/bin/jidu-identify.sh" 2>&1)
has "model detected" "$OUT" "JIDU6J11-6111"
has "RAM reported" "$OUT" "RAM: 500 MiB"
has "BDF listed" "$OUT" "board-2.bin"
has "boot counter" "$OUT" "jidu_boot_attempts"
has "active slot" "$OUT" "active slot: ubi"

echo "=== 2. switch to a non-Jio ISP (PPPoE) ==="
sh "$FILES/usr/bin/jidu-isp.sh" pppoe "myuser@isp" "mypass" >/dev/null
check "wan proto"    "$(uci get network.wan.proto)"    "pppoe"
check "wan username" "$(uci get network.wan.username)" "myuser@isp"
check "wan password" "$(uci get network.wan.password)" "mypass"
check "firewall nat" "$(uci get firewall.@zone[1].masq)" "1"

echo "=== 3. switch to a static IP ==="
sh "$FILES/usr/bin/jidu-isp.sh" static 203.0.113.10 255.255.255.0 203.0.113.1 8.8.8.8 >/dev/null
check "static proto"  "$(uci get network.wan.proto)"    "static"
check "static ip"     "$(uci get network.wan.ipaddr)"   "203.0.113.10"
check "static gw"     "$(uci get network.wan.gateway)"  "203.0.113.1"
check "old username gone" "$(uci get network.wan.username 2>/dev/null || echo GONE)" "GONE"

echo "=== 4. show output ==="
OUT=$(sh "$FILES/usr/bin/jidu-isp.sh" show 2>&1)
has "show prints wan" "$OUT" "network.wan"

echo "=== 5. boot guard: normal boot increments the counter ==="
WG="$FILES/etc/init.d/jidu-bootguard"
mkdir -p "$TMP/etc"
printf '/dev/mtd0 0x0 0x40000 0x20000\n' > "$TMP/etc/fw_env.config"
export JIDU_FW_ENV_CONFIG="$TMP/etc/fw_env.config"
export JIDU_PROC_MTD="$PROC/mtd"
sh -c ". '$WG'; start" >/dev/null 2>&1
check "counter incremented" "$(fw_printenv -n jidu_boot_attempts)" "1"

echo "=== 6. boot guard: clean shutdown clears it ==="
sh -c ". '$WG'; stop" >/dev/null 2>&1
check "counter cleared" "$(fw_printenv -n jidu_boot_attempts)" "0"

echo "=== 7. boot guard: 3 failed boots triggers the rollback ==="
fw_setenv jidu_boot_attempts 2
sh -c ". '$WG'; start" >/dev/null 2>&1
check "bootcmd rolled back" \
  "$(fw_printenv -n bootcmd)" \
  "ubi part rootfs_1; ubi read 0x46000000 kernel; bootm 0x46000000"
if [ -s "$REBOOT_LOG" ]; then ok "reboot requested"; else bad "reboot requested"; fi

echo
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
