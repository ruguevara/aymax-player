```
     ______    ____       ____________      ________     ______      ____      ____
    /      \   \   \     /   /|       \    /       |    /      \     \   \    /   /
   /   /\   \    \   \ /   /  |   |\   \  /   /|   |   /   /\   \      \   \/   /
  /   /__\   \     \  '  /    |   | \   \/   / |   |  /   /__\   \      /      \
 /    ____    \     |   |     |   |  \      /  |   | /    ____    \   /   /  \   \
/____/    \____\    |___|     |___|   \____/   |___|/____/    \____\/____/    \____\
```

# AYMax player

A standalone player build for [AYMax](https://pixelmatter.org/aymax/), a
constant-time AY-3-8912 synthesis engine for the ZX Spectrum 128K. This repo takes a track in the
[TAYM](https://github.com/ruguevara/taym) interchange format, converts it
to the AYMax runtime format, and assembles a `.tap` and a `.sna` that load
and play it.

The engine ships as a prebuilt binary in `bin/`. Its source is not part of
this repo. The converter scripts, the wrapper assembly, the tape loader, and
the format documentation are all here and are MIT licensed.

## Features

AYMax runs one timer-driven effect on top of the normal 50 Hz register
stream. The timer resolution is one quant of 228 T (about 15.5 kHz on a
128K). In Atari ST and chiptune terms it can play:

| Effect | Chiptune name | AYMax kernel |
|--------|---------------|--------------|
| Volume toggled between two levels at a timer rate | SID voice (Atari ST "SID sound") | `K-COUNTDOWN` on R8, R9, R10 |
| SID voice with adjustable pulse width | SID with PWM / duty sweep | `K-DDS-DUTY` on R8, R9, R10 |
| Envelope retriggered at a timer rate | Sync Buzzer | `K-DDS-EDGE` on R13 (any pitch), `K-COUNTDOWN` on R13 |
| Envelope retriggered with two shapes per period: shape A at the period start, shape B at a duty point that can be swept | Sync Buzzer alternating two envelope shapes with PWM sweep | `K-DDS-EDGE` on R13 (swept duty), `K-COUNTDOWN` on R13 (fixed 50/50) |
| 4-bit PCM on a channel volume, one sample per quant | Digi-drums | `K-SAMPLE` on R8, R9, R10 |
| 4-bit PCM at any rate, pitched | Pitched digi-drums, sampled instruments | `K-DDS-SAMPLE` on R8, R9, R10 |
| Tone period low byte modulated at a timer rate by a 256-byte table | FM (a two-level table toggles the pitch like a timer; a ramp or sine gives vibrato and FM timbres) | `K-WAVETABLE` on R0, R2, R4 |
| 256-byte table streamed into one register at a DDS rate | Wavetable volume (custom waveforms), fast arpeggio on tone period, noise sweeps, mixer and envelope period modulation | `K-WAVETABLE` on R0..R12 |

Plain AY features (tone, noise, mixer, hardware envelope, buzz bass) come
from the 50 Hz register stream and need no kernel. Only one kernel runs at a
time, so one timer effect is active per frame; see Limits.

## Requirements

- Python 3.10 or newer (no extra packages)
- [sjasmplus](https://github.com/z00m128/sjasmplus) 1.20 or newer on PATH
- GNU make
- `make setup` once, to fetch the `scripts/taym` submodule

## Quick start

```
git clone --recursive https://github.com/ruguevara/aymax-player.git
cd aymax-player
make
```

This builds the example track and writes `build/atarized.tap` and
`build/atarized.sna`. Load either in a ZX Spectrum 128K emulator, or play
the `.tap` on real hardware.

## Usage

```
make TAYM=path/to/song.taym                         # loop, noise border, black screen
make TAYM=path/to/song.taym PLAY_ONCE=1             # play once, hold the last frame
make TAYM=path/to/song.taym BORDER=0                # black border
make TAYM=path/to/song.taym SCREEN=path/to/pic.scr  # show a loading screen
make TAYM=examples/atarized.taym SCREEN=examples/atarized.scr
make clean
```

Options can be combined. Outputs are `build/<name>.tap` and
`build/<name>.sna`, where `<name>` is the base name of the `.taym` file.

The music from the Atarin demo (Techno Lab, 2018; music by Nik-O, code by
Kowalski; https://demozoo.org/productions/186527/) is also included as
`examples/atarin.taym`. It is the copyright of its authors and is here
only as a test track; it is not covered by this repo's MIT license. It
uses SID, duty, and sampled voices on channel B:

```
make TAYM=examples/atarin.taym
```

The source snapshot is not distributed. Pass its path, or set `ATARIN_SNA`
and omit it:

```
python3 scripts/atarin2taym.py path/to/snapshot_pentagon.sna -o examples/atarin.taym
ATARIN_SNA=path/to/snapshot_pentagon.sna python3 -B tests/test_atarin2taym.py
```

The tests that need the snapshot are skipped when `ATARIN_SNA` is unset.
Conversion uses the main segment. `scripts/atarinpsg.py` exports PSG diagnostics, and
`scripts/atarin2ays.py` extracts AYS samples. These tools use the standard
library; the optional `atarin2ays.py --preview-wav` needs SoundFile.

| Option | Default | Effect |
|--------|---------|--------|
| `TAYM=file.taym` | `examples/atarized.taym` | Input track. |
| `PLAY_ONCE=1` | `0` (loop) | Play the track once, then hold the last frame. With `0` the track restarts from its loop point. |
| `BORDER=0` | `1` | `1` shows a noise pattern on the border while the track plays. `0` keeps the border black. |
| `SCREEN=file.scr` | none | 6912-byte ZX screen (pixels + attributes) shown while the track plays. The `.sna` contains it; the tape loads it as the first block. Without it the startup code clears the screen to black. |
| `COMPACT=1` | `0` | Drop the 128K check and its messages from the BASIC loader (about 380 bytes less tape). |
| `ENGINE=auto` | `auto` | Engine variant: `auto` picks the smallest binary in `bin/` whose kernel set covers the track; or name one: `full`, `nosamples`, `nowavetables`, `min`, `sid`. A variant that lacks a kernel the track uses fails the build with a message. |
| `PYTHON=...` | `python3` | Python interpreter used for the converter. |
| `SJASMPLUS=...` | `sjasmplus` | Assembler binary. |

`PLAY_ONCE`, `BORDER`, `SCREEN`, `COMPACT`, and `ENGINE` only change the
final assembly step, so switching them does not rerun the converter.

## Engine variants

`bin/` holds one prebuilt engine per kernel set. Smaller sets give a
smaller tape:

| Binary | Kernels | Size |
|--------|---------|------|
| `aymax_player.bin` | all six | 6731 |
| `aymax_player_nosamples.bin` | no `K-SAMPLE`, `K-DDS-SAMPLE` | 5689 |
| `aymax_player_nowavetables.bin` | no `K-WAVETABLE` | 6546 |
| `aymax_player_min.bin` | no samples, no wavetables | 5504 |
| `aymax_player_sid.bin` | `K-COUNTDOWN` and `K-DDS-DUTY` only (SID voice and PWM) | 5088 |

`psgpack.py` writes the kernels a track uses as `KernelMask` in
`<name>_dir.inc`; each `.inc` exports its set as `AymaxKernels`. With
`ENGINE=auto` the Makefile compares the two and takes the smallest match.

## How it works

1. `scripts/taym2aymax.py` converts the `.taym` track to unpacked PSG
   records, an event stream, and the sample and wavetable data.
2. `scripts/psgpack.py` packs the records into `<name>.pack` (a 20-lane LZ
   stream) and lays out `<name>_assets.bin` (samples and wavetables in
   16 KiB bank images). When the pack fits in the free tail of the last
   asset bank, it is appended there, so a small track needs one bank.
   `<name>_dir.inc` tells the wrapper the bank sizes and where the pack is.
3. `src/player.asm` places the engine binary, the pack, and the asset banks
   in memory, adds a small startup stub behind the engine, and writes the
   `.sna`. `src/loader.asm` writes the `.tap`: a BASIC loader plus one
   block per memory bank in use, and one block for the engine and stub.

The data formats are described in [docs/aymax-format.md](docs/aymax-format.md).

## Memory map

| Bank | Address       | Contents |
|------|---------------|----------|
| 4, 6 | `#C000-#FFFF` | Asset banks: sample directory, wavetables, samples, and the packed track when it fits behind them. Bank 6 only when the track needs it. |
| 0, 1 | `#C000-#FFFF` | Packed track when it does not fit in the asset bank. Bank 1 only when the pack exceeds 16 KiB. |
| 2    | `#8000-#BFFF` | Engine code and runtime RAM up to `AymaxRamEnd`; IM2 table at `#BE00-#BFC1`. The startup stub sits at `AymaxProgramEnd`; it runs once before the engine uses that RAM. |
| 5    | `#4000-#5AFF` | Screen (`SCREEN=`), or cleared to black by the stub. |
| 5    | `#5D00-#5FFF` | BASIC loader and loading code (`.tap` only). |

The engine never returns. It owns the CPU, the stack (`#BDFF` down), and
interrupt mode 2 while the track plays.

## Engine symbols

`bin/aymax_player.inc` is generated together with the binary. The wrapper
uses these symbols:

| Symbol | Meaning |
|--------|---------|
| `AymaxPlayerStart` | Entry point. Copies the sample directory, installs IM2, starts playback. Never returns. |
| `AymaxProgramEnd`  | End of the binary image (`bin/aymax_player.bin` covers `#8000` to here). |
| `AymaxRamEnd`      | End of engine RAM. Nothing else may live in `#8000` to here. |
| `AymaxLoopImm`     | 16-bit word that selects the end-of-track node. |
| `AymaxLoopReset`, `AymaxStopReset` | Values for `AymaxLoopImm`: loop, or play once. |
| `AymaxPackStart`, `AymaxPackBankInit` | Pack header address and `#7FFD` value of the first pack bank. The stub writes them before entry; defaults are `#C000` in bank 0. |
| `AymaxPackBank`, `AymaxPageBase` | Default pack bank and the paged window (0, `#C000`). |
| `AymaxKernels` | Bit mask of the kernels in this variant (bit = kernel id). |
| `AymaxHasSamples`, `AymaxHasWavetables` | 1 when the variant has the sample kernels, the wavetable kernel. |
| `AymaxAssetBank0`, `AymaxAssetBank1` | Asset banks (4, 6). |
| `AymaxSampleDir`   | 256-byte page that receives the sample directory at start. Only in variants with sample kernels. |
| `AymaxBorderOp`    | Opcode of the border `out`; write `#DB` to turn the border effect off. |

## Limits

- Packed track: 32 KiB (two banks).
- Assets: 32 KiB (two banks); all wavetables must fit in bank 4.
- 63 samples.
- One timer lane at a time: the engine runs a single synthesis kernel, so
  only one timer effect can be active in any frame. A new START replaces
  the running one.
- One AY register at a time: the active kernel drives exactly one target
  register; the other registers only take their plain per-frame values.
- One kernel event per frame. See the format document for the allowed
  kernel and register pairs.

## Files

- `Makefile` -- build driver.
- `src/player.asm` -- wrapper: engine, pack, assets, startup stub, `.sna`.
- `src/loader.asm` -- 128K tape loader and `.tap` writer.
- `bin/aymax_player*.bin`, `bin/aymax_player*.inc` -- prebuilt engine variants and their symbols.
- `scripts/taym2aymax.py`, `scripts/psgpack.py`, `scripts/aymax_fx.py`,
  `scripts/aymax_assets.py` -- the converter.
- `scripts/taym/` -- TAYM format package (submodule).
- `scripts/atarin2taym.py`, `scripts/atarinpsg.py`, `scripts/atarin2ays.py` -- Atarin conversion tools.
- `docs/aymax-format.md` -- packed track and asset format.
- `examples/atarized.taym`, `examples/atarized.scr` -- example track
  ("Atarized" by otomata) and its screen.
- `examples/atarin.taym` -- Atarin main segment with timer and sample events
  (music by Nik-O / Techno Lab, copyright of its authors).
- `tests/` -- converter tests: `python3 tests/test_taym2aymax.py`,
  `python3 tests/test_psgpack.py`, `python3 tests/test_atarin2taym.py`.

## Notes

- The `.tap` checks for a 128K machine and stops with a message on a 48K.
- Emulators must be set to ZX Spectrum 128K (or a compatible 128K model)
  with an AY chip.

## License

MIT, see [LICENSE](LICENSE). `src/loader.asm` is derived from the sjasmplus
`io_tape_ldrs.h` loader (zlib license). The engine binary in `bin/` may be
redistributed with your track builds under the same MIT terms.

## Credits

ru grantez (ex ruguevara) — main work; spke — inspiration, ideas, advices.

## Greets to

diver, pator, megus, sq, bfox, n1k-o, fatalsnipe, grongy, dalthon, jammerC64, wbcbz7, kowalski, volutar, tmk, true-grue and all chiptune musicians and demosceners.
