"""Tests for the Atarin -> TAYM converter. Plain asserts; run directly.

  python3 -B tests/test_atarin2taym.py

The Atarin snapshot is not distributed: set ATARIN_SNA to its path. Tests
that need it are skipped otherwise. Checks that the TAYM validates,
round-trips, and carries SID/DUTY/SAMPLE timer events.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "taym" / "python" / "src"))

from atarin2ays import DEFAULT_SNA  # noqa: E402

SKIP = DEFAULT_SNA is None or not DEFAULT_SNA.exists()


def test_background_uses_atarin_mixer_masks():
    from atarin2taym import _background_frames
    from taym import spec
    from taym.model import Mods

    def frame(r9, period=0):
        regs = bytearray(14)
        regs[2] = period
        regs[9] = r9
        return regs

    frames = [
        frame(0x2F, 41),  # SID leaves the mixer unchanged
        frame(0x4F, 41),  # duty disables tone B
        frame(0x85),      # sample disables tone and noise B
        frame(0),         # sample remains active between trigger and STOP
        frame(0x2F, 41),  # SID clears the preceding sample mask
        frame(0),
    ]
    mods = [
        Mods(command=spec.CMD_START),
        Mods(command=spec.CMD_START),
        Mods(command=spec.CMD_START),
        Mods(command=spec.CMD_EMPTY),
        Mods(command=spec.CMD_START),
        Mods(command=spec.CMD_STOP),
    ]
    assert [r[7] for r in _background_frames(frames, mods)] == [0, 0x02, 0x12, 0x12, 0, 0]


def test_convert_validates_and_round_trips():
    if SKIP:
        print("  (skipped: set ATARIN_SNA)")
        return
    from atarin2taym import ATARIN_CLOCK, CPU_HZ, FRAME_RATE, convert, DEFAULT_SNA
    from taym import spec, read_taym, write_taym, validate
    taym = convert(DEFAULT_SNA, "main")
    assert validate(taym) == []
    assert len(taym.timers) == 1
    assert len(taym.mods) == taym.trak.frame_count
    assert taym.chips[0].chip_type_id == spec.CHIP_TYPE_AY
    assert taym.chips[0].clock_hz == 1_750_000
    assert ATARIN_CLOCK * 2 == CPU_HZ == 3_500_000
    assert abs(taym.trak.frame_rate_hz - FRAME_RATE) < 1e-5
    assert taym.frame_data["PSG0"][:4] == b"PSG\x1a"
    cmds = {m.command for m in taym.mods}
    assert spec.CMD_START in cmds and spec.CMD_STOP in cmds
    assert any(m.command == spec.CMD_START for m in taym.mods)
    # SID, DUTY, and SAMPLE all bind R9.
    targets = {a.target_id for a in taym.actions}
    assert 0x09 in targets
    data = write_taym(taym)
    assert write_taym(read_taym(data)) == data
    assert (ROOT / "examples" / "atarin.taym").read_bytes() == data


def test_sid_duty_sample_edges_present():
    if SKIP:
        print("  (skipped: set ATARIN_SNA)")
        return
    from atarin2taym import (
        SAMPLE_RATE, convert, decode_sample, load_banks, read_ptr_table, DEFAULT_SNA,
    )
    from taym import spec
    taym = convert(DEFAULT_SNA, "main")
    banks = load_banks(DEFAULT_SNA)
    source_samples = {
        tuple(decode_sample(banks[3], ptr)): i
        for i, ptr in enumerate(read_ptr_table(banks[2]))
    }
    starts = [i for i, m in enumerate(taym.mods) if m.command == spec.CMD_START]
    assert len(starts) >= 10
    # Sample STARTs bind a no-loop R9 lane of AY volume codes; squares loop [vol,0].
    sample_starts = 0
    sample_indices = set()
    square_starts = 0
    for i in starts:
        m = taym.mods[i]
        acts = taym.actions[m.first_action:m.first_action + m.action_count]
        assert all(a.target_id == 0x09 for a in acts)
        a = acts[0]
        assert a.source_mode == spec.SRC_BIND_LANE
        lane = taym.lanes[a.operand]
        if lane.loop_index == spec.NO_LOOP:
            sample_starts += 1
            assert lane.length > 1
            # Raw AY volume codes, not c*17 linear expansion.
            vals = taym.vu08[lane.value_offset:lane.value_offset + lane.length]
            assert max(vals) <= 15
            assert tuple(vals) in source_samples
            sample_indices.add(source_samples[tuple(vals)])
            assert abs(spec.from_fix16(m.base_timer_value) - SAMPLE_RATE) < 1e-5
        else:
            square_starts += 1
    assert sample_starts > 0
    assert {5, 6} <= sample_indices
    assert square_starts > 0
    dutyish = sum(1 for i in starts
                  if taym.mods[i].timer_lane_ref not in
                  (spec.TLAN_NONE, spec.TLAN_UNCHANGED)
                  and taym.tlanes[taym.mods[i].timer_lane_ref].length == 2)
    assert dutyish > 0


def test_sid_duty_underflow_and_sample_stop():
    if SKIP:
        print("  (skipped: set ATARIN_SNA)")
        return
    import math
    from atarin2taym import (
        CPU_HZ, DUTY_BUDGET_SUB, SAMPLE_RATE, FRAME_RATE,
        _sid_params, _duty_params, convert, DEFAULT_SNA,
    )
    from taym import spec
    assert abs(SAMPLE_RATE - 500_000 / 83) < 1e-12
    # P=23: 16*23=368 < 644 → interval 65536+368, not 368.
    regs = [0] * 14
    regs[2], regs[3], regs[9] = 23, 0, 0x2F
    _, _, rate = _sid_params(regs)
    assert abs(rate - CPU_HZ / (65536 + 368)) < 1e-6
    regs[2], regs[3] = 41, 0
    _, _, rate41 = _sid_params(regs)
    assert abs(rate41 - CPU_HZ / (16 * 41)) < 1e-6
    # P=22 B=4: ta=440 < 780 → wrapped first interval.
    regs[2], regs[3], regs[9] = 22, (4 << 4), 0x4F
    *_, fa, fb = _duty_params(regs)
    wrapped = (440 - DUTY_BUDGET_SUB) & 0xFFFF
    assert abs(fa - CPU_HZ / wrapped) < 1e-6
    assert abs(fb - CPU_HZ / ((16 - 4) * 22)) < 1e-6

    taym = convert(DEFAULT_SNA, "main")
    # Sample lifetime: STOP at start+ceil(N*fps/rate), unless a new START replaces earlier.
    for i, m in enumerate(taym.mods):
        if m.command != spec.CMD_START:
            continue
        a = taym.actions[m.first_action]
        lane = taym.lanes[a.operand]
        if lane.loop_index != spec.NO_LOOP:
            continue
        dur = max(1, math.ceil(lane.length * FRAME_RATE / SAMPLE_RATE))
        end = i + dur
        if end >= len(taym.mods):
            continue                   # truncated by end of track
        nxt = next((j for j in range(i + 1, end + 1)
                    if taym.mods[j].command in (spec.CMD_STOP, spec.CMD_START)), None)
        assert nxt is not None, f"sample at {i}: no STOP/START by {end}"
        if taym.mods[nxt].command == spec.CMD_STOP:
            assert nxt <= end, f"sample at {i}: STOP at {nxt} after {end}"
    # Review example: frame-2415 voice sample ends at 2452 on Pentagon timing.
    assert taym.mods[2415].command == spec.CMD_START
    assert taym.mods[2452].command == spec.CMD_STOP


def test_sample_pitch_survives_aymax_pack():
    """Atarin sample codes stay native-rate in the DDS sample bank blob."""
    if SKIP:
        print("  (skipped: set ATARIN_SNA)")
        return
    import math

    from atarin2taym import FRAME_RATE, SAMPLE_RATE, convert, DEFAULT_SNA
    from aymax_fx import EV_START, K_DDS_SAMPLE
    from aymax_assets import DDS_SAMPLE_GUARD_CODES
    import taym2aymax
    import psgpack
    from taym import spec

    taym = convert(DEFAULT_SNA, "main")
    frame_limit = min(
        taym.trak.frame_count,
        len(taym2aymax.parse_psg(taym.frame_data["PSG0"])),
    )
    if frame_limit >= 16:
        frame_limit -= frame_limit % 16
    table = taym2aymax.SampleTable()
    rows = [row for _rec, _entry, row in taym2aymax.convert(
        taym, lambda _msg: None, sample_table=table,
        frame_limit=frame_limit,
    )]
    compiled = set(table)

    checked = set()
    timer_count = len(taym.timers)
    for row in rows:
        if row["event"] != EV_START or row["kernel"] != K_DDS_SAMPLE:
            continue
        m = taym.mods[row["frame"] * timer_count]
        assert m.command == spec.CMD_START
        lane = taym.lanes[taym.actions[m.first_action].operand]
        assert lane.loop_index == spec.NO_LOOP
        source = tuple(taym.vu08[lane.value_offset:lane.value_offset + lane.length])
        duration_frames = max(1, math.ceil(len(source) * FRAME_RATE / SAMPLE_RATE))
        boundary_codes = math.ceil(duration_frames * SAMPLE_RATE / FRAME_RATE)
        padded_count = (max(len(source), boundary_codes)
                        + DDS_SAMPLE_GUARD_CODES)
        cooked = source + (0,) * (padded_count - len(source))
        assert cooked in compiled
        assert cooked[:len(source)] == source
        assert not any(cooked[len(source):])
        assert len(cooked) == padded_count
        assert len(cooked) < len(taym2aymax.upsample_sample_codes(
            source, SAMPLE_RATE, taym2aymax.QUANT_HZ))
        checked.add(cooked)

    assert checked == compiled
    assets = psgpack.AssetBankSet(list(table), [])
    blob = assets.build()
    for key, offset, count in zip(table, assets.sample_offsets, assets.sample_sizes):
        start = (offset & 0x7FFF) + (16384 if offset & 0x8000 else 0)
        packed = blob[start:start + count]
        unpacked = tuple(n for byte in packed for n in (byte >> 4, byte & 15))
        assert unpacked[:len(key)] == key
        assert not any(unpacked[len(key):])
        assert count == len(packed) == math.ceil(len(key) / 2)


def test_ays_export():
    import struct
    import tempfile
    from atarin2ays import write_ays

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sample.ays"
        for codes in ([1, 2, 15], [1, 2, 15, 3]):
            for nibble in (False, True):
                write_ays(path, codes, 6000, nibble)
                data = path.read_bytes()
                assert struct.unpack("<4sBBBBHIH", data[:16]) == (
                    b"AYS1", 1, 1, int(nibble), 0, 6000, len(codes), 0)
                if nibble:
                    expected = b"\x21\x0f" if len(codes) == 3 else b"\x21\x3f"
                    assert data[16:] == expected
                else:
                    assert data[16:] == bytes(codes)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
