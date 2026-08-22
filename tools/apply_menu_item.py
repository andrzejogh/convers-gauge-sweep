#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_menu_item.py - add your own on/off item to the Convers+ `Advanced` menu screen
(KOD partition, base 0x5000).

REQUIRES M0RTAR'S MODIFIED FIRMWARE. On this cluster the live `Advanced` screen is NOT
the factory descriptor at 0x08A9C8 - that one is dead. m0rtar relocated the screen to
0x0DD224 (Digital speed / Engine temp & Voltage / on main / in standby) and hooked it up
with two pointers in the screen tables at 0x04B398 and 0x04C070. This script does exactly
the same thing one more time: it clones the live descriptor into free space, appends one
record, and repoints those same two pointers. Nothing is moved in place.

On stock firmware the descriptor is not there, the header check below fails, and the
script aborts without writing anything. That is the intended behaviour - it cannot
damage an unmodified image.

FORMAT (offsets from the header H = "00 6B 00 07")
  H+0x04..H+0x33  12 x BE32  screen title (prologue + ASCII fallback + 10 languages)
  H+0x34          <- this is what the screen tables point at
  H+0x3C          item count (u16 twice)
  H+0x7C          first item record
  record = 0x48 B: +0x00 type (0x37 = on/off, 0x20 = label only)
                   +0x04..+0x2F  11 x BE32 label per language
                   +0x30,+0x34   state icons
                   +0x3C         pointer to the SETTING BYTE in RAM

The setting byte is written and persisted by the cluster's own menu engine, including
storage in EEPROM - this script only tells the engine which byte to use.

Usage:
  python apply_menu_item.py <in.bin> <out.bin> [--label "Gauge sweep"]
                            [--value-addr 0x400018BB] [--new-base 0xDDD00] [--index 1]
"""
import struct, sys

BASE = 0x5000
FILL = 0xEF                       # filler byte used for free space in this partition

# --- the live Advanced screen in m0rtar-modified firmware ---
SRC_START = 0x0DD200              # descriptor prologue
SRC_END = 0x0DD3EC                # end of block (only 0xEF beyond this)
HDR_OFF = 0x40                    # H = SRC_START + 0x40
ITEM_STRIDE = 0x48
FIRST_ITEM_OFF = HDR_OFF + 0x7C
CNT_OFF = HDR_OFF + 0x3C
TBLPTR_OFF = HDR_OFF + 0x34       # what the screen tables point at

NEW_BASE = 0x0DDD00               # free 0xEF space (~100 KB of it from 0x0DDC64)
VALUE_ADDR = 0x400018D1           # setting byte written by the menu engine
LABEL = "Gauge sweep"
INDEX = 1                         # insert right after "Digital speed", before the temperature section
SLOT = 0x20                       # menu string slots are 0x20 B


def _opt(argv, name, default, conv):
    if name in argv:
        i = argv.index(name); v = conv(argv[i + 1]); del argv[i:i + 2]; return v
    return default


def build(data, label=LABEL, value_addr=VALUE_ADDR, new_base=NEW_BASE, index=INDEX,
          quiet=False):
    """Returns (new_image, report). Does not modify 'data'."""
    d = bytearray(data)
    say = (lambda *a: None) if quiet else print

    def rd32(va): return struct.unpack(">I", d[va - BASE:va - BASE + 4])[0]

    src_len = SRC_END - SRC_START
    H = SRC_START + HDR_OFF

    # ---------- 1. validate the input image ----------
    assert rd32(H) == 0x006B0007, f"no screen header @{H:#x} (found {rd32(H):#010x}) - this image does not have m0rtar's relocated Advanced menu; the menu variant of the patch does not apply here"
    cnt_a, cnt_b = struct.unpack(">HH", d[H + 0x3C - BASE:H + 0x40 - BASE])
    assert cnt_a == cnt_b, f"inconsistent item count: {cnt_a} / {cnt_b}"
    n_items = cnt_a
    first_item = SRC_START + FIRST_ITEM_OFF
    items_end = first_item + n_items * ITEM_STRIDE
    assert rd32(first_item) == 0x37, f"first item is not type 0x37 ({rd32(first_item):#x})"
    say(f"source: screen @{SRC_START:#x} (H={H:#x}), {n_items} items, "
        f"records {first_item:#x}..{items_end:#x}")

    # pointers from the screen tables (outside the block) to H+0x34
    tbl_target = SRC_START + TBLPTR_OFF
    slots = [o + BASE for o in range(0, len(d) - 4, 2)
             if struct.unpack(">I", d[o:o + 4])[0] == tbl_target
             and not (SRC_START <= o + BASE < SRC_END)]
    assert slots, f"found no screen-table entry pointing at {tbl_target:#x}"
    say(f"screen tables: {len(slots)} entries -> " + ", ".join(f"{s:#x}" for s in slots))

    # ---------- 2. layout plan for the new location ----------
    # [0 .. src_len)          copy of the source
    # the new record goes RIGHT AFTER the last one (the engine walks by index, stride 0x48)
    new_item_off = (items_end - SRC_START)
    new_item_end = new_item_off + ITEM_STRIDE
    tail_off = ((new_item_end + 3) & ~3)          # anything displaced + the label go here
    label_off = tail_off                           # (filled in below)

    # Would the new record overwrite data inside the block? In this firmware yes:
    # a 12-byte block @0x0DD3E0 pointed at from H+0x68. We locate it by that pointer.
    clash = []
    for off in range(0, src_len, 4):
        v = struct.unpack(">I", d[SRC_START + off - BASE:SRC_START + off - BASE + 4])[0]
        if SRC_START <= v < SRC_END and new_item_off <= (v - SRC_START) < new_item_end:
            clash.append((off, v - SRC_START))
    assert len(clash) <= 1, f"more than one block would have to move: {clash}"

    out = bytearray(d[SRC_START - BASE:SRC_END - BASE])
    moved = None
    if clash:
        ptr_off, blob_off = clash[0]
        # the block runs to the end of the source, or to the first filler byte
        end = blob_off
        while end < src_len and out[end] != FILL:
            end += 1
        blob = bytes(out[blob_off:end])
        moved = (ptr_off, blob_off, len(blob))
        say(f"relocated block: {SRC_START + blob_off:#x} ({len(blob)} B), "
            f"pointer @{SRC_START + ptr_off:#x}")
        out[blob_off:end] = bytes([FILL]) * len(blob)
        while len(out) < tail_off:
            out.append(FILL)
        blob_new_off = len(out)
        out += blob
        struct.pack_into(">I", out, ptr_off, new_base + blob_new_off)
        label_off = (len(out) + 3) & ~3
    while len(out) < label_off:
        out.append(FILL)

    # the item label, in a 0x20 B slot
    enc = label.encode("latin-1")
    assert len(enc) < SLOT, f"label longer than {SLOT - 1} characters"
    out += enc + bytes([0]) * (SLOT - len(enc))
    label_addr = new_base + label_off

    # ---------- 3. relocate the copy's internal pointers ----------
    delta = new_base - SRC_START
    nreloc = 0
    for off in range(0, src_len, 4):
        v = struct.unpack(">I", out[off:off + 4])[0]
        if SRC_START <= v < SRC_END:
            struct.pack_into(">I", out, off, v + delta)
            nreloc += 1
    say(f"relocated {nreloc} internal pointers (delta {delta:+#x})")

    # ---------- 4. new item record: clone of the first one, inserted at 'index' ----------
    proto = bytearray(out[FIRST_ITEM_OFF:FIRST_ITEM_OFF + ITEM_STRIDE])
    for i in range(11):                                   # 11 label slots -> our string
        struct.pack_into(">I", proto, 0x04 + 4 * i, label_addr)
    struct.pack_into(">I", proto, 0x3C, value_addr)       # setting byte
    recs = [bytearray(out[FIRST_ITEM_OFF + i * ITEM_STRIDE:
                          FIRST_ITEM_OFF + (i + 1) * ITEM_STRIDE]) for i in range(n_items)]
    pos = n_items if index < 0 else min(index, n_items)
    recs.insert(pos, proto)
    for i, r in enumerate(recs):                          # rewrite the whole contiguous run of records
        out[FIRST_ITEM_OFF + i * ITEM_STRIDE:FIRST_ITEM_OFF + (i + 1) * ITEM_STRIDE] = r
    say(f"new item at index {pos} @{new_base + FIRST_ITEM_OFF + pos * ITEM_STRIDE:#x}: "
        f"'{label}', label @{label_addr:#x}, setting byte -> {value_addr:#010x}")

    # ---------- 5. item count ----------
    struct.pack_into(">HH", out, CNT_OFF, n_items + 1, n_items + 1)
    say(f"item count: {n_items} -> {n_items + 1}")

    # ---------- 6. write into the image + repoint the screen tables ----------
    no = new_base - BASE
    assert all(b == FILL for b in d[no:no + len(out)]), \
        f"target area {new_base:#x}..{new_base + len(out):#x} is not free (0xEF)"
    d[no:no + len(out)] = out
    new_tbl_target = new_base + TBLPTR_OFF
    for s in slots:
        struct.pack_into(">I", d, s - BASE, new_tbl_target)
    say(f"screen tables repointed: {tbl_target:#x} -> {new_tbl_target:#x}")

    # ---------- 7. sanity-check the result ----------
    for off in range(0, len(out), 4):
        v = struct.unpack(">I", out[off:off + 4])[0]
        assert not (SRC_START <= v < SRC_END), \
            f"a pointer to the old block survived @{new_base + off:#x} = {v:#x}"
    for i in range(n_items + 1):
        m = new_base + FIRST_ITEM_OFF + i * ITEM_STRIDE
        t = struct.unpack(">I", d[m - BASE:m - BASE + 4])[0]
        assert t in (0x37, 0x20), f"item {i} @{m:#x}: bad type {t:#x}"

    rep = dict(new_base=new_base, length=len(out), items=n_items + 1, index=pos,
               label_addr=label_addr, value_addr=value_addr, slots=slots,
               new_item=new_base + FIRST_ITEM_OFF + pos * ITEM_STRIDE, moved=moved)
    return bytes(d), rep


def main():
    argv = sys.argv[1:]
    label = _opt(argv, '--label', LABEL, str)
    value_addr = _opt(argv, '--value-addr', VALUE_ADDR, lambda x: int(x, 0))
    new_base = _opt(argv, '--new-base', NEW_BASE, lambda x: int(x, 0))
    index = _opt(argv, '--index', INDEX, lambda x: int(x, 0))
    if len(argv) < 2:
        sys.exit("usage: python apply_menu_item.py <in.bin> <out.bin> "
                 "[--label ...] [--value-addr ...]\n"
                 "       see the docstring at the top of this file")
    fi, fo = argv[0], argv[1]
    data = open(fi, 'rb').read()
    out, rep = build(data, label, value_addr, new_base, index)
    open(fo, 'wb').write(out)
    print(f"new descriptor: {rep['new_base']:#x}..{rep['new_base'] + rep['length']:#x} "
          f"({rep['length']} B), {rep['items']} items")
    print("Written", fo)


if __name__ == '__main__':
    main()
