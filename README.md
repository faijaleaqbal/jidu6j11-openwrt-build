# JIDU6J11-6111 — custom OpenWrt build

Self-hosted build of OpenWrt for the **Jio AX6000 JIDU6J11-6111**
(Qualcomm IPQ9574, `qualcommbe/ipq95xx` target).

## Where the sources come from

| Piece | Source | Why |
|---|---|---|
| OpenWrt tree | `Sandiep/openwrt` @ branch `JIDU6J11-PR` | The only tree with a **per-model** device tree for the 6111: `target/linux/qualcommbe/dts/ipq9554-jiorouter-ax6000-jidu6j11-6111.dts`. Upstream PR: [openwrt/openwrt#24432](https://github.com/openwrt/openwrt/pull/24432) (closed, unmerged — the 6j11 has no official OpenWrt). |
| Wi-Fi BDF | `Sandiep/firmware_qca-wireless` @ branch `JIDU6J11` | `board-jiorouter_ax6000-jidu6j11.{ipq9574,qcn9074}`. These are **not** in upstream `project/firmware/qca-wireless` at the revision the `ipq-wifi` package pins; upstream PR [openwrt/firmware_qca-wireless#146](https://github.com/openwrt/firmware_qca-wireless/pull/146) was closed without merging. |

## The one patch this build applies

`package/firmware/ipq-wifi/Makefile` is repointed from
`$(PROJECT_GIT)/project/firmware/qca-wireless.git` at the pinned upstream
revision to `https://github.com/Sandiep/firmware_qca-wireless.git` at
`a8974a05d1c6bc571a34f7e4ef858bc4689c402b` (hash check set to `skip`).

Without it the `ipq-wifi-jiorouter_ax6000-jidu6j11` package installs **nothing**:
its `install-overlay` globs `board-jiorouter_ax6000-jidu6j11.*` inside the
fetched tree, and those files are absent upstream. The build still succeeds —
it just ships the generic IPQ9574 board data instead of the JIDU one.

## How the build differs from the community release

The published community image (`themayankraut/JIO-AX6000-6J11-OpenWRT-SSH`
v1.0.0) is built from the **older** `JIDU6J11` branch and uses a **single
generic device tree** for all five 6j11 models:

```
model     = "JioRouter AX6000 JIDU6J11"
compatible = "jiorouter,ax6000-jidu6j11", "qcom,ipq9574"
```

Verified by parsing the shipped `sysupgrade.bin` FIT/DT directly — the string
`6111` does not appear in it. This build selects the **6111-specific** target
instead, whose device tree adds:

```
compatible = "jiorouter,ax6000-jidu6j11-6111", "jiorouter,ax6000-jidu6j11", "qcom,ipq9574"
&wifi0 { qcom,ath11k-calibration-variant = "JIDU6J11-6111"; };
&wifi1 { qcom,ath11k-calibration-variant = "JIDU6J11-6111"; };
```

`qcom,ath11k-calibration-variant` is read by ath11k upstream
(`drivers/net/wireless/ath/ath11k/core.c`, `ath11k_set_board_data()`), which
appends `,variant=<string>` to the BDF lookup name. So the 6111 build asks for
a *different* Wi-Fi calibration blob than the generic build.

### Also different: the sysupgrade partition scheme

| | Community v1.0.0 (`JIDU6J11` branch) | This build (`JIDU6J11-PR` branch) |
|---|---|---|
| `platform.sh` | no `jiorouter_initial_setup`; `CI_UBIPART="firmware"` | `jiorouter_initial_setup` runs from the initramfs: writes `/etc/fw_env.config` for `0:APPSBLENV`, then `fw_setenv mtdparts 'mtdparts=nand0:0xe100000@0x1700000(ubi)'`, `bootcmd 'ubi part ubi; ubi read 0x44000000 kernel; bootm 0x44000000'`, `bootargs 'console=ttyMSM0,115200n8 ubi.mtd=ubi root=/dev/ubiblock0_1 rootfstype=squashfs'` |
| Kernel load address | `0x46000000` | `0x44000000` (permanent), `0x46000000` (initramfs) |
| Post-sysupgrade layout | `firmware` partition | single `ubi` partition, `0xe100000` @ `0x1700000` |

**Consequence:** the sysupgrade half of the flash is *not* the community
procedure. After stage 1 boots the initramfs, the `platform.sh` on this build
rewrites the U-Boot environment itself. Follow the images this build produces,
not the community's `flash.sh`, for step 2. Stage 1 (writing the factory UBI to
the idle slot) is the same.

## Running a build

Push a tag, or dispatch the workflow manually:

```bash
git tag v1 && git push --tags
# or: gh workflow run build.yml
```

Artifacts land in the GitHub Actions run, named:

- `openwrt-qualcommbe-ipq95xx-jiorouter_ax6000-jidu6j11-initramfs-factory.ubi`
- `openwrt-qualcommbe-ipq95xx-jiorouter_ax6000-jidu6j11-squashfs-sysupgrade.bin`
- `openwrt-qualcommbe-ipq95xx-jiorouter_ax6000-jidu6j11-initramfs-uImage.itb`

## Before you flash anything

Read `docs/BACKUP.md` in the `wpfyorg/idu-unlock` repo and take a full backup
first. The 6j11 has **no official revert path** — after `sysupgrade` the stock
firmware is gone, and your only way back is the `mtd23`/`mtd24` images from
your own backup.

Also read the verification checklist before writing to flash: the idle-slot
name, its mtd number, and its offset/size come from *your* unit's
`/proc/cmdline` and `/proc/mtd`, never from these docs.
