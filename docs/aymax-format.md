# AYMax Packed Track Format

This document describes the data AYMax ships to the ZX Spectrum 128K
runtime: the packed stream (`.pack`), the shared asset banks (`_assets.bin`),
the small include file (`_dir.inc`), and the unpacked intermediate files
that sit between `taym2aymax.py` and `psgpack.py`.

The document is data-only. It does not describe the Z80 depacker, register
allocation, or playback timing. See `docs/psg-packer.md` and
`docs/v1-2-spec.md` for that. All multi-byte fields are little-endian
unless stated otherwise.

## 1. Overview

The pipeline has two stages:

```text
SONG.taym --taym2aymax.py--> unpacked files --psgpack.py--> packed files

  <stem>_psg.bin
  <stem>_events.bin        --psgpack.py <stem>-->  <stem>.pack
  <stem>_samples.bin                               <stem>_assets.bin
  <stem>_wavetables.bin                            <stem>_dir.inc
                                                     + asset directory asm
```

`taym2aymax.py` converts a TAYM interchange file (from Bitphase or another
TAYM exporter) into four unpacked files. `psgpack.py` reads those four files
and writes the packed stream, the packed asset banks, an asm asset
directory, and `_dir.inc`. A third tool, `psg2aymax.py`, produces the same
four unpacked files from a Bulba `.PSG` dump instead of a `.taym` file;
downstream, `psgpack.py` does not care which converter produced its input.

Memory placement at runtime:

- `.pack` loads at `#C000`, one 16 KiB image per bank, starting at hardware
  bank 0, then bank 1.
- `_assets.bin` loads at `#C000` in bank 4, then bank 6 if it overflows.

## 2. Unpacked intermediate files

These files are the contract between the converters and the packer: plain,
headerless, independent of any packed representation.

### 2.1 `<stem>_psg.bin`

One 16-byte record per frame, records back to back, no header.

| Bytes | Field | Meaning |
|---|---|---|
| 0..13 | PSG bytes | AY registers in player write order (see order below) |
| 14..15 | `control16` | little-endian control word |

Player write order for bytes 0..13: `R0 R1 R2 R3 R4 R5 R6 R11 R12 R13 R7 R8
R9 R10`. Each byte is the hardware-effective, masked value for that
register.

`control16` layout:

```text
low byte (bits 7:0):
  bits 3:0  target register, 0..13
  bits 6:4  kernel id, 0..5
  bit 7     duty-lane value at this frame is absolute (see 4.3)

high byte (bits 7:0):
  bits 1:0  event id: 0 EMPTY, 1 START, 2 MODIFY, 3 STOP
  bits 7:2  zero
```

Canonical EMPTY is `#0000`. Canonical STOP has zero target and kernel bits
(only the event bits are set). At most one event fires per frame.

### 2.2 `<stem>_events.bin`

One 4-byte entry for every frame whose `control16` event is START or
MODIFY, in frame order, no header. EMPTY and STOP frames add no entry, so
the event count in this file is less than or equal to the frame count.

| Bytes | Field | Meaning |
|---|---|---|
| 0 | B/index | second kernel value, or a sample/wavetable table index |
| 1 | duty8 | DDS duty threshold (K-DDS-DUTY / K-DDS-EDGE only) |
| 2..3 | timer16 | little-endian countdown reload or DDS increment |

Unused fields for a given kernel are zero.

### 2.3 `<stem>_samples.bin`

Repeated blocks, one per sample, in table order:

| Bytes | Field | Meaning |
|---|---|---|
| 0..1 | `code_count` | little-endian u16, number of codes that follow |
| 2..2+code_count-1 | codes | one byte per code, each in 0..15 |

A sample table entry cannot be empty (`code_count` must be nonzero). Blocks
are not aligned; readers must walk the length-prefixed sequence from the
start. File order defines the sample index used by `_events.bin` entries
(0-based, first block is index 0).

### 2.4 `<stem>_wavetables.bin`

Consecutive 256-byte tables, no header, no length prefix. Table `i` occupies
bytes `256*i .. 256*i+255`. File order defines the wavetable index used by
`_events.bin` entries. The file size must be a multiple of 256.

## 3. Kernels and events

### 3.1 Events

| Id | Name | Reads parameter lanes | Meaning |
|---:|---|---|---|
| 0 | EMPTY | no | keep the active kernel state |
| 1 | START | yes | start or retrigger a kernel on a target |
| 2 | MODIFY | yes | update parameters of the active kernel; same target/kernel required |
| 3 | STOP | no | stop the active kernel and release its target |

Every frame carries exactly one event. At most one START or MODIFY entry
exists per frame; a frame can never hold two.

### 3.2 Kernels

Valid kernel/target pairs (`PLAYER_MAPPINGS` in `taym2aymax.py`):

| Id | Name | Valid targets |
|---:|---|---|
| 0 | `K-COUNTDOWN` | R8, R9, R10, R13 |
| 1 | `K-DDS-DUTY` | R8, R9, R10 (not R13: every write retriggers the envelope) |
| 2 | `K-DDS-EDGE` | R13 |
| 3 | `K-SAMPLE` | R8, R9, R10 |
| 4 | `K-WAVETABLE` | R0..R12 |
| 5 | `K-DDS-SAMPLE` | R8, R9, R10 |

Any other kernel/target pair on a START or MODIFY entry is rejected. MODIFY
is also rejected for `K-SAMPLE` and `K-DDS-SAMPLE` (sample and DDS-sample
playback cannot be modified mid-stream, only started and stopped).

START claims a target and (re)initializes the kernel: phase, sample
position, wavetable phase, and countdown side all reset. MODIFY updates
parameters (A, B, duty8, timer16, table/sample index as applicable) while
preserving the running phase or position. STOP releases the target; the
normal PSG register byte for that target resumes from the `_psg.bin` record
on the next frame.

## 4. Packed stream: `<stem>.pack`

`psgpack.py` reinterprets each 16-byte PSG record plus its optional 4-byte
event entry as one 20-byte logical frame column, splits it into 20
independent byte lanes, LZ-packs each lane, and interlaces the packed bytes
in the order the runtime consumes them.

### 4.1 Lane assignment

| Lane | Source | Content |
|---:|---|---|
| 0..13 | `_psg.bin` bytes 0..13 | PSG registers, player write order |
| 14 | `_psg.bin` byte 14 | control16 low byte |
| 15 | `_psg.bin` byte 15 | control16 high byte |
| 16 | `_events.bin` byte 0 | B / sample index / wavetable index |
| 17 | `_events.bin` byte 1 | duty8 (delta-coded, see 4.3) |
| 18 | `_events.bin` byte 2 | timer16 low byte |
| 19 | `_events.bin` byte 3 | timer16 high byte |

Lanes 16..19 are event lanes: a frame without a START or MODIFY entry has no
real value for them. The packer fills each such frame with either 0 or the
previous meaningful value, whichever packs smaller; a reader that only
needs to reproduce `_psg.bin`/`_events.bin` can ignore these don't-care
positions.

### 4.2 Packet encoding

Each lane is packed independently as a sequence of packets:

```text
0LLLLLLL            literal packet: L+1 (1..128) literal bytes follow
1LLLLLLL dddddddd   match packet:   copy L+3 (3..130) bytes from history;
                     one distance byte follows
```

Distance is in lane bytes (frames), not physical bytes: byte value 0 means
distance 256, other values mean 1..255. A match cannot read before the
start of the current block (prefix or loop, see 4.4). History is capped at
256 bytes (the runtime ring size), so distance never exceeds 256 even if
more history exists. The packer picks the packet parse that minimizes
encoded bytes (any valid parse decodes correctly; this only affects which
one is chosen).

### 4.3 Duty lane delta encoding

Lane 17 (duty8) does not store raw values. Within each block (prefix or
loop, independently), it stores modulo-256 deltas between successive
`K-DDS-DUTY` / `K-DDS-EDGE` START/MODIFY events:

- The first duty event in a block stores its value as-is (delta from an
  implicit previous value of 0), and sets control16 low-byte bit 7 on that
  frame.
- Every later duty event in the same block stores `(value - previous) &
  0xFF`, with bit 7 clear.
- Frames that are not a `K-DDS-DUTY`/`K-DDS-EDGE` START or MODIFY carry a
  don't-care byte in lane 17 (see 4.1) and bit 7 clear.

A reader reconstructs the absolute duty value by starting from 0 at each
block boundary, using the stored byte as an absolute value when bit 7 is
set, and otherwise adding the stored byte (mod 256) to the running value.
Kernels other than `K-DDS-DUTY`/`K-DDS-EDGE` never read lane 17.

### 4.4 Interlacing, prefix/loop blocks

All 20 lanes are packed as two independent sets of blocks: a prefix block
(frames `0 .. loop_frame-1`) and a loop block (frames `loop_frame ..
frame_count-1`). Each lane's prefix and loop packets are two separate,
independent LZ streams; a match never crosses the prefix/loop boundary.

Within a block, the packed bytes of all 20 lanes interlace in strict frame
order: for each frame, in lane order 0..19, a finished lane packet emits its
next header and first literal/distance byte; an active literal packet emits
its next literal byte; an active match emits nothing. Header+first-byte
pairs and continuation literals are interleaved by this per-lane, per-frame
consumption order, not grouped by lane.

### 4.5 Header and blob layout

```text
+0  u16 frame_count
+2  u16 loop_frame
+4  u16 loop_offset      byte offset (from blob start) of the loop block
+6  u16 data_offset      byte offset (from blob start) of the first byte
                         of the interlaced prefix block, always 8
+8  interlaced prefix block, then interlaced loop block
```

A frame's interlaced bytes must never start at an offset whose low 14 bits
are `>= 0x3F00` (the last 256 bytes of a bank). A frame consumes at most 40
bytes (20 lanes x at most 2 bytes each), so a frame that starts before that
point never crosses a bank boundary. When the next frame would start in the
last 256 bytes of a bank, the packer emits zero pad bytes up to the next
multiple of `0x4000` and starts the frame at the bank start instead. The
rule applies at every frame start, including the first frame of the loop
block. `loop_offset` may point at such a pad: the runtime bank check at
frame start skips it. `data_offset` is always 8.

### 4.6 Bank placement and size cap

The blob (header + prefix block + loop block, including any frame-start
pad) must fit in `2 * 16384 = 32768` bytes. It maps onto consecutive 16 KiB
pages at `#C000`, starting at hardware bank 0: bytes `0 .. 16383` in bank 0,
bytes `16384 .. 32767` in bank 1 (if present). A one-bank track occupies
only bank 0.

`frame_count` must be at least 1; a zero-frame pack is rejected.
`loop_frame` must satisfy `0 <= loop_frame < frame_count`.

## 5. Asset banks: `<stem>_assets.bin`

`_assets.bin` holds packed samples and canonical wavetables, shared by every
kernel that references them. It loads at `#C000` in hardware bank 4, and,
if it overflows, continues in bank 6. File size is 16384 bytes (one bank)
or 32768 bytes (two banks); each present bank is padded to a full 16 KiB
image.

### 5.1 Header page (first 256 bytes of bank 4 only)

| Bytes | Field | Meaning |
|---|---|---|
| 0 | count | number of samples, 0..63 |
| 1 | reserved | unused, always 0 |
| 2.. | entries | one 4-byte entry per sample, in sample-table order |

Each entry:

| Bytes | Field | Meaning |
|---|---|---|
| 0..1 | offset | u16; bit 15 set means bank 6, clear means bank 4; remaining 15 bits are the byte offset from `#C000` in that bank |
| 2..3 | count | packed byte count for this sample (nibble-packed size, not code count) |

At most 63 samples fit the header page (`2 + 63*4 = 254 <= 256`).

### 5.2 Wavetables

Wavetables are stored first, as one contiguous, 256-byte-aligned block
starting at byte offset 256 in bank 4 (right after the header page).
Wavetable `i` occupies bytes `256 + 256*i .. 256 + 256*i + 255`. All
wavetables for a track must fit in bank 4 alongside the header; they never
spill into bank 6. An event's wavetable index is added directly to the base
page (offset 256 = page `#C1`), so a reader does not need the header
entries to find wavetables.

### 5.3 Samples

Samples follow the wavetable block in bank 4, packed two 4-bit AY volume
codes per byte, high nibble first: for codes `c[0], c[1], c[2], ...`, byte
`i` is `(c[2*i] << 4) | c[2*i+1]` (0 for a missing final code on an
odd-length sample). Each sample is one contiguous run of packed bytes; a
sample never splits across bank 4 and bank 6 (the packer places it entirely
in whichever bank has room, preferring bank 4). If assets do not fit in
banks 4 and 6 combined, the packer fails.

## 6. `<stem>_dir.inc`

A generated equ-only include for the public player wrapper, written next to
the asset directory asm:

```asm
; AUTO-GENERATED by psgpack
AssetBankCount equ <1 or 2>
AssetUsed0 equ <bytes used in bank 4, 0..16384>
AssetUsed1 equ <bytes used in bank 6, 0..16384>
```

`AssetBankCount` tells the wrapper how many asset banks to load/page in (1
if only bank 4 is used, 2 if bank 6 also holds data). `AssetUsed0` and
`AssetUsed1` give the exact used byte count per bank, so a loader can copy
only the live portion of `_assets.bin` instead of the full padded image.

`psgpack.py` also writes a separate, longer asset directory asm (default
path `src/gen/asset_dir.g.asm`, overridable with `--dir-asm`) with the full
`SampleDirTable`, `WavetableBank`/`WavetablePage`, and per-bank
kind/first-offset equs for the AYMax engine build. That file is a generated
build artifact for this repository's own player, not part of the portable
data contract, so it is not detailed here.

## 7. Limits summary

| Quantity | Limit |
|---|---|
| Frame record size (`_psg.bin`) | 16 bytes |
| Event entry size (`_events.bin`) | 4 bytes |
| Frame count | >= 1 |
| Loop frame | `0 <= loop_frame < frame_count` |
| Sample codes per sample | 1..65535, each code 0..15 |
| Sample table entries | <= 63 (directory page cap); parser itself allows up to 255 |
| Wavetable size | exactly 256 bytes |
| Wavetable table entries | up to 255 |
| Literal packet run | 1..128 bytes |
| Match packet run | 3..130 bytes |
| Match distance | 1..256 frames (lane bytes) |
| Packed stream (`.pack`) size | <= 32768 bytes (2 x 16 KiB banks) |
| Frame start offset (low 14 bits) | `< 0x3F00`; padded to the next bank start otherwise |
| Asset image (`_assets.bin`) size | 16384 or 32768 bytes |
| Wavetable block placement | bank 4 only, byte offset 256, must fit whole |
| Sample placement | one bank only per sample (bank 4 or bank 6) |
| Kernel ids | 0..5 |
| Target register ids | 0..13 |
