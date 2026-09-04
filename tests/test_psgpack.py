"""Tests for the 20-lane PSG packer. Plain asserts; run directly.

  .venv311/bin/python tests/test_psgpack.py
"""
import random
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import psgpack  # noqa: E402


def test_constants():
    assert psgpack.NLANES == 20
    assert psgpack.HEADER_SIZE == 8
    assert psgpack.LIT_MAX == 128
    assert psgpack.MATCH_MIN == 3
    assert psgpack.MATCH_MAX == 130
    assert psgpack.WINDOW == 256
    print("ok  constants")


def test_unpacked_asset_parsers():
    raw_samples = bytes([3, 0, 1, 2, 3, 1, 0, 9])
    assert psgpack.parse_samples(raw_samples) == [bytes([1, 2, 3]), bytes([9])]
    assert psgpack.parse_samples(b"") == []
    assert psgpack.pack_sample_nibbles([0x0A, 0x05, 0x03]) == bytes([0xA5, 0x30])

    wave = bytes(range(256))
    assert psgpack.parse_wavetables(wave * 2) == [wave, wave]
    assert psgpack.parse_wavetables(b"") == []

    bad_samples = [b"\x01", b"\x00\x00", b"\x02\x00\x01", b"\x01\x00\x10"]
    for data in bad_samples:
        try:
            psgpack.parse_samples(data)
        except ValueError:
            pass
        else:
            assert False, f"malformed sample data accepted: {data!r}"
    try:
        psgpack.parse_samples(bytes([1, 0, 0]) * 256)
    except ValueError as e:
        assert "255" in str(e)
    else:
        assert False, "256 samples accepted"
    for data in (b"\x00", bytes(256 * 256)):
        try:
            psgpack.parse_wavetables(data)
        except ValueError:
            pass
        else:
            assert False, f"malformed wavetable data accepted: {len(data)} B"
    print("ok  unpacked asset parsers")


def test_asset_bank_layout():
    samples = [bytes([1, 2, 3, 4]), bytes([9, 8, 7])]
    assets = psgpack.AssetBankSet(samples, [])
    blob = assets.build()
    assert blob[0] == 2
    assert int.from_bytes(blob[4:6], "little") == 2
    assert len(blob) == psgpack.ASSET_BANK_SIZE
    assert assets.bank_count == 1
    assert assets.bank_kinds == [1, 0]
    assert assets.bank_first == [
        psgpack.SAMPLE_HEADER + 2 * psgpack.SAMPLE_DIR_ENTRY, 0]
    assert assets.bank_used[1] == 0
    assert len(assets.sample_offsets) == 2

    spill = psgpack.AssetBankSet(
        [bytes([1]) * 32000, bytes([2]) * 1000], [])
    spill_blob = spill.build()
    assert len(spill_blob) == 2 * psgpack.ASSET_BANK_SIZE
    assert spill.bank_count == 2
    assert spill.sample_offsets[0] < 0x4000
    assert spill.sample_offsets[1] & 0x8000

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp) / "asset_dir.g.asm"
        assets.write_dir_asm(directory)
        text = directory.read_text()
        assert "SharedBankCount equ 1" in text
        assert "AssetKind0 equ 1" in text
        assert "AssetKind1 equ 0" in text
        assert f"AssetFirst0 equ {assets.bank_first[0]}" in text
        spill.write_dir_asm(directory)
        assert "SharedBankCount equ 2" in directory.read_text()
    print("ok  sample asset bank layout")


def test_empty_assets_and_wavetable_placement():
    empty = psgpack.AssetBankSet([], [])
    blob = empty.build()
    assert len(blob) == psgpack.ASSET_BANK_SIZE
    assert blob[:2] == b"\x00\x00"
    assert empty.bank_count == 1
    assert empty.bank_kinds == [0, 0]
    assert empty.bank_first == empty.bank_used == [psgpack.SAMPLE_HEADER, 0]

    # Wavetables always go first, in bank 0 (hardware bank 4) at offset 256,
    # right after the header page. A sample that doesn't fit what's left of
    # bank 0 spills to bank 1 (hardware bank 6).
    waves = [bytes([value]) * 256 for value in (1, 2)]
    assets = psgpack.AssetBankSet([bytes([1]) * 32000], waves)
    blob = assets.build()
    assert len(blob) == 2 * psgpack.ASSET_BANK_SIZE
    assert assets.wavetable_bank == 4
    assert assets.wavetable_base == 256
    assert blob[256:256 + 256 * len(waves)] == b"".join(waves)
    assert assets.sample_offsets == [0x8000]
    assert assets.bank_used == [256 + 256 * len(waves), 16000]
    assert assets.bank_kinds == [2, 1]
    assert assets.bank_first == [256, 0]

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp) / "asset_dir.g.asm"
        empty.write_dir_asm(directory)
        assert "dw 6, 1" in directory.read_text()
        assets.write_dir_asm(directory)
        text = directory.read_text()
        assert "WavetableBank equ 4" in text
        assert "WavetablePage equ 193" in text
        assert "WavetableDirTable" not in text
    print("ok  empty assets and wavetable placement")


def test_write_dir_inc():
    assets = psgpack.AssetBankSet([bytes([1]) * 32000], [bytes([1]) * 256])
    assets.build()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "atarized_dir.inc"
        assets.write_dir_inc(path)
        text = path.read_text()
        assert f"AssetBankCount equ {assets.bank_count}" in text
        assert f"AssetUsed0 equ {assets.bank_used[0]}" in text
        assert f"AssetUsed1 equ {assets.bank_used[1]}" in text
        assert "SampleDirTable" not in text
    print("ok  write_dir_inc")


def _roundtrip_block(data: bytes):
    packed = psgpack.pack_block(data)
    out, end = psgpack.unpack_block(packed, 0, len(data))
    assert out == data
    assert end == len(packed)
    # also with a non-zero ofs prefix
    prefix = b"\xaa\xbb"
    out2, end2 = psgpack.unpack_block(prefix + packed, len(prefix), len(data))
    assert out2 == data
    assert end2 == len(prefix) + len(packed)


def test_block_roundtrip():
    cases = []
    cases.append(b"")                                          # empty
    cases.append(bytes([7] * 64))                               # constant run
    cases.append(bytes([1, 2] * 80))                            # period-2
    rng = random.Random(1)
    alphabet = bytes([3, 7, 11, 19, 23])
    cases.append(bytes(rng.choice(alphabet) for _ in range(300)))
    # long repeated slice at distance > 128
    head = bytes(range(140))
    mid = head[:40]                                             # dist 140 > 128
    cases.append(head + mid + bytes([9, 8, 7, 6]))

    for data in cases:
        _roundtrip_block(data)
    print("ok  block roundtrip")


def test_distance_edges():
    # distance 1: one literal then match len 130 dist 1 -> 131 equal bytes
    a = 0x5A
    pkt = bytes([0x00, a, 0x80 | (130 - 3), 1])
    out, end = psgpack.unpack_block(pkt, 0, 131)
    assert out == bytes([a] * 131)
    assert end == len(pkt)

    # distance 255: 255 distinct literals (128 + 127), then match len 3 dist 255
    lit = bytes(range(255))
    pkt = bytes([127]) + lit[:128] + bytes([126]) + lit[128:] + bytes([0x80, 255])
    out, end = psgpack.unpack_block(pkt, 0, 258)
    assert out == lit + lit[0:3]
    assert end == len(pkt)

    # distance 256 (dist byte 0): 256 literals then match len 3
    lit256 = bytes((i * 3 + 1) & 0xFF for i in range(256))
    pkt = (bytes([127]) + lit256[:128]
           + bytes([127]) + lit256[128:]
           + bytes([0x80, 0]))
    out, end = psgpack.unpack_block(pkt, 0, 259)
    assert out == lit256 + lit256[0:3]
    assert end == len(pkt)
    print("ok  distance edges")


def test_match_before_block_start_rejected():
    # first packet is a match with nothing produced yet
    pkt = bytes([0x80, 1])   # match len 3, dist 1
    try:
        psgpack.unpack_block(pkt, 0, 3)
    except ValueError:
        pass
    else:
        assert False, "expected ValueError for match before block start"
    print("ok  match before block start rejected")


def _structured_sequences():
    """~20 seeded random and structured sequences for packer comparison."""
    seqs = []
    # structured
    seqs.append(b"")
    seqs.append(b"\x00")
    seqs.append(b"\xff" * 17)
    seqs.append(bytes([1, 2] * 100))
    seqs.append(bytes(range(256)))
    seqs.append(bytes(range(256)) + bytes(range(50)))
    seqs.append(b"ABCD" * 200)
    seqs.append(bytes([0] * 50 + [1] * 50 + [0] * 50))
    # seeded random, mixed alphabets and lengths
    for seed, alphabet, n in [
        (2, 4, 1), (3, 4, 3), (4, 8, 17), (5, 8, 128),
        (6, 16, 256), (7, 16, 400), (8, 32, 512), (9, 64, 1000),
        (10, 128, 1500), (11, 256, 2000), (12, 3, 300),
        (13, 5, 800),
    ]:
        rng = random.Random(seed)
        seqs.append(bytes(rng.randrange(alphabet) for _ in range(n)))
    assert len(seqs) >= 20
    return seqs


def test_optimal_not_worse_than_greedy():
    for data in _structured_sequences():
        opt = psgpack.pack_block(data)
        greedy = psgpack.pack_block_greedy(data)
        assert len(opt) <= len(greedy), (
            f"optimal {len(opt)} > greedy {len(greedy)} for len={len(data)}"
        )
        # sanity: both decode to the same data
        if data:
            assert psgpack.unpack_block(opt, 0, len(data))[0] == data
            assert psgpack.unpack_block(greedy, 0, len(data))[0] == data
    print("ok  optimal not worse than greedy")


def _no_match_sequence(n: int, seed: int = 99) -> bytes:
    """Build n bytes with no 3-byte match inside the WINDOW lookback."""
    rng = random.Random(seed)
    out = bytearray()
    # start from a permutation of 0..255 then extend carefully
    base = list(range(256))
    rng.shuffle(base)
    out.extend(base)
    while len(out) < n:
        placed = False
        for _ in range(10000):
            b = rng.randrange(256)
            trial = out + bytes([b])
            bad = False
            # only newly completed 3-byte keys can introduce a match
            p0 = max(0, len(trial) - 3)
            for p in range(p0, len(trial) - 2):
                key = bytes(trial[p:p + 3])
                q0 = max(0, p - psgpack.WINDOW)
                for q in range(q0, p):
                    if bytes(trial[q:q + 3]) == key:
                        bad = True
                        break
                if bad:
                    break
            if not bad:
                out.append(b)
                placed = True
                break
        assert placed, f"could not extend no-match sequence at {len(out)}"
    return bytes(out[:n])


def test_lit_max_boundary():
    data = _no_match_sequence(400)
    # confirm the builder really forbids matches
    for p in range(len(data) - 2):
        key = data[p:p + 3]
        for q in range(max(0, p - psgpack.WINDOW), p):
            assert data[q:q + 3] != key

    packed = psgpack.pack_block(data)
    out, end = psgpack.unpack_block(packed, 0, len(data))
    assert out == data
    assert end == len(packed)

    # walk packets: every header must be a literal with L-1 <= 127
    ofs = 0
    produced = 0
    while produced < len(data):
        hdr = packed[ofs]
        ofs += 1
        assert (hdr & 0x80) == 0, f"unexpected match at ofs {ofs - 1}"
        assert hdr <= 127
        length = hdr + 1
        assert 1 <= length <= psgpack.LIT_MAX
        ofs += length
        produced += length
    assert ofs == len(packed)
    print("ok  lit max boundary")


def test_reject_zero_frame_pack():
    lanes = [b"" for _ in range(psgpack.NLANES)]
    try:
        psgpack.pack(lanes, 0)
        assert False, "zero-frame pack must raise"
    except ValueError as e:
        assert "zero-frame" in str(e)
    print("ok  reject zero-frame pack")


def test_file_roundtrip_with_loop():
    n, loop_frame = 300, 100
    rng = random.Random(42)
    lanes = []
    for k in range(psgpack.NLANES):
        lanes.append(bytes(rng.randrange(16) for _ in range(n)))

    blob = psgpack.pack(lanes, loop_frame)
    out_lanes, out_loop = psgpack.unpack(blob)
    assert out_loop == loop_frame
    assert out_lanes == list(lanes)

    frame_count, hdr_loop, loop_ofs, data_off = struct.unpack_from("<HHHH", blob, 0)
    assert frame_count == n and hdr_loop == loop_frame
    prefix, prefix_end = psgpack.unpack_interlaced_block(
        blob, data_off, loop_frame)
    loop, loop_end = psgpack.unpack_interlaced_block(
        blob, loop_ofs, n - loop_frame)
    assert prefix_end == loop_ofs
    assert loop_end == len(blob)
    for k in range(psgpack.NLANES):
        assert prefix[k] == lanes[k][:loop_frame]
        assert loop[k] == lanes[k][loop_frame:]
    print("ok  file roundtrip with loop")


def test_physical_interlace_order():
    lanes = [bytes((k, k + 20, k + 40, k + 60))
             for k in range(psgpack.NLANES)]
    blob = psgpack.pack(lanes, 0)
    n, loop_frame, loop_ofs, data_off = struct.unpack_from("<HHHH", blob, 0)
    assert (n, loop_frame, loop_ofs, data_off) == (4, 0, data_off, data_off)
    assert data_off >= psgpack.HEADER_SIZE

    expected = bytearray()
    for k in range(psgpack.NLANES):
        expected += bytes((3, lanes[k][0]))       # literal length 4 + first byte
    for frame in range(1, 4):
        expected += bytes(lane[frame] for lane in lanes)
    assert blob[data_off:] == bytes(expected)
    assert psgpack.unpack(blob) == (lanes, 0)
    print("ok  physical interlace order")


def test_frame_starts_skip_bank_tail():
    # random (near-incompressible) lanes, long enough to cross a 16 KiB
    # bank boundary at least once.
    n = 1300
    rng = random.Random(9)
    lanes = [bytes(rng.randrange(256) for _ in range(n))
             for _ in range(psgpack.NLANES)]
    loop_frame = 400
    blob = psgpack.pack(lanes, loop_frame)
    _n, hdr_loop, loop_ofs, data_off = struct.unpack_from("<HHHH", blob, 0)
    assert hdr_loop == loop_frame
    assert data_off == psgpack.HEADER_SIZE
    assert len(blob) > psgpack.BANK_SIZE, "test needs a bank crossing"

    def walk(ofs, frames):
        """Replay frame starts like the depacker: skip bank tails, never
        let a frame's bytes cross a 0x4000 boundary."""
        remaining = [0] * psgpack.NLANES
        literals = [False] * psgpack.NLANES
        starts = []
        for _ in range(frames):
            if ofs & (psgpack.BANK_SIZE - 1) >= psgpack.BANK_SIZE - 256:
                ofs += (-ofs) % psgpack.BANK_SIZE
            starts.append(ofs)
            frame_start_bank = ofs // psgpack.BANK_SIZE
            for k in range(psgpack.NLANES):
                if remaining[k] == 0:
                    hdr = blob[ofs]
                    ofs += 2
                    if hdr & 0x80:
                        remaining[k] = (hdr & 0x7F) + psgpack.MATCH_MIN - 1
                        literals[k] = False
                    else:
                        remaining[k] = hdr
                        literals[k] = True
                else:
                    if literals[k]:
                        ofs += 1
                    remaining[k] -= 1
            assert (ofs - 1) // psgpack.BANK_SIZE == frame_start_bank, (
                "frame crossed a bank boundary")
        return ofs, starts

    end1, starts1 = walk(data_off, loop_frame)
    assert end1 == loop_ofs
    end2, starts2 = walk(loop_ofs, n - loop_frame)
    assert end2 == len(blob)

    for ofs in starts1 + starts2:
        assert (ofs & (psgpack.BANK_SIZE - 1)) < psgpack.BANK_SIZE - 256
    # loop_ofs itself may sit in a bank tail; walk() skips it like the runtime.

    out_lanes, out_loop = psgpack.unpack(blob)
    assert out_loop == loop_frame
    assert out_lanes == lanes
    print("ok  frame starts skip bank tail")


def _synthetic_six_frames():
    """Six 16-byte records; frames 1 and 4 carry START/MODIFY entries."""
    records = []
    for f in range(6):
        rec = bytearray(range(f, f + 16))
        # clear low 2 bits of byte 15, then set event code
        rec[15] = (rec[15] & ~3)
        if f == 1:
            rec[15] |= 1          # EV_START
        elif f == 4:
            rec[15] |= 2          # EV_MODIFY
        elif f in (0, 2):
            rec[15] |= 0          # empty
        else:
            rec[15] |= 3          # stop / other
        records.append(bytes(rec))
    psg = b"".join(records)
    # two recognizable 4-byte entries
    events = bytes([0xB1, 0xD1, 0x71, 0x81, 0xB2, 0xD2, 0x72, 0x82])
    return psg, events, records


def test_build_lanes_and_fills():
    psg, events, records = _synthetic_six_frames()
    lanes, entry_mask = psgpack.build_lanes(psg, events)

    assert len(lanes) == psgpack.NLANES
    assert len(entry_mask) == 6
    assert entry_mask == [False, True, False, False, True, False]

    for k in range(16):
        col = bytes(records[f][k] for f in range(6))
        assert bytes(lanes[k]) == col

    # event lanes: entry bytes at frames 1 and 4, zeros elsewhere
    e0 = events[0:4]
    e1 = events[4:8]
    for j in range(4):
        expected = bytearray(6)
        expected[1] = e0[j]
        expected[4] = e1[j]
        assert bytes(lanes[16 + j]) == bytes(expected)

    # fill policies
    for j in range(4):
        zero = psgpack.fill_lane(lanes[16 + j], entry_mask, "zero")
        rept = psgpack.fill_lane(lanes[16 + j], entry_mask, "repeat")
        assert zero == bytes(lanes[16 + j])          # already zeros in don't-care
        # repeat: hold previous entry (0 before first)
        assert rept[0] == 0
        assert rept[1] == e0[j]
        assert rept[2] == e0[j] and rept[3] == e0[j]
        assert rept[4] == e1[j]
        assert rept[5] == e1[j]
    print("ok  build_lanes and fills")


def test_asset_indexes_are_validated():
    def event(kernel, index):
        record = bytearray(16)
        record[14] = (kernel << 4) | 8
        record[15] = psgpack.EV_START
        return bytes(record), bytes([index, 0, 1, 0])

    psg, events = event(psgpack.K_SAMPLE, 0)
    psgpack.validate_asset_indexes(psg, events, [b"\x01"], [])
    psg, events = event(psgpack.K_WAVETABLE, 0)
    psgpack.validate_asset_indexes(psg, events, [], [bytes(256)])

    for kernel in (psgpack.K_SAMPLE, psgpack.K_DDS_SAMPLE,
                   psgpack.K_WAVETABLE):
        psg, events = event(kernel, 0)
        try:
            psgpack.validate_asset_indexes(psg, events, [], [])
        except ValueError as e:
            assert "index 0" in str(e)
        else:
            assert False, f"kernel {kernel} accepted a missing asset"
    print("ok  asset indexes are validated")


def test_pack_streams_end_to_end():
    psg6, events6, _ = _synthetic_six_frames()
    # repeat to 300 frames (50 x 6), keep event-code/entry consistency
    reps = 50
    psg = psg6 * reps
    events = events6 * reps
    assert len(psg) == 300 * 16
    assert len(events) == 100 * 4                   # 2 entries per 6 frames * 50

    lanes, entry_mask = psgpack.build_lanes(psg, events)
    blob, stats = psgpack.pack_streams(psg, events, loop_frame=0)
    out_lanes, out_loop = psgpack.unpack(blob)
    assert out_loop == 0
    restored = psgpack.decode_duty_deltas(out_lanes, out_loop)

    for k in range(16):
        assert restored[k] == lanes[k]

    for k in (16, 18, 19):
        for f, has in enumerate(entry_mask):
            if has:
                assert out_lanes[k][f] == lanes[k][f]
    for f in range(len(entry_mask)):
        if psgpack._is_duty_event(lanes, f):
            assert restored[17][f] == lanes[17][f]

    assert stats["packed_bytes"] == len(blob)
    assert stats["frames"] == 300
    assert stats["raw_bytes"] == 300 * psgpack.NLANES
    assert 0 < stats["bytes_per_frame"] <= psgpack.NLANES + psgpack.HEADER_SIZE
    print("ok  pack_streams end to end")


def test_duty_delta_roundtrip_with_loop():
    n, loop_frame = 9, 4
    records = [bytearray(16) for _ in range(n)]
    entries = bytearray()

    def event(frame, code, kernel, duty):
        records[frame][14] = (kernel << 4) | 8
        records[frame][15] = code
        entries.extend((0x55, duty, 0x34, 0x12))

    event(0, psgpack.EV_START, psgpack.K_DDS_DUTY, 10)
    event(2, psgpack.EV_MODIFY, psgpack.K_DDS_DUTY, 20)
    event(4, psgpack.EV_START, psgpack.K_DDS_EDGE, 200)
    event(6, psgpack.EV_MODIFY, psgpack.K_DDS_EDGE, 3)
    event(7, psgpack.EV_START, 0, 99)  # countdown does not consume duty

    psg = b"".join(records)
    lanes, _ = psgpack.build_lanes(psg, bytes(entries))
    blob, _ = psgpack.pack_streams(psg, bytes(entries), loop_frame)
    packed_lanes, packed_loop = psgpack.unpack(blob)

    assert packed_loop == loop_frame
    assert packed_lanes[14][0] & 0x80
    assert not packed_lanes[14][2] & 0x80
    assert packed_lanes[14][4] & 0x80
    assert not packed_lanes[14][6] & 0x80
    assert [packed_lanes[17][f] for f in (0, 2, 4, 6)] == [10, 10, 200, 59]

    restored = psgpack.decode_duty_deltas(packed_lanes, packed_loop)
    assert restored[14] == lanes[14]
    for f in (0, 2, 4, 6):
        assert restored[17][f] == lanes[17][f]
    print("ok  duty delta roundtrip with loop")


def test_bassline_real():
    psg_path = ROOT / "build" / "bassline_psg.bin"
    ev_path = ROOT / "build" / "bassline_events.bin"
    if not psg_path.exists() or not ev_path.exists():
        print("skipped  bassline_real (build/bassline_*.bin missing)")
        return
    psg = psg_path.read_bytes()
    events = ev_path.read_bytes()
    blob, stats = psgpack.pack_streams(psg, events, loop_frame=0)
    assert len(blob) <= psgpack.MAX_PACK_SIZE
    assert stats["frames"] == len(psg) // 16
    assert stats["packed_bytes"] == len(blob)
    # unpack again and check register lanes equal build_lanes columns
    lanes, entry_mask = psgpack.build_lanes(psg, events)
    out_lanes, _ = psgpack.unpack(blob)
    restored = psgpack.decode_duty_deltas(out_lanes, 0)
    for k in range(16):
        assert restored[k] == lanes[k]
    for k in (16, 18, 19):
        for f, has in enumerate(entry_mask):
            if has:
                assert out_lanes[k][f] == lanes[k][f]
    for f in range(len(entry_mask)):
        if psgpack._is_duty_event(lanes, f):
            assert restored[17][f] == lanes[17][f]
    print(f"ok  bassline_real ({stats['packed_bytes']} B, "
          f"{stats['frames']} frames)")


if __name__ == "__main__":
    for t in [
        test_constants,
        test_unpacked_asset_parsers,
        test_asset_bank_layout,
        test_empty_assets_and_wavetable_placement,
        test_write_dir_inc,
        test_block_roundtrip,
        test_distance_edges,
        test_match_before_block_start_rejected,
        test_optimal_not_worse_than_greedy,
        test_lit_max_boundary,
        test_reject_zero_frame_pack,
        test_file_roundtrip_with_loop,
        test_physical_interlace_order,
        test_frame_starts_skip_bank_tail,
        test_build_lanes_and_fills,
        test_asset_indexes_are_validated,
        test_pack_streams_end_to_end,
        test_duty_delta_roundtrip_with_loop,
        test_bassline_real,
    ]:
        t()
    print("all psgpack tests passed")
