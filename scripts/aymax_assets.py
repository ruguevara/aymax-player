#!/usr/bin/env python3
"""Shared AYMax 1.2 input parsing and unpacked asset tables.

  parse_psg          Bulba .PSG dump -> absolute AY register states per frame.
  SampleTable        deduplicated 4-bit sample code streams (K-SAMPLE,
                     K-DDS-SAMPLE) plus the rate cooks that feed them.
  WavetableTable     deduplicated 256-byte tables (K-WAVETABLE).
  serialize_*        the <stem>_samples.bin / <stem>_wavetables.bin layouts.

Used by taym2aymax.py and psg2aymax.py.
"""

from __future__ import annotations

import math
import struct

from aymax_fx import CPU_HZ, QUANT_T

QUANT_HZ = CPU_HZ / QUANT_T
DDS_SAMPLE_HZ = QUANT_HZ / 2
DDS_SAMPLE_GUARD_CODES = 16
WAVETABLE_SIZE = 256
MIN_WAVETABLE_ITEMS = 3
PSG_MAGIC = b"PSG\x1a"
PSG_HEADER_SIZE = 16
PSG_END = 0xFD
PSG_SKIP = 0xFE
PSG_FRAME = 0xFF
R13 = 13
R13_NO_WRITE = 0xFF
MAX_TABLE_ENTRIES = 255
MAX_U16 = 0xFFFF
# Hardware-effective masks for R0..R13.
AY_REG_MASKS = (
    0xFF,
    0x0F,
    0xFF,
    0x0F,
    0xFF,
    0x0F,
    0x1F,
    0xFF,
    0x1F,
    0x1F,
    0x1F,
    0xFF,
    0xFF,
    0x0F,
)
def parse_psg(data):
    """Return one absolute AY register state per Bulba PSG frame.

    Writing R13 retriggers the envelope. Use the no-write value after each
    frame unless the source writes a new shape.
    """
    if len(data) < PSG_HEADER_SIZE or data[:4] != PSG_MAGIC:
        raise ValueError("not a .PSG (bad magic)")
    body = data[PSG_HEADER_SIZE:]
    shadow = [0] * 14
    shadow[R13] = R13_NO_WRITE
    frames = []
    started = False
    index = 0

    def commit():
        frames.append(shadow[:])
        shadow[R13] = R13_NO_WRITE

    while index < len(body):
        command = body[index]
        if command == PSG_END:
            break
        if command == PSG_FRAME:
            if started:
                commit()
            started = True
            index += 1
        elif command == PSG_SKIP:
            if index + 1 >= len(body):
                raise ValueError("truncated .PSG repeat command")
            repeat_count = body[index + 1]
            index += 2
            if not started:
                started = True
            for _ in range(repeat_count):
                commit()
        else:
            if index + 1 >= len(body):
                raise ValueError("truncated .PSG register write")
            if command < len(shadow):
                shadow[command] = body[index + 1]
            index += 2
    if started:
        commit()
    return frames

def validate_sample_rate(freq, location):
    """Accept source rates that K-SAMPLE can upsample to its quant rate."""
    if not 0 < freq <= QUANT_HZ:
        raise ValueError(
            f"{location}: sample rate {freq} is outside player range "
            f"0..{QUANT_HZ:.1f} Hz"
        )


def cook_dds_sample_rate(freq, frame):
    """Cook a source-code rate for the half-quant DDS sample kernel."""
    if not 0 < freq <= DDS_SAMPLE_HZ:
        raise ValueError(
            f"frame {frame}: DDS sample rate {freq} is outside player range "
            f"0..{DDS_SAMPLE_HZ:.1f} Hz"
        )
    inc = math.floor(freq * 65536 / DDS_SAMPLE_HZ)
    return max(1, min(inc, 0xFFFF))


def pad_dds_sample_codes(codes, rate, frame_rate):
    """Pad a DDS sample with zero codes through its STOP frame."""
    if frame_rate <= 0:
        raise ValueError("sample frame rate must be positive")
    stop_frames = max(1, math.ceil(len(codes) * frame_rate / rate))
    boundary_codes = math.ceil(stop_frames * rate / frame_rate)
    padded_count = max(len(codes), boundary_codes) + DDS_SAMPLE_GUARD_CODES
    return list(codes) + [0] * (padded_count - len(codes)), stop_frames


def upsample_sample_codes(codes, src_rate, dst_rate):
    """Linear resample in the AY's log-like volume-code space."""
    if dst_rate < src_rate:
        raise ValueError("sample output rate is below source rate")
    n = math.ceil(len(codes) * dst_rate / src_rate)
    out = []
    for i in range(n):
        pos = i * src_rate / dst_rate
        lo = min(len(codes) - 1, math.floor(pos))
        hi = min(len(codes) - 1, lo + 1)
        value = codes[lo] + (codes[hi] - codes[lo]) * (pos - lo)
        out.append(math.floor(value + 0.5))
    return out

class SampleTable:
    """Deduplicated unpacked sample code streams."""

    def __init__(self):
        self._entries = []
        self._index = {}

    def __iter__(self):
        return iter(self._entries)

    def __len__(self):
        return len(self._entries)

    def intern(self, codes):
        key = tuple(codes)
        if not key:
            raise ValueError("empty sample")
        if any(not 0 <= code <= 0x0F for code in key):
            raise ValueError("sample code outside 0..15")
        if key in self._index:
            return self._index[key]
        idx = len(self._entries)
        if idx >= MAX_TABLE_ENTRIES:
            raise ValueError("sample table exceeds 255 entries")
        self._entries.append(key)
        self._index[key] = idx
        return idx

def _wavetable_freqs(freqs, count, where):
    """Return one positive step frequency per wavetable value."""
    if len(freqs) == 1:
        freqs = freqs * count
    if len(freqs) != count or any(freq <= 0 for freq in freqs):
        raise ValueError(f"{where}: wavetable needs one positive frequency per value")
    return freqs


def cook_wavetable_rate(freqs, count, where):
    """Cook the 8.8-table DDS increment from one complete source cycle."""
    freqs = _wavetable_freqs(freqs, count, where)
    cycle_seconds = sum(1.0 / freq for freq in freqs)
    fundamental = 1.0 / cycle_seconds
    inc = round(65536 * fundamental / QUANT_HZ)
    return max(0, min(inc, 0xFFFF)), fundamental

class WavetableTable:
    """Deduplicated 256-byte tables generated from loop values and timing."""

    def __init__(self):
        self._entries = []
        self._index = {}

    def __iter__(self):
        return iter(self._entries)

    def __len__(self):
        return len(self._entries)

    def intern(self, values, freqs, where):
        if not MIN_WAVETABLE_ITEMS <= len(values) <= WAVETABLE_SIZE:
            raise ValueError(
                f"{where}: wavetable has {len(values)} items; expected 3..256"
            )
        freqs = _wavetable_freqs(freqs, len(values), where)
        durations = [1.0 / freq for freq in freqs]
        duration_sum = sum(durations)

        # Give one table entry to each source step. Divide the remaining entries
        # by duration. Assign the rounding remainder by largest fractional part.
        remaining = WAVETABLE_SIZE - len(values)
        raw_extra = [duration / duration_sum * remaining for duration in durations]
        extra = [math.floor(value) for value in raw_extra]
        residue = remaining - sum(extra)
        order = sorted(
            range(len(values)),
            key=lambda i: (raw_extra[i] - extra[i], -i),
            reverse=True,
        )
        for i in order[:residue]:
            extra[i] += 1

        table = bytes(
            value & 0xFF
            for value, count in zip(values, extra)
            for _ in range(count + 1)
        )
        assert len(table) == WAVETABLE_SIZE
        if table in self._index:
            return self._index[table]
        index = len(self._entries)
        if index >= MAX_TABLE_ENTRIES:
            raise ValueError("wavetable bank exceeds 255 entries")
        self._entries.append(table)
        self._index[table] = index
        return index


def serialize_samples(samples):
    """Serialize length-prefixed unpacked sample code streams."""
    out = bytearray()
    for codes in samples:
        if len(codes) > MAX_U16:
            raise ValueError("sample exceeds 65535 codes")
        out += struct.pack("<H", len(codes))
        out += bytes(codes)
    return bytes(out)


def serialize_wavetables(wavetables):
    """Serialize consecutive canonical 256-byte wavetables."""
    return b"".join(wavetables)

