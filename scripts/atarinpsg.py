#!/usr/bin/env python3
"""Decode the Atarin snapshot's delta-coded AY register stream.

The default output is a flat 16-byte diagnostic record:

    R0 R1 R2 R3 R4 R5 R6 R11 R12 R13 R7 R8 R9 R10 param16-LE

`--psg` writes a standard Bulba `.PSG` dump. `--fx-txt` writes a readable
event table. Segment reset clears decoder state but keeps the 256-byte rings,
so the intro is decoded before the main segment.
"""

import argparse
import math
import struct
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from atarin2ays import DEFAULT_SNA, load_banks  # noqa: E402

SEGMENTS = {"intro": 0xC000, "main": 0xC652}
RECORD_ORDER = [0, 1, 2, 3, 4, 5, 6, 11, 12, 13, 7, 8, 9, 10]
RECORD_SIZE = 16


class SlotDecoder:
    """The 14-slot ring-buffer decoder; state persists across segments."""

    def __init__(self):
        self.rings = [bytearray(256) for _ in range(14)]
        self.state = [[0, 0, 0] for _ in range(14)]  # [write_ptr, delta_step, countdown]

    def decode_segment(self, bank1, seg_addr):
        """Decode one whole segment; returns a list of 14-byte frames (R0..R13)."""
        off = seg_addr - 0xC000
        nframes = struct.unpack_from("<H", bank1, off)[0]
        pos = off + 2
        for s in self.state:                     # restart clears slot state, keeps rings
            s[0] = s[1] = s[2] = 0
        frames = []
        for _ in range(nframes):
            regs = bytearray(14)
            for slot in range(13, -1, -1):
                ptr, step, cnt = self.state[slot]
                ring = self.rings[slot]
                if cnt:
                    cnt -= 1
                    if step:
                        val = ring[(ptr + step) & 0xFF]
                    else:
                        val = bank1[pos]; pos += 1
                else:
                    ctl = bank1[pos]; pos += 1
                    cnt = ctl >> 1
                    if ctl & 1:
                        step = bank1[pos]; pos += 1
                        val = ring[(ptr + step) & 0xFF]
                    else:
                        step = 0
                        val = bank1[pos]; pos += 1
                ring[ptr] = val
                self.state[slot] = [(ptr + 1) & 0xFF, step, cnt]
                regs[slot] = val
            frames.append(bytes(regs))
            assert pos <= len(bank1), "stream ran past the end of bank 1"
        return frames


SID_QUANT_T = 229                       # AySid's grid (the SMC value is HALF_PERIOD - QUANT_T)
TYPE_SID = 0xC0                         # fx type 11: SID square (countdown-toggle)
TYPE_DUTY = 0x80                        # fx type 10: DDS duty kernel
DUTY_INC_NUM = 65536 * SID_QUANT_T // 32   # INC = this / P (full period = 32*P T)


def fx_frame(regs, prev, detune=0, tone_through=False):
    """Map one Atarin frame to AYMax fx fields + param16 (see module docstring).

    prev = {'type': 0/TYPE_SID/TYPE_DUTY, 'P': int} carries the fx state
    across frames. param16 is emitted only on type edges and on pitch
    changes. A direct square<->duty switch re-emits, because the
    engine-final encodings differ. The per-note volume (R9 low bits) is
    carried in bits 3:0 of the fx-typed volume byte every fx frame. The duty
    nibble is carried in R1's chip-masked top bits every duty frame.

    detune adds T-states to the SID half-period so the CPU square drifts
    against the live AY tone (FX_TONE_THROUGH builds) -- the classic SID PWM
    shimmer. tone_through also rewrites R2/R3 on SID frames to the EFFECTIVE
    period (half_period/16, always <= $FFF) and clears the tone bit in R7.
    Reason: the raw $20xx params would put the hardware tone an octave above
    the CPU square (the chip masks R3 to 4 bits), and AND-ing f with 2f
    gives an octave stack instead of the classic drifting pulse.
    """
    r = bytearray(regs)
    param = 0
    r[8] &= 0x1F
    r[10] &= 0x1F
    r9 = r[9]
    if r9 & 0x80:                        # Flat diagnostics omit sample events.
        r[9] = 0
        prev["type"] = 0
    elif r9 & 0x40:                      # duty voice -> DDS duty kernel
        B = r[3] >> 4                    # AY_DUTY_INIT: duty param = R3 high nibble
        P = (r[2] | (r[3] << 8)) & 0x0FFF
        if P == 0:                       # Atarin: zero period = voice off
            r[9] = r9 & 0x0F
            prev["type"] = 0
        else:
            inc = max(1, min((DUTY_INC_NUM + P // 2) // P, 0xFFFF))
            if prev["type"] != TYPE_DUTY or P != prev["P"]:
                param = inc
            r[1] = (r[1] & 0x0F) | (B << 4)  # duty nibble for TmrDuty (chip-masked bits)
            r[9] = TYPE_DUTY | (r9 & 0x0F)   # fx type 10 + per-note volume
            prev["type"] = TYPE_DUTY
            prev["P"] = P
            prev["clamped"] = False          # INC is clamp-free (P <= 4095)
    elif r9 & 0x20:                      # square voice -> SID engine
        P = r[2] | (r[3] << 8)
        if P & 0xF000:                   # AY_SQUARE_INIT: flagged params are doubled
            P = (P * 2) & 0xFFFF
        half_p = (16 * P) & 0xFFFF       # AY_SQUARE_START's x16 wraps in 16 bits,
                                         # so $20xx aliases to bass periods
        wo_qt = half_p - SID_QUANT_T + detune   # AySid .periodWoQT_imm value
        clamped = not (1 <= wo_qt <= 32767)
        wo_qt = max(1, min(wo_qt, 32767))
        if prev["type"] != TYPE_SID or P != prev["P"]:
            param = wo_qt
            assert param != 0, "param16==0 is the no-change sentinel"
        if tone_through:
            pe = half_p >> 4                 # effective AY period (P mod 4096)
            r[2] = pe & 0xFF
            r[3] = pe >> 8
            r[7] &= ~(1 << 1)                # ensure tone B enabled in the mixer
        r[9] = TYPE_SID | (r9 & 0x0F)    # fx type 11 + per-note volume
        prev["type"] = TYPE_SID
        prev["P"] = P
        prev["clamped"] = clamped
    else:                                # plain PSG frame
        r[9] &= 0x1F
        prev["type"] = 0
    return bytes(r), param


def plain_frame(regs):
    """Convert software-square frames to plain AY squares for --plain."""
    r = bytearray(regs)
    for v in (8, 9, 10):
        if r[v] & 0x80:                  # sample trigger: low bits = index, not volume
            r[v] = 0
        elif r[v] & 0x60:                # square/duty voice: low bits = synth volume
            r[v] &= 0x1F
    return bytes(r)


def to_record(regs, param16=0):
    return bytes(regs[i] for i in RECORD_ORDER) + struct.pack("<H", param16)


# Per-register canonical masks: bits the AY actually latches. R1/R3/R5 are
# 4-bit period highs, R6 is 5 bits, R7 keeps the 6 mixer bits (IO-direction
# bits 7:6 don't belong in a music dump), R8-R10 are 5 bits, R13 is the 4-bit
# shape (bit 7 is the don't-retrigger sentinel, tested before masking).
AY_REG_MASK = (0xFF, 0x0F, 0xFF, 0x0F, 0xFF, 0x0F, 0x1F,
               0x3F, 0x1F, 0x1F, 0x1F, 0xFF, 0xFF, 0x0F)


def write_psg(path, frames):
    """Write a standard .PSG register dump (AY_Emul/Bulba convention) for
    regular PSG players: 16-byte 'PSG\\x1a' header, then $FF = next frame
    (50 Hz interrupt), reg,value pairs (changed regs only), $FD = end.

    Frames pass through plain_frame() first -- the clean fx-less degradation
    (squares become plain AY squares with their volume, samples become
    silence). Then each register is reduced to the bits the AY actually
    latches (AY_REG_MASK). The volume nibble is also zeroed when the
    envelope-mode bit is set, because the chip ignores it then. This keeps
    the delta stream minimal and makes diffs against other dumps of the same
    music meaningful. R13 is written whenever bit 7 is clear: every write
    retriggers the envelope, so it is emitted per event, not per change.
    """
    out = bytearray(b"PSG\x1a")
    out += bytes(12)
    shadow = [None] * 14
    for regs in frames:
        regs = plain_frame(regs)
        out.append(0xFF)
        for r in range(14):
            v = regs[r]
            if r == 13:
                if v & 0x80:
                    continue                 # don't-retrigger sentinel
                out += bytes((13, v & AY_REG_MASK[13]))
                continue
            v &= AY_REG_MASK[r]
            if r in (8, 9, 10) and v & 0x10:
                v = 0x10                     # env mode: volume nibble ignored
            if shadow[r] != v:
                out += bytes((r, v))
                shadow[r] = v
    out.append(0xFD)
    Path(path).write_bytes(out)
    return len(out)


NOTE_NAMES = ("C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-")
CPU_HZ = 3_546_900                      # 128K clock the fx engines count in


def note_name(freq):
    if freq <= 0:
        return "???"
    n = round(12 * math.log2(freq / 440.0)) + 69   # MIDI number, A4 = 440
    if not 0 <= n <= 119:
        return "???"
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


def write_fx_txt(path, frames, start=0, empty_run=4, marks=None):
    """Tracker-like text view of the fx layer (--fx-txt).

    One row per frame. Unchanged columns show dots (the tracker idiom). A
    run of more than empty_run consecutive fx-less frames collapses into one
    'SKP' row (start frame, skipped-frame count in the P column). The note
    is decoded back from param16, the engine-final pitch: SID half-period
    16*P - 229, duty DDS INC 65536*229/(32*P). P is the raw stream period
    that fx_frame tracked. '!' marks a clamped SID period. vol = per-note
    volume (fx vol byte bits 3:0). dty = the raw duty nibble B (duty frames
    only).

    Sample events (R9 bit 7; mapped to SILENCE in the record output) get one
    'SMP' row each. The note column shows the sample index ('S3' =
    SAMPLE_PTR_TABLE entry 3; the stream stores index*2 in bits 4:1; there
    is no per-event volume or pitch -- the rate is the fixed ~6 kHz loop).
    The P column shows the duration in frames (trigger up to the next
    event). The silence after the trigger belongs to that row, not to
    OFF/SKP rows. marks = {frame_number: label} inserts '; --- label ---'
    comment lines (segment boundaries).
    """
    names = {TYPE_SID: "SID ", TYPE_DUTY: "DUTY"}
    lines = ["frame  fx    note      P  vol  dty",
             "-----  ----  ---  -----  ---  ---"]
    prev = {"type": 0, "P": 0, "clamped": False}
    last_vol = last_duty = None
    smp = None                           # pending (frame, index) sample trigger
    pending = 0                          # fx-less frames not yet emitted

    def flush(upto):
        nonlocal pending
        if pending > empty_run:
            lines.append(f"{upto - pending:5d}  SKP        {pending:5d}")
        else:
            for j in range(upto - pending, upto):
                lines.append(f"{j:5d}  ---")
        pending = 0

    def flush_smp(upto):
        nonlocal smp
        if smp:
            frame, idx = smp
            lines.append(f"{frame:5d}  SMP   S{idx:<2d}  {upto - frame:5d}")
            smp = None

    for i, regs in enumerate(frames, start):
        if marks and i in marks:
            flush_smp(i)
            flush(i)
            lines.append(f"; --- {marks[i]} ---")
        was = prev["type"]
        out, param = fx_frame(regs, prev)
        typ = prev["type"]
        if regs[9] & 0x80:               # sample event (silenced in the records)
            flush_smp(i)
            flush(i)
            smp = (i, (regs[9] & 0x1E) >> 1)  # AY_SAMPLE_INIT: index*2 in bits 4:1
            last_vol = last_duty = None
            continue
        if typ == 0:
            if smp:                      # silence belongs to the pending sample row
                pass
            elif was:                    # fx-off edge
                lines.append(f"{i:5d}  OFF")
                last_vol = last_duty = None
            else:
                pending += 1
            continue
        flush_smp(i)
        if pending:
            flush(i)
        edge = typ != was
        fx = names[typ] if edge else "...."
        if param:
            if typ == TYPE_SID:          # param = half_period - SID_QUANT_T
                freq = CPU_HZ / (2 * (param + SID_QUANT_T))
            else:                        # param = DDS INC per 229-T quant
                freq = CPU_HZ * param / (65536 * SID_QUANT_T)
            clamp = "!" if prev.get("clamped") else " "
            note = f"{note_name(freq):<3s}  {prev['P']:5d}{clamp}"
        else:
            note = "...    ... "
        vol = out[9] & 0x0F
        vcol = f"{vol:3d}" if edge or vol != last_vol else "  ."
        last_vol = vol
        if typ == TYPE_DUTY:
            duty = out[1] >> 4
            dcol = f"{duty:3d}" if edge or duty != last_duty else "  ."
            last_duty = duty
        else:
            dcol = ""
        lines.append(f"{i:5d}  {fx}  {note} {vcol}  {dcol}".rstrip())
    flush_smp(start + len(frames))
    flush(start + len(frames))
    Path(path).write_text("\n".join(lines) + "\n")
    return len(lines)


def main():
    ap = argparse.ArgumentParser(description="Atarin PSG stream -> AYMax frame records")
    ap.add_argument("sna", nargs="?", default=DEFAULT_SNA,
                    help="128K snapshot (default: $ATARIN_SNA)")
    ap.add_argument("-o", "--out", default="build/atarin_psg.bin",
                    help="output file (default: build/atarin_psg.bin)")
    ap.add_argument("--segment", choices=tuple(SEGMENTS), default="main",
                    help="which stream segment to emit (default: main)")
    ap.add_argument("-f", "--frames", type=int, default=None,
                    help="max frames to emit (default: 1024 = one 16K bank for the "
                         ".bin, truncated to a multiple of 16 for the page-aligned "
                         "wrap; the whole segment for --psg)")
    ap.add_argument("--psg", metavar="PATH", nargs="?", const="build/atarin.psg",
                    help="write a standard fx-less .PSG register dump for regular "
                         "PSG players instead of the AYMax .bin "
                         "(default PATH: build/atarin.psg)")
    ap.add_argument("--fx-txt", metavar="PATH", nargs="?", const="build/atarin_fx.txt",
                    help="write a tracker-like text view of the fx layer (type/"
                         "note/volume/duty per frame, sample events as SMP rows, "
                         "empty runs collapsed) of the WHOLE track in playback "
                         "order (intro then main, continuous frame numbers; "
                         "--segment is ignored) instead of the AYMax .bin "
                         "(default PATH: build/atarin_fx.txt)")
    ap.add_argument("--start", type=int, default=0,
                    help="skip this many frames of the segment first (default 0)")
    ap.add_argument("--plain", action="store_true",
                    help="fx-less output: squares degrade to plain AY squares")
    ap.add_argument("--solo", action="store_true",
                    help="isolate the fx channel: zero the A/C volumes (R8/R10)")
    ap.add_argument("--sid-detune", type=int, nargs="?", const=5, default=0,
                    metavar="T",
                    help="add T T-states to the SID half-period (5 if given bare) "
                         "so the CPU square drifts against the live AY tone -- "
                         "PWM shimmer; audible only with FX_TONE_THROUGH builds")
    ap.add_argument("--tone-through", action="store_true",
                    help="rewrite R2/R3 on fx frames to the EFFECTIVE period so "
                         "the AY tone matches the CPU square 1:1 ($20xx params "
                         "otherwise land an octave up); pair with the "
                         "FX_TONE_THROUGH build and --sid-detune")
    ap.add_argument("--no-sanitize", action="store_true",
                    help="keep the raw synth event bits in R8-R10")
    args = ap.parse_args()
    if args.sna is None:
        ap.error("no snapshot: pass a path or set ATARIN_SNA")

    banks = load_banks(args.sna)
    bank1 = banks[1]

    dec = SlotDecoder()
    intro = dec.decode_segment(bank1, SEGMENTS["intro"])   # fills the rings
    marks = None
    if args.fx_txt:                      # whole track in playback order:
        frames = intro + dec.decode_segment(bank1, SEGMENTS["main"])
        marks = {len(intro): "main segment"}   # intro once, then main (loops)
    elif args.segment == "intro":
        frames = intro
    else:
        frames = dec.decode_segment(bank1, SEGMENTS["main"])

    total = len(frames)
    whole = args.psg or args.fx_txt
    nframes = args.frames if args.frames is not None else (total if whole else 1024)
    frames = frames[args.start:args.start + nframes]
    if not whole:
        frames = frames[:len(frames) // 16 * 16]
    if not frames:
        sys.exit(f"no frames left (segment has {total}, --start {args.start})")

    if args.psg:
        size = write_psg(args.psg, frames)
        print(f"{args.segment} segment: {total} frames decoded, "
              f"{len(frames)} (from {args.start}) -> {args.psg} ({size} bytes, .PSG)")
        return

    if args.fx_txt:
        Path(args.fx_txt).parent.mkdir(parents=True, exist_ok=True)
        rows = write_fx_txt(args.fx_txt, frames, args.start, marks=marks)
        print(f"whole track (intro+main): {total} frames decoded, "
              f"{len(frames)} (from {args.start}) -> {args.fx_txt} ({rows} lines, fx view)")
        return

    events = Counter()
    for regs in frames:
        for bit, name in ((0x80, "sample"), (0x40, "duty"), (0x20, "square")):
            if regs[9] & bit:
                events[name] += 1
    skipped_r13 = sum(1 for regs in frames if regs[13] & 0x80)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    edges = params = clamps = 0
    prev = {"type": 0, "P": 0, "clamped": False}
    with out.open("wb") as f:
        for regs in frames:
            param = 0
            if args.no_sanitize:
                pass
            elif args.plain:
                regs = plain_frame(regs)
            else:
                was = prev["type"]
                regs, param = fx_frame(regs, prev, args.sid_detune, args.tone_through)
                edges += was != prev["type"]
                params += param != 0
                clamps += prev.get("clamped", False) and param != 0
            if args.solo:
                r = bytearray(regs)
                r[8] = r[10] = 0          # silence channels A and C
                regs = bytes(r)
            f.write(to_record(regs, param))

    print(f"{args.segment} segment: {total} frames decoded, "
          f"emitting {len(frames)} (from {args.start}) -> {out} "
          f"({len(frames) * RECORD_SIZE} bytes)")
    print(f"event frames: sample={events['sample']} duty={events['duty']} "
          f"square={events['square']}; R13 don't-write frames: {skipped_r13}")
    if not (args.plain or args.no_sanitize):
        print(f"fx: {edges} on/off edges, {params} param frames "
              f"({clamps} clamped periods)")


if __name__ == "__main__":
    main()
