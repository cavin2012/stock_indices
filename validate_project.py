#!/usr/bin/env python3
"""离线校验指数数据与页面载荷，避免更新后静默写入坏数据。"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from history_engine import (
    MIN_ARCHIVE_COMPLETENESS,
    MIN_ESTIMATED_CONFIDENCE,
    MIN_RECENT_PUBLISHED_PLATFORMS,
    QUALITY_MEASURED,
    QUALITY_PROVISIONAL,
    QUALITY_USABLE,
)
from trading_calendar import is_trading_day, latest_trading_day
from sector_catalog import SECTOR_CATALOG_VERSION, SECTORS


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
SCORE_KEYS = ("baoma", "novice", "retail", "composite")
SIGNAL_KEYS = ("optimism", "fomo", "panic", "newcomer", "intensity", "attention", "heat")
MIN_HISTORY_SCHEMA_VERSION = 2
QUALITY_SCHEMA_VERSION = 3
MEASURED = "measured"
ESTIMATED = "estimated"
LEGACY_SECTORS = {
    "communication", "semiconductor", "ai", "robotics", "consumer", "healthcare",
    "new_energy", "automobile", "finance", "real_estate", "military", "metals",
    "chemicals", "media", "utilities", "agriculture",
}
EXPECTED_SECTORS = set(SECTORS)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssertionError(f"无法读取 {path.name}: {exc}") from exc


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def check_sector_catalog(errors: list[str]) -> None:
    """校验细分板块目录，避免页面说明和真实采集入口悄悄漂移。"""

    require(len(EXPECTED_SECTORS) == 30, "细分板块目录必须恰好包含 30 个板块", errors)
    company_codes: list[str] = []
    parent_keys: set[str] = set()
    for key, definition in SECTORS.items():
        label = f"SECTORS[{key}]"
        require(bool(str(definition.get("name") or "").strip()), f"{label}.name 为空", errors)
        parent_key = str(definition.get("parentKey") or "").strip()
        parent_name = str(definition.get("parentName") or "").strip()
        require(bool(parent_key), f"{label}.parentKey 为空", errors)
        require(bool(parent_name), f"{label}.parentName 为空", errors)
        require(bool(str(definition.get("description") or "").strip()), f"{label}.description 为空", errors)
        parent_keys.add(parent_key)
        representatives = definition.get("representativeCompanies")
        require(isinstance(representatives, list), f"{label}.representativeCompanies 不是数组", errors)
        if not isinstance(representatives, list):
            continue
        require(len(representatives) == 3, f"{label} 必须恰好有 3 家典型公司", errors)
        for index, company in enumerate(representatives):
            company_label = f"{label}.representativeCompanies[{index}]"
            require(isinstance(company, Mapping), f"{company_label} 不是对象", errors)
            if not isinstance(company, Mapping):
                continue
            code = str(company.get("code") or "")
            require(bool(re.fullmatch(r"\d{6}", code)), f"{company_label}.code 不是6位股票代码", errors)
            require(bool(str(company.get("name") or "").strip()), f"{company_label}.name 为空", errors)
            require(bool(str(company.get("role") or "").strip()), f"{company_label}.role 为空", errors)
            company_codes.append(code)
    require(len(parent_keys) >= 6, "细分板块至少应归入 6 个清晰大类", errors)
    require(
        len(company_codes) == len(set(company_codes)),
        "典型公司必须固定一个主板块，不能跨细分板块重复计数",
        errors,
    )


def finite_score(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and 0.0 <= number <= 100.0


def check_score_map(payload: Any, label: str, keys: Iterable[str], errors: list[str]) -> None:
    require(isinstance(payload, dict), f"{label} 不是对象", errors)
    if not isinstance(payload, dict):
        return
    for key in keys:
        require(finite_score(payload.get(key)), f"{label}.{key} 不在 0-100", errors)


def check_history_sector(item: Any, label: str, errors: list[str]) -> str | None:
    require(isinstance(item, dict), f"{label} 不是对象", errors)
    if not isinstance(item, dict):
        return None
    key = str(item.get("key") or "")
    require(bool(key), f"{label}.key 为空", errors)
    check_score_map(item.get("scores"), f"{label}.scores", SCORE_KEYS, errors)
    check_score_map(item.get("signals"), f"{label}.signals", SIGNAL_KEYS, errors)
    require(finite_score(item.get("coverage")), f"{label}.coverage 不在 0-100", errors)
    require(finite_score(item.get("confidence")), f"{label}.confidence 不在 0-100", errors)
    return key or None


def expected_segments(history: list[Any]) -> list[dict[str, Any]]:
    """按类型、方法口径和A股交易日连续性重建分段。"""

    segments: list[dict[str, Any]] = []
    for point in history:
        if not isinstance(point, dict):
            continue
        kind = str(point.get("type") or "")
        methodology = str(point.get("methodology") or "legacy_unspecified")
        day_text = str(point.get("date") or "")
        try:
            current_day = date.fromisoformat(day_text)
        except ValueError:
            # 日期错误会由历史点校验单独报告；这里不生成误导性的分段。
            continue
        previous_day = (
            date.fromisoformat(str(segments[-1]["endDate"])) if segments else None
        )
        contiguous = (
            previous_day is not None
            and is_trading_day(current_day)
            and latest_trading_day(current_day - timedelta(days=1)) == previous_day
        )
        if (
            not segments
            or segments[-1]["type"] != kind
            or segments[-1].get("methodology") != methodology
            or not contiguous
        ):
            segments.append({
                "type": kind,
                "methodology": methodology,
                "startDate": day_text,
                "endDate": day_text,
                "count": 1,
            })
        else:
            segments[-1]["endDate"] = day_text
            segments[-1]["count"] = int(segments[-1]["count"]) + 1
    return segments


def check_history_meta(
    payload: Any,
    history: list[Any],
    dates: list[str],
    errors: list[str],
) -> None:
    require(isinstance(payload, dict), "history_meta.json 顶层不是对象", errors)
    if not isinstance(payload, dict):
        return

    schema_version = payload.get("schemaVersion")
    require(
        isinstance(schema_version, int)
        and not isinstance(schema_version, bool)
        and schema_version >= MIN_HISTORY_SCHEMA_VERSION,
        f"history_meta.schemaVersion 必须 >= {MIN_HISTORY_SCHEMA_VERSION}",
        errors,
    )
    measured_count = sum(
        isinstance(point, dict) and point.get("type") == MEASURED for point in history
    )
    estimated_count = sum(
        isinstance(point, dict) and point.get("type") == ESTIMATED for point in history
    )
    usable_estimated_count = sum(
        isinstance(point, dict)
        and point.get("type") == ESTIMATED
        and inferred_quality_status(point) == QUALITY_USABLE
        for point in history
    )
    provisional_estimated_count = sum(
        isinstance(point, dict)
        and point.get("type") == ESTIMATED
        and inferred_quality_status(point) == QUALITY_PROVISIONAL
        for point in history
    )
    require(payload.get("count") == len(history), "history_meta.count 与历史点数量不一致", errors)
    require(
        payload.get("startDate") == (dates[0] if dates else None),
        "history_meta.startDate 与历史起点不一致",
        errors,
    )
    require(
        payload.get("endDate") == (dates[-1] if dates else None),
        "history_meta.endDate 与历史终点不一致",
        errors,
    )
    require(
        payload.get("measuredCount") == measured_count,
        "history_meta.measuredCount 与实测点数量不一致",
        errors,
    )
    require(
        payload.get("estimatedCount") == estimated_count,
        "history_meta.estimatedCount 与估算点数量不一致",
        errors,
    )
    if isinstance(schema_version, int) and schema_version >= QUALITY_SCHEMA_VERSION:
        require(
            payload.get("usableEstimatedCount") == usable_estimated_count,
            "history_meta.usableEstimatedCount 与可用估算点数量不一致",
            errors,
        )
        require(
            payload.get("provisionalEstimatedCount") == provisional_estimated_count,
            "history_meta.provisionalEstimatedCount 与待完善估算点数量不一致",
            errors,
        )
    require(
        payload.get("segments") == expected_segments(history),
        "history_meta.segments 与历史类型/日期连续性不一致",
        errors,
    )
    if estimated_count:
        require(
            isinstance(payload.get("retrospectiveArchive"), Mapping),
            "历史含估算点，但 history_meta.retrospectiveArchive 缺失或无效",
            errors,
        )
        archive = payload.get("retrospectiveArchive")
        if isinstance(schema_version, int) and schema_version >= QUALITY_SCHEMA_VERSION and isinstance(archive, Mapping):
            thresholds = archive.get("qualityThresholds")
            require(isinstance(thresholds, Mapping), "retrospectiveArchive.qualityThresholds 缺失", errors)
            if isinstance(thresholds, Mapping):
                require(thresholds.get("archiveCompleteness") == MIN_ARCHIVE_COMPLETENESS, "回溯档案完整度门槛不一致", errors)
                require(thresholds.get("confidence") == MIN_ESTIMATED_CONFIDENCE, "回溯置信度门槛不一致", errors)
                require(thresholds.get("recent7PublishedPlatformCount") == MIN_RECENT_PUBLISHED_PLATFORMS, "回溯7日发布平台门槛不一致", errors)


def inferred_quality_status(point: Mapping[str, Any]) -> str:
    if point.get("type") == MEASURED:
        return QUALITY_MEASURED
    explicit = str(point.get("qualityStatus") or "").lower()
    if explicit in {QUALITY_USABLE, QUALITY_PROVISIONAL}:
        return explicit
    confidence = point.get("confidence")
    return QUALITY_USABLE if finite_score(confidence) and float(confidence) >= MIN_ESTIMATED_CONFIDENCE else QUALITY_PROVISIONAL


def check_quality(point: Mapping[str, Any], label: str, errors: list[str]) -> None:
    """校验新版显式质量门槛；旧版 estimated 仅按 confidence 向后兼容。"""

    schema_version = point.get("schemaVersion")
    point_type = point.get("type")
    explicit = point.get("qualityStatus")
    details = point.get("qualityDetails")
    if point_type == MEASURED:
        if isinstance(schema_version, int) and schema_version >= QUALITY_SCHEMA_VERSION:
            require(explicit == QUALITY_MEASURED, f"{label}.qualityStatus 应为 measured", errors)
            require(isinstance(details, Mapping), f"{label}.qualityDetails 缺失", errors)
            if isinstance(details, Mapping):
                require(details.get("passed") is True, f"{label}.qualityDetails.passed 应为 true", errors)
                require(details.get("basis") == "daily_measured_snapshot", f"{label}.qualityDetails.basis 无效", errors)
        return

    if not isinstance(schema_version, int) or schema_version < QUALITY_SCHEMA_VERSION:
        if explicit is not None:
            require(
                explicit == inferred_quality_status(point),
                f"{label}.qualityStatus 与旧版 confidence 推断不一致",
                errors,
            )
        return

    require(explicit in {QUALITY_USABLE, QUALITY_PROVISIONAL}, f"{label}.qualityStatus 无效", errors)
    require(isinstance(details, Mapping), f"{label}.qualityDetails 缺失", errors)
    if not isinstance(details, Mapping):
        return
    archive = details.get("archiveCompleteness")
    confidence = details.get("confidence")
    platform_count = details.get("recent7PublishedPlatformCount")
    require(finite_score(archive), f"{label}.qualityDetails.archiveCompleteness 无效", errors)
    require(confidence == point.get("confidence"), f"{label}.qualityDetails.confidence 与点位不一致", errors)
    require(
        isinstance(platform_count, int) and not isinstance(platform_count, bool) and platform_count >= 0,
        f"{label}.qualityDetails.recent7PublishedPlatformCount 无效",
        errors,
    )
    require(details.get("archiveCompletenessThreshold") == MIN_ARCHIVE_COMPLETENESS, f"{label} 档案门槛不一致", errors)
    require(details.get("confidenceThreshold") == MIN_ESTIMATED_CONFIDENCE, f"{label} 置信度门槛不一致", errors)
    require(details.get("recent7PublishedPlatformThreshold") == MIN_RECENT_PUBLISHED_PLATFORMS, f"{label} 7日平台门槛不一致", errors)
    require(details.get("publishedWindowDays") == 7, f"{label}.qualityDetails.publishedWindowDays 应为7", errors)
    require(details.get("usesPublishedAt") is True, f"{label}.qualityDetails 必须基于 publishedAt", errors)
    platforms = details.get("recent7PublishedPlatforms")
    require(isinstance(platforms, list), f"{label}.qualityDetails.recent7PublishedPlatforms 不是数组", errors)
    if isinstance(platforms, list) and isinstance(platform_count, int):
        require(len(set(map(str, platforms))) == len(platforms), f"{label} 7日发布平台存在重复", errors)
        require(len(platforms) == platform_count, f"{label} 7日发布平台列表与数量不一致", errors)
    passed = (
        finite_score(archive)
        and float(archive) >= MIN_ARCHIVE_COMPLETENESS
        and confidence == point.get("confidence")
        and finite_score(confidence)
        and float(confidence) >= MIN_ESTIMATED_CONFIDENCE
        and isinstance(platform_count, int)
        and platform_count >= MIN_RECENT_PUBLISHED_PLATFORMS
    )
    expected_status = QUALITY_USABLE if passed else QUALITY_PROVISIONAL
    require(explicit == expected_status, f"{label}.qualityStatus 与三道质量门槛不一致", errors)
    require(details.get("passed") is passed, f"{label}.qualityDetails.passed 不一致", errors)
    failed = set(details.get("failedChecks") or []) if isinstance(details.get("failedChecks"), list) else None
    expected_failed = set()
    if not finite_score(archive) or float(archive) < MIN_ARCHIVE_COMPLETENESS:
        expected_failed.add("archiveCompleteness")
    if not finite_score(confidence) or float(confidence) < MIN_ESTIMATED_CONFIDENCE:
        expected_failed.add("confidence")
    if not isinstance(platform_count, int) or platform_count < MIN_RECENT_PUBLISHED_PLATFORMS:
        expected_failed.add("recent7PublishedPlatformCount")
    require(failed == expected_failed, f"{label}.qualityDetails.failedChecks 不一致", errors)


def parse_data_js(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AssertionError(f"无法读取 data.js: {exc}") from exc
    match = re.fullmatch(r"\s*window\.INDEX_DATA\s*=\s*(\{.*\})\s*;\s*", text, re.S)
    if not match:
        raise AssertionError("data.js 不是有效的 window.INDEX_DATA 载荷")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise AssertionError(f"data.js 内 JSON 无效: {exc}") from exc


def validate(require_days: int, require_start: str | None) -> list[str]:
    errors: list[str] = []
    check_sector_catalog(errors)
    latest = load_json(DATA_DIR / "latest.json")
    history = load_json(DATA_DIR / "history.json")
    history_meta = load_json(DATA_DIR / "history_meta.json")
    data_js = parse_data_js(ROOT / "data.js")

    require(isinstance(latest, dict), "latest.json 顶层不是对象", errors)
    require(isinstance(history, list), "history.json 顶层不是数组", errors)
    if not isinstance(latest, dict) or not isinstance(history, list):
        return errors

    check_score_map(latest.get("scores"), "latest.scores", SCORE_KEYS, errors)
    check_score_map(latest.get("signals"), "latest.signals", SIGNAL_KEYS, errors)
    require(latest.get("history") == history, "latest.json.history 与 history.json 不一致", errors)
    require(
        latest.get("historyMeta") == history_meta,
        "latest.json.historyMeta 与 history_meta.json 不一致",
        errors,
    )
    require(data_js == latest, "data.js 与 latest.json 不一致", errors)
    require(
        str(latest.get("sectorCatalogVersion") or "") == str(SECTOR_CATALOG_VERSION),
        "latest.sectorCatalogVersion 与当前细分板块目录不一致",
        errors,
    )

    dates: list[str] = []
    for index, point in enumerate(history):
        label = f"history[{index}]"
        require(isinstance(point, dict), f"{label} 不是对象", errors)
        if not isinstance(point, dict):
            continue
        text_date = str(point.get("date") or "")
        try:
            parsed = date.fromisoformat(text_date)
        except ValueError:
            errors.append(f"{label}.date 无效: {text_date!r}")
            continue
        require(parsed <= date.today(), f"{label} 出现未来日期 {text_date}", errors)
        require(is_trading_day(parsed), f"{label} 不是A股交易日: {text_date}", errors)
        dates.append(text_date)

        schema_version = point.get("schemaVersion")
        require(
            isinstance(schema_version, int)
            and not isinstance(schema_version, bool)
            and schema_version >= MIN_HISTORY_SCHEMA_VERSION,
            f"{label}.schemaVersion 必须 >= {MIN_HISTORY_SCHEMA_VERSION}",
            errors,
        )
        for key in SCORE_KEYS:
            require(finite_score(point.get(key)), f"{label}.{key} 不在 0-100", errors)
        require(finite_score(point.get("coverage")), f"{label}.coverage 不在 0-100", errors)
        require(finite_score(point.get("confidence")), f"{label}.confidence 不在 0-100", errors)
        check_score_map(point.get("signals"), f"{label}.signals", SIGNAL_KEYS, errors)

        point_type = point.get("type")
        require(point_type in {MEASURED, ESTIMATED}, f"{label}.type 必须是 measured/estimated", errors)
        expected_mode = "daily" if point_type == MEASURED else "retrospective"
        expected_is_estimated = point_type == ESTIMATED
        require(
            point.get("mode") == expected_mode,
            f"{label}.mode 与 type 不一致，应为 {expected_mode}",
            errors,
        )
        require(
            type(point.get("isEstimated")) is bool
            and point.get("isEstimated") is expected_is_estimated,
            f"{label}.isEstimated 与 type 不一致",
            errors,
        )
        check_quality(point, label, errors)

        point_sectors = point.get("sectors")
        require(isinstance(point_sectors, list), f"{label}.sectors 不是数组", errors)
        if isinstance(point_sectors, list):
            sector_keys = [
                key
                for sector_index, sector in enumerate(point_sectors)
                if (key := check_history_sector(
                    sector, f"{label}.sectors[{sector_index}]", errors
                )) is not None
            ]
            key_set = set(sector_keys)
            known_catalog = frozenset(key_set) in {
                frozenset(EXPECTED_SECTORS),
                frozenset(LEGACY_SECTORS),
            }
            require(
                len(point_sectors) == len(sector_keys) == len(key_set) and known_catalog,
                f"{label}.sectors 必须是当前 30 板块或可识别的旧 16 板块，且 key 不重复",
                errors,
            )
            if str(point.get("sectorCatalogVersion") or "") == str(SECTOR_CATALOG_VERSION):
                require(key_set == EXPECTED_SECTORS, f"{label} 标记为当前目录但板块集合不完整", errors)

    require(dates == sorted(dates), "历史日期没有升序排列", errors)
    require(len(dates) == len(set(dates)), "历史日期存在重复", errors)
    require(len(dates) >= require_days, f"历史只有 {len(dates)} 天，少于要求的 {require_days} 天", errors)
    if require_start:
        try:
            required_start = date.fromisoformat(require_start).isoformat()
        except ValueError:
            errors.append(f"--require-start 不是有效日期: {require_start!r}")
        else:
            require(bool(dates) and dates[0] <= required_start, f"历史起点 {dates[0] if dates else '无'} 晚于 {required_start}", errors)

    check_history_meta(history_meta, history, dates, errors)

    sectors = latest.get("sectorIndices")
    require(isinstance(sectors, list), "latest.sectorIndices 不是数组", errors)
    if isinstance(sectors, list):
        key_list = [str(item.get("key")) for item in sectors if isinstance(item, dict)]
        keys = set(key_list)
        require(
            len(sectors) == len(EXPECTED_SECTORS)
            and len(key_list) == len(EXPECTED_SECTORS)
            and len(keys) == len(EXPECTED_SECTORS),
            f"latest.sectorIndices 必须恰好包含 {len(EXPECTED_SECTORS)} 个不重复板块",
            errors,
        )
        require(keys == EXPECTED_SECTORS, f"板块集合不完整：缺少 {sorted(EXPECTED_SECTORS - keys)}，多出 {sorted(keys - EXPECTED_SECTORS)}", errors)
        for item in sectors:
            if not isinstance(item, dict):
                errors.append("sectorIndices 中有非对象元素")
                continue
            key = str(item.get("key") or "unknown")
            check_score_map(item.get("scores"), f"sector[{key}].scores", SCORE_KEYS, errors)
            check_score_map(item.get("signals"), f"sector[{key}].signals", SIGNAL_KEYS, errors)
            definition = SECTORS.get(key)
            if not isinstance(definition, Mapping):
                continue
            for field in ("name", "parentKey", "parentName", "description"):
                require(
                    item.get(field) == definition.get(field),
                    f"sector[{key}].{field} 与板块目录不一致",
                    errors,
                )
            require(
                item.get("representativeCompanies") == definition.get("representativeCompanies"),
                f"sector[{key}].representativeCompanies 与板块目录不一致",
                errors,
            )

    for filename in ("dashboard.html", "dashboard.js", "styles.css", "一键更新指数.cmd"):
        require((ROOT / filename).is_file(), f"缺少页面/入口文件 {filename}", errors)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="校验指数历史、最新快照和页面载荷")
    parser.add_argument("--require-days", type=int, default=1, help="至少要求多少个历史日期")
    parser.add_argument("--require-start", help="历史最晚必须回溯到该日期（YYYY-MM-DD）")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    try:
        errors = validate(max(0, args.require_days), args.require_start)
    except AssertionError as exc:
        errors = [str(exc)]
    if errors:
        print("校验失败：", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    if not args.quiet:
        print("校验通过：最新快照、历史、板块和网页载荷一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
