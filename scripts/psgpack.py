#!/usr/bin/env python3
"""Pack unpacked AYMax 1.2 data into the runtime banks.

Input: the four files produced by taym2aymax: PSG records, event entries,
length-prefixed unpacked samples, and consecutive 256-byte wavetables.
Output: the interlaced stream blob, packed asset banks, the asset directory
asm, and an equ-only bank size include for the public wrapper.

Lane model. Each frame is 20 bytes, split into 20 independent byte lanes:

  lane  0..13  register bytes in record write order (record bytes 0..13)
  lane 14..15  control16 lo, hi (record bytes 14..15)
  lane 16..19  event entry bytes [B, duty8, timer16 lo, timer16 hi]

Event lanes are per-frame: frames without a START/MODIFY entry carry a
don't-care byte. The packer fills don't-care bytes per lane with the policy
(zero or repeat-previous) that packs smaller; the player never reads them.

Duty lane 17 stores modulo-256 deltas between K-DDS-DUTY/K-DDS-EDGE events.
Bit 7 of control lane 14 marks the first duty value in each packed block as
absolute. Other kernels do not consume lane 17.

Wire format (all offsets little-endian, relative to blob start):

  +0   u16 frame_count
  +2   u16 loop_frame           first frame of the loop block
  +4   u16 loop_offset          interlaced loop-block offset
  +6   u16 data_offset          first interlaced byte (8 plus optional pad)
  +8   optional pad bytes, then interlaced packed bytes

The runtime maps the blob across consecutive 16 KiB banks at #C000.
A packet pair must not start on the last byte of a bank, because ReadPack
reads the header and the first extra byte together. The packer inserts
0..40 pad bytes after the header so that never happens. Continuation
literals may sit on the last bank byte; the depacker wraps C to the next
page or bank.

Each lane is split at loop_frame into a prefix block and a loop block. Every
lane remains an independent LZ packet stream, but the bytes for all 20 lanes
are emitted in the exact frame/lane order in which the runtime consumes them.
Each block is independent (matches never reference bytes before the block
start), so the runtime loop resets one shared packed-source pointer. Packets:

  0LLLLLLL  literal run: L+1 (1..128) literal bytes follow
  1LLLLLLL  match: copy L+3 (3..130) bytes; one distance byte follows,
            0 means 256, else 1..255 (distance in frames = lane bytes)

The runtime output rings are 256 bytes per lane, so distance is capped at
256; distance 256 reads the ring slot about to be overwritten
(fetch-before-store in the depacker). The packer additionally caps distance
at the bytes already produced in the current block.

The parse is optimal (backward DP over exact byte costs). Equal-size parses
prefer fewer runtime depack nodes, then fewer packets.
"""

import argparse
import struct
from collections import defaultdict
from pathlib import Path

NLANES = 20
RECORD_SIZE = 16
LIT_MAX = 128                 # max literals per packet
MATCH_MIN = 3
MATCH_MAX = 130
WINDOW = 256                  # ring size = max distance
HEADER_SIZE = 8                 # frame_count, loop_frame, loop_offset, data_offset
BANK_SIZE = 16384
MAX_PACK_BANKS = 2
MAX_PACK_SIZE = BANK_SIZE * MAX_PACK_BANKS
BANK_PAD_MAX = 40               # shift pair starts off the last bank byte
ASSET_BANK_SIZE = 16384
MAX_ASSET_BANKS = 2
SAMPLE_HEADER = 2
SAMPLE_DIR_ENTRY = 4
WAVETABLE_SIZE = 256

EV_START, EV_MODIFY = 1, 2
K_DDS_DUTY, K_DDS_EDGE = 1, 2
K_SAMPLE, K_WAVETABLE, K_DDS_SAMPLE = 3, 4, 5


# --------------------------------------------------------------------------
# Asset parsing and packing
# --------------------------------------------------------------------------
def parse_samples(data: bytes):
    """Parse repeated u16 code counts followed by unpacked AY volume codes."""
    samples = []
    ofs = 0
    while ofs < len(data):
        if len(data) - ofs < 2:
            raise ValueError(f"sample {len(samples)}: truncated code count")
        count = struct.unpack_from("<H", data, ofs)[0]
        ofs += 2
        if not count:
            raise ValueError(f"sample {len(samples)}: zero code count")
        if len(data) - ofs < count:
            raise ValueError(f"sample {len(samples)}: truncated code data")
        sample = data[ofs:ofs + count]
        ofs += count
        if any(code > 15 for code in sample):
            raise ValueError(f"sample {len(samples)}: code outside 0..15")
        samples.append(sample)
        if len(samples) > 255:
            raise ValueError("sample table exceeds 255 entries")
    return samples


def parse_wavetables(data: bytes):
    """Parse consecutive canonical 256-byte wavetables."""
    if len(data) % WAVETABLE_SIZE:
        raise ValueError(
            f"wavetable data size {len(data)} is not a multiple of 256")
    count = len(data) // WAVETABLE_SIZE
    if count > 255:
        raise ValueError("wavetable table exceeds 255 entries")
    return [data[ofs:ofs + WAVETABLE_SIZE]
            for ofs in range(0, len(data), WAVETABLE_SIZE)]


def pack_sample_nibbles(codes):
    """Pack AY volume codes 0..15, high nibble first."""
    out = bytearray()
    for i in range(0, len(codes), 2):
        lo = codes[i + 1] if i + 1 < len(codes) else 0
        out.append((codes[i] << 4) | lo)
    return bytes(out)


class AssetBankSet:
    """Pack samples and page-aligned wavetables into banks 4 and 6."""

    def __init__(self, samples, wavetables):
        self.samples = samples
        self.wavetables = wavetables
        self.bank_count = 0

    @staticmethod
    def _align(value, alignment):
        return (value + alignment - 1) & -alignment

    def build(self):
        if len(self.samples) > 63:
            raise ValueError("at most 63 samples fit the directory page")
        data_base = SAMPLE_HEADER + SAMPLE_DIR_ENTRY * len(self.samples)
        images = [bytearray(data_base), bytearray()]
        images[0][0] = len(self.samples) & 0xFF
        cursors = [data_base, 0]
        first = [None, None]
        kinds = [0, 0]             # bit 0: samples, bit 1: wavetables

        # Wavetables go first, in bank 0 (hardware bank 4), right after the
        # header page, so a prebuilt player can address them without reading
        # the generated directory.
        self.wavetable_bank = 4
        self.wavetable_base = 0
        wave_size = len(self.wavetables) * WAVETABLE_SIZE
        if wave_size:
            cursor = self._align(data_base, WAVETABLE_SIZE)
            if cursor + wave_size > ASSET_BANK_SIZE:
                raise ValueError("all wavetables must fit one shared asset bank")
            self.wavetable_base = cursor
            kinds[0] |= 2
            first[0] = cursor
            images[0].extend(bytes(cursor - len(images[0])))
            images[0] += b"".join(self.wavetables)
            cursors[0] = cursor + wave_size

        self.sample_offsets = []
        self.sample_sizes = []
        for i, sample in enumerate(self.samples):
            packed = pack_sample_nibbles(sample)
            count = len(packed)
            bank = next((candidate for candidate in range(MAX_ASSET_BANKS)
                         if cursors[candidate] + count <= ASSET_BANK_SIZE), None)
            if bank is None:
                raise ValueError(
                    f"assets exceed {MAX_ASSET_BANKS} banks of "
                    f"{ASSET_BANK_SIZE} B")
            off = cursors[bank] | (0x8000 if bank else 0)
            self.sample_offsets.append(off)
            self.sample_sizes.append(count)
            kinds[bank] |= 1
            if first[bank] is None:
                first[bank] = cursors[bank]
            start = SAMPLE_HEADER + i * SAMPLE_DIR_ENTRY
            images[0][start:start + SAMPLE_DIR_ENTRY] = struct.pack(
                "<HH", off, count)
            images[bank].extend(bytes(cursors[bank] - len(images[bank])))
            images[bank] += packed
            cursors[bank] += count

        self.bank_used = cursors
        self.bank_kinds = kinds
        self.bank_first = [
            start if start is not None else cursors[i]
            for i, start in enumerate(first)]
        self.bank_count = max(i + 1 for i, used in enumerate(cursors) if used)
        if wave_size:
            assert self.wavetable_bank == 4 and self.wavetable_base == 256
        for image in images[:self.bank_count]:
            image.extend(bytes(ASSET_BANK_SIZE - len(image)))
        return b"".join(images[:self.bank_count])

    def write_dir_asm(self, path):
        """Write the sample directory and constant wavetable location."""
        if not self.bank_count:
            raise RuntimeError("build asset banks before writing the directory")
        lines = ["; AUTO-GENERATED by taym2aymax — shared asset directories",
                 f"SharedBankCount equ {self.bank_count}",
                 "SampleDirTable"]
        if not self.samples:
            lines.append("        dw 6, 1")
        else:
            for off, count in zip(self.sample_offsets, self.sample_sizes):
                lines.append(f"        dw {off}, {count}")
        lines += [
            f"WavetableBank equ {self.wavetable_bank}",
            f"WavetablePage equ {0xC0 + self.wavetable_base // 256}",
            f"AssetKind0 equ {self.bank_kinds[0]}",
            f"AssetKind1 equ {self.bank_kinds[1]}",
            f"AssetFirst0 equ {self.bank_first[0]}",
            f"AssetFirst1 equ {self.bank_first[1]}",
            f"AssetUsed0 equ {self.bank_used[0]}",
            f"AssetUsed1 equ {self.bank_used[1]}",
        ]
        Path(path).write_text("\n".join(lines) + "\n")

    def write_dir_inc(self, path):
        """Write an equ-only asset bank size summary for the public wrapper."""
        if not self.bank_count:
            raise RuntimeError("build asset banks before writing the directory")
        lines = ["; AUTO-GENERATED by psgpack",
                  f"AssetBankCount equ {self.bank_count}"]
        lines += [f"AssetUsed{i} equ {used}"
                  for i, used in enumerate(self.bank_used)]
        Path(path).write_text("\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# Lane matrix construction
# --------------------------------------------------------------------------
def build_lanes(psg: bytes, events: bytes):
    """Split the stream pair into 20 lanes. Event-lane don't-care positions
    are returned as a mask so the fill policy can be chosen later.

    Returns (lanes, entry_mask): lanes = list of 20 bytearrays (event-lane
    don't-care bytes left as 0), entry_mask = per-frame bool (has an entry).
    """
    if len(psg) % RECORD_SIZE:
        raise ValueError(f"psg stream size {len(psg)} not a record multiple")
    if len(events) % 4:
        raise ValueError(f"event stream size {len(events)} not an entry multiple")
    n = len(psg) // RECORD_SIZE
    lanes = [bytearray(n) for _ in range(NLANES)]
    entry_mask = [False] * n
    ev = 0
    for f in range(n):
        rec = psg[f * RECORD_SIZE:(f + 1) * RECORD_SIZE]
        for k in range(16):
            lanes[k][f] = rec[k]
        if rec[15] & 3 in (EV_START, EV_MODIFY):
            entry = events[ev * 4:(ev + 1) * 4]
            if len(entry) < 4:
                raise ValueError(f"frame {f}: event stream exhausted")
            for k in range(4):
                lanes[16 + k][f] = entry[k]
            entry_mask[f] = True
            ev += 1
    if ev * 4 != len(events):
        raise ValueError(f"event stream has {len(events) // 4} entries, "
                         f"records consume {ev}")
    return lanes, entry_mask


def validate_asset_indexes(psg: bytes, events: bytes, samples, wavetables):
    """Reject event indexes outside the matching unpacked asset table."""
    lanes, entry_mask = build_lanes(psg, events)
    for frame, has_entry in enumerate(entry_mask):
        if not has_entry:
            continue
        kernel = (lanes[14][frame] >> 4) & 7
        index = lanes[16][frame]
        if kernel in (K_SAMPLE, K_DDS_SAMPLE) and index >= len(samples):
            raise ValueError(
                f"frame {frame}: sample index {index}; "
                f"table has {len(samples)} entries")
        if kernel == K_WAVETABLE and index >= len(wavetables):
            raise ValueError(
                f"frame {frame}: wavetable index {index}; "
                f"table has {len(wavetables)} entries")


def fill_lane(lane: bytearray, entry_mask, policy: str) -> bytes:
    """Fill don't-care positions of an event lane: 'zero' keeps 0, 'repeat'
    holds the previous meaningful value (0 before the first entry)."""
    if policy == "zero":
        return bytes(lane)
    out = bytearray(lane)
    prev = 0
    for f, has in enumerate(entry_mask):
        if has:
            prev = out[f]
        else:
            out[f] = prev
    return bytes(out)


def _is_duty_event(lanes, frame: int) -> bool:
    event = lanes[15][frame] & 3
    kernel = (lanes[14][frame] >> 4) & 7
    return event in (EV_START, EV_MODIFY) and kernel in (K_DDS_DUTY, K_DDS_EDGE)


def encode_duty_deltas(lanes, loop_frame: int):
    """Return copied lanes with duty-event deltas and their meaningful mask.

    Prefix and loop blocks restart independently. The first duty event in
    each block keeps its absolute value and sets control-low bit 7.
    """
    out = [bytearray(lane) for lane in lanes]
    n = len(out[0])
    duty_mask = [False] * n
    for start, end in ((0, loop_frame), (loop_frame, n)):
        previous = 0
        first = True
        for frame in range(start, end):
            out[14][frame] &= 0x7F
            if not _is_duty_event(out, frame):
                continue
            value = out[17][frame]
            out[17][frame] = (value - previous) & 0xFF
            previous = value
            duty_mask[frame] = True
            if first:
                out[14][frame] |= 0x80
                first = False
    return out, duty_mask


def decode_duty_deltas(lanes, loop_frame: int):
    """Reference inverse of encode_duty_deltas()."""
    out = [bytearray(lane) for lane in lanes]
    n = len(out[0])
    for start, end in ((0, loop_frame), (loop_frame, n)):
        previous = 0
        first = True
        for frame in range(start, end):
            if _is_duty_event(out, frame):
                absolute = bool(out[14][frame] & 0x80)
                if first and not absolute:
                    raise ValueError(f"frame {frame}: duty block has no absolute value")
                previous = ((0 if absolute else previous) + out[17][frame]) & 0xFF
                out[17][frame] = previous
                first = False
            out[14][frame] &= 0x7F
    return out


# --------------------------------------------------------------------------
# Block packer: optimal parse (backward DP, exact byte cost, then runtime
# node cost and packet count)
# --------------------------------------------------------------------------
def _longest_matches(data: bytes):
    """For each position p return (maxlen, dist) of the longest match with
    distance <= min(WINDOW, p), length capped at MATCH_MAX, or (0, 0).
    Any length in [MATCH_MIN, maxlen] is achievable with the same dist."""
    n = len(data)
    best = [(0, 0)] * n
    heads = defaultdict(list)     # 3-byte key -> positions (ascending)
    for p in range(n - MATCH_MIN + 1):
        key = data[p:p + MATCH_MIN]
        blen, bdist = 0, 0
        limit = min(MATCH_MAX, n - p)
        for q in reversed(heads[key]):
            if q < p - WINDOW:
                break
            length = MATCH_MIN
            # self-overlap allowed: compare against already-fixed data
            while length < limit and data[q + length] == data[p + length]:
                length += 1
            if length > blen:
                blen, bdist = length, p - q
                if blen == limit:
                    break
        best[p] = (blen, bdist)
        heads[key].append(p)
    return best


def pack_block(data: bytes) -> bytes:
    """Optimal parse of one block. Empty block packs to empty bytes."""
    n = len(data)
    if n == 0:
        return b""
    matches = _longest_matches(data)
    INF = (1 << 30, 1 << 30, 1 << 30)
    # cost[p] = (bytes, runtime nodes, packets) to encode data[p:].
    # New literal packet: ReadPair + ParsePair, then two nodes per later
    # literal. New match packet: ReadPair + ParsePair + first FetchMatch,
    # then one node per later match byte. The lane entry is included.
    cost = [INF] * (n + 1)
    cost[n] = (0, 0, 0)
    choice = [None] * n           # ('lit', j) or ('match', l, dist)
    for p in range(n - 1, -1, -1):
        best = INF
        pick = None
        for j in range(1, min(LIT_MAX, n - p) + 1):
            c = (1 + j + cost[p + j][0],
                 2 * j + 1 + cost[p + j][1],
                 1 + cost[p + j][2])
            if c < best:
                best, pick = c, ("lit", j)
        mlen, mdist = matches[p]
        for length in range(MATCH_MIN, mlen + 1):
            c = (2 + cost[p + length][0],
                 length + 3 + cost[p + length][1],
                 1 + cost[p + length][2])
            if c < best:
                best, pick = c, ("match", length, mdist)
        cost[p] = best
        choice[p] = pick
    out = bytearray()
    p = 0
    while p < n:
        ch = choice[p]
        if ch[0] == "lit":
            j = ch[1]
            out.append(j - 1)
            out += data[p:p + j]
            p += j
        else:
            _, length, dist = ch
            out.append(0x80 | (length - MATCH_MIN))
            out.append(dist & 0xFF)   # 256 -> 0
            p += length
    return bytes(out)


def pack_block_greedy(data: bytes) -> bytes:
    """Greedy longest-match parse (test baseline for the DP)."""
    n = len(data)
    matches = _longest_matches(data)
    out = bytearray()
    lit = bytearray()

    def flush():
        while lit:
            take = min(LIT_MAX, len(lit))
            out.append(take - 1)
            out.extend(lit[:take])
            del lit[:take]

    p = 0
    while p < n:
        mlen, mdist = matches[p]
        if mlen >= MATCH_MIN:
            flush()
            out.append(0x80 | (mlen - MATCH_MIN))
            out.append(mdist & 0xFF)
            p += mlen
        else:
            lit.append(data[p])
            p += 1
    flush()
    return bytes(out)


def unpack_block(blob: bytes, ofs: int, out_len: int):
    """Reference block decoder. Returns (data, end_ofs)."""
    out = bytearray()
    while len(out) < out_len:
        hdr = blob[ofs]
        ofs += 1
        if hdr & 0x80:
            length = (hdr & 0x7F) + MATCH_MIN
            dist = blob[ofs] or 256
            ofs += 1
            if dist > len(out):
                raise ValueError(f"match distance {dist} reaches before "
                                 f"block start (pos {len(out)})")
            for _ in range(length):
                out.append(out[-dist])
        else:
            length = hdr + 1
            out += blob[ofs:ofs + length]
            ofs += length
    if len(out) != out_len:
        raise ValueError(f"block overruns: {len(out)} > {out_len}")
    return bytes(out), ofs


# --------------------------------------------------------------------------
# Whole-file pack / unpack
# --------------------------------------------------------------------------
def _runtime_nodes(packed: bytes, out_len: int) -> int:
    """Return the proposed runtime node count for one packed lane block."""
    ofs = 0
    produced = 0
    nodes = 0
    while produced < out_len:
        hdr = packed[ofs]
        ofs += 1
        if hdr & 0x80:
            length = (hdr & 0x7F) + MATCH_MIN
            ofs += 1
            nodes += length + 3
        else:
            length = hdr + 1
            ofs += length
            nodes += 2 * length + 1
        produced += length
    assert produced == out_len
    assert ofs == len(packed)
    return nodes


def interlace_block(lanes) -> tuple[bytes, list[int], list[int]]:
    """Pack independent lane blocks, then interlace bytes by consumption.

    Returns (interlaced bytes, per-lane packed sizes, pair-start offsets).
    Each new packet emits its header and first literal/distance together.
    Later literal bytes emit when their lane is visited; active matches
    consume no packed bytes.
    """
    assert len(lanes) == NLANES
    n = len(lanes[0])
    assert all(len(lane) == n for lane in lanes)
    if n == 0:
        return b"", [0] * NLANES, []

    packed = [pack_block(bytes(lane)) for lane in lanes]
    offsets = [0] * NLANES
    remaining = [0] * NLANES
    literals = [False] * NLANES
    body = bytearray()
    pair_starts = []

    for _frame in range(n):
        for k in range(NLANES):
            stream = packed[k]
            if remaining[k] == 0:
                hdr = stream[offsets[k]]
                aux = stream[offsets[k] + 1]
                offsets[k] += 2
                pair_starts.append(len(body))
                body += bytes((hdr, aux))
                if hdr & 0x80:
                    length = (hdr & 0x7F) + MATCH_MIN
                    literals[k] = False
                else:
                    length = hdr + 1
                    literals[k] = True
                remaining[k] = length - 1
            else:
                if literals[k]:
                    body.append(stream[offsets[k]])
                    offsets[k] += 1
                remaining[k] -= 1

    assert all(rem == 0 for rem in remaining)
    assert offsets == [len(stream) for stream in packed]
    return bytes(body), [len(stream) for stream in packed], pair_starts


def unpack_interlaced_block(blob: bytes, ofs: int, frame_count: int):
    """Reference-decode one interlaced block. Return (lanes, end offset)."""
    lanes = [bytearray() for _ in range(NLANES)]
    remaining = [0] * NLANES
    literals = [False] * NLANES
    distances = [0] * NLANES

    for _frame in range(frame_count):
        for k in range(NLANES):
            if remaining[k] == 0:
                hdr = blob[ofs]
                aux = blob[ofs + 1]
                ofs += 2
                if hdr & 0x80:
                    length = (hdr & 0x7F) + MATCH_MIN
                    dist = aux or WINDOW
                    if dist > len(lanes[k]):
                        raise ValueError(
                            f"lane {k}: match distance {dist} reaches before "
                            f"block start (pos {len(lanes[k])})")
                    literals[k] = False
                    distances[k] = dist
                    lanes[k].append(lanes[k][-dist])
                else:
                    length = hdr + 1
                    literals[k] = True
                    lanes[k].append(aux)
                remaining[k] = length - 1
            else:
                if literals[k]:
                    lanes[k].append(blob[ofs])
                    ofs += 1
                else:
                    lanes[k].append(lanes[k][-distances[k]])
                remaining[k] -= 1

    if any(remaining):
        raise ValueError("interlaced block ends inside a lane packet")
    return [bytes(lane) for lane in lanes], ofs


def _bank_pair_pad(pair_starts, data_off: int) -> int:
    """Return pad so no packet pair starts on the last byte of a 16 KiB bank."""
    for pad in range(BANK_PAD_MAX + 1):
        base = data_off + pad
        if all((base + start) % BANK_SIZE != BANK_SIZE - 1 for start in pair_starts):
            return pad
    raise ValueError("cannot shift packet pairs off the last byte of a bank")


def _pack_layout(lanes, loop_frame: int):
    """Return (blob, lane sizes, prefix bytes) for 20 equal-length lanes."""
    assert len(lanes) == NLANES
    n = len(lanes[0])
    assert all(len(lane) == n for lane in lanes)
    if n == 0:
        raise ValueError("refusing zero-frame pack: Depack.Init prefills "
                         "frames and would read past an empty blob")
    if not 0 <= loop_frame < n:
        raise ValueError(f"loop_frame {loop_frame} outside 0..{n - 1}")

    prefix, prefix_sizes, prefix_starts = interlace_block(
        [bytes(lane[:loop_frame]) for lane in lanes])
    loop, loop_sizes, loop_starts = interlace_block(
        [bytes(lane[loop_frame:]) for lane in lanes])
    pair_starts = list(prefix_starts)
    pair_starts.extend(len(prefix) + start for start in loop_starts)
    pad = _bank_pair_pad(pair_starts, HEADER_SIZE)
    data_off = HEADER_SIZE + pad
    loop_ofs = data_off + len(prefix)
    hdr = struct.pack("<HHHH", n, loop_frame, loop_ofs, data_off)
    assert len(hdr) == HEADER_SIZE
    lane_sizes = [a + b for a, b in zip(prefix_sizes, loop_sizes)]
    return hdr + bytes(pad) + prefix + loop, lane_sizes, len(prefix), data_off


def pack(lanes, loop_frame: int) -> bytes:
    """Pack 20 independent lanes into one interlaced wire blob."""
    return _pack_layout(lanes, loop_frame)[0]


def unpack(blob: bytes):
    """Reference file decoder. Returns (lanes, loop_frame)."""
    n, loop_frame, loop_ofs, data_off = struct.unpack_from("<HHHH", blob, 0)
    if not HEADER_SIZE <= data_off <= loop_ofs <= len(blob):
        raise ValueError(f"data/loop offsets {data_off}/{loop_ofs} outside packed blob")
    if loop_frame:
        prefix, end = unpack_interlaced_block(blob, data_off, loop_frame)
        if end != loop_ofs:
            raise ValueError(f"prefix ends at {end}, loop starts at {loop_ofs}")
    else:
        prefix = [b""] * NLANES
        if loop_ofs != data_off:
            raise ValueError(f"zero-length prefix has loop offset {loop_ofs}")
    loop, end = unpack_interlaced_block(blob, loop_ofs, n - loop_frame)
    if end != len(blob):
        raise ValueError(f"packed block leaves {len(blob) - end} trailing bytes")
    return [prefix[k] + loop[k] for k in range(NLANES)], loop_frame


# --------------------------------------------------------------------------
def pack_streams(psg: bytes, events: bytes, loop_frame: int = 0):
    """Full pipeline: stream pair -> (blob, stats dict). Event lanes pick the
    smaller fill policy per lane; the choice is invisible to the decoder."""
    lanes, entry_mask = build_lanes(psg, events)
    source_lanes = lanes
    lanes, duty_mask = encode_duty_deltas(lanes, loop_frame)
    fills = []
    final = [bytes(l) for l in lanes[:16]]
    for k in range(16, NLANES):
        meaningful = duty_mask if k == 17 else entry_mask
        zero = fill_lane(lanes[k], meaningful, "zero")
        rept = fill_lane(lanes[k], meaningful, "repeat")
        # Cost of a lane = its two independent packed blocks. Prefer fewer
        # runtime nodes when both fill policies use the same number of bytes.
        def cost(d):
            prefix = pack_block(d[:loop_frame])
            loop = pack_block(d[loop_frame:])
            return (len(prefix) + len(loop),
                    _runtime_nodes(prefix, loop_frame)
                    + _runtime_nodes(loop, len(d) - loop_frame))
        if cost(rept) < cost(zero):
            final.append(rept)
            fills.append("repeat")
        else:
            final.append(zero)
            fills.append("zero")
    blob, lane_sizes, prefix_bytes, data_off = _pack_layout(final, loop_frame)

    # verify: reference depack must reproduce every lane byte
    out_lanes, out_loop = unpack(blob)
    assert out_loop == loop_frame
    for k in range(NLANES):
        assert out_lanes[k] == final[k], f"lane {k} round-trip mismatch"
    restored = decode_duty_deltas(out_lanes, loop_frame)
    assert restored[14] == source_lanes[14]
    for frame, meaningful in enumerate(duty_mask):
        if meaningful:
            assert restored[17][frame] == source_lanes[17][frame]

    n = len(final[0])
    stats = {
        "frames": n,
        "loop_frame": loop_frame,
        "raw_bytes": n * NLANES,
        "packed_bytes": len(blob),
        "bytes_per_frame": len(blob) / n if n else 0.0,
        "lane_sizes": lane_sizes,
        "prefix_bytes": prefix_bytes,
        "data_offset": data_off,
        "loop_bytes": len(blob) - data_off - prefix_bytes,
        "event_fills": fills,
    }
    return blob, stats


def main():
    ap = argparse.ArgumentParser(description="Pack unpacked AYMax 1.2 data")
    ap.add_argument("stem", help="input and output stem")
    ap.add_argument("--loop-frame", type=int, default=0)
    ap.add_argument("--dir-asm", default="src/gen/asset_dir.g.asm",
                     help="output path for the sample/wavetable directory asm")
    args = ap.parse_args()

    stem = Path(args.stem)
    psg_path = stem.parent / f"{stem.name}_psg.bin"
    events_path = stem.parent / f"{stem.name}_events.bin"
    samples_path = stem.parent / f"{stem.name}_samples.bin"
    wavetables_path = stem.parent / f"{stem.name}_wavetables.bin"
    pack_path = stem.parent / f"{stem.name}.pack"
    assets_path = stem.parent / f"{stem.name}_assets.bin"
    dir_path = Path(args.dir_asm)
    dir_inc_path = stem.parent / f"{stem.name}_dir.inc"

    try:
        psg = psg_path.read_bytes()
        events = events_path.read_bytes()
        samples = parse_samples(samples_path.read_bytes())
        wavetables = parse_wavetables(wavetables_path.read_bytes())
        validate_asset_indexes(psg, events, samples, wavetables)
        blob, st = pack_streams(psg, events, args.loop_frame)
        asset_banks = AssetBankSet(samples, wavetables)
        assets_blob = asset_banks.build()
    except (OSError, ValueError) as e:
        ap.error(str(e))

    if len(blob) > MAX_PACK_SIZE:
        ap.error(f"packed blob {len(blob)} B exceeds "
                 f"{MAX_PACK_BANKS} x {BANK_SIZE} B banks")

    pack_path.parent.mkdir(parents=True, exist_ok=True)
    dir_path.parent.mkdir(parents=True, exist_ok=True)
    pack_path.write_bytes(blob)
    assets_path.write_bytes(assets_blob)
    asset_banks.write_dir_asm(dir_path)
    asset_banks.write_dir_inc(dir_inc_path)

    raw16 = st["frames"] * 16
    print(f"{pack_path.name}: {st['frames']} frames, "
          f"loop {st['loop_frame']} -> {st['packed_bytes']} B "
          f"({st['bytes_per_frame']:.2f} B/frame, "
          f"{st['raw_bytes'] / st['packed_bytes']:.1f}x vs 20 B/frame, "
          f"{raw16 / st['packed_bytes']:.1f}x vs 16 B records)")
    names = [f"R{r}" for r in (0, 1, 2, 3, 4, 5, 6, 11, 12, 13, 7, 8, 9, 10)]
    names += ["ctlL", "ctlH", "evB", "evDuty", "evTlo", "evThi"]
    per = "  ".join(f"{nm}={sz}" for nm, sz in zip(names, st["lane_sizes"]))
    print(f"  logical lanes: {per}")
    print(f"  interlaced blocks: data_off={st['data_offset']}  "
          f"prefix={st['prefix_bytes']}  loop={st['loop_bytes']}")
    banks = (st["packed_bytes"] + BANK_SIZE - 1) // BANK_SIZE
    print(f"  pack banks: {banks} / {MAX_PACK_BANKS}")
    print(f"  event lane fills: {', '.join(st['event_fills'])}")
    usage = ", ".join(
        f"bank {bank}={used} B"
        for bank, used in zip((4, 6), asset_banks.bank_used)
        if used)
    bank_word = "bank" if asset_banks.bank_count == 1 else "banks"
    print(f"  assets: {len(samples)} samples, {len(wavetables)} wavetables, "
          f"{sum(asset_banks.bank_used)} B used in "
          f"{asset_banks.bank_count} {bank_word} ({usage}) -> {assets_path}")


if __name__ == "__main__":
    main()
