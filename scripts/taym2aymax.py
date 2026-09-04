#!/usr/bin/env python3
"""Convert a TAYM interchange file into unpacked AYMax 1.2 data.

This tool reads the chip-agnostic TAYM source of truth (from Bitphase direct
export or any TAYM exporter). It writes unpacked PSG, event, and asset data.

  Bitphase --btp-to-taym--> .taym --taym2aymax--> AYMax 1.2 streams

TAYM -> AYMax mapping:

  PSG0 chunk     -> per-frame absolute register values (raw Bulba dump).
  timer          -> one effect slot. The MODS grid (frame x timer) carries the
                    per-frame commands.
  MODS START     -> install or retrigger; AYMax START on a new kernel identity.
  MODS MODULATE  -> the timer lane changed; AYMax MODIFY when parameters change.
  MODS EMPTY     -> keep the running state; AYMax MODIFY only if value A changed.
  MODS STOP      -> AYMax STOP.
  value LANE     -> the owned target's per-step A/B values.
  timer TLAN     -> the per-step retrigger frequencies (ABS_RATE_HZ 16.16 Hz).

Outputs:

  <stem>_psg.bin         16 B per frame: 14 registers + control16 LE.
  <stem>_events.bin      4 B per START/MODIFY entry.
  <stem>_samples.bin     u16 code count followed by unpacked codes per sample.
  <stem>_wavetables.bin  concatenated 256-byte tables.

  scripts/taym2aymax.py SONG.taym -o build/song --start 560 --frames 560
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "taym" / "python" / "src"))

from aymax_fx import (  # noqa: E402
    CPU_HZ,
    EV_EMPTY,
    EV_MODIFY,
    EV_START,
    EV_STOP,
    EVENT_NAMES,
    K_COUNTDOWN,
    K_DDS_DUTY,
    K_DDS_EDGE,
    K_DDS_SAMPLE,
    K_SAMPLE,
    K_WAVETABLE,
    KERNEL_NAMES,
    QUANT_T,
    control16,
    event_entry,
    to_record_v12,
)
from aymax_assets import (  # noqa: E402
    AY_REG_MASKS,
    DDS_SAMPLE_HZ,
    MIN_WAVETABLE_ITEMS,
    QUANT_HZ,
    R13,
    R13_NO_WRITE,
    SampleTable,
    WavetableTable,
    cook_dds_sample_rate,
    cook_wavetable_rate,
    pad_dds_sample_codes,
    parse_psg,
    serialize_samples,
    serialize_wavetables,
    upsample_sample_codes,
    validate_sample_rate,
)
from taym import spec  # noqa: E402
from taym.codec import read_taym  # noqa: E402
from taym.engine.engine import (  # noqa: E402
    _amp_full_scale,
    _combine_amp_code,
    _dac_table,
)

VOL_TARGETS = frozenset({8, 9, 10})
SAMPLE_KERNELS = frozenset({K_SAMPLE, K_DDS_SAMPLE})

MAX_TIMER_RELOAD = 0x7FFF
FRAME_ALIGNMENT = 16
WARNING_LIMIT = 20




def cook_dds_edge(freqs):
    """Convert two half-phase retrigger frequencies [fA, fB] into the
    K-DDS-EDGE increment, duty8, and effective frequency."""
    if not freqs or freqs[0] <= 0:
        return 0, 128, 0.0
    period_a = CPU_HZ / freqs[0]
    period_b = CPU_HZ / freqs[1] if len(freqs) > 1 and freqs[1] > 0 else period_a
    cycle = period_a + period_b
    inc = round(65536 * QUANT_T / cycle)
    inc = max(1, min(inc, 0xFFFF))
    thr = round(period_a / cycle * 256)
    thr = max(1, min(thr, 255))
    return inc, thr, CPU_HZ / cycle


def cook_countdown(freq, warn, frame):
    """Convert a half-period frequency (Hz) into the K-COUNTDOWN half-T
    reload timer16."""
    if freq <= 0:
        return 0, False
    half = round(CPU_HZ / freq)
    timer16 = round((half - QUANT_T) / 2)
    clamped = not (1 <= timer16 <= MAX_TIMER_RELOAD)
    if clamped:
        warn(f"frame {frame}: timer16 {timer16} clamped (freq {freq:.1f} Hz)")
        timer16 = max(1, min(timer16, MAX_TIMER_RELOAD))
    return timer16, clamped


@dataclass(frozen=True)
class CookedValues:
    value_b: int
    timer: int
    frequency: float
    duty: int = 0
    clamped: bool = False


class KernelMap:
    """One AYMax kernel bound to one TAYM target lane."""

    kernel = None

    def __init__(self, target, value_a, value_b=0, chain_length=1):
        self.target = target
        self.value_a = value_a
        self.value_b = value_b
        self.chain_length = chain_length

    @classmethod
    def bind_values(cls, target, values):
        value_b = values[1] if len(values) > 1 else values[0]
        return cls(target, values[0], value_b, len(values))

    def identity(self, slot):
        return (slot, self.kernel, self.target, self.chain_length)

    def cook_values(self, freqs, warn, frame):
        raise NotImplementedError


class _DdsMap(KernelMap):
    def cook_values(self, freqs, warn, frame):
        increment, duty, frequency = cook_dds_edge(freqs)
        return CookedValues(self.value_b, increment, frequency, duty)


class DdsEdgeMap(_DdsMap):
    kernel = K_DDS_EDGE


class DdsDutyMap(_DdsMap):
    kernel = K_DDS_DUTY


class CountdownMap(KernelMap):
    kernel = K_COUNTDOWN

    def cook_values(self, freqs, warn, frame):
        rate = freqs[0] if freqs else 0.0
        timer, clamped = cook_countdown(rate, warn, frame)
        return CookedValues(self.value_b, timer, rate, clamped=clamped)


class SampleMap(KernelMap):
    kernel = K_SAMPLE

    def __init__(self, target, sample_index, code_count):
        super().__init__(target, 0, sample_index, code_count)
        self.sample_index = sample_index

    def identity(self, slot):
        return (slot, self.kernel, self.target, self.sample_index)

    def cook_values(self, freqs, warn, frame):
        return CookedValues(self.sample_index, 1, QUANT_HZ)


class DdsSampleMap(SampleMap):
    kernel = K_DDS_SAMPLE

    def __init__(self, target, sample_index, code_count, rate, stop_frames):
        super().__init__(target, sample_index, code_count)
        self.rate = rate
        self.stop_frames = stop_frames

    def cook_values(self, freqs, warn, frame):
        increment = cook_dds_sample_rate(self.rate, frame)
        return CookedValues(self.sample_index, increment, DDS_SAMPLE_HZ * increment / 65536)


class WavetableMap(KernelMap):
    kernel = K_WAVETABLE

    def __init__(self, target, value_a, wave_index, chain_length):
        super().__init__(target, value_a, wave_index, chain_length)
        self.wave_index = wave_index

    def cook_values(self, freqs, warn, frame):
        increment, frequency = cook_wavetable_rate(
            freqs, self.chain_length, f"frame {frame}"
        )
        return CookedValues(self.wave_index, increment, frequency)


@dataclass
class TimerBinding:
    kernel_map: KernelMap
    lanes: list
    frequencies: list
    source_lanes: list
    fixed_registers: dict
    is_new: bool
    stop_frame: int | None
    sample_end: int | None
    baked_modulates: bool
    base_timer_value: int
    timer_lane_ref: int | None


@dataclass
class PlayerState:
    kernel_map: KernelMap | None = None
    identity: tuple | None = None
    value_a: int | None = None
    value_b: int | None = None
    duty: int | None = None
    timer: int | None = None
    frequency: float = 0.0


@dataclass
class EnvelopeState:
    sticky_shape: int | None = None
    queued_shape: int | None = None
    was_enabled: bool = False
    was_owned: bool = False


def _split_tone_period_lanes(lanes):
    """Keep an AY tone period in PSG and vary only its low register."""
    if len(lanes) != 2:
        return lanes, {}
    by_target = {
        target: (target, values, loop_index) for target, values, loop_index in lanes
    }
    for low in (0, 2, 4):
        high = low + 1
        if set(by_target) != {low, high}:
            continue
        low_lane = by_target[low]
        high_lane = by_target[high]
        if (
            not low_lane[1]
            or not high_lane[1]
            or len(low_lane[1]) != len(high_lane[1])
            or low_lane[2] != high_lane[2]
        ):
            return lanes, {}
        return [low_lane], {low: low_lane[1][0], high: high_lane[1][0]}
    return lanes, {}


def _classify_sample(target, values, freqs, warn, where, sample_table, frame_rate):
    if sample_table is None:
        warn(f"unsupported instrument {where}: sample table missing")
        return None

    rate = freqs[0] if freqs else 0.0
    validate_sample_rate(rate, where)
    is_dds = rate <= DDS_SAMPLE_HZ
    if is_dds:
        values, stop_frames = pad_dds_sample_codes(values, rate, frame_rate)
    else:
        values = upsample_sample_codes(values, rate, QUANT_HZ)

    try:
        index = sample_table.intern(values)
    except ValueError as error:
        warn(f"unsupported instrument {where}: {error}")
        return None

    if is_dds:
        return DdsSampleMap(target, index, len(values), rate, stop_frames)
    return SampleMap(target, index, len(values))


def _classify_wavetable(
    target, values, loop_index, freqs, warn, where, wavetable_table
):
    if target == R13:
        warn(
            f"unsupported instrument {where}: R13 loop longer than two "
            f"({len(values)} items)"
        )
        return None
    if loop_index != 0:
        warn(
            f"unsupported instrument {where}: wavetable loop must start "
            f"at item 0 (got {loop_index})"
        )
        return None
    if wavetable_table is None:
        warn(f"unsupported instrument {where}: wavetable bank missing")
        return None

    try:
        index = wavetable_table.intern(values, freqs, where)
    except ValueError as error:
        warn(f"unsupported instrument {error}")
        return None
    return WavetableMap(target, values[0], index, len(values))


def classify_from_chain(
    lanes,
    freqs,
    warn,
    where="",
    sample_table=None,
    wavetable_table=None,
    frame_rate=50.0,
):
    """Return the AYMax kernel that can play one decoded TAYM chain.

    Each lane contains a target, its values, and its loop index.
    """
    if len(lanes) != 1:
        warn(
            f"unsupported instrument {where}: expected one target reg, "
            f"got {sorted(t for t, _, _ in lanes)}"
        )
        return None
    target, values, loop_index = lanes[0]
    if loop_index == spec.NO_LOOP and target in VOL_TARGETS:
        return _classify_sample(
            target, values, freqs, warn, where, sample_table, frame_rate
        )
    if len(values) >= MIN_WAVETABLE_ITEMS:
        return _classify_wavetable(
            target,
            values,
            loop_index,
            freqs,
            warn,
            where,
            wavetable_table,
        )

    equal_intervals = len(freqs) == 1 or math.isclose(
        freqs[0], freqs[1], rel_tol=1e-6, abs_tol=1e-6
    )
    if equal_intervals:
        map_class = CountdownMap
    elif target == R13:
        map_class = DdsEdgeMap
    else:
        map_class = DdsDutyMap
    return map_class.bind_values(target, values)


# Event dump columns: (header, width, alignment).
_DUMP_COLUMNS = (
    ("frame", 5, ">"),
    ("event", 6, "<"),
    ("kernel", 9, "<"),
    ("tgt", 4, ">"),
    ("A", 3, ">"),
    ("B", 3, ">"),
    ("duty", 4, ">"),
    ("timer16", 7, ">"),
    ("c.ev", 4, ">"),
    ("c.k", 3, ">"),
    ("c.t", 3, ">"),
    ("ctl16", 5, ">"),
)
_DUMP_LINE = "  ".join(f"{{:{align}{width}}}" for _, width, align in _DUMP_COLUMNS)


def write_events_txt(path, rows):
    """Write the 1.2 streams as a text table."""
    lines = [
        _DUMP_LINE.format(*(name for name, _, _ in _DUMP_COLUMNS)),
        _DUMP_LINE.format(*("-" * width for _, width, _ in _DUMP_COLUMNS)),
    ]
    for row in rows:
        event = EVENT_NAMES.get(row["event"], "?")
        kernel = (
            KERNEL_NAMES.get(row["kernel"], "?") if row["kernel"] is not None else "."
        )
        target = f"R{row['target']:02d}" if row["target"] is not None else "."
        value_a = f"{row['value_a']:02X}" if row["value_a"] is not None else "."
        if row["event"] in (EV_START, EV_MODIFY):
            value_b = f"{row['ev_b']:02X}"
            duty = f"{row['ev_duty']:d}"
            timer = f"{row['ev_timer']:d}"
        else:
            value_b = duty = timer = "."
        control = row["control16"]
        lines.append(
            _DUMP_LINE.format(
                row["frame"],
                event,
                kernel,
                target,
                value_a,
                value_b,
                duty,
                timer,
                (control >> 8) & 0x03,
                (control >> 4) & 0x07,
                control & 0x0F,
                f"{control:04X}",
            )
        )
    Path(path).write_text("\n".join(lines) + "\n")
    return len(lines)


# Kernel/target pairs present in the player.
PLAYER_MAPPINGS = frozenset(
    {
        (K_COUNTDOWN, 8),
        (K_COUNTDOWN, 9),
        (K_COUNTDOWN, 10),
        (K_COUNTDOWN, 13),
        (K_DDS_DUTY, 8),
        (K_DDS_DUTY, 9),
        (K_DDS_DUTY, 10),
        (K_DDS_EDGE, 13),
    }
    | {
        (kernel, target)
        for kernel in (K_SAMPLE, K_DDS_SAMPLE)
        for target in VOL_TARGETS
    }
    | {(K_WAVETABLE, target) for target in range(13)}
)


def validate_player_rows(rows):
    """Reject mappings not supported by the player."""
    for row in rows:
        if row["event"] not in (EV_START, EV_MODIFY):
            continue
        if row["kernel"] == K_DDS_DUTY and row["target"] == R13:
            raise ValueError(
                f"frame {row['frame']}: K-DDS-DUTY cannot target R13 "
                "(each write retriggers the envelope)"
            )
        pair = (row["kernel"], row["target"])
        if pair not in PLAYER_MAPPINGS:
            raise ValueError(
                f"frame {row['frame']}: unsupported player mapping "
                f"{KERNEL_NAMES[row['kernel']]} R{row['target']}"
            )
        if row["event"] == EV_MODIFY and row["kernel"] in SAMPLE_KERNELS:
            raise ValueError(
                f"frame {row['frame']}: {KERNEL_NAMES[row['kernel']]} "
                "MODIFY is malformed"
            )


@dataclass
class Context:
    """Converter inputs shared by the binding decode."""

    taym: object
    warn: object
    samples: SampleTable
    wavetables: WavetableTable

    @property
    def frame_rate(self):
        return self.taym.trak.frame_rate_hz


def _where(frame, timer_index):
    return f"frame {frame} timer {timer_index}"


def _equal_intervals(freqs):
    return len(freqs) == 1 or math.isclose(
        freqs[0], freqs[1], rel_tol=1e-6, abs_tol=1e-6
    )


def _mod_actions(taym, mod):
    """Return the ACTN records of one MODS cell."""
    return taym.actions[mod.first_action : mod.first_action + mod.action_count]


def _lane_values(taym, lane):
    """Return the values from one LANE."""
    pool = taym.pool_for(lane.value_type)
    return list(pool[lane.value_offset : lane.value_offset + lane.length])


def _masked_lane_values(taym, lane, target):
    return [value & AY_REG_MASKS[target] for value in _lane_values(taym, lane)]


def _timer_freqs(taym, timer, timer_lane_ref, base_timer_value):
    """Decode one timer's per-step retrigger frequencies in Hz.

    timer_lane_ref None: one frequency from MODS.base_timer_value.
    Otherwise one frequency per TLAN step.
    """
    chip = taym.chips[timer.chip_index]
    if timer.clock_mode == spec.CLOCK_ABS_RATE_HZ:
        base = spec.from_fix16(base_timer_value)
    else:
        divider = timer.clock_divider * base_timer_value
        base = chip.clock_hz / divider if divider else 0.0
    if timer_lane_ref is None:
        return [base]
    tlan = taym.tlanes[timer_lane_ref]
    values = taym.vu32[tlan.value_offset : tlan.value_offset + tlan.length]
    if tlan.timing_mode == spec.TM_RELATIVE:
        return [base * spec.from_fix16(value) for value in values]
    if timer.clock_mode == spec.CLOCK_ABS_RATE_HZ:
        return [spec.from_fix16(value) for value in values]
    return [
        chip.clock_hz / (timer.clock_divider * value) if value else base
        for value in values
    ]


def _next_sample_command_frame(taym, timer_index, start_frame):
    """Return the next START/STOP frame for one timer, or the track end."""
    timer_count = len(taym.timers)
    for frame in range(start_frame + 1, taym.trak.frame_count):
        command = taym.mods[frame * timer_count + timer_index].command
        if command in (spec.CMD_START, spec.CMD_STOP):
            return frame
    return taym.trak.frame_count


def _sample_and_volume_actions(actions):
    """Split one MODS cell into its bound 0x80 lanes and R8/R9/R10 actions."""
    sample = [
        action
        for action in actions
        if action.target_id == spec.TGT_SAMPLE_AMPLITUDE
        and action.source_mode == spec.SRC_BIND_LANE
    ]
    volume = [action for action in actions if action.target_id in spec.AY_AMP_REGS]
    return sample, volume


def _virtual_sample_lane(ctx, timer_index, start_frame, start_mod, frequencies):
    """Resolve TAYM's linear sample-amplitude target to one AY volume lane.

    The AYMax sample kernels write 4-bit DAC codes directly. Combine 0x80 with
    its paired inline R8/R9/R10 volume through the TAYM AY/YM DAC rule. Bake
    phase-preserving volume MODULATE commands into the codes because SAMPLE
    MODIFY cannot change a running sample.
    """
    taym = ctx.taym
    where = _where(start_frame, timer_index)
    sample_actions, volume_actions = _sample_and_volume_actions(
        _mod_actions(taym, start_mod)
    )
    if len(sample_actions) != 1 or len(volume_actions) != 1:
        raise ValueError(
            f"{where}: sample amplitude requires one bound 0x80 lane and one "
            "paired R8/R9/R10"
        )
    sample_action, volume_action = sample_actions[0], volume_actions[0]
    if volume_action.source_mode != spec.SRC_INLINE_VALUE:
        raise ValueError(f"{where}: AYMax requires an inline sample volume")
    if len(frequencies) != 1 or frequencies[0] <= 0:
        raise ValueError(f"{where}: AYMax requires a fixed sample rate")

    lane = taym.lanes[sample_action.operand]
    lane_values = _lane_values(taym, lane)
    rate = frequencies[0]
    end_frame = _next_sample_command_frame(taym, timer_index, start_frame)
    source_count = math.ceil((end_frame - start_frame) * rate / ctx.frame_rate)
    if source_count <= len(lane_values) or lane.loop_index == spec.NO_LOOP:
        values = lane_values[:source_count]
    else:
        values = list(lane_values[: lane.loop_index])
        cycle = lane_values[lane.loop_index :]
        if not cycle:
            raise ValueError(f"{where}: empty sample loop")
        while len(values) < source_count:
            values.extend(cycle[: source_count - len(values)])

    volume_changes = [(0, volume_action.operand)]
    timer_count = len(taym.timers)
    for frame in range(start_frame + 1, end_frame):
        modulation = taym.mods[frame * timer_count + timer_index]
        if modulation.command != spec.CMD_MODULATE:
            continue
        if modulation.timer_lane_ref != spec.TLAN_UNCHANGED:
            raise ValueError(
                f"{_where(frame, timer_index)}: sample rate MODULATE is unsupported"
            )
        sample_changes, volume_changes_here = _sample_and_volume_actions(
            _mod_actions(taym, modulation)
        )
        if (
            len(sample_changes) != 1
            or sample_changes[0].operand != sample_action.operand
            or len(volume_changes_here) != 1
            or volume_changes_here[0].target_id != volume_action.target_id
            or volume_changes_here[0].source_mode != spec.SRC_INLINE_VALUE
        ):
            raise ValueError(
                f"{_where(frame, timer_index)}: sample MODULATE must keep the "
                "lane and paired volume register"
            )
        offset = math.ceil((frame - start_frame) * rate / ctx.frame_rate)
        volume_changes.append((offset, volume_changes_here[0].operand))

    chip = taym.chips[taym.timers[timer_index].chip_index]
    table = _dac_table(chip.variant)
    full_scale = _amp_full_scale(lane.value_type)
    codes = []
    change = 0
    for index, amplitude in enumerate(values):
        while (
            change + 1 < len(volume_changes) and index >= volume_changes[change + 1][0]
        ):
            change += 1
        codes.append(
            _combine_amp_code(table, volume_changes[change][1], amplitude, full_scale)
        )
    return (
        (volume_action.target_id, codes, spec.NO_LOOP),
        end_frame,
        len(volume_changes) > 1,
    )


def _resolve_start(ctx, timer_index, frame, start_mod):
    """Return the lane and frequency chains installed by START."""
    taym = ctx.taym
    actions = _mod_actions(taym, start_mod)
    timer_lane_ref = start_mod.timer_lane_ref
    if timer_lane_ref in (spec.TLAN_NONE, spec.TLAN_UNCHANGED):
        timer_lane_ref = None
    frequencies = _timer_freqs(
        taym, taym.timers[timer_index], timer_lane_ref, start_mod.base_timer_value
    )

    if any(action.target_id == spec.TGT_SAMPLE_AMPLITUDE for action in actions):
        lane, sample_end, baked_modulates = _virtual_sample_lane(
            ctx, timer_index, frame, start_mod, frequencies
        )
        return [lane], frequencies, sample_end, baked_modulates

    lanes = []
    for action in actions:
        if action.source_mode != spec.SRC_BIND_LANE:
            continue
        lane = taym.lanes[action.operand]
        target = action.target_id
        values = _masked_lane_values(taym, lane, target)
        lanes.append((target, values, lane.loop_index))
    if not lanes:
        return None
    return lanes, frequencies, None, False


def _classify(ctx, lanes, frequencies, frame, timer_index):
    return classify_from_chain(
        lanes,
        frequencies,
        ctx.warn,
        where=_where(frame, timer_index),
        sample_table=ctx.samples,
        wavetable_table=ctx.wavetables,
        frame_rate=ctx.frame_rate,
    )


def _create_binding(ctx, timer_index, frame, start_mod):
    decoded = _resolve_start(ctx, timer_index, frame, start_mod)
    if decoded is None:
        ctx.warn(f"{_where(frame, timer_index)}: START installs no bound target")
        return None

    source_lanes, frequencies, sample_end, baked_modulates = decoded
    lanes, fixed_registers = _split_tone_period_lanes(source_lanes)
    kernel_map = _classify(ctx, lanes, frequencies, frame, timer_index)
    if kernel_map is None:
        return None

    stop_frame = None
    if kernel_map.kernel == K_DDS_SAMPLE:
        stop_frame = frame + kernel_map.stop_frames
        if sample_end is not None:
            stop_frame = min(stop_frame, sample_end)

    timer_lane_ref = (
        None if start_mod.timer_lane_ref == spec.TLAN_NONE else start_mod.timer_lane_ref
    )
    return TimerBinding(
        kernel_map=kernel_map,
        lanes=lanes,
        frequencies=frequencies,
        source_lanes=source_lanes,
        fixed_registers=fixed_registers,
        is_new=True,
        stop_frame=stop_frame,
        sample_end=sample_end,
        baked_modulates=baked_modulates,
        base_timer_value=start_mod.base_timer_value,
        timer_lane_ref=timer_lane_ref,
    )


def _update_modulation(ctx, timer_index, frame, modulation, binding):
    taym = ctx.taym
    where = _where(frame, timer_index)
    kernel = binding.kernel_map.kernel
    if kernel in SAMPLE_KERNELS and binding.baked_modulates:
        binding.is_new = False
        return
    if kernel in SAMPLE_KERNELS:
        raise ValueError(f"{where}: {KERNEL_NAMES[kernel]} MODULATE is malformed")

    if modulation.action_count:
        lanes = {
            target: (values, loop_index)
            for target, values, loop_index in binding.source_lanes
        }
        for action in _mod_actions(taym, modulation):
            if (
                action.target_id not in lanes
                or action.source_mode != spec.SRC_BIND_LANE
            ):
                raise ValueError(
                    f"{where}: unsupported MODULATE source for target "
                    f"{action.target_id}"
                )
            lane = taym.lanes[action.operand]
            old_values, old_loop = lanes[action.target_id]
            values = _masked_lane_values(taym, lane, action.target_id)
            if len(values) != len(old_values) or lane.loop_index != old_loop:
                raise ValueError(f"{where}: MODULATE changes lane shape")
            lanes[action.target_id] = values, lane.loop_index

        binding.source_lanes = [
            (target, *lanes[target]) for target, _values, _loop in binding.source_lanes
        ]
        binding.lanes, binding.fixed_registers = _split_tone_period_lanes(
            binding.source_lanes
        )

    base_timer_value = modulation.base_timer_value or binding.base_timer_value
    timer_lane_ref = binding.timer_lane_ref
    if modulation.timer_lane_ref == spec.TLAN_NONE:
        timer_lane_ref = None
    elif modulation.timer_lane_ref != spec.TLAN_UNCHANGED:
        timer_lane_ref = modulation.timer_lane_ref
    binding.frequencies = _timer_freqs(
        taym, taym.timers[timer_index], timer_lane_ref, base_timer_value
    )
    binding.base_timer_value = base_timer_value
    binding.timer_lane_ref = timer_lane_ref

    old_map = binding.kernel_map
    needs_classification = old_map.kernel == K_WAVETABLE or (
        old_map.kernel == K_COUNTDOWN and not _equal_intervals(binding.frequencies)
    )
    if needs_classification:
        kernel_map = _classify(ctx, binding.lanes, binding.frequencies, frame, timer_index)
    else:
        kernel_map = type(old_map).bind_values(old_map.target, binding.lanes[0][1])

    if kernel_map is None:
        raise ValueError(f"{where}: unsupported MODULATE mapping")
    binding.kernel_map = kernel_map
    binding.is_new = False


def _update_bindings(ctx, frame, bindings):
    taym = ctx.taym
    timer_count = len(taym.timers)
    for timer_index in range(timer_count):
        mod = taym.mods[frame * timer_count + timer_index]
        binding = bindings[timer_index]

        if mod.command == spec.CMD_EMPTY:
            if (
                binding is not None
                and binding.kernel_map.kernel == K_DDS_SAMPLE
                and binding.stop_frame <= frame
            ):
                bindings[timer_index] = None
        elif mod.command == spec.CMD_STOP:
            bindings[timer_index] = None
        elif mod.command == spec.CMD_START:
            bindings[timer_index] = _create_binding(ctx, timer_index, frame, mod)
        elif mod.command == spec.CMD_MODULATE and binding is not None:
            _update_modulation(ctx, timer_index, frame, mod, binding)


def _apply_envelope_state(registers, source_shape, event, target, state):
    if source_shape is not None:
        state.sticky_shape = source_shape

    is_owned = target == R13
    is_suppressed = is_owned or (event == EV_START and state.was_owned)
    if source_shape is not None:
        state.queued_shape = source_shape if is_suppressed else None

    envelope_enabled = bool((registers[8] | registers[9] | registers[10]) & 0x10)
    needs_shape = (
        not is_suppressed
        and registers[R13] == R13_NO_WRITE
        and state.sticky_shape is not None
    )
    if needs_shape:
        if state.queued_shape is not None:
            registers[R13] = state.queued_shape
            state.queued_shape = None
        elif state.was_owned or (not state.was_enabled and envelope_enabled):
            registers[R13] = state.sticky_shape

    state.was_enabled = envelope_enabled
    state.was_owned = is_owned


def _update_player(frame, slot, bindings, player, warn):
    if slot is None:
        event = EV_STOP if player.kernel_map is not None else EV_EMPTY
        player.kernel_map = None
        player.identity = None
        player.value_a = None
        return event, None

    binding = bindings[slot]
    kernel_map = binding.kernel_map
    identity = kernel_map.identity(slot)
    start = (
        binding.is_new
        or player.kernel_map is None
        or identity != player.identity
    )
    if kernel_map.kernel in SAMPLE_KERNELS:
        value_a = 0
    else:
        value_a = kernel_map.value_a & AY_REG_MASKS[kernel_map.target]
    cooked = kernel_map.cook_values(binding.frequencies, warn, frame)
    changed = kernel_map.kernel not in SAMPLE_KERNELS and (
        value_a != player.value_a
        or cooked.value_b != player.value_b
        or cooked.duty != player.duty
        or cooked.timer != player.timer
    )

    player.kernel_map = kernel_map
    player.identity = identity
    player.value_a = value_a
    player.value_b = cooked.value_b
    player.duty = cooked.duty
    player.timer = cooked.timer
    player.frequency = cooked.frequency
    binding.is_new = False

    if start:
        return EV_START, cooked
    if changed:
        return EV_MODIFY, cooked
    return EV_EMPTY, cooked


def convert(
    taym, warn, sample_table=None, wavetable_table=None, frame_start=0, frame_limit=None
):
    """Yield one PSG record, event entry, and report row per TAYM frame.

    Frames before frame_start update converter state but produce no output.
    frame_limit is the exclusive end. The tables collect referenced assets.
    """
    if frame_start < 0:
        raise ValueError("start frame must be non-negative")
    if sample_table is None:
        sample_table = SampleTable()
    if wavetable_table is None:
        wavetable_table = WavetableTable()
    timer_count = len(taym.timers)
    frame_count = taym.trak.frame_count

    psg_bytes = taym.frame_data.get("PSG0")
    if psg_bytes is None:
        raise ValueError("TAYM has no PSG0 frame-data chunk (need an AY PSG dump)")
    psg = parse_psg(psg_bytes)
    n = min(frame_count, len(psg))
    if frame_limit is not None:
        n = min(n, frame_limit)
    if frame_count != len(psg):
        warn(f"frame count mismatch: trak {frame_count}, psg {len(psg)}; using {n}")

    ctx = Context(taym, warn, sample_table, wavetable_table)
    bindings = [None] * timer_count
    player = PlayerState()
    envelope = EnvelopeState()

    for frame in range(n):
        registers = list(psg[frame])
        source_shape = None if registers[R13] == R13_NO_WRITE else registers[R13] & 0x0F

        _update_bindings(ctx, frame, bindings)

        active = [
            timer_index
            for timer_index, binding in enumerate(bindings)
            if binding is not None
        ]
        if len(active) > 1:
            warn(f"frame {frame}: {len(active)} active timers; using timer {active[0]}")
        slot = active[0] if active else None

        event, cooked = _update_player(frame, slot, bindings, player, warn)
        value_a = player.value_a

        kernel_map = player.kernel_map
        kernel = kernel_map.kernel if kernel_map else None
        target = kernel_map.target if kernel_map else None
        if kernel_map is not None:
            for register, fixed_value in bindings[slot].fixed_registers.items():
                registers[register] = fixed_value
            assert value_a == value_a & AY_REG_MASKS[target]
            registers[target] = value_a

        _apply_envelope_state(registers, source_shape, event, target, envelope)

        is_entry = event in (EV_START, EV_MODIFY)
        if is_entry:
            ctl = control16(event, kernel, target)
            entry = event_entry(
                player.value_b or 0, player.duty or 0, player.timer or 0
            )
        else:
            ctl = control16(event if event == EV_STOP else EV_EMPTY)
            entry = None

        row = {
            "frame": frame,
            "event": event,
            "kernel": kernel,
            "target": target,
            "value_a": value_a,
            "ev_b": player.value_b if is_entry else None,
            "ev_duty": player.duty if is_entry else None,
            "ev_timer": player.timer if is_entry else None,
            "control16": ctl,
            "freq": player.frequency if kernel_map else 0.0,
            "clamped": cooked.clamped if cooked else False,
            "start": event == EV_START,
        }
        if frame < frame_start:
            continue
        if frame == frame_start and kernel_map and event != EV_START:
            raise ValueError(
                f"start frame {frame} is inside active {KERNEL_NAMES[kernel]}; "
                "choose its START frame"
            )
        yield to_record_v12(registers, ctl), entry, row

    for binding in bindings:
        if (
            binding is not None
            and binding.kernel_map.kernel == K_DDS_SAMPLE
            and binding.stop_frame >= n
            and binding.sample_end != n
        ):
            raise ValueError(
                f"frame range ends before DDS-SAMPLE STOP at frame {binding.stop_frame}"
            )


def _argument_parser():
    parser = argparse.ArgumentParser(description="TAYM -> unpacked AYMax 1.2 data")
    parser.add_argument("taym")
    parser.add_argument(
        "-o",
        "--out",
        metavar="STEM",
        help="output stem (default build/<name>)",
    )
    parser.add_argument(
        "--start",
        type=int,
        default=0,
        help="first source frame to emit (default: 0)",
    )
    parser.add_argument(
        "--frames",
        type=int,
        help="maximum number of frames to emit (default: all)",
    )
    parser.add_argument(
        "--events-txt",
        metavar="PATH",
        nargs="?",
        const="",
        help="write the stream dump (default <stem>_events.txt)",
    )
    return parser


def _selected_frame_limit(taym, start, max_frames):
    psg_data = taym.frame_data.get("PSG0")
    if psg_data is None:
        raise ValueError("TAYM has no PSG0 frame-data chunk")

    source_frames = min(taym.trak.frame_count, len(parse_psg(psg_data)))
    if start >= source_frames:
        raise ValueError(f"start frame {start} is past the {source_frames}-frame input")

    output_frames = source_frames - start
    if max_frames is not None:
        output_frames = min(output_frames, max_frames)
    if output_frames >= FRAME_ALIGNMENT:
        output_frames -= output_frames % FRAME_ALIGNMENT
    return start + output_frames


def _collect_streams(taym, warn, frame_start, frame_limit):
    rows = []
    records = bytearray()
    events = bytearray()
    sample_table = SampleTable()
    wavetable_table = WavetableTable()

    for record, entry, row in convert(
        taym,
        warn,
        sample_table=sample_table,
        wavetable_table=wavetable_table,
        frame_start=frame_start,
        frame_limit=frame_limit,
    ):
        records += record
        if entry is not None:
            events += entry
        rows.append(row)

    validate_player_rows(rows)
    return records, events, rows, sample_table, wavetable_table


def main():
    parser = _argument_parser()
    args = parser.parse_args()

    if args.start < 0:
        parser.error("--start must be non-negative")
    if args.frames is not None and args.frames <= 0:
        parser.error("--frames must be positive")

    name = Path(args.taym).stem
    stem = Path(args.out) if args.out else Path("build") / name
    psg_path = stem.parent / f"{stem.name}_psg.bin"
    events_path = stem.parent / f"{stem.name}_events.bin"
    samples_path = stem.parent / f"{stem.name}_samples.bin"
    wavetables_path = stem.parent / f"{stem.name}_wavetables.bin"

    warnings = []
    warn = warnings.append
    try:
        taym = read_taym(Path(args.taym).read_bytes())
        frame_limit = _selected_frame_limit(taym, args.start, args.frames)
        records, events, rows, sample_table, wavetable_table = _collect_streams(
            taym, warn, args.start, frame_limit
        )
        samples_blob = serialize_samples(sample_table)
        wavetables_blob = serialize_wavetables(wavetable_table)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    psg_path.parent.mkdir(parents=True, exist_ok=True)
    psg_path.write_bytes(records)
    events_path.write_bytes(events)
    samples_path.write_bytes(samples_blob)
    wavetables_path.write_bytes(wavetables_blob)

    event_counts = Counter(row["event"] for row in rows)
    chip = taym.chips[0] if taym.chips else None
    clock_hz = chip.clock_hz if chip else 0
    print(
        f"{name}: clk={clock_hz} rate={taym.trak.frame_rate_hz:.1f}Hz "
        f"{taym.trak.frame_count} frames -> "
        f"{psg_path} ({len(records)} B, {len(rows)} records) + "
        f"{events_path} ({len(events)} B, {len(events) // 4} entries)"
    )
    print(
        "  events: "
        + " ".join(
            f"{EVENT_NAMES[event]}={count}"
            for event, count in sorted(event_counts.items())
        )
    )
    print(
        f"  unpacked assets: {len(sample_table)} samples "
        f"({len(samples_blob)} B) -> {samples_path}; "
        f"{len(wavetable_table)} wavetables "
        f"({len(wavetables_blob)} B) -> {wavetables_path}"
    )

    if args.events_txt is not None:
        dump_path = (
            Path(args.events_txt)
            if args.events_txt
            else stem.parent / f"{stem.name}_events.txt"
        )
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        line_count = write_events_txt(dump_path, rows)
        print(f"  stream dump -> {dump_path} ({line_count} lines)")

    if warnings:
        print(f"  {len(warnings)} warning(s):", file=sys.stderr)
        for warning in warnings[:WARNING_LIMIT]:
            print(f"    {warning}", file=sys.stderr)
        if len(warnings) > WARNING_LIMIT:
            print(
                f"    ... +{len(warnings) - WARNING_LIMIT} more",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
