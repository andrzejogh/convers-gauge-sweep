# Flashing walkthrough — dump → patch → pack → flash

> ⚠️ Read the disclaimer in the [README](../README.md) first. Bricking is possible. Keep verified backups. Proceed at your own risk.

This project produces a patched firmware image. **It does not read from or write to your cluster** — you use your own dump/flash workflow for that. Community methods and hardware for Convers+ clusters are documented on the [microhacker forum](https://microhacker.denkdose.de/).

> Confirmed on a **Ford Mondeo MK4 facelift (FL)** — firmware 1412-FL, partition `CS7T-14C026-CD`. Other vehicles and builds are unverified; the scripts abort in Step 2 if the bytes do not match.

## Prerequisites

- Python 3.8+ (`python --version`)
- Your **original firmware image** and **EEPROM**, backed up and verified
- Your **original VBF** file — used as the packing template, because it carries the correct part number, load address and CRC layout

## Step 0 — Back up

Dump the full firmware **and** the EEPROM. Make at least two copies and verify them (compare sizes and hashes). If a flash goes wrong, these are your only way back. Do **not** skip this, and do **not** upload these files anywhere — they contain Ford code and your VIN.

## Step 1 — Get the code partition

The patch operates on the program image based at `0x5000` (`CS7T-14C026-CD` on the confirmed setup). If your dump is already that raw image, use it as `main.bin`. If you have a VBF:

```bash
python tools/vbf_tool.py unpack original.vbf main.bin
```

## Step 2 — Apply the patch

**Variant 1 — default, no menu item.** Works on any matching 1412-FL image:

```bash
python tools/apply_gauge_sweep.py main.bin main_sweep.bin
```

**Variant 2 — with a `Gauge sweep` item in the `Advanced` menu.** Requires firmware modified by m0rtar (see the README); on anything else the second command aborts without writing:

```bash
python tools/apply_gauge_sweep.py main.bin tmp.bin --flag 0x400018BB
python tools/apply_menu_item.py tmp.bin main_sweep.bin \
       --label "Gauge sweep" --value-addr 0x400018BB
```

Both tools print what they matched and what they wrote. Every hook site is checked against its expected bytes, and the code cave is checked for being empty, so a mismatch stops the process instead of producing a bad image.

## Step 3 — Repack into a VBF

Use your **original** VBF as the template, so the output keeps the correct part number, address and CRC framing:

```bash
python tools/vbf_tool.py pack original.vbf main_sweep.bin main_sweep.vbf
```

The tool recomputes the checksums and validates the container. You should see `validation: OK`.

## Step 4 — Verify before flashing (recommended)

Confirm the patched image differs from your original **only** in the expected regions. Anything outside them means something went wrong — do not flash.

**Variant 1 — 177 changed bytes:**

| file offset | size | what |
|---|---|---|
| `0x02FFCA` | 4 B | hook, power-on branch |
| `0x030682` | 4 B | hook, cluster-off branch |
| `0x0968AC` | 188 B | code cave (was all zeros) |

**Variant 2 — 798 changed bytes:** the three above (cave is 224 B) plus

| file offset | size | what |
|---|---|---|
| `0x04639A` | 1 B | screen table pointer |
| `0x047072` | 1 B | screen table pointer |
| `0x0D8D00` | 592 B | new menu descriptor (was `0xEF` filler) |

```bash
# list changed byte offsets (Git Bash / Linux / macOS)
cmp -l main.bin main_sweep.bin | wc -l
cmp -l main.bin main_sweep.bin | head
```

A round-trip check is also worth the ten seconds it takes:

```bash
python tools/vbf_tool.py unpack main_sweep.vbf roundtrip.bin
cmp main_sweep.bin roundtrip.bin && echo "VBF round-trip OK"
```

## Step 5 — Flash

Flash `main_sweep.vbf` with your usual tool. Afterwards:

1. Power the cluster normally (ignition on).
2. Both needles should swing to full scale and back — **once**.
3. Switch off, wait a moment, switch on again — one sweep again.

If you built variant 2, open `Settings → Advanced`; there should be a `Gauge sweep` entry. Switching it off and on again gives an immediate needle test, and the setting survives ignition cycles.

## If it goes wrong

- **Cluster won't boot / black screen:** flash your **original** firmware VBF back. This is why Step 0 exists.
- **Deep brick (no bootloader response):** recovery typically needs JTAG/BDM hardware. See the "recover bricked Convers+" threads on the microhacker forum.
- **No sweep at all after a good flash:** first switch the ignition on for a minute or so, then off, then on again — a very short first cycle may not arm the end-of-cycle detection. If it still does nothing, open an issue with the output of Step 2.
- **More than one sweep in a row:** that is the failure mode this design is built to prevent, and we want to hear about it. Open an issue and say how many, and whether it happens on every ignition or only sometimes.
