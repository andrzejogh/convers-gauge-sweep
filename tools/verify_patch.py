#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_patch.py - check a patched image against your original, before you flash it.

Answers one question: does the patched file differ from the original ONLY where it is
supposed to? Anything else means something went wrong, and you should not flash.

It also re-derives the branch targets from the patched bytes - the same way the CPU
will - and confirms both hooks now point into the code cave.

Needs nothing but Python 3. (The disassembly listing is optional and uses Capstone if
you have it installed.)

Usage:
  python verify_patch.py <original.bin> <patched.bin> [--disasm]
"""
import struct, sys

BASE = 0x5000
HOOK_TICK, ORIG_TICK = 0x34fca, 0x34ec8
HOOK_OFF, ORIG_OFF = 0x35682, 0x3536a
CAVE = 0x9b8ac
CAVE_MAX = 0x200
MENU_PTRS = (0x4b39a, 0x4c072)
MENU_DESC = 0xddd00
MENU_DESC_MAX = 0x400


def bl_target(d, va):
    """Decode a Thumb-1 BL pair exactly as the CPU does."""
    h1, h2 = struct.unpack(">HH", d[va - BASE:va - BASE + 4])
    if (h1 & 0xF800) != 0xF000:
        return None
    off = ((h1 & 0x7FF) << 12) | ((h2 & 0x7FF) << 1)
    if off & 0x400000:
        off -= 0x800000
    return va + 4 + off


def main():
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if len(a) < 2:
        print(__doc__)
        return 1
    orig = open(a[0], "rb").read()
    new = open(a[1], "rb").read()

    ok = True

    def check(cond, good, bad):
        nonlocal ok
        print(("  OK   " if cond else "  FAIL ") + (good if cond else bad))
        ok &= bool(cond)

    print("size")
    check(len(orig) == len(new),
          f"both images are {len(orig)} bytes",
          f"size differs: {len(orig)} vs {len(new)} - wrong file?")
    if len(orig) != len(new):
        return 1

    diff = [i for i in range(len(orig)) if orig[i] != new[i]]
    if not diff:
        print("\n  FAIL  the images are identical - the patch did not apply")
        return 1

    menu = any((p - BASE) in diff for p in MENU_PTRS)
    variant = "2 (with the Advanced menu item)" if menu else "1 (default, no menu item)"
    print(f"\nvariant detected: {variant}")

    allowed = set()
    for h in (HOOK_TICK, HOOK_OFF):
        allowed |= set(range(h - BASE, h - BASE + 4))
    allowed |= set(range(CAVE - BASE, CAVE - BASE + CAVE_MAX))
    if menu:
        for p in MENU_PTRS:
            allowed.add(p - BASE)
        allowed |= set(range(MENU_DESC - BASE, MENU_DESC - BASE + MENU_DESC_MAX))

    stray = [i for i in diff if i not in allowed]
    print("\nchanged regions")
    groups = []
    s = p = diff[0]
    for i in diff[1:]:
        if i > p + 16:
            groups.append((s, p))
            s = i
        p = i
    groups.append((s, p))
    for g0, g1 in groups:
        n = sum(1 for i in diff if g0 <= i <= g1)
        mark = "" if all(i in allowed for i in range(g0, g1 + 1)) else "   <-- UNEXPECTED"
        print(f"  file 0x{g0:06X}..0x{g1:06X}  (VA 0x{g0 + BASE:06X})  {n} bytes{mark}")
    check(not stray,
          f"{len(diff)} changed bytes, all inside the expected regions",
          f"{len(stray)} changed bytes OUTSIDE the expected regions - DO NOT FLASH")

    print("\nhooks")
    for name, hook, orig_t in (("power-on ", HOOK_TICK, ORIG_TICK),
                               ("cluster-off", HOOK_OFF, ORIG_OFF)):
        was = bl_target(orig, hook)
        now = bl_target(new, hook)
        check(was == orig_t,
              f"{name}: original called {orig_t:#x} as expected",
              f"{name}: original called {was and hex(was)}, expected {orig_t:#x} "
              f"- this is not the firmware this patch targets")
        check(now is not None and CAVE <= now < CAVE + CAVE_MAX,
              f"{name}: now calls the code cave at {now:#x}",
              f"{name}: now calls {now and hex(now)}, which is not the cave")

    print("\ncode cave")
    co = CAVE - BASE
    used = max((i for i in diff if co <= i < co + CAVE_MAX), default=co) - co + 1
    check(all(b == 0 for b in orig[co:co + used]),
          f"the {used} bytes used were all zero in the original",
          "the cave was NOT empty in the original - something else lives there")

    if menu:
        print("\nmenu descriptor")
        no = MENU_DESC - BASE
        dused = max((i for i in diff if no <= i < no + MENU_DESC_MAX), default=no) - no + 1
        check(all(b == 0xEF for b in orig[no:no + dused]),
              f"the {dused} bytes used were all 0xEF filler in the original",
              "the descriptor area was NOT free filler - do not flash")

    print("\n" + ("ALL CHECKS PASSED - image looks sane." if ok else
                 "SOMETHING IS WRONG - do not flash this image."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
