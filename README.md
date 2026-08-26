# convers-gauge-sweep

**Welcome needle sweep for the Ford Convers+ instrument cluster** — the tachometer and
speedometer swing to full scale and back when you switch the ignition on, using the
cluster's **own factory sweep routine**.

Ships with a second, optional tool: a generic way to add your **own on/off item to the
`Advanced` menu**, with the setting stored persistently by the cluster itself.

---

## 🧪 Project status — NEW AND UNDER VERIFICATION

**This is a young project, published for testing.**

The current version has been flashed to a real car and works there: exactly one sweep per
ignition cycle, nothing unexpected while driving, menu behaves correctly. It also passes a 20-check emulator
suite that runs the cluster's real gauge task (see [docs/TESTING.md](docs/TESTING.md)).

**Update — one open question now closed.** The patch keeps its "sweep already done" marker in
a 16-byte window at the very top of SRAM, and the one thing earlier evidence could only infer
was whether the cluster **bootloader** leaves that window alone between restarts.
[@wojtkowiak](https://github.com/wojtkowiak) disassembled a JTAG dump of the bootloader and
confirmed it directly: its stack tops out well below the window and it never writes into it
(details in [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md)). Thanks! That turns the strongest
remaining "we believe" into "we checked".

That is **one car, one firmware build, over days — not months**. It has not yet been
verified across different vehicles, firmware variants or long-term use. If you flash it,
please treat yourself as a tester: read [docs/TESTING.md](docs/TESTING.md) first, run it in
the emulator against your own dump before touching the car, and
[open an issue](../../issues) with what you see — good or bad. Reports are the only thing
that turns "works on my car" into "works".

---

## ⚠️ DISCLAIMER — READ BEFORE YOU DO ANYTHING

Reflashing an instrument cluster can **brick it**. A bricked cluster may mean no
speedometer, no warning lights, and a car that is not roadworthy. Recovery may require
JTAG/BDM access or a replacement unit.

- This is an unofficial modification. It is **not** endorsed by Ford.
- **Make a verified backup of your own firmware AND EEPROM before you flash anything.**
  That backup is your only way back.
- Your dump may contain your **VIN and other vehicle data**. Never publish it, and never
  attach it to an issue.
- Check whether modifying instrument cluster software is legal where you drive, and what
  it does to your warranty and insurance.
- **You do this at your own risk.** No warranty of any kind — see [LICENSE](LICENSE).

---

## What it does

The firmware already contains a complete needle-sweep routine — the same one the factory
test menu runs (hold **OK** and switch the ignition on). This patch does not drive the
needles itself. It just **asks for that routine**, once per ignition cycle.

That distinction matters:

- the motion is paced by the **hardware**, and the next stage only starts once the steppers
  report they have arrived, so both needles stay perfectly in sync;
- the patch never takes over the needles — the normal update runs on every single pass,
  exactly as before;
- the entire effect is **setting one bit**. No calibration tables, no arithmetic of our own,
  nothing in the speed path.

**10 bytes** of existing code are modified — two `bl` instructions retargeted, and two
pointer bytes for the menu variant. Everything else is written into space that was empty.

---

## Two variants — pick the right one

### 1. Default — no menu item (recommended, works on any 1412-FL)

The sweep is always on. Nothing is added to any menu. This is the variant to use unless
you specifically want a switch.

```bash
python tools/apply_gauge_sweep.py main.bin main_sweep.bin
python tools/vbf_tool.py pack main.vbf main_sweep.bin main_sweep.vbf
```

### 2. With a `Gauge sweep` item in the `Advanced` menu

Adds a switchable **`Gauge sweep`** entry to `Settings → Advanced`. The setting is stored
by the cluster's own menu engine and survives ignition cycles. Switching it back on gives
you an immediate needle test, which is a pleasant way to show it off.

> **⚠️ This variant only applies to firmware modified by m0rtar** — the build whose
> `Advanced` menu already contains `Digital speed` and `Engine temp & Voltage`. On stock
> firmware that menu screen does not exist in the form this tool clones.
>
> You do not have to check by hand: the tool **verifies the image and aborts without
> writing anything** if the screen is not there. On stock firmware you will see:
>
> ```
> AssertionError: no screen header @0xdd240 (found 0xefefefef) - this image does not
> have m0rtar's relocated Advanced menu; the menu variant of the patch does not apply here
> ```
>
> If you get that, use variant 1.

```bash
python tools/apply_gauge_sweep.py main.bin tmp.bin --flag 0x400018BB
python tools/apply_menu_item.py tmp.bin main_sweep_menu.bin \
       --label "Gauge sweep" --value-addr 0x400018BB
python tools/vbf_tool.py pack main.vbf main_sweep_menu.bin main_sweep_menu.vbf
```

The two tools are deliberately separate: `apply_menu_item.py` knows nothing about sweeps
and can add an on/off item for **your own** modification just as well.

---

## Compatibility

| | |
|---|---|
| Cluster | Ford **Convers+** (MAC7116, ARM7TDMI-S, Thumb, big-endian) |
| Firmware | **1412-FL**, `main` partition `CS7T-14C026-CD`, based at `0x5000` |
| Confirmed on | Mondeo MK4 FL (facelift) |
| Variant 2 additionally | firmware modified by **m0rtar** (Advanced menu with Digital speed) |

The scripts **verify the exact bytes** at every hook site and at the code cave, and abort
if anything differs. That makes trying it on a different build safe: it either matches or
it refuses. If it refuses on your dump, please open an issue with the message — that tells
us about a firmware variant we have not seen.

---

## Requirements

- **Python 3** — that is all. None of the tools here need third-party packages.
- Your own **`main.bin`** — the `0x5000`-based code partition, unpacked from your VBF
- A way to read and write the cluster (see [docs/FLASHING.md](docs/FLASHING.md))

---

## Repository layout

```
tools/apply_gauge_sweep.py   the patch itself (two code veneers, one code cave)
tools/apply_menu_item.py     add your own on/off item to the Advanced menu
tools/vbf_tool.py            VBF <-> BIN, with checksums
tools/verify_patch.py        check a patched image before you flash it
docs/HOW_IT_WORKS.md         what is changed, byte by byte, and why
docs/FLASHING.md             reading, patching and writing back
docs/TESTING.md              verify on your own dump before touching the car
```

**No firmware images are included and none may be committed** — see
[.gitignore](.gitignore). Every user patches their own dump.

---

## Limitations and known behaviour

- **The sweep runs once per ignition cycle**, on the first start that reaches normal
  operation. The cluster restarts several times in a row at ignition-on; that is a property
  of the cluster, not of this patch, and the patch is built around it.
- **After a very short cycle** — ignition on and straight back off — the next start may not
  sweep. Ending a cycle is detected either from a long enough run followed by a short power
  outage, or from a long outage on its own; a very brief cycle can satisfy neither in time.
  It corrects itself on the following cycle.
- **The analog and digital speed readouts do not show the same number**, and never did.
  They come from two different variables and two different processing chains — the needle
  goes through the factory calibration table (which by law reads high), the digital value
  does not. This patch touches neither of them.
- **Variant 2 needs m0rtar's firmware**, as described above.

---

## Contributing

Bug reports, dumps that the tools refuse, and confirmations from other vehicles are all
welcome — open an [issue](../../issues). Please **never attach a firmware dump**; it
contains your VIN.

If you have a cluster on the bench, a CAN capture, or a different firmware build, that is
exactly the kind of help this project needs.

## Acknowledgements

- **[@wojtkowiak](https://github.com/wojtkowiak)** — disassembled a JTAG dump of the cluster
  bootloader (PBL) and confirmed, statically and dynamically, that it never touches the
  16-byte SRAM window this patch uses for its persistent marker. That independently settled
  the one memory-safety question the earlier evidence could only infer. Thanks!

## License

MIT — see [LICENSE](LICENSE).
