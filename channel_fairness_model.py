"""Pure channel-balanced aggregation for community sentiment signals.

The model has four immutable top-level seats: Eastmoney, Sina, Tonghuashun and
Taoguba.  Each owns 25% of the final estimate.  A channel that is missing does
not donate its seat to the channels that happened to be collected; its value is
the neutral baseline instead.

Within a channel, ``expectedUnits`` defines the fixed voting seats.  In
``company`` mode these are normally three representative companies.  In
``sector`` mode they are the configured sector-community streams.  Both modes
use the same fixed-seat maths: every expected unit has equal weight, unexpected
units are ignored, and missing units are neutralised.

Sample size is deliberately not a target voting weight.  It is used only for
the bounded precision factor ``n / (n + 12)`` that shrinks each fixed unit
seat's own departure from neutral and for reliability diagnostics.  A
high-sample neutral company therefore cannot lend precision to an unrelated
low-sample extreme company, while accidental source duplication still cannot
create an extra seat.

The module contains no I/O and is safe to exercise with synthetic observations.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date
from typing import Dict, List, Mapping, MutableMapping, Sequence, Tuple

from fairness_model import score_personas


MODEL_VERSION = "channel_equal_four_seats_v1"
CHANNELS: Tuple[str, str, str, str] = ("eastmoney", "sina", "ths", "taoguba")
CHANNEL_TARGET_WEIGHT = 0.25
DEFAULT_DAY_WEIGHTS: Tuple[float, float, float] = (0.50, 0.30, 0.20)
SAMPLE_PRIOR = 12.0

EMOTION_KEYS: Tuple[str, ...] = (
    "optimism",
    "fomo",
    "panic",
    "newcomer",
    "intensity",
)
SIGNAL_KEYS: Tuple[str, ...] = (*EMOTION_KEYS, "attention")
SIGNAL_BASELINES: Dict[str, float] = {
    "optimism": 50.0,
    "fomo": 50.0,
    "panic": 0.0,
    "newcomer": 50.0,
    "intensity": 50.0,
    "attention": 50.0,
}
VALID_STATUSES = {"observed", "verified_zero", "unobserved"}


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _clamp(value: object, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, _safe_float(value)))


def _round1(value: object) -> float:
    return round(_clamp(value), 1)


def _normalise_day(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        raise ValueError("trading day cannot be empty")
    return date.fromisoformat(text).isoformat()


def _normalise_status(row: Mapping[str, object] | None) -> str:
    if row is None:
        return "unobserved"
    raw = str(row.get("status") or "").strip().lower().replace("-", "_")
    raw = {
        "available": "observed",
        "zero": "verified_zero",
        "verifiedzero": "verified_zero",
        "missing": "unobserved",
        "unknown": "unobserved",
    }.get(raw, raw)
    if not raw:
        signals = row.get("signals") if isinstance(row.get("signals"), Mapping) else row
        has_signal = any(signals.get(key) is not None for key in SIGNAL_KEYS)
        has_sample = _safe_float(row.get("sampleCount")) > 0
        raw = "observed" if has_signal or has_sample or row.get("available") is True else "unobserved"
    if raw not in VALID_STATUSES:
        raise ValueError(f"invalid observation status: {raw!r}")
    return raw


def _precision(sample_count: object) -> float:
    count = max(0.0, _safe_float(sample_count))
    return count / (count + SAMPLE_PRIOR) if count > 0 else 0.0


def _signal_from_rate(rate: float, kind: str) -> float:
    if kind == "fomo":
        return _round1(50.0 + 30.0 * math.tanh((rate - 0.06) / 0.07))
    if kind == "newcomer":
        return _round1(50.0 + 28.0 * math.tanh((rate - 0.02) / 0.035))
    if kind == "intensity":
        return _round1(50.0 + 25.0 * math.tanh((rate - 0.18) / 0.16))
    raise ValueError(kind)


def _signals_from_metrics(metrics: Mapping[str, object]) -> Dict[str, float]:
    """Accept canonical signals or the project's current ``dailyRaw`` metrics."""

    canonical = {
        key: _clamp(metrics.get(key), 0.0, 100.0)
        for key in SIGNAL_KEYS
        if metrics.get(key) is not None
    }
    if canonical:
        return canonical
    if not any(key in metrics for key in ("net", "fomo", "panic", "newcomer", "intense")):
        return {}
    net = _safe_float(metrics.get("net"))
    fomo = max(0.0, _safe_float(metrics.get("fomo")))
    panic = max(0.0, _safe_float(metrics.get("panic")))
    newcomer = max(0.0, _safe_float(metrics.get("newcomer")))
    intense = max(0.0, _safe_float(metrics.get("intense")))
    return {
        "optimism": _round1(50.0 + 45.0 * net),
        "fomo": _signal_from_rate(fomo, "fomo"),
        "panic": _round1(100.0 * (1.0 - math.exp(-panic / 0.12))),
        "newcomer": _signal_from_rate(newcomer, "newcomer"),
        "intensity": _signal_from_rate(intense, "intensity"),
    }


def _canonical_observation(raw: Mapping[str, object] | None) -> Dict[str, object]:
    row = raw if isinstance(raw, Mapping) else {}
    status = _normalise_status(row if row else None)
    nested = row.get("signals")
    signals_raw = nested if isinstance(nested, Mapping) else row
    signals = _signals_from_metrics(signals_raw)
    sample_count = max(0, int(_safe_float(row.get("sampleCount"))))
    if status == "observed" and signals and sample_count == 0:
        sample_count = 1
    return {
        "status": status,
        "sampleCount": sample_count,
        "signals": signals if status == "observed" else {},
    }


def _source_days(source: Mapping[str, object], days: Sequence[str]) -> Dict[str, Dict[str, object]]:
    analysis_value = source.get("analysis")
    analysis = analysis_value if isinstance(analysis_value, Mapping) else {}
    daily_value = source.get("dailySignals")
    if not isinstance(daily_value, Mapping):
        daily_value = analysis.get("dailySignals")
    daily: Dict[str, Mapping[str, object]] = {}
    if isinstance(daily_value, Mapping):
        for raw_day, raw_row in daily_value.items():
            if isinstance(raw_row, Mapping):
                daily[_normalise_day(raw_day)] = raw_row

    raw_value = analysis.get("dailyRaw")
    raw_by_day: Dict[str, Mapping[str, object]] = {}
    if isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes)):
        for item in raw_value:
            if isinstance(item, Mapping) and item.get("date"):
                raw_by_day[_normalise_day(item.get("date"))] = item

    verified_value = source.get("verifiedZeroDays", [])
    if isinstance(verified_value, (str, date)):
        verified_value = [verified_value]
    verified = {
        _normalise_day(item)
        for item in verified_value
    } if isinstance(verified_value, Sequence) else set()

    result: Dict[str, Dict[str, object]] = {}
    for day in days:
        if day in daily:
            result[day] = _canonical_observation(daily[day])
            continue
        raw = raw_by_day.get(day)
        if raw is not None and (raw.get("available") is True or raw.get("status") == "observed"):
            metrics = raw.get("metrics") if isinstance(raw.get("metrics"), Mapping) else raw
            result[day] = _canonical_observation({
                "status": "observed",
                "sampleCount": raw.get("sampleCount", 0),
                "signals": _signals_from_metrics(metrics),
            })
        elif day in verified:
            result[day] = {"status": "verified_zero", "sampleCount": 0, "signals": {}}
        else:
            result[day] = {"status": "unobserved", "sampleCount": 0, "signals": {}}
    return result


def _parse_specs(
    channel_specs: Mapping[str, Mapping[str, object]],
) -> Dict[str, Dict[str, object]]:
    if not isinstance(channel_specs, Mapping):
        raise ValueError("channel_specs must be a mapping")
    missing_specs = [channel for channel in CHANNELS if channel not in channel_specs]
    if missing_specs:
        raise ValueError(f"missing channel specs: {', '.join(missing_specs)}")
    unexpected_specs = sorted(set(str(key) for key in channel_specs) - set(CHANNELS))
    if unexpected_specs:
        raise ValueError(f"unexpected channel specs: {', '.join(unexpected_specs)}")

    parsed: Dict[str, Dict[str, object]] = {}
    for channel in CHANNELS:
        raw = channel_specs[channel]
        if not isinstance(raw, Mapping):
            raise ValueError(f"channel spec for {channel} must be a mapping")
        mode = str(raw.get("mode") or "").strip().lower()
        if mode not in {"company", "sector"}:
            raise ValueError(f"channel {channel} mode must be 'company' or 'sector'")
        expected_raw = raw.get("expectedUnits")
        if not isinstance(expected_raw, Sequence) or isinstance(expected_raw, (str, bytes)):
            raise ValueError(f"channel {channel} expectedUnits must be an explicit sequence")
        expected = tuple(str(item).strip() for item in expected_raw if str(item).strip())
        if not expected:
            raise ValueError(f"channel {channel} expectedUnits cannot be empty")
        if len(set(expected)) != len(expected):
            raise ValueError(f"channel {channel} expectedUnits must be unique")
        parsed[channel] = {
            "mode": mode,
            "expectedUnits": expected,
            "allowAttention": bool(raw.get("allowAttention", True)),
        }
    return parsed


def _merge_unit_day(rows: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    """Collapse duplicate source rows without allowing them to manufacture n."""

    observed = [row for row in rows if row.get("status") == "observed"]
    if observed:
        signals: Dict[str, float] = {}
        samples: Dict[str, int] = {}
        for signal in SIGNAL_KEYS:
            eligible = [row for row in observed if signal in (row.get("signals") or {})]
            if not eligible:
                continue
            # Source count and page size are not votes.  Equal averaging prevents
            # a larger page from overpowering another view of the same unit.
            signals[signal] = sum(float(row["signals"][signal]) for row in eligible) / len(eligible)
            # Max, rather than sum, makes an exact duplicated source idempotent.
            samples[signal] = max(int(row.get("sampleCount") or 0) for row in eligible)
        return {
            "status": "observed",
            "signals": signals,
            "signalSamples": samples,
            "sampleCount": max(samples.values(), default=0),
            "sourceRowCount": len(rows),
        }
    if rows and all(row.get("status") == "verified_zero" for row in rows):
        status = "verified_zero"
    elif any(row.get("status") == "verified_zero" for row in rows):
        status = "verified_zero"
    else:
        status = "unobserved"
    return {
        "status": status,
        "signals": {},
        "signalSamples": {},
        "sampleCount": 0,
        "sourceRowCount": len(rows),
    }


def aggregate_channel_fairness(
    source_rows: Sequence[Mapping[str, object]],
    trading_days: Sequence[object],
    channel_specs: Mapping[str, Mapping[str, object]],
    *,
    day_weights: Sequence[float] = DEFAULT_DAY_WEIGHTS,
) -> Dict[str, object]:
    """Aggregate community signals through four immutable 25% channel seats.

    Each source row identifies ``channel`` and ``unitKey`` and supplies either
    ``dailySignals`` or an ``analysis.dailyRaw`` payload.  ``channel_specs`` must
    explicitly configure all four channels, for example::

        {
            "eastmoney": {"mode": "company", "expectedUnits": ["a", "b", "c"]},
            "sina": {"mode": "company", "expectedUnits": ["a", "b", "c"]},
            "ths": {"mode": "sector", "expectedUnits": ["pcb-community"],
                    "allowAttention": False},
            "taoguba": {"mode": "sector", "expectedUnits": ["pcb-forum"]},
        }
    """

    days = tuple(_normalise_day(day) for day in trading_days)
    if len(days) != 3 or len(set(days)) != 3:
        raise ValueError("exactly three unique trading days are required")
    if len(day_weights) != 3:
        raise ValueError("day_weights must contain exactly three values")
    raw_day_weights = [max(0.0, _safe_float(weight)) for weight in day_weights]
    total_day_weight = sum(raw_day_weights)
    if total_day_weight <= 0:
        raise ValueError("day_weights must contain positive mass")
    weights = tuple(weight / total_day_weight for weight in raw_day_weights)
    specs = _parse_specs(channel_specs)

    flags: set[str] = set()
    grouped: MutableMapping[str, MutableMapping[str, MutableMapping[str, List[Mapping[str, object]]]]] = (
        defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    )
    ignored_channel_rows = 0
    for source in source_rows:
        if not isinstance(source, Mapping):
            continue
        channel = str(source.get("channel") or source.get("platform") or "").strip().lower()
        if channel not in CHANNELS:
            ignored_channel_rows += 1
            continue
        unit_key = str(
            source.get("unitKey")
            or source.get("companyKey")
            or source.get("sectorKey")
            or ""
        ).strip()
        if not unit_key:
            raise ValueError("every recognised source row needs unitKey")
        if unit_key not in specs[channel]["expectedUnits"]:
            flags.add(f"{channel}_unexpected_unit_ignored")
            continue
        source_days = _source_days(source, days)
        for day in days:
            grouped[channel][unit_key][day].append(source_days[day])

    if ignored_channel_rows:
        flags.add("unknown_channel_rows_ignored")

    unit_days: Dict[str, Dict[str, Dict[str, Dict[str, object]]]] = {}
    for channel in CHANNELS:
        unit_days[channel] = {}
        for unit in specs[channel]["expectedUnits"]:
            unit_days[channel][unit] = {}
            for day in days:
                raw_rows = grouped[channel][unit][day]
                if len(raw_rows) > 1:
                    flags.add("duplicate_unit_sources_collapsed")
                unit_days[channel][unit][day] = _merge_unit_day(raw_rows)

    channel_day: Dict[str, Dict[str, Dict[str, object]]] = {channel: {} for channel in CHANNELS}
    channel_signal_values: Dict[str, Dict[str, float]] = {}
    channel_evidence_values: Dict[str, Dict[str, float]] = {}
    channel_reliability: Dict[str, Dict[str, float]] = {}
    channel_point_coverage: Dict[str, Dict[str, float]] = {}

    for channel in CHANNELS:
        expected = specs[channel]["expectedUnits"]
        expected_count = len(expected)
        allow_attention = bool(specs[channel]["allowAttention"])
        if not allow_attention:
            flags.add(f"{channel}_attention_disabled")
        if not any(grouped[channel][unit][day] for unit in expected for day in days):
            flags.add(f"{channel}_missing")
        if any(not any(grouped[channel][unit][day] for day in days) for unit in expected):
            flags.add(f"{channel}_missing_units")

        per_day_values: Dict[str, Dict[str, float]] = {}
        per_day_evidence: Dict[str, Dict[str, float]] = {}
        per_day_reliability: Dict[str, Dict[str, float]] = {}
        per_day_coverage: Dict[str, Dict[str, float]] = {}
        for day in days:
            values: Dict[str, float] = {}
            evidence: Dict[str, float] = {}
            reliabilities: Dict[str, float] = {}
            coverages: Dict[str, float] = {}
            observed_units = sum(
                1 for unit in expected if unit_days[channel][unit][day]["status"] == "observed"
            )
            verified_units = sum(
                1 for unit in expected if unit_days[channel][unit][day]["status"] == "verified_zero"
            )

            for signal in SIGNAL_KEYS:
                if signal == "attention" and not allow_attention:
                    values[signal] = SIGNAL_BASELINES[signal]
                    evidence[signal] = SIGNAL_BASELINES[signal]
                    reliabilities[signal] = 0.0
                    coverages[signal] = 0.0
                    continue
                available_units = [
                    unit for unit in expected
                    if signal in unit_days[channel][unit][day]["signals"]
                ]
                coverage = len(available_units) / expected_count
                deviation = sum(
                    float(unit_days[channel][unit][day]["signals"][signal]) - SIGNAL_BASELINES[signal]
                    for unit in available_units
                ) / expected_count
                # Shrink every fixed unit seat with *that unit's own* sample.
                # Applying one channel-level precision to the combined deviation
                # would let a high-n neutral unit lend precision to an unrelated
                # low-n extreme unit.  That is a hidden cross-unit reweighting.
                shrunk_deviation = sum(
                    (
                        float(unit_days[channel][unit][day]["signals"][signal])
                        - SIGNAL_BASELINES[signal]
                    )
                    * _precision(
                        unit_days[channel][unit][day]["signalSamples"].get(signal, 0)
                    )
                    for unit in available_units
                ) / expected_count
                evidence[signal] = _clamp(SIGNAL_BASELINES[signal] + deviation)
                values[signal] = _clamp(SIGNAL_BASELINES[signal] + shrunk_deviation)
                coverages[signal] = coverage
                reliabilities[signal] = sum(
                    _precision(unit_days[channel][unit][day]["signalSamples"].get(signal, 0))
                    for unit in available_units
                ) / expected_count

            if observed_units:
                status = "observed"
            elif verified_units:
                status = "verified_zero"
            else:
                status = "unobserved"
            channel_day[channel][day] = {
                "status": status,
                "observedUnitCount": observed_units,
                "verifiedZeroUnitCount": verified_units,
                "unobservedUnitCount": expected_count - observed_units - verified_units,
                "unitCoverage": observed_units / expected_count,
                "signals": values,
                "evidenceSignals": evidence,
                "signalReliability": reliabilities,
                "signalPointCoverage": coverages,
            }
            per_day_values[day] = values
            per_day_evidence[day] = evidence
            per_day_reliability[day] = reliabilities
            per_day_coverage[day] = coverages

        channel_signal_values[channel] = {
            signal: sum(weight * per_day_values[day][signal] for day, weight in zip(days, weights))
            for signal in SIGNAL_KEYS
        }
        channel_evidence_values[channel] = {
            signal: sum(weight * per_day_evidence[day][signal] for day, weight in zip(days, weights))
            for signal in SIGNAL_KEYS
        }
        channel_reliability[channel] = {
            signal: sum(weight * per_day_reliability[day][signal] for day, weight in zip(days, weights))
            for signal in SIGNAL_KEYS
        }
        channel_point_coverage[channel] = {
            signal: sum(weight * per_day_coverage[day][signal] for day, weight in zip(days, weights))
            for signal in SIGNAL_KEYS
        }

    final_signals: Dict[str, float] = {}
    evidence_signals: Dict[str, float] = {}
    signal_reliability: Dict[str, float] = {}
    for signal in SIGNAL_KEYS:
        baseline = SIGNAL_BASELINES[signal]
        final_signals[signal] = _round1(
            baseline + sum(
                CHANNEL_TARGET_WEIGHT * (channel_signal_values[channel][signal] - baseline)
                for channel in CHANNELS
            )
        )
        evidence_signals[signal] = _round1(
            baseline + sum(
                CHANNEL_TARGET_WEIGHT * (channel_evidence_values[channel][signal] - baseline)
                for channel in CHANNELS
            )
        )
        signal_reliability[signal] = sum(
            CHANNEL_TARGET_WEIGHT * channel_reliability[channel][signal]
            for channel in CHANNELS
        )

    final_signals["heat"] = _round1(
        final_signals["fomo"] * 0.30
        + final_signals["newcomer"] * 0.15
        + final_signals["intensity"] * 0.25
        + final_signals["attention"] * 0.30
    )
    evidence_signals["heat"] = _round1(
        evidence_signals["fomo"] * 0.30
        + evidence_signals["newcomer"] * 0.15
        + evidence_signals["intensity"] * 0.25
        + evidence_signals["attention"] * 0.30
    )

    channel_heats: Dict[str, float] = {}
    for channel in CHANNELS:
        values = channel_signal_values[channel]
        channel_heats[channel] = (
            values["fomo"] * 0.30
            + values["newcomer"] * 0.15
            + values["intensity"] * 0.25
            + values["attention"] * 0.30
        )
    hot_channels = sum(value > 50.0 for value in channel_heats.values())
    cold_channels = sum(value < 50.0 for value in channel_heats.values())
    breadth = _round1(50.0 + 50.0 * (hot_channels - cold_channels) / len(CHANNELS))

    channel_weights: List[Dict[str, object]] = []
    platform_summary: List[Dict[str, object]] = []
    for channel in CHANNELS:
        formal_influence = CHANNEL_TARGET_WEIGHT * max(channel_reliability[channel].values())
        average_reliability = sum(channel_reliability[channel].values()) / len(SIGNAL_KEYS)
        observed_units = sum(
            any(unit_days[channel][unit][day]["status"] == "observed" for day in days)
            for unit in specs[channel]["expectedUnits"]
        )
        weight_row = {
            "key": channel,
            "channel": channel,
            "targetWeight": CHANNEL_TARGET_WEIGHT,
            "formalInfluence": round(formal_influence, 6),
            "neutralisedWeight": round(CHANNEL_TARGET_WEIGHT - formal_influence, 6),
            "reliability": round(average_reliability, 6),
            "reliabilityPercent": round(average_reliability * 100.0, 1),
            "available": observed_units > 0,
        }
        channel_weights.append(weight_row)
        platform_summary.append({
            **weight_row,
            "mode": specs[channel]["mode"],
            "expectedUnitCount": len(specs[channel]["expectedUnits"]),
            "observedUnitCount": observed_units,
            "allowAttention": specs[channel]["allowAttention"],
            "emotionWeight": round(CHANNEL_TARGET_WEIGHT * channel_reliability[channel]["optimism"], 6),
            "attentionWeight": round(CHANNEL_TARGET_WEIGHT * channel_reliability[channel]["attention"], 6),
        })

    unit_weights: Dict[str, List[Dict[str, object]]] = {}
    for channel in CHANNELS:
        expected = specs[channel]["expectedUnits"]
        seat = CHANNEL_TARGET_WEIGHT / len(expected)
        unit_weights[channel] = []
        for unit in expected:
            effective = seat * sum(
                weight
                * _precision(unit_days[channel][unit][day]["signalSamples"].get("optimism", 0))
                * (1.0 if "optimism" in unit_days[channel][unit][day]["signals"] else 0.0)
                for day, weight in zip(days, weights)
            )
            unit_weights[channel].append({
                "key": unit,
                "mode": specs[channel]["mode"],
                "targetWithinChannel": round(1.0 / len(expected), 6),
                "targetFinalWeight": round(seat, 6),
                "formalInfluence": round(effective, 6),
                "neutralisedWeight": round(seat - effective, 6),
            })

    effective_days: List[Dict[str, object]] = []
    for day, weight in zip(days, weights):
        effective_days.append({
            "date": day,
            "weight": round(weight, 6),
            "channelTargetWeights": {channel: CHANNEL_TARGET_WEIGHT for channel in CHANNELS},
            "channelFormalInfluence": {
                channel: round(
                    CHANNEL_TARGET_WEIGHT
                    * weight
                    * channel_day[channel][day]["signalReliability"]["optimism"],
                    6,
                )
                for channel in CHANNELS
            },
            "channels": {
                channel: {
                    "status": channel_day[channel][day]["status"],
                    "observedUnitCount": channel_day[channel][day]["observedUnitCount"],
                    "unitCoverage": round(channel_day[channel][day]["unitCoverage"], 6),
                    "signalReliability": {
                        key: round(value, 6)
                        for key, value in channel_day[channel][day]["signalReliability"].items()
                    },
                }
                for channel in CHANNELS
            },
        })

    overall_reliability = sum(signal_reliability.values()) / len(SIGNAL_KEYS)
    if overall_reliability < 0.60:
        flags.add("low_reliability")
    observed_channel_count = sum(bool(row["available"]) for row in channel_weights)
    if observed_channel_count < len(CHANNELS):
        flags.add("missing_channels_neutralised")

    max_target = max(float(row["targetWeight"]) for row in channel_weights)
    max_formal = max(float(row["formalInfluence"]) for row in channel_weights)
    concentration = {
        "channelInfluenceCap": CHANNEL_TARGET_WEIGHT,
        "maxTargetChannelWeight": round(max_target, 6),
        "maxFormalChannelInfluence": round(max_formal, 6),
        "targetChannelHHI": round(sum(float(row["targetWeight"]) ** 2 for row in channel_weights), 6),
        "capBreached": (
            max_target > CHANNEL_TARGET_WEIGHT + 1e-12
            or max_formal > CHANNEL_TARGET_WEIGHT + 1e-12
        ),
    }

    unique_samples = 0
    for channel in CHANNELS:
        for unit in specs[channel]["expectedUnits"]:
            for day in days:
                unique_samples += int(unit_days[channel][unit][day]["sampleCount"])

    return {
        "available": observed_channel_count > 0,
        "modelVersion": MODEL_VERSION,
        "signals": final_signals,
        "evidenceSignals": evidence_signals,
        "scores": score_personas(final_signals, breadth),
        "breadth": breadth,
        "metrics": {
            "rawSampleCount": unique_samples,
            "effectiveSample": round(unique_samples * overall_reliability, 3),
            "observedChannelCount": observed_channel_count,
            "channelCoverage": round(observed_channel_count / len(CHANNELS), 6),
        },
        "diagnostics": {
            "method": "fixed_four_channels_then_fixed_units_then_fixed_50_30_20",
            "channelWeights": channel_weights,
            "unitWeights": unit_weights,
            "effectiveDayWeights": effective_days,
            "reliability": round(overall_reliability, 6),
            "reliabilityPercent": round(overall_reliability * 100.0, 1),
            "signalReliability": {
                key: round(value, 6) for key, value in signal_reliability.items()
            },
            "channelSignals": {
                channel: {key: _round1(value) for key, value in values.items()}
                for channel, values in channel_signal_values.items()
            },
            "channelPointCoverage": {
                channel: {key: round(value, 6) for key, value in values.items()}
                for channel, values in channel_point_coverage.items()
            },
            "platformSummary": platform_summary,
            "flags": sorted(flags),
            "concentration": concentration,
            "samplePrior": SAMPLE_PRIOR,
            "statusSemantics": {
                "verified_zero": "collector verified a complete stream with zero qualifying posts",
                "unobserved": "collector cannot determine whether qualifying posts existed",
            },
        },
    }


# A concise alias is useful at call sites while the longer name remains
# self-documenting for standalone use.
aggregate_channel_balanced = aggregate_channel_fairness


__all__ = [
    "CHANNELS",
    "CHANNEL_TARGET_WEIGHT",
    "DEFAULT_DAY_WEIGHTS",
    "MODEL_VERSION",
    "SAMPLE_PRIOR",
    "aggregate_channel_balanced",
    "aggregate_channel_fairness",
]
