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

echo "=============================================="
echo " JIDU hardware identity"
echo "=============================================="

echo
echo "--- model / SoC ---"
if [ -r /proc/device-tree/model ]; then
	tr -d '\0' < /proc/device-tree/model; echo
else
	echo "model: (no device tree)"
fi
if [ -r /proc/device-tree/compatible ]; then
	tr '\0' '\n' < /proc/device-tree/compatible | sed 's/^/  compatible: /'
fi
if [ -r /proc/cpuinfo ]; then
	echo "  cpu: $(grep -m1 -E '^(processor|model name|Hardware)' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//')"
fi

echo
echo "--- memory ---"
if [ -r /proc/meminfo ]; then
	echo "  RAM: $(( $(grep MemTotal /proc/meminfo | awk '{print $2}') / 1024 )) MiB"
fi

echo
echo "--- NAND partitions (from /proc/mtd) ---"
if [ -r /proc/mtd ]; then
	cat /proc/mtd
else
	echo "  (no /proc/mtd)"
fi

echo
echo "--- NAND identity ---"
if [ -r /proc/mtd ]; then
	echo "  total MTD size: $(awk '{s+=$2} END {printf "%.1f MiB\n", s/1048576}' /proc/mtd)"
fi
dmesg 2>/dev/null | grep -iE 'nand|spi-nand' | head -5 | sed 's/^/  /'

echo
echo "--- active A/B slot ---"
if [ -r /proc/cmdline ]; then
	ACTIVE=$(grep -oE 'ubi\.mtd=[^ ]+' /proc/cmdline | cut -d= -f2)
	echo "  active slot: ${ACTIVE:-<none>}"
	case "$ACTIVE" in
		rootfs)   echo "  idle slot:   rootfs_1" ;;
		rootfs_1) echo "  idle slot:   rootfs" ;;
		*)        echo "  idle slot:   (unknown - read /proc/cmdline by hand)" ;;
	esac
fi

echo
echo "--- U-Boot environment ---"
if command -v fw_printenv >/dev/null 2>&1; then
	echo "  bootcmd : $(fw_printenv -n bootcmd 2>/dev/null)"
	echo "  bootargs: $(fw_printenv -n bootargs 2>/dev/null)"
	echo "  mtdids  : $(fw_printenv -n mtdids 2>/dev/null)"
	echo "  mtdparts: $(fw_printenv -n mtdparts 2>/dev/null)"
	echo "  ---"
	fw_printenv 2>/dev/null | grep -E '^(dual_boot|ipaddr|serverip)' | sed 's/^/  /'
else
	echo "  (fw_printenv not available - uboot-envtools not installed?)"
fi

echo
echo "--- Wi-Fi calibration ---"
for d in /lib/firmware/ath11k/IPQ9574/hw1.0 /lib/firmware/ath11k/QCN9074/hw1.0; do
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
for f in /lib/firmware/ath11k/IPQ9574/hw1.0/board-2.bin \
         /lib/firmware/ath11k/QCN9074/hw1.0/board-2.bin; do
	if [ -f "$f" ]; then
		echo "  $f: $(stat -c %s "$f" 2>/dev/null) bytes"
	else
		echo "  $f: (missing - generic board data will be used)"
	fi
done

echo
echo "--- MAC addresses ---"
for d in lan wan; do
	echo "  $d: $(cat /sys/class/net/$d/address 2>/dev/null || echo '(n/a)')"
done
echo "  ART partition MAC (factory):"
if [ -r /proc/mtd ]; then
	ART=$(grep -oE 'mtd[0-9]+: .*"(0:)?ART"' /proc/mtd | grep -oE '^mtd[0-9]+' | head -1)
	if [ -n "$ART" ]; then
		echo "    $ART holds 0:ART"
	else
		echo "    (no 0:ART partition found)"
	fi
fi

echo
echo "--- build identity ---"
if [ -r /etc/openwrt_release ]; then
	grep -E 'DISTRIB_|VERSION' /etc/openwrt_release | sed 's/^/  /'
fi
if [ -r /etc/openwrt_version ]; then
	echo "  banner: $(cat /etc/openwrt_version)"
fi
if [ -r /etc/board.json ]; then
	echo "  board model: $(sed -n 's/.*"model"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' /etc/board.json | head -1)"
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
