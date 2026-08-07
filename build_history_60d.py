"""把 collect_history_60d.py 的 SQLite 档案重算为项目 60 日历史。

本脚本完全离线，不发起网络请求。它会：

1. 以当前板块目录重建 market_keys 与 sector_source_keys，并复用档案中同 key 的来源；
2. 把所有回溯帖子加载为 update_index.score_results_as_of 可识别的结果结构；
3. 逐日以 ESTIMATED 模式重算，互动数固定为 0，避免回溯时看到的累计互动前视；
4. 使用 history_engine.upsert_history 合并，确保同日 measured 永远优先；
5. 原子更新 history.json、history_meta.json、latest.json 与 data.js。

先预演：
    python build_history_60d.py --dry-run

正式写入：
    python build_history_60d.py
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import update_index as core
import community_archive
from sector_heat import HEAT_MODEL_VERSION
from history_engine import (
    ESTIMATED,
    MEASURED,
    MIN_ARCHIVE_COMPLETENESS,
    MIN_ESTIMATED_CONFIDENCE,
    MIN_RECENT_PUBLISHED_PLATFORMS,
    QUALITY_PROVISIONAL,
    QUALITY_USABLE,
    as_of_datetime,
    build_history_meta,
    build_history_point,
    quality_status_of,
    record_type_of,
    upsert_history,
)
from trading_calendar import (
    is_trading_day,
    latest_trading_day,
    previous_trading_days,
    trading_day_gap,
)


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DEFAULT_DB_PATH = DATA_DIR / "history_60d.sqlite3"
DEFAULT_BUILD_REPORT = DATA_DIR / "history_60d_build_report.json"
CN_TZ = timezone(timedelta(hours=8))


def load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def atomic_write_json(path: Path, payload: object) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def source_url(platform: str, code: str) -> str:
    if platform == "eastmoney":
        return f"https://guba.eastmoney.com/list,{code}.html"
    if platform == "sina":
        if code.startswith(("5", "6", "9")):
            symbol = "sh" + code
        elif code.startswith(("4", "8")):
            symbol = "bj" + code
        else:
            symbol = "sz" + code
        return "https://guba.sina.cn/?" + f"s=bar&name={symbol}&from=redirect"
    return ""


def load_results_from_db(
    db_path: Path | str,
) -> Tuple[
    Dict[str, Dict[str, object]],
    List[str],
    Dict[str, List[str]],
    Dict[str, object],
]:
    """读取历史档案，并还原评分函数所需的来源关系。

    返回 ``(results, market_keys, sector_source_keys, archive_meta)``。
    ``comments/clicks/likes`` 一律为 0；SQLite 中的真实采集时观察值保存在
    ``observedComments/observedClicks/observedLikes``，不会参与 ESTIMATED 评分。
    """

    path = Path(db_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"历史数据库不存在：{path}")
    # 构建器只读档案；URI 只读模式避免误创建文件，也不会改写采集状态。
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not {"meta", "sources", "posts"}.issubset(tables):
            raise RuntimeError("数据库不是 collect_history_60d.py 生成的有效档案")
        meta = {
            str(row["key"]): str(row["value"])
            for row in conn.execute("SELECT key, value FROM meta").fetchall()
        }
        source_rows = conn.execute(
            "SELECT * FROM sources ORDER BY platform, source_key"
        ).fetchall()
        if not source_rows:
            raise RuntimeError("数据库没有来源状态")

        configured_specs, configured_market_keys, configured_sector_keys = core.build_source_specs()
        configured_key_set = set(configured_specs)
        archived_key_set = {str(row["source_key"]) for row in source_rows}
        usable_key_set = configured_key_set & archived_key_set
        results: Dict[str, Dict[str, object]] = {}
        market_keys: List[str] = [
            key for key in configured_market_keys if key in usable_key_set
        ]
        sector_source_keys: Dict[str, List[str]] = {
            sector_key: [key for key in keys if key in usable_key_set]
            for sector_key, keys in configured_sector_keys.items()
        }
        current_membership: Dict[str, List[str]] = defaultdict(list)
        for key in market_keys:
            current_membership[key].append("__market__")
        for sector_key, keys in sector_source_keys.items():
            for key in keys:
                current_membership[key].append(sector_key)
        source_states: List[Dict[str, object]] = []
        for row in source_rows:
            key = str(row["source_key"])
            if key not in usable_key_set:
                continue
            platform = str(row["platform"])
            code = str(row["code"])
            try:
                archived_sectors = [str(item) for item in json.loads(row["sectors_json"] or "[]")]
            except (json.JSONDecodeError, TypeError):
                archived_sectors = []
            sectors = sorted(set(current_membership.get(key) or ()))
            results[key] = {
                "key": key,
                "code": code,
                "name": str(row["name"]),
                "platform": platform,
                "url": source_url(platform, code),
                "posts": [],
                "fetchedAt": str(row["updated_at"]),
                "fresh": False,
                "historicalArchive": True,
                "sectors": sectors,
                "archivedSectors": archived_sectors,
            }
            source_states.append({
                "key": key,
                "platform": platform,
                "code": code,
                "sectors": sectors,
                "archivedSectors": archived_sectors,
                "status": str(row["status"]),
                "totalRequests": int(row["total_requests"] or 0),
                "sampleIndex": int(row["sample_index"] or 0)
                if "sample_index" in row.keys() else 0,
                "oldestSeen": row["oldest_seen"],
                "newestSeen": row["newest_seen"],
                "pastCutoffPages": int(row["past_cutoff_pages"] or 0),
                "lastError": row["last_error"],
            })

        post_rows = conn.execute(
            """
            SELECT platform, source_key, post_id, source_code, source_name,
                   published_at, observed_at, title, author_hash,
                   comments, clicks, likes, kind
            FROM posts
            ORDER BY published_at, source_key, post_id
            """
        )
        post_count = 0
        for row in post_rows:
            source_key = str(row["source_key"])
            if source_key not in results:
                continue
            try:
                published = datetime.fromisoformat(str(row["published_at"]))
            except ValueError:
                continue
            if published.tzinfo is None:
                published = published.replace(tzinfo=CN_TZ)
            else:
                published = published.astimezone(CN_TZ)
            platform = str(row["platform"])
            raw_id = str(row["post_id"])
            results[source_key]["posts"].append({
                "id": raw_id if raw_id.startswith(platform + ":") else f"{platform}:{raw_id}",
                "title": str(row["title"]),
                "published": published,
                "publishTime": published.isoformat(),
                # 历史估算必须忽略采集时已经累计的互动数。
                "comments": 0,
                "clicks": 0,
                "likes": 0,
                "observedComments": int(row["comments"] or 0),
                "observedClicks": int(row["clicks"] or 0),
                "observedLikes": int(row["likes"] or 0),
                "collectedAt": str(row["observed_at"]),
                "authorKey": str(row["author_hash"]),
                "authorHash": str(row["author_hash"]),
                "sourceKey": source_key,
                "sourceCode": str(row["source_code"]),
                "source": str(row["source_name"]),
                "platform": platform,
                "platformName": core.PLATFORM_NAMES.get(platform, platform),
                "kind": str(row["kind"] or "post"),
                "historicalEstimated": True,
            })
            post_count += 1

        archive_meta: Dict[str, object] = {
            "dbPath": str(path),
            "windowAsOf": meta.get("window_as_of"),
            "scoreStart": meta.get("score_start"),
            "rawCutoff": meta.get("raw_cutoff"),
            "strategy": meta.get("collection_strategy"),
            "sinaTargetSpacingDays": int(meta.get("sina_target_spacing_days") or 2),
            "eastmoneySourceRequestCap": int(meta.get("eastmoney_source_request_cap") or 14),
            "sinaSourceRequestCap": int(meta.get("sina_source_request_cap") or 50),
            "interactionCountsIgnored": True,
            "sourceCount": len(results),
            "configuredSourceCount": len(configured_specs),
            "archiveSourceCount": len(source_rows),
            "postCount": post_count,
            "sourceStates": source_states,
        }
        return results, sorted(market_keys), sector_source_keys, archive_meta
    finally:
        conn.close()


def parse_stored_datetime(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def archive_completeness(archive_meta: Mapping[str, object]) -> float:
    """估算 0–1 档案完整度；仅用于置信度，不参与指数值。"""

    raw_cutoff = parse_stored_datetime(archive_meta.get("rawCutoff"))
    as_of = parse_stored_datetime(archive_meta.get("windowAsOf"))
    span = max(1.0, (as_of - raw_cutoff).total_seconds()) if raw_cutoff and as_of else 1.0
    span_days = (
        (as_of.date() - raw_cutoff.date()).days + 1
        if raw_cutoff and as_of else 1
    )
    strategy = str(archive_meta.get("strategy") or "")
    spacing = max(1, int(archive_meta.get("sinaTargetSpacingDays") or 2))
    east_cap = max(1, int(archive_meta.get("eastmoneySourceRequestCap") or 14))
    sina_cap = max(1, int(archive_meta.get("sinaSourceRequestCap") or 50))
    ratios: List[float] = []
    for state in archive_meta.get("sourceStates", []):
        if not isinstance(state, Mapping):
            continue
        status = str(state.get("status") or "")
        if status == "complete":
            ratios.append(1.0)
            continue
        if strategy == "representative":
            platform = str(state.get("platform") or "")
            if platform == "sina":
                expected_targets = min(sina_cap, math.ceil(span_days / spacing))
                ratios.append(min(1.0, float(state.get("sampleIndex") or 0) / expected_targets))
                continue
            if platform == "eastmoney":
                if int(state.get("pastCutoffPages") or 0) >= 2:
                    ratios.append(1.0)
                else:
                    ratios.append(min(0.95, float(state.get("totalRequests") or 0) / east_cap))
                continue
        oldest = parse_stored_datetime(state.get("oldestSeen"))
        newest = parse_stored_datetime(state.get("newestSeen"))
        if raw_cutoff and oldest and oldest <= raw_cutoff:
            ratios.append(0.95 if status == "capped" else 1.0)
        elif oldest and newest:
            ratios.append(min(0.9, max(0.0, (newest - oldest).total_seconds() / span)))
        else:
            ratios.append(0.0)
    observed_ratio = sum(ratios) / len(ratios) if ratios else 0.0
    configured_count = max(1, int(archive_meta.get("configuredSourceCount") or len(ratios) or 1))
    matched_count = max(0, int(archive_meta.get("sourceCount") or len(ratios)))
    return observed_ratio * min(1.0, matched_count / configured_count)


def resolve_date_range(
    args: argparse.Namespace,
    archive_meta: Mapping[str, object],
) -> Tuple[date, date]:
    end_text = args.end or str(archive_meta.get("windowAsOf") or "")[:10]
    if not end_text:
        raise ValueError("数据库缺少历史窗口，请显式传 --start 和 --end")
    try:
        end = date.fromisoformat(str(end_text)[:10])
    except ValueError as exc:
        raise ValueError("--start/--end 需要 YYYY-MM-DD") from exc
    if args.start:
        try:
            start = date.fromisoformat(str(args.start)[:10])
        except ValueError as exc:
            raise ValueError("--start/--end 需要 YYYY-MM-DD") from exc
    else:
        # 新模型只需要目标日及此前两个交易日，不再为旧30日窗口牺牲
        # 一整月历史。默认重算截至档案末端的最近60个交易日。
        desired = previous_trading_days(latest_trading_day(end), 60)
        start = desired[-1]
        raw_cutoff = parse_stored_datetime(archive_meta.get("rawCutoff"))
        if raw_cutoff is not None:
            cursor = raw_cutoff.date()
            safe_sessions: List[date] = []
            while len(safe_sessions) < 3:
                if is_trading_day(cursor):
                    safe_sessions.append(cursor)
                cursor += timedelta(days=1)
            start = max(start, safe_sessions[-1])
    if start > end:
        raise ValueError("--start 不能晚于 --end")
    if (end - start).days > 365:
        raise ValueError("一次最多重算 366 个自然日")
    return start, end


def complete_retrospective_end(
    requested_end: date,
    archive_meta: Mapping[str, object],
) -> Tuple[date, Optional[date]]:
    """只允许完整采集完的交易日进入回溯估算。

    旧采集器把纯日期错误地固定为当天 00:00。该日只有零点以前的帖子，
    不能被报告成完整估算日；页面已有的 measured 点会继续保护当天结果。
    """

    collector_as_of = parse_stored_datetime(archive_meta.get("windowAsOf"))
    if collector_as_of is None or collector_as_of.date() > requested_end:
        return latest_trading_day(requested_end), None
    if collector_as_of.time() >= datetime_time(23, 59):
        # 显式 --end 不能越过档案真正覆盖到的最后一天。
        return latest_trading_day(min(requested_end, collector_as_of.date())), None
    partial_day = collector_as_of.date()
    return latest_trading_day(min(requested_end, partial_day - timedelta(days=1))), partial_day


def upgrade_latest_measured(
    history: Sequence[Mapping[str, object]],
    latest: Mapping[str, object],
) -> Tuple[List[Dict[str, object]], List[str]]:
    """把旧版扁平实测点补成 v2 结构，不改变当日任何指数值。"""

    market_date = str(latest.get("marketDate") or "")
    scores = latest.get("scores") if isinstance(latest.get("scores"), Mapping) else {}
    signals = latest.get("signals") if isinstance(latest.get("signals"), Mapping) else {}
    sectors = latest.get("sectorIndices") if isinstance(latest.get("sectorIndices"), list) else []
    diagnostics = latest.get("diagnostics") if isinstance(latest.get("diagnostics"), Mapping) else {}
    if not market_date or not scores or not signals or not sectors:
        return [dict(item) for item in history if isinstance(item, Mapping)], []

    target = next((
        item for item in history
        if isinstance(item, Mapping)
        and str(item.get("date") or "") == market_date
        and record_type_of(item) == MEASURED
    ), None)
    if target is None or int(target.get("schemaVersion") or 0) >= 2:
        return [dict(item) for item in history if isinstance(item, Mapping)], []
    try:
        scores_match = all(
            float(target.get(key) or 0) == float(scores.get(key) or 0)
            for key in ("baoma", "novice", "retail", "composite")
        )
    except (TypeError, ValueError):
        scores_match = False
    if not scores_match:
        return [dict(item) for item in history if isinstance(item, Mapping)], []

    platform_rows = latest.get("community", {}).get("platforms", []) if isinstance(latest.get("community"), Mapping) else []
    configured_seen = sum(
        int(item.get("sourceCount") or 0)
        for item in platform_rows if isinstance(item, Mapping)
    )
    failures = latest.get("failures") if isinstance(latest.get("failures"), list) else []
    source_count = int(diagnostics.get("sourceCount") or configured_seen)
    expected_source_count = max(1, source_count, configured_seen + len(failures))
    def sector_available(item: Mapping[str, object]) -> bool:
        metrics = item.get("metrics") if isinstance(item.get("metrics"), Mapping) else {}
        return bool(item.get("available", int(metrics.get("samplePosts") or 0) > 0))

    sector_count = int(
        diagnostics.get("availableSectorCount")
        or sum(sector_available(item) for item in sectors if isinstance(item, Mapping))
    )
    as_of = str(latest.get("updatedAt") or market_date)
    upgraded = build_history_point(
        as_of=as_of,
        scores=scores,
        signals=signals,
        sectors=sectors,
        platform_count=int(diagnostics.get("platformCount") or sum(
            int(item.get("sourceCount") or 0) > 0
            for item in platform_rows if isinstance(item, Mapping)
        )),
        expected_platform_count=len(core.PLATFORM_NAMES),
        source_count=source_count,
        expected_source_count=expected_source_count,
        sector_count=sector_count,
        expected_sector_count=len(core.SECTORS),
        sample_posts=int(diagnostics.get("allSamplePosts") or diagnostics.get("samplePosts") or 0),
        record_type=MEASURED,
        fresh_source_count=int(diagnostics.get("freshSourceCount") or source_count),
        captured_at=str(latest.get("updatedAt") or as_of),
        methodology=str(latest.get("methodology") or "community_text_multiplatform_v2"),
    )
    return upsert_history(history, upgraded), [market_date]


def rebuild_stale_measured_points(
    history: Sequence[Mapping[str, object]],
    market_keys: Sequence[str],
    sector_source_keys: Mapping[str, Sequence[str]],
    *,
    expected_source_count: int,
) -> Tuple[List[Dict[str, object]], List[str], List[str], List[Dict[str, str]]]:
    """用按发布日期保存的原始档案迁移旧口径实测点。

    只处理仍是旧方法或旧热度模型的 measured 日期；并以原 ``capturedAt``
    截断首次采集时间，避免把后来才发现的旧帖伪装成当时已经看到的帖子。
    """

    archive_dir = core.HISTORY_PATH.parent / "community_archive"
    if not archive_dir.is_dir():
        return [dict(item) for item in history if isinstance(item, Mapping)], [], [], []
    rebuilt = [dict(item) for item in history if isinstance(item, Mapping)]
    rescored: List[str] = []
    demoted_to_estimated: List[str] = []
    failures: List[Dict[str, str]] = []
    for point in list(rebuilt):
        if record_type_of(point) != MEASURED:
            continue
        method = str(point.get("methodology") or "")
        sectors = point.get("sectors") if isinstance(point.get("sectors"), list) else []
        stale_heat = any(
            not isinstance(item.get("heatModel"), Mapping)
            or str((item.get("heatModel") or {}).get("version") or "") != HEAT_MODEL_VERSION
            for item in sectors if isinstance(item, Mapping)
        )
        point_sector_keys = {
            str(item.get("key"))
            for item in sectors
            if isinstance(item, Mapping) and item.get("key")
        }
        stale_catalog = point_sector_keys != set(core.SECTORS)
        if "three_trading_day" in method and not stale_heat and not stale_catalog:
            continue
        try:
            market_day = date.fromisoformat(str(point.get("date") or ""))
        except ValueError:
            continue
        captured = parse_stored_datetime(point.get("capturedAt")) or as_of_datetime(market_day)
        if captured.date() != market_day:
            captured = as_of_datetime(market_day)
        window = previous_trading_days(market_day, 3)
        archive_start = datetime.combine(window[-1], datetime_time.min, tzinfo=CN_TZ)
        try:
            archived_results = community_archive.load_archived_results(
                archive_dir,
                start=archive_start,
                end=captured,
                observed_at=captured,
            )
            replacement_type = MEASURED
            scoring_cutoff = captured
            if not archived_results:
                # 旧实测发生在增量档案启用前时，无法诚实恢复“当时看见的页面”。
                # 改用完整发布日期档案做无互动回溯，并明确降级为 estimated。
                scoring_cutoff = as_of_datetime(market_day)
                archived_results = community_archive.load_archived_results(
                    archive_dir,
                    start=archive_start,
                    end=scoring_cutoff,
                )
                replacement_type = ESTIMATED
            if not archived_results:
                raise RuntimeError("归档窗口没有可用于迁移的帖子")
            scored = core.score_results_as_of(
                archived_results,
                market_keys,
                sector_source_keys,
                as_of=scoring_cutoff,
                record_type=replacement_type,
            )
            broad = scored["broad"]
            sector_rows = scored["sectorIndices"]
            sample_posts = int(broad["diagnostics"]["samplePosts"]) + sum(
                int(item["metrics"]["samplePosts"]) for item in sector_rows
            )
            replacement = build_history_point(
                as_of=scoring_cutoff,
                scores=scored["scores"],
                signals=scored["signals"],
                sectors=sector_rows,
                platform_count=int(scored["availablePlatformCount"]),
                expected_platform_count=len(core.PLATFORM_NAMES),
                source_count=int(scored["availableSourceCount"]),
                expected_source_count=expected_source_count,
                sector_count=int(scored["availableSectorCount"]),
                expected_sector_count=len(core.SECTORS),
                sample_posts=sample_posts,
                record_type=replacement_type,
                fresh_source_count=(
                    int(scored["availableSourceCount"])
                    if replacement_type == MEASURED else None
                ),
                archive_completeness=1.0 if replacement_type == ESTIMATED else None,
                recent_published_platform_count=(
                    len(recent_published_platforms(archived_results, scoring_cutoff, days=7))
                    if replacement_type == ESTIMATED else None
                ),
                recent_published_platforms=(
                    recent_published_platforms(archived_results, scoring_cutoff, days=7)
                    if replacement_type == ESTIMATED else None
                ),
                captured_at=(captured if replacement_type == MEASURED else datetime.now(CN_TZ)),
                methodology=(
                    "community_text_company_equal_three_trading_day_v2"
                    if replacement_type == MEASURED
                    else "community_text_archive_company_equal_three_trading_day_v2"
                ),
            )
            if replacement_type == ESTIMATED:
                rebuilt = [
                    item for item in rebuilt
                    if str(item.get("date") or "") != market_day.isoformat()
                ]
            rebuilt = upsert_history(rebuilt, replacement)
            if replacement_type == MEASURED:
                rescored.append(market_day.isoformat())
            else:
                demoted_to_estimated.append(market_day.isoformat())
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            failures.append({"date": market_day.isoformat(), "reason": str(exc)})
    return rebuilt, rescored, demoted_to_estimated, failures


def recent_published_platforms(
    results: Mapping[str, Dict[str, object]],
    as_of: datetime | date | str,
    *,
    days: int = 7,
) -> List[str]:
    """精确统计截至当日、最近若干交易日内真实发帖的平台。

    这里只读取帖子自身的 ``published`` / ``publishTime``，不使用采集时间、
    当前互动量或来源状态，避免把后来观察到的信息误当成历史当日可见信息。
    """

    cutoff = as_of_datetime(as_of)
    trading_dates = set(previous_trading_days(cutoff.date(), max(1, int(days))))
    platforms = set()
    for result in results.values():
        if not isinstance(result, Mapping):
            continue
        result_platform = str(result.get("platform") or "")
        posts = result.get("posts") if isinstance(result.get("posts"), list) else []
        for post in posts:
            if not isinstance(post, Mapping):
                continue
            published = post.get("published")
            if not isinstance(published, datetime):
                published = core.parse_time(post.get("publishTime"))
            if not isinstance(published, datetime):
                continue
            instant = (
                published.replace(tzinfo=cutoff.tzinfo)
                if published.tzinfo is None
                else published.astimezone(cutoff.tzinfo)
            )
            if instant.date() in trading_dates and instant <= cutoff:
                platform = str(post.get("platform") or result_platform)
                if platform:
                    platforms.add(platform)
    return sorted(platforms)


def score_history_points(
    results: Mapping[str, Dict[str, object]],
    market_keys: Sequence[str],
    sector_source_keys: Mapping[str, Sequence[str]],
    *,
    start: date,
    end: date,
    expected_source_count: int,
    completeness: float,
    captured_at: datetime,
) -> Tuple[List[Dict[str, object]], List[Dict[str, str]]]:
    points: List[Dict[str, object]] = []
    skipped: List[Dict[str, str]] = []
    current = start
    while current <= end:
        if not is_trading_day(current):
            current += timedelta(days=1)
            continue
        cutoff = as_of_datetime(current)
        try:
            published_platforms = recent_published_platforms(results, cutoff, days=7)
            scored = core.score_results_as_of(
                results,
                market_keys,
                sector_source_keys,
                as_of=cutoff,
                record_type=ESTIMATED,
            )
            broad = scored["broad"]
            sectors = scored["sectorIndices"]
            sample_posts = int(broad["diagnostics"]["samplePosts"]) + sum(
                int(item["metrics"]["samplePosts"]) for item in sectors
            )
            point = build_history_point(
                as_of=cutoff,
                scores=scored["scores"],
                signals=scored["signals"],
                sectors=sectors,
                platform_count=int(scored["availablePlatformCount"]),
                # 历史采集器当前覆盖东财/新浪；仍以项目三平台为完整基准，
                # 缺少同花顺体现在 coverage，而不以 50 分注入指数。
                expected_platform_count=len(core.PLATFORM_NAMES),
                source_count=int(scored["availableSourceCount"]),
                expected_source_count=expected_source_count,
                sector_count=int(scored["availableSectorCount"]),
                expected_sector_count=len(core.SECTORS),
                sample_posts=sample_posts,
                record_type=ESTIMATED,
                archive_completeness=completeness,
                recent_published_platform_count=len(published_platforms),
                recent_published_platforms=published_platforms,
                captured_at=captured_at,
                methodology="community_text_retrospective_three_trading_day_v1",
            )
            points.append(point)
        except RuntimeError as exc:
            skipped.append({"date": current.isoformat(), "reason": str(exc)})
        current += timedelta(days=1)
    return points, skipped


def replay_causal_sector_heat(
    history: Sequence[Mapping[str, object]],
) -> List[Dict[str, object]]:
    """沿最终可见日期链重放板块热度，兼容实测夹心与日期缺口。"""

    ordered = sorted(
        (item for item in history if isinstance(item, dict)),
        key=lambda item: str(item.get("date") or ""),
    )
    previous_point: Mapping[str, object] | None = None
    for point in ordered:
        sectors = point.get("sectors") if isinstance(point.get("sectors"), list) else []
        if sectors and any(
            item.get("relativeHeat") is None
            or not isinstance(item.get("heatModel"), Mapping)
            or str((item.get("heatModel") or {}).get("version") or "")
            != str(HEAT_MODEL_VERSION)
            for item in sectors if isinstance(item, Mapping)
        ):
            core.calibrate_sector_heats(sectors, float(point.get("heat") or 50.0))
        if previous_point is not None and sectors:
            try:
                gap_days = max(1, trading_day_gap(
                    date.fromisoformat(str(previous_point.get("date"))),
                    date.fromisoformat(str(point.get("date"))),
                ))
            except ValueError:
                gap_days = 1
            core.stabilize_sector_heats(
                sectors,
                previous_point.get("sectors", []),
                float(point.get("heat") or 50.0),
                gap_days=gap_days,
            )
        previous_point = point
    return ordered


def sync_latest_sector_heat_from_history(
    latest: Dict[str, object],
    history: Sequence[Mapping[str, object]],
) -> bool:
    """把 marketDate 对应历史点的展示热度同步回首页丰富板块对象。"""

    market_date = str(latest.get("marketDate") or "")
    latest_sectors = latest.get("sectorIndices")
    if not market_date or not isinstance(latest_sectors, list):
        return False
    point = next((
        item for item in history
        if str(item.get("date") or "") == market_date
        and isinstance(item.get("sectors"), list)
    ), None)
    if point is None:
        return False
    history_by_key = {
        str(item.get("key")): item
        for item in point.get("sectors", [])
        if isinstance(item, Mapping) and item.get("key")
    }
    synced = 0
    for item in latest_sectors:
        if not isinstance(item, dict):
            continue
        source = history_by_key.get(str(item.get("key")))
        if source is None:
            continue
        for field in (
            "heat", "legacyHeat", "absoluteHeat", "relativeHeat",
            "heatRank", "heatTier",
        ):
            if field in source:
                item[field] = source[field]
        source_model = source.get("heatModel") if isinstance(source.get("heatModel"), Mapping) else {}
        target_model = item.get("heatModel") if isinstance(item.get("heatModel"), Mapping) else {}
        item["heatModel"] = {**dict(target_model), **dict(source_model)}
        synced += 1
    if synced != len(history_by_key) or not synced:
        return False

    diagnostics = latest.get("diagnostics") if isinstance(latest.get("diagnostics"), dict) else {}
    audit = diagnostics.get("sectorHeatModel") if isinstance(diagnostics.get("sectorHeatModel"), Mapping) else {}
    final_values = [float(item.get("heat") or 0.0) for item in latest_sectors if isinstance(item, Mapping)]
    relative_values = [float(item.get("relativeHeat") or 50.0) for item in latest_sectors if isinstance(item, Mapping)]
    first_model = next((
        item.get("heatModel") for item in latest_sectors
        if isinstance(item, Mapping) and isinstance(item.get("heatModel"), Mapping)
    ), {})
    diagnostics["sectorHeatModel"] = {
        **dict(audit),
        **{
            key: first_model[key]
            for key in (
                "version", "status", "marketAnchor", "temporalSmoothing",
                "temporalAlpha", "temporalGapDays", "temporalEffectiveAlpha",
                "relativeDailyCap", "finalDailyCap", "usesMarketPrice",
                "formalPersonaScoresChanged",
            )
            if key in first_model
        },
        "relativeRange": round(max(relative_values) - min(relative_values), 1),
        "finalRange": round(max(final_values) - min(final_values), 1),
        "stabilizedSectorCount": synced,
    }
    latest["diagnostics"] = diagnostics
    return True


def backup_files(paths: Sequence[Path]) -> Optional[Path]:
    existing = [path for path in paths if path.exists()]
    if not existing:
        return None
    stamp = datetime.now(CN_TZ).strftime("%Y%m%d_%H%M%S")
    backup_dir = DATA_DIR / "backups" / f"history_build_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    for path in existing:
        shutil.copy2(path, backup_dir / path.name)
    return backup_dir


def build(args: argparse.Namespace) -> int:
    results, market_keys, sector_source_keys, archive_meta = load_results_from_db(args.db)
    start, requested_end = resolve_date_range(args, archive_meta)
    estimated_end, partial_collector_day = complete_retrospective_end(requested_end, archive_meta)
    if estimated_end < start:
        raise RuntimeError("采集窗口没有完整交易日可用于回溯估算")
    completeness = archive_completeness(archive_meta)
    captured_at = datetime.now(CN_TZ)
    points, skipped = score_history_points(
        results,
        market_keys,
        sector_source_keys,
        start=start,
        end=estimated_end,
        expected_source_count=int(archive_meta.get("configuredSourceCount") or archive_meta["sourceCount"]),
        completeness=completeness,
        captured_at=captured_at,
    )
    if not points:
        raise RuntimeError("没有任何日期生成可用估算点；请先继续采集历史文本")

    existing_history = load_json(core.HISTORY_PATH, [])
    if not isinstance(existing_history, list):
        existing_history = []
    latest_raw = load_json(core.LATEST_PATH, {})
    latest = dict(latest_raw) if isinstance(latest_raw, Mapping) else {}
    existing_history, measured_upgraded = upgrade_latest_measured(existing_history, latest)
    # 新口径只保存交易日。旧周末或法定休市日点不进入图表、统计或后续
    # 板块热度的因果链。
    existing_history = [
        item for item in existing_history
        if isinstance(item, Mapping)
        and item.get("date")
        and is_trading_day(str(item.get("date")))
    ]
    (
        existing_history,
        measured_rescored,
        measured_demoted_to_estimated,
        measured_rescore_failures,
    ) = rebuild_stale_measured_points(
        existing_history,
        market_keys,
        sector_source_keys,
        expected_source_count=int(archive_meta.get("configuredSourceCount") or archive_meta["sourceCount"]),
    )
    measured_before = {
        str(item.get("date")) for item in existing_history
        if isinstance(item, Mapping) and record_type_of(item) == MEASURED
    }
    merged = list(existing_history)
    for point in points:
        merged = upsert_history(merged, point)
    # 合并后沿“用户实际可见的日期链”统一重放因果平滑。这样即使未来
    # 出现估算→实测→估算的夹心结构，后续点也只会继承可见实测点，
    # 不会继承一条已经被实测覆盖的隐藏估算链。
    merged = replay_causal_sector_heat(merged)
    latest_heat_synchronized = sync_latest_sector_heat_from_history(latest, merged)
    measured_preserved = sorted(
        day for day in measured_before
        if any(
            isinstance(item, Mapping)
            and str(item.get("date")) == day
            and record_type_of(item) == MEASURED
            for item in merged
        )
    )
    estimated_merged = sum(
        record_type_of(item) == ESTIMATED
        and start.isoformat() <= str(item.get("date") or "") <= estimated_end.isoformat()
        for item in merged if isinstance(item, Mapping)
    )
    usable_estimated = sum(
        record_type_of(item) == ESTIMATED and quality_status_of(item) == QUALITY_USABLE
        for item in merged if isinstance(item, Mapping)
    )
    provisional_estimated = sum(
        record_type_of(item) == ESTIMATED and quality_status_of(item) == QUALITY_PROVISIONAL
        for item in merged if isinstance(item, Mapping)
    )
    quality_thresholds = {
        "archiveCompleteness": MIN_ARCHIVE_COMPLETENESS,
        "confidence": MIN_ESTIMATED_CONFIDENCE,
        "recent7PublishedPlatformCount": MIN_RECENT_PUBLISHED_PLATFORMS,
    }
    retrospective_archive = {
        "collectorAsOf": archive_meta.get("windowAsOf"),
        "requestedStartDate": start.isoformat(),
        "requestedEndDate": requested_end.isoformat(),
        "estimatedStartDate": start.isoformat(),
        "estimatedEndDate": estimated_end.isoformat(),
        "partialCollectorDateExcluded": (
            partial_collector_day.isoformat() if partial_collector_day else None
        ),
        "measuredDatesProtected": measured_preserved,
        "measuredDatesUpgraded": measured_upgraded,
        "measuredDatesRescored": measured_rescored,
        "measuredDatesDemotedToEstimated": measured_demoted_to_estimated,
        "measuredRescoreFailures": measured_rescore_failures,
        "interactionCountsIgnored": True,
        "archiveCompleteness": round(completeness * 100.0, 1),
        "qualityThresholds": quality_thresholds,
        "note": "回溯估算只生成A股交易日，并仅使用最近三个交易日的公开文本；不完整采集日由每日实测点承担。",
    }
    history_meta = build_history_meta(merged, updated_at=captured_at)
    history_meta["retrospectiveArchive"] = retrospective_archive

    build_report = {
        "generatedAt": captured_at.isoformat(),
        "db": str(Path(args.db).resolve()),
        "startDate": start.isoformat(),
        "endDate": requested_end.isoformat(),
        "requestedEndDate": requested_end.isoformat(),
        "estimatedEndDate": estimated_end.isoformat(),
        "collectorAsOf": archive_meta.get("windowAsOf"),
        "partialCollectorDateExcluded": (
            partial_collector_day.isoformat() if partial_collector_day else None
        ),
        "requestedDays": (requested_end - start).days + 1,
        "requestedTradingDays": sum(
            is_trading_day(start + timedelta(days=offset))
            for offset in range((requested_end - start).days + 1)
        ),
        "estimatedPointsBuilt": len(points),
        "estimatedPointsMerged": estimated_merged,
        "usableEstimatedPoints": usable_estimated,
        "provisionalEstimatedPoints": provisional_estimated,
        "qualityThresholds": quality_thresholds,
        "skipped": skipped,
        "measuredDatesRescored": measured_rescored,
        "measuredDatesDemotedToEstimated": measured_demoted_to_estimated,
        "measuredRescoreFailures": measured_rescore_failures,
        "archiveCompleteness": round(completeness * 100.0, 1),
        "sourceCount": int(archive_meta["sourceCount"]),
        "configuredSourceCount": int(archive_meta.get("configuredSourceCount") or archive_meta["sourceCount"]),
        "archiveSourceCount": int(archive_meta.get("archiveSourceCount") or archive_meta["sourceCount"]),
        "postCount": int(archive_meta["postCount"]),
        "marketSourceCount": len(market_keys),
        "sectorSourceCounts": {
            key: len(value) for key, value in sector_source_keys.items()
        },
        "interactionCountsIgnored": True,
        "measuredDatesProtected": measured_preserved,
        "measuredDatesUpgraded": measured_upgraded,
        "latestHeatSynchronized": latest_heat_synchronized,
        "historyMeta": history_meta,
    }

    print(
        f"离线重算完成：{start}~{estimated_end}，生成 {len(points)} 个估算点，"
        f"跳过 {len(skipped)} 天；档案完整度 {completeness * 100:.1f}%。"
    )
    if partial_collector_day:
        print(
            f"采集实际截止 {archive_meta.get('windowAsOf')}；"
            f"{partial_collector_day} 不作为完整回溯日，由实测点保护。"
        )
    if measured_preserved:
        print("已保护实测日期：" + "、".join(measured_preserved))
    if args.dry_run:
        print("当前为预演，不写入项目文件。")
        print(json.dumps(build_report, ensure_ascii=False, indent=2))
        return 0

    if not latest:
        raise RuntimeError("latest.json 缺失或异常，拒绝覆盖 data.js")
    backup_dir = None if args.no_backup else backup_files([
        core.HISTORY_PATH,
        core.HISTORY_META_PATH,
        core.LATEST_PATH,
        core.DATA_JS_PATH,
    ])
    latest["history"] = merged
    latest["historyMeta"] = history_meta
    latest["retrospectiveBuild"] = {
        "updatedAt": captured_at.isoformat(),
        "startDate": start.isoformat(),
        "endDate": estimated_end.isoformat(),
        "requestedEndDate": requested_end.isoformat(),
        "collectorAsOf": archive_meta.get("windowAsOf"),
        "partialCollectorDateExcluded": (
            partial_collector_day.isoformat() if partial_collector_day else None
        ),
        "estimatedPointsBuilt": len(points),
        "estimatedPointsMerged": estimated_merged,
        "usableEstimatedPoints": usable_estimated,
        "provisionalEstimatedPoints": provisional_estimated,
        "qualityThresholds": quality_thresholds,
        "archiveCompleteness": round(completeness * 100.0, 1),
        "interactionCountsIgnored": True,
        "latestHeatSynchronized": latest_heat_synchronized,
        "database": str(Path(args.db).resolve()),
    }
    sources = latest.get("sources") if isinstance(latest.get("sources"), dict) else {}
    sources["history"] = (
        "交易日历史由最近三个交易日的公开社区文本回溯估算；低于档案、置信度或发布平台门槛时标为待完善；互动量按0处理"
    )
    latest["sources"] = sources

    atomic_write_json(core.HISTORY_PATH, merged)
    atomic_write_json(core.HISTORY_META_PATH, history_meta)
    atomic_write_json(core.LATEST_PATH, latest)
    atomic_write_text(
        core.DATA_JS_PATH,
        "window.INDEX_DATA = " + json.dumps(latest, ensure_ascii=False, indent=2) + ";\n",
    )
    atomic_write_json(Path(args.build_report).resolve(), build_report)
    print(f"已写入 {len(merged)} 个历史点。")
    if backup_dir:
        print(f"原文件备份：{backup_dir}")
    print(f"构建报告：{Path(args.build_report).resolve()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从历史SQLite离线构建60日估算并安全合并项目页面数据",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="采集器SQLite路径")
    parser.add_argument("--start", help="历史第一天 YYYY-MM-DD；默认读取数据库")
    parser.add_argument("--end", help="历史最后一天 YYYY-MM-DD；默认读取数据库")
    parser.add_argument("--build-report", default=str(DEFAULT_BUILD_REPORT), help="构建审计报告")
    parser.add_argument("--dry-run", action="store_true", help="计算并显示结果，但不写项目文件")
    parser.add_argument("--no-backup", action="store_true", help="正式写入前不备份原文件")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return build(args)
    except (ValueError, RuntimeError, FileNotFoundError, sqlite3.Error) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
