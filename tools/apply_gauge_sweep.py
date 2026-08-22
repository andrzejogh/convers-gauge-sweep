#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_gauge_sweep.py - welcome needle sweep for the Ford Convers+ instrument cluster
(NXP MAC7116, ARM7TDMI-S, Thumb, big-endian, KOD partition based at 0x5000).

WHAT IT DOES
  The firmware already contains a complete needle-sweep routine - the same one the
  factory test menu runs (hold OK + ignition). This patch does not drive the needles
  itself. It simply ASKS for that routine, once per ignition cycle:

    request  : set bit 7 of *(u8*)0x400075EA  (gauge module flags)
    accepted : FUN_0x353DE @0x3540E - when the power state from FUN_0x22C32 is 2 or 3
               and the module state (*(u8*)0x400075E8) is 9, it rewrites flags to 0x70
    executed : FUN_0x34F6E - stages zero -> full scale -> zero, each one advancing only
               after FUN_0x34B38 confirms the steppers have arrived

  So the whole patch amounts to SETTING ONE BIT. The motion itself is 100% factory:
  the pace comes from the hardware and both needles stay in sync, exactly like the
  built-in gauge test.

TWO HOOKS, ONE CODE CAVE
  0x34FCA  (bl 0x34EC8) - the "power ON" branch of FUN_0x34F6E. Reachable only while
                          the module is in state 9 and no sweep is running. This is
                          where the request is issued. Always calls the original.
  0x35682  (bl 0x3536A) - the dispatcher case for module state 22, i.e. "cluster off".
                          This is the ONLY place that clears the marker, and while the
                          cluster is powered it is never executed at all.

  That asymmetry is deliberate and it is the core of the design: no memory corruption,
  however it arises, can produce a sweep while you are driving, because the code that
  would have to clear the marker is not running then.

STATE - TWO WORDS, ZERO BYTES OF .bss
  Disassembling the C runtime startup (ARM code at 0x5348..0x5410) gives the SRAM map:

    0x40000000..0x40000810   noinit  - startup does not touch it
    0x40000810..0x4000190C   .data   - reloaded from ROM (0xD630C) on EVERY start
    0x4000190C..0x4000B008   .bss    - zeroed on EVERY start
    0x4000B008..0x4000B7F0   heap    - filled with 0xEFEFEFEF, and verified afterwards
    0x4000B7F0               lower stack canary (0x5A, checked at 0x1E5EE)
    0x4000B7F4..0x4000BFEC   stack   - filled with 0xEFEFEFEF on every start
    0x4000BFEC               upper stack canary = initial SP (checked at 0x1E5E6)
    0x4000BFF0..0x4000BFFF   noinit, and not referenced anywhere in the image

  This matters because the cluster firmware RESTARTS SEVERAL TIMES per ignition cycle,
  wiping .bss each time. Any "already done" flag kept in .bss cannot survive, which is
  why the sweep used to run four or five times in a row. The state therefore lives in
  the last region above:

    0x4000BFF4 (u32):  bits 31..16 = header 0x5B39 (is this content ours, or garbage
                                     left in RAM after a cold start?)
                       bit  15     = a sweep has already run in this ignition cycle
                       bits 14..0  = counter of WORKING passes, saturating at ONRUN
    0x4000BFF8 (u32):  counter of consecutive UNUSUAL passes - either with the menu
                       item switched off, or with the cluster powered down; reset on
                       every normal working pass

  Do not move this state into .bss. An empirical write map of the whole image - built by
  firing every one of the 3820 functions at an emulator and recording every SRAM write -
  found no contiguous 16-byte area in .bss that nothing writes to. One earlier version kept a byte at 0x40009331 - which turned
  out to sit inside a 500-byte text buffer at 0x400092DE that the firmware clears, among
  other times, when you change the radio station.

DECIDING THAT AN IGNITION CYCLE HAS ENDED
  Only the state-22 hook clears the marker, and it needs the cluster to be unpowered
  (*(u8*)0x40006B31 is neither 2 nor 3) for a number of consecutive passes. Which
  threshold applies depends on how much the cycle actually worked:

    working passes >= ONRUN  ->  CLEAR_TICKS  (~3 s)   this was a drive, so a few
                                                       seconds unpowered is proof enough
    working passes <  ONRUN  ->  CLEAR_LONG   (~60 s)  the proof is then the LONG outage
                                                       itself; a restart from the
                                                       start-up burst ends within a few
                                                       seconds and cannot sit unpowered
                                                       that long

  In the second case the working counter is also reset, so consecutive restarts from the
  start-up burst cannot add up to the threshold between them.

  If the cluster dies at key-off before the veneer counts CLEAR_TICKS, the working
  counter is still saturated, so the clearing simply completes during the start-up phase
  of the next cycle and the sweep happens normally.

EVERYTHING IS COUNTED IN TASK PASSES, NEVER IN SECONDS
  The gauge module task runs about 50 times per second (measured on the car). The unit
  of the firmware's own time base (*(u16*)0x40006672) was never verified, and three
  earlier versions failed precisely because thresholds were expressed in seconds and
  converted through that assumption. Nothing here depends on it. The --uptime-s option
  exists ONLY to measure that unit: build with --uptime-s 20 and time with a stopwatch
  how long it actually takes before the needles move.

Usage:
  python apply_gauge_sweep.py <in.bin> <out.bin> [--onrun 3000] [--clear-ticks 150]
                              [--clear-long 3000] [--off-ticks 1000] [--flag 0x400018BB]
                              [--cookie 0x4000BFF4] [--cave 0x9b8ac] [--uptime-s 0]

  --flag enables the optional on/off switch byte written by the Advanced menu item
  (see apply_menu_item.py). Without it the sweep is always on.
"""
import struct, sys

BASE = 0x5000

# --- hooks and their original targets ---
HOOK_TICK, ORIG_TICK = 0x34fca, 0x34ec8   # "power ON" branch of FUN_0x34F6E
HOOK_OFF,  ORIG_OFF  = 0x35682, 0x3536a   # dispatcher case for module state 22 (cluster off)
GST_FLAGS = 0x400075ea                    # gauge module flags (bit 7 = sweep request)
REQ_BIT = 0x80
PWR_STATE = 0x40006b31                    # power state (what FUN_0x22C32 returns); 2/3 = on

COOKIE = 0x4000bff4       # NOT initialised by startup - survives a CPU restart
CNT2 = 0x4000bff8         # second word of the same region: counter of unusual passes
TAG = 0x5B39              # header (bits 31..16) - tells our content from RAM garbage
DONE_BIT = 15             # a sweep already ran in this ignition cycle
CNT_BITS = 15             # bits 14..0 - WORKING pass counter (saturates at ONRUN)
CAVE = 0x9b8ac            # free space in the KOD partition (448 zero bytes)

COARSE = 0x40006672       # firmware time base, 14-bit (ISR FUN_0x1E6D4) - unit UNVERIFIED
COARSE_MS = 128
MASK = 0x3FFF
SPEED_RAW = 0x40007596    # WARNING: not referenced anywhere in the image - meaning UNCONFIRMED

_argv = sys.argv[1:]
def _opt(name, default, conv=lambda x: int(x, 0)):
    global _argv
    if name in _argv:
        i = _argv.index(name); v = conv(_argv[i + 1]); del _argv[i:i + 2]; return v
    return default
def _flag(name):
    global _argv
    if name in _argv:
        _argv.remove(name); return True
    return False

# How many CONSECUTIVE unpowered passes end an ignition cycle when the cycle did some
# real work. The task runs about 50 times per second (measured on the car), so 150
# passes is roughly 3 seconds.
CLEAR_TICKS = _opt('--clear-ticks', 150)
# SECOND unpowered threshold, for cycles SHORTER than ONRUN. A long outage is by itself
# sufficient proof that the cycle ended, regardless of how much work it did: a restart
# from the start-up burst ends within a few seconds and cannot sit unpowered this long.
# Thanks to this threshold a short ignition cycle also arms the clearing, so ONRUN does
# not have to be lowered.
CLEAR_LONG = _opt('--clear-long', 3000)
# How many WORKING passes a run needs before its end counts as a SHORT end of cycle.
# 3000 passes is about 60 s of work at the measured 50 Hz - and it is the ONLY value
# confirmed on the car: an earlier version cleared the marker when the PREVIOUS start had
# >= 3000 passes (its counter was reset on every restart, so it measured per start), and
# on the car exactly ONE sweep came out at start-up. That proves no start in the burst
# exceeds 3000. Lowering this threshold leaves the range verified on real hardware.
ONRUN = _opt('--onrun', 3000)
# Optional delay before the request, taken from the firmware time base. OFF by default -
# the request is only ever accepted in module state 9 anyway. Useful for MEASURING the
# length of that unit: build with --uptime-s 20 and time when the needles actually move.
UPTIME_S = _opt('--uptime-s', 0)
UPTIME_U = (UPTIME_S * 1000) // COARSE_MS        # 0 = condition disabled
# How many passes with the menu item switched off before we clear the marker. The switch
# byte lives in .data, so every restart resets it to the ROM value (0xF8) and the EEPROM
# restores the real setting only later - this debounce absorbs that flicker.
OFF_TICKS = _opt('--off-ticks', 1000)

COOKIE = _opt('--cookie', COOKIE)
CNT2 = _opt('--cnt2', CNT2)
CAVE = _opt('--cave', CAVE)
# Optional on/off byte written by the Advanced menu item (see apply_menu_item.py).
# Non-zero = sweep enabled. The ROM default there is 0xF8, i.e. enabled.
FLAG = _opt('--flag', None)
SPEED_GUARD = _flag('--speed-guard')

assert 0 <= UPTIME_U < MASK, "--uptime-s out of range"
assert 1 <= CLEAR_TICKS <= 0xFFFF, "--clear-ticks out of range"
assert CLEAR_TICKS <= CLEAR_LONG <= 0xFFFFF, "--clear-long must be >= --clear-ticks"
assert 1 <= OFF_TICKS <= 0xFFFF, "--off-ticks out of range"
assert 1 <= ONRUN < (1 << CNT_BITS) - 1, "--onrun out of range (max 32766)"
assert COOKIE % 4 == 0 and CNT2 % 4 == 0, "state words must be 4-byte aligned"
assert COOKIE != CNT2, "state words must not overlap"

def hw(v): return struct.pack('>H', v & 0xFFFF)

def ebl(src, dst):
    """Thumb-1 BL (halfword pair), +-4 MB range."""
    o = (dst - (src + 4)) & 0x1FFFFFF
    S = (o >> 24) & 1; i1 = (o >> 23) & 1; i2 = (o >> 22) & 1
    im10 = (o >> 12) & 0x3FF; im11 = (o >> 1) & 0x7FF
    j1 = (~(i1 ^ S)) & 1; j2 = (~(i2 ^ S)) & 1
    return struct.pack('>HH', 0xF000 | (S << 10) | im10,
                       0xD000 | (j1 << 13) | (j2 << 11) | im11)

EQ, NE, CS, CC = 0, 1, 2, 3      # condition codes for 'bcc'

P = []
def I(*a): P.append(a)
def L(n): P.append(('L', n))

# ==================== CAVE_SWEEP - the "power ON" branch =========================
# Entered from 0x34FCA (replacing bl 0x34EC8). Reachable ONLY while the module is in
# state 9 and no sweep is running. This is the only place that issues the request.
I('push', [4])                                    # push {r4,lr}
I('ldrl', 4, ('c', COOKIE))
I('ldr_i', 1, 4, 0)
I('lsrs_i', 2, 1, 16); I('ldrl', 3, ('c', TAG)); I('cmp_r', 2, 3)
I('bcc', EQ, 'L_tag')
I('lsls_i', 1, 3, 16)                             # RAM garbage -> start from a bare header
L('L_tag')
if FLAG is not None:
    I('ldrl', 0, ('c', FLAG)); I('ldrb_i', 0, 0, 0)
    I('cmp_i', 0, 0); I('bcc', NE, 'L_on')        # != 0 -> menu item enabled
    # --- menu item OFF: after OFF_TICKS passes wipe the word, so switching it
    #     back on gives an immediate needle test ---
    I('ldrl', 0, ('c', CNT2)); I('ldr_i', 2, 0, 0)
    I('adds_i8', 2, 1); I('str_i', 2, 0, 0)
    I('ldrl', 3, ('c', OFF_TICKS)); I('cmp_r', 2, 3)
    I('bcc', CC, 'L_save')
    I('ldrl', 3, ('c', TAG)); I('lsls_i', 1, 3, 16)
    I('b', 'L_save')
    L('L_on')
    I('ldrl', 0, ('c', CNT2)); I('movs_i', 2, 0); I('str_i', 2, 0, 0)  # unusual counter = 0
else:
    I('ldrl', 0, ('c', CNT2)); I('movs_i', 2, 0); I('str_i', 2, 0, 0)
# --- WORKING pass counter (saturates at ONRUN) ---
I('lsls_i', 2, 1, 32 - CNT_BITS); I('lsrs_i', 2, 2, 32 - CNT_BITS)
I('ldrl', 3, ('c', ONRUN)); I('cmp_r', 2, 3)
I('bcc', CS, 'L_run')                             # already saturated
I('adds_i8', 2, 1)
I('lsrs_i', 1, 1, CNT_BITS); I('lsls_i', 1, 1, CNT_BITS); I('orrs', 1, 2)
L('L_run')
# --- has a sweep already run in this ignition cycle? ---
I('lsls_i', 2, 1, 31 - DONE_BIT); I('lsrs_i', 2, 2, 31)
I('cmp_i', 2, 0); I('bcc', NE, 'L_save')
if UPTIME_U:
    # --- optional delay since firmware start (MEASUREMENT build) ---
    I('ldrl', 2, ('c', COARSE)); I('ldrh_i', 2, 2, 0)
    I('ldrl', 3, ('c', MASK)); I('ands', 2, 3)
    I('ldrl', 3, ('c', UPTIME_U)); I('cmp_r', 2, 3)
    I('bcc', CC, 'L_save')
if SPEED_GUARD:
    # Optional: skip while the speed field is != 0. NOTE - it only SKIPS the pass,
    # it does not set the bit, so it can never block the sweep permanently.
    I('ldrl', 2, ('c', SPEED_RAW)); I('ldrh_i', 2, 2, 0)
    I('cmp_i', 2, 0); I('bcc', NE, 'L_save')
I('movs_i', 3, 1); I('lsls_i', 3, 3, DONE_BIT); I('orrs', 1, 3)
I('str_i', 1, 4, 0)
I('ldrl', 1, ('c', GST_FLAGS)); I('ldrb_i', 2, 1, 0)
I('movs_i', 3, REQ_BIT); I('orrs', 2, 3); I('strb_i', 2, 1, 0)   # request the factory sweep
I('b', 'L_norm')
L('L_save')
I('str_i', 1, 4, 0)
L('L_norm')
I('bl', ORIG_TICK)                                # ALWAYS run the normal needle update
I('pop', [4]); I('pop_r3'); I('bx', 3)

# ==================== CAVE_OFF - module state 22 ("cluster off") ==================
# Entered from 0x35682 (replacing bl 0x3536A). This is the ONLY place that clears the
# marker and it runs ONLY while the cluster is off. Registers r0-r3 only: the caller
# keeps 0x400075E6 in r4 and zero in r5, and uses both after the original returns.
L('L_off')
I('push_lr')
I('ldrl', 0, ('c', PWR_STATE)); I('ldrb_i', 0, 0, 0)
I('cmp_i', 0, 2); I('bcc', EQ, 'L_off_end')       # 2 or 3 = power is on
I('cmp_i', 0, 3); I('bcc', EQ, 'L_off_end')
I('ldrl', 0, ('c', COOKIE)); I('ldr_i', 1, 0, 0)
I('lsrs_i', 2, 1, 16); I('ldrl', 3, ('c', TAG)); I('cmp_r', 2, 3)
I('bcc', NE, 'L_off_end')                         # garbage -> nothing to clear
I('lsls_i', 2, 1, 32 - CNT_BITS); I('lsrs_i', 2, 2, 32 - CNT_BITS)
I('ldrl', 3, ('c', ONRUN)); I('cmp_r', 2, 3)
I('bcc', CS, 'L_off_krotki')
# Too little work -> this is either the start-up phase or a very short ignition cycle.
# 1) reset the working counter so consecutive restarts from the burst cannot ADD UP to
#    the threshold (an earlier version reset it via a .bss byte; we use no .bss at all),
# 2) use the LONGER unpowered threshold - a long outage is proof by itself, and a restart
#    from the burst ends within a few seconds.
I('lsrs_i', 1, 1, CNT_BITS); I('lsls_i', 1, 1, CNT_BITS)
I('str_i', 1, 0, 0)
I('ldrl', 3, ('c', CLEAR_LONG))
I('b', 'L_off_prog')
L('L_off_krotki')
I('ldrl', 3, ('c', CLEAR_TICKS))
L('L_off_prog')
# r0 = state word address, r3 = chosen threshold
I('ldrl', 2, ('c', CNT2)); I('ldr_i', 1, 2, 0)
I('adds_i8', 1, 1); I('str_i', 1, 2, 0)
I('cmp_r', 1, 3)
I('bcc', CC, 'L_off_end')
I('ldrl', 3, ('c', TAG)); I('lsls_i', 1, 3, 16)
I('str_i', 1, 0, 0)                               # ignition cycle is over
I('movs_i', 1, 0); I('str_i', 1, 2, 0)            # unusual counter = 0
L('L_off_end')
I('bl', ORIG_OFF)
I('pop_r3'); I('bx', 3)


def enc(m, a, addr, lab, lit):
    if m == 'push':    return hw(0xB400 | 0x0100 | sum(1 << r for r in a[0]))
    if m == 'push_lr': return hw(0xB500)
    if m == 'pop':     return hw(0xBC00 | sum(1 << r for r in a[0]))
    if m == 'pop_r3':  return hw(0xBC08)
    if m == 'movs_i':  return hw(0x2000 | (a[0] << 8) | (a[1] & 0xFF))
    if m == 'cmp_i':   return hw(0x2800 | (a[0] << 8) | (a[1] & 0xFF))
    if m == 'adds_i8': return hw(0x3000 | (a[0] << 8) | (a[1] & 0xFF))
    if m == 'orrs':    return hw(0x4300 | (a[1] << 3) | a[0])
    if m == 'ands':    return hw(0x4000 | (a[1] << 3) | a[0])
    if m == 'lsls_i':  return hw(0x0000 | ((a[2] & 0x1F) << 6) | (a[1] << 3) | a[0])
    if m == 'lsrs_i':  return hw(0x0800 | ((a[2] & 0x1F) << 6) | (a[1] << 3) | a[0])
    if m == 'bics':    return hw(0x4380 | (a[1] << 3) | a[0])
    if m == 'subs_r':  return hw(0x1A00 | (a[2] << 6) | (a[1] << 3) | a[0])
    if m == 'cmp_r':   return hw(0x4280 | (a[1] << 3) | a[0])
    if m == 'ldrb_i':  return hw(0x7800 | (a[2] << 6) | (a[1] << 3) | a[0])
    if m == 'strb_i':  return hw(0x7000 | (a[2] << 6) | (a[1] << 3) | a[0])
    if m == 'ldrh_i':  return hw(0x8800 | ((a[2] >> 1) << 6) | (a[1] << 3) | a[0])
    if m == 'strh_i':  return hw(0x8000 | ((a[2] >> 1) << 6) | (a[1] << 3) | a[0])
    if m == 'ldr_i':   return hw(0x6800 | ((a[2] >> 2) << 6) | (a[1] << 3) | a[0])
    if m == 'str_i':   return hw(0x6000 | ((a[2] >> 2) << 6) | (a[1] << 3) | a[0])
    if m == 'bx':      return hw(0x4700 | (a[0] << 3))
    if m == 'ldrl':
        la = lit[a[1]]; off = la - ((addr + 4) & ~3)
        assert 0 <= off <= 0x3FC and off % 4 == 0, f"ldrl {off:#x}@{addr:#x}"
        return hw(0x4800 | (a[0] << 8) | (off >> 2))
    if m == 'b':
        off = lab[a[0]] - (addr + 4); assert -2048 <= off <= 2046, f"b {off}@{addr:#x}"
        return hw(0xE000 | ((off >> 1) & 0x7FF))
    if m == 'bcc':
        off = lab[a[1]] - (addr + 4); assert -256 <= off <= 254, f"bcc {off}@{addr:#x}"
        return hw(0xD000 | (a[0] << 8) | ((off >> 1) & 0xFF))
    if m == 'bl':      return ebl(addr, a[0])
    raise ValueError(m)

# --- layout: code first, then the literal pool ---
addr = CAVE; lab = {}
for op in P:
    if op[0] == 'L': lab[op[1]] = addr
    else: addr += 4 if op[0] == 'bl' else 2
pool = addr + (addr & 2); lits = []
for op in P:
    if op[0] == 'ldrl' and op[2] not in lits: lits.append(op[2])
lit = {}; a = pool
for k in lits: lit[k] = a; a += 4
TOTAL_LEN = a - CAVE
CAVE_OFF = lab['L_off']

out = bytearray(); addr = CAVE
for op in P:
    if op[0] == 'L': continue
    out += enc(op[0], op[1:], addr, lab, lit); addr += 4 if op[0] == 'bl' else 2
while addr < pool: out += hw(0x46C0); addr += 2      # nop (mov r8,r8)
for k in lits: out += struct.pack('>I', k[1]); addr += 4
assert len(out) == TOTAL_LEN


def main():
    if len(_argv) < 2:
        sys.exit("usage: python apply_gauge_sweep.py <in.bin> <out.bin> [options]\n"
                 "       see the docstring at the top of this file for the option list")
    fi, fo = _argv[0], _argv[1]
    d = bytearray(open(fi, 'rb').read())
    co = CAVE - BASE

    print(f"factory sweep request: bit {REQ_BIT:#04x} of {GST_FLAGS:#010x}")
    print(f"state word: {COOKIE:#010x} - header {TAG:#06x}, bit {DONE_BIT} = "
          f"'sweep done', bits {CNT_BITS - 1}..0 = WORKING pass counter (sat. {ONRUN})")
    print(f"second word: {CNT2:#010x} - unusual pass counter (menu off / power off)")
    print("bytes of .bss used by this patch: 0")
    print(f"end of ignition cycle (state-22 hook), two unpowered thresholds:")
    print(f"   after >= {ONRUN} working passes (~{ONRUN / 50:.0f} s): "
          f"{CLEAR_TICKS} unpowered passes (~{CLEAR_TICKS / 50:.1f} s)")
    print(f"   after a shorter run:                "
          f"{CLEAR_LONG} unpowered passes (~{CLEAR_LONG / 50:.0f} s)")
    print("delay since start: " + (f"{UPTIME_S} s ({UPTIME_U} time-base units) - MEASUREMENT BUILD"
                                      if UPTIME_U else "none (the request is only accepted in state 9 anyway)")
          + ("; speed guard ON (skips a pass, never blocks permanently)" if SPEED_GUARD else ""))
    print(f"switch: {'none (always on)' if FLAG is None else f'byte {FLAG:#010x}'}"
          + ("" if FLAG is None else f", switching it off for {OFF_TICKS} passes clears the marker"))

    ok = True
    for name, hook, orig in (("TICK", HOOK_TICK, ORIG_TICK), ("OFF", HOOK_OFF, ORIG_OFF)):
        ho = hook - BASE
        got = bytes(d[ho:ho + 4]); exp = ebl(hook, orig)
        good = got == exp
        ok &= good
        print(f"HOOK_{name} @{hook:#x}: {got.hex(' ')} (expected bl {orig:#x} = {exp.hex(' ')})"
              f" {'OK' if good else 'MISMATCH'}")
    assert ok, "input image does not match - is this really a 1412-FL KOD partition?"
    assert not any(d[co:co + TOTAL_LEN]), f"code cave @{CAVE:#x} is not empty"

    d[HOOK_TICK - BASE:HOOK_TICK - BASE + 4] = ebl(HOOK_TICK, CAVE)
    d[HOOK_OFF - BASE:HOOK_OFF - BASE + 4] = ebl(HOOK_OFF, CAVE_OFF)
    d[co:co + TOTAL_LEN] = out

    print(f"CAVE_SWEEP @{CAVE:#x}, CAVE_OFF @{CAVE_OFF:#x}, {TOTAL_LEN} B total")
    open(fo, 'wb').write(d)
    print("Written", fo)


if __name__ == '__main__':
    main()
