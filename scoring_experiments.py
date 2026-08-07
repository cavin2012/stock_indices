"""历史去偏与真实语料人群分层的独立实验。

该文件只读历史 SQLite，并调用现有正式评分函数；不会修改指数核心代码或
页面数据。实验回答三个问题：

1. 每来源、每日稳定限额抽样能否降低采集进度造成的历史重写？
2. 用明确行为语料分群，而不是扩大权重差，能否形成有证据的人群差异？
3. 农业养殖中疑似正负语境误判的帖子，对分数究竟有多大影响？

运行：
    python scoring_experiments.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import build_history_60d as history_builder
import data_quality_report as quality
import update_index as core
from history_engine import CN_TZ


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DEFAULT_DB = DATA_DIR / "history_60d.sqlite3"
DEFAULT_JSON = DATA_DIR / "scoring_experiments.json"
DEFAULT_MARKDOWN = DATA_DIR / "scoring_experiments.md"
DEFAULT_DAILY_CAP = 8
PERSONA_KEYS = ("baoma", "novice", "retail")

# “宝妈”在实验中严格解释为一种显式的家庭稳健语料风格，不推断作者性别、
# 年龄或真实家庭身份。明确家庭词优先，稳健投资词作为风格证据。
HOUSEHOLD_EXPLICIT_RE = re.compile(
    r"宝妈|宝宝|孩子|小孩|奶粉|学费|家庭|家里|生活费|买菜|房贷|车贷|"
    r"老公|老婆|母婴|养老|退休|工资|存款|闲钱|攒钱"
)
HOUSEHOLD_CAUTIOUS_RE = re.compile(
    r"稳健|长期持有|长线|分红|股息|定投|价值投资|拿着不动|不折腾|"
    r"低风险|保本|每个月|每月投入|闲钱投资|养老钱"
)
NOVICE_RE = re.compile(
    r"新手|小白|第一次|刚入市|刚开户|不懂|求教|请教|请问|老师|大神|"
    r"怎么|如何|为什么|为何|怎么办|能买吗|能不能买|该不该|要不要|"
    r"多少买|怎么看|啥意思|什么意思|解套吗|补仓吗|卖吗|能买吗"
)
NOVICE_QUESTION_CONTEXT_RE = re.compile(
    r"股票|个股|板块|仓位|成本|均线|分红|除权|涨停|跌停|买|卖|补仓|解套"
)
RETAIL_ACTION_RE = re.compile(
    r"买入|卖出|加仓|减仓|补仓|清仓|满仓|半仓|持仓|仓位|成本|做T|"
    r"止损|止盈|抄底|追涨|套牢|解套|看多|看空|突破|支撑|压力|量能|"
    r"均线|主力|庄家|洗盘|出货|筹码|技术面|基本面|龙虎榜|集合竞价|"
    r"涨停|跌停|龙头|资金流|换仓"
)


def stable_post_key(source_key: str, post: Mapping[str, object]) -> bytes:
    identity = "|".join((
        source_key,
        str(post.get("id") or ""),
        str(post.get("publishTime") or ""),
        core.normalize_text(str(post.get("title") or "")),
    ))
    return hashlib.sha256(identity.encode("utf-8")).digest()


def stable_source_day_cap(
    results: Mapping[str, Mapping[str, object]],
    cap: int = DEFAULT_DAILY_CAP,
) -> Tuple[Dict[str, Dict[str, object]], Dict[str, object]]:
    """每来源、每发布日做稳定哈希抽样，并先沿用作者每日三条上限。

    哈希抽样不看情绪分数，避免为了得到想要的结果挑帖子；新翻页数据只会在
    哈希顺序更靠前时替换样本，因而比“取最新 N 条”更适合断点续采。
    """

    limit = max(1, int(cap))
    output: Dict[str, Dict[str, object]] = {}
    total_before = 0
    total_after = 0
    capped_buckets = 0
    author_dropped = 0
    for source_key, source in results.items():
        groups: Dict[str, List[Mapping[str, object]]] = defaultdict(list)
        for post in source.get("posts", []):
            if not isinstance(post, Mapping):
                continue
            published = post.get("published")
            if not isinstance(published, datetime):
                continue
            groups[published.astimezone(CN_TZ).date().isoformat()].append(post)
            total_before += 1

        selected: List[Mapping[str, object]] = []
        for _, posts in sorted(groups.items()):
            author_counts: Counter = Counter()
            author_limited: List[Mapping[str, object]] = []
            for post in sorted(
                posts,
                key=lambda item: item.get("published") or datetime.min.replace(tzinfo=CN_TZ),
                reverse=True,
            ):
                author = str(
                    post.get("authorKey") or post.get("authorHash")
                    or post.get("id") or core.normalize_text(str(post.get("title") or ""))
                )
                if author_counts[author] >= core.AUTHOR_DAILY_LIMIT:
                    author_dropped += 1
                    continue
                author_counts[author] += 1
                author_limited.append(post)
            if len(author_limited) > limit:
                capped_buckets += 1
            selected.extend(sorted(
                author_limited,
                key=lambda post: stable_post_key(source_key, post),
            )[:limit])

        selected.sort(key=lambda item: item.get("published") or datetime.min.replace(tzinfo=CN_TZ))
        total_after += len(selected)
        output[source_key] = {**source, "posts": selected}
    return output, {
        "capPerSourcePerPublishedDay": limit,
        "postsBefore": total_before,
        "postsAfter": total_after,
        "retainedPct": quality.round_value(total_after / total_before * 100.0, 1) if total_before else None,
        "cappedSourceDayBuckets": capped_buckets,
        "authorOverflowRemovedBeforeSampling": author_dropped,
        "selection": "sha256(source, id, publishTime, normalizedTitle) smallest N",
    }


def classify_persona(text: str) -> Tuple[Optional[str], Optional[str]]:
    """返回互斥的行为语料类型；无明确证据则不分群。"""

    cleaned = str(text or "").strip()
    if not cleaned:
        return None, None
    if HOUSEHOLD_EXPLICIT_RE.search(cleaned):
        return "baoma", "explicit_household"
    if HOUSEHOLD_CAUTIOUS_RE.search(cleaned):
        return "baoma", "cautious_household_style"
    if NOVICE_RE.search(cleaned):
        return "novice", "explicit_question_or_learning"
    if re.search(r"[?？]", cleaned) and NOVICE_QUESTION_CONTEXT_RE.search(cleaned):
        return "novice", "market_question"
    if RETAIL_ACTION_RE.search(cleaned):
        return "retail", "active_trading_language"
    return None, None


def split_persona_results(
    results: Mapping[str, Mapping[str, object]],
    start: date,
    end: date,
) -> Tuple[Dict[str, Dict[str, Dict[str, object]]], Dict[str, object]]:
    split: Dict[str, Dict[str, Dict[str, object]]] = {
        key: {} for key in PERSONA_KEYS
    }
    total_counts: Counter = Counter()
    window_counts: Counter = Counter()
    reason_counts: Dict[str, Counter] = defaultdict(Counter)
    unlabeled = 0
    for source_key, source in results.items():
        buckets: Dict[str, List[Mapping[str, object]]] = {key: [] for key in PERSONA_KEYS}
        for post in source.get("posts", []):
            if not isinstance(post, Mapping):
                continue
            label, reason = classify_persona(str(post.get("title") or ""))
            if label is None:
                unlabeled += 1
                continue
            buckets[label].append(post)
            total_counts[label] += 1
            reason_counts[label][str(reason)] += 1
            published = post.get("published")
            if isinstance(published, datetime) and start <= published.date() <= end:
                window_counts[label] += 1
        for label in PERSONA_KEYS:
            split[label][source_key] = {**source, "posts": buckets[label]}
    labeled = sum(total_counts.values())
    return split, {
        "interpretation": {
            "baoma": "显式家庭/生活资金或稳健长期风格，不推断真实人口属性",
            "novice": "明确提问、求教、首次入市或市场知识疑问",
            "retail": "明确买卖、仓位、技术分析或主动交易表达",
        },
        "exclusivePriority": ["baoma", "novice", "retail"],
        "archiveCounts": dict(total_counts),
        "scoreWindowCounts": dict(window_counts),
        "reasonCounts": {key: dict(values) for key, values in reason_counts.items()},
        "unlabeledCount": unlabeled,
        "labeledCount": labeled,
        "labeledPct": quality.round_value(labeled / (labeled + unlabeled) * 100.0, 1)
        if labeled + unlabeled else None,
    }


def mask_persona_evidence_for_scoring(
    results: Mapping[str, Mapping[str, object]],
    persona: str,
) -> Tuple[Dict[str, Dict[str, object]], Dict[str, int]]:
    """遮住分群所用词后再评分，用于检查“先挑词、再给同一词加分”的循环。

    这只是严格的敏感性测试：生产方案更适合使用人工标注验证过的独立分类器，
    而不是永久删除原文中的行为信息。
    """

    if persona == "baoma":
        patterns = (HOUSEHOLD_EXPLICIT_RE, HOUSEHOLD_CAUTIOUS_RE)
    elif persona == "novice":
        patterns = (NOVICE_RE,)
    elif persona == "retail":
        patterns = (RETAIL_ACTION_RE,)
    else:
        raise ValueError(f"未知人群：{persona}")
    output: Dict[str, Dict[str, object]] = {}
    masked = 0
    dropped = 0
    for source_key, source in results.items():
        posts: List[Dict[str, object]] = []
        for raw in source.get("posts", []):
            post = dict(raw)
            original = str(post.get("title") or "")
            scoring_text = original
            for pattern in patterns:
                scoring_text = pattern.sub(" ", scoring_text)
            scoring_text = re.sub(r"\s+", " ", scoring_text).strip(" ，。！？!?：:；;")
            if scoring_text != original:
                masked += 1
            if not core.valid_text(scoring_text):
                dropped += 1
                continue
            post["evidenceTitle"] = original
            post["title"] = scoring_text
            posts.append(post)
        output[source_key] = {**source, "posts": posts}
    return output, {"maskedPosts": masked, "droppedWithoutResidualText": dropped}


def filter_collected_at(
    results: Mapping[str, Mapping[str, object]],
    cutoff: str,
) -> Dict[str, Dict[str, object]]:
    output: Dict[str, Dict[str, object]] = {}
    for source_key, source in results.items():
        output[source_key] = {
            **source,
            "posts": [
                post for post in source.get("posts", [])
                if str(post.get("collectedAt") or "") <= cutoff
            ],
        }
    return output


def first_completed_run(db_path: Path) -> Optional[Dict[str, object]]:
    connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT run_id, started_at, finished_at, requests, posts_inserted
            FROM runs
            WHERE status = 'completed' AND finished_at IS NOT NULL
            ORDER BY started_at
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def score_points(
    results: Mapping[str, Dict[str, object]],
    market_keys: Sequence[str],
    sector_source_keys: Mapping[str, Sequence[str]],
    archive_meta: Mapping[str, object],
    start: date,
    end: date,
) -> Tuple[List[Dict[str, object]], List[Dict[str, str]]]:
    return history_builder.score_history_points(
        results,
        market_keys,
        sector_source_keys,
        start=start,
        end=end,
        expected_source_count=int(archive_meta.get("sourceCount") or 1),
        completeness=history_builder.archive_completeness(archive_meta),
        captured_at=datetime.now(CN_TZ),
    )


def aligned_values(
    points: Sequence[Mapping[str, object]],
    metric: str,
) -> Dict[str, float]:
    return {str(point["date"]): float(point[metric]) for point in points}


def scoring_bias_metrics(
    points: Sequence[Mapping[str, object]],
    reference_points: Optional[Sequence[Mapping[str, object]]] = None,
) -> Dict[str, object]:
    dates = [str(point["date"]) for point in points]
    composite = [float(point["composite"]) for point in points]
    heat = [float(point["heat"]) for point in points]
    direction = [float(point["direction"]) for point in points]
    own_samples = [float(point["samplePosts"]) for point in points]
    ordinal = [float(index) for index in range(len(points))]
    metrics: Dict[str, object] = {
        "days": len(points),
        "composite": quality.describe(composite),
        "heat": quality.describe(heat),
        "direction": quality.describe(direction),
        "samplePosts": quality.describe(own_samples),
        "compositeVsOwnSamplePosts": quality.pearson(composite, own_samples),
        "heatVsOwnSamplePosts": quality.pearson(heat, own_samples),
        "directionVsOwnSamplePosts": quality.pearson(direction, own_samples),
        "compositeVsDateOrder": quality.pearson(composite, ordinal),
        "heatVsDateOrder": quality.pearson(heat, ordinal),
        "directionVsDateOrder": quality.pearson(direction, ordinal),
        "personaDifferentiation": quality.analyze_persona_differentiation(points),
    }
    if reference_points is not None:
        reference_samples = aligned_values(reference_points, "samplePosts")
        paired = [
            (composite_value, heat_value, direction_value, reference_samples[day])
            for day, composite_value, heat_value, direction_value
            in zip(dates, composite, heat, direction) if day in reference_samples
        ]
        metrics["compositeVsBaselineSamplePosts"] = quality.pearson(
            [item[0] for item in paired], [item[3] for item in paired]
        )
        metrics["heatVsBaselineSamplePosts"] = quality.pearson(
            [item[1] for item in paired], [item[3] for item in paired]
        )
        metrics["directionVsBaselineSamplePosts"] = quality.pearson(
            [item[2] for item in paired], [item[3] for item in paired]
        )
    return metrics


def revision_stability(
    earlier: Sequence[Mapping[str, object]],
    later: Sequence[Mapping[str, object]],
) -> Dict[str, object]:
    old = aligned_values(earlier, "composite")
    new = aligned_values(later, "composite")
    dates = sorted(set(old) & set(new))
    differences = [new[day] - old[day] for day in dates]
    return {
        "comparedDays": len(dates),
        "changedDays": sum(value != 0 for value in differences),
        "meanAbsoluteRevision": quality.round_value(
            statistics.mean(abs(value) for value in differences), 2
        ) if differences else None,
        "maxAbsoluteRevision": quality.round_value(max(abs(value) for value in differences), 2)
        if differences else None,
        "revision": quality.describe(differences),
        "largestRevisions": sorted(
            [
                {
                    "date": day,
                    "earlier": old[day],
                    "later": new[day],
                    "change": quality.round_value(new[day] - old[day], 1),
                }
                for day in dates
            ],
            key=lambda item: abs(float(item["change"])),
            reverse=True,
        )[:10],
    }


def evidence_weight(point: Mapping[str, object]) -> float:
    """只收缩、不放大分群差；单平台时权重自然受限。"""

    sample_ratio = min(1.0, max(0.0, float(point.get("samplePosts") or 0)) / 120.0)
    source_ratio = min(1.0, max(0.0, float(point.get("sourceCount") or 0)) / 8.0)
    platform_ratio = min(1.0, max(0.0, float(point.get("platformCount") or 0)) / 2.0)
    evidence_ratio = math.sqrt(sample_ratio * source_ratio * platform_ratio)
    # 历史点的 confidence 已经包含预期 83 来源、平台缺失与档案完整度折价。
    # 以它作为硬上限，避免单平台但帖子很多的子群获得过高权重。
    confidence_ratio = min(1.0, max(0.0, float(point.get("confidence") or 0)) / 100.0)
    return min(0.65, evidence_ratio, confidence_ratio)


def publication_gate(
    results: Mapping[str, Mapping[str, object]],
    points: Sequence[Mapping[str, object]],
    archive_completeness: float,
    lookback_days: int = 7,
) -> Dict[str, object]:
    """用真实发布日期而非滚动模型平台数，判断历史日能否定稿。"""

    publication_days: Dict[str, set] = defaultdict(set)
    for source in results.values():
        platform = str(source.get("platform") or "unknown")
        for post in source.get("posts", []):
            published = post.get("published") if isinstance(post, Mapping) else None
            if isinstance(published, datetime):
                publication_days[platform].add(published.astimezone(CN_TZ).date())
    rows: List[Dict[str, object]] = []
    for point in points:
        day = date.fromisoformat(str(point["date"]))
        platforms = sorted(
            platform for platform, days in publication_days.items()
            if any(0 <= (day - candidate).days < lookback_days for candidate in days)
        )
        reasons: List[str] = []
        if len(platforms) < 2:
            reasons.append("trailing_publication_platforms_below_2")
        if archive_completeness < 0.70:
            reasons.append("archive_completeness_below_70pct")
        if float(point.get("confidence") or 0) < 55:
            reasons.append("confidence_below_55")
        rows.append({
            "date": point["date"],
            "publishedPlatformsTrailingDays": platforms,
            "publishedPlatformCount": len(platforms),
            "confidence": point.get("confidence"),
            "eligible": not reasons,
            "reasons": reasons,
        })
    return {
        "criteria": {
            "trailingPublicationDays": lookback_days,
            "minimumPublishedPlatforms": 2,
            "minimumArchiveCompletenessPct": 70,
            "minimumConfidence": 55,
        },
        "eligibleDays": sum(bool(row["eligible"]) for row in rows),
        "provisionalDays": sum(not bool(row["eligible"]) for row in rows),
        "rows": rows,
    }


def cohort_persona_comparison(
    baseline_points: Sequence[Mapping[str, object]],
    cohort_points: Mapping[str, Sequence[Mapping[str, object]]],
) -> Dict[str, object]:
    baseline = {str(point["date"]): point for point in baseline_points}
    cohort_maps = {
        key: {str(point["date"]): point for point in points}
        for key, points in cohort_points.items()
    }
    per_persona: Dict[str, Dict[str, object]] = {}
    daily: List[Dict[str, object]] = []
    for persona in PERSONA_KEYS:
        raw_values: List[float] = []
        final_values: List[float] = []
        weights: List[float] = []
        for day, base in baseline.items():
            cohort = cohort_maps[persona].get(day)
            if cohort is None:
                continue
            weight = evidence_weight(cohort)
            raw_value = float(cohort["composite"])
            base_value = float(base["composite"])
            final = base_value + weight * (raw_value - base_value)
            raw_values.append(raw_value)
            final_values.append(final)
            weights.append(weight)
        per_persona[persona] = {
            "availableDays": len(final_values),
            "rawCohortScore": quality.describe(raw_values),
            "evidenceWeightedScore": quality.describe(final_values),
            "evidenceWeight": quality.describe(weights),
        }

    joint_days = sorted(
        set(baseline)
        & set(cohort_maps["baoma"])
        & set(cohort_maps["novice"])
        & set(cohort_maps["retail"])
    )
    final_ranges: List[float] = []
    raw_ranges: List[float] = []
    original_ranges: List[float] = []
    for day in joint_days:
        base = baseline[day]
        raw_scores: Dict[str, float] = {}
        final_scores: Dict[str, float] = {}
        weights: Dict[str, float] = {}
        for persona in PERSONA_KEYS:
            cohort = cohort_maps[persona][day]
            raw = float(cohort["composite"])
            weight = evidence_weight(cohort)
            raw_scores[persona] = raw
            weights[persona] = weight
            final_scores[persona] = quality.round_value(
                float(base["composite"]) + weight * (raw - float(base["composite"])), 1
            )
        raw_range = max(raw_scores.values()) - min(raw_scores.values())
        final_range = max(final_scores.values()) - min(final_scores.values())
        original_range = max(float(base[key]) for key in PERSONA_KEYS) - min(
            float(base[key]) for key in PERSONA_KEYS
        )
        raw_ranges.append(raw_range)
        final_ranges.append(final_range)
        original_ranges.append(original_range)
        daily.append({
            "date": day,
            "marketBaseline": base["composite"],
            "originalPersonaScores": {key: base[key] for key in PERSONA_KEYS},
            "rawCohortScores": raw_scores,
            "evidenceWeights": {key: quality.round_value(value, 3) for key, value in weights.items()},
            "evidenceWeightedScores": final_scores,
            "originalRange": original_range,
            "rawCohortRange": quality.round_value(raw_range, 1),
            "evidenceWeightedRange": quality.round_value(final_range, 1),
        })
    return {
        "principle": (
            "三个人群先按明确行为文本形成互斥子样本，三组使用完全相同的评分公式；"
            "结果只向市场基准收缩，绝不放大组间差。"
        ),
        "personas": per_persona,
        "jointAvailableDays": len(joint_days),
        "originalDailyMaxRange": quality.describe(original_ranges),
        "rawCohortDailyMaxRange": quality.describe(raw_ranges),
        "evidenceWeightedDailyMaxRange": quality.describe(final_ranges),
        "daily": daily,
    }


def agriculture_rows(points: Sequence[Mapping[str, object]]) -> Dict[str, Dict[str, object]]:
    output: Dict[str, Dict[str, object]] = {}
    for point in points:
        sectors = [item for item in point.get("sectors", []) if isinstance(item, Mapping)]
        agriculture = next((item for item in sectors if item.get("key") == "agriculture"), None)
        if agriculture is None:
            continue
        available = [item for item in sectors if bool(item.get("available"))]
        score = float(agriculture.get("scores", {}).get("composite", 0))
        rank = 1 + sum(
            float(item.get("scores", {}).get("composite", 0)) > score
            for item in available
        )
        output[str(point["date"])] = {
            "score": score,
            "heat": float(agriculture.get("heat", 0)),
            "direction": float(agriculture.get("direction", 0)),
            "samplePosts": int(agriculture.get("samplePosts", 0)),
            "rank": rank,
        }
    return output


def agriculture_comparison(
    baseline: Sequence[Mapping[str, object]],
    variant: Sequence[Mapping[str, object]],
) -> Dict[str, object]:
    old = agriculture_rows(baseline)
    new = agriculture_rows(variant)
    dates = sorted(set(old) & set(new))
    score_changes = [new[day]["score"] - old[day]["score"] for day in dates]
    heat_changes = [new[day]["heat"] - old[day]["heat"] for day in dates]
    direction_changes = [new[day]["direction"] - old[day]["direction"] for day in dates]
    return {
        "comparedDays": len(dates),
        "changedScoreDays": sum(value != 0 for value in score_changes),
        "scoreChange": quality.describe(score_changes),
        "meanAbsoluteScoreChange": quality.round_value(
            statistics.mean(abs(value) for value in score_changes), 2
        ) if score_changes else None,
        "heatChange": quality.describe(heat_changes),
        "directionChange": quality.describe(direction_changes),
        "largestScoreImpacts": sorted(
            [
                {
                    "date": day,
                    "before": old[day],
                    "after": new[day],
                    "scoreChange": new[day]["score"] - old[day]["score"],
                }
                for day in dates
            ],
            key=lambda item: abs(float(item["scoreChange"])),
            reverse=True,
        )[:10],
    }


def remove_agriculture_context_risks(
    results: Mapping[str, Mapping[str, object]],
    agriculture_source_keys: Sequence[str],
    start: date,
    end: date,
) -> Tuple[Dict[str, Dict[str, object]], List[Dict[str, object]]]:
    targets = set(agriculture_source_keys)
    output: Dict[str, Dict[str, object]] = {}
    removed: List[Dict[str, object]] = []
    for source_key, source in results.items():
        posts: List[Mapping[str, object]] = []
        for post in source.get("posts", []):
            title = str(post.get("title") or "")
            published = post.get("published")
            is_risk = (
                source_key in targets
                and isinstance(published, datetime)
                and start <= published.date() <= end
                and quality.AGRICULTURE_CONTEXT_RISK.search(title) is not None
                and core.sentiment_score(title) > 0
            )
            if is_risk:
                removed.append({
                    "date": published.date().isoformat(),
                    "source": str(source.get("name") or source_key),
                    "title": title,
                    "lexiconScore": core.sentiment_score(title),
                })
            else:
                posts.append(post)
        output[source_key] = {**source, "posts": posts}
    return output, removed


def build_report(db_path: Path, cap: int) -> Dict[str, object]:
    results, market_keys, sector_source_keys, archive_meta = history_builder.load_results_from_db(db_path)
    start = date.fromisoformat(str(archive_meta.get("scoreStart"))[:10])
    requested_end = date.fromisoformat(str(archive_meta.get("windowAsOf"))[:10])
    end, partial_day = history_builder.complete_retrospective_end(requested_end, archive_meta)

    baseline_points, baseline_skipped = score_points(
        results, market_keys, sector_source_keys, archive_meta, start, end
    )
    balanced_results, cap_stats = stable_source_day_cap(results, cap)
    balanced_points, balanced_skipped = score_points(
        balanced_results, market_keys, sector_source_keys, archive_meta, start, end
    )

    run = first_completed_run(db_path)
    stability: Dict[str, object] = {"available": False}
    if run and run.get("finished_at"):
        earlier_results = filter_collected_at(results, str(run["finished_at"]))
        earlier_baseline, _ = score_points(
            earlier_results, market_keys, sector_source_keys, archive_meta, start, end
        )
        earlier_balanced_results, earlier_cap_stats = stable_source_day_cap(earlier_results, cap)
        earlier_balanced, _ = score_points(
            earlier_balanced_results, market_keys, sector_source_keys, archive_meta, start, end
        )
        stability = {
            "available": True,
            "earlierSnapshot": run,
            "earlierPostCount": sum(len(item.get("posts", [])) for item in earlier_results.values()),
            "currentPostCount": sum(len(item.get("posts", [])) for item in results.values()),
            "earlierBalancedSampling": earlier_cap_stats,
            "baseline": revision_stability(earlier_baseline, baseline_points),
            "balanced": revision_stability(earlier_balanced, balanced_points),
        }

    persona_results, persona_evidence = split_persona_results(balanced_results, start, end)
    cohort_points: Dict[str, List[Dict[str, object]]] = {}
    cohort_skipped: Dict[str, List[Dict[str, str]]] = {}
    heldout_points: Dict[str, List[Dict[str, object]]] = {}
    heldout_skipped: Dict[str, List[Dict[str, str]]] = {}
    heldout_masking: Dict[str, Dict[str, int]] = {}
    for persona in PERSONA_KEYS:
        points, skipped = score_points(
            persona_results[persona], market_keys, sector_source_keys,
            archive_meta, start, end,
        )
        cohort_points[persona] = points
        cohort_skipped[persona] = skipped
        heldout_results, masking = mask_persona_evidence_for_scoring(
            persona_results[persona], persona
        )
        masked_points, masked_skipped = score_points(
            heldout_results, market_keys, sector_source_keys,
            archive_meta, start, end,
        )
        heldout_points[persona] = masked_points
        heldout_skipped[persona] = masked_skipped
        heldout_masking[persona] = masking
    persona_comparison = cohort_persona_comparison(balanced_points, cohort_points)
    persona_comparison["evidence"] = persona_evidence
    persona_comparison["skippedDays"] = cohort_skipped
    persona_comparison["lexicallyHeldOut"] = cohort_persona_comparison(
        balanced_points, heldout_points
    )
    persona_comparison["lexicallyHeldOut"]["masking"] = heldout_masking
    persona_comparison["lexicallyHeldOut"]["skippedDays"] = heldout_skipped

    context_filtered_results, removed = remove_agriculture_context_risks(
        results, sector_source_keys.get("agriculture", []), start, end
    )
    context_filtered_points, context_skipped = score_points(
        context_filtered_results, market_keys, sector_source_keys,
        archive_meta, start, end,
    )

    report: Dict[str, object] = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(CN_TZ).isoformat(),
        "database": str(db_path.resolve()),
        "window": {
            "startDate": start.isoformat(),
            "estimatedEndDate": end.isoformat(),
            "partialCollectorDateExcluded": partial_day.isoformat() if partial_day else None,
        },
        "snapshot": {
            "posts": int(archive_meta.get("postCount") or 0),
            "sources": int(archive_meta.get("sourceCount") or 0),
            "archiveCompletenessPct": quality.round_value(
                history_builder.archive_completeness(archive_meta) * 100.0, 1
            ),
        },
        "historyBiasExperiment": {
            "baseline": scoring_bias_metrics(baseline_points),
            "balanced": scoring_bias_metrics(balanced_points, baseline_points),
            "sampling": cap_stats,
            "revisionStability": stability,
            "publicationGate": publication_gate(
                results,
                baseline_points,
                history_builder.archive_completeness(archive_meta),
            ),
            "verdict": (
                "稳定限额抽样能降低样本量相关性和补采修订，但若与原样本量/日期仍高度相关，"
                "它只能作为鲁棒层，不能代替跨平台与档案完整度门槛。"
            ),
            "baselineSkipped": baseline_skipped,
            "balancedSkipped": balanced_skipped,
        },
        "personaEvidenceExperiment": persona_comparison,
        "agricultureContextExperiment": {
            "removedPostCount": len(removed),
            "removedPosts": removed,
            "contextOnlySensitivity": agriculture_comparison(
                baseline_points, context_filtered_points
            ),
            "balancedSamplingSensitivity": agriculture_comparison(
                baseline_points, balanced_points
            ),
            "skippedDays": context_skipped,
        },
        "recommendation": {
            "minimumChanges": [
                "历史 estimated 模式先按来源×发布日期执行作者三条上限，再做稳定哈希最多 8 条抽样；measured 不变。",
                "保存 daily raw coverage；近 7 日真实发布平台少于两个、档案完整度低于 70% 或置信度低于 55 时标为 provisional，不进入平滑趋势结论。",
                "宝妈/小白/散户改为行为语料子样本：家庭稳健、明确提问、主动交易；无证据文本不强分群。",
                "三组使用同一套评分公式，再按样本/来源/平台证据向市场基准收缩；证据不足显示不可用，不放大差异。",
                "用与评分词典隔离的人工标注集验证人群分类；当前正则结果只作为可行性证明，不能直接替换正式指数。",
                "为否定、转折、换仓标的增加语境单测；先修农业报告中已命中的具体句式。",
            ],
            "doNotDo": [
                "不要按日期回归后强制把综合分与时间相关性变成零，这会删除真实行情热度并产生全窗口前视。",
                "不要给三类指数乘不同放大系数或设置固定偏移，这只是视觉造差。",
                "不要在单平台缺失时注入 50 分伪装完整，也不要把低置信度曲线当精确历史事实。",
            ],
        },
    }
    return report


def render_markdown(report: Mapping[str, object]) -> str:
    bias = report["historyBiasExperiment"]
    baseline = bias["baseline"]
    balanced = bias["balanced"]
    stability = bias["revisionStability"]
    gate = bias["publicationGate"]
    persona = report["personaEvidenceExperiment"]
    heldout = persona["lexicallyHeldOut"]
    agriculture = report["agricultureContextExperiment"]
    lines = [
        "# 历史去偏与人群语料分层实验",
        "",
        f"> 快照：{report['generatedAt']}；帖子 {report['snapshot']['posts']}；"
        f"档案完整度 {report['snapshot']['archiveCompletenessPct']}%。本实验未修改正式评分代码。",
        "",
        "## 1. 历史采集进度去偏",
        "",
        "方案：estimated 模式按每来源、每发布日期先执行作者上限，再用与情绪无关的稳定哈希保留最多 8 条。",
        "",
        "| 指标 | 原方案 | 稳定限额方案 |",
        "|---|---:|---:|",
        f"| 帖子样本 | {bias['sampling']['postsBefore']} | {bias['sampling']['postsAfter']} |",
        f"| 综合分与自身 samplePosts 相关性 | {baseline['compositeVsOwnSamplePosts']} | {balanced['compositeVsOwnSamplePosts']} |",
        f"| 热度与自身 samplePosts 相关性 | {baseline['heatVsOwnSamplePosts']} | {balanced['heatVsOwnSamplePosts']} |",
        f"| 综合分与原方案 samplePosts 相关性 | — | {balanced.get('compositeVsBaselineSamplePosts')} |",
        f"| 热度与原方案 samplePosts 相关性 | — | {balanced.get('heatVsBaselineSamplePosts')} |",
        f"| 综合分与日期顺序相关性 | {baseline['compositeVsDateOrder']} | {balanced['compositeVsDateOrder']} |",
        f"| 综合分标准差 | {baseline['composite'].get('stdev')} | {balanced['composite'].get('stdev')} |",
        "",
        f"实验判断：{bias['verdict']}",
        "",
        f"按“近 {gate['criteria']['trailingPublicationDays']} 日至少两个真实发布平台 + "
        f"档案完整度至少 {gate['criteria']['minimumArchiveCompletenessPct']}% + "
        f"置信度至少 {gate['criteria']['minimumConfidence']}”门槛，当前可定稿 "
        f"{gate['eligibleDays']} 天，仍属 provisional {gate['provisionalDays']} 天。",
        "",
    ]
    if stability.get("available"):
        old = stability["baseline"]
        new = stability["balanced"]
        lines.extend([
            "断点补采稳定性（第一批结束快照 → 当前快照）：",
            "",
            "| 指标 | 原方案 | 稳定限额方案 |",
            "|---|---:|---:|",
            f"| 比较日数 | {old['comparedDays']} | {new['comparedDays']} |",
            f"| 历史分数发生变化的天数 | {old['changedDays']} | {new['changedDays']} |",
            f"| 平均绝对修订 | {old['meanAbsoluteRevision']} | {new['meanAbsoluteRevision']} |",
            f"| 最大绝对修订 | {old['maxAbsoluteRevision']} | {new['maxAbsoluteRevision']} |",
            "",
        ])

    evidence = persona["evidence"]
    lines.extend([
        "## 2. 不人为造差的人群指数",
        "",
        persona["principle"],
        "",
        f"稳定样本中有明确人群证据的帖子占 {evidence['labeledPct']}%。",
        "",
        "| 人群 | 评分窗证据帖 | 可用日 | 原始子群均分 | 收缩后均分 | 证据权重中位数 |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    names = {"baoma": "家庭稳健型（宝妈风格）", "novice": "新手提问型", "retail": "主动交易型散户"}
    for key in PERSONA_KEYS:
        row = persona["personas"][key]
        lines.append(
            f"| {names[key]} | {evidence['scoreWindowCounts'].get(key, 0)} | "
            f"{row['availableDays']} | {row['rawCohortScore'].get('mean')} | "
            f"{row['evidenceWeightedScore'].get('mean')} | {row['evidenceWeight'].get('median')} |"
        )
    lines.extend([
        "",
        "| 三类日内最大差 | 均值 | 中位数 | 最大值 |",
        "|---|---:|---:|---:|",
        f"| 原三套权重 | {persona['originalDailyMaxRange'].get('mean')} | "
        f"{persona['originalDailyMaxRange'].get('median')} | {persona['originalDailyMaxRange'].get('max')} |",
        f"| 真实子群、未收缩 | {persona['rawCohortDailyMaxRange'].get('mean')} | "
        f"{persona['rawCohortDailyMaxRange'].get('median')} | {persona['rawCohortDailyMaxRange'].get('max')} |",
        f"| 真实子群、证据收缩后 | {persona['evidenceWeightedDailyMaxRange'].get('mean')} | "
        f"{persona['evidenceWeightedDailyMaxRange'].get('median')} | {persona['evidenceWeightedDailyMaxRange'].get('max')} |",
        f"| 遮住分群词后再评分、证据收缩后 | {heldout['evidenceWeightedDailyMaxRange'].get('mean')} | "
        f"{heldout['evidenceWeightedDailyMaxRange'].get('median')} | "
        f"{heldout['evidenceWeightedDailyMaxRange'].get('max')} |",
        "",
        "区分度来自不同语料的实际结果；收缩步骤只会减小、不会放大人群与市场基准的差距。"
        "“遮住分群词”是防止同一个词既负责选组又负责加分的严格敏感性测试。",
        "",
        "## 3. 农业养殖语境敏感性",
        "",
        f"筛出并临时删除 {agriculture['removedPostCount']} 条“词典正分但疑似负向语境”帖子。",
        "",
        "| 对照 | 影响天数 | 平均绝对分数变化 | 最大下降 | 最大上升 |",
        "|---|---:|---:|---:|---:|",
    ])
    context = agriculture["contextOnlySensitivity"]
    balanced_agri = agriculture["balancedSamplingSensitivity"]
    lines.append(
        f"| 只删除疑似误判句 | {context['changedScoreDays']} | {context['meanAbsoluteScoreChange']} | "
        f"{context['scoreChange'].get('min')} | {context['scoreChange'].get('max')} |"
    )
    lines.append(
        f"| 稳定限额抽样整体影响 | {balanced_agri['changedScoreDays']} | "
        f"{balanced_agri['meanAbsoluteScoreChange']} | {balanced_agri['scoreChange'].get('min')} | "
        f"{balanced_agri['scoreChange'].get('max')} |"
    )
    lines.extend([
        "",
        "这能区分两件事：个别语境误判的直接影响，以及采集量/来源结构带来的更大整体影响。",
        "",
        "## 最小可落地改动",
        "",
    ])
    for item in report["recommendation"]["minimumChanges"]:
        lines.append(f"- {item}")
    lines.extend(["", "不建议：", ""])
    for item in report["recommendation"]["doNotDo"]:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="历史去偏与人群语料分层独立实验")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--daily-cap", type=int, default=DEFAULT_DAILY_CAP)
    parser.add_argument("--json-output", default=str(DEFAULT_JSON))
    parser.add_argument("--markdown-output", default=str(DEFAULT_MARKDOWN))
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--format", choices=("summary", "json", "markdown"), default="summary")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = build_report(Path(args.db), args.daily_cap)
        json_text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        markdown = render_markdown(report)
        if not args.no_write:
            atomic_write(Path(args.json_output).resolve(), json_text)
            atomic_write(Path(args.markdown_output).resolve(), markdown)
        if args.format == "json":
            print(json_text, end="")
        elif args.format == "markdown":
            print(markdown, end="")
        else:
            bias = report["historyBiasExperiment"]
            persona = report["personaEvidenceExperiment"]
            print(
                "去偏相关性：综合×样本 "
                f"{bias['baseline']['compositeVsOwnSamplePosts']} → "
                f"{bias['balanced']['compositeVsOwnSamplePosts']}；热度×样本 "
                f"{bias['baseline']['heatVsOwnSamplePosts']} → "
                f"{bias['balanced']['heatVsOwnSamplePosts']}；"
                f"日期 {bias['baseline']['compositeVsDateOrder']} → "
                f"{bias['balanced']['compositeVsDateOrder']}。"
            )
            print(
                "三类日均最大差："
                f"原 {persona['originalDailyMaxRange'].get('mean')} → "
                f"真实语料收缩后 {persona['evidenceWeightedDailyMaxRange'].get('mean')}；"
                f"遮住分群词后 {persona['lexicallyHeldOut']['evidenceWeightedDailyMaxRange'].get('mean')}。"
            )
            print(
                f"农业疑似语境帖 {report['agricultureContextExperiment']['removedPostCount']} 条。"
            )
            if not args.no_write:
                print(f"JSON：{Path(args.json_output).resolve()}")
                print(f"Markdown：{Path(args.markdown_output).resolve()}")
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"实验失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
