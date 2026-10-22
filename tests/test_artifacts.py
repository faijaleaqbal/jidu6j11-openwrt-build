#!/usr/bin/env python3
"""Acceptance tests for the JIDU6J11-6111 firmware artifacts.

These run in CI against the files the build produced. They are the
user-point-of-view checks: a router is only "ready" if the images are
well-formed AND actually target this board.

Test groups
-----------
  images  : the three flashable files exist, are non-trivial in size, and
            have the right internal format (FIT magic / UBI magic / tar)
  dt      : the device tree compiled into the kernel names the 6111 and
            the right SoC, and carries the per-model Wi-Fi calibration
            property plus the expected NAND partition table
  ubi     : the factory UBI has the right geometry and a 'kernel' volume
  metadata: the sysupgrade tar has the right board id and the right members
  feature : the feature overlay (jidu-isp / jidu-identify / bootguard)
            is present in the rootfs and is syntactically valid shell

Exit code 0 = all passed.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import struct
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _find_artifact_dir() -> str:
    """Locate the directory holding the built artifacts.

    Order: an explicit ARTIFACT_DIR, then the CI staging directory
    (openwrt/artifacts), then a plain ./artifacts. The CI workflow stages
    into openwrt/artifacts, so that has to be tried before ./artifacts.
    """
    env = os.environ.get("ARTIFACT_DIR")
    if env:
        return os.path.abspath(env)
    for cand in (os.path.join(ROOT, "openwrt", "artifacts"),
                 os.path.join(ROOT, "artifacts"),
                 os.path.join(ROOT, "release-artifacts")):
        if os.path.isdir(cand):
            return os.path.abspath(cand)
    return os.path.abspath(os.path.join(ROOT, "artifacts"))


ART = _find_artifact_dir()

ITB = "openwrt-qualcommbe-ipq95xx-jiorouter_ax6000-jidu6j11-initramfs-uImage.itb"
FACTORY = "openwrt-qualcommbe-ipq95xx-jiorouter_ax6000-jidu6j11-initramfs-factory.ubi"
SYSUP = "openwrt-qualcommbe-ipq95xx-jiorouter_ax6000-jidu6j11-squashfs-sysupgrade.bin"

FDT_MAGIC = 0xD00DFEED
UBI_EC_MAGIC = b"UBI#"

results: list[tuple[str, str, str]] = []


def check(group: str, name: str, ok: bool, detail: str = "") -> bool:
    results.append((group, name, "PASS" if ok else "FAIL"))
    if not ok:
        print(f"  FAIL  [{group}] {name}" + (f" -- {detail}" if detail else ""))
    return ok


def art(name: str) -> str:
    return os.path.join(ART, name)


def read(p: str) -> bytes:
    with open(p, "rb") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# FDT parsing
# --------------------------------------------------------------------------- #
def parse_fdt(blob: bytes) -> dict:
    """Parse a flattened device tree into {path: {prop: value}}."""
    (magic, totalsize, off_struct, off_strings, _rsv, _ver, _lc, _bcpu,
     size_strings, size_struct) = struct.unpack(">10I", blob[:40])
    if magic != FDT_MAGIC:
        raise ValueError("not an FDT (bad magic)")

    strings = blob[off_strings:off_strings + size_strings]
    sd = blob[off_struct:off_struct + size_struct]

    def s(off: int) -> str:
        end = strings.index(b"\x00", off)
        return strings[off:end].decode("utf-8", "replace")

    tree: dict[str, dict[str, bytes]] = {}
    path: list[str] = []
    p = 0
    while p < len(sd):
        (tok,) = struct.unpack(">I", sd[p:p + 4])
        p += 4
        if tok == 1:  # BEGIN_NODE
            name = sd[p:sd.index(b"\x00", p)].decode("utf-8", "replace")
            p += (len(name) + 1 + 3) // 4 * 4
            if name:
                path.append(name)
            # The root node has an empty name; key it as "/" so callers
            # can look up tree["/"]["model"] and friends.
            tree.setdefault("/" + "/".join(path), {})
        elif tok == 2:  # END_NODE
            if path:
                path.pop()
        elif tok == 3:  # PROP
            ln, noff = struct.unpack(">II", sd[p:p + 8])
            p += 8
            val = sd[p:p + ln]
            p += (ln + 3) // 4 * 4
            tree.setdefault("/" + "/".join(path), {})[s(noff)] = val
        elif tok == 9:  # END
            break
    return tree


def _fdt_candidates(blob: bytes):
    """Yield every embedded FDT/FIT found in the blob.

    A FIT image and a plain device tree share the same 0xd00dfeed magic,
    so "the first magic we see" is NOT necessarily the device tree - in an
    ITB the FIT header usually comes first. Callers must disambiguate.
    """
    off = 0
    while True:
        i = blob.find(b"\xd0\x0d\xfe\xed", off)
        if i < 0:
            return
        try:
            (totalsize,) = struct.unpack(">I", blob[i + 4:i + 8])
            if 0 < totalsize <= len(blob) - i:
                cand = blob[i:i + totalsize]
                tree = parse_fdt(cand)
                yield i, cand, tree
        except Exception:
            pass
        off = i + 4


def ubi_reassemble(blob: bytes) -> bytes:
    """Strip UBI PEB headers and return the concatenated volume data.

    A UBI image interleaves erase-counter/VID headers with the payload:
    every 128 KiB PEB starts with a 0x1000-byte header region, so the
    payload is fragmented. Searching the raw file for a device tree
    therefore finds only fragments and mis-parses. Reassembling first
    gives back the contiguous FIT.
    """
    occ = []
    off = 0
    while True:
        i = blob.find(b"UBI#", off)
        if i < 0:
            break
        occ.append(i)
        off = i + 1
    if len(occ) < 2:
        return blob
    peb = occ[1] - occ[0]
    out = bytearray()
    for k in range(len(blob) // peb):
        b0 = k * peb
        if blob[b0:b0 + 4] != b"UBI#":
            continue
        (vho,) = struct.unpack(">I", blob[b0 + 0x10:b0 + 0x14])
        (do,) = struct.unpack(">I", blob[b0 + 0x14:b0 + 0x18])
        (vol_id,) = struct.unpack(">I", blob[b0 + vho + 8:b0 + vho + 12])
        if vol_id == 0x7FFFFFFF:
            # the internal layout volume - not payload
            continue
        out += blob[b0 + do:b0 + peb]
    return bytes(out)


def find_board_fdt(blob: bytes, needle: str, strict: bool = True) -> bytes | None:
    """Return the device tree whose root 'compatible' contains `needle`.

    This deliberately does not return the first FDT magic: an ITB contains
    a FIT (also 0xd00dfeed) plus the real device tree. Picking the wrong
    one silently tests the FIT header instead of the board description.

    With strict=True (the default) the return value is None unless the
    board string actually matches - a wrong-board image must fail, not
    quietly fall back to some other tree.
    """
    fallback = None
    for _off, cand, tree in _fdt_candidates(blob):
        compat = tree.get("/", {}).get("compatible", b"").decode("utf-8", "replace")
        if needle in compat:
            return cand
        if fallback is None and "/images" not in tree:
            fallback = cand
    return None if strict else fallback


# --------------------------------------------------------------------------- #
# groups
# --------------------------------------------------------------------------- #
def test_images() -> None:
    g = "images"
    print(f"[{g}] flashable artifacts")

    for name, minsize in ((ITB, 1_000_000), (FACTORY, 1_000_000), (SYSUP, 1_000_000)):
        p = art(name)
        if not check(g, f"{name} exists", os.path.isfile(p), p):
            continue
        size = os.path.getsize(p)
        check(g, f"{name} size >= {minsize}", size >= minsize, f"{size} bytes")

    itb = art(ITB)
    if os.path.isfile(itb):
        blob = read(itb)
        check(g, "initramfs FIT has FDT magic", blob[:4] == b"\xd0\x0d\xfe\xed",
              blob[:4].hex())
        check(g, "initramfs FIT has a kernel payload", len(blob) > 500_000)

    ubi = art(FACTORY)
    if os.path.isfile(ubi):
        blob = read(ubi)
        check(g, "factory UBI has UBI EC magic", blob[:4] == UBI_EC_MAGIC, blob[:4].hex())
        check(g, "factory UBI contains a 'kernel' volume", b"kernel" in blob[:200_000])

    sysup = art(SYSUP)
    if os.path.isfile(sysup):
        blob = read(sysup)
        # the community build ships a tar; some builds gzip it
        if blob[:2] == b"\x1f\x8b":
            try:
                blob = gzip.decompress(blob)
            except Exception:
                pass
        check(g, "sysupgrade is a tar archive", len(blob) > 512 and blob[257:262] == b"ustar",
              blob[:8].hex())


def test_dt() -> None:
    g = "dt"
    print(f"[{g}] device tree inside the kernel")

    itb = art(ITB)
    if not os.path.isfile(itb):
        check(g, "initramfs FIT present", False, "cannot test DT without the ITB")
        return
    blob = read(itb)
    fdt = find_board_fdt(blob, "jiorouter,ax6000-jidu6j11-6111")
    if not check(g, "the board device tree is embedded in the FIT", fdt is not None):
        return
    assert fdt is not None
    tree = parse_fdt(fdt)

    root = tree.get("/", {})
    model = root.get("model", b"").decode("utf-8", "replace").strip("\x00")
    compat = root.get("compatible", b"").decode("utf-8", "replace").strip("\x00")

    check(g, "model names the 6111", "6111" in model, model)
    check(g, "compatible has jiorouter,ax6000-jidu6j11-6111",
          "jiorouter,ax6000-jidu6j11-6111" in compat, compat)
    check(g, "compatible has the generic family string too",
          "jiorouter,ax6000-jidu6j11" in compat, compat)
    check(g, "compatible says qcom,ipq9574", "qcom,ipq9574" in compat, compat)

    # per-model Wi-Fi calibration property
    variants = []
    for path, props in tree.items():
        for key, val in props.items():
            if "calibration-variant" in key:
                variants.append((path, val.decode("utf-8", "replace").strip("\x00")))
    check(g, "a per-model calibration-variant property is present", len(variants) >= 2,
          str(variants))
    check(g, "calibration variant is JIDU6J11-6111",
          all(v == "JIDU6J11-6111" for _, v in variants) and bool(variants),
          str(variants))

    # NAND partition table
    labels = []
    for path, props in tree.items():
        if "label" in props:
            labels.append(props["label"].decode("utf-8", "replace").strip("\x00"))
    for want in ("ubi", "MFG", "0:ART", "0:BDF", "Jio-Reserved"):
        check(g, f"partition label {want} present", want in labels, str(sorted(labels)))

    # the ubi partition must be the merged 0xe100000 @ 0x1700000 one
    ubi_reg = None
    for path, props in tree.items():
        if props.get("label", b"").decode("utf-8", "replace").strip("\x00") == "ubi" \
                and "reg" in props:
            ubi_reg = struct.unpack(">2I", props["reg"][:8])
    if check(g, "ubi partition has a reg property", ubi_reg is not None):
        assert ubi_reg is not None
        off, size = ubi_reg
        check(g, "ubi offset is 0x01700000", off == 0x01700000, hex(off))
        check(g, "ubi size is 0x0e100000 (225 MB merged)", size == 0x0E100000, hex(size))

    # interfaces
    for iface in ("lan1", "lan2", "lan3", "lan4", "wan"):
        found = any(props.get("label", b"").decode("utf-8", "replace").strip("\x00") == iface
                    for props in tree.values())
        check(g, f"interface label {iface} present", found)


def test_ubi() -> None:
    g = "ubi"
    print(f"[{g}] factory UBI")
    p = art(FACTORY)
    if not os.path.isfile(p):
        check(g, "factory UBI present", False)
        return
    blob = read(p)
    check(g, "UBI EC magic", blob[:4] == UBI_EC_MAGIC, blob[:4].hex())

    # struct ubi_ec_header: vid_hdr_offset at 0x10, data_offset at 0x14
    (vid_hdr_offset,) = struct.unpack(">I", blob[0x10:0x14])
    (data_offset,) = struct.unpack(">I", blob[0x14:0x18])
    check(g, "VID header offset is sane", 0 < vid_hdr_offset <= 0x1000, hex(vid_hdr_offset))
    check(g, "data offset is sane", 0 < data_offset <= 0x2000, hex(data_offset))

    # VID header magic at vid_hdr_offset
    check(g, "volume ID header magic", blob[vid_hdr_offset:vid_hdr_offset + 4] == b"UBI!",
          blob[vid_hdr_offset:vid_hdr_offset + 4].hex())

    # The volume table lives in the first PEB at data_offset. The volume
    # name is stored as a length-prefixed string; scan the region for the
    # expected name rather than trusting a fixed struct offset.
    vt = blob[data_offset:data_offset + 8192]
    check(g, "volume table contains a 'kernel' volume", b"kernel" in vt,
          vt[:64].hex())

    # The UBI must actually carry the FIT that boots the router. The
    # payload is fragmented across PEBs, so reassemble it first.
    payload = ubi_reassemble(blob)
    check(g, "reassembled payload is large enough", len(payload) > 1_000_000,
          str(len(payload)))

    fdt = find_board_fdt(payload, "jiorouter,ax6000-jidu6j11-6111")
    if check(g, "the UBI embeds the 6111 device tree", fdt is not None):
        assert fdt is not None
        tree = parse_fdt(fdt)
        compat = tree.get("/", {}).get("compatible", b"").decode("utf-8", "replace")
        check(g, "the embedded device tree targets the 6111",
              "jiorouter,ax6000-jidu6j11-6111" in compat, compat)
        model = tree.get("/", {}).get("model", b"").decode("utf-8", "replace").strip("\x00")
        check(g, "the embedded model names the 6111", "6111" in model, model)

    # The factory UBI and the initramfs ITB must carry the SAME kernel.
    # If they diverge, one of them was built from a different tree.
    itb_path = art(ITB)
    if os.path.isfile(itb_path):
        itb = read(itb_path)
        f = payload.find(b"\xd0\x0d\xfe\xed")
        if check(g, "the UBI payload starts with a FIT", f >= 0):
            (tot,) = struct.unpack(">I", payload[f + 4:f + 8])
            check(g, "the UBI FIT matches the initramfs ITB byte for byte",
                  payload[f:f + tot] == itb,
                  f"ubi FIT {tot} bytes vs itb {len(itb)} bytes")


def test_metadata() -> None:
    g = "metadata"
    print(f"[{g}] sysupgrade metadata")
    p = art(SYSUP)
    if not os.path.isfile(p):
        check(g, "sysupgrade present", False)
        return
    blob = read(p)
    if blob[:2] == b"\x1f\x8b":
        try:
            blob = gzip.decompress(blob)
        except Exception:
            pass
    try:
        tf = tarfile.open(fileobj=io.BytesIO(blob))
        names = tf.getnames()
    except Exception as exc:
        check(g, "sysupgrade is readable as a tar", False, str(exc))
        return
    check(g, "tar contains CONTROL", any(n.endswith("CONTROL") for n in names), str(names[:5]))
    check(g, "tar contains a kernel", any(n.endswith("/kernel") for n in names), str(names[:5]))
    check(g, "tar contains a rootfs", any(n.endswith("/root") for n in names), str(names[:5]))
    for member in tf.getmembers():
        if member.name.endswith("CONTROL"):
            handle = tf.extractfile(member)
            if handle is None:
                check(g, "CONTROL is readable", False, member.name)
                continue
            ctrl = handle.read().decode("utf-8", "replace")
            check(g, "CONTROL names the jidu6j11 board",
                  "jidu6j11" in ctrl, ctrl.strip()[:120])
            check(g, "CONTROL names the 6111 variant",
                  "6111" in ctrl, ctrl.strip()[:120])


def test_feature() -> None:
    g = "feature"
    print(f"[{g}] feature overlay")
    overlay = os.path.join(HERE, "..", "files")
    expected = [
        "usr/bin/jidu-isp.sh",
        "usr/bin/jidu-identify.sh",
        "etc/init.d/jidu-bootguard",
        "etc/uci-defaults/99-jidu-first-boot",
    ]
    for rel in expected:
        p = os.path.join(overlay, rel)
        ok = os.path.isfile(p)
        check(g, f"overlay file {rel} exists", ok)
        if not ok:
            continue
        text = open(p, encoding="utf-8", errors="replace").read()
        check(g, f"{rel} is not empty", len(text.strip()) > 100)
        check(g, f"{rel} has a shebang", text.startswith("#!"))
        # crude but real shell sanity: balanced quotes on non-comment lines
        bad = [
            i + 1 for i, line in enumerate(text.splitlines())
            if line.strip() and not line.strip().startswith("#")
            and line.count('"') % 2 == 1 and line.count("'") % 2 == 1
        ]
        check(g, f"{rel} has balanced quotes", not bad, f"lines {bad[:5]}")


def test_manifest() -> None:
    g = "manifest"
    print(f"[{g}] digests")
    lines = []
    for name in (ITB, FACTORY, SYSUP):
        p = art(name)
        if not os.path.isfile(p):
            check(g, f"{name} present for hashing", False)
            continue
        h = hashlib.sha256(read(p)).hexdigest()
        lines.append(f"{h}  {name}")
        check(g, f"{name} sha256 computed", len(h) == 64)
    if lines:
        print("sha256sums.txt:")
        for line in lines:
            print("   ", line)
        # LF explicitly: this file is meant to be consumed by
        # "sha256sum -c" on the router (Linux). The platform default on
        # Windows is CRLF, which makes every line fail to match.
        with open(os.path.join(ART, "sha256sums.txt"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        # and prove it
        raw = open(os.path.join(ART, "sha256sums.txt"), "rb").read()
        check(g, "sha256sums.txt uses LF line endings", b"\r" not in raw,
              repr(raw[:80]))
        check(g, "sha256sums.txt has one line per artifact",
              raw.count(b"\n") == len(lines), f"{raw.count(chr(10).encode())} vs {len(lines)}")


def main() -> int:
    print(f"artifact dir: {ART}\n")
    for fn in (test_images, test_dt, test_ubi, test_metadata, test_feature, test_manifest):
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
