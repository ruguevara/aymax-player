# AYMax player

A public wrapper around the prebuilt AYMax AY player engine for the ZX
Spectrum 128K. It takes a `.taym` track, converts it, and assembles a
standalone `.tap` and `.sna` that load and play the track. Engine source is
not included; only the prebuilt binary in `bin/`.

## Requirements

- Python 3.10+
- sjasmplus 1.20+ on PATH
- `make setup` once, to fetch the `taym` converter submodule

## Usage

```
make                          # builds the example track examples/atarized.taym
make TAYM=path/to/song.taym
```

Optional:

```
make TAYM=path/to/song.taym PLAY_ONCE=1
```

`PLAY_ONCE=1` plays the track once instead of looping.

Outputs go to `build/<name>.tap` and `build/<name>.sna`, where `<name>` is
the `.taym` file's base name.

## How it works

1. `scripts/taym2aymax.py` converts the `.taym` track to unpacked PSG,
   event, sample, and wavetable data.
2. `scripts/psgpack.py` packs that data into `<name>.pack` (the compressed
   note/event stream) and `<name>_assets.bin` (samples and wavetables), plus
   `<name>_dir.inc` (bank layout constants for the wrapper).
3. `src/player.asm` assembles the engine binary, the packed track, the
   assets, and a small startup stub into one `.sna` and `.tap`.

## Memory map

| Bank | Contents |
|------|----------|
| 0/1  | Packed track (`<name>.pack`), 16 KiB each, bank 1 only if > 16 KiB |
| 2    | Engine code, `#8000`..`AymaxRamEnd`, plus IM2 table at `#BE00`..`#BFC1` |
| 4/6  | Assets (`<name>_assets.bin`), 16 KiB each, bank 6 only if the track uses it |
| 5    | Startup stub at `#6000` (selects loop mode, jumps into the engine) |

## Limits

- Packed track: 32 KiB max (2 banks).
- Assets (samples + wavetables): 32 KiB max (2 banks).
- 63 samples max.

## Files

- `src/player.asm` -- top-level wrapper: loads the engine, the track, and
  the assets, then jumps in.
- `src/loader.asm` -- 128K TAP loader (BASIC stub + loading code).
- `bin/aymax_player.bin` -- prebuilt engine, raw binary, load address `#8000`.
- `bin/aymax_player.inc` -- engine symbols (`AymaxPlayerStart`,
  `AymaxRamEnd`, bank numbers, and so on) used by the wrapper.
- `scripts/taym2aymax.py`, `scripts/psgpack.py` -- the `.taym` converter.
- `scripts/taym/` -- the TAYM format submodule (`make setup` to fetch).
- `examples/atarized.taym` -- example track ("Atarized" by otomata).
- `tests/` -- converter tests: `python3 tests/test_taym2aymax.py`,
  `python3 tests/test_psgpack.py`.
- `Makefile` -- build driver; see Usage above.

## Notes

- The engine loops by default. `PLAY_ONCE=1` patches the loop mode in the
  startup stub, so it applies to both the `.tap` and the `.sna`.
- The `.tap` loads on real hardware or in an emulator set to ZX Spectrum
  128K with an AY chip; it refuses to run past its check on 48K.
