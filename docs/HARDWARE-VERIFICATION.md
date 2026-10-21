# Hardware verification checklist — JIDU6111

Run this **on the router** before and after flashing. Every line is a
command you can copy-paste. Work through them in order; do not skip
ahead to the flash step until the "before" column is satisfied.

Copy the whole block and paste it into a SSH session — it prints
everything at once and does not change anything.

---

## Phase 0 — On the STOCK firmware (before any flash)

### 0.1 Identify the hardware

```sh
echo "=== model ==="
cat /proc/device-tree/model 2>/dev/null; echo
echo "=== compatible ==="
cat /proc/device-tree/compatible 2>/dev/null | tr '\0' ' '; echo
echo "=== SoC ==="
cat /proc/cpuinfo | head -5
echo "=== RAM ==="
grep MemTotal /proc/meminfo
echo "=== NAND ==="
cat /proc/mtd | head -3
dmesg 2>/dev/null | grep -iE "nand.*(detected|size)" | head -3
```

**Expected:**

| Item | Expected value |
|---|---|
| `model` | contains `6111` (or `JIDU6J11`) |
| `compatible` | contains `jiorouter` and `ipq9574` |
| SoC | `IPQ9574` / `Qualcomm` |
| RAM | ~**512 MB** (or whatever your unit reports) |
| NAND | **256 MB** typical — **write down the exact number** |

> ⚠️ If `model` says `JIDU6J11` **without** `6111`, your unit may be a
> different variant (6411/6611/6811/6911). The firmware in this repo is
> for **6111 only**. Stop and confirm the variant.

### 0.2 Record the partition table — this is the critical step

```sh
echo "=== /proc/mtd ==="
cat /proc/mtd
echo "=== /proc/cmdline ==="
cat /proc/cmdline
echo "=== u-boot env ==="
fw_printenv 2>/dev/null || strings /dev/mtd0 2>/dev/null | grep -iE "mtdparts|bootcmd|bootargs"
```

**Write down, verbatim:**

- the **idle slot** name (the one NOT currently mounted as rootfs)
- its **mtd number**
- its **offset** and **size**
- the current `bootcmd` and `mtdparts`

```sh
echo "=== what is mounted ==="
mount | grep -E "ubifs|squashfs|jffs2"
df -h | head -10
```

> ⚠️ **Do not copy partition offsets from any document — including this
> one.** The community release (`mtd23`/`mtd24`, load `0x46000000`) and
> the `JIDU6J11-PR` branch (single `ubi` @ `0xe100000`@`0x1700000`, load
> `0x44000000`) are **different layouts**. Your unit's own `/proc/mtd` is
> the only authority.

### 0.3 Take the backup — do not skip this

```sh
mkdir -p /tmp/backup && cd /tmp/backup
# every MTD partition, one file each
for m in /dev/mtd[0-9]*; do
  n=$(basename "$m")
  dd if="$m" of="$n.bin" bs=128k 2>/dev/null
  echo "$n: $(stat -c %s "$n.bin" 2>/dev/null) bytes"
done
# the U-Boot environment
fw_printenv > fw_env.txt 2>/dev/null
# the running rootfs
dd if=/dev/rootfs of=rootfs.full bs=1M 2>/dev/null || \
  dd if=/dev/mtdblock23 of=rootfs.full bs=1M 2>/dev/null
ls -la
```

Then **copy `/tmp/backup` off the router** to your PC (scp, or `tar czf`
+ download from LuCI). Verify the copy is non-zero in size.

```sh
cd /tmp && tar czf /tmp/backup.tgz backup/ && ls -la /tmp/backup.tgz
```

> A backup you have not copied off the router is not a backup.

### 0.4 Confirm the firmware version is unlockable

```sh
cat /etc/openwrt_release 2>/dev/null
cat /etc/firmware_version 2>/dev/null
```

The `wpfyorg/idu-unlock` route works on **`R3.2.6` and older**. Newer
firmware returns `TEST` and the unlock will not proceed. Check before
you start.

---

## Phase 1 — Root over SSH (no flash)

Uses `wpfyorg/idu-unlock`. This does **not** write to flash and is fully
reversible.

1. Download the unlocker on your PC:
   `git clone https://github.com/wpfyorg/idu-unlock.git`
2. Read its `README.md` for the current usage.
3. Run it against the router's IP.

You should end up with a root SSH session. Use it to run Phase 0.3 if you
have not already backed up.

**Verify:** `id` → `uid=0(root)`.

---

## Phase 2 — Flash the initramfs (the safe step)

The initramfs runs entirely from RAM. If it fails, **nothing was
overwritten** and the stock firmware still boots.

It is also **required**, not just safer: the `jiorouter_initial_setup()`
function in `platform.sh` only runs when `rootfs_type` is `tmpfs` — i.e.
from the initramfs. That function is what writes the U-Boot environment
the squashfs image later boots from:

```
mtdids  = nand0=nand0
mtdparts = mtdparts=nand0:0xe100000@0x1700000(ubi)
bootcmd  = ubi part ubi; ubi read 0x44000000 kernel; bootm 0x44000000
bootargs = console=ttyMSM0,115200n8 ubi.mtd=ubi root=/dev/ubiblock0_1 rootfstype=squashfs
```

It needs a partition named **`0:APPSBLENV`** to exist. If it does not, the
upgrade aborts with:

```
unable to find mtd partition 0:APPSBLENV
```

If you see that, your unit's partition table does not match what this
branch expects — **stop** and compare `/proc/mtd` against the values
above before writing anything.

1. Put `openwrt-qualcommbe-ipq95xx-jiorouter_ax6000-jidu6j11-initramfs-uImage.itb`
   on a TFTP server reachable from the router.
2. From the router's shell, load and boot it. Use the `Sandiep/openwrt`
   `JIDU6J11-PR` procedure — **not** the community release's `flash.sh`:
   ```sh
   # example for the PR branch's single-ubi layout
   tftp <tftp-server-ip>
   > openwrt-...-initramfs-uImage.itb
   ubi part ubi
   ubi read 0x44000000 kernel
   bootm 0x44000000
   ```
3. The kernel console is `ttyMSM0,115200n8`. Attach a UART cable
   (3.3 V TTL: TX/RX/GND) to watch the boot.

### Phase 2 verification — run this IN the initramfs

```sh
sh /usr/bin/jidu-identify.sh
```

**Every check must read OK.** Specifically confirm:

| Check | Must be |
|---|---|
| model | the **6111** variant |
| `ubi` partition offset | `0x01700000` |
| `ubi` partition size | `0x0e100000` |
| Wi-Fi board data | present, non-zero size, in `/lib/firmware/ath11k/IPQ9574/hw1.0/` |
| RAM | matches what you recorded in Phase 0.1 |
| NAND | matches what you recorded in Phase 0.1 |

```sh
# spot-check the board data yourself
ls -la /lib/firmware/ath11k/IPQ9574/hw1.0/
ls -la /lib/firmware/ath11k/QCN9074/hw1.0/
```

### Phase 2 functional checks

```sh
# Ethernet
ip addr show br-lan
ping -c 3 8.8.8.8

# Wi-Fi
iw dev
iw dev wlan0 info
uci show wireless | grep -E 'country|encryption'
```

If Wi-Fi shows up but has **no carrier / very low TX power**, the BDF is
wrong — stop and do not flash to NAND. Re-check that the `ipq-wifi` BDF
patch actually landed in the build.

---

## Phase 3 — Flash to NAND

Only after Phase 2 is fully green.

1. From the initramfs, `sysupgrade` the squashfs image:
   ```sh
   sysupgrade -n /tmp/openwrt-...-squashfs-sysupgrade.bin
   ```
2. The `platform.sh` on the `JIDU6J11-PR` branch runs
   `jiorouter_initial_setup` from the initramfs, which rewrites the
   U-Boot environment:
   ```
   mtdparts = mtdparts=nand0:0xe100000@0x1700000(ubi)
   bootcmd  = ubi part ubi; ubi read 0x44000000 kernel; bootm 0x44000000
   bootargs = console=ttyMSM0,115200n8 ubi.mtd=ubi root=/dev/ubiblock0_1 rootfstype=squashfs
   ```
3. Wait for the reboot. It can take 3–5 minutes on first boot while the
   rootfs is expanded.

### Phase 3 verification

```sh
# did the env actually change?
fw_printenv mtdparts
fw_printenv bootcmd
fw_printenv bootargs
fw_printenv jidu_boot_attempts

# is the new layout live?
cat /proc/mtd
mount | grep -E "ubifs|squashfs"

# did the features install?
ls -la /usr/bin/jidu-isp.sh /usr/bin/jidu-identify.sh
ls -la /etc/init.d/jidu-bootguard
ls -la /etc/uci-defaults/99-jidu-first-boot
```

Then the functional pass:

```sh
sh /usr/bin/jidu-identify.sh
sh /usr/bin/jidu-isp.sh show

# reboot persistence
sysctl -w net.ipv4.ip_forward=1   # sanity
sync && reboot
# after it comes back:
fw_printenv jidu_boot_attempts   # must be 0 after a clean boot
```

---

## Phase 4 — The ISP switch (the whole point)

```sh
# Airtel / any PPPoE ISP
sh /usr/bin/jidu-isp.sh pppoe your@username yourpassword

# or, if your ISP uses DHCP:
sh /usr/bin/jidu-isp.sh dhcp

# static
sh /usr/bin/jidu-isp.sh static 203.0.113.10 255.255.255.0 203.0.113.1 8.8.8.8
```

Check it took:

```sh
ifstatus wan
logread -e netifd | tail -20
ping -c 3 8.8.8.8
```

> If the ISP's own router also hands out `192.168.1.x`, change the LAN
> side: `sh /usr/bin/jidu-isp.sh pppoe user pass 192.168.10.1`.

---

## Phase 5 — The boot guard (test the safety net)

You do **not** have to test this by breaking the router. Test the
mechanism:

```sh
# simulate 3 failed boots
fw_setenv jidu_boot_attempts 3
sync && reboot

# after it comes back, the guard should have acted:
fw_printenv bootcmd
fw_printenv jidu_boot_attempts
logread -e jidu-bootguard
```

Expected: the log shows `boot failed ... attempting recovery`, the
`bootcmd` was rewritten to the alternate slot or the stock `bootipq`, and
the router rebooted.

A real (safe) destructive test: set the counter above the threshold
**and** make the current slot unbootable, then confirm the router comes
back on the other slot. Do this only with a UART cable attached.

---

## Phase 6 — Wi-Fi validation

```sh
iw dev
iw dev wlan0 link
iw dev wlan0 station dump
# regulatory domain
iw reg get
```

Throughput sanity: `iperf3` against a wired host, and a 5 GHz and a
2.4 GHz client both connecting.

```sh
# channels and power
iw dev wlan0 info
iw dev wlan1 info
```

---

## Failure triage

| Symptom | Likely cause | Action |
|---|---|---|
| No boot at all after flash | bad partition write | UART console, restore Phase 0.3 backup |
| Boots, no Wi-Fi | BDF patch did not land | check `/lib/firmware/ath11k/`, rebuild |
| Wi-Fi but very low power | wrong calibration variant | confirm `6111` in the DTB |
| No WAN | ISP needs a different mode | try `dhcp` / `static` / `pppoe` |
| LAN and ISP both 192.168.1.x | address collision | set a different LAN IP |
| Router unreachable after sysupgrade | U-Boot env rewritten, kernel not loading | UART console, check `bootcmd` |

### Getting to a UART console

- 3.3 V TTL, **not** 5 V
- TX/RX **crossed** (router TX → your RX, router RX → your TX)
- GND connected
- 115200 8N1, no flow control
- The console is `ttyMSM0` on the 6j11
- Interrupt U-Boot with a break/Ctrl-C during the countdown

```sh
# from the router shell, if you still have one
cat /proc/cmdline
dmesg | grep -i "console"
```

---

## Sign-off checklist

Do not consider the flash successful until every box is ticked:

- [ ] Phase 0.3 backup taken **and copied off the router**
- [ ] `jidu-identify.sh` reports all checks OK in the initramfs
- [ ] Wi-Fi present with correct regulatory domain
- [ ] Ethernet working, WAN up
- [ ] `sysupgrade` completed, reboot clean
- [ ] `jidu_boot_attempts` is `0` after a clean boot
- [ ] `jidu-isp.sh` successfully switched the ISP
- [ ] `jidu-isp.sh show` output is sane
- [ ] UART console tested and working (before you need it)
