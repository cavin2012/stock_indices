"""生成 V4 公司等权板块社区热度的可复核审计报告。

该脚本只读取已经生成的历史和最新快照 JSON，不联网、不读取行情。
正式排名稳定性只使用 ``rankEligible`` 板块，并且不跨 methodology
断点比较；公平性部分检查公司影响上限、同花顺正式情绪上限、可靠度、
公司覆盖和留一来源扰动。
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence

from fairness_model import (
    COMPANY_INFLUENCE_CAP,
    MODEL_VERSION as FAIRNESS_MODEL_VERSION,
    THS_EMOTION_CAP,
)
from sector_heat import HEAT_MODEL_VERSION


ROOT = Path(__file__).resolve().parent
DEFAULT_HISTORY = ROOT / "data" / "history.json"
DEFAULT_LATEST = ROOT / "data" / "latest.json"
DEFAULT_JSON = ROOT / "data" / "sector_heat_v4_fairness_audit.json"
DEFAULT_MARKDOWN = ROOT / "data" / "sector_heat_v4_fairness_audit.md"


def load_history(path: Path) -> List[Mapping[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, Mapping):
        payload = payload.get("history", [])
    if not isinstance(payload, list):
        raise ValueError(f"历史文件不是数组：{path}")
    return [row for row in payload if isinstance(row, Mapping)]


def load_latest(path: Path) -> Mapping[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"最新快照不是对象：{path}")
    return payload


def number(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, probability)) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def describe(values: Iterable[float]) -> Dict[str, float]:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    if not clean:
        return {key: 0.0 for key in ("min", "p10", "median", "mean", "p90", "max")}
    return {
        "min": round(min(clean), 2),
        "p10": round(quantile(clean, 0.10), 2),
        "median": round(statistics.median(clean), 2),
        "mean": round(statistics.mean(clean), 2),
        "p90": round(quantile(clean, 0.90), 2),
        "max": round(max(clean), 2),
    }


def available_sectors(point: Mapping[str, object]) -> List[Mapping[str, object]]:
    sectors = point.get("sectors", [])
    if not isinstance(sectors, list):
        return []
    return [
        item for item in sectors
        if isinstance(item, Mapping) and bool(item.get("available", True))
    ]


def sector_fairness(item: Mapping[str, object]) -> Mapping[str, object]:
    direct = item.get("fairness")
    if isinstance(direct, Mapping):
        return direct
    metrics = item.get("metrics")
    if isinstance(metrics, Mapping) and isinstance(metrics.get("fairness"), Mapping):
        return metrics["fairness"]
    return {}


def is_rank_eligible(item: Mapping[str, object]) -> bool:
    fairness = sector_fairness(item)
    if "rankEligible" in fairness:
        return fairness.get("rankEligible") is True
    model = item.get("heatModel")
    return bool(isinstance(model, Mapping) and model.get("rankEligible") is True)


def rank_eligible_sectors(point: Mapping[str, object]) -> List[Mapping[str, object]]:
    return [item for item in available_sectors(point) if is_rank_eligible(item)]


def value_range(sectors: Sequence[Mapping[str, object]], field: str) -> float:
    values = [number(item.get(field)) for item in sectors if item.get(field) is not None]
    return max(values) - min(values) if values else 0.0


def ranked_keys(point: Mapping[str, object]) -> List[str]:
    sectors = sorted(
        rank_eligible_sectors(point),
        key=lambda item: (number(item.get("heat")), str(item.get("key", ""))),
        reverse=True,
    )
    return [str(item.get("key")) for item in sectors if item.get("key")]


def pearson(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        return 0.0
    left_mean = statistics.mean(left)
    right_mean = statistics.mean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    denominator = math.sqrt(
        sum((a - left_mean) ** 2 for a in left)
        * sum((b - right_mean) ** 2 for b in right)
    )
    return numerator / denominator if denominator else 0.0


def methodology_of(point: Mapping[str, object]) -> str:
    return str(point.get("methodology") or "legacy_unspecified")


def rank_stability(history: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    ordered = sorted(history, key=lambda row: str(row.get("date", "")))
    top3_overlaps: List[float] = []
    rank_correlations: List[float] = []
    top1_retained = 0
    pairs = 0
    boundary_pairs_excluded = 0
    insufficient_pairs_excluded = 0
    eligible_counts = [len(rank_eligible_sectors(point)) for point in ordered]
    for previous, current in zip(ordered, ordered[1:]):
        if methodology_of(previous) != methodology_of(current):
            boundary_pairs_excluded += 1
            continue
        previous_keys = ranked_keys(previous)
        current_keys = ranked_keys(current)
        common = set(previous_keys) & set(current_keys)
        if len(common) < 2:
            insufficient_pairs_excluded += 1
            continue
        pairs += 1
        top_size = min(3, len(previous_keys), len(current_keys))
        top3_overlaps.append(
            len(set(previous_keys[:top_size]) & set(current_keys[:top_size])) / top_size
        )
        top1_retained += int(bool(previous_keys and current_keys and previous_keys[0] == current_keys[0]))
        previous_rank = {key: index + 1 for index, key in enumerate(previous_keys)}
        current_rank = {key: index + 1 for index, key in enumerate(current_keys)}
        common_keys = sorted(common)
        rank_correlations.append(pearson(
            [previous_rank[key] for key in common_keys],
            [current_rank[key] for key in common_keys],
        ))
    return {
        "rankingPopulation": "rankEligible sectors only; methodology boundaries excluded",
        "historyDays": len(ordered),
        "daysWithFormalRanking": sum(count >= 2 for count in eligible_counts),
        "rankEligibleSectorCount": describe(float(count) for count in eligible_counts),
        "adjacentPairs": pairs,
        "methodologyBoundaryPairsExcluded": boundary_pairs_excluded,
        "insufficientEligiblePairsExcluded": insufficient_pairs_excluded,
        "meanTop3OverlapPct": round(100.0 * statistics.mean(top3_overlaps), 1) if top3_overlaps else 0.0,
        "top1RetentionPct": round(100.0 * top1_retained / pairs, 1) if pairs else 0.0,
        "meanSpearmanLikeRankCorrelation": round(statistics.mean(rank_correlations), 3) if rank_correlations else 0.0,
        "status": "available" if pairs else "insufficient_rank_eligible_history",
    }


def adjacent_sector_changes(
    history: Sequence[Mapping[str, object]],
    field: str = "heat",
) -> List[float]:
    ordered = sorted(history, key=lambda row: str(row.get("date", "")))
    changes: List[float] = []
    for previous, current in zip(ordered, ordered[1:]):
        previous_map = {
            str(item.get("key")): item for item in available_sectors(previous) if item.get("key")
        }
        current_map = {
            str(item.get("key")): item for item in available_sectors(current) if item.get("key")
        }
        for key in set(previous_map) & set(current_map):
            if previous_map[key].get(field) is None or current_map[key].get(field) is None:
                continue
            changes.append(abs(
                number(current_map[key].get(field)) - number(previous_map[key].get(field))
            ))
    return changes


def activity_attention_diagnostics(
    history: Sequence[Mapping[str, object]],
) -> Dict[str, float]:
    activity_values: List[float] = []
    attention_values: List[float] = []
    identical = 0
    for point in history:
        for item in available_sectors(point):
            model = item.get("heatModel") if isinstance(item.get("heatModel"), Mapping) else {}
            signals = item.get("signals") if isinstance(item.get("signals"), Mapping) else {}
            if model.get("activityIndex") is None or signals.get("attention") is None:
                continue
            activity = number(model.get("activityIndex"))
            attention = number(signals.get("attention"))
            activity_values.append(activity)
            attention_values.append(attention)
            identical += int(abs(activity - attention) <= 1e-9)
    return {
        "sectorDays": len(activity_values),
        "correlation": round(pearson(activity_values, attention_values), 3),
        "exactlyIdenticalPct": round(100.0 * identical / len(activity_values), 2) if activity_values else 0.0,
    }


def history_by_date(history: Sequence[Mapping[str, object]]) -> Dict[str, Mapping[str, object]]:
    return {str(point.get("date")): point for point in history if point.get("date")}


def methodology_audit(history: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    ordered = sorted(
        (point for point in history if point.get("date")),
        key=lambda point: str(point.get("date")),
    )
    segments: List[Dict[str, object]] = []
    boundaries: List[Dict[str, object]] = []
    for point in ordered:
        day = str(point.get("date"))
        methodology = methodology_of(point)
        eligible_count = len(rank_eligible_sectors(point))
        if not segments or segments[-1]["methodology"] != methodology:
            if segments:
                boundaries.append({
                    "fromDate": segments[-1]["endDate"],
                    "atDate": day,
                    "fromMethodology": segments[-1]["methodology"],
                    "toMethodology": methodology,
                })
            segments.append({
                "methodology": methodology,
                "startDate": day,
                "endDate": day,
                "days": 1,
                "rankEligibleDays": int(eligible_count >= 2),
                "rankEligibleSectorDays": eligible_count,
            })
        else:
            segments[-1]["endDate"] = day
            segments[-1]["days"] = int(segments[-1]["days"]) + 1
            segments[-1]["rankEligibleDays"] = int(segments[-1]["rankEligibleDays"]) + int(eligible_count >= 2)
            segments[-1]["rankEligibleSectorDays"] = int(segments[-1]["rankEligibleSectorDays"]) + eligible_count
    return {
        "segments": segments,
        "boundaries": boundaries,
        "breakCount": len(boundaries),
        "latestMethodology": methodology_of(ordered[-1]) if ordered else None,
        "comparisonRule": "只比较同一 methodology 段内变化；断点两侧绝对值不直接比较。",
    }


def latest_sector_rows(latest: Mapping[str, object] | None) -> List[Mapping[str, object]]:
    if not isinstance(latest, Mapping):
        return []
    rows = latest.get("sectorIndices", [])
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping) and bool(row.get("available", True))]


def fairness_diagnostics(sectors: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    rows: List[Dict[str, object]] = []
    company_maximums: List[tuple[float, str]] = []
    ths_weights: List[tuple[float, str]] = []
    reliabilities: List[float] = []
    coverages: List[float] = []
    perturbations: List[tuple[float, str]] = []
    cap_breaches: List[str] = []

    for sector in sectors:
        fairness = sector_fairness(sector)
        if not fairness:
            continue
        name = str(sector.get("name") or sector.get("key") or "unknown")
        reliability = number(
            fairness.get("reliabilityPercent"),
            number(fairness.get("reliability")) * 100.0,
        )
        coverage = 100.0 * number(fairness.get("weightedCompanyCoverage"))
        stability = fairness.get("stabilityBand") if isinstance(fairness.get("stabilityBand"), Mapping) else {}
        max_delta = number(stability.get("maxDelta"))
        concentration = fairness.get("concentration") if isinstance(fairness.get("concentration"), Mapping) else {}
        company_max = number(concentration.get("maxFinalCompanyWeight"))
        if concentration.get("capBreached") is True:
            cap_breaches.append(name)

        observed_ths = 0.0
        platform_weights = fairness.get("platformWeights")
        if isinstance(platform_weights, Mapping):
            for company_days in platform_weights.values():
                if not isinstance(company_days, Mapping):
                    continue
                for platform_rows in company_days.values():
                    if not isinstance(platform_rows, list):
                        continue
                    for platform_row in platform_rows:
                        if isinstance(platform_row, Mapping) and platform_row.get("platform") == "ths":
                            observed_ths = max(observed_ths, number(platform_row.get("emotionWeight")))
        platform_summary = fairness.get("platformSummary")
        if isinstance(platform_summary, list):
            for platform_row in platform_summary:
                if isinstance(platform_row, Mapping) and platform_row.get("key") == "ths":
                    observed_ths = max(observed_ths, number(platform_row.get("emotionWeight")))

        reliabilities.append(reliability)
        coverages.append(coverage)
        perturbations.append((max_delta, name))
        company_maximums.append((company_max, name))
        ths_weights.append((observed_ths, name))
        rows.append({
            "key": str(sector.get("key") or ""),
            "name": name,
            "rankEligible": is_rank_eligible(sector),
            "reliabilityPercent": round(reliability, 1),
            "weightedCompanyCoveragePercent": round(coverage, 1),
            "maxFinalCompanyWeightPercent": round(company_max * 100.0, 2),
            "maxThsFormalEmotionWeightPercent": round(observed_ths * 100.0, 2),
            "leaveOneSourceOutMaxDelta": round(max_delta, 2),
            "confidenceLabel": fairness.get("confidenceLabel"),
        })

    maximum_company = max(company_maximums, default=(0.0, None))
    maximum_ths = max(ths_weights, default=(0.0, None))
    maximum_perturbation = max(perturbations, default=(0.0, None))
    return {
        "model": FAIRNESS_MODEL_VERSION,
        "sectorCount": len(rows),
        "rankEligibleSectorCount": sum(row["rankEligible"] for row in rows),
        "companyFormalWeight": {
            "configuredCapPercent": round(COMPANY_INFLUENCE_CAP * 100.0, 1),
            "observedMaximumPercent": round(maximum_company[0] * 100.0, 2),
            "observedMaximumSector": maximum_company[1],
            "capBreached": bool(cap_breaches),
            "breachSectors": cap_breaches,
        },
        "thsFormalEmotion": {
            "configuredPerCompanyDayCapPercent": round(THS_EMOTION_CAP * 100.0, 1),
            "observedMaximumPercent": round(maximum_ths[0] * 100.0, 2),
            "observedMaximumSector": maximum_ths[1],
            "formalVolumeAndAttentionWeightPercent": 0.0,
        },
        "reliabilityPercent": describe(reliabilities),
        "weightedCompanyCoveragePercent": describe(coverages),
        "leaveOneSourceOutMaxDelta": {
            **describe(value for value, _name in perturbations),
            "maximumSector": maximum_perturbation[1],
            "isStatisticalConfidenceInterval": False,
        },
        "sectors": sorted(rows, key=lambda row: (number(row["reliabilityPercent"]), row["name"])),
    }


def find_default_baseline() -> Path | None:
    candidates = sorted(
        (ROOT / "data" / "backups").glob("history_build_*/history.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    # 优先选取尚未包含新版字段的最后一份旧版历史，避免后续重复重建产生的
    # 新版备份被误当作“旧模型”对照。
    for candidate in candidates:
        try:
            history = load_history(candidate)
            if any(
                item.get("relativeHeat") is None
                for point in history
                for item in available_sectors(point)
            ):
                return candidate
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return candidates[0] if candidates else None


def build_audit(
    history: Sequence[Mapping[str, object]],
    baseline: Sequence[Mapping[str, object]] | None = None,
    latest: Mapping[str, object] | None = None,
) -> Dict[str, object]:
    ordered = sorted(history, key=lambda row: str(row.get("date", "")))
    daily_rows = []
    uses_market_price = False
    for point in ordered:
        sectors = available_sectors(point)
        models = [item.get("heatModel") for item in sectors if isinstance(item.get("heatModel"), Mapping)]
        uses_market_price = uses_market_price or any(bool(model.get("usesMarketPrice")) for model in models)
        gate = number(models[0].get("dispersionGate")) if models else 0.0
        top = ranked_keys(point)
        daily_rows.append({
            "date": str(point.get("date", "")),
            "methodology": methodology_of(point),
            "sectorCount": len(sectors),
            "rankEligibleSectorCount": len(rank_eligible_sectors(point)),
            "legacyRange": round(value_range(sectors, "legacyHeat"), 1),
            "relativeRange": round(value_range(sectors, "relativeHeat"), 1),
            "finalRange": round(value_range(sectors, "heat"), 1),
            "dispersionGate": round(gate, 3),
            "top3": top[:3],
        })

    result: Dict[str, object] = {
        "model": HEAT_MODEL_VERSION,
        "fairnessModel": FAIRNESS_MODEL_VERSION,
        "historyStart": daily_rows[0]["date"] if daily_rows else None,
        "historyEnd": daily_rows[-1]["date"] if daily_rows else None,
        "days": len(daily_rows),
        "daysWithDistinguishableSpread": sum(row["dispersionGate"] > 0 for row in daily_rows),
        "usesMarketPrice": uses_market_price,
        "legacyEmotionRange": describe(row["legacyRange"] for row in daily_rows),
        "relativeHeatRange": describe(row["relativeRange"] for row in daily_rows),
        "finalDisplayRange": describe(row["finalRange"] for row in daily_rows),
        "finalDisplayDailyChange": describe(adjacent_sector_changes(ordered, "heat")),
        "activityAttentionDiagnostics": activity_attention_diagnostics(ordered),
        "rankStability": rank_stability(ordered),
        "methodologyAudit": methodology_audit(ordered),
        "fairnessDiagnostics": fairness_diagnostics(latest_sector_rows(latest)),
        "daily": daily_rows,
    }

    if baseline is not None:
        baseline_dates = history_by_date(baseline)
        current_dates = history_by_date(history)
        paired_dates = sorted(set(baseline_dates) & set(current_dates))
        baseline_ranges = [
            value_range(available_sectors(baseline_dates[date]), "heat") for date in paired_dates
        ]
        current_ranges = [
            value_range(available_sectors(current_dates[date]), "heat") for date in paired_dates
        ]
        result["baselineComparison"] = {
            "pairedDays": len(paired_dates),
            "oldDisplayRange": describe(baseline_ranges),
            "newDisplayRange": describe(current_ranges),
            "oldDisplayDailyChange": describe(adjacent_sector_changes(baseline, "heat")),
            "newDisplayDailyChange": describe(adjacent_sector_changes(history, "heat")),
            "medianExpansionPoints": round(
                statistics.median(current_ranges) - statistics.median(baseline_ranges), 2
            ) if paired_dates else 0.0,
            "comparisonStatus": "descriptive_only_across_methodology_breaks",
            "note": "只保留展示热度范围的描述性对照；公司等权 V4 会改变正式估计，不做‘正式指数未变化’声明。",
        }
    return result


def render_markdown(audit: Mapping[str, object], baseline_path: Path | None) -> str:
    legacy = audit["legacyEmotionRange"]
    relative = audit["relativeHeatRange"]
    final = audit["finalDisplayRange"]
    daily_change = audit["finalDisplayDailyChange"]
    stability = audit["rankStability"]
    activity_check = audit["activityAttentionDiagnostics"]
    fairness = audit["fairnessDiagnostics"]
    methodology = audit["methodologyAudit"]
    company_weight = fairness["companyFormalWeight"]
    ths_weight = fairness["thsFormalEmotion"]
    reliability = fairness["reliabilityPercent"]
    company_coverage = fairness["weightedCompanyCoveragePercent"]
    perturbation = fairness["leaveOneSourceOutMaxDelta"]
    lines = [
        "# 板块社区热度 V4 公司等权公平性审计",
        "",
        f"- 历史区间：{audit['historyStart']} 至 {audit['historyEnd']}（{audit['days']} 个交易日）。",
        f"- 有统计区分度的日期：{audit['daysWithDistinguishableSpread']} / {audit['days']}。",
        f"- 使用行情或价格数据：{'是' if audit['usesMarketPrice'] else '否'}。",
        f"- 热度模型：`{audit['model']}`；公平聚合模型：`{audit['fairnessModel']}`。",
        "",
        "## 公司等权与来源公平性",
        "",
        f"- 正式公司权重上限：{company_weight['configuredCapPercent']}%；当前观察最大值 "
        f"{company_weight['observedMaximumPercent']}%（{company_weight['observedMaximumSector'] or '无可用快照'}）。",
        f"- 公司权重是否越限：{'是' if company_weight['capBreached'] else '否'}。",
        f"- 同花顺每家公司、每个交易日的正式情绪上限：{ths_weight['configuredPerCompanyDayCapPercent']}%；"
        f"当前观察最大值 {ths_weight['observedMaximumPercent']}%（{ths_weight['observedMaximumSector'] or '无可用快照'}）。",
        f"- 同花顺正式发帖量/关注度权重：{ths_weight['formalVolumeAndAttentionWeightPercent']}%。",
        f"- 板块可靠度：中位数 {reliability['median']}%，P10–P90 为 "
        f"{reliability['p10']}%–{reliability['p90']}%。",
        f"- 加权公司覆盖：中位数 {company_coverage['median']}%，P10–P90 为 "
        f"{company_coverage['p10']}%–{company_coverage['p90']}%。",
        f"- 留一来源最大扰动：中位数 {perturbation['median']} 点，P90 {perturbation['p90']} 点，"
        f"最大 {perturbation['max']} 点（{perturbation['maximumSector'] or '无'}）。",
        "- 留一来源扰动是稳健性压力测试，不是统计置信区间。",
        "",
        "## 板块间跨度",
        "",
        f"- 原情绪躁动值：每日跨度中位数 {legacy['median']}，P10–P90 为 {legacy['p10']}–{legacy['p90']}。",
        f"- 交易日相对热度：每日跨度中位数 {relative['median']}，P10–P90 为 {relative['p10']}–{relative['p90']}。",
        f"- 页面综合热度：每日跨度中位数 {final['median']}，P10–P90 为 {final['p10']}–{final['p90']}。",
        f"- 单板块相邻交易日变化：中位数 {daily_change['median']}，P90 {daily_change['p90']}，最大 {daily_change['max']}。",
        "",
        "## 排名稳定性",
        "",
        "- 正式排名只纳入 `rankEligible=true` 的板块，并排除 methodology 断点两侧的相邻日。",
        f"- 有正式排名的历史日：{stability['daysWithFormalRanking']} / {stability['historyDays']}；"
        f"可比较相邻日：{stability['adjacentPairs']} 对。",
        f"- 因方法断点排除 {stability['methodologyBoundaryPairsExcluded']} 对；"
        f"因合格板块不足排除 {stability['insufficientEligiblePairsExcluded']} 对。",
    ]
    if stability["status"] == "available":
        lines.extend([
            f"- 相邻交易日 Top 3 平均重合率：{stability['meanTop3OverlapPct']}%。",
            f"- 相邻交易日第一名保持率：{stability['top1RetentionPct']}%。",
            f"- 相邻交易日排名相关：{stability['meanSpearmanLikeRankCorrelation']}。",
        ])
    else:
        lines.append("- 当前同口径、具排名资格的连续历史不足，暂不报告稳定率百分比。")
    lines.extend([
        f"- 活跃速度与旧关注度相关：{activity_check['correlation']}；逐项完全相同比例 {activity_check['exactlyIdenticalPct']}%。",
        "",
        "## 方法断点",
        "",
        f"- 共识别 {methodology['breakCount']} 个方法断点；最新方法：`{methodology['latestMethodology']}`。",
    ])
    for boundary in methodology["boundaries"]:
        lines.append(
            f"- {boundary['fromDate']} → {boundary['atDate']}："
            f"`{boundary['fromMethodology']}` → `{boundary['toMethodology']}`。"
        )
    lines.append(f"- {methodology['comparisonRule']}")
    comparison = audit.get("baselineComparison")
    if isinstance(comparison, Mapping):
        old = comparison["oldDisplayRange"]
        new = comparison["newDisplayRange"]
        old_change = comparison["oldDisplayDailyChange"]
        new_change = comparison["newDisplayDailyChange"]
        lines.extend([
            "",
            "## 旧快照描述性对照",
            "",
            f"- 对照文件：`{baseline_path}`",
            f"- 同日期旧版跨度中位数：{old['median']}；当前版本：{new['median']}；差值 {comparison['medianExpansionPoints']} 点。",
            f"- 相邻交易日变化中位数/P90：旧版 {old_change['median']}/{old_change['p90']}，当前版本 {new_change['median']}/{new_change['p90']}。",
            f"- {comparison['note']}",
        ])
    lines.extend([
        "",
        "## 判读",
        "",
        "V4 先按平台和交易日汇总到公司，再按公司等权汇总板块；缺失公司席位保留中性权重，"
        "不会把缺失权重重新分给剩余公司。低可靠度或公司覆盖不足的板块不进入正式排名。"
        "公司等权重构会改变正式估计，因此本报告不再声称宝妈、小白、散户或综合指数保持不变。",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="审计 V4 公司等权板块社区热度的公平性与稳定性")
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--latest", type=Path, default=DEFAULT_LATEST)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    args = parser.parse_args()

    baseline_path = args.baseline or find_default_baseline()
    history = load_history(args.history)
    latest = load_latest(args.latest) if args.latest.exists() else None
    baseline = load_history(baseline_path) if baseline_path and baseline_path.exists() else None
    audit = build_audit(history, baseline, latest)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown.write_text(render_markdown(audit, baseline_path), encoding="utf-8")
    print(f"已生成审计：{args.markdown}")


if __name__ == "__main__":
    main()
