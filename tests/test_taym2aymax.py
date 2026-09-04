"""Tests for the TAYM -> AYMax 1.2 converter. Plain asserts; run directly.

  python3 tests/test_taym2aymax.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "taym" / "python" / "src"))

import aymax_assets  # noqa: E402
import taym2aymax  # noqa: E402
from aymax_fx import (  # noqa: E402
    EV_EMPTY, EV_START, EV_MODIFY, EV_STOP, K_DDS_EDGE, K_COUNTDOWN, K_DDS_DUTY,
    K_SAMPLE, K_WAVETABLE, K_DDS_SAMPLE, KERNEL_NAMES, control16,
)
from taym import spec, write_taym, read_taym  # noqa: E402
from taym.model import Actn, Chip, Lane, Mods, Taym, Timr, Tlan, Trak  # noqa: E402


def _no_warn(_):
    pass


def _streams(gen):
    """Collect the output of a convert() generator into (records, events, rows)."""
    records, events, rows = bytearray(), bytearray(), []
    for rec, entry, row in gen:
        records += rec
        if entry is not None:
            events += entry
        rows.append(row)
    return bytes(records), bytes(events), rows


# --- synthetic TAYM model -> AYMax (no Bitphase front-end at all) ----------
def _hand_buzz_taym():
    """Build a 4-frame TAYM with one R13 buzz timer.

    Frame 0: START on a single-shape lane (shape A=0x0D -> 50% duty).
    Frame 1: START on a two-period (duty) lane. Frames 2-3: EMPTY (inherit)."""
    fa = 265.0                                   # approx. bassline buzz fundamental
    pa = spec.to_fix16(fa)
    vu32 = [pa, spec.to_fix16(fa * 0.9), spec.to_fix16(fa * 1.1)]
    tlanes = [Tlan(timing_mode=spec.TM_ABSOLUTE, value_offset=0, length=1, loop_index=0),
              Tlan(timing_mode=spec.TM_ABSOLUTE, value_offset=1, length=2, loop_index=0)]
    vu08 = [0x0D, 0x09]                           # R13 shapes A, B
    lanes = [Lane(value_type=spec.VT_U8, value_offset=0, length=1, loop_index=0),
             Lane(value_type=spec.VT_U8, value_offset=0, length=2, loop_index=0)]
    actions = [Actn(target_id=13, source_mode=spec.SRC_BIND_LANE, operand=0),
               Actn(target_id=13, source_mode=spec.SRC_BIND_LANE, operand=1)]
    timers = [Timr(chip_index=0, clock_mode=spec.CLOCK_ABS_RATE_HZ, clock_divider=0)]
    mods = [Mods(command=spec.CMD_START, base_timer_value=pa, timer_lane_ref=0,
                 first_action=0, action_count=1),
            Mods(command=spec.CMD_START, base_timer_value=pa, timer_lane_ref=1,
                 first_action=1, action_count=1),
            Mods(command=spec.CMD_EMPTY),
            Mods(command=spec.CMD_EMPTY)]
    psg = bytearray(b"PSG\x1a" + bytes(12))
    for _ in range(4):
        psg += b"\xff"
    psg += b"\xfd"
    chip = Chip(clock_hz=1773400, chip_type_id=spec.CHIP_TYPE_AY, name="AY",
                frame_data_tag="PSG0", variant=spec.AY_VARIANT_AY,
                config=spec.AY_LAYOUT_MONO)
    trak = Trak(frame_rate_hz=50.0, frame_count=4, loop_frame=spec.NO_LOOP)
    return Taym(trak=trak, chips=[chip], timers=timers, mods=mods, actions=actions,
                lanes=lanes, tlanes=tlanes, vu08=vu08, vu32=vu32,
                frame_data={"PSG0": bytes(psg)})


def test_synthetic_buzz_events():
    """A hand-built TAYM converts directly (no Bitphase front-end).

    Frame 0 is a single-period R13 buzz -> K-COUNTDOWN START. Frame 1 changes
    to a two-period (duty) R13 chain. That is a new identity -> K-DDS-EDGE
    START (a retrigger). Frames 2-3 are EMPTY and inherit with no parameter
    change, so they emit no event entry."""
    taym = read_taym(write_taym(_hand_buzz_taym()))
    _rec, _ev, rows = _streams(taym2aymax.convert(taym, _no_warn))
    assert rows[0]["event"] == EV_START
    assert rows[0]["kernel"] == K_COUNTDOWN and rows[0]["target"] == 13
    assert rows[0]["value_a"] == 0x0D
    assert rows[1]["event"] == EV_START and rows[1]["kernel"] == K_DDS_EDGE
    assert rows[2]["event"] not in (EV_START, EV_MODIFY)
    assert rows[2]["ev_timer"] is None
    print("ok  synthetic buzz COUNTDOWN START -> DDS-EDGE START -> EMPTY")


def test_tlan_none_start_uses_base_timer_value():
    """A START with no TLAN is valid TAYM: the base timer value is the rate."""
    taym = _hand_buzz_taym()
    taym.mods[0].timer_lane_ref = spec.TLAN_NONE
    taym = read_taym(write_taym(taym))
    _rec, _ev, rows = _streams(taym2aymax.convert(taym, _no_warn))
    assert rows[0]["event"] == EV_START
    assert rows[0]["kernel"] == K_COUNTDOWN
    assert abs(rows[0]["freq"] - 265.0) < 0.01, rows[0]["freq"]
    assert rows[0]["ev_timer"] is not None and rows[0]["ev_timer"] > 0
    print("ok  TLAN_NONE START uses base_timer_value")


def test_tlan_none_modulate_keeps_base_timer_value():
    taym = _hand_buzz_taym()
    taym.mods[1] = Mods(command=spec.CMD_MODULATE,
                        base_timer_value=spec.to_fix16(300.0),
                        timer_lane_ref=spec.TLAN_NONE)
    taym = read_taym(write_taym(taym))
    _rec, _ev, rows = _streams(taym2aymax.convert(taym, _no_warn))
    assert rows[1]["event"] == EV_MODIFY
    assert rows[1]["kernel"] == K_COUNTDOWN
    assert rows[1]["ev_timer"] > 0
    print("ok  TLAN_NONE MODULATE keeps base_timer_value")


def test_chip_period_tlan_uses_period_units():
    taym = _hand_buzz_taym()
    taym.timers[0] = Timr(
        chip_index=0, clock_mode=spec.CLOCK_CHIP_PERIOD, clock_divider=8)
    taym.vu32 = [222, 222, 222]
    taym.mods[0].base_timer_value = 222
    taym.mods[1].base_timer_value = 222
    taym = read_taym(write_taym(taym))
    _rec, _ev, rows = _streams(taym2aymax.convert(taym, _no_warn))
    assert abs(rows[0]["freq"] - 998.536036) < 0.0001
    assert rows[0]["ev_timer"] == 1662
    assert not rows[0]["clamped"]
    print("ok  CHIP_PERIOD TLAN uses chip period units")


def test_bound_volume_lane_supplies_value_a():
    taym = _hand_buzz_taym()
    taym.actions = [
        Actn(target_id=8, source_mode=spec.SRC_BIND_LANE, operand=0),
        Actn(target_id=8, source_mode=spec.SRC_BIND_LANE, operand=2),
    ]
    taym.vu08 = [13, 0, 9, 0]
    taym.lanes[0] = Lane(
        value_type=spec.VT_U8, value_offset=0, length=2, loop_index=0)
    taym.lanes.append(
        Lane(value_type=spec.VT_U8, value_offset=2, length=2, loop_index=0))
    taym.mods[1] = Mods(
        command=spec.CMD_MODULATE, timer_lane_ref=spec.TLAN_UNCHANGED,
        first_action=1, action_count=1)
    taym = read_taym(write_taym(taym))
    records, _ev, rows = _streams(taym2aymax.convert(taym, _no_warn))
    assert rows[0]["value_a"] == 13 and records[11] == 13
    assert rows[1]["event"] == EV_MODIFY
    assert rows[1]["value_a"] == 9 and records[16 + 11] == 9
    print("ok  bound volume lane supplies value A")


def test_tone_period_timer_uses_low_register_only():
    """A timer-owned 12-bit tone period keeps its first value in PSG."""
    taym = _hand_buzz_taym()
    low = [220, 232, 220, 208]
    high = [1, 1, 1, 1]
    rate = spec.to_fix16(265.0)
    taym.vu08 = low + high
    taym.vu32 = [rate] * 4
    taym.lanes = [
        Lane(value_type=spec.VT_U8, value_offset=0, length=4, loop_index=0),
        Lane(value_type=spec.VT_U8, value_offset=4, length=4, loop_index=0),
    ]
    taym.tlanes = [
        Tlan(timing_mode=spec.TM_ABSOLUTE, value_offset=0, length=4, loop_index=0),
    ]
    taym.actions = [
        Actn(target_id=4, source_mode=spec.SRC_BIND_LANE, operand=0),
        Actn(target_id=5, source_mode=spec.SRC_BIND_LANE, operand=1),
    ]
    taym.mods = [
        Mods(command=spec.CMD_START, base_timer_value=rate, timer_lane_ref=0,
             first_action=0, action_count=2),
        Mods(command=spec.CMD_EMPTY),
        Mods(command=spec.CMD_STOP),
        Mods(command=spec.CMD_EMPTY),
    ]
    taym = read_taym(write_taym(taym))
    warnings = []
    records, _events, rows = _streams(taym2aymax.convert(
        taym, warnings.append, wavetable_table=aymax_assets.WavetableTable()))
    assert not warnings
    assert rows[0]["event"] == EV_START
    assert rows[0]["kernel"] == K_WAVETABLE and rows[0]["target"] == 4
    assert records[4:6] == bytes([low[0], high[0]])
    assert records[16 + 4:16 + 6] == bytes([low[0], high[0]])
    print("ok  tone period stays in PSG and timer varies only the low register")


def test_repeated_start_same_identity_retriggers():
    """A TAYM START on an active timer replaces and retriggers it, even when
    the parameters are unchanged."""
    taym = _hand_buzz_taym()
    first = taym.mods[0]
    taym.mods[1] = Mods(command=spec.CMD_START,
                        base_timer_value=first.base_timer_value,
                        timer_lane_ref=first.timer_lane_ref,
                        first_action=first.first_action,
                        action_count=first.action_count)
    taym = read_taym(write_taym(taym))
    _rec, ev, rows = _streams(taym2aymax.convert(taym, _no_warn))
    assert rows[0]["event"] == EV_START
    assert rows[1]["event"] == EV_START
    assert rows[1]["start"] is True
    assert len(ev) == 8
    print("ok  repeated START with same identity retriggers")


def test_stop_consumes_no_event_entry():
    """START -> EMPTY -> STOP -> EMPTY consumes only the START entry."""
    taym = _hand_buzz_taym()
    taym.mods = [taym.mods[1], Mods(command=spec.CMD_EMPTY),
                 Mods(command=spec.CMD_STOP), Mods(command=spec.CMD_EMPTY)]
    taym = read_taym(write_taym(taym))
    records, events, rows = _streams(taym2aymax.convert(taym, _no_warn))
    assert [row["event"] for row in rows] == [
        EV_START, EV_EMPTY, EV_STOP, EV_EMPTY,
    ]
    assert len(events) == 4
    assert records[2 * 16 + 14:2 * 16 + 16] == b"\x00\x03"
    taym2aymax.validate_player_rows(rows)
    print("ok  STOP is canonical and consumes no event entry")


def test_player_accepts_countdown_and_edge():
    """K-COUNTDOWN (R13) and K-DDS-EDGE (R13) are live player mappings."""
    taym = read_taym(write_taym(_hand_buzz_taym()))
    _rec, _ev, rows = _streams(taym2aymax.convert(taym, _no_warn))
    taym2aymax.validate_player_rows(rows)
    print("ok  player accepts COUNTDOWN/R13 and DDS-EDGE/R13")


def test_player_accepts_dds_duty_volume_targets():
    rows = [
        {"frame": 0, "event": EV_START, "kernel": K_DDS_DUTY, "target": 8},
        {"frame": 1, "event": EV_MODIFY, "kernel": K_DDS_DUTY, "target": 9},
        {"frame": 2, "event": EV_START, "kernel": K_DDS_DUTY, "target": 10},
        {"frame": 3, "event": EV_EMPTY, "kernel": None, "target": None},
    ]
    taym2aymax.validate_player_rows(rows)
    print("ok  player accepts DDS-DUTY/R8/R9/R10")


def test_player_accepts_countdown_volume_targets():
    rows = [
        {"frame": 0, "event": EV_START, "kernel": K_COUNTDOWN, "target": 8},
        {"frame": 1, "event": EV_MODIFY, "kernel": K_COUNTDOWN, "target": 9},
        {"frame": 2, "event": EV_START, "kernel": K_COUNTDOWN, "target": 10},
        {"frame": 3, "event": EV_STOP, "kernel": None, "target": None},
    ]
    taym2aymax.validate_player_rows(rows)
    print("ok  player accepts COUNTDOWN/R8/R9/R10")


def test_player_accepts_start_on_last_frame():
    """The depacker wrap path reaches PlResume and restores the START splice."""
    rows = [
        {"frame": 0, "event": EV_EMPTY, "kernel": None, "target": None},
        {"frame": 1, "event": EV_START, "kernel": K_SAMPLE, "target": 9},
    ]
    taym2aymax.validate_player_rows(rows)
    print("ok  player accepts START on the loop's last frame")


def test_player_rejects_dds_duty_r13():
    rows = [
        {"frame": 0, "event": EV_START, "kernel": K_DDS_DUTY, "target": 13},
        {"frame": 1, "event": EV_EMPTY, "kernel": None, "target": None},
    ]
    try:
        taym2aymax.validate_player_rows(rows)
    except ValueError as e:
        assert "R13" in str(e)
    else:
        assert False, "K-DDS-DUTY/R13 must be rejected"
    print("ok  player rejects DDS-DUTY/R13")


def test_player_accepts_sample_volume_targets():
    for kernel in (K_SAMPLE, K_DDS_SAMPLE):
        for target in (8, 9, 10):
            rows = [
                {"frame": 0, "event": EV_START,
                 "kernel": kernel, "target": target},
                {"frame": 1, "event": EV_EMPTY,
                 "kernel": None, "target": None},
            ]
            taym2aymax.validate_player_rows(rows)
    print("ok  player accepts SAMPLE and DDS-SAMPLE on R8/R9/R10")


def test_dds_sample_kernel_encoding():
    assert (K_COUNTDOWN, K_DDS_DUTY, K_DDS_EDGE,
            K_SAMPLE, K_WAVETABLE) == (0, 1, 2, 3, 4)
    assert K_DDS_SAMPLE == 5
    assert KERNEL_NAMES[K_DDS_SAMPLE] == "dds_sample"
    assert control16(EV_START, K_DDS_SAMPLE, 9) == 0x0159
    print("ok  DDS-SAMPLE is kernel 5 and START encodes 0x0159")


def test_player_accepts_wavetable_mapping():
    rows = [
        {"frame": 0, "event": EV_START, "kernel": K_WAVETABLE, "target": 9},
        {"frame": 1, "event": EV_EMPTY, "kernel": None, "target": None},
    ]
    taym2aymax.validate_player_rows(rows)
    print("ok  player accepts WAVETABLE/R9")


def test_wavetable_generation_and_dedup():
    values = [14, 14, 15, 14, 14, 14, 4, 13,
              13, 9, 9, 10, 11, 12, 13, 0]
    table = aymax_assets.WavetableTable()
    first = table.intern(values, [100.0] * 16, "test")
    second = table.intern(values, [200.0] * 16, "test")
    assert first == second == 0
    blob = table._entries[0]
    assert len(blob) == 256
    assert blob == b"".join(bytes([value]) * 16 for value in values)
    inc, fundamental = aymax_assets.cook_wavetable_rate(
        [100.0] * 16, 16, "test")
    assert fundamental == 6.25
    assert inc == round(65536 * fundamental / aymax_assets.QUANT_HZ)

    shaped = aymax_assets.WavetableTable()
    shaped.intern([1, 2, 3], [1.0, 2.0, 4.0], "test")
    assert shaped._entries[0] == (
        bytes([1]) * 146 + bytes([2]) * 73 + bytes([3]) * 37)
    print("ok  wavetable generation, timing, and dedup")


def test_swingycat_wavetable_conversion():
    taym = read_taym((ROOT / "tests" / "data" / "swingycat.taym").read_bytes())
    table = aymax_assets.WavetableTable()
    _records, events, rows = _streams(taym2aymax.convert(
        taym, _no_warn, wavetable_table=table))
    counts = {event: sum(row["event"] == event for row in rows)
              for event in (EV_EMPTY, EV_START, EV_MODIFY, EV_STOP)}
    assert counts == {EV_EMPTY: 672, EV_START: 41, EV_MODIFY: 15, EV_STOP: 40}
    assert len(events) == (41 + 15) * 4
    assert all(row["kernel"] == K_WAVETABLE and row["target"] == 8
               for row in rows if row["event"] in (EV_START, EV_MODIFY))
    assert len(table._entries) == 1 and len(table._entries[0]) == 256
    taym2aymax.validate_player_rows(rows)
    print("ok  swingycat WAVETABLE START/MODIFY/STOP and one deduped table")


def test_sample_rate():
    aymax_assets.validate_sample_rate(6000.0, 0)
    src_rate = 500_000 / 83
    cooked = aymax_assets.upsample_sample_codes(
        [1, 2, 3, 4], src_rate, aymax_assets.QUANT_HZ)
    assert cooked[0] == 1 and cooked[-1] == 4 and len(cooked) == 11
    assert aymax_assets.cook_dds_sample_rate(
        aymax_assets.DDS_SAMPLE_HZ, 0) == 0xFFFF
    assert 0 < aymax_assets.cook_dds_sample_rate(6000.0, 0) < 0xFFFF
    for rate in (0, aymax_assets.QUANT_HZ + 1):
        try:
            aymax_assets.validate_sample_rate(rate, 0)
        except ValueError:
            pass
        else:
            assert False, "unsupported SAMPLE rate accepted"
    print("ok  sample rate")


def test_unpacked_asset_serialization():
    samples = aymax_assets.SampleTable()
    assert samples.intern([1, 2, 3]) == 0
    assert samples.intern([1, 2, 3]) == 0
    assert samples.intern([9]) == 1
    assert aymax_assets.serialize_samples(samples) == bytes(
        [3, 0, 1, 2, 3, 1, 0, 9])
    assert samples._entries == [(1, 2, 3), (9,)]

    waves = aymax_assets.WavetableTable()
    waves.intern([1, 2, 3, 4], [100.0] * 4, "test")
    assert aymax_assets.serialize_wavetables(waves) == waves._entries[0]
    assert len(aymax_assets.serialize_wavetables(waves)) == 256
    assert aymax_assets.serialize_samples(aymax_assets.SampleTable()) == b""
    assert aymax_assets.serialize_wavetables(aymax_assets.WavetableTable()) == b""
    print("ok  unpacked sample and wavetable serialization")


def test_sample_table_rejects_256th_entry():
    table = aymax_assets.SampleTable()
    for i in range(255):
        table.intern([i & 15, (i >> 4) & 15, (i >> 8) & 15])
    try:
        table.intern([15, 15, 15, 15])
    except ValueError as e:
        assert "255" in str(e)
    else:
        assert False, "256th sample table entry accepted"
    print("ok  sample table rejects the 256th entry")


def _hand_sample_taym(target=9, rate_hz=6000.0):
    """4-frame TAYM: sample START on one volume register, then STOP."""
    codes = [0x0C, 0x08, 0x04, 0x00, 0x0F]
    rate = spec.to_fix16(rate_hz)
    lanes = [Lane(value_type=spec.VT_U8, value_offset=0, length=len(codes),
                  loop_index=spec.NO_LOOP)]
    actions = [Actn(target_id=target, source_mode=spec.SRC_BIND_LANE, operand=0)]
    timers = [Timr(chip_index=0, clock_mode=spec.CLOCK_ABS_RATE_HZ, clock_divider=0)]
    mods = [Mods(command=spec.CMD_START, base_timer_value=rate,
                 timer_lane_ref=spec.TLAN_NONE, first_action=0, action_count=1),
            Mods(command=spec.CMD_EMPTY),
            Mods(command=spec.CMD_STOP),
            Mods(command=spec.CMD_EMPTY)]
    psg = bytearray(b"PSG\x1a" + bytes(12))
    for _ in range(4):
        psg += b"\xff"
    psg += b"\xfd"
    chip = Chip(clock_hz=1773400, chip_type_id=spec.CHIP_TYPE_AY, name="AY",
                frame_data_tag="PSG0", variant=spec.AY_VARIANT_AY,
                config=spec.AY_LAYOUT_MONO)
    trak = Trak(frame_rate_hz=50.0, frame_count=4, loop_frame=spec.NO_LOOP)
    return Taym(trak=trak, chips=[chip], timers=timers, mods=mods, actions=actions,
                lanes=lanes, tlanes=[], vu08=codes, vu32=[],
                frame_data={"PSG0": bytes(psg)})


def test_sample_classify_and_no_modify():
    taym = read_taym(write_taym(_hand_sample_taym()))
    table = aymax_assets.SampleTable()
    _rec, _ev, rows = _streams(taym2aymax.convert(taym, _no_warn, sample_table=table))
    assert rows[0]["event"] == EV_START
    assert rows[0]["kernel"] == K_DDS_SAMPLE and rows[0]["target"] == 9
    assert rows[0]["ev_b"] == 0
    assert rows[0]["ev_timer"] == aymax_assets.cook_dds_sample_rate(
        6000.0, 0)
    assert rows[1]["event"] == EV_STOP
    assert rows[2]["event"] == EV_EMPTY
    assert table._entries
    codes = table._entries[0]
    assert codes[:5] == (0x0C, 0x08, 0x04, 0x00, 0x0F)
    assert len(codes) == 136 and not any(codes[5:])
    print("ok  DDS sample classify, frame padding, and injected STOP")


def test_sample_volume_target_conversion():
    for target in (8, 9, 10):
        for rate_hz, kernel in ((6000.0, K_DDS_SAMPLE),
                                (8000.0, K_SAMPLE)):
            taym = read_taym(write_taym(
                _hand_sample_taym(target, rate_hz)))
            table = aymax_assets.SampleTable()
            records, events, rows = _streams(taym2aymax.convert(
                taym, _no_warn, sample_table=table))
            assert rows[0]["kernel"] == kernel
            assert rows[0]["target"] == target
            assert int.from_bytes(records[14:16], "little") == control16(
                EV_START, kernel, target)
            assert len(events) == 4
            taym2aymax.validate_player_rows(rows)
    print("ok  SAMPLE and DDS-SAMPLE convert on R8/R9/R10")


def test_virtual_sample_amplitude_and_volume_modulate():
    """0x80 is combined with R9 and its volume change is baked in place."""
    taym = _hand_sample_taym()
    taym.vu08 = [0xFF] * 400
    taym.lanes[0].length = len(taym.vu08)
    taym.actions = [
        Actn(target_id=9, source_mode=spec.SRC_INLINE_VALUE, operand=15),
        Actn(target_id=spec.TGT_SAMPLE_AMPLITUDE,
             source_mode=spec.SRC_BIND_LANE, operand=0),
        Actn(target_id=9, source_mode=spec.SRC_INLINE_VALUE, operand=11),
        Actn(target_id=spec.TGT_SAMPLE_AMPLITUDE,
             source_mode=spec.SRC_BIND_LANE, operand=0),
    ]
    taym.mods = [
        Mods(command=spec.CMD_START, base_timer_value=spec.to_fix16(6000.0),
             timer_lane_ref=spec.TLAN_NONE, first_action=0, action_count=2),
        Mods(command=spec.CMD_MODULATE, timer_lane_ref=spec.TLAN_UNCHANGED,
             first_action=2, action_count=2),
        Mods(command=spec.CMD_START, base_timer_value=spec.to_fix16(6000.0),
             timer_lane_ref=spec.TLAN_NONE, first_action=0, action_count=2),
        Mods(command=spec.CMD_EMPTY),
    ]

    table = aymax_assets.SampleTable()
    _rec, _ev, rows = _streams(taym2aymax.convert(
        taym, _no_warn, sample_table=table))

    assert [r["event"] for r in rows] == [EV_START, EV_EMPTY, EV_START, EV_EMPTY]
    assert all(r["target"] == 9 for r in rows)
    first = table._entries[0]
    second = table._entries[1]
    assert first[:120] == (15,) * 120
    assert first[120:240] == (11,) * 120
    assert not any(first[240:])
    assert second[:240] == (15,) * 240
    assert not any(second[240:])
    print("ok  virtual sample amplitude and baked volume MODULATE")


def test_short_sample_classification():
    table = aymax_assets.SampleTable()
    km = taym2aymax.classify_from_chain(
        [(9, [12, 0], spec.NO_LOOP)], [6000.0], _no_warn,
        where="short sample", sample_table=table)
    assert km.kernel == K_DDS_SAMPLE and km.chain_length == 136
    print("ok  short no-loop volume lane is DDS-SAMPLE")


def test_high_rate_sample_keeps_fixed_rate_kernel():
    table = aymax_assets.SampleTable()
    km = taym2aymax.classify_from_chain(
        [(9, [12, 0], spec.NO_LOOP)], [8000.0], _no_warn,
        where="high-rate sample", sample_table=table)
    assert km.kernel == K_SAMPLE
    assert km.chain_length == 4
    print("ok  high-rate no-loop lane stays fixed-rate SAMPLE")


def test_sample_modulate_is_rejected():
    taym = _hand_sample_taym()
    taym.mods[1] = Mods(command=spec.CMD_MODULATE,
                        base_timer_value=spec.to_fix16(6000.0),
                        timer_lane_ref=spec.TLAN_NONE)
    try:
        _streams(taym2aymax.convert(taym, _no_warn))
    except ValueError as e:
        assert "dds_sample MODULATE" in str(e)
    else:
        assert False, "SAMPLE MODULATE accepted"
    print("ok  SAMPLE MODULATE is rejected")


def test_dds_sample_requires_stop_inside_frame_range():
    taym = _hand_sample_taym()
    taym.vu08 = [1] * 1000
    taym.lanes[0].length = len(taym.vu08)
    taym.mods[1:] = [Mods(command=spec.CMD_EMPTY) for _ in taym.mods[1:]]
    try:
        _streams(taym2aymax.convert(taym, _no_warn))
    except ValueError as e:
        assert "ends before DDS-SAMPLE STOP" in str(e)
    else:
        assert False, "lengthless DDS sample outlived the frame range"
    print("ok  DDS-SAMPLE requires its injected STOP inside the frame range")


def test_frame_limit_excludes_tail_samples():
    taym = _hand_sample_taym()
    taym.mods = [Mods(command=spec.CMD_EMPTY), Mods(command=spec.CMD_EMPTY),
                 taym.mods[0], Mods(command=spec.CMD_EMPTY)]
    table = aymax_assets.SampleTable()
    _streams(taym2aymax.convert(
        taym, _no_warn, sample_table=table, frame_limit=2))
    assert not table._entries
    print("ok  frame limit excludes tail samples")


def _psg_only_taym(reg_writes):
    """TAYM with no timers. reg_writes is one {reg: value} dict per frame."""
    psg = bytearray(b"PSG\x1a" + bytes(12))
    for wr in reg_writes:
        psg += b"\xff"
        for reg, value in wr.items():
            psg += bytes((reg, value))
    psg += b"\xfd"
    n = len(reg_writes)
    chip = Chip(clock_hz=1773400, chip_type_id=spec.CHIP_TYPE_AY, name="AY",
                frame_data_tag="PSG0", variant=spec.AY_VARIANT_AY,
                config=spec.AY_LAYOUT_MONO)
    trak = Trak(frame_rate_hz=50.0, frame_count=n, loop_frame=spec.NO_LOOP)
    return Taym(trak=trak, chips=[chip], timers=[], mods=[],
                frame_data={"PSG0": bytes(psg)})


def _record_r13(records, frame):
    rec = records[frame * 16:(frame + 1) * 16]
    return rec[9]  # RECORD_ORDER index of R13


def test_r13_retriggers_when_env_bit_rises():
    """Same envelope shape, env bit off then on: rewrite R13 so AY retriggers."""
    taym = read_taym(write_taym(_psg_only_taym([
        {9: 0x1F, 13: 0x0C},   # env on, shape C
        {9: 0x1F},             # same shape omitted
        {9: 0x0F},             # env off
        {},                    # still off, R13 sentinel
        {9: 0x1F},             # env on again, dump omits R13
    ])))
    records, _events, rows = _streams(taym2aymax.convert(taym, _no_warn))
    assert _record_r13(records, 0) == 0x0C
    assert _record_r13(records, 1) == 0xFF
    assert _record_r13(records, 2) == 0xFF
    assert _record_r13(records, 3) == 0xFF
    assert _record_r13(records, 4) == 0x0C
    assert [row["event"] for row in rows] == [EV_EMPTY] * 5
    print("ok  R13 retriggers when env bit rises at the same shape")


def test_r13_restores_after_kernel_stop():
    """STOP of an R13 kernel restores the last dump shape on that frame."""
    taym = _hand_buzz_taym()
    psg = bytearray(b"PSG\x1a" + bytes(12))
    psg += bytes((0xFF, 13, 0x0C))  # dump shape before the kernel
    psg += b"\xff\xff\xff"
    psg += b"\xfd"
    taym.frame_data = {"PSG0": bytes(psg)}
    taym.mods = [taym.mods[1], Mods(command=spec.CMD_EMPTY),
                 Mods(command=spec.CMD_STOP), Mods(command=spec.CMD_EMPTY)]
    taym = read_taym(write_taym(taym))
    records, _events, rows = _streams(taym2aymax.convert(taym, _no_warn))
    assert [row["event"] for row in rows] == [
        EV_START, EV_EMPTY, EV_STOP, EV_EMPTY,
    ]
    assert rows[0]["target"] == 13
    assert _record_r13(records, 0) == 0x0D  # kernel A overwrites the dump
    assert _record_r13(records, 2) == 0x0C  # STOP restores dump shape
    assert _record_r13(records, 3) == 0xFF
    print("ok  R13 restores dump shape after kernel STOP")


def test_r13_queue_does_not_overwrite_new_source_shape():
    """A queued suppressed shape must not replace a later PSG R13 write."""
    taym = _hand_buzz_taym()
    taym.actions[1].target_id = 10
    taym.lanes[1].length = 1
    taym.tlanes[1].length = 1
    psg = bytearray(b"PSG\x1a" + bytes(12))
    psg += bytes((0xFF, 13, 0x0C))  # R13 kernel owns this frame
    psg += bytes((0xFF, 13, 0x0C))  # suppressed by the R10 START handoff
    psg += bytes((0xFF, 13, 0x0D))  # reaches PSG and supersedes queued C
    psg += b"\xff\xfd"                    # omit R13: do not restore C
    taym.frame_data = {"PSG0": bytes(psg)}
    taym = read_taym(write_taym(taym))
    records, _events, rows = _streams(taym2aymax.convert(taym, _no_warn))
    assert rows[0]["target"] == 13
    assert rows[1]["event"] == EV_START and rows[1]["kernel"] == K_COUNTDOWN
    assert rows[1]["target"] == 10
    assert [_record_r13(records, frame) for frame in range(4)] == [
        0x0D, 0x0C, 0x0D, 0xFF,
    ]
    print("ok  queued R13 shape yields to later source shape")


def test_frame_range_starts_at_explicit_start():
    taym = read_taym(write_taym(_hand_buzz_taym()))
    records, events, rows = _streams(taym2aymax.convert(
        taym, _no_warn, frame_start=1, frame_limit=4))
    assert len(records) == 3 * 16 and len(events) == 4
    assert rows[0]["frame"] == 1 and rows[0]["event"] == EV_START
    assert rows[0]["kernel"] == K_DDS_EDGE
    try:
        _streams(taym2aymax.convert(
            taym, _no_warn, frame_start=2, frame_limit=4))
    except ValueError as e:
        assert "inside active" in str(e)
    else:
        assert False, "range started inside an active effect"
    print("ok  frame range starts at explicit START")


if __name__ == "__main__":
    for t in [test_synthetic_buzz_events,
              test_tlan_none_start_uses_base_timer_value,
              test_tlan_none_modulate_keeps_base_timer_value,
              test_chip_period_tlan_uses_period_units,
              test_bound_volume_lane_supplies_value_a,
              test_tone_period_timer_uses_low_register_only,
              test_repeated_start_same_identity_retriggers,
              test_stop_consumes_no_event_entry,
              test_player_accepts_countdown_and_edge,
              test_player_accepts_dds_duty_volume_targets,
              test_player_accepts_countdown_volume_targets,
              test_player_accepts_start_on_last_frame,
              test_player_rejects_dds_duty_r13,
              test_player_accepts_sample_volume_targets,
              test_dds_sample_kernel_encoding,
              test_player_accepts_wavetable_mapping,
              test_wavetable_generation_and_dedup,
              test_swingycat_wavetable_conversion,
              test_sample_rate,
              test_unpacked_asset_serialization,
              test_sample_table_rejects_256th_entry,
              test_sample_classify_and_no_modify,
              test_sample_volume_target_conversion,
              test_virtual_sample_amplitude_and_volume_modulate,
              test_short_sample_classification,
              test_high_rate_sample_keeps_fixed_rate_kernel,
              test_sample_modulate_is_rejected,
              test_dds_sample_requires_stop_inside_frame_range,
              test_frame_limit_excludes_tail_samples,
              test_frame_range_starts_at_explicit_start,
              test_r13_retriggers_when_env_bit_rises,
              test_r13_restores_after_kernel_stop,
              test_r13_queue_does_not_overwrite_new_source_shape]:
        t()
    print("all taym2aymax tests passed")
