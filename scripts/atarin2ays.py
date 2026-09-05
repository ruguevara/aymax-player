#!/usr/bin/env python3
"""Extract the 4-bit digital samples from the Atarin demo into AYMax .ays files.

The Atarin demo (tekkno lab, 2018) stores 8 hand-made 4-bit AY volume-code
samples (~6 kHz) in bank 3 of its 128K snapshot, pointed to by SAMPLE_PTR_TABLE
at $AF00. Those codes use the same nibble layout AYMax expects (low nibble =
earlier sample), so the conversion is a pure passthrough: decode the stream up
to its low-nibble==0 terminator and wrap it in the 16-byte AYS1 header.

No quantisation and no resampling: the codes are emitted unchanged at their
native ~6 kHz rate. When AYMax's 15556 Hz loop plays them as-is, they sound
pitched up. That is expected and out of scope here.

    scripts/atarin2ays.py                       # defaults: atarin snapshot -> build/atarin_NN.nibble.ays
    scripts/atarin2ays.py path/to.sna -o build  # explicit snapshot + out dir
    scripts/atarin2ays.py --pack byte           # 1 code/byte instead of nibble
    scripts/atarin2ays.py --preview-wav         # also write float WAVs to audition
"""

import argparse
import os
import struct
import sys
from pathlib import Path

# The Atarin snapshot is not distributed. Pass its path or set ATARIN_SNA.
DEFAULT_SNA = Path(os.environ["ATARIN_SNA"]) if os.environ.get("ATARIN_SNA") else None
SAMPLE_PTR_TABLE = 0xAF00   # in bank 2 ($8000-$BFFF window)
NATIVE_RATE = 6000          # demo plays the samples at ~6 kHz


def load_banks(sna_path):
    """Reconstruct the 8 RAM banks from a standard 128K .sna."""
    data = Path(sna_path).read_bytes()
    body = data[27:]                       # skip 27-byte header
    first48 = body[:0xC000]                # banks 5, 2, <current> at $4000/$8000/$C000
    rest = body[0xC000:]
    port7ffd = rest[2]
    cur = port7ffd & 7
    blob = rest[4:]                        # remaining 5 banks, ascending, minus 5/2/cur
    banks = {
        5: first48[0x0000:0x4000],
        2: first48[0x4000:0x8000],
        cur: first48[0x8000:0xC000],
    }
    for i, b in enumerate(x for x in range(8) if x not in (5, 2, cur)):
        banks[b] = blob[i * 16384:(i + 1) * 16384]
    return banks


def read_ptr_table(bank2):
    off = SAMPLE_PTR_TABLE - 0x8000
    return [struct.unpack("<H", bank2[off + i * 2:off + i * 2 + 2])[0]
            for i in range(16)]


def decode_sample(bank3, start):
    """Walk the nibble stream from $C000-relative `start`, stop at lo-nibble==0.

    Returns (codes, end_addr) or None if the pointer can't decode cleanly.
    """
    if not (0xC000 <= start <= 0xFFFF):
        return None
    codes = []
    a = start - 0xC000
    while a < len(bank3):
        byte = bank3[a]
        lo = byte & 0x0F
        if lo == 0:                        # terminator
            return codes, a + 0xC000
        codes.append(lo)
        codes.append(byte >> 4)
        a += 1
    return None                            # ran off the bank without a terminator


def write_ays(path, codes, rate, nibble):
    header = struct.pack("<4sBBBBHIH", b"AYS1", 1, 1, int(nibble), 0,
                         rate, len(codes), 0)
    data = bytes(codes)
    if nibble:
        padded = data + b"\x00"
        data = bytes(padded[i] | (padded[i + 1] << 4)
                     for i in range(0, len(data), 2))
    Path(path).write_bytes(header + data)


def main():
    ap = argparse.ArgumentParser(description="Atarin demo samples -> AYMax .ays")
    ap.add_argument("sna", nargs="?", default=DEFAULT_SNA,
                    help="128K snapshot (default: $ATARIN_SNA)")
    ap.add_argument("-o", "--outdir", default="build",
                    help="output directory (default: build)")
    ap.add_argument("--pack", choices=("nibble", "byte"), default="nibble",
                    help="nibble = 2 codes/byte (default), byte = 1 code/byte")
    ap.add_argument("-r", "--rate", type=int, default=NATIVE_RATE,
                    help=f"sample rate written to the header (default {NATIVE_RATE})")
    ap.add_argument("--preview-wav", action="store_true",
                    help="also write a float .wav per sample for PC audition")
    args = ap.parse_args()
    if args.sna is None:
        ap.error("no snapshot: pass a path or set ATARIN_SNA")

    banks = load_banks(args.sna)
    ptrs = read_ptr_table(banks[2])
    bank3 = banks[3]

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    nibble = args.pack == "nibble"

    n = 0
    print(f"{'#':>2}  {'start':>6}  {'end':>6}  {'bytes':>5}  {'samples':>7}  "
          f"{'ms@%d' % args.rate:>7}  file")
    for ptr in ptrs:
        decoded = decode_sample(bank3, ptr)
        if decoded is None or not decoded[0]:
            continue                       # garbage slot, or immediate terminator
        codes, end = decoded
        n += 1
        out = outdir / f"atarin_{n:02d}.{args.pack}.ays"
        write_ays(out, codes, args.rate, nibble)
        if args.preview_wav:
            import soundfile as sf
            sys.path.insert(0, str(Path(__file__).resolve().parent / "taym" / "python" / "src"))
            from taym import spec
            from taym.engine.engine import _dac_table
            dac = _dac_table(spec.AY_VARIANT_AY)
            wav = out.with_suffix(".preview.wav")
            sf.write(wav, [dac[code] for code in codes], args.rate, subtype="FLOAT")
        ms = len(codes) / args.rate * 1000
        print(f"{n:>2}  {ptr:#06x}  {end:#06x}  {end - ptr:>5}  "
              f"{len(codes):>7}  {ms:>7.0f}  {out}")

    if n == 0:
        sys.exit("no decodable samples found in SAMPLE_PTR_TABLE")
    print(f"\nwrote {n} sample(s) to {outdir}/")


if __name__ == "__main__":
    main()
