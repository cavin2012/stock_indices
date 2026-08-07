"""对 60 日社区情绪回溯数据做只读、可重复的数据质量审计。

本工具不联网、不修改 SQLite，也不改写指数页面数据。它复用正式构建器的
同一套历史评分入口，在一个内存快照上检查：

* 日期、平台、来源和板块覆盖是否均衡；
* 指数极端日是否与样本量/采集进度高度绑定；
* 宝妈、小白、散户三条指数是否有足够区分度；
* 生猪养殖等农业子板块高分来自方向、热度还是语境误判；
* coverage / confidence 是否诚实反映档案缺口。

运行：
    python data_quality_report.py

只在终端预览、不写报告文件：
    python data_quality_report.py --no-write --format markdown
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import build_history_60d as history_builder
import update_index as core
from history_engine import CN_TZ


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DEFAULT_DB = DATA_DIR / "history_60d.sqlite3"
DEFAULT_JSON = DATA_DIR / "history_60d_quality_report.json"
DEFAULT_MARKDOWN = DATA_DIR / "history_60d_quality_report.md"
REPORT_SCHEMA_VERSION = 1
PERSONA_KEYS = ("baoma", "novice", "retail")
METRIC_KEYS = (
    "baoma", "novice", "retail", "composite", "heat", "direction",
    "coverage", "confidence", "samplePosts", "platformCount",
    "sourceCount", "sectorCount",
)
PLATFORM_ORDER = tuple(core.PLATFORM_NAMES)

# 这些表达中含有被词典单独视为正向的词，但整句通常是看空、否定或换仓语境。
# 它不是另一套情绪模型，只用于找出值得人工复核的“正分但疑似负向”样例。
AGRICULTURE_CONTEXT_RISK = re.compile(
    r"突破不了|放量下杀|下探|破前底|反弹不是反转|快跑|跑路|出货|"
    r"卖出.{0,20}(?:买入|换入|换成)|不要.{0,12}(?:抄底|买入|加仓)|"
    r"没有龙头|不再.{0,12}(?:看多|喊多)"
)


def round_value(value: float, digits: int = 2) -> float:
    return round(float(value), digits)


def percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    """线性插值分位数；空序列返回 None。"""

    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, float(fraction))) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def describe(values: Iterable[float]) -> Dict[str, object]:
    """生成稳定、JSON 友好的描述统计。"""

    numbers = [float(value) for value in values]
    if not numbers:
        return {"count": 0}
    mean = statistics.mean(numbers)
    stdev = statistics.pstdev(numbers)
    return {
        "count": len(numbers),
        "min": round_value(min(numbers)),
        "p10": round_value(percentile(numbers, 0.10) or 0.0),
        "p25": round_value(percentile(numbers, 0.25) or 0.0),
        "median": round_value(statistics.median(numbers)),
        "p75": round_value(percentile(numbers, 0.75) or 0.0),
        "p90": round_value(percentile(numbers, 0.90) or 0.0),
        "max": round_value(max(numbers)),
        "mean": round_value(mean),
        "stdev": round_value(stdev),
        "coefficientOfVariation": round_value(stdev / abs(mean), 3) if mean else None,
    }


def pearson(left: Sequence[float], right: Sequence[float]) -> Optional[float]:
    if len(left) != len(right) or len(left) < 2:
        return None
    x_values = [float(value) for value in left]
    y_values = [float(value) for value in right]
    x_mean = statistics.mean(x_values)
    y_mean = statistics.mean(y_values)
    numerator = sum(
        (x_value - x_mean) * (y_value - y_mean)
        for x_value, y_value in zip(x_values, y_values)
    )
    denominator = math.sqrt(
        sum((value - x_mean) ** 2 for value in x_values)
        * sum((value - y_mean) ** 2 for value in y_values)
    )
    if denominator <= 0:
        return None
    return round_value(numerator / denominator, 3)


def date_sequence(start: date, end: date) -> List[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def source_progress(archive_meta: Mapping[str, object]) -> Dict[str, object]:
    by_platform: Dict[str, Counter] = defaultdict(Counter)
    request_totals: Counter = Counter()
    for raw in archive_meta.get("sourceStates", []):
        if not isinstance(raw, Mapping):
            continue
        platform = str(raw.get("platform") or "unknown")
        status = str(raw.get("status") or "unknown")
        by_platform[platform][status] += 1
        request_totals[platform] += int(raw.get("totalRequests") or 0)
    return {
        platform: {
            "statusCounts": dict(sorted(counts.items())),
            "totalRequests": request_totals[platform],
            "sourceCount": sum(counts.values()),
        }
        for platform, counts in sorted(by_platform.items())
    }


def analyze_raw_coverage(
    results: Mapping[str, Mapping[str, object]],
    sector_source_keys: Mapping[str, Sequence[str]],
    start: date,
    end: date,
) -> Dict[str, object]:
    """按帖子真实发布日期审计覆盖，不把额外档案余量算成当日发布。"""

    expected_days = date_sequence(start, end)
    daily_posts: Dict[date, Counter] = {day: Counter() for day in expected_days}
    daily_sources: Dict[date, Dict[str, set]] = {
        day: defaultdict(set) for day in expected_days
    }
    platform_sources_configured: Counter = Counter()
    platform_sources_with_posts: Dict[str, set] = defaultdict(set)
    platform_active_days: Dict[str, set] = defaultdict(set)
    source_window_posts: Counter = Counter()

    for source_key, source in results.items():
        platform = str(source.get("platform") or "unknown")
        platform_sources_configured[platform] += 1
        for post in source.get("posts", []):
            published = post.get("published") if isinstance(post, Mapping) else None
            if not isinstance(published, datetime):
                continue
            day = published.astimezone(CN_TZ).date()
            if not (start <= day <= end):
                continue
            daily_posts[day][platform] += 1
            daily_sources[day][platform].add(source_key)
            platform_sources_with_posts[platform].add(source_key)
            platform_active_days[platform].add(day)
            source_window_posts[source_key] += 1

    platform_keys = list(dict.fromkeys(
        list(PLATFORM_ORDER)
        + sorted(platform_sources_configured)
        + sorted(platform_sources_with_posts)
    ))
    rows: List[Dict[str, object]] = []
    for day in expected_days:
        counts = {platform: int(daily_posts[day][platform]) for platform in platform_keys}
        source_counts = {
            platform: len(daily_sources[day].get(platform, set()))
            for platform in platform_keys
        }
        active_platforms = [platform for platform, count in counts.items() if count > 0]
        rows.append({
            "date": day.isoformat(),
            "posts": counts,
            "sourceCounts": source_counts,
            "totalPosts": sum(counts.values()),
            "activePlatformCount": len(active_platforms),
            "activePlatforms": active_platforms,
        })

    platform_summary: Dict[str, Dict[str, object]] = {}
    for platform in platform_keys:
        values = [int(row["posts"][platform]) for row in rows]
        platform_summary[platform] = {
            "name": core.PLATFORM_NAMES.get(platform, platform),
            "configuredSources": int(platform_sources_configured[platform]),
            "sourcesWithPostsInWindow": len(platform_sources_with_posts[platform]),
            "postsInWindow": sum(values),
            "activeDays": len(platform_active_days[platform]),
            "dailyPosts": describe(values),
        }

    sector_source_counts: Dict[str, Dict[str, object]] = {}
    for sector_key, source_keys in sector_source_keys.items():
        raw_by_platform: Counter = Counter()
        raw_by_source: Dict[str, int] = {}
        for source_key in source_keys:
            source = results.get(source_key, {})
            platform = str(source.get("platform") or "unknown")
            count = int(source_window_posts[source_key])
            raw_by_platform[platform] += count
            raw_by_source[source_key] = count
        total = sum(raw_by_source.values())
        largest = max(raw_by_source.values()) if raw_by_source else 0
        sector_source_counts[sector_key] = {
            "rawPosts": total,
            "rawPostsByPlatform": dict(sorted(raw_by_platform.items())),
            "rawPostsBySource": dict(sorted(
                raw_by_source.items(), key=lambda item: (-item[1], item[0])
            )),
            "topSourceSharePct": round_value(largest / total * 100.0, 1) if total else None,
        }

    total_values = [int(row["totalPosts"]) for row in rows]
    active_platform_counts = [int(row["activePlatformCount"]) for row in rows]
    return {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "expectedDays": len(rows),
        "activeRawDays": sum(value > 0 for value in total_values),
        "zeroRawDays": [row["date"] for row in rows if int(row["totalPosts"]) == 0],
        "singlePlatformRawDays": sum(value == 1 for value in active_platform_counts),
        "multiPlatformRawDays": sum(value >= 2 for value in active_platform_counts),
        "platformsWithAnyPosts": [
            platform for platform in platform_keys
            if int(platform_summary[platform]["postsInWindow"]) > 0
        ],
        "dailyTotalPosts": describe(total_values),
        "platformSummary": platform_summary,
        "daily": rows,
        "sectorRawCoverage": sector_source_counts,
    }


def analyze_persona_differentiation(points: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    triples = [tuple(float(point[key]) for key in PERSONA_KEYS) for point in points]
    ranges = [max(values) - min(values) for values in triples]
    pairwise = {
        "baomaVsNovice": [abs(values[0] - values[1]) for values in triples],
        "baomaVsRetail": [abs(values[0] - values[2]) for values in triples],
        "noviceVsRetail": [abs(values[1] - values[2]) for values in triples],
    }
    exact_equal = sum(len(set(values)) == 1 for values in triples)
    return {
        "days": len(points),
        "meanScores": {
            key: round_value(statistics.mean(float(point[key]) for point in points))
            if points else None
            for key in PERSONA_KEYS
        },
        "dailyRange": describe(ranges),
        "exactlyEqualDays": exact_equal,
        "exactlyEqualPct": round_value(exact_equal / len(points) * 100.0, 1) if points else None,
        "rangeAtMostOneDays": sum(value <= 1.0 for value in ranges),
        "rangeAtMostOnePct": round_value(
            sum(value <= 1.0 for value in ranges) / len(points) * 100.0, 1
        ) if points else None,
        "uniqueTriples": len(set(triples)),
        "pairwiseAbsoluteDifference": {
            name: describe(values) for name, values in pairwise.items()
        },
    }


def outlier_dates(points: Sequence[Mapping[str, object]], metric: str) -> List[Dict[str, object]]:
    values = [float(point[metric]) for point in points]
    if len(values) < 4:
        return []
    first = percentile(values, 0.25)
    third = percentile(values, 0.75)
    if first is None or third is None:
        return []
    spread = third - first
    lower = first - 1.5 * spread
    upper = third + 1.5 * spread
    return [
        {"date": str(point["date"]), "value": point[metric], "fence": "high" if value > upper else "low"}
        for point, value in zip(points, values)
        if value < lower or value > upper
    ]


def analyze_history(points: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    metric_summary = {
        key: describe(float(point[key]) for point in points)
        for key in METRIC_KEYS
    }
    composite = [float(point["composite"]) for point in points]
    samples = [float(point["samplePosts"]) for point in points]
    sources = [float(point["sourceCount"]) for point in points]
    heat = [float(point["heat"]) for point in points]
    direction = [float(point["direction"]) for point in points]
    ordinal = [float(index) for index in range(len(points))]
    correlations = {
        "compositeVsSamplePosts": pearson(composite, samples),
        "heatVsSamplePosts": pearson(heat, samples),
        "compositeVsSourceCount": pearson(composite, sources),
        "compositeVsHeat": pearson(composite, heat),
        "compositeVsDirection": pearson(composite, direction),
        "samplePostsVsDateOrder": pearson(samples, ordinal),
        "compositeVsDateOrder": pearson(composite, ordinal),
    }
    sorted_high = sorted(points, key=lambda item: float(item["composite"]), reverse=True)
    sorted_low = sorted(points, key=lambda item: float(item["composite"]))
    daily_moves = [
        {
            "date": str(current["date"]),
            "previousDate": str(previous["date"]),
            "change": round_value(float(current["composite"]) - float(previous["composite"]), 1),
        }
        for previous, current in zip(points, points[1:])
    ]
    daily_moves.sort(key=lambda item: abs(float(item["change"])), reverse=True)

    def compact_day(point: Mapping[str, object]) -> Dict[str, object]:
        return {
            "date": point["date"],
            "composite": point["composite"],
            "baoma": point["baoma"],
            "novice": point["novice"],
            "retail": point["retail"],
            "heat": point["heat"],
            "direction": point["direction"],
            "samplePosts": point["samplePosts"],
            "platformCount": point["platformCount"],
            "sourceCount": point["sourceCount"],
            "coverage": point["coverage"],
            "confidence": point["confidence"],
        }

    return {
        "days": len(points),
        "startDate": str(points[0]["date"]) if points else None,
        "endDate": str(points[-1]["date"]) if points else None,
        "metrics": metric_summary,
        "correlations": correlations,
        "highestCompositeDays": [compact_day(point) for point in sorted_high[:5]],
        "lowestCompositeDays": [compact_day(point) for point in sorted_low[:5]],
        "largestDayOverDayMoves": daily_moves[:5],
        "iqrOutliers": {
            metric: outlier_dates(points, metric)
            for metric in ("composite", "heat", "direction", "samplePosts", "coverage")
        },
        "personaDifferentiation": analyze_persona_differentiation(points),
        "daily": [compact_day(point) for point in points],
    }


def analyze_sectors(
    points: Sequence[Mapping[str, object]],
    raw_coverage: Mapping[str, object],
) -> List[Dict[str, object]]:
    sector_keys = list(core.SECTORS)
    observations: Dict[str, List[Tuple[Mapping[str, object], Mapping[str, object], Optional[int]]]] = {
        key: [] for key in sector_keys
    }
    for point in points:
        sectors = [item for item in point.get("sectors", []) if isinstance(item, Mapping)]
        available = [item for item in sectors if bool(item.get("available"))]
        ranks = {
            str(item.get("key")): 1 + sum(
                float(other.get("scores", {}).get("composite", 0))
                > float(item.get("scores", {}).get("composite", 0))
                for other in available
            )
            for item in available
        }
        by_key = {str(item.get("key")): item for item in sectors}
        for sector_key in sector_keys:
            item = by_key.get(sector_key)
            if item is not None:
                observations[sector_key].append((point, item, ranks.get(sector_key)))

    raw_by_sector = raw_coverage.get("sectorRawCoverage", {})
    report_rows: List[Dict[str, object]] = []
    for sector_key in sector_keys:
        rows = observations[sector_key]
        available_rows = [(point, item, rank) for point, item, rank in rows if bool(item.get("available"))]
        composites = [float(item.get("scores", {}).get("composite", 0)) for _, item, _ in available_rows]
        persona_ranges = [
            max(float(item.get("scores", {}).get(key, 0)) for key in PERSONA_KEYS)
            - min(float(item.get("scores", {}).get(key, 0)) for key in PERSONA_KEYS)
            for _, item, _ in available_rows
        ]
        ranks = [float(rank) for _, _, rank in available_rows if rank is not None]
        top_days = sorted(
            available_rows,
            key=lambda row: float(row[1].get("scores", {}).get("composite", 0)),
            reverse=True,
        )[:5]
        raw = raw_by_sector.get(sector_key, {}) if isinstance(raw_by_sector, Mapping) else {}
        report_rows.append({
            "key": sector_key,
            "name": str(core.SECTORS[sector_key]["name"]),
            "daysAvailable": len(available_rows),
            "availabilityPct": round_value(len(available_rows) / len(points) * 100.0, 1) if points else None,
            "score": describe(composites),
            "heat": describe(float(item.get("heat", 0)) for _, item, _ in available_rows),
            "direction": describe(float(item.get("direction", 0)) for _, item, _ in available_rows),
            "samplePosts": describe(float(item.get("samplePosts", 0)) for _, item, _ in available_rows),
            "platformCount": describe(float(item.get("platformCount", 0)) for _, item, _ in available_rows),
            "sourceCount": describe(float(item.get("sourceCount", 0)) for _, item, _ in available_rows),
            "coverage": describe(float(item.get("coverage", 0)) for _, item, _ in available_rows),
            "confidence": describe(float(item.get("confidence", 0)) for _, item, _ in available_rows),
            "rank": describe(ranks),
            "topThreeDays": sum(rank is not None and rank <= 3 for _, _, rank in available_rows),
            "personaDailyRange": describe(persona_ranges),
            "rawPosts": raw.get("rawPosts"),
            "rawPostsByPlatform": raw.get("rawPostsByPlatform", {}),
            "rawPostsBySource": raw.get("rawPostsBySource", {}),
            "topSourceSharePct": raw.get("topSourceSharePct"),
            "highestDays": [
                {
                    "date": point["date"],
                    "score": item.get("scores", {}).get("composite"),
                    "heat": item.get("heat"),
                    "direction": item.get("direction"),
                    "samplePosts": item.get("samplePosts"),
                    "platformCount": item.get("platformCount"),
                    "rank": rank,
                }
                for point, item, rank in top_days
            ],
        })
    return sorted(
        report_rows,
        key=lambda item: float(item.get("rank", {}).get("mean", 999.0)),
    )


def agriculture_context_risk_examples(
    results: Mapping[str, Mapping[str, object]],
    source_keys: Sequence[str],
    start: date,
    end: date,
    limit: int = 8,
) -> List[Dict[str, object]]:
    examples: List[Dict[str, object]] = []
    seen = set()
    for source_key in source_keys:
        source = results.get(source_key, {})
        for post in source.get("posts", []):
            if not isinstance(post, Mapping):
                continue
            published = post.get("published")
            title = str(post.get("title") or "").strip()
            if not isinstance(published, datetime) or not (start <= published.date() <= end):
                continue
            normalized = core.normalize_text(title)
            if not normalized or normalized in seen or not AGRICULTURE_CONTEXT_RISK.search(title):
                continue
            raw_score = core.sentiment_score(title)
            if raw_score <= 0:
                continue
            seen.add(normalized)
            examples.append({
                "date": published.astimezone(CN_TZ).date().isoformat(),
                "source": str(source.get("name") or source_key),
                "title": title,
                "lexiconScore": raw_score,
                "reason": "句中含否定/下杀/换仓语境，但当前词典净分仍为正",
            })
    examples.sort(key=lambda item: (-int(item["lexiconScore"]), str(item["date"])), reverse=False)
    return examples[:limit]


def build_alerts(report: Mapping[str, object]) -> List[Dict[str, object]]:
    alerts: List[Dict[str, object]] = []
    raw = report["rawCoverage"]
    history = report["historyDistribution"]
    snapshot = report["snapshot"]
    personas = history["personaDifferentiation"]
    sectors = report["sectors"]
    agriculture = report["agricultureAudit"]

    def add(severity: str, code: str, title: str, evidence: str, recommendation: str) -> None:
        alerts.append({
            "severity": severity,
            "code": code,
            "title": title,
            "evidence": evidence,
            "recommendation": recommendation,
        })

    scored_platforms = list(raw.get("platformsWithAnyPosts", []))
    if len(scored_platforms) < 2:
        add(
            "critical", "SCORED_WINDOW_SINGLE_PLATFORM",
            "60 日发布数据尚未形成跨平台验证",
            f"评分窗口内有帖的平台仅 {scored_platforms or '无'}；"
            f"{raw.get('singlePlatformRawDays')} 个活跃自然日只有单一平台。",
            "继续采集到新浪目标日期进入评分窗口；在此之前把整段历史明确标为单平台估算。",
        )

    ths_posts = int(raw.get("platformSummary", {}).get("ths", {}).get("postsInWindow", 0))
    if ths_posts == 0:
        add(
            "high", "THS_HISTORY_MISSING",
            "同花顺没有进入历史回溯档案",
            "同花顺在评分窗口的历史帖子数为 0；当前历史采集库只配置东财和新浪来源。",
            "若公开接口稳定且条款允许，补充同花顺历史文本；否则在页面来源说明中持续披露该缺口。",
        )

    completeness = float(snapshot.get("archiveCompletenessPct") or 0)
    if completeness < 70.0:
        add(
            "high", "ARCHIVE_INCOMPLETE",
            "历史翻页进度仍不足以定稿",
            f"构建器估算档案完整度 {completeness:.1f}%。",
            "保持断点续采，至少达到 70% 后再把 60 日曲线视为可比较序列。",
        )

    volume = raw.get("dailyTotalPosts", {})
    median_volume = float(volume.get("median") or 0)
    max_volume = float(volume.get("max") or 0)
    if median_volume > 0 and max_volume / median_volume >= 8.0:
        add(
            "high", "DAILY_VOLUME_IMBALANCE",
            "不同日期的可见帖子量高度不均",
            f"单日帖子中位数 {median_volume:.0f}，最大值 {max_volume:.0f}，"
            f"相差 {max_volume / median_volume:.1f} 倍；零帖子日 {len(raw.get('zeroRawDays', []))} 天。",
            "按日期检查来源翻页深度，增加日/来源权重上限，并把发布量覆盖单独展示。",
        )

    sample_correlation = history.get("correlations", {}).get("compositeVsSamplePosts")
    if sample_correlation is not None and abs(float(sample_correlation)) >= 0.65:
        add(
            "high", "SCORE_VOLUME_COUPLING",
            "综合分与样本量高度同步",
            f"综合分与 samplePosts 的皮尔逊相关系数为 {float(sample_correlation):.3f}。",
            "区分“讨论热度”和“多空方向”，对 attention 的量级效应做时间截面标准化，并做去量级敏感性回测。",
        )

    mean_range = float(personas.get("dailyRange", {}).get("mean") or 0)
    same_or_close = float(personas.get("rangeAtMostOnePct") or 0)
    if mean_range < 2.0 or same_or_close >= 60.0:
        add(
            "high", "PERSONA_LOW_SEPARATION",
            "宝妈、小白、散户三条指数区分度不足",
            f"三类指数日均最大差仅 {mean_range:.2f} 分；{same_or_close:.1f}% 的日期相差不超过 1 分。",
            "后续应先识别真实行为语料/提问模式，再聚合各人群；仅对同一组信号换权重很难形成独立指数。",
        )

    sector_sample_medians = [
        float(item.get("samplePosts", {}).get("median") or 0)
        for item in sectors if float(item.get("samplePosts", {}).get("median") or 0) > 0
    ]
    if sector_sample_medians and max(sector_sample_medians) / min(sector_sample_medians) >= 5.0:
        add(
            "medium", "SECTOR_SAMPLE_IMBALANCE",
            "板块之间样本规模差异较大",
            f"板块滚动样本中位数从 {min(sector_sample_medians):.0f} 到 {max(sector_sample_medians):.0f}，"
            f"相差 {max(sector_sample_medians) / min(sector_sample_medians):.1f} 倍。",
            "板块排名旁同时显示样本与置信度，并对低样本板块避免精确名次叙事。",
        )

    agriculture_rank = float(agriculture.get("rank", {}).get("mean") or 99)
    agriculture_platforms = float(agriculture.get("platformCount", {}).get("median") or 0)
    risk_count = len(agriculture.get("positiveScoreContextRisks", []))
    agriculture_name = str(agriculture.get("name") or "农业子板块")
    if agriculture_rank <= 6.0 and agriculture_platforms <= 1.0:
        add(
            "high", "AGRICULTURE_SINGLE_PLATFORM_HIGH_RANK",
            f"{agriculture_name}的靠前排名缺少跨平台确认",
            f"{agriculture_name}平均排名 {agriculture_rank:.1f}，但滚动平台数中位数仅 {agriculture_platforms:.0f}；"
            f"另发现 {risk_count} 条正分但疑似负向语境样例。",
            "在新浪覆盖进入窗口前降低结论强度；补充否定、转折、换仓和标的指向识别。",
        )

    confidence = history.get("metrics", {}).get("confidence", {})
    add(
        "info", "CONFIDENCE_CONSERVATIVE",
        "置信度没有掩盖数据缺口",
        f"60 日置信度范围 {confidence.get('min')}–{confidence.get('max')}，中位数 {confidence.get('median')}；"
        "回溯估算上限与档案完整度已进入计算。",
        "保留当前保守口径；只有平台、来源、日期覆盖改善后再让置信度自然上升。",
    )

    order = {"critical": 0, "high": 1, "medium": 2, "info": 3}
    return sorted(alerts, key=lambda item: (order[item["severity"]], item["code"]))


def build_quality_report(
    db_path: Path,
    results: Mapping[str, Mapping[str, object]],
    market_keys: Sequence[str],
    sector_source_keys: Mapping[str, Sequence[str]],
    archive_meta: Mapping[str, object],
    points: Sequence[Mapping[str, object]],
    skipped: Sequence[Mapping[str, object]],
    start: date,
    requested_end: date,
    estimated_end: date,
    partial_day: Optional[date],
) -> Dict[str, object]:
    raw_coverage = analyze_raw_coverage(results, sector_source_keys, start, estimated_end)
    history = analyze_history(points)
    sectors = analyze_sectors(points, raw_coverage)
    agriculture_keys = [
        key for key, definition in core.SECTORS.items()
        if str(definition.get("parentKey") or "") == "agriculture"
    ]
    focus_key = "hog_farming" if "hog_farming" in agriculture_keys else (agriculture_keys[0] if agriculture_keys else "")
    agriculture = next(
        (dict(item) for item in sectors if item.get("key") == focus_key),
        {
            "key": focus_key or "agriculture_unavailable",
            "name": str(core.SECTORS.get(focus_key, {}).get("name") or "农业子板块"),
            "score": {}, "rank": {}, "heat": {}, "direction": {},
            "platformCount": {}, "highestDays": [],
        },
    )
    agriculture["positiveScoreContextRisks"] = agriculture_context_risk_examples(
        results,
        [
            source_key
            for sector_key in agriculture_keys
            for source_key in sector_source_keys.get(sector_key, [])
        ],
        start,
        estimated_end,
    )
    database_stat = db_path.stat()
    report: Dict[str, object] = {
        "schemaVersion": REPORT_SCHEMA_VERSION,
        "generatedAt": datetime.now(CN_TZ).isoformat(),
        "status": "pending",
        "snapshot": {
            "database": str(db_path),
            "databaseBytes": database_stat.st_size,
            "databaseModifiedAt": datetime.fromtimestamp(database_stat.st_mtime, CN_TZ).isoformat(),
            "collectorAsOf": archive_meta.get("windowAsOf"),
            "rawCutoff": archive_meta.get("rawCutoff"),
            "requestedStartDate": start.isoformat(),
            "requestedEndDate": requested_end.isoformat(),
            "estimatedEndDate": estimated_end.isoformat(),
            "partialCollectorDateExcluded": partial_day.isoformat() if partial_day else None,
            "postCountLoaded": int(archive_meta.get("postCount") or 0),
            "sourceCount": int(archive_meta.get("sourceCount") or 0),
            "marketSourceCount": len(market_keys),
            "archiveCompletenessPct": round_value(
                history_builder.archive_completeness(archive_meta) * 100.0, 1
            ),
            "collectionStrategy": archive_meta.get("strategy"),
            "collectorProgress": source_progress(archive_meta),
            "historyPointsBuilt": len(points),
            "skippedDays": list(skipped),
        },
        "rawCoverage": raw_coverage,
        "historyDistribution": history,
        "sectors": sectors,
        "agricultureAudit": agriculture,
        "confidenceAudit": {
            "formulaInterpretation": (
                "coverage 衡量平台/来源/板块/样本/档案完整度；confidence 对回溯偏差再次折价，"
                "当前历史估算即使覆盖完整也不超过 85。"
            ),
            "coverage": history.get("metrics", {}).get("coverage", {}),
            "confidence": history.get("metrics", {}).get("confidence", {}),
            "daysBelow40": sum(float(point.get("confidence", 0)) < 40 for point in points),
            "daysBelow50": sum(float(point.get("confidence", 0)) < 50 for point in points),
            "daysAtLeast55": sum(float(point.get("confidence", 0)) >= 55 for point in points),
        },
        "methodNotes": [
            "历史点由 build_history_60d.py 与正式页面相同的 score_results_as_of 路径重算。",
            "原始日期覆盖按帖子 published_at 统计；额外档案余量只用于恢复交易日窗口，不直接计入目标日。",
            "历史互动量被固定忽略，避免用采集时累计评论/点赞回看过去。",
            "相关性用于发现潜在耦合，不单独证明因果或评分错误。",
            "农业语境风险样例是规则筛查，必须人工复核，不能当成新模型标签。",
        ],
    }
    report["alerts"] = build_alerts(report)
    critical = sum(item["severity"] == "critical" for item in report["alerts"])
    high = sum(item["severity"] == "high" for item in report["alerts"])
    report["status"] = (
        "needs_more_collection" if critical
        else "usable_with_caution" if high
        else "ready"
    )
    report["alertCounts"] = dict(Counter(item["severity"] for item in report["alerts"]))
    return report


def markdown_escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: Mapping[str, object]) -> str:
    snapshot = report["snapshot"]
    raw = report["rawCoverage"]
    history = report["historyDistribution"]
    personas = history["personaDifferentiation"]
    agriculture = report["agricultureAudit"]
    lines = [
        "# 宝妈·小白·散户指数：60 日数据质量审计",
        "",
        f"> 结论状态：`{report['status']}`。本报告只读分析，不修改采集库与指数计算。",
        "",
        "## 审计快照",
        "",
        "| 项目 | 结果 |",
        "|---|---:|",
        f"| 生成时间 | {markdown_escape(report['generatedAt'])} |",
        f"| 历史窗口 | {snapshot['requestedStartDate']} 至 {snapshot['estimatedEndDate']} |",
        f"| 重算点数 | {snapshot['historyPointsBuilt']} |",
        f"| 已加载帖子 | {snapshot['postCountLoaded']} |",
        f"| 配置来源 | {snapshot['sourceCount']} |",
        f"| 档案完整度 | {snapshot['archiveCompletenessPct']}% |",
        f"| 评分窗内有帖平台 | {', '.join(raw['platformsWithAnyPosts']) or '无'} |",
        "",
        "## 需要优先处理的问题",
        "",
        "| 级别 | 问题 | 证据 | 建议 |",
        "|---|---|---|---|",
    ]
    severity_name = {"critical": "严重", "high": "高", "medium": "中", "info": "说明"}
    for alert in report["alerts"]:
        lines.append(
            f"| {severity_name.get(alert['severity'], alert['severity'])} "
            f"`{alert['code']}` | {markdown_escape(alert['title'])} | "
            f"{markdown_escape(alert['evidence'])} | {markdown_escape(alert['recommendation'])} |"
        )

    lines.extend([
        "",
        "## 平台与日期覆盖",
        "",
        "| 平台 | 历史库配置来源 | 评分窗有帖来源 | 帖子数 | 活跃日 |",
        "|---|---:|---:|---:|---:|",
    ])
    for platform, row in raw["platformSummary"].items():
        lines.append(
            f"| {markdown_escape(row['name'])} | {row['configuredSources']} | "
            f"{row['sourcesWithPostsInWindow']} | {row['postsInWindow']} | {row['activeDays']} |"
        )
    volume = raw["dailyTotalPosts"]
    lines.extend([
        "",
        f"- 自然日：{raw['expectedDays']} 天；有原始帖子 {raw['activeRawDays']} 天；"
        f"零帖子日：{', '.join(raw['zeroRawDays']) or '无'}。",
        f"- 单日帖子数中位数 {volume.get('median')}，最大 {volume.get('max')}，"
        f"变异系数 {volume.get('coefficientOfVariation')}。",
        f"- 活跃日中单平台日 {raw['singlePlatformRawDays']} 天，多平台日 {raw['multiPlatformRawDays']} 天。",
        "",
        "## 指数分布与耦合",
        "",
        "| 指标 | 最小 | 中位数 | 最大 | 均值 | 标准差 |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for key in ("baoma", "novice", "retail", "composite", "heat", "direction", "coverage", "confidence", "samplePosts"):
        row = history["metrics"][key]
        lines.append(
            f"| {key} | {row.get('min')} | {row.get('median')} | {row.get('max')} | "
            f"{row.get('mean')} | {row.get('stdev')} |"
        )
    lines.extend([
        "",
        "关键相关系数（只用于质量诊断，不代表因果）：",
        "",
    ])
    for key, value in history["correlations"].items():
        lines.append(f"- `{key}`：{value}")

    lines.extend([
        "",
        "## 三类人群指数区分度",
        "",
        f"- 日均最大差：{personas['dailyRange'].get('mean')} 分；最大日差：{personas['dailyRange'].get('max')} 分。",
        f"- 三条完全相同：{personas['exactlyEqualDays']} 天（{personas['exactlyEqualPct']}%）。",
        f"- 最大差不超过 1 分：{personas['rangeAtMostOneDays']} 天（{personas['rangeAtMostOnePct']}%）。",
        f"- 60 日内不同三元组数量：{personas['uniqueTriples']}。",
        "",
        "## 板块覆盖与结果",
        "",
        "| 平均排名 | 板块 | 可用日 | 均分 | 分数范围 | 样本中位数 | 平台中位数 | 置信度中位数 | 原始帖子 | 头部来源占比 |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for sector in report["sectors"]:
        lines.append(
            f"| {sector['rank'].get('mean')} | {markdown_escape(sector['name'])} | "
            f"{sector['daysAvailable']} | {sector['score'].get('mean')} | "
            f"{sector['score'].get('min')}–{sector['score'].get('max')} | "
            f"{sector['samplePosts'].get('median')} | {sector['platformCount'].get('median')} | "
            f"{sector['confidence'].get('median')} | {sector.get('rawPosts')} | "
            f"{sector.get('topSourceSharePct')}% |"
        )

    lines.extend([
        "",
        f"## {markdown_escape(str(agriculture.get('name') or '农业子板块'))}专项核查",
        "",
        f"{markdown_escape(str(agriculture.get('name') or '农业子板块'))} 60 日均分 {agriculture['score'].get('mean')}，平均排名 "
        f"{agriculture['rank'].get('mean')}；热度均值 {agriculture['heat'].get('mean')}，"
        f"方向均值 {agriculture['direction'].get('mean')}。滚动平台数中位数 "
        f"{agriculture['platformCount'].get('median')}。",
        "",
        "最高分日：",
        "",
        "| 日期 | 分数 | 热度 | 方向 | 样本 | 平台 | 排名 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for row in agriculture.get("highestDays", []):
        lines.append(
            f"| {row['date']} | {row['score']} | {row['heat']} | {row['direction']} | "
            f"{row['samplePosts']} | {row['platformCount']} | {row['rank']} |"
        )
    risks = agriculture.get("positiveScoreContextRisks", [])
    lines.extend([
        "",
        f"自动筛出的正分但疑似负向语境：{len(risks)} 条（需人工复核）。",
        "",
    ])
    for item in risks:
        lines.append(
            f"- {item['date']} · {markdown_escape(item['source'])} · 词典分 {item['lexiconScore']}："
            f"“{markdown_escape(item['title'])}”"
        )

    lines.extend([
        "",
        "## 最高与最低综合分日",
        "",
        "| 类型 | 日期 | 综合 | 热度 | 方向 | 样本 | 平台 | 置信度 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ])
    for label, rows in (("最高", history["highestCompositeDays"]), ("最低", history["lowestCompositeDays"])):
        for row in rows:
            lines.append(
                f"| {label} | {row['date']} | {row['composite']} | {row['heat']} | "
                f"{row['direction']} | {row['samplePosts']} | {row['platformCount']} | {row['confidence']} |"
            )

    lines.extend([
        "",
        "## 口径说明",
        "",
    ])
    for note in report["methodNotes"]:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def generate(db_path: Path) -> Dict[str, object]:
    results, market_keys, sector_source_keys, archive_meta = history_builder.load_results_from_db(db_path)
    start_text = str(archive_meta.get("scoreStart") or "")[:10]
    requested_end_text = str(archive_meta.get("windowAsOf") or "")[:10]
    if not start_text or not requested_end_text:
        raise RuntimeError("数据库缺少 score_start 或 window_as_of")
    start = date.fromisoformat(start_text)
    requested_end = date.fromisoformat(requested_end_text)
    estimated_end, partial_day = history_builder.complete_retrospective_end(requested_end, archive_meta)
    completeness = history_builder.archive_completeness(archive_meta)
    points, skipped = history_builder.score_history_points(
        results,
        market_keys,
        sector_source_keys,
        start=start,
        end=estimated_end,
        expected_source_count=int(archive_meta.get("sourceCount") or 1),
        completeness=completeness,
        captured_at=datetime.now(CN_TZ),
    )
    return build_quality_report(
        db_path.resolve(),
        results,
        market_keys,
        sector_source_keys,
        archive_meta,
        points,
        skipped,
        start,
        requested_end,
        estimated_end,
        partial_day,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只读审计 60 日社区情绪历史数据质量")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="历史 SQLite 数据库")
    parser.add_argument("--json-output", default=str(DEFAULT_JSON), help="JSON 审计报告")
    parser.add_argument("--markdown-output", default=str(DEFAULT_MARKDOWN), help="Markdown 审计报告")
    parser.add_argument("--no-write", action="store_true", help="不写报告文件")
    parser.add_argument(
        "--format", choices=("summary", "json", "markdown"), default="summary",
        help="终端输出格式",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = generate(Path(args.db))
        markdown = render_markdown(report)
        json_text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if not args.no_write:
            atomic_write(Path(args.json_output).resolve(), json_text)
            atomic_write(Path(args.markdown_output).resolve(), markdown)
        if args.format == "json":
            print(json_text, end="")
        elif args.format == "markdown":
            print(markdown, end="")
        else:
            print(
                f"质量状态：{report['status']}；历史点 {report['snapshot']['historyPointsBuilt']}；"
                f"帖子 {report['snapshot']['postCountLoaded']}；档案完整度 "
                f"{report['snapshot']['archiveCompletenessPct']}%。"
            )
            print(
                "问题：" + "，".join(
                    f"{item['severity']}:{item['code']}" for item in report["alerts"]
                )
            )
            if not args.no_write:
                print(f"JSON：{Path(args.json_output).resolve()}")
                print(f"Markdown：{Path(args.markdown_output).resolve()}")
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"审计失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
