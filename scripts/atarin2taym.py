#!/usr/bin/env python3
"""Convert the Atarin demo into a TAYM interchange file with timer events.

Decodes the snapshot's delta-compressed AY stream (atarinpsg.SlotDecoder) into
an embedded PSG0 chunk, and derives one timer column from the reverse-
engineered R9 event bits (atarin-reverse README section 5):

    R9 bit5  SID square   -> COUNTDOWN on R9  (equal half-period rate)
    R9 bit6  duty square  -> DDS-DUTY on R9   (unequal high/low rates)
    R9 bit7  PCM sample   -> R9 lane of AY volume codes at ~6 kHz

Pitch/duty/volume changes while a voice stays in the same mode are MODULATE;
mode off (SID/DUTY bits clear) is STOP. Sample triggers are one frame wide;
a STOP is scheduled when the nibble stream duration ends (or earlier on a
new voice). Empty sample-table slots emit STOP.

SID/duty rates model the 16-bit budget underflow in AY_SQUARE_START /
AY_DUTY_COMPUTE (P<=40 and (16+B)*P < 780).

PSG0 is the plain register dump (fx bits cleared). R7 follows Atarin's
mode-specific mixer OR mask: unchanged for SID, $02 for duty, and $12 for
samples.

    scripts/atarin2taym.py                       # default snapshot, main segment
    scripts/atarin2taym.py snap.sna --segment intro -o intro.taym
"""
from __future__ import annotations

import argparse
import math
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "taym" / "python" / "src"))
from atarinpsg import SEGMENTS, SlotDecoder, plain_frame, write_psg  # noqa: E402
from atarin2ays import DEFAULT_SNA, load_banks  # noqa: E402
from taym import spec, write_taym, validate  # noqa: E402
from taym.model import Actn, Chip, Lane, Mods, Taym, Timr, Tlan, Trak  # noqa: E402

CPU_HZ = 3_500_000
ATARIN_CLOCK = CPU_HZ // 2
FRAME_RATE = CPU_HZ / 71_680
# Pentagon AY_SAMPLE_LOOP alternates 571 T and 591 T per nibble.
SAMPLE_RATE = CPU_HZ / 581
SAMPLE_PTR_TABLE = 0xAF00
TARGET_R9 = 0x09
SAMPLE_MIXER_OR = 0x12           # tone B + noise B off
DUTY_MIXER_OR = 0x02             # tone B off
SID_BUDGET_SUB = 644             # AY_SQUARE_START: 16*P - 644
DUTY_BUDGET_SUB = 780            # AY_DUTY_COMPUTE: (16+B)*P - 780


def read_ptr_table(bank2):
    off = SAMPLE_PTR_TABLE - 0x8000
    return [struct.unpack("<H", bank2[off + i * 2:off + i * 2 + 2])[0]
            for i in range(16)]


def decode_sample(bank3, start):
    """Nibble stream from bank3; stop at lo-nibble==0. Returns codes or []."""
    if not (0xC000 <= start <= 0xFFFF):
        return []
    codes = []
    a = start - 0xC000
    while a < len(bank3):
        byte = bank3[a]
        lo = byte & 0x0F
        if lo == 0:
            return codes
        codes.append(lo)
        codes.append(byte >> 4)
        a += 1
    return codes


def psg_bytes(frames) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".psg", delete=False) as f:
        tmp = Path(f.name)
    try:
        write_psg(tmp, frames)
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


def parse_psg_framecount(data: bytes) -> int:
    body, i, n = data[16:], 0, 0
    while i < len(body):
        b = body[i]
        if b == 0xFD:
            break
        if b == 0xFF:
            n += 1; i += 1
        elif b == 0xFE:
            n += body[i + 1]; i += 2
        else:
            i += 2
    return n


def _sid_params(regs):
    """Return (P_eff, vol, half_period_rate_hz) for an R9 bit5 frame.

    AY_SQUARE_START stores ((16*P) - 644) in 16 bits. For P >= 41 the edge
    interval measures exactly 16*P (overhead cancels). For P <= 40 the subtract
    underflows and the edge interval is about 65536 + 16*P.
    """
    P = regs[2] | (regs[3] << 8)
    if P & 0xF000:                       # AY_SQUARE_INIT: flagged params doubled
        P = (P * 2) & 0xFFFF
    prod16 = (16 * P) & 0xFFFF           # ADD HL,HL x4 in AY_SQUARE_START
    if prod16 < SID_BUDGET_SUB:          # P <= 40: 16-bit budget underflow
        half_p = 65536 + prod16
    else:
        half_p = prod16 if prod16 else 0x10000
    vol = regs[9] & 0x0F
    return P, vol, CPU_HZ / half_p


def _duty_params(regs):
    """Return (P, B, vol, rate_hi, rate_lo) or None if P==0 (voice off).

    AY_DUTY_COMPUTE stores ((16+B)*P - 780) in 16 bits as the first-edge
    budget. When ta < 780 that subtract wraps; schedule rate_hi from the
    wrapped interval. The second-edge offset is (16-B)*P.
    """
    B = regs[3] >> 4
    P = (regs[2] | (regs[3] << 8)) & 0x0FFF
    if P == 0:
        return None
    vol = regs[9] & 0x0F
    ta = (16 + B) * P
    tb = (16 - B) * P
    if ta < DUTY_BUDGET_SUB:             # 16-bit period-budget underflow
        interval_hi = (ta - DUTY_BUDGET_SUB) & 0xFFFF
    else:
        interval_hi = ta
    interval_lo = tb if tb else 1
    return P, B, vol, CPU_HZ / interval_hi, CPU_HZ / interval_lo


def _parse_want(regs):
    """Classify one stream frame into a voice want, or None (plain / off)."""
    r9 = regs[9]
    if r9 & 0x80:
        return ("sample", r9 & 0x0F)
    if r9 & 0x40:
        d = _duty_params(regs)
        if d is None:
            return None
        P, B, vol, fa, fb = d
        return ("duty", P, B, vol, fa, fb)
    if r9 & 0x20:
        P, vol, rate = _sid_params(regs)
        return ("sid", P, vol, rate)
    return None


class _Pools:
    """Dedup VU08/VU32 slices and LANE/TLAN/ACTN records."""

    def __init__(self):
        self.vu08: list[int] = []
        self.vu32: list[int] = []
        self.lanes: list[Lane] = []
        self.tlanes: list[Tlan] = []
        self.actions: list[Actn] = []
        self._u8 = {}
        self._u32 = {}
        self._lane = {}
        self._tlan = {}
        self._act = {}

    def _append_u8(self, values):
        key = tuple(values)
        if key in self._u8:
            return self._u8[key]
        off = len(self.vu08)
        self.vu08.extend(values)
        self._u8[key] = off
        return off

    def _append_u32(self, values):
        key = tuple(values)
        if key in self._u32:
            return self._u32[key]
        off = len(self.vu32)
        self.vu32.extend(values)
        self._u32[key] = off
        return off

    def lane_u8(self, values, loop_index):
        off = self._append_u8(values)
        key = (spec.VT_U8, off, len(values), loop_index)
        if key in self._lane:
            return self._lane[key]
        idx = len(self.lanes)
        self.lanes.append(Lane(value_type=spec.VT_U8, value_offset=off,
                               length=len(values), loop_index=loop_index))
        self._lane[key] = idx
        return idx

    def tlan_abs(self, rates_hz, loop_index):
        vals = [spec.to_fix16(r) for r in rates_hz]
        off = self._append_u32(vals)
        key = (spec.TM_ABSOLUTE, off, len(vals), loop_index)
        if key in self._tlan:
            return self._tlan[key]
        idx = len(self.tlanes)
        self.tlanes.append(Tlan(timing_mode=spec.TM_ABSOLUTE, value_offset=off,
                                length=len(vals), loop_index=loop_index))
        self._tlan[key] = idx
        return idx

    def act_slice(self, acts):
        """Append a sorted action slice; return (first, count)."""
        acts = tuple(sorted(acts, key=lambda a: a.target_id))
        key = tuple((a.target_id, a.source_mode, a.operand) for a in acts)
        if key in self._act:
            return self._act[key]
        first = len(self.actions)
        self.actions.extend(acts)
        self._act[key] = (first, len(acts))
        return first, len(acts)


def _fix_rate(hz: float) -> int:
    if not spec.fits_fix16(hz) or hz <= 0:
        raise ValueError(f"timer rate {hz:.3f} Hz out of 16.16 range")
    return spec.to_fix16(hz)


def _build_timers(frames, sample_codes) -> tuple:
    """Build (mods, pools) for one timer from Atarin voice edges."""
    pools = _Pools()
    mods: list[Mods] = []
    # active: None | ('sid', P, vol, rate) | ('duty', P, B, vol, fa, fb)
    #         | ('sample', idx)
    active = None

    def start_sid(P, vol, rate):
        lane = pools.lane_u8([vol, 0], loop_index=0)
        first, n = pools.act_slice([
            Actn(target_id=TARGET_R9, source_mode=spec.SRC_BIND_LANE, operand=lane)])
        return Mods(command=spec.CMD_START, base_timer_value=_fix_rate(rate),
                    timer_lane_ref=spec.TLAN_NONE, first_action=first, action_count=n)

    def start_duty(P, B, vol, fa, fb):
        lane = pools.lane_u8([vol, 0], loop_index=0)
        tlan = pools.tlan_abs([fa, fb], loop_index=0)
        first, n = pools.act_slice([
            Actn(target_id=TARGET_R9, source_mode=spec.SRC_BIND_LANE, operand=lane)])
        return Mods(command=spec.CMD_START, base_timer_value=_fix_rate(fa),
                    timer_lane_ref=tlan, first_action=first, action_count=n)

    def start_sample(idx):
        # Atarin writes AY volume-register codes; bind them to R9 directly
        # (not TGT_SAMPLE_AMPLITUDE — that path expects linear amplitude).
        codes = sample_codes[idx]
        lane = pools.lane_u8(codes, loop_index=spec.NO_LOOP)
        first, n = pools.act_slice([
            Actn(target_id=TARGET_R9, source_mode=spec.SRC_BIND_LANE, operand=lane)])
        return Mods(command=spec.CMD_START, base_timer_value=_fix_rate(SAMPLE_RATE),
                    timer_lane_ref=spec.TLAN_NONE, first_action=first, action_count=n)

    def sample_stop_frame(start_i, n_codes):
        """First plain frame after the nibble stream finishes (~6 kHz)."""
        dur = max(1, math.ceil(n_codes * FRAME_RATE / SAMPLE_RATE))
        return start_i + dur

    for i, regs in enumerate(frames):
        want = _parse_want(regs)
        if want is not None and want[0] == "sample":
            idx = want[1]
            codes = sample_codes[idx] if idx < len(sample_codes) else []
            if not codes:
                # Empty table slot (e.g. index 0): silence, not a 1-sample START.
                mods.append(Mods(command=spec.CMD_STOP))
                active = None
            else:
                mods.append(start_sample(idx))
                active = ("sample", idx, sample_stop_frame(i, len(codes)))
            continue

        if want is not None and want[0] == "sid":
            _, P, vol, rate = want
            if active and active[0] == "sid":
                aP, aVol = active[1], active[2]
                if P == aP and vol == aVol:
                    mods.append(Mods(command=spec.CMD_EMPTY))
                else:
                    lane = pools.lane_u8([vol, 0], loop_index=0)
                    first, n = pools.act_slice([
                        Actn(target_id=TARGET_R9, source_mode=spec.SRC_BIND_LANE,
                             operand=lane)])
                    base = _fix_rate(rate) if P != aP else 0
                    tref = spec.TLAN_NONE if P != aP else spec.TLAN_UNCHANGED
                    # vol-only: rebind lane; pitch-only: retune base; both: both.
                    acount = n if vol != aVol else 0
                    afirst = first if acount else 0
                    mods.append(Mods(command=spec.CMD_MODULATE, base_timer_value=base,
                                     timer_lane_ref=tref, first_action=afirst,
                                     action_count=acount))
                active = ("sid", P, vol, rate)
            else:
                mods.append(start_sid(P, vol, rate))
                active = ("sid", P, vol, rate)
            continue

        if want is not None and want[0] == "duty":
            _, P, B, vol, fa, fb = want
            if active and active[0] == "duty":
                aP, aB, aVol = active[1], active[2], active[3]
                if P == aP and B == aB and vol == aVol:
                    mods.append(Mods(command=spec.CMD_EMPTY))
                else:
                    lane = pools.lane_u8([vol, 0], loop_index=0)
                    first, n = pools.act_slice([
                        Actn(target_id=TARGET_R9, source_mode=spec.SRC_BIND_LANE,
                             operand=lane)])
                    if P != aP or B != aB:
                        tlan = pools.tlan_abs([fa, fb], loop_index=0)
                        base = _fix_rate(fa)
                        tref = tlan
                    else:
                        base = 0
                        tref = spec.TLAN_UNCHANGED
                    acount = n if vol != aVol else 0
                    afirst = first if acount else 0
                    mods.append(Mods(command=spec.CMD_MODULATE, base_timer_value=base,
                                     timer_lane_ref=tref, first_action=afirst,
                                     action_count=acount))
                active = ("duty", P, B, vol, fa, fb)
            else:
                mods.append(start_duty(P, B, vol, fa, fb))
                active = ("duty", P, B, vol, fa, fb)
            continue

        # plain frame: sample until its duration ends; SID/DUTY stop now.
        if active and active[0] == "sample":
            if i >= active[2]:
                mods.append(Mods(command=spec.CMD_STOP))
                active = None
            else:
                mods.append(Mods(command=spec.CMD_EMPTY))
        elif active is not None:
            mods.append(Mods(command=spec.CMD_STOP))
            active = None
        else:
            mods.append(Mods(command=spec.CMD_EMPTY))

    return mods, pools


def _background_frames(frames, mods):
    """Plain PSG frames with Atarin's mode-specific mixer OR mask."""
    mixer_or = 0
    out = []
    for regs, m in zip(frames, mods):
        want = _parse_want(regs)
        if m.command == spec.CMD_STOP:
            mixer_or = 0
        elif want is not None and want[0] == "sample":
            mixer_or = SAMPLE_MIXER_OR
        elif want is not None and want[0] == "duty":
            mixer_or = DUTY_MIXER_OR
        elif want is not None and want[0] == "sid":
            mixer_or = 0
        r = bytearray(plain_frame(regs))
        r[7] |= mixer_or
        out.append(bytes(r))
    return out


def convert(sna_path, segment: str) -> Taym:
    banks = load_banks(sna_path)
    ptrs = read_ptr_table(banks[2])
    sample_codes = [decode_sample(banks[3], p) for p in ptrs]

    dec = SlotDecoder()
    dec.decode_segment(banks[1], SEGMENTS["intro"])     # ring warmup
    frames = dec.decode_segment(banks[1], SEGMENTS[segment])

    mods, pools = _build_timers(frames, sample_codes)
    bg = _background_frames(frames, mods)
    psg = psg_bytes(bg)
    fc = parse_psg_framecount(psg)
    if fc != len(mods):
        raise RuntimeError(f"frame count mismatch: psg {fc} vs mods {len(mods)}")

    return Taym(
        trak=Trak(frame_rate_hz=FRAME_RATE, frame_count=fc, loop_frame=spec.NO_LOOP),
        chips=[Chip(clock_hz=ATARIN_CLOCK, chip_type_id=spec.CHIP_TYPE_AY,
                    name="AY", frame_data_tag="PSG0")],
        timers=[Timr(chip_index=0, clock_mode=spec.CLOCK_ABS_RATE_HZ, clock_divider=0)],
        mods=mods,
        actions=pools.actions,
        lanes=pools.lanes,
        tlanes=pools.tlanes,
        vu08=pools.vu08,
        vu16=[],
        vu32=pools.vu32,
        frame_data={"PSG0": psg},
    )


def main():
    ap = argparse.ArgumentParser(description="Atarin demo -> TAYM")
    ap.add_argument("sna", nargs="?", default=DEFAULT_SNA,
                    help="128K snapshot (default: $ATARIN_SNA)")
    ap.add_argument("--segment", choices=tuple(SEGMENTS), default="main")
    ap.add_argument("-o", "--out", default="build/atarin.taym")
    ap.add_argument("--no-validate", action="store_true")
    args = ap.parse_args()
    if args.sna is None:
        ap.error("no snapshot: pass a path or set ATARIN_SNA")

    taym = convert(args.sna, args.segment)
    if not args.no_validate:
        problems = validate(taym)
        if problems:
            print(f"VALIDATION FAILED ({len(problems)}):", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 1

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_bytes(write_taym(taym))
    from collections import Counter
    hist = Counter(m.command for m in taym.mods)
    names = {spec.CMD_EMPTY: "EMPTY", spec.CMD_START: "START",
             spec.CMD_MODULATE: "MODULATE", spec.CMD_STOP: "STOP"}
    parts = ", ".join(f"{names[c]}={hist[c]}" for c in sorted(hist))
    print(f"wrote {args.out}: {taym.trak.frame_count} frames, PSG0 "
          f"{len(taym.frame_data['PSG0'])} bytes, 1 timer ({parts})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
