#!/usr/bin/env python3
"""AYMax record, event, name, and diagnostic text helpers."""

import math
import struct
from pathlib import Path

# PSG write order, followed by a 16-bit control field.
RECORD_ORDER = (0, 1, 2, 3, 4, 5, 6, 11, 12, 13, 7, 8, 9, 10)
RECORD_SIZE = 16

QUANT_T = 228                 # the 228 T / 15556 Hz grid (every engine's reload base)
CPU_HZ = 3_546_900            # 128K CPU clock the fx engines count T-states in

# Diagnostic flat-stream type bits used by atarinpsg.py.
TYPE_PSG = 0x00
TYPE_DUTY = 0x80              # fx type 10: DDS-duty kernel
TYPE_SID = 0xC0              # fx type 11, sub-type 0: SID square (countdown-toggle)
TYPE_BUZZ = 0xD0             # fx type 11, sub-type 1: sync-buzzer (countdown-toggle, R13)

DUTY_INC_NUM = 65536 * QUANT_T // 32   # DDS INC = this / P (full period = 32*P T)

NOTE_NAMES = ("C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-")


def to_record(regs, param16=0):
    """Pack 14 absolute registers (R0..R13) + param16 into a 16-byte
    write-order record."""
    return bytes(regs[i] for i in RECORD_ORDER) + struct.pack("<H", param16 & 0xFFFF)


# Event stream encoding. See docs/v1-2-spec.md.
RECORD_ORDER_V12 = RECORD_ORDER

# Event identities (control16 high byte bits 1:0).
EV_EMPTY = 0
EV_START = 1
EV_MODIFY = 2
EV_STOP = 3

# Fixed kernel identities (control16 low byte bits 6:4).
K_COUNTDOWN = 0
K_DDS_DUTY = 1
K_DDS_EDGE = 2
K_SAMPLE = 3
K_WAVETABLE = 4
K_DDS_SAMPLE = 5

KERNEL_NAMES = {
    K_COUNTDOWN: "countdown", K_DDS_DUTY: "dds_duty",
    K_DDS_EDGE: "dds_edge", K_SAMPLE: "sample",
    K_WAVETABLE: "wavetable", K_DDS_SAMPLE: "dds_sample",
}
EVENT_NAMES = {EV_EMPTY: "EMPTY", EV_START: "START", EV_MODIFY: "MODIFY", EV_STOP: "STOP"}


def control16(event, kernel=0, target=0):
    """Pack target, kernel, and event into the v1.2 control word."""
    if event in (EV_EMPTY, EV_STOP):         # canonical: no kernel/target bits
        return event << 8
    return (event << 8) | ((kernel & 7) << 4) | (target & 0x0F)


def to_record_v12(regs, ctl16):
    """Pack 14 absolute registers and little-endian control16."""
    return bytes(regs[i] for i in RECORD_ORDER_V12) + struct.pack("<H", ctl16 & 0xFFFF)


def event_entry(value_b=0, duty8=0, timer16=0):
    """Pack [B/index, duty8, timer16 low, timer16 high]."""
    return struct.pack("<BBH", value_b & 0xFF, duty8 & 0xFF, timer16 & 0xFFFF)


def note_name(freq):
    if freq <= 0:
        return "???"
    n = round(12 * math.log2(freq / 440.0)) + 69   # MIDI number, A4 = 440
    if not 0 <= n <= 119:
        return "???"
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


def param_to_freq(typ, param16):
    """Convert an engine-final param16 back to the source frequency (Hz).
    This inverts the converter formulas. BUZZ and SID both reload in half-T
    units (param*2 + QUANT_T = T per retrigger / per half-period -- the
    unified AyCpuSid kernel); duty is a DDS increment."""
    if param16 == 0:
        return 0.0
    if typ == TYPE_BUZZ:
        return CPU_HZ / (2 * param16 + QUANT_T)          # retrigger rate (half-T reload)
    if typ == TYPE_SID:
        return CPU_HZ / (2 * param16 + QUANT_T)          # square half-period (half-T reload)
    if typ == TYPE_DUTY:
        return CPU_HZ * param16 / (65536 * QUANT_T)       # DDS INC
    return 0.0


def engine_name(typ, val_a, val_b):
    """Resolve the concrete engine the player would run for this fx instance.
    SID and both buzz variants are one kernel (AyCpuSid, K-COUNTDOWN); the
    label names the variant. Buzz depends on the shapes: A==B = AyBuzz,
    A!=B = AyBuzz2. SID is the volume-square variant."""
    if typ == TYPE_BUZZ:
        return "AyCpuSid/buzz" if val_a == val_b else "AyCpuSid/buzz2"
    if typ == TYPE_SID:
        return "AyCpuSid/sid"
    if typ == TYPE_DUTY:
        return "AyDuty"
    return "----"


def write_fx_txt(path, rows, empty_run=4):
    """Tracker-like fx view. rows = list of dicts produced by the converter,
    one per frame:

        {frame, type, param, P, reg, val_a, val_b, duty, freq, raw_tmr,
         clamped, edge, marks}

    type in {TYPE_PSG, TYPE_SID, TYPE_DUTY, TYPE_BUZZ}; param = engine-final
    param16 (0 = no change); P = raw source period before conversion; reg =
    target AY reg the kernel writes; val_a/val_b = the countdown-toggle's two
    values (shapes for buzzer, loud/off for square); duty = duty nibble (DUTY
    only); freq = raw timer Hz; edge = True on fx-on / instrument-change;
    marks = comment label.

    The `engine` column resolves the concrete player engine (engine_name():
    AyBuzz vs AyBuzz2 by valA==valB, AySid, AyDuty). It is shown on an edge
    or when it changes mid-run. The remaining columns show the kernel
    parameters (glossary.md): the engine writes valA/valB (alternately when
    they differ) to `reg` at the `Hz` rate; `P` = engine-final reload,
    `note` = its pitch. Unchanged columns show dots. More than empty_run
    consecutive plain-PSG frames collapse to one SKP row."""
    reg_names = {13: "R13", 8: "R8", 9: "R9", 10: "R10"}
    # `engine` = resolved player engine on an edge (AyBuzz vs AyBuzz2 = shape
    # A==B vs A!=B), '....' on steady/glide frames. valA/valB are kept so the
    # single-shape (A==B) vs composite (A!=B) case is visible per frame.
    # tmrHz is the raw source timer rate. Hz is the kernel rate.
    lines = ["frame  engine    note      Hz    tmrHz      P  reg  valA valB  dty",
             "-----  -------   ---   ------  -------  -----  ---  ---- ----  ---"]
    last = {"reg": None, "a": None, "b": None, "duty": None, "eng": None}
    pending = 0                   # plain-PSG frames not yet emitted
    pend_from = 0

    def flush(upto):
        nonlocal pending
        if pending > empty_run:
            lines.append(f"{pend_from:5d}  SKP        {pending:5d}")
        else:
            for j in range(upto - pending, upto):
                lines.append(f"{j:5d}  ---")
        pending = 0

    def col(val, key, width, edge):
        """val as int, '.' if unchanged from last and not an edge, '' if None."""
        if val is None:
            return " " * width
        if not edge and val == last[key]:
            return f"{'.':>{width}}"
        last[key] = val
        return f"{val:>{width}d}"

    was = TYPE_PSG
    for r in rows:
        i = r["frame"]
        if r.get("marks"):
            flush(i)
            lines.append(f"; --- {r['marks']} ---")
        typ = r["type"]
        if typ == TYPE_PSG:
            if was != TYPE_PSG:
                lines.append(f"{i:5d}  OFF")
                last = {k: None for k in last}
            else:
                if pending == 0:
                    pend_from = i
                pending += 1
            was = typ
            continue
        if pending:
            flush(i)
        edge = r.get("edge", typ != was)
        name = engine_name(typ, r.get("val_a"), r.get("val_b"))
        # show the engine on an edge OR when it changes mid-run (AyBuzz<->AyBuzz2
        # as shape A==B flips), '....' on steady frames
        eng = name if (edge or name != last["eng"]) else "...."
        last["eng"] = name
        param = r["param"]
        freq = r.get("freq") or 0.0          # engine-final retrigger rate this frame
        raw_tmr = r.get("raw_tmr") or 0.0
        hzcol = f"{freq:6.1f}" if freq else "     ."
        tmrcol = f"{raw_tmr:7.1f}" if raw_tmr else "      ."
        if param:                            # period changed this frame
            clamp = "!" if r.get("clamped") else " "
            P = r.get("P")
            pstr = f"{P:5d}" if P is not None else "  . "
            note = f"{note_name(param_to_freq(typ, param)):<3s}   {hzcol}  {tmrcol}  {pstr}{clamp}"
        else:                                # steady: pitch unchanged, still show Hz/tmrHz
            note = f"...   {hzcol}  {tmrcol}    ... "
        reg = r.get("reg")
        rcol = reg_names.get(reg, f"R{reg}" if reg is not None else "")
        if not edge and reg == last["reg"]:
            rcol = "."
        last["reg"] = reg
        acol = col(r.get("val_a"), "a", 4, edge)
        bcol = col(r.get("val_b"), "b", 4, edge)
        dcol = col(r.get("duty"), "duty", 3, edge)
        lines.append(f"{i:5d}  {eng:<7s}  {note}  {rcol:>3s}  {acol} {bcol}  {dcol}".rstrip())
        was = typ
    # trailing plain-PSG run
    if pending:
        flush(pend_from + pending)
    Path(path).write_text("\n".join(lines) + "\n")
    return len(lines)
