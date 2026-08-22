# How it works

The short version: the firmware already knows how to sweep the needles. This patch asks it
to, once per ignition cycle, and spends most of its code deciding when "once per ignition
cycle" has come around again.

---

## 1. The sweep is not ours

Hold **OK** and switch the ignition on, and the cluster runs a gauge test: both needles to
full scale and back. That routine is in the firmware already, and it is good — the pace
comes from the stepper hardware, and each stage waits until both needles report they have
arrived, so they stay in sync.

Rather than driving the needles ourselves (an early version did, and they visibly
wandered), the patch just raises the request that starts it:

```
request   set bit 7 of *(u8*)0x400075EA           (gauge module flags)
accepted  FUN_0x353DE @0x3540E - if the power state from FUN_0x22C32 is 2 or 3
          and the module state (*(u8*)0x400075E8) is 9, flags are rewritten to 0x70
executed  FUN_0x34F6E - stages zero -> full scale -> zero, each advancing only after
          FUN_0x34B38 confirms the steppers arrived
```

The stages use the **same setter functions and the same calibration table** as normal
operation (`FUN_0x34D04`, `FUN_0x34D18`, `FUN_0x343A8`). "Full scale" is the calibrated end
of the printed scale, not an overdrive into the mechanical stop. Nothing about the needles'
calibration can be changed by a sweep — the four zero-offset fields at `0x400075F2..F8`
have no references anywhere in the image.

---

## 2. The hard part: the cluster restarts several times per ignition

This is the fact the whole design is built around, and it took a while to establish.

At ignition-on, the firmware does not start once. It **restarts four or five times in a
row**, and each restart re-runs the C runtime startup. That startup, at ARM code
`0x5348..0x5410`, gives the SRAM map:

| range | what | what startup does to it |
|---|---|---|
| `0x40000000..0x40000810` | noinit | **nothing** |
| `0x40000810..0x4000190C` | `.data` | reloaded from ROM at `0xD630C` |
| `0x4000190C..0x4000B008` | `.bss` | **zeroed** |
| `0x4000B008..0x4000B7F0` | heap | filled `0xEFEFEFEF`, and verified afterwards |
| `0x4000B7F0` | lower stack canary | `0x5A`, checked at `0x1E5EE` |
| `0x4000B7F4..0x4000BFEC` | stack | filled `0xEFEFEFEF` |
| `0x4000BFEC` | upper stack canary = initial SP | `0x5A`, checked at `0x1E5E6` |
| `0x4000BFF0..0x4000BFFF` | noinit, **unreferenced anywhere in the image** | **nothing** |

So a "sweep already done" flag kept in `.bss` is wiped on every restart, and the sweep runs
once per restart. That is exactly what several early versions did.

### Where the state lives instead

```
0x4000BFF4 (u32)   bits 31..16  header 0x5B39  - is this content ours, or RAM garbage?
                   bit  15      a sweep already ran in this ignition cycle
                   bits 14..0   counter of WORKING passes, saturating at ONRUN

0x4000BFF8 (u32)   counter of consecutive UNUSUAL passes - menu item off, or cluster
                   powered down; reset on every normal working pass
```

**Zero bytes of `.bss` are used.** That is not stylistic. An empirical write map of the
whole image — built by firing every one of the 3820 functions at an emulator and recording
every SRAM write — found **no contiguous 16-byte area in `.bss` that nothing writes to**.
An earlier version kept a byte at `0x40009331`, which turned out to sit at offset `+0x52`
inside a **500-byte text buffer at `0x400092DE`**
(`FUN_0x576AA` clears it, among other times, when you change the radio station). The
symptom on the road was a sweep roughly once a minute.

Why `0x4000BFF4` is trusted, in decreasing order of strength:

1. The startup code, read directly, does not touch it, and the stack grows **down** from
   `0x4000BFEC`.
2. Field evidence: an earlier build kept its marker there and produced **exactly one sweep**
   at ignition, repeatedly. For that, the word had to survive the whole restart burst —
   including whatever the bootloader does between restarts.
3. No literal anywhere in the image falls in `0x4000BFF0..0x4000BFFF`. The single
   occurrence of `0x4000BFFF` is a **bound constant** in the address validator at `0x43F74`.
4. No computed address can reach it either: the upper SRAM contains exactly four bases
   (`0x4000B008`, `0x4000B7F0`, `0x4000BFEC`, `0x4000BFFF`), none used with a register index
   or an offset that reaches our words.
5. The firmware's pattern guard (`FUN_0x150DA`, two callers) covers `0x4000B008..0x4000B7F0`
   and only **reads**, so our writes cannot trip its error path.

The bootloader itself (`0x0..0x5000`) is not in any available dump, so point 1 does not
cover it — point 2 does.

---

## 3. Two hooks, and why they are not equivalent

Both hooks replace an existing `bl` and always call the original, so the firmware's own flow
is unchanged apart from a few cycles.

| hook | replaces | runs when | what our veneer does |
|---|---|---|---|
| `0x34FCA` | `bl 0x34EC8` | cluster **powered**, module state 9, no sweep in progress | raises the request, keeps the working counter |
| `0x35682` | `bl 0x3536A` | module state **22**, i.e. cluster **off** | the only code that clears the marker |

That asymmetry is the core of the design. The clearing path lives in a dispatcher case that
**is not executed at all while the cluster is powered**. So no memory corruption, however it
arises, can produce a sweep while you are driving — that is a property of the control flow,
not of a well-chosen address.

An earlier version had a second clearing path inside the *powered* hook. That is precisely
the one that turned a stray `memset` into needles sweeping at 90 km/h.

---

## 4. Deciding that an ignition cycle has ended

The cluster-off hook needs the power state (`*(u8*)0x40006B31`, what `FUN_0x22C32` returns)
to be neither 2 nor 3 for a number of consecutive passes. Which threshold applies depends on
how much the cycle worked:

| working passes | unpowered passes required | why |
|---|---|---|
| ≥ `ONRUN` (3000, ~60 s) | `CLEAR_TICKS` (150, ~3 s) | this was a drive; a few seconds unpowered is proof enough |
| < `ONRUN` | `CLEAR_LONG` (3000, ~60 s) | the long outage is proof by itself — a restart from the start-up burst ends within seconds and cannot sit unpowered that long |

In the second case the working counter is also **reset**, so consecutive restarts from the
burst cannot add up to the threshold between them.

If the cluster loses power at key-off before the veneer counts `CLEAR_TICKS`, the working
counter is still saturated — so the clearing simply completes during the start-up phase of
the next cycle, and the sweep happens normally.

### Everything is counted in task passes, never in seconds

The gauge task runs about **50 times per second** — measured on the car, not assumed. The
unit of the firmware's own time base (`*(u16*)0x40006672`) has never been verified, and
three earlier versions failed precisely because thresholds were expressed in seconds and
converted through that assumption. Nothing in the current design depends on it.

The `--uptime-s` option exists **only** to measure that unit: build with `--uptime-s 20` and
time with a stopwatch how long it really takes before the needles move.

---

## 5. What is actually written to the image

**10 bytes** of existing code change. Everything else goes into space that was empty.

```
0x034FCA   4 B   bl 0x34EC8  ->  bl <cave>          (retargeted, still a bl)
0x035682   4 B   bl 0x3536A  ->  bl <cave>
0x09B8AC 188 B   code cave, was 448 bytes of zeros  (224 B in the menu variant)
```

The menu variant adds:

```
0x04B39A   1 B   screen table pointer byte
0x04C072   1 B   screen table pointer byte
0x0DDD00 592 B   cloned menu descriptor, was 0xEF filler
```

### Memory accessed at runtime

The gauge task runs ~50 Hz and exactly **one** of the two veneers runs on each pass.

Powered branch: reads the state word and the menu byte, writes the state word, and — on
the single pass that issues the request — read-modify-writes `0x400075EA`.

Cluster-off branch: reads the power state; only if it is not 2 or 3 does it read and write
the two state words.

That is the complete list. The patch reads and writes **four addresses** in total, plus a
few bytes of stack.

---

## 6. The menu item

`apply_menu_item.py` is independent of the sweep and can carry any on/off setting.

On this cluster the live `Advanced` screen is **not** the factory descriptor at `0x08A9C8`
— that one is dead. m0rtar's build relocated the screen to `0x0DD224` and hooked it up with
two pointers in the screen tables. The tool does the same thing once more: clones the live
descriptor into free `0xEF` space, relocates its internal pointers, appends one `0x48`-byte
record of type `0x37` (on/off) pointing at a setting byte in RAM, bumps the item count, and
repoints those same two table entries. Nothing is moved in place, and the old descriptor is
left intact but orphaned.

The setting byte is written and persisted by the cluster's **own** menu engine, including
storage in EEPROM. The patch only reads it.

On firmware without that relocated screen the header check fails and the tool aborts
without writing anything — which is why the menu variant cannot damage a stock image.
