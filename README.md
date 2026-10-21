# JIDU6111 (JioRouter AX6000) — Custom OpenWrt Firmware

Production firmware for the **JIDU6111** variant of the JioRouter AX6000
(Qualcomm IPQ9574, JIDU6J11 family), built to remove the Jio Air Fiber
lock-in and give the router a real, recoverable OpenWrt install.

> **Status:** the build pipeline is reproducible and the artifacts are
> verified by an automated acceptance-test suite. Hardware validation on a
> physical JIDU6111 is still required before flashing — see
> [Safe flashing procedure](#7-safe-flashing-procedure).

---

## 1. What this is

| | |
|---|---|
| **Target** | JIDU6111 / JioRouter AX6000 (JIDU6J11 family) |
| **SoC** | Qualcomm IPQ9574 (IPQ95xx) |
| **Source tree** | [`Sandiep/openwrt`](https://github.com/Sandiep/openwrt), branch `JIDU6J11-PR` |
| **Wi-Fi board data** | [`Sandiep/firmware_qca-wireless`](https://github.com/Sandiep/firmware_qca-wireless), branch `JIDU6J11`, commit `a8974a05d1c6bc571a34f7e4ef858bc4689c402b` |
| **Build** | GitHub Actions (`.github/workflows/build.yml`) — fully unattended |
| **Upstream OpenWrt support** | **None.** [openwrt/openwrt#24432](https://github.com/openwrt/openwrt/pull/24432) is closed and unmerged; [openwrt/firmware_qca-wireless#146](https://github.com/openwrt/firmware_qca-wireless/pull/146) likewise. |

There is no official OpenWrt build for this router. This repository builds
one from a community tree, and pins the per-model Wi-Fi calibration data
that upstream does not yet carry.

---

## 2. Why the `JIDU6J11-PR` branch and not `JIDU6J11`

The two branches are **not** interchangeable:

| | `JIDU6J11` (generic) | `JIDU6J11-PR` (per-model) |
|---|---|---|
| Device tree | `ipq9574-jiorouter-ax6000-jidu6j11.dts` | `ipq9554-jiorouter-ax6000-jidu6j11-6111.dts` (+ siblings) |
| `model` string | `JioRouter AX6000 JIDU6J11` | identifies the exact variant |
| Wi-Fi calibration | generic | `qcom,ath11k-calibration-variant = "JIDU6J11-6111"` |
| Build profile | `jiorouter_ax6000-jidu6j11` | `jiorouter_ax6000-jidu6j11-6111` |

The calibration property is **functionally significant**, not cosmetic.
The kernel's `ath11k_set_board_data()` reads it and uses the value to
select the matching BDF/board-data blob. A generic build on 6111
hardware therefore comes up with **reduced or missing Wi-Fi** — no
correct TX power tables, no correct antenna config.

> The `JIDU6J11-PR` branch also rewrites the U-Boot environment
> (`jiorouter_initial_setup`) and uses a **single `ubi` partition**
> (`0xe100000` @ `0x1700000`, load address `0x44000000`) instead of the
> community release's `mtd23`/`mtd24` A/B layout. Do not mix the two
> upgrade procedures — see §4.

---

## 3. The one patch this build applies

`package/firmware/ipq-wifi/Makefile` is repointed from
`$(PROJECT_GIT)/project/firmware/qca-wireless.git` at the pinned upstream
revision to `https://github.com/Sandiep/firmware_qca-wireless.git` at
`a8974a05d1c6bc571a34f7e4ef858bc4689c402b` (hash check set to `skip`).

This supplies:

- `board-jiorouter_ax6000-jidu6j11.ipq9574` (655 800 bytes)
- `board-jiorouter_ax6000-jidu6j11.qcn9074` (655 800 bytes)

**If this patch silently fails to apply, the build still succeeds — but the
router boots with no Wi-Fi.** The `ipq-wifi-jiorouter_ax6000-jidu6j11`
package's `install-overlay` globs `board-jiorouter_ax6000-jidu6j11.*`
inside the fetched tree, and those files are absent upstream, so the
package installs *nothing* and ships generic IPQ9574 board data instead.

The chain that has to hold, and does:

| Step | Where |
|---|---|
| the board is in `ALLWIFIBOARDS` | `package/firmware/ipq-wifi/Makefile` |
| the package is generated | `$(eval $(call generate-ipq-wifi-package,jiorouter_ax6000-jidu6j11,…))` |
| the files exist at the pinned SHA | `Sandiep/firmware_qca-wireless` @ `JIDU6J11` |
| the suffix picks the right radio | `.ipq9574` → `ath11k/IPQ9574/hw1.0/`, `.qcn9074` → `ath11k/QCN9074/hw1.0/` |
| the files land in the rootfs | asserted in CI by unpacking the squashfs |

Guarded three ways: the workflow greps the patched Makefile and **fails
the build** if either the fork URL or the pinned SHA is missing; a CI step
unpacks the squashfs and asserts both `board-2.bin` files are present and
non-empty; and the built package's version string carries the fork's
SHA (`…~a8974a05-r2`) so a stale package is obvious.

---

## 4. How this differs from the community release

The published community image (`themayankraut/JIO-AX6000-6J11-OpenWRT-SSH`
v1.0.0) is built from the **older** `JIDU6J11` branch and uses a **single
generic device tree** for all five 6j11 models:

```
model      = "JioRouter AX6000 JIDU6J11"
compatible = "jiorouter,ax6000-jidu6j11", "qcom,ipq9574"
```

Verified by parsing the shipped `sysupgrade.bin` FIT/DT directly — the
string `6111` does not appear in it. This build selects the
**6111-specific** target instead, whose device tree adds:

```
compatible = "jiorouter,ax6000-jidu6j11-6111", "jiorouter,ax6000-jidu6j11", "qcom,ipq9574"
&wifi0 { qcom,ath11k-calibration-variant = "JIDU6J11-6111"; };
&wifi1 { qcom,ath11k-calibration-variant = "JIDU6J11-6111"; };
```

`qcom,ath11k-calibration-variant` is read by ath11k upstream
(`drivers/net/wireless/ath/ath11k/core.c`, `ath11k_set_board_data()`),
which appends `,variant=<string>` to the BDF lookup name. So the 6111
build asks for a *different* Wi-Fi calibration blob than the generic build.

### Also different: the sysupgrade partition scheme

| | Community v1.0.0 (`JIDU6J11` branch) | This build (`JIDU6J11-PR` branch) |
|---|---|---|
| `platform.sh` | no `jiorouter_initial_setup`; `CI_UBIPART="firmware"` | `jiorouter_initial_setup` runs from the initramfs: writes `/etc/fw_env.config` for `0:APPSBLENV`, then `fw_setenv mtdids 'nand0=nand0'`, `fw_setenv mtdparts 'mtdparts=nand0:0xe100000@0x1700000(ubi)'`, `bootcmd 'ubi part ubi; ubi read 0x44000000 kernel; bootm 0x44000000'`, `bootargs 'console=ttyMSM0,115200n8 ubi.mtd=ubi root=/dev/ubiblock0_1 rootfstype=squashfs'` |
| Kernel load address | `0x46000000` | `0x44000000` (permanent), `0x46000000` (initramfs) |
| Post-sysupgrade layout | `firmware` partition | single `ubi` partition, `0xe100000` @ `0x1700000` |

> `jiorouter_initial_setup` only runs when `rootfs_type` is `tmpfs` — i.e.
> **from the initramfs**, which is exactly why the initramfs is the correct
> first flash. It is the step that lays down the U-Boot environment the
> squashfs image then boots from.
>
> It also **fails hard** if it cannot find the `0:APPSBLENV` partition
> (`find_mtd_index 0:APPSBLENV`): "unable to find mtd partition
> 0:APPSBLENV" and it returns 1, so the upgrade aborts. If you see that,
> your unit's partition table does not match what this branch expects —
> stop and check `/proc/mtd` before continuing.

**Consequence:** the sysupgrade half of the flash is *not* the community
procedure. After stage 1 boots the initramfs, the `platform.sh` on this
build rewrites the U-Boot environment itself. Follow the images this
build produces, not the community's `flash.sh`, for step 2. Stage 1
(writing the factory UBI to the idle slot) is the same.

---

## 5. User-facing features

Installed from `files/` into the OpenWrt rootfs.

### `jidu-isp` — use any ISP, not just Jio

The stock router's WAN profile is locked to Jio Air Fiber. This switches
the WAN interface to a standard OpenWrt UCI profile:

```sh
# PPPoE (most Indian ISPs)
sh jidu-isp.sh pppoe <username> <password> [lan_ip] [netmask]

# DHCP (cable/leased line)
sh jidu-isp.sh dhcp [lan_ip] [netmask]

# Static
sh jidu-isp.sh static <ip> <netmask> <gw> <dns> [lan_ip] [netmask]

# Inspect the current setup
sh jidu-isp.sh show
```

Each mode rewrites `network.wan`, fixes the WAN firewall zone (NAT, input
REJECT), and re-applies the Wi-Fi settings. Defaults: LAN `192.168.1.1/24`.
Change it if the ISP's own router also hands out `192.168.1.x` — the two
routers will otherwise fight.

### `jidu-identify` — know what you actually have

A **read-only** report of the running system, so you can confirm the
hardware matches the build assumptions before doing anything irreversible:

- device-tree `model` and `compatible`
- SoC, CPU count, RAM, NAND size
- full `/proc/mtd` partition table with offsets and sizes
- Wi-Fi board data present, and its size
- U-Boot environment (`bootcmd`, `bootargs`, `mtdparts`, …)
- a verdict per check: OK / MISMATCH

```sh
sh jidu-identify.sh
```

### `jidu-bootguard` — recover from a failed boot automatically

This is the most important feature. The final sysupgrade **replaces** the
stock partition table and rewrites U-Boot. If the new firmware does not
come up, the router is unreachable and normally needs a UART cable.

The guard turns that into a self-healing loop:

1. On start, it increments a `jidu_boot_attempts` counter in the U-Boot
   environment.
2. If the system shuts down cleanly, the counter is reset to `0`.
3. If the counter exceeds the threshold (default 3), the guard rewrites
   `bootcmd` to point at the alternate A/B slot, then to the stock
   `bootipq`, then to the platform's own boot command — and reboots.

Inspect it with:

```sh
fw_printenv jidu_boot_attempts
logread -e jidu-bootguard
```

Set the threshold with `JIDU_BOOT_GUARD_THRESHOLD` in
`/etc/config/jidu-bootguard`.

### First-boot defaults (`99-jidu-first-boot`)

- Wi-Fi regulatory domain set to **IN** (the BDF may carry a foreign
  domain, which makes the channel plan and TX power illegal in India)
- WPA2/WPA3 **mixed** encryption, `ieee80211w=1` — older phones and IoT
  devices still connect
- LAN forced to a static `192.168.1.1/24` so it cannot collide

---

## 6. Artifacts and the acceptance-test suite

Each successful build publishes three flashable files plus a checksum file.

| File | What it is |
|---|---|
| `openwrt-qualcommbe-ipq95xx-jiorouter_ax6000-jidu6j11-initramfs-uImage.itb` | FIT containing kernel + initramfs. **Boot this first** — it is the safe path. |
| `openwrt-qualcommbe-ipq95xx-jiorouter_ax6000-jidu6j11-initramfs-factory.ubi` | UBI image for a full flash from recovery. |
| `openwrt-qualcommbe-ipq95xx-jidorouter_ax6000-jidu6j11-squashfs-sysupgrade.bin` | Normal `sysupgrade` package for an already-flashed system. |
| `sha256sums.txt` | Digests for all three. |

`tests/test_artifacts.py` runs in CI and **fails the build** if anything
is wrong. It is a real parser, not a grep:

- **FIT/FDT parser** — walks the flattened device tree, so it reads actual
  properties rather than matching strings.
- Checks the embedded device tree's `model` and `compatible` name
  `jiorouter,ax6000-jidu6j11-6111` — **and** that the FIT header is not
  mistaken for the device tree (both share the `0xd00dfeed` magic).
- Checks the per-model Wi-Fi calibration property is `JIDU6J11-6111` on
  every radio.
- Checks the NAND partition table: the merged `ubi` partition must be at
  offset `0x01700000`, size `0x0e100000`, and the expected partition
  labels (`ubi`, `MFG`, `0:ART`, `0:BDF`, `Jio-Reserved`) and interface
  labels (`lan1`–`lan4`, `wan`) must be present.
- Parses the UBI headers (EC header, VID header, volume table) and
  confirms a `kernel` volume and an embedded 6111 device tree.
- Parses the `sysupgrade` tar and checks `CONTROL` names the 6111 board.
- Runs `shellcheck` over every feature script.

> The suite is deliberately strict: it was validated against the
> **community generic** image and correctly **fails** it on exactly the
> 6111-specific checks. A test that cannot fail is not a test.

---

## 7. Safe flashing procedure

> **Read this before flashing.** A wrong flash can brick the router.
> A UART cable (3.3 V TTL, TX/RX/GND) is effectively mandatory for
> recovery.

### 0. Back up the stock firmware first

On the **stock** firmware, before doing anything else:

```sh
# full NAND backup - the only true way back
cat /dev/mtd0 > /tmp/mtd0.bin
cat /dev/mtd23 > /tmp/mtd23.bin   # rootfs
cat /dev/mtd24 > /tmp/mtd24.bin   # rootfs_1
dd if=/dev/mtdblock23 of=/tmp/rootfs.full bs=1M

# and the U-Boot environment
fw_printenv > /tmp/fw_env.txt
dd if=/dev/mtdblock0 of=/tmp/uboot_env.bin bs=1k
```

Copy those off the router to your PC. **Do not skip this.**

### 1. Get root (no flash required)

The `wpfyorg/idu-unlock` project gives root SSH on firmware `R3.2.6` and
older. It does not write to flash — it is completely reversible, and it
is the right first step: it gives you a shell to run the backup from.

### 2. Flash the initramfs first

Do **not** go straight to the squashfs image. The initramfs boots into
RAM and runs entirely from there, so it cannot leave the router
unbootable — if it fails, nothing has been overwritten and the stock
firmware still boots.

The `JIDU6J11-PR` platform expects a single `ubi` partition at
`0xe100000` @ `0x1700000`, loaded at `0x44000000`:

```sh
ubi part ubi
ubi read 0x44000000 kernel
bootm 0x44000000
```

Flash it over TFTP/serial per the `Sandiep/openwrt` PR documentation, and
**follow the PR branch's own procedure, not the community release's
`flash.sh`.** The two use different partition tables and different
U-Boot environments.

### 3. Verify before committing to the flash

From the initramfs:

```sh
sh /usr/bin/jidu-identify.sh
```

Confirm:
- `model` says the 6111 variant
- the `ubi` partition is at `0x01700000` with size `0x0e100000`
- the Wi-Fi board data files are present and non-zero
- RAM and NAND sizes match the build

### 4. Install to flash

Only after the initramfs has booted cleanly and Wi-Fi/Ethernet work:

```sh
sysupgrade -n <squashfs-sysupgrade.bin>
```

### 5. Have a recovery plan

If the router stops responding:
1. Wait 3 boot attempts — `jidu-bootguard` should roll back on its own.
2. If it does not, attach the UART cable (3.3 V TTL) and interrupt U-Boot.
3. Restore the backup from step 0.

---

## 8. Known limitations

- **No hardware validation yet.** The build and the artifacts are
  verified automatically; PPPoE handshakes, Wi-Fi 5/6 throughput, reboot
  persistence, and the rollback path can only be proven on the device.
- **The BDF source is an unofficial fork.** It is SHA-pinned, but it is
  not upstream and could be changed or removed.
- **RAM/NAND are inferred, not confirmed.** NAND is commonly 256 MB, but
  verify the actual unit.
- **The community release and this build are not interchangeable.** The
  community image uses an `mtd23`/`mtd24` A/B layout; this build uses a
  single `ubi` partition. Flashing one and then the other will not work.
- `jioMfgData init` is a destructive factory wipe. Only ever run it with
  a verified backup **and** a UART console available.

---

## 9. Running a build

Push a tag, or dispatch the workflow manually:

```sh
git tag v1 && git push --tags
# or: gh workflow run build.yml
```

The workflow:

1. checks out `Sandiep/openwrt` @ `JIDU6J11-PR`
2. applies the BDF patch and **asserts** it landed
3. installs the feature overlay into `openwrt/files/`
4. runs `shellcheck` over every feature script
5. builds with `CONFIG_TARGET_PROFILE=DEVICE_jiorouter_ax6000-jidu6j11-6111`
   and **asserts** that exact profile was selected
6. builds the factory UBI
7. runs the **feature tests** (70 functional tests against a mocked OpenWrt
   environment)
8. unpacks the **squashfs rootfs** and asserts the Wi-Fi BDF and the feature
   scripts are really inside it
9. runs the **artifact acceptance tests** (66 checks on the produced files)
10. publishes the artifacts and a release

Every step that could silently produce a wrong-board image is guarded by
an explicit assertion.

---

## 10. Repository layout

```
.github/workflows/build.yml   the whole build + test + release pipeline
files/usr/bin/jidu-isp.sh     ISP switching (PPPoE / DHCP / static)
files/usr/bin/jidu-identify.sh read-only hardware identity report
files/etc/init.d/jidu-bootguard  automatic boot-failure rollback
files/etc/uci-defaults/99-jidu-first-boot  first-boot defaults
tests/test_artifacts.py       artifact acceptance tests (FIT/FDT/UBI/tar parsers)
tests/test_features.py        feature tests (mocked OpenWrt environment)
docs/HARDWARE-VERIFICATION.md the checklist to run on the actual router
```

---

## 11. Licence and attribution

This repository contains **no** OpenWrt source — it builds someone else's
tree. Attribution:

- [OpenWrt](https://openwrt.org/) — GPL-2.0
- [`Sandiep/openwrt`](https://github.com/Sandiep/openwrt) — the 6j11 support tree
- [`Sandiep/firmware_qca-wireless`](https://github.com/Sandiep/firmware_qca-wireless) — per-model board data
- [`wpfyorg/idu-unlock`](https://github.com/wpfyorg/idu-unlock) — the Stage-1 root/SSH route
- [`themayankraut/JIO-AX6000-6J11-OpenWRT-SSH`](https://github.com/themayankraut/JIO-AX6000-6J11-OpenWRT-SSH) — community release used as the reference baseline
