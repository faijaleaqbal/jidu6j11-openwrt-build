#!/bin/sh
# jidu-isp — configure WAN for any ISP, and give the LAN a sane default.
#
# The Jio IDU ships locked to Jio's own WAN profile. This script reconfigures
# the OpenWrt WAN interface for a third-party ISP (PPPoE / DHCP / static) and
# is safe to re-run: it only writes what it is told to.
#
# Usage (on the router, over SSH):
#   sh jidu-isp.sh pppoe <user> <pass> [lan_ip] [netmask]
#   sh jidu-isp.sh dhcp   [lan_ip] [netmask]
#   sh jidu-isp.sh static <ip> <netmask> <gw> <dns> [lan_ip] [netmask]
#   sh jidu-isp.sh show
#
# LAN default: 192.168.1.1/24 -> the OpenWrt default. Change it if the ISP
# router also hands out 192.168.1.x, which would collide.

set -e

MODE="${1:-show}"
shift 2>/dev/null || true

LAN_IP="${LAN_IP:-192.168.1.1}"
LAN_MASK="${LAN_MASK:-255.255.255.0}"

# Which physical port is WAN depends on the board. On the 6j11 the DSA switch
# exposes lan1..lan4 and a separate wan; the OpenWrt network config already
# maps them. Only the L3 setup is done here.
LAN_DEV="${LAN_DEV:-br-lan}"

usage() {
	echo "usage: $0 {pppoe|dhcp|static|show} ..." >&2
	exit 1
}

have() { command -v "$1" >/dev/null 2>&1; }

require_uci() {
	if ! have uci; then
		echo "uci not found - is this an OpenWrt system?" >&2
		exit 1
	fi
}

# ---- base: LAN is always static, and always the same shape --------------
base_lan() {
	uci -q delete network.lan.proto 2>/dev/null || true
	uci set network.lan.proto='static'
	uci set network.lan.ipaddr="$LAN_IP"
	uci set network.lan.netmask="$LAN_MASK"
}

# ---- WAN: drop whatever the previous profile left behind -----------------
reset_wan() {
	for opt in proto username password ipaddr netmask gateway dns \
	           peerdns defaultroute; do
		uci -q delete "network.wan.$opt" 2>/dev/null || true
	done
}

# ---- firewall: WAN must be in the wan zone, with NAT ---------------------
firewall_wan() {
	# zone index 1 is the stock 'wan' zone on OpenWrt
	uci -q set firewall.@zone[1].name='wan' 2>/dev/null || true
	uci -q set firewall.@zone[1].network='wan' 2>/dev/null || true
	uci -q set firewall.@zone[1].input='REJECT' 2>/dev/null || true
	uci -q set firewall.@zone[1].output='ACCEPT' 2>/dev/null || true
	uci -q set firewall.@zone[1].forward='REJECT' 2>/dev/null || true
	uci -q set firewall.@zone[1].masq='1' 2>/dev/null || true
	uci -q set firewall.@zone[1].mtu_fix='1' 2>/dev/null || true
}

# ---- Wi-Fi: WPA2/WPA3 mixed (older clients still connect), India domain --
wireless_tune() {
	[ -e /etc/config/wireless ] || return 0

	# every wifi-iface
	for iface in $(uci show wireless 2>/dev/null | grep '=wifi-iface' | cut -d. -f2 | cut -d= -f1); do
		uci set "wireless.$iface.encryption='sae-mixed'" 2>/dev/null || true
		uci set "wireless.$iface.ieee80211w='1'" 2>/dev/null || true
	done
	# every radio
	for radio in radio0 radio1; do
		uci -q set "wireless.$radio.country='IN'" 2>/dev/null || true
		uci -q set "wireless.$radio.disabled='0'" 2>/dev/null || true
	done
}

apply() {
	uci commit network
	uci commit firewall
	uci commit wireless
	# Restart the network stack. On a minimal initramfs there may be no
	# /etc/init.d/network (or it may fail); the config is already
	# committed, so a failure here must not abort the script.
	if [ -x /etc/init.d/network ]; then
		/etc/init.d/network restart >/dev/null 2>&1 || \
			(ubus call network reload >/dev/null 2>&1 || true)
	fi
}

case "$MODE" in
	pppoe)
		[ $# -ge 2 ] || usage
		require_uci
		USER="$1"; PASS="$2"
		LAN_IP="${3:-$LAN_IP}"; LAN_MASK="${4:-$LAN_MASK}"
		base_lan; reset_wan
		uci set network.wan.proto='pppoe'
		uci set network.wan.username="$USER"
		uci set network.wan.password="$PASS"
		uci set network.wan.keepalive='5 5'
		firewall_wan; wireless_tune; apply
		echo "[+] WAN set to PPPoE as '$USER'. LAN is $LAN_IP/$LAN_MASK."
		;;
	dhcp)
		require_uci
		LAN_IP="${1:-$LAN_IP}"; LAN_MASK="${2:-$LAN_MASK}"
		base_lan; reset_wan
		uci set network.wan.proto='dhcp'
		firewall_wan; wireless_tune; apply
		echo "[+] WAN set to DHCP. LAN is $LAN_IP/$LAN_MASK."
		;;
	static)
		[ $# -ge 4 ] || usage
		require_uci
		IP="$1"; MASK="$2"; GW="$3"; DNS="$4"
		LAN_IP="${5:-$LAN_IP}"; LAN_MASK="${6:-$LAN_MASK}"
		base_lan; reset_wan
		uci set network.wan.proto='static'
		uci set network.wan.ipaddr="$IP"
		uci set network.wan.netmask="$MASK"
		uci set network.wan.gateway="$GW"
		uci set network.wan.dns="$DNS"
		firewall_wan; wireless_tune; apply
		echo "[+] WAN set to static $IP/$MASK via $GW. LAN is $LAN_IP/$LAN_MASK."
		;;
	show)
		echo "--- network.lan ---"
		uci show network.lan 2>/dev/null || echo "(no uci / no config)"
		echo "--- network.wan ---"
		uci show network.wan 2>/dev/null || true
		echo "--- firewall wan zone ---"
		uci show firewall 2>/dev/null | grep -E 'zone\[1\]|@zone\[1\]' | head -20 || true
		echo "--- wireless ---"
		uci show wireless 2>/dev/null | grep -E 'encryption|country|ieee80211w' | head -20 || true
		echo "--- live interfaces ---"
		if [ -d /sys/class/net ]; then
			for d in /sys/class/net/*; do
				[ -e "$d" ] || continue
				n=${d##*/}
				[ "$n" = "lo" ] && continue
				printf '  %-10s state=%s carrier=%s\n' "$n" \
					"$(cat "$d/operstate" 2>/dev/null)" \
					"$(cat "$d/carrier" 2>/dev/null)"
			done
		fi
		if [ -e "/sys/class/net/$LAN_DEV" ]; then
			echo "  LAN bridge $LAN_DEV is present"
		else
			echo "  WARNING: LAN bridge $LAN_DEV not found"
		fi
		;;
	*)
		usage
		;;
esac
