# Testing — before you touch the car

> This project is new. The more of this you do, the more useful your report is if
> something behaves differently on your vehicle.

Everything on this page runs on your desk, against **your own dump**. No car, no CAN
adapter, no risk.

---

## 1. The patch tools verify the image themselves

You do not have to check compatibility by hand. Both tools read the exact bytes at every
site they touch and **abort without writing** if anything differs:

- every hook site must contain the expected `bl` instruction;
- the code cave must be 448 bytes of zeros;
- for the menu variant, the `Advanced` screen header must be present, the item count must
  be self-consistent, the first record must be the expected type, the screen tables must
  point where expected, and the destination must be free `0xEF` filler.

So the honest test of "does this apply to my firmware?" is simply to run it. If it prints
`OK` on every line, it matched. If it raises an `AssertionError`, it did not — and nothing
was written.

A stock image without m0rtar's menu gives exactly this, which is the intended outcome:

```
AssertionError: no screen header @0xdd240 (found 0xefefefef) - this image does not have
m0rtar's relocated Advanced menu; the menu variant of the patch does not apply here
```

## 2. Verify the result before flashing

`verify_patch.py` compares your original with the patched file and answers one question:
**did anything change outside the regions it is supposed to?**

```bash
python tools/verify_patch.py main.bin main_sweep.bin
```

It detects which variant you built, lists the changed regions, re-derives both branch
targets the way the CPU will and confirms they now point into the code cave, and checks
that the space used was genuinely empty beforehand.

```
variant detected: 1 (default, no menu item)

changed regions
  file 0x02FFCA..0x02FFCD  (VA 0x034FCA)  4 bytes
  file 0x030682..0x030685  (VA 0x035682)  4 bytes
  file 0x0968AC..0x096967  (VA 0x09B8AC)  169 bytes
  OK   177 changed bytes, all inside the expected regions

hooks
  OK   power-on : original called 0x34ec8 as expected
  OK   power-on : now calls the code cave at 0x9b8ac
  OK   cluster-off: original called 0x3536a as expected
  OK   cluster-off: now calls the code cave at 0x9b8fc

code cave
  OK   the 188 bytes used were all zero in the original

ALL CHECKS PASSED - image looks sane.
```

Any line that says `UNEXPECTED` or `DO NOT FLASH` means stop. The check has teeth — flip a
single byte anywhere else in the file and it fails.

## 3. Verify the VBF round-trip

Packing recomputes checksums. Confirm the container gives back exactly what you put in:

```bash
python tools/vbf_tool.py pack original.vbf main_sweep.bin main_sweep.vbf
python tools/vbf_tool.py unpack main_sweep.vbf roundtrip.bin
cmp main_sweep.bin roundtrip.bin && echo "round-trip OK"
```

`pack` also validates the container itself and should print `validation: OK`.

---

## What is tested beyond this

The patch is developed against an emulator harness that loads the firmware into a Unicorn
ARM core and runs **the cluster's real gauge task** (`FUN_0x353DE`) pass by pass, reading
back the actual module registers. The suite currently has 20 checks, including:

| check | what it does |
|---|---|
| factory sweep | bit 7 of `0x400075EA` really drives the stock routine: request → `0x70` → zero → full scale → zero → flags cleared |
| hardware pacing | with the steppers reporting "still moving", the sequence stalls; it advances only once they report arrival |
| survives restarts | five simulated firmware restarts (`.data` from ROM, `.bss` zeroed, time base reset) → **exactly one** request |
| start-up phase | a fresh start with a small working counter must **not** clear the marker; with a saturated one it must |
| text buffer | 1800 passes while the `0x400092DE` buffer is cleared and rewritten over and over → **one** request, and the patch writes nothing into that buffer |
| driving | 10 000 passes with the time base jumping over its whole range → **one** request, and the clearing hook executes **zero** times |
| short cycle | a short run must not be cleared by the short threshold, but must be by the long one |
| image diff | the patched image differs from stock only in the two hooks and the cave |

That harness is part of the development tree rather than this repository, because it also
needs a firmware image to run against and several tools that are not specific to this
patch. **If you want to run it, open an issue and say so** — it is straightforward to
publish, and knowing somebody would use it is a good reason to do the work.

---

## Reporting

Useful reports say what you saw, in this order:

1. vehicle, firmware part number, and which variant you built;
2. what happened at the **first** ignition after flashing;
3. what happened at the **next few** ignition cycles;
4. anything at all while driving.

Please **never attach a firmware dump** — it contains your VIN. The output of the patch
tools and of `verify_patch.py` is safe to paste and is usually enough.
