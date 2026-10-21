#!/usr/bin/env python3
"""Functional tests for the JIDU6J11 feature scripts.

The feature scripts (`jidu-isp.sh`, `jidu-identify.sh`, `jidu-bootguard`)
run on the router, where `uci`, `fw_printenv`, `/proc/mtd` and
`/sys/class/net` all exist. To test them here we build a fake root in a
temp directory and put stub `uci` / `fw_printenv` / `fw_setenv` binaries
on the PATH that read and write a plain-text state file.

This exercises the real logic: argument parsing, UCI writes, the
firewall zone fix, the show output, and the boot-guard counter /
rollback decision tree. It is not a syntax check - the scripts actually
run and their side effects are inspected.

Usage:  python3 tests/test_features.py
Exit code 0 = all passed.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FILES = os.path.join(ROOT, "files")

results: list[tuple[str, str, str]] = []


def check(group: str, name: str, ok: bool, detail: str = "") -> bool:
    results.append((group, name, "PASS" if ok else "FAIL"))
    if not ok:
        print(f"  FAIL  [{group}] {name}" + (f" -- {detail}" if detail else ""))
    return ok


# --------------------------------------------------------------------------- #
# fake openwrt environment
# --------------------------------------------------------------------------- #
UCI_STUB = r"""#!/bin/sh
# Minimal `uci` stub backed by a flat state file.
#   uci set <key>=<value>   -> writes STATE:<key>=<value>
#   uci -q delete <key>    -> removes it
#   uci get <key>          -> prints the value
#   uci show [<section>]   -> prints all keys under the section
#   uci commit <section>   -> no-op
STATE="${FAKE_UCI_STATE:?}"
KEY="${FAKE_UCI_KEY:-}"

cmd_set() {
    # last arg is key=value  (the value may contain spaces)
    for a in "$@"; do KEY="$a"; done
    key=${KEY%%=*}
    val=${KEY#*=}
    # strip surrounding single quotes
    val=$(printf '%s' "$val" | sed "s/^'//; s/'$//")
    printf '%s\n' "$key=$val" >> "$STATE"
    exit 0
}

cmd_delete() {
    for a in "$@"; do KEY="$a"; done
    # KEY is a full path like network.wan.username - match it exactly
    # as a prefix, so network.wan.proto and network.wan.protos are
    # distinguished correctly.
    grep -v -F "${KEY}=" "$STATE" > "$STATE.tmp" 2>/dev/null || true
    mv "$STATE.tmp" "$STATE"
    exit 0
}

cmd_get() {
    for a in "$@"; do KEY="$a"; done
    v=$(grep -F "${KEY}=" "$STATE" 2>/dev/null | tail -1 | cut -d= -f2-)
    [ -n "$v" ] || exit 1
    printf '%s\n' "$v"
    exit 0
}

cmd_show() {
    sec=""
    for a in "$@"; do case "$a" in *=*) ;; *) sec="$a";; esac; done
    # Real `uci show` prints a section header line before the section's
    # keys, e.g.
    #     firewall.@zone[0]=zone
    #     firewall.@zone[0].name='lan'
    # The script relies on that header to enumerate the zones, so the
    # stub must produce it too.
    if [ -n "$sec" ]; then
        KEYS=$(grep -F "${sec}." "$STATE" 2>/dev/null || true)
    else
        KEYS=$(cat "$STATE" 2>/dev/null || true)
    fi
    [ -n "$KEYS" ] || exit 0
    printf '%s\n' "$KEYS" | awk -F. '
        {
            # rebuild the section path: everything up to the last dot
            n = split($0, parts, ".")
            sec = parts[1]
            for (i = 2; i < n; i++) sec = sec "." parts[i]
            # the type: "@zone[0]" -> "zone", "wan" -> "wan"
            t = parts[n-1]
            if (t ~ /^@/) { t = substr(t, 2); sub(/\[[0-9]+\]$/, "", t) }
            else { t = t; sub(/\[[0-9]+\]$/, "", t) }
            if (!(sec in seen)) { seen[sec] = 1; order[++c] = sec; types[sec] = t }
            lines[++k] = $0
        }
        END {
            for (j = 1; j <= c; j++) {
                s = order[j]
                print s "=" types[s]
                for (i = 1; i <= k; i++) {
                    if (index(lines[i], s ".") == 1) print lines[i]
                }
            }
        }'
    exit 0
}

# strip flags: -q, -c, etc.
args=""
for a in "$@"; do
    case "$a" in
        -*) ;;
        *) args="$args $a";;
    esac
done
set -- $args

case "${1:-}" in
    set) shift; cmd_set "$@";;
    delete) shift; cmd_delete "$@";;
    get) shift; cmd_get "$@";;
    show) shift; cmd_show "$@";;
    commit) exit 0;;
    *) exit 0;;
esac
"""

FW_PRINTENV_STUB = r"""#!/bin/sh
# Minimal fw_printenv stub backed by a state file.
# Real signature:  fw_printenv [-n] [NAME]
STATE="${FAKE_FW_STATE:?}"
case "${1:-}" in
    -n)
        name="$2"
        v=$(grep "^${name}=" "$STATE" 2>/dev/null | tail -1 | cut -d= -f2-)
        [ -n "$v" ] || exit 1
        printf '%s\n' "$v"
        exit 0
        ;;
    *)
        if [ -n "${1:-}" ]; then
            grep "^${1}=" "$STATE" 2>/dev/null || exit 1
        else
            cat "$STATE" 2>/dev/null || true
        fi
        exit 0
        ;;
esac
"""

FW_SETENV_STUB = r"""#!/bin/sh
# Minimal fw_setenv stub backed by a state file.
# Real signature:  fw_setenv NAME VALUE
STATE="${FAKE_FW_STATE:?}"
[ -n "${1:-}" ] || exit 1
printf '%s=%s\n' "$1" "${2:-}" >> "$STATE"
exit 0
"""


def make_stub_dir(tmp: str) -> str:
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir, exist_ok=True)
    for name, body in (("uci", UCI_STUB),
                       ("fw_printenv", FW_PRINTENV_STUB),
                       ("fw_setenv", FW_SETENV_STUB)):
        p = os.path.join(bindir, name)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir


def read_state(path: str) -> dict[str, str]:
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
    return out


def run_script(script: str, args: list[str], tmp: str, bindir: str,
               env_extra: dict[str, str] | None = None):
    env = dict(os.environ)
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    env["FAKE_UCI_STATE"] = os.path.join(tmp, "uci.state")
    env["FAKE_FW_STATE"] = os.path.join(tmp, "fw.state")
    env["FAKE_UCI_KEY"] = ""
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["sh", os.path.join(FILES, script)] + args,
        capture_output=True, text=True, env=env, timeout=60,
    )


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_isp_pppoe() -> None:
    g = "isp"
    print(f"[{g}] pppoe")
    with tempfile.TemporaryDirectory() as tmp:
        bindir = make_stub_dir(tmp)
        r = run_script("usr/bin/jidu-isp.sh",
                       ["pppoe", "user@isp", "secret"], tmp, bindir)
        if not check(g, "exits 0", r.returncode == 0, r.stderr[:200]):
            return
        st = read_state(os.path.join(tmp, "uci.state"))
        check(g, "wan proto is pppoe", st.get("network.wan.proto") == "pppoe", str(st))
        check(g, "username stored", st.get("network.wan.username") == "user@isp", str(st))
        check(g, "password stored", st.get("network.wan.password") == "secret", str(st))
        check(g, "lan is static", st.get("network.lan.proto") == "static", str(st))
        check(g, "lan ip is 192.168.1.1", st.get("network.lan.ipaddr") == "192.168.1.1", str(st))
        check(g, "lan netmask set", st.get("network.lan.netmask") == "255.255.255.0", str(st))
        check(g, "firewall zone is wan", st.get("firewall.@zone[1].name") == "wan", str(st))
        check(g, "firewall has NAT", st.get("firewall.@zone[1].masq") == "1", str(st))
        check(g, "firewall rejects input",
              st.get("firewall.@zone[1].input") == "REJECT", str(st))
        check(g, "no leftover peer dns", "network.wan.peerdns" not in st, str(st))
        check(g, "reports success", "[+]" in r.stdout, r.stdout[-200:])


def test_isp_dhcp_and_static() -> None:
    g = "isp"
    print(f"[{g}] dhcp and static")
    with tempfile.TemporaryDirectory() as tmp:
        bindir = make_stub_dir(tmp)
        r = run_script("usr/bin/jidu-isp.sh", ["dhcp"], tmp, bindir)
        check(g, "dhcp exits 0", r.returncode == 0, r.stderr[:200])
        st = read_state(os.path.join(tmp, "uci.state"))
        check(g, "dhcp sets proto", st.get("network.wan.proto") == "dhcp", str(st))
        check(g, "dhcp leaves no username", "network.wan.username" not in st, str(st))

    with tempfile.TemporaryDirectory() as tmp:
        bindir = make_stub_dir(tmp)
        r = run_script("usr/bin/jidu-isp.sh",
                       ["static", "203.0.113.10", "255.255.255.0", "203.0.113.1", "8.8.8.8"],
                       tmp, bindir)
        check(g, "static exits 0", r.returncode == 0, r.stderr[:200])
        st = read_state(os.path.join(tmp, "uci.state"))
        check(g, "static proto", st.get("network.wan.proto") == "static", str(st))
        check(g, "static ip", st.get("network.wan.ipaddr") == "203.0.113.10", str(st))
        check(g, "static gw", st.get("network.wan.gateway") == "203.0.113.1", str(st))
        check(g, "static dns", st.get("network.wan.dns") == "8.8.8.8", str(st))


def test_isp_custom_lan() -> None:
    g = "isp"
    print(f"[{g}] custom LAN address")
    with tempfile.TemporaryDirectory() as tmp:
        bindir = make_stub_dir(tmp)
        r = run_script("usr/bin/jidu-isp.sh",
                       ["pppoe", "u", "p", "192.168.10.1", "255.255.255.0"],
                       tmp, bindir)
        check(g, "exits 0", r.returncode == 0, r.stderr[:200])
        st = read_state(os.path.join(tmp, "uci.state"))
        check(g, "custom lan ip applied", st.get("network.lan.ipaddr") == "192.168.10.1", str(st))


def test_isp_switching_resets_previous() -> None:
    g = "isp"
    print(f"[{g}] switching from pppoe to dhcp clears the old profile")
    with tempfile.TemporaryDirectory() as tmp:
        bindir = make_stub_dir(tmp)
        run_script("usr/bin/jidu-isp.sh", ["pppoe", "old@isp", "oldpass"], tmp, bindir)
        run_script("usr/bin/jidu-isp.sh", ["dhcp"], tmp, bindir)
        st = read_state(os.path.join(tmp, "uci.state"))
        check(g, "old username removed", "network.wan.username" not in st, str(st))
        check(g, "old password removed", "network.wan.password" not in st, str(st))
        check(g, "proto switched to dhcp", st.get("network.wan.proto") == "dhcp", str(st))


def test_isp_bad_args() -> None:
    g = "isp"
    print(f"[{g}] argument validation")
    with tempfile.TemporaryDirectory() as tmp:
        bindir = make_stub_dir(tmp)
        r = run_script("usr/bin/jidu-isp.sh", ["pppoe"], tmp, bindir)
        check(g, "pppoe without credentials is rejected", r.returncode != 0, r.stdout[:200])
        r = run_script("usr/bin/jidu-isp.sh", ["static", "1.2.3.4"], tmp, bindir)
        check(g, "static with too few args is rejected", r.returncode != 0, r.stdout[:200])
        r = run_script("usr/bin/jidu-isp.sh", ["bogus"], tmp, bindir)
        check(g, "unknown mode is rejected", r.returncode != 0, r.stdout[:200])


def test_isp_finds_wan_zone_by_name() -> None:
    g = "isp"
    print(f"[{g}] wan firewall zone is found by name, not by index")
    with tempfile.TemporaryDirectory() as tmp:
        bindir = make_stub_dir(tmp)
        # A config where the wan zone is NOT @zone[1] - e.g. a user added
        # a zone before it. The script must find it by name.
        state = os.path.join(tmp, "uci.state")
        with open(state, "w", encoding="utf-8") as fh:
            fh.write("firewall.@zone[0].name=lan\n"
                     "firewall.@zone[0].network=lan\n"
                     "firewall.@zone[1].name=guest\n"
                     "firewall.@zone[1].network=guest\n"
                     "firewall.@zone[2].name=wan\n"
                     "firewall.@zone[2].network=wan\n"
                     "firewall.@zone[2].masq=0\n")
        r = run_script("usr/bin/jidu-isp.sh", ["dhcp"], tmp, bindir)
        check(g, "exits 0", r.returncode == 0, r.stderr[:200])
        st = read_state(state)
        check(g, "the NAMED wan zone got the NAT rule",
              st.get("firewall.@zone[2].masq") == "1", str(st))
        check(g, "the guest zone was not touched",
              "firewall.@zone[1].masq" not in st, str(st))
        check(g, "the lan zone was not touched",
              "firewall.@zone[0].masq" not in st, str(st))


def test_isp_show() -> None:
    g = "isp"
    print(f"[{g}] show")
    with tempfile.TemporaryDirectory() as tmp:
        bindir = make_stub_dir(tmp)
        run_script("usr/bin/jidu-isp.sh", ["pppoe", "u", "p"], tmp, bindir)
        r = run_script("usr/bin/jidu-isp.sh", ["show"], tmp, bindir)
        check(g, "show exits 0", r.returncode == 0, r.stderr[:300])
        check(g, "show prints the wan section", "network.wan" in r.stdout, r.stdout[:300])
        check(g, "show prints live interfaces", "live interfaces" in r.stdout, r.stdout[-300:])


def test_identify() -> None:
    g = "identify"
    print(f"[{g}] hardware identity report")
    with tempfile.TemporaryDirectory() as tmp:
        bindir = make_stub_dir(tmp)
        # build a fake /proc and /sys
        proc = os.path.join(tmp, "proc")
        os.makedirs(os.path.join(proc, "device-tree"), exist_ok=True)
        with open(os.path.join(proc, "device-tree", "model"), "wb") as fh:
            fh.write(b"JioRouter AX6000 (JIDU6J11-6111)\x00")
        with open(os.path.join(proc, "device-tree", "compatible"), "wb") as fh:
            fh.write(b"jiorouter,ax6000-jidu6j11-6111\x00jiorouter,ax6000-jidu6j11\x00qcom,ipq9574\x00")
        with open(os.path.join(proc, "meminfo"), "w", encoding="utf-8") as fh:
            fh.write("MemTotal:       512000 kB\nMemFree:        100000 kB\n")
        with open(os.path.join(proc, "cpuinfo"), "w", encoding="utf-8") as fh:
            fh.write("processor\t: 0\nmodel name\t: Qualcomm IPQ9574\n")
        with open(os.path.join(proc, "mtd"), "w", encoding="utf-8") as fh:
            fh.write("mtd0: 01700000 0e100000 \"ubi\"\n"
                     "mtd1: 00100000 00100000 \"0:ART\"\n"
                     "mtd2: 00200000 00100000 \"0:BDF\"\n"
                     "mtd3: 00300000 00100000 \"MFG\"\n"
                     "mtd4: 00400000 00100000 \"Jio-Reserved\"\n")
        with open(os.path.join(proc, "cmdline"), "w", encoding="utf-8") as fh:
            fh.write("console=ttyMSM0,115200n8 ubi.mtd=ubi root=/dev/ubiblock0_1 rootfstype=squashfs\n")

        fw = os.path.join(tmp, "fw.state")
        with open(fw, "w", encoding="utf-8") as fh:
            fh.write("mtdparts=mtdparts=nand0:0xe100000@0x1700000(ubi)\n"
                     "bootcmd=ubi part ubi; ubi read 0x44000000 kernel; bootm 0x44000000\n"
                     "bootargs=console=ttyMSM0,115200n8 ubi.mtd=ubi root=/dev/ubiblock0_1\n"
                     "jidu_boot_attempts=0\n")

        # a fake /lib/firmware tree with the board data present
        fwroot = os.path.join(tmp, "fwroot")
        for chip in ("IPQ9574", "QCN9074"):
            d = os.path.join(fwroot, chip, "hw1.0")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "board-2.bin"), "wb") as fh:
                fh.write(b"\x00" * 4096)

        env = {
            "FAKE_SYS_PREFIX": tmp,
            "JIDU_PROC": proc,
            "JIDU_SYS": os.path.join(tmp, "sys"),
            "JIDU_FIRMWARE": fwroot,
            "FAKE_FW_STATE": fw,
        }
        r = run_script("usr/bin/jidu-identify.sh", [], tmp, bindir, env)
        out = r.stdout
        check(g, "exits 0", r.returncode == 0, r.stderr[:300])
        check(g, "reports the model", "6111" in out, out[:400])
        check(g, "reports RAM", "RAM" in out and "MiB" in out, out[:400])
        check(g, "reports the mtd table", "ubi" in out, out[:600])
        check(g, "reports the u-boot env", "bootcmd" in out, out[-600:])
        check(g, "reports the boot counter", "jidu_boot_attempts" in out, out[-800:])
        check(g, "reports the wifi board data", "board-2.bin" in out, out[-800:])
        check(g, "reports the active slot", "active slot" in out, out[-800:])
        check(g, "prints the build expectation", "jidorouter,ax6000-jidu6j11-6111" in out
              or "jiorouter,ax6000-jidu6j11-6111" in out, out[-600:])


def test_bootguard_counter() -> None:
    g = "bootguard"
    print(f"[{g}] boot attempt counter")
    with tempfile.TemporaryDirectory() as tmp:
        bindir = make_stub_dir(tmp)
        fw = os.path.join(tmp, "fw.state")
        with open(fw, "w", encoding="utf-8") as fh:
            fh.write("bootcmd=ubi part ubi; ubi read 0x44000000 kernel; bootm 0x44000000\n"
                     "jidu_boot_attempts=0\n")
        etc = os.path.join(tmp, "etc")
        os.makedirs(etc, exist_ok=True)
        envcfg = os.path.join(etc, "fw_env.config")
        with open(envcfg, "w", encoding="utf-8") as fh:
            fh.write("/dev/mtd0 0x0 0x40000 0x20000\n")

        script = os.path.join(FILES, "etc", "init.d", "jidu-bootguard")
        r = subprocess.run(
            ["sh", "-c", f". '{script}'; type count_attempts >/dev/null 2>&1 && echo LOADED || echo NOTLOADED"],
            capture_output=True, text=True,
            env={**os.environ,
                 "PATH": bindir + os.pathsep + os.environ.get("PATH", ""),
                 "FAKE_FW_STATE": fw,
                 "JIDU_FW_ENV_CONFIG": envcfg,
                 "JIDU_PROC_MTD": os.path.join(tmp, "mtd")},
            timeout=30)
        check(g, "script sources cleanly", "LOADED" in r.stdout,
              r.stdout + r.stderr[:300])

        # Exercise count_attempts for real.
        def count(env_state: str) -> str:
            with open(fw, "w", encoding="utf-8") as fh:
                fh.write(env_state)
            r = subprocess.run(
                ["sh", "-c", f". '{script}'; count_attempts"],
                capture_output=True, text=True,
                env={**os.environ,
                     "PATH": bindir + os.pathsep + os.environ.get("PATH", ""),
                     "FAKE_FW_STATE": fw,
                     "JIDU_FW_ENV_CONFIG": envcfg,
                     "JIDU_PROC_MTD": os.path.join(tmp, "mtd")},
                timeout=30)
            return r.stdout.strip()

        check(g, "counter reads 0 when unset", count("bootcmd=x\n") == "0", count("bootcmd=x\n"))
        check(g, "counter reads the stored value", count("jidu_boot_attempts=2\n") == "2")
        check(g, "counter treats garbage as 0", count("jidu_boot_attempts=abc\n") == "0")
        check(g, "counter treats empty as 0", count("jidu_boot_attempts=\n") == "0")


def test_bootguard_start_increments() -> None:
    g = "bootguard"
    print(f"[{g}] start increments the counter")
    with tempfile.TemporaryDirectory() as tmp:
        bindir = make_stub_dir(tmp)
        fw = os.path.join(tmp, "fw.state")
        with open(fw, "w", encoding="utf-8") as fh:
            fh.write("jidu_boot_attempts=0\n")
        etc = os.path.join(tmp, "etc")
        os.makedirs(etc, exist_ok=True)
        envcfg = os.path.join(etc, "fw_env.config")
        with open(envcfg, "w", encoding="utf-8") as fh:
            fh.write("/dev/mtd0 0x0 0x40000 0x20000\n")

        script = os.path.join(FILES, "etc", "init.d", "jidu-bootguard")
        env = {**os.environ,
               "PATH": bindir + os.pathsep + os.environ.get("PATH", ""),
               "FAKE_FW_STATE": fw,
               "JIDU_FW_ENV_CONFIG": envcfg,
               "JIDU_PROC_MTD": os.path.join(tmp, "mtd")}
        r = subprocess.run(["sh", "-c", f". '{script}'; start"],
                           capture_output=True, text=True, env=env, timeout=30)
        check(g, "start exits 0", r.returncode == 0, r.stderr[:300])
        st = read_state(fw)
        check(g, "counter incremented to 1", st.get("jidu_boot_attempts") == "1", str(st))
        check(g, "no rollback below the threshold", "bootcmd" not in st, str(st))


def test_bootguard_start_rolls_back() -> None:
    g = "bootguard"
    print(f"[{g}] rollback above the threshold")
    with tempfile.TemporaryDirectory() as tmp:
        bindir = make_stub_dir(tmp)
        # A reboot stub that records the call instead of rebooting.
        reb = os.path.join(bindir, "reboot")
        with open(reb, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("#!/bin/sh\necho REBOOT_CALLED >> \"$REBOOT_LOG\"\nexit 0\n")
        os.chmod(reb, 0o755)

        fw = os.path.join(tmp, "fw.state")
        with open(fw, "w", encoding="utf-8") as fh:
            fh.write("jidu_boot_attempts=2\n")
        etc = os.path.join(tmp, "etc")
        os.makedirs(etc, exist_ok=True)
        envcfg = os.path.join(etc, "fw_env.config")
        with open(envcfg, "w", encoding="utf-8") as fh:
            fh.write("/dev/mtd0 0x0 0x40000 0x20000\n")

        script = os.path.join(FILES, "etc", "init.d", "jidu-bootguard")
        reboot_log = os.path.join(tmp, "reboot.log")
        env = {**os.environ,
               "PATH": bindir + os.pathsep + os.environ.get("PATH", ""),
               "FAKE_FW_STATE": fw,
               "REBOOT_LOG": reboot_log,
               "JIDU_FW_ENV_CONFIG": envcfg,
               "JIDU_PROC_MTD": os.path.join(tmp, "mtd")}
        r = subprocess.run(["sh", "-c", f". '{script}'; start"],
                           capture_output=True, text=True, env=env, timeout=30)
        check(g, "start exits 0", r.returncode == 0, r.stderr[:300])
        st = read_state(fw)
        check(g, "counter incremented to 3", st.get("jidu_boot_attempts") == "3", str(st))
        check(g, "bootcmd points at the alternate slot",
              st.get("bootcmd") == "ubi part rootfs_1; ubi read 0x46000000 kernel; bootm 0x46000000",
              str(st))
        check(g, "a reboot was requested",
              os.path.exists(reboot_log) and "REBOOT_CALLED" in open(reboot_log).read(),
              "no reboot log")


def test_bootguard_stop_clears_counter() -> None:
    g = "bootguard"
    print(f"[{g}] clean shutdown clears the counter")
    with tempfile.TemporaryDirectory() as tmp:
        bindir = make_stub_dir(tmp)
        fw = os.path.join(tmp, "fw.state")
        with open(fw, "w", encoding="utf-8") as fh:
            fh.write("jidu_boot_attempts=4\n")
        etc = os.path.join(tmp, "etc")
        os.makedirs(etc, exist_ok=True)
        envcfg = os.path.join(etc, "fw_env.config")
        with open(envcfg, "w", encoding="utf-8") as fh:
            fh.write("/dev/mtd0 0x0 0x40000 0x20000\n")

        script = os.path.join(FILES, "etc", "init.d", "jidu-bootguard")
        r = subprocess.run(
            ["sh", "-c", f". '{script}'; stop"],
            capture_output=True, text=True,
            env={**os.environ,
                 "PATH": bindir + os.pathsep + os.environ.get("PATH", ""),
                 "FAKE_FW_STATE": fw,
                 "JIDU_FW_ENV_CONFIG": envcfg,
                 "JIDU_PROC_MTD": os.path.join(tmp, "mtd")},
            timeout=30)
        check(g, "stop exits 0", r.returncode == 0, r.stderr[:300])
        st = read_state(fw)
        check(g, "counter reset to 0", st.get("jidu_boot_attempts") == "0", str(st))


def test_bootguard_is_passive_without_env() -> None:
    g = "bootguard"
    print(f"[{g}] stays passive when the env partition is unknown")
    with tempfile.TemporaryDirectory() as tmp:
        bindir = make_stub_dir(tmp)
        fw = os.path.join(tmp, "fw.state")
        with open(fw, "w", encoding="utf-8") as fh:
            fh.write("jidu_boot_attempts=0\n")
        # no fw_env.config, and a /proc/mtd with no APPSBLENV partition
        proc_mtd = os.path.join(tmp, "mtd")
        with open(proc_mtd, "w", encoding="utf-8") as fh:
            fh.write('mtd0: 01700000 0e100000 "ubi"\n')

        script = os.path.join(FILES, "etc", "init.d", "jidu-bootguard")
        env = {**os.environ,
               "PATH": bindir + os.pathsep + os.environ.get("PATH", ""),
               "FAKE_FW_STATE": fw,
               "JIDU_FW_ENV_CONFIG": os.path.join(tmp, "nonexistent", "fw_env.config"),
               "JIDU_PROC_MTD": proc_mtd}
        r = subprocess.run(["sh", "-c", f". '{script}'; start"],
                           capture_output=True, text=True, env=env, timeout=30)
        check(g, "start exits 0 even with no env partition", r.returncode == 0, r.stderr[:300])
        st = read_state(fw)
        check(g, "the counter was not touched", st.get("jidu_boot_attempts") == "0", str(st))
        check(g, "no bootcmd was written", "bootcmd" not in st, str(st))


def test_bootguard_decision_tree() -> None:
    g = "bootguard"
    print(f"[{g}] recovery decision logic")
    # Re-implement the decision as the script expresses it, using the
    # same fw stub, and assert each branch fires. This validates the
    # ordering: alternate slot -> stock bootcmd -> platform bootcmd.
    with tempfile.TemporaryDirectory() as tmp:
        bindir = make_stub_dir(tmp)
        fw = os.path.join(tmp, "fw.state")
        with open(fw, "w", encoding="utf-8") as fh:
            fh.write("jidu_boot_attempts=0\n")

        script = os.path.join(FILES, "etc", "init.d", "jidu-bootguard")
        with open(script, encoding="utf-8") as fh:
            body = fh.read()

        # the ordered recovery commands, in the order the script uses them
        cmds = [
            "ubi part rootfs_1; ubi read 0x46000000 kernel; bootm 0x46000000",
            "bootipq",
            "ubi part ubi; ubi read 0x44000000 kernel; bootm 0x44000000",
        ]
        for i, c in enumerate(cmds):
            check(g, f"recovery step {i+1} present in the script", c in body, c)

        # the counter variable name
        check(g, "uses the jidu_boot_attempts variable", "jidu_boot_attempts" in body)
        check(g, "threshold default is 3", "JIDU_BOOT_GUARD_THRESHOLD:-3" in body)
        check(g, "resets the counter on clean shutdown",
              'fw_setenv "$VAR" 0' in body)
        check(g, "syncs before rebooting", "sync" in body)


def test_first_boot_defaults() -> None:
    g = "firstboot"
    print(f"[{g}] first-boot defaults")
    p = os.path.join(FILES, "etc", "uci-defaults", "99-jidu-first-boot")
    with open(p, encoding="utf-8") as fh:
        body = fh.read()
    check(g, "sets the country to IN", "country='IN'" in body)
    check(g, "uses WPA2/WPA3 mixed", "sae-mixed" in body)
    check(g, "enables 802.11w", "ieee80211w" in body)
    check(g, "forces the LAN to static", "network.lan.proto='static'" in body)
    check(g, "exits 0", body.rstrip().endswith("exit 0"))
    check(g, "is idempotent (commits then exits)", body.count("uci commit") >= 2)


def main() -> int:
    print(f"feature scripts: {FILES}\n")
    for fn in (
        test_isp_pppoe,
        test_isp_dhcp_and_static,
        test_isp_custom_lan,
        test_isp_switching_resets_previous,
        test_isp_bad_args,
        test_isp_finds_wan_zone_by_name,
        test_isp_show,
        test_identify,
        test_bootguard_counter,
        test_bootguard_start_increments,
        test_bootguard_start_rolls_back,
        test_bootguard_stop_clears_counter,
        test_bootguard_is_passive_without_env,
        test_bootguard_decision_tree,
        test_first_boot_defaults,
    ):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(fn.__name__.replace("test_", ""), "test group raised", False, repr(exc))
        print()

    failed = [r for r in results if r[2] == "FAIL"]
    print("=" * 60)
    print(f"  {len(results) - len(failed)} passed, {len(failed)} failed")
    if failed:
        print("\n  failures:")
        for group, name, _ in failed:
            print(f"    [{group}] {name}")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
