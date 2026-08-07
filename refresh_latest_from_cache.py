"""用最后一次完整论坛缓存重建当前快照，不联网。

该脚本只用于评分口径迁移或网络临时不可用时的维护；正常每日更新仍运行
``update_index.py`` 并要求拿到新鲜公开社区数据。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, Mapping

import update_index as core
from history_engine import MEASURED, build_history_meta, build_history_point, upsert_history
from trading_calendar import calendar_mode


def main() -> int:
    latest_raw = core.load_json(core.LATEST_PATH, {})
    history_raw = core.load_json(core.HISTORY_PATH, [])
    if not isinstance(latest_raw, Mapping) or not isinstance(history_raw, list):
        raise RuntimeError("当前快照或历史文件不可用")
    latest = dict(latest_raw)
    captured = core.parse_time(latest.get("updatedAt"))
    if captured is None:
        raise RuntimeError("当前快照缺少有效 updatedAt")
    results = core.load_forum_cache()
    specs, market_keys, sector_source_keys = core.build_source_specs()
    results = {
        key: value for key, value in results.items()
        if key in specs and isinstance(value, Mapping) and value.get("posts")
    }
    if not results:
        raise RuntimeError("最后一次完整论坛缓存为空")

    scored = core.score_results_as_of(
        results,
        market_keys,
        sector_source_keys,
        as_of=captured,
        record_type=MEASURED,
    )
    market_day = core.scoring_market_day(captured)
    date_key = market_day.isoformat()
    sectors = scored["sectorIndices"]
    prior_points = [
        point for point in history_raw
        if isinstance(point, Mapping)
        and str(point.get("date") or "") < date_key
        and isinstance(point.get("sectors"), list)
    ]
    if prior_points:
        prior = max(prior_points, key=lambda point: str(point.get("date") or ""))
        try:
            gap = core.trading_day_gap(
                datetime.strptime(str(prior.get("date")), "%Y-%m-%d").date(),
                market_day,
            )
        except ValueError:
            gap = 1
        temporal_audit = core.stabilize_sector_heats(
            sectors,
            prior.get("sectors", []),
            float(scored["signals"]["heat"]),
            gap_days=gap,
        )
        scored["sectorHeatAudit"] = {
            **dict(scored.get("sectorHeatAudit") or {}),
            **temporal_audit,
        }

    broad = scored["broad"]
    sample_total = int(broad["diagnostics"]["samplePosts"]) + sum(
        int(item["metrics"]["samplePosts"]) for item in sectors
    )
    history_point = build_history_point(
        as_of=captured,
        scores=scored["scores"],
        signals=scored["signals"],
        sectors=sectors,
        platform_count=int(scored["availablePlatformCount"]),
        expected_platform_count=len(core.PLATFORM_NAMES),
        source_count=int(scored["availableSourceCount"]),
        expected_source_count=len(specs),
        sector_count=int(scored["availableSectorCount"]),
        expected_sector_count=len(core.SECTORS),
        sample_posts=sample_total,
        record_type=MEASURED,
        fresh_source_count=int(scored["availableSourceCount"]),
        captured_at=captured,
        methodology="community_text_company_equal_three_trading_day_v2",
    )
    history = upsert_history(history_raw, history_point)
    history_meta = build_history_meta(history, updated_at=datetime.now(core.CN_TZ))
    old_meta = latest.get("historyMeta") if isinstance(latest.get("historyMeta"), Mapping) else {}
    if isinstance(old_meta.get("retrospectiveArchive"), Mapping):
        history_meta["retrospectiveArchive"] = old_meta["retrospectiveArchive"]

    sector_samples = []
    for item in sectors:
        sector_samples.extend(item.get("samplePosts", [])[:2])
    community_samples = list(broad.get("samplePosts", [])) + sector_samples
    community_samples.sort(key=lambda item: str(item.get("publishTime") or ""), reverse=True)
    platform_counts: Dict[str, int] = {
        platform: sum(1 for result in results.values() if result.get("platform") == platform)
        for platform in core.PLATFORM_NAMES
    }
    old_diagnostics = latest.get("diagnostics") if isinstance(latest.get("diagnostics"), Mapping) else {}
    diagnostics = {
        **dict(old_diagnostics),
        **dict(broad["diagnostics"]),
        "marketDate": date_key,
        "scoringTradingDays": [day.isoformat() for day in core.scoring_trading_days(captured)],
        "tradingDayWeights": list(core.TRADING_DAY_WEIGHTS),
        "calendarMode": calendar_mode(market_day),
        "sectorSamplePosts": sum(int(item["metrics"]["samplePosts"]) for item in sectors),
        "allSamplePosts": sample_total,
        "platformCount": int(scored["availablePlatformCount"]),
        "sourceCount": int(scored["availableSourceCount"]),
        "freshSourceCount": int(scored["availableSourceCount"]),
        "cachedSourceCount": len(results),
        "collectedSourceCount": len(results),
        "availableSectorCount": int(scored["availableSectorCount"]),
        "availableParentSectorCount": int(scored.get("availableParentSectorCount") or 0),
        "configuredSectorCount": len(core.SECTORS),
        "configuredParentSectorCount": len(core.PARENT_SECTORS),
        "sectorCatalogVersion": core.SECTOR_CATALOG_VERSION,
        "sectorCatalogId": core.SECTOR_CATALOG_ID,
        "sectorSourcePlan": {
            "eastmoneyStocksPerSector": 3,
            "sinaStocksPerSector": 1,
            "thsStocksPerSector": 1,
            "broadIndustryForumsUsed": False,
            "configuredSourceCount": len(specs),
        },
        "parentSectorSummary": scored.get("parentSectorSummary", []),
        "coverage": history_point["coverage"],
        "sectorHeatModel": scored.get("sectorHeatAudit", {}),
        "fairnessModelVersion": core.FAIRNESS_MODEL_VERSION,
        "rankEligibleSectorCount": sum(
            bool(item.get("metrics", {}).get("fairness", {}).get("rankEligible"))
            for item in sectors
        ),
        "lowConfidenceSectorCount": sum(
            item.get("metrics", {}).get("fairness", {}).get("confidenceLabel") == "低"
            for item in sectors
        ),
        "companyEqualWeighting": True,
        "missingEvidenceNeutralised": True,
        "thsExcludedFromFormalVolume": True,
        "offlineMethodologyRebuildAt": datetime.now(core.CN_TZ).isoformat(),
        "dataCapturedAt": captured.isoformat(),
    }
    sources = dict(latest.get("sources") or {})
    sources.update({
        "window": "仅最近3个A股交易日文本；按平台日、公司、板块依次聚合，再以最近50%/上一交易日30%/上两个交易日20%固定合成；缺失席位保持中性且不重新分权",
        "tradingCalendar": f"A股交易日历（{calendar_mode(market_day)}）；周末与休市帖子不计分，非交易日更新归属最近交易日",
        "companyFairness": "正式顺序为平台→公司→板块；三家公司完整时各占1/3，样本数只影响平台内混合与置信度",
        "sectorHeat": "板块主热度V4使用公司等权关注度与情绪温度；达到覆盖、有效样本和留一来源稳定性门槛才给正式名次",
        "sectorActivity": "声量占85%、独立参与广度占15%；点赞和评论累计量不计分，同花顺热榜不参与声量和关注",
        "marketFormula": "固定席位为大盘社区40% + 8个大类社区50% + 热度扩散面10%；板块在大类内等权、大类再等权，缺失席位保持中性",
        "sectorCatalog": f"细分板块目录 {core.SECTOR_CATALOG_ID}（版本{core.SECTOR_CATALOG_VERSION}）：30个细分板块、8个大类，每板块展示3家唯一主归类的典型公司",
        "sectorSourcePlan": f"共配置{len(specs)}个公开社区入口；三家公司各有固定席位，第一家公司的额外新浪/同花顺来源只在公司内部交叉核验",
        "representativeCompanyNote": "典型公司仅用于解释板块边界与定位论坛，不是指数成份股或投资推荐",
    })
    latest.update({
        "version": core.VERSION,
        "sectorCatalogVersion": core.SECTOR_CATALOG_VERSION,
        "sectorCatalogId": core.SECTOR_CATALOG_ID,
        "methodology": "community_text_company_equal_three_trading_day_v2",
        "scoringModel": "platform_day_calibration_then_company_equal_50_30_20_v2",
        "marketDate": date_key,
        # 保留真实数据采集时间，不把本次离线迁移伪装成一次新抓取。
        "updatedAt": captured.isoformat(),
        "confidence": int(history_point["confidence"]),
        "scores": scored["scores"],
        "signals": scored["signals"],
        "heat": scored["signals"]["heat"],
        "direction": scored["signals"]["optimism"],
        "diagnostics": diagnostics,
        "community": {
            "sources": [core.source_public_info(results[key]) for key in market_keys if key in results],
            "samplePosts": community_samples[:12],
            "platforms": [
                {"key": key, "name": core.PLATFORM_NAMES[key], "sourceCount": platform_counts[key]}
                for key in core.PLATFORM_NAMES
            ],
        },
        "sectorIndices": sectors,
        "history": history,
        "historyMeta": history_meta,
        "sources": sources,
    })
    core.save_json(core.HISTORY_PATH, history)
    core.save_json(core.HISTORY_META_PATH, history_meta)
    core.save_json(core.LATEST_PATH, latest)
    core.save_text(
        core.DATA_JS_PATH,
        "window.INDEX_DATA = " + json.dumps(latest, ensure_ascii=False, indent=2) + ";\n",
    )
    print(
        f"离线口径迁移完成：{date_key} 综合{latest['scores']['composite']}，"
        f"热度{latest['heat']}，来源{diagnostics['sourceCount']}。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
