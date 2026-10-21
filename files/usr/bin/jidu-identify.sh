#!/bin/sh
# jidu-identify — report this router's hardware identity and compare it with
# what the running OpenWrt build expects.
#
# Purpose: the 6j11 family shares ONE community image across five model
# numbers. This script answers "is the firmware on this box actually the
# right one for THIS unit?" by reporting the model string, the SoC, the
# partition layout, the Wi-Fi calibration source and the U-Boot environment
# side by side with the build's expectations.
#
# Read-only. Touches nothing.
#
# The system paths are overridable so the report can be exercised in a
# test harness; on the router they take their normal values.

PROC="${JIDU_PROC:-/proc}"
SYS="${JIDU_SYS:-/sys}"
FIRMWARE="${JIDU_FIRMWARE:-/lib/firmware/ath11k}"
RELEASE="${JIDU_RELEASE:-/etc/openwrt_release}"
VERSION="${JIDU_VERSION:-/etc/openwrt_version}"
BOARDJSON="${JIDU_BOARDJSON:-/etc/board.json}"

echo "=============================================="
echo " JIDU hardware identity"
echo "=============================================="

echo
echo "--- model / SoC ---"
if [ -r "$PROC/device-tree/model" ]; then
	tr -d '\0' < "$PROC/device-tree/model"; echo
else
	echo "model: (no device tree)"
fi
if [ -r "$PROC/device-tree/compatible" ]; then
	tr '\0' '\n' < "$PROC/device-tree/compatible" | sed 's/^/  compatible: /'
fi
if [ -r "$PROC/cpuinfo" ]; then
	echo "  cpu: $(grep -m1 -E '^(processor|model name|Hardware)' "$PROC/cpuinfo" | cut -d: -f2- | sed 's/^ *//')"
fi

echo
echo "--- memory ---"
if [ -r "$PROC/meminfo" ]; then
	echo "  RAM: $(( $(grep MemTotal "$PROC/meminfo" | awk '{print $2}') / 1024 )) MiB"
fi

echo
echo "--- NAND partitions (from $PROC/mtd) ---"
if [ -r "$PROC/mtd" ]; then
	cat "$PROC/mtd"
else
	echo "  (no $PROC/mtd)"
fi

echo
echo "--- NAND identity ---"
if [ -r "$PROC/mtd" ]; then
	echo "  total MTD size: $(awk '{s+=$2} END {printf "%.1f MiB\n", s/1048576}' "$PROC/mtd")"
fi
dmesg 2>/dev/null | grep -iE 'nand|spi-nand' | head -5 | sed 's/^/  /'

echo
echo "--- active A/B slot ---"
if [ -r "$PROC/cmdline" ]; then
	ACTIVE=$(grep -oE 'ubi\.mtd=[^ ]+' "$PROC/cmdline" | cut -d= -f2)
	echo "  active slot: ${ACTIVE:-<none>}"
	case "$ACTIVE" in
		rootfs)   echo "  idle slot:   rootfs_1" ;;
		rootfs_1) echo "  idle slot:   rootfs" ;;
		*)        echo "  idle slot:   (unknown - read $PROC/cmdline by hand)" ;;
	esac
fi

echo
echo "--- U-Boot environment ---"
if command -v fw_printenv >/dev/null 2>&1; then
	echo "  bootcmd : $(fw_printenv -n bootcmd 2>/dev/null)"
	echo "  bootargs: $(fw_printenv -n bootargs 2>/dev/null)"
	echo "  mtdids  : $(fw_printenv -n mtdids 2>/dev/null)"
	echo "  mtdparts: $(fw_printenv -n mtdparts 2>/dev/null)"
	echo "  jidu_boot_attempts: $(fw_printenv -n jidu_boot_attempts 2>/dev/null || echo '(unset)')"
	echo "  ---"
	fw_printenv 2>/dev/null | grep -E '^(dual_boot|ipaddr|serverip)' | sed 's/^/  /'
else
	echo "  (fw_printenv not available - uboot-envtools not installed?)"
fi

echo
echo "--- Wi-Fi calibration ---"
for d in "$FIRMWARE/IPQ9574/hw1.0" "$FIRMWARE/QCN9074/hw1.0"; do
	if [ -d "$d" ]; then
		echo "  $d:"
		for b in "$d"/*; do
			[ -e "$b" ] || continue
			printf '    %-40s %s bytes\n' "${b##*/}" \
				"$(stat -c %s "$b" 2>/dev/null || echo '?')"
		done
	else
		echo "  $d: (missing)"
	fi
done

echo
echo "--- Wi-Fi board data (BDF) ---"
for f in "$FIRMWARE/IPQ9574/hw1.0/board-2.bin" \
         "$FIRMWARE/QCN9074/hw1.0/board-2.bin"; do
	if [ -f "$f" ]; then
		echo "  $f: $(stat -c %s "$f" 2>/dev/null) bytes"
	else
		echo "  $f: (missing - generic board data will be used)"
	fi
done

echo
echo "--- MAC addresses ---"
for d in lan wan; do
	echo "  $d: $(cat "$SYS/class/net/$d/address" 2>/dev/null || echo '(n/a)')"
done
echo "  ART partition MAC (factory):"
if [ -r "$PROC/mtd" ]; then
	ART=$(grep -oE 'mtd[0-9]+: .*"(0:)?ART"' "$PROC/mtd" | grep -oE '^mtd[0-9]+' | head -1)
	if [ -n "$ART" ]; then
		echo "    $ART holds 0:ART"
	else
		echo "    (no 0:ART partition found)"
	fi
fi

echo
echo "--- build identity ---"
if [ -r "$RELEASE" ]; then
	grep -E 'DISTRIB_|VERSION' "$RELEASE" | sed 's/^/  /'
fi
if [ -r "$VERSION" ]; then
	echo "  banner: $(cat "$VERSION")"
fi
if [ -r "$BOARDJSON" ]; then
	echo "  board model: $(sed -n 's/.*"model"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$BOARDJSON" | head -1)"
fi

echo
echo "=============================================="
echo " Expectation for a JIDU6J11-6111 build:"
echo "   compatible includes jiorouter,ax6000-jidu6j11-6111"
echo "   SoC is Qualcomm IPQ9574 (qualcommbe/ipq95xx)"
echo "   Wi-Fi BDF present for IPQ9574 AND QCN9074"
echo "   Wi-Fi cal extracted from the 0:ART partition"
echo "   post-sysupgrade: one 'ubi' partition, 0xe100000 @ 0x1700000"
echo "=============================================="
