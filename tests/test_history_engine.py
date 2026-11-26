from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from unittest import mock
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import community_archive
import build_history_60d
import collect_history_60d
import history_engine
import update_index
import validate_project
from trading_calendar import is_trading_day


TZ = history_engine.CN_TZ


def post(
    title: str,
    published: datetime,
    *,
    post_id: str = "1",
    source_key: str = "eastmoney:test",
    platform: str = "eastmoney",
    comments: int = 0,
    likes: int = 0,
) -> dict[str, object]:
    return {
        "id": f"{platform}:{post_id}",
        "title": title,
        "published": published,
        "publishTime": published.isoformat(),
        "comments": comments,
        "clicks": 0,
        "likes": likes,
        "sourceKey": source_key,
        "sourceCode": "test",
        "source": "测试来源",
        "platform": platform,
        "platformName": platform,
        "kind": "post",
    }


def catalog_sector(
    key: str,
    definition: dict[str, object],
    scores: dict[str, int],
    signals: dict[str, float] | dict[str, int],
    *,
    sample_posts: int = 30,
) -> dict[str, object]:
    return {
        "key": key,
        "name": definition["name"],
        "parentKey": definition["parentKey"],
        "parentName": definition["parentName"],
        "description": definition["description"],
        "representativeCompanies": definition["representativeCompanies"],
        "catalogVersion": str(update_index.SECTOR_CATALOG_VERSION),
        "available": True,
        "expectedSourceCount": 4,
        "scores": scores,
        "signals": signals,
        "metrics": {"samplePosts": sample_posts, "platformCount": 3, "sourceCount": 4},
    }


class HistoryEngineTests(unittest.TestCase):
    def test_date_as_of_is_end_of_day_and_future_is_hidden(self) -> None:
        cutoff = history_engine.as_of_datetime("2026-03-01")
        self.assertEqual(cutoff.hour, 23)
        self.assertTrue(history_engine.visible_at(datetime(2026, 3, 1, 12, tzinfo=TZ), cutoff))
        self.assertFalse(history_engine.visible_at(datetime(2026, 3, 2, 0, tzinfo=TZ), cutoff))

    def test_weighted_average_never_adds_neutral_residual(self) -> None:
        self.assertEqual(history_engine.normalized_weighted_average([(0.4, 80.0)]), 80.0)
        self.assertIsNone(history_engine.normalized_weighted_average([]))

    def test_history_point_contains_signals_sectors_and_flat_coverage(self) -> None:
        sector = {
            "key": "ai",
            "name": "人工智能",
            "available": True,
            "scores": {"baoma": 70, "novice": 68, "retail": 72, "composite": 70},
            "signals": {
                "optimism": 65, "fomo": 70, "panic": 10,
                "newcomer": 60, "intensity": 66, "attention": 75, "heat": 70,
            },
            "heat": 78.2,
            "legacyHeat": 70.0,
            "absoluteHeat": 72.4,
            "relativeHeat": 81.1,
            "heatRank": 1,
            "heatTier": "头部热点",
            "heatModel": {
                "version": "sector_community_heat_v2",
                "status": "applied",
                "marketAnchor": 68.0,
                "activityIndex": 80.0,
                "formalPersonaScoresChanged": False,
            },
            "metrics": {"samplePosts": 50, "platformCount": 3, "sourceCount": 5},
        }
        signals = {
            "optimism": 63, "fomo": 68, "panic": 12,
            "newcomer": 57, "intensity": 65, "attention": 73,
            "heat": 68, "breadth": 60,
        }
        point = history_engine.build_history_point(
            as_of="2026-03-01",
            scores={"baoma": 66, "novice": 64, "retail": 67, "composite": 66},
            signals=signals,
            sectors=[sector],
            platform_count=3,
            expected_platform_count=3,
            source_count=5,
            expected_source_count=5,
            sector_count=1,
            expected_sector_count=1,
            sample_posts=50,
            fresh_source_count=5,
        )
        self.assertEqual(point["type"], "measured")
        self.assertEqual(point["qualityStatus"], history_engine.QUALITY_MEASURED)
        self.assertFalse(point["isEstimated"])
        self.assertIsInstance(point["coverage"], float)
        self.assertIsInstance(point["confidence"], int)
        self.assertEqual(set(history_engine.SIGNAL_KEYS).issubset(point["signals"]), True)
        self.assertEqual(len(point["sectors"]), 1)
        self.assertEqual(set(history_engine.SIGNAL_KEYS).issubset(point["sectors"][0]["signals"]), True)
        self.assertEqual(point["sectors"][0]["heat"], 78.2)
        self.assertEqual(point["sectors"][0]["relativeHeat"], 81.1)
        self.assertEqual(point["sectors"][0]["legacyHeat"], 70.0)
        self.assertEqual(point["sectors"][0]["heatRank"], 1)
        self.assertEqual(point["sectors"][0]["heatTier"], "头部热点")
        self.assertEqual(point["sectors"][0]["heatModel"]["version"], "sector_community_heat_v2")

        estimated = history_engine.build_history_point(
            as_of="2026-03-01",
            scores=point,
            signals=signals,
            sectors=[sector],
            platform_count=3,
            expected_platform_count=3,
            source_count=5,
            expected_source_count=5,
            sector_count=1,
            expected_sector_count=1,
            sample_posts=50,
            record_type="estimated",
            archive_completeness=1.0,
        )
        self.assertLess(estimated["sectors"][0]["confidence"], point["sectors"][0]["confidence"])
        self.assertEqual(estimated["qualityStatus"], history_engine.QUALITY_PROVISIONAL)
        self.assertIn("recent7PublishedPlatformCount", estimated["qualityDetails"]["failedChecks"])

        usable = history_engine.build_history_point(
            as_of="2026-03-01",
            scores=point,
            signals=signals,
            sectors=[sector],
            platform_count=3,
            expected_platform_count=3,
            source_count=5,
            expected_source_count=5,
            sector_count=1,
            expected_sector_count=1,
            sample_posts=50,
            record_type="estimated",
            archive_completeness=1.0,
            recent_published_platform_count=2,
            recent_published_platforms=["eastmoney", "sina"],
        )
        self.assertEqual(usable["qualityStatus"], history_engine.QUALITY_USABLE)
        self.assertTrue(usable["qualityDetails"]["passed"])

    def test_old_estimated_quality_is_inferred_from_confidence(self) -> None:
        self.assertEqual(
            history_engine.quality_status_of(
                {"type": "estimated", "confidence": 54}
            ),
            history_engine.QUALITY_PROVISIONAL,
        )
        self.assertEqual(
            history_engine.quality_status_of(
                {"type": "estimated", "confidence": 55}
            ),
            history_engine.QUALITY_USABLE,
        )
        self.assertEqual(
            history_engine.quality_status_of(
                {"type": "measured", "confidence": 0}
            ),
            history_engine.QUALITY_MEASURED,
        )

    def test_retrospective_quality_requires_all_three_thresholds(self) -> None:
        status, details = history_engine.retrospective_quality(
            archive_completeness=0.70,
            confidence=55,
            recent_published_platform_count=2,
            recent_published_platforms=["sina", "eastmoney"],
        )
        self.assertEqual(status, history_engine.QUALITY_USABLE)
        self.assertTrue(details["passed"])
        self.assertEqual(details["recent7PublishedPlatforms"], ["eastmoney", "sina"])

        cases = [
            (0.699, 55, 2, "archiveCompleteness"),
            (0.70, 54, 2, "confidence"),
            (0.70, 55, 1, "recent7PublishedPlatformCount"),
        ]
        for archive, confidence, platforms, failed_check in cases:
            with self.subTest(failed_check=failed_check):
                status, details = history_engine.retrospective_quality(
                    archive_completeness=archive,
                    confidence=confidence,
                    recent_published_platform_count=platforms,
                )
                self.assertEqual(status, history_engine.QUALITY_PROVISIONAL)
                self.assertIn(failed_check, details["failedChecks"])

    def test_estimated_never_overwrites_measured_same_date(self) -> None:
        measured = {"date": "2026-03-01", "type": "measured", "composite": 70}
        estimated = {"date": "2026-03-01", "type": "estimated", "composite": 30}
        history = history_engine.upsert_history([measured], estimated)
        self.assertEqual(history[0]["composite"], 70)
        replacement = {"date": "2026-03-01", "type": "measured", "composite": 71}
        history = history_engine.upsert_history([estimated], replacement)
        self.assertEqual(history[0]["composite"], 71)

    def test_history_meta_splits_measured_and_estimated_segments(self) -> None:
        history = [
            {"date": "2026-02-27", "type": "estimated"},
            {"date": "2026-03-02", "type": "estimated"},
            {"date": "2026-03-03", "type": "measured"},
        ]
        meta = history_engine.build_history_meta(history, updated_at="2026-03-03T12:00:00+08:00")
        self.assertEqual(meta["estimatedCount"], 2)
        self.assertEqual(meta["measuredCount"], 1)
        self.assertEqual(len(meta["segments"]), 2)

    def test_history_meta_splits_same_type_on_date_gap(self) -> None:
        history = [
            {"date": "2026-03-02", "type": "estimated"},
            {"date": "2026-03-04", "type": "estimated"},
        ]
        meta = history_engine.build_history_meta(history, updated_at="2026-03-04T12:00:00+08:00")
        self.assertEqual(len(meta["segments"]), 2)
        self.assertEqual(meta["segments"][0]["endDate"], "2026-03-02")
        self.assertEqual(meta["segments"][1]["startDate"], "2026-03-04")

    def test_history_meta_treats_friday_to_monday_as_contiguous(self) -> None:
        history = [
            {"date": "2026-08-07", "type": "estimated"},
            {"date": "2026-08-10", "type": "estimated"},
        ]
        meta = history_engine.build_history_meta(history, updated_at="2026-08-10T18:00:00+08:00")
        self.assertEqual(len(meta["segments"]), 1)
        self.assertEqual(meta["segments"][0]["count"], 2)

    def test_history_meta_breaks_contiguous_same_type_when_methodology_changes(self) -> None:
        history = [
            {
                "date": "2026-03-02",
                "type": "measured",
                "methodology": "community_equal_company_v1",
            },
            {
                "date": "2026-03-03",
                "type": "measured",
                "methodology": "community_equal_company_v2",
            },
        ]

        meta = history_engine.build_history_meta(
            history,
            updated_at="2026-03-03T18:00:00+08:00",
        )

        self.assertEqual(len(meta["segments"]), 2)
        self.assertEqual(
            [segment["methodology"] for segment in meta["segments"]],
            ["community_equal_company_v1", "community_equal_company_v2"],
        )
        self.assertEqual(meta["segments"][0]["endDate"], "2026-03-02")
        self.assertEqual(meta["segments"][1]["startDate"], "2026-03-03")
        self.assertEqual(meta["methodologyBreakCount"], 1)
        self.assertEqual(meta["latestMethodology"], "community_equal_company_v2")


class CollectorRegressionTests(unittest.TestCase):
    def test_daily_eastmoney_end_marker_is_successful_empty_source(self):
        spec = {
            "key": "eastmoney:BKTEST",
            "platform": "eastmoney",
            "code": "BKTEST",
            "name": "测试行业吧",
        }
        with mock.patch.object(
            update_index,
            "request_json",
            return_value={"me": "到底啦", "re": ""},
        ), mock.patch.object(update_index, "request_text") as page_fallback:
            result = update_index.fetch_eastmoney(spec)

        self.assertEqual(result["posts"], [])
        self.assertTrue(result["fresh"])
        page_fallback.assert_not_called()

    def test_pure_date_as_of_means_complete_end_of_day(self) -> None:
        parsed = collect_history_60d.parse_as_of("2026-08-05")
        self.assertEqual(parsed.date(), date(2026, 8, 5))
        self.assertEqual(parsed.time(), time.max)
        explicit = collect_history_60d.parse_as_of("2026-08-05T12:34:56+08:00")
        self.assertEqual(explicit.hour, 12)

    def test_eastmoney_end_marker_is_a_normal_complete_page(self) -> None:
        response = collect_history_60d.NetworkResponse(
            url="https://example.invalid",
            text=json.dumps({"me": "到底啦", "re": None}, ensure_ascii=False),
            requested_at="2026-08-05T12:00:00+08:00",
            elapsed_ms=1,
            http_status=200,
            response_bytes=20,
            response_sha256="hash",
        )
        spec = collect_history_60d.SourceSpec(
            key="eastmoney:test",
            platform="eastmoney",
            code="test",
            name="测试",
            sectors=("ai",),
        )
        parsed = collect_history_60d.parse_eastmoney_page(response, spec, {"next_page": 7})
        self.assertEqual(parsed.rows_returned, 0)
        self.assertTrue(parsed.empty_is_complete)
        self.assertEqual(parsed.next_page, 8)

    def test_schema_repairs_legacy_end_marker_error(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        collect_history_60d.init_schema(conn)
        conn.execute(
            "INSERT INTO sources(source_key,platform,code,name,sectors_json,status,last_error,updated_at) "
            "VALUES(?,?,?,?,?,'error',?,?)",
            ("eastmoney:test", "eastmoney", "test", "测试", "[]", "到底啦", "2026-08-05T00:00:00+08:00"),
        )
        conn.commit()
        collect_history_60d.init_schema(conn)
        row = conn.execute(
            "SELECT status, consecutive_errors, last_error FROM sources WHERE source_key='eastmoney:test'"
        ).fetchone()
        self.assertEqual(row["status"], "complete")
        self.assertEqual(row["consecutive_errors"], 0)
        self.assertIsNone(row["last_error"])
        conn.close()

    def test_legacy_midnight_database_can_resume_with_same_date(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        collect_history_60d.init_schema(conn)
        collect_history_60d.set_meta(conn, "window_as_of", "2026-08-05T00:00:00+08:00")
        collect_history_60d.set_meta(conn, "score_days", 60)
        collect_history_60d.set_meta(conn, "context_days", 30)
        collect_history_60d.set_meta(conn, "collection_strategy", "representative")
        collect_history_60d.set_meta(conn, "sina_target_spacing_days", 2)
        conn.commit()
        args = SimpleNamespace(
            as_of="2026-08-05",
            days=60,
            context_days=30,
            strategy="representative",
            sina_target_spacing=2,
            eastmoney_page_cap=14,
            sina_page_cap=50,
        )
        as_of, _, score_start = collect_history_60d.resolve_window(conn, args)
        self.assertEqual(as_of.time(), time.min)
        self.assertEqual(score_start, date(2026, 6, 7))
        conn.close()

    def test_daily_and_historical_source_layouts_are_explicit(self) -> None:
        specs, _, sector_keys = update_index.build_source_specs()
        self.assertEqual(len(update_index.SECTORS), 30)
        self.assertEqual(len(specs), 311)
        for keys in sector_keys.values():
            counts = Counter(str(specs[key]["platform"]) for key in keys)
            self.assertEqual(counts["eastmoney"], 3)
            self.assertEqual(counts["sina"], 3)
            self.assertEqual(counts["taoguba"], 3)
            self.assertIn(counts["ths"], {1, 2})
        history_two, _ = collect_history_60d.build_sources(2)
        history_one, _ = collect_history_60d.build_sources(1)
        self.assertEqual(len(history_two), 156)
        self.assertEqual(len(history_one), 126)
        self.assertEqual(collect_history_60d.sina_symbol("sh000001"), "sh000001")
        self.assertIn("name=sh000001", collect_history_60d.sina_discovery_url("sh000001"))
        self.assertTrue(
            {"sina:sh000001", "sina:sz399001", "sina:sz399006"}.issubset(
                {source.key for source in history_one if "__market__" in source.sectors}
            )
        )

    def test_partial_collector_day_is_excluded_from_estimates(self) -> None:
        end, partial = build_history_60d.complete_retrospective_end(
            date(2026, 8, 5), {"windowAsOf": "2026-08-05T00:00:00+08:00"}
        )
        self.assertEqual(end, date(2026, 8, 4))
        self.assertEqual(partial, date(2026, 8, 5))
        full_end, full_partial = build_history_60d.complete_retrospective_end(
            date(2026, 8, 6), {"windowAsOf": "2026-08-05T23:59:59+08:00"}
        )
        self.assertEqual(full_end, date(2026, 8, 5))
        self.assertIsNone(full_partial)

    def test_recent_published_platforms_uses_true_seven_trading_day_window(self) -> None:
        cutoff = datetime(2026, 8, 5, 23, 59, tzinfo=TZ)
        results = {
            "eastmoney:test": {
                "platform": "eastmoney",
                "posts": [post("东财窗口内", cutoff - timedelta(days=6), platform="eastmoney")],
            },
            "sina:test": {
                "platform": "sina",
                "posts": [post("新浪当日", cutoff - timedelta(hours=1), platform="sina")],
            },
            "ths:old": {
                "platform": "ths",
                "posts": [
                    post("窗口外旧帖", cutoff - timedelta(days=9), platform="ths"),
                    post("未来帖子", cutoff + timedelta(seconds=1), post_id="future", platform="ths"),
                ],
            },
        }
        self.assertEqual(
            build_history_60d.recent_published_platforms(results, cutoff),
            ["eastmoney", "sina"],
        )

    def test_prepare_history_preserves_history_when_latest_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            history_path = root / "history.json"
            history_path.write_text(
                json.dumps([{"date": "2026-08-05", "type": "measured"}]),
                encoding="utf-8",
            )
            originals = (
                update_index.LATEST_PATH,
                update_index.HISTORY_PATH,
                update_index.V3_HISTORY_PATH,
                update_index.LEGACY_HISTORY_PATH,
            )
            try:
                update_index.LATEST_PATH = root / "missing-latest.json"
                update_index.HISTORY_PATH = history_path
                update_index.V3_HISTORY_PATH = root / "v3.json"
                update_index.LEGACY_HISTORY_PATH = root / "v2.json"
                self.assertEqual(update_index.prepare_history()[0]["date"], "2026-08-05")
            finally:
                (
                    update_index.LATEST_PATH,
                    update_index.HISTORY_PATH,
                    update_index.V3_HISTORY_PATH,
                    update_index.LEGACY_HISTORY_PATH,
                ) = originals

    def test_prepare_history_preserves_temporally_smoothed_v2_sector_heat(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            history_path = root / "history.json"
            latest_path = root / "latest.json"
            history_path.write_text(json.dumps([{
                "date": "2026-08-05",
                "heat": 58.0,
                "sectors": [{
                    "key": "semiconductor",
                    "available": True,
                    "relativeHeat": 64.2,
                    "heat": 66.1,
                    "heatModel": {
                        "version": "sector_community_heat_v2",
                        "temporalSmoothing": "causal_ema_previous_day",
                        "rawRelativeHeat": 72.0,
                    },
                }],
            }]), encoding="utf-8")
            latest_path.write_text(json.dumps({"version": update_index.VERSION}), encoding="utf-8")
            originals = update_index.LATEST_PATH, update_index.HISTORY_PATH
            try:
                update_index.LATEST_PATH = latest_path
                update_index.HISTORY_PATH = history_path
                prepared = update_index.prepare_history()
            finally:
                update_index.LATEST_PATH, update_index.HISTORY_PATH = originals

            sector = prepared[0]["sectors"][0]
            self.assertEqual(sector["relativeHeat"], 64.2)
            self.assertEqual(sector["heat"], 66.1)
            self.assertEqual(sector["heatModel"]["rawRelativeHeat"], 72.0)

    def test_causal_replay_uses_visible_measured_point_in_sandwich_history(self) -> None:
        def heat_sector(relative: float, final: float) -> dict:
            return {
                "key": "semiconductor",
                "available": True,
                "relativeHeat": relative,
                "absoluteHeat": 50.0,
                "heat": final,
                "heatModel": {
                    "marketAnchor": 50.0,
                    "rawRelativeHeat": relative,
                    "rawFinalHeat": final,
                },
            }

        history = [
            {"date": "2026-07-30", "type": "estimated", "heat": 50.0, "sectors": [heat_sector(50.0, 50.0)]},
            {"date": "2026-07-31", "type": "measured", "heat": 50.0, "sectors": [heat_sector(80.0, 80.0)]},
            {"date": "2026-08-03", "type": "estimated", "heat": 50.0, "sectors": [heat_sector(20.0, 20.0)]},
        ]

        replayed = build_history_60d.replay_causal_sector_heat(history)
        measured_sector = replayed[1]["sectors"][0]
        following_sector = replayed[2]["sectors"][0]

        self.assertEqual(measured_sector["relativeHeat"], 70.0)
        self.assertEqual(following_sector["heatModel"]["previousRelativeHeat"], 70.0)
        self.assertEqual(following_sector["relativeHeat"], 50.0)

    def test_history_rebuild_syncs_latest_sector_heat_layer(self) -> None:
        latest = {
            "marketDate": "2026-08-06",
            "diagnostics": {"sectorHeatModel": {"status": "applied"}},
            "sectorIndices": [{
                "key": "semiconductor",
                "heat": 40.0,
                "relativeHeat": 40.0,
                "metrics": {"samplePosts": 100},
                "heatModel": {"version": "sector_community_heat_v2"},
            }],
        }
        history = [{
            "date": "2026-08-06",
            "sectors": [{
                "key": "semiconductor",
                "heat": 66.0,
                "relativeHeat": 68.0,
                "absoluteHeat": 55.0,
                "legacyHeat": 58.0,
                "heatRank": 1,
                "heatTier": "明显升温",
                "heatModel": {
                    "version": "sector_community_heat_v2",
                    "temporalSmoothing": "causal_ema_previous_day",
                    "temporalGapDays": 1,
                },
            }],
        }]

        synced = build_history_60d.sync_latest_sector_heat_from_history(latest, history)

        self.assertTrue(synced)
        self.assertEqual(latest["sectorIndices"][0]["heat"], 66.0)
        self.assertEqual(latest["sectorIndices"][0]["metrics"]["samplePosts"], 100)
        self.assertEqual(latest["diagnostics"]["sectorHeatModel"]["finalRange"], 0.0)

    def test_legacy_measured_latest_point_is_upgraded_without_score_changes(self) -> None:
        scores = {"baoma": 58, "novice": 57, "retail": 59, "composite": 58}
        signals = {
            "optimism": 52.3,
            "fomo": 55.0,
            "panic": 44.0,
            "newcomer": 51.0,
            "intensity": 56.0,
            "attention": 57.0,
            "heat": 56.9,
        }
        sectors = []
        for key, definition in update_index.SECTORS.items():
            sectors.append({
                "key": key,
                "name": definition["name"],
                "available": True,
                "scores": scores,
                "signals": signals,
                "metrics": {"samplePosts": 20, "platformCount": 3, "sourceCount": 5},
            })
        latest = {
            "marketDate": "2026-08-05",
            "updatedAt": "2026-08-05T20:00:00+08:00",
            "scores": scores,
            "signals": signals,
            "sectorIndices": sectors,
            "diagnostics": {
                "platformCount": 3,
                "sourceCount": 80,
                "freshSourceCount": 80,
                "availableSectorCount": len(update_index.SECTORS),
                "allSamplePosts": 4000,
            },
            "community": {"platforms": [
                {"key": "eastmoney", "sourceCount": 47},
                {"key": "sina", "sourceCount": 16},
                {"key": "ths", "sourceCount": 17},
            ]},
            "failures": [],
        }
        legacy = [{"date": "2026-08-05", **scores, "heat": 56.9, "direction": 52.3}]
        upgraded, dates = build_history_60d.upgrade_latest_measured(legacy, latest)
        self.assertEqual(dates, ["2026-08-05"])
        self.assertEqual(upgraded[0]["schemaVersion"], history_engine.HISTORY_SCHEMA_VERSION)
        self.assertEqual(upgraded[0]["type"], "measured")
        self.assertEqual({key: upgraded[0][key] for key in scores}, scores)
        self.assertEqual(len(upgraded[0]["sectors"]), len(update_index.SECTORS))


class AsOfScoringTests(unittest.TestCase):
    def test_sentiment_caps_phrases_and_handles_negation_skepticism(self) -> None:
        self.assertEqual(update_index.sentiment_score("涨停涨停涨停"), 3)
        self.assertLessEqual(update_index.sentiment_score("不会上涨"), 0)
        self.assertLess(update_index.sentiment_score("龙头就是个名，天天跌"), 0)
        self.assertLessEqual(update_index.sentiment_score("铜价新高，库存也新高，看不懂"), 2)
        self.assertFalse(update_index.contains_nonnegated("龙头就是个名", update_index.FOMO_WORDS))
        self.assertFalse(update_index.contains_nonnegated("不会暴跌", update_index.PANIC_WORDS))
        self.assertTrue(update_index.newcomer_flag("现在能买吗？"))

    def test_long_marketing_copy_is_filtered_but_normal_long_discussion_survives(self) -> None:
        marketing = "下午好我是花姐，兄弟们今日上车，盘中操作会持续分享，大家记得点赞关注我，评论区一起挑战翻倍。"
        normal = "这个板块最近讨论很多，我更关心订单兑现、库存变化和盈利质量，短期上涨不代表基本面已经完全反转，大家还是要注意风险。"
        self.assertFalse(update_index.valid_text(marketing))
        self.assertTrue(update_index.valid_text(normal))

    def test_source_analysis_excludes_future_text(self) -> None:
        as_of = datetime(2026, 3, 2, 18, tzinfo=TZ)
        posts = [
            post("坚定看多马上上涨", as_of - timedelta(hours=2), post_id="past"),
            post("清仓暴跌太可怕", as_of + timedelta(minutes=1), post_id="future"),
        ]
        result = update_index.source_analysis(posts, as_of)
        self.assertIsNotNone(result)
        self.assertEqual(result["sampleCount"], 1)
        self.assertGreater(result["signals"]["optimism"], 50)

    def test_retrospective_ignores_current_interactions(self) -> None:
        as_of = datetime(2026, 3, 2, 18, tzinfo=TZ)
        quiet = [post("坚定看多马上上涨", as_of - timedelta(hours=2), comments=0, likes=0)]
        viral = [post("坚定看多马上上涨", as_of - timedelta(hours=2), comments=100000, likes=100000)]
        quiet_result = update_index.source_analysis(quiet, as_of, "estimated")
        viral_result = update_index.source_analysis(viral, as_of, "estimated")
        self.assertEqual(quiet_result["signals"], viral_result["signals"])
        measured = update_index.source_analysis(viral, as_of, "measured")
        self.assertEqual(measured["signals"]["attention"], viral_result["signals"]["attention"])
        self.assertFalse(measured["attentionBaseline"]["interactionUsedForScoring"])

    def test_single_available_platform_is_not_blended_with_default_50(self) -> None:
        as_of = datetime(2026, 3, 2, 18, tzinfo=TZ)
        posts = [post("坚定看多马上上涨", as_of - timedelta(hours=2))]
        source = update_index.source_analysis(posts, as_of)
        grouped = update_index.analyze_group(posts, as_of=as_of)
        self.assertTrue(grouped["available"])
        self.assertAlmostEqual(grouped["signals"]["optimism"], source["signals"]["optimism"], places=1)

    def test_author_is_limited_to_three_posts_per_day(self) -> None:
        as_of = datetime(2026, 3, 2, 18, tzinfo=TZ)
        posts = []
        for index in range(6):
            item = post("坚定看多马上上涨", as_of - timedelta(minutes=index), post_id=str(index))
            item["authorKey"] = "same-author"
            posts.append(item)
        result = update_index.source_analysis(posts, as_of)
        self.assertEqual(result["sampleCount"], 3)

    def test_exact_text_is_deduplicated_across_platforms(self) -> None:
        as_of = datetime(2026, 3, 2, 18, tzinfo=TZ)
        east = post("同一篇宣传内容", as_of, post_id="e", source_key="eastmoney:e", platform="eastmoney")
        sina = post("同一篇宣传内容", as_of, post_id="s", source_key="sina:s", platform="sina")
        results = {
            "eastmoney:e": {"posts": [east]},
            "sina:s": {"posts": [sina]},
        }
        merged = update_index.merge_posts(results, ["eastmoney:e", "sina:s"])
        self.assertEqual(len(merged), 1)

    def test_market_scoring_skips_unavailable_sectors(self) -> None:
        as_of = datetime(2026, 3, 2, 18, tzinfo=TZ)
        _, market_keys, sector_keys = update_index.build_source_specs()
        first_sector = next(iter(update_index.SECTORS))
        market_key = market_keys[0]
        sector_key = sector_keys[first_sector][0]
        results = {
            market_key: {
                "key": market_key, "platform": "eastmoney", "name": "市场", "posts": [
                    post("坚定看多马上上涨", as_of - timedelta(hours=1), post_id="m", source_key=market_key)
                ], "fresh": True,
            },
            sector_key: {
                "key": sector_key, "platform": "eastmoney", "name": "板块", "posts": [
                    post("坚定看多马上上涨", as_of - timedelta(hours=1), post_id="s", source_key=sector_key)
                ], "fresh": True,
            },
        }
        scored = update_index.score_results_as_of(results, market_keys, sector_keys, as_of=as_of)
        self.assertEqual(scored["availableSectorCount"], 1)
        self.assertEqual(scored["availablePlatformCount"], 1)
        # One source occupies only one of twelve fixed company/channel seats;
        # its tiny evidence is allowed to round to neutral, never bearish.
        self.assertGreaterEqual(scored["signals"]["optimism"], 50)


class ArchiveTests(unittest.TestCase):
    def test_archive_is_incremental_deduplicated_and_loadable(self) -> None:
        collected = datetime(2026, 8, 5, 18, tzinfo=TZ)
        published = datetime(2026, 8, 5, 10, tzinfo=TZ)
        item = post("坚定看多马上上涨", published, comments=3, likes=2)
        results = {
            "eastmoney:test": {
                "key": "eastmoney:test", "code": "test", "name": "测试来源",
                "platform": "eastmoney", "posts": [item],
            },
            "eastmoney:duplicate-source": {
                "key": "eastmoney:duplicate-source", "code": "dup", "name": "重复来源",
                "platform": "eastmoney", "posts": [{**item, "sourceKey": "eastmoney:duplicate-source"}],
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "2025-01-01.json").write_text("[]", encoding="utf-8")
            first = community_archive.archive_results(results, root, collected_at=collected)
            self.assertEqual(first["added"], 1)
            self.assertFalse((root / "2025-01-01.json").exists())
            item["comments"] = 999
            second = community_archive.archive_results(results, root, collected_at=collected + timedelta(hours=1))
            self.assertEqual(second["added"], 0)
            saved = json.loads((root / "2026-08-05.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["commentsAtCollection"], 3)
            self.assertEqual(len(saved[0]["sourceKeys"]), 2)
            loaded = community_archive.load_archived_results(
                root,
                start=datetime(2026, 8, 5, 0, tzinfo=TZ),
                end=datetime(2026, 8, 5, 23, 59, tzinfo=TZ),
            )
            self.assertEqual(len(loaded["eastmoney:test"]["posts"]), 1)
            self.assertIsInstance(loaded["eastmoney:test"]["posts"][0]["published"], datetime)

    def test_manifest_v2_source_coverage_round_trips(self) -> None:
        collected = datetime(2026, 8, 6, 18, tzinfo=TZ)
        published = datetime(2026, 8, 6, 10, tzinfo=TZ)
        results = {
            "eastmoney:test": {
                "key": "eastmoney:test",
                "code": "test",
                "name": "测试来源",
                "platform": "eastmoney",
                "posts": [post("测试社区帖子", published)],
                "rawRows": 137,
                "pageCount": 2,
                "oldestSeen": "2026-08-04T09:31:00+08:00",
                "newestSeen": "2026-08-06T17:59:00+08:00",
                "capHit": False,
                "chronological": True,
                "coverageDays": ["2026-08-06", "2026-08-05", "2026-08-04"],
                "thirdTradingDayReached": True,
                "freshFetchedNow": True,
                "coverageStatus": "third_trading_day_reached",
                "paginationMode": "page",
            },
            "ths:empty": {
                "key": "ths:empty",
                "code": "empty",
                "name": "同花顺空样本",
                "platform": "ths",
                "posts": [],
                "rawRows": 8,
                "pageCount": 1,
                "oldestSeen": "2026-08-05T13:00:00+08:00",
                "newestSeen": "2026-08-06T16:00:00+08:00",
                "capHit": True,
                "chronological": False,
                "coverageDays": ["2026-08-06", "2026-08-05"],
                "thirdTradingDayReached": False,
                "freshFetchedNow": False,
                "coverageStatus": "non_chronological_hot_feed",
                "paginationMode": "ranked_hot_feed",
            },
        }

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            community_archive.archive_results(results, root, collected_at=collected)

            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schemaVersion"], 2)
            run = manifest["collectionRuns"]["2026-08-06"]
            coverage_by_key = {
                row["key"]: row for row in run["sourceCoverage"]
            }
            self.assertEqual(set(coverage_by_key), set(results))
            self.assertEqual(coverage_by_key["eastmoney:test"]["rawRows"], 137)
            self.assertEqual(coverage_by_key["eastmoney:test"]["coverageDays"], results["eastmoney:test"]["coverageDays"])
            self.assertIs(coverage_by_key["eastmoney:test"]["chronological"], True)
            self.assertIs(coverage_by_key["ths:empty"]["chronological"], False)
            self.assertIs(coverage_by_key["ths:empty"]["capHit"], True)

            loaded = community_archive.load_archived_results(
                root,
                start=datetime(2026, 8, 6, 0, tzinfo=TZ),
                end=datetime(2026, 8, 6, 23, 59, tzinfo=TZ),
                observed_at=collected,
            )

        for source_key, expected in results.items():
            self.assertIn(source_key, loaded)
            restored = loaded[source_key]
            for field in (
                "rawRows", "pageCount", "oldestSeen", "newestSeen", "capHit",
                "chronological", "coverageDays", "thirdTradingDayReached",
                "freshFetchedNow", "coverageStatus", "paginationMode",
            ):
                self.assertEqual(restored[field], expected[field], (source_key, field))
            self.assertEqual(restored["fetchedAt"], collected.isoformat())
            self.assertIs(restored["rawRowsEstimated"], False)
        self.assertEqual(loaded["ths:empty"]["posts"], [])


class SqliteBackfillTests(unittest.TestCase):
    def test_sqlite_posts_are_scored_as_estimated_points(self) -> None:
        as_of = datetime(2026, 3, 2, 18, tzinfo=TZ)
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "history.sqlite3"
            conn = sqlite3.connect(str(db_path))
            collect_history_60d.init_schema(conn)
            specs, configured_market, configured_sectors = update_index.build_source_specs()
            market_key = next(key for key in configured_market if specs[key]["platform"] == "eastmoney")
            first_sector = next(iter(update_index.SECTORS))
            sector_key = next(
                key for key in configured_sectors[first_sector]
                if specs[key]["platform"] == "eastmoney"
            )
            sources = [
                (market_key, "eastmoney", specs[market_key]["code"], specs[market_key]["name"], '["legacy_market"]'),
                (sector_key, "eastmoney", specs[sector_key]["code"], specs[sector_key]["name"], '["legacy_sector"]'),
            ]
            conn.executemany(
                "INSERT INTO sources(source_key,platform,code,name,sectors_json,status,updated_at) "
                "VALUES(?,?,?,?,?,'complete',?)",
                [(*row, as_of.isoformat()) for row in sources],
            )
            for source_key, _, code, name, _ in sources:
                conn.execute(
                    "INSERT INTO posts(platform,source_key,post_id,source_code,source_name,published_at,"
                    "observed_at,title,author_hash,comments,clicks,likes,kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "eastmoney", source_key, code, code, name,
                        (as_of - timedelta(hours=1)).isoformat(), as_of.isoformat(),
                        "坚定看多马上上涨", "author", 999999, 999999, 999999, "post",
                    ),
                )
            conn.commit()
            conn.close()
            results, market, sectors, _ = build_history_60d.load_results_from_db(db_path)
            points, skipped = build_history_60d.score_history_points(
                results,
                market,
                sectors,
                start=as_of.date(),
                end=as_of.date(),
                expected_source_count=2,
                completeness=1.0,
                captured_at=as_of,
            )
            self.assertFalse(skipped)
            self.assertEqual(points[0]["type"], "estimated")
            self.assertTrue(points[0]["isEstimated"])
            self.assertEqual(points[0]["qualityStatus"], history_engine.QUALITY_PROVISIONAL)
            self.assertEqual(
                points[0]["qualityDetails"]["recent7PublishedPlatformCount"], 1
            )
            self.assertGreaterEqual(points[0]["direction"], 50)


class ValidatorTests(unittest.TestCase):
    def test_validator_recomputes_explicit_quality_status(self) -> None:
        scores = {"baoma": 60, "novice": 60, "retail": 60, "composite": 60}
        signals = {
            "optimism": 55, "fomo": 55, "panic": 40,
            "newcomer": 50, "intensity": 55, "attention": 60, "heat": 58,
        }
        point = history_engine.build_history_point(
            as_of="2026-08-04",
            scores=scores,
            signals=signals,
            sectors=[],
            platform_count=3,
            expected_platform_count=3,
            source_count=3,
            expected_source_count=3,
            sector_count=0,
            expected_sector_count=1,
            sample_posts=100,
            record_type="estimated",
            archive_completeness=1.0,
            recent_published_platform_count=2,
            recent_published_platforms=["eastmoney", "sina"],
        )
        errors: list[str] = []
        validate_project.check_quality(point, "point", errors)
        self.assertEqual(errors, [])
        point["qualityStatus"] = history_engine.QUALITY_PROVISIONAL
        validate_project.check_quality(point, "point", errors)
        self.assertTrue(any("三道质量门槛" in error for error in errors))

    def test_validator_accepts_modern_payload_and_rejects_missing_panic(self) -> None:
        scores = {"baoma": 61, "novice": 60, "retail": 62, "composite": 61}
        signals = {
            "optimism": 58.0,
            "fomo": 54.0,
            "panic": 37.0,
            "newcomer": 49.0,
            "intensity": 55.0,
            "attention": 60.0,
            "heat": 57.0,
        }
        sectors = [
            catalog_sector(key, definition, scores, signals)
            for key, definition in update_index.SECTORS.items()
        ]
        point = history_engine.build_history_point(
            as_of="2026-08-05",
            scores=scores,
            signals=signals,
            sectors=sectors,
            platform_count=3,
            expected_platform_count=3,
            source_count=100,
            expected_source_count=100,
            sector_count=len(update_index.SECTORS),
            expected_sector_count=len(update_index.SECTORS),
            sample_posts=480,
            fresh_source_count=100,
        )
        history = [point]
        meta = history_engine.build_history_meta(
            history, updated_at="2026-08-05T20:00:00+08:00"
        )
        latest = {
            "sectorCatalogVersion": update_index.SECTOR_CATALOG_VERSION,
            "scores": scores,
            "signals": signals,
            "sectorIndices": sectors,
            "history": history,
            "historyMeta": meta,
        }

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = root / "data"
            data_dir.mkdir()
            originals = (validate_project.ROOT, validate_project.DATA_DIR)
            try:
                validate_project.ROOT = root
                validate_project.DATA_DIR = data_dir

                def write_payload() -> None:
                    (data_dir / "latest.json").write_text(
                        json.dumps(latest, ensure_ascii=False), encoding="utf-8"
                    )
                    (data_dir / "history.json").write_text(
                        json.dumps(history, ensure_ascii=False), encoding="utf-8"
                    )
                    (data_dir / "history_meta.json").write_text(
                        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
                    )
                    (root / "data.js").write_text(
                        "window.INDEX_DATA = " + json.dumps(latest, ensure_ascii=False) + ";\n",
                        encoding="utf-8",
                    )
                    for filename in ("dashboard.html", "dashboard.js", "styles.css", "一键更新指数.cmd"):
                        (root / filename).write_text("placeholder", encoding="utf-8")

                write_payload()
                self.assertEqual(validate_project.validate(1, "2026-08-05"), [])

                del history[0]["signals"]["panic"]
                latest["history"] = history
                write_payload()
                errors = validate_project.validate(1, None)
                self.assertTrue(any("signals.panic" in error for error in errors))
            finally:
                validate_project.ROOT, validate_project.DATA_DIR = originals


class FullBuildIntegrationTests(unittest.TestCase):
    def test_partial_archive_builds_trading_day_estimates_plus_protected_measured_day(self) -> None:
        signals = {
            "optimism": 58.0,
            "fomo": 54.0,
            "panic": 37.0,
            "newcomer": 49.0,
            "intensity": 55.0,
            "attention": 60.0,
            "heat": 57.0,
        }
        scores = {"baoma": 61, "novice": 60, "retail": 62, "composite": 61}
        latest_sectors = [
            catalog_sector(key, definition, scores, signals, sample_posts=20)
            for key, definition in update_index.SECTORS.items()
        ]
        latest = {
            "version": update_index.VERSION,
            "sectorCatalogVersion": update_index.SECTOR_CATALOG_VERSION,
            "methodology": "community_text_multiplatform_v2",
            "marketDate": "2026-08-05",
            "updatedAt": "2026-08-05T20:00:00+08:00",
            "confidence": 95,
            "scores": scores,
            "signals": signals,
            "sectorIndices": latest_sectors,
            "diagnostics": {
                "platformCount": 3,
                "sourceCount": 100,
                "freshSourceCount": 100,
                "availableSectorCount": len(update_index.SECTORS),
                "allSamplePosts": 320,
            },
            "community": {"platforms": [
                {"key": "eastmoney", "sourceCount": 47},
                {"key": "sina", "sourceCount": 36},
                {"key": "ths", "sourceCount": 17},
            ]},
            "history": [{"date": "2026-08-05", **scores, "heat": 57.0, "direction": 58.0}],
            "sources": {},
            "failures": [],
        }

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = root / "data"
            data_dir.mkdir()
            db_path = data_dir / "history.sqlite3"
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            collect_history_60d.init_schema(conn)
            meta = {
                "window_as_of": "2026-08-05T00:00:00+08:00",
                "score_start": "2026-06-07",
                "raw_cutoff": "2026-05-08T00:00:00+08:00",
                "collection_strategy": "representative",
                "sina_target_spacing_days": "2",
                "eastmoney_source_request_cap": "14",
                "sina_source_request_cap": "50",
            }
            for key, value in meta.items():
                collect_history_60d.set_meta(conn, key, value)
            specs, configured_market, configured_sectors = update_index.build_source_specs()
            market_key = next(key for key in configured_market if specs[key]["platform"] == "eastmoney")
            first_sector = next(iter(update_index.SECTORS))
            sector_key = next(
                key for key in configured_sectors[first_sector]
                if specs[key]["platform"] == "eastmoney"
            )
            source_rows = [
                (market_key, specs[market_key]["code"], specs[market_key]["name"], '["legacy_market"]'),
                (sector_key, specs[sector_key]["code"], specs[sector_key]["name"], '["legacy_sector"]'),
            ]
            conn.executemany(
                "INSERT INTO sources(source_key,platform,code,name,sectors_json,status,updated_at) "
                "VALUES(?,'eastmoney',?,?,?,'complete','2026-08-05T00:00:00+08:00')",
                source_rows,
            )
            current = date(2026, 6, 7)
            while current <= date(2026, 8, 4):
                published = datetime.combine(current, time(10), tzinfo=TZ)
                for source_key, code, name, _ in source_rows:
                    conn.execute(
                        "INSERT INTO posts(platform,source_key,post_id,source_code,source_name,published_at,"
                        "observed_at,title,author_hash,comments,clicks,likes,kind) "
                        "VALUES('eastmoney',?,?,?,?,?,?,?,?,0,0,0,'post')",
                        (
                            source_key,
                            f"{code}-{current.isoformat()}",
                            code,
                            name,
                            published.isoformat(),
                            "2026-08-05T00:00:00+08:00",
                            f"坚定看多市场上涨 {current.isoformat()}",
                            f"author-{source_key}-{current.isoformat()}",
                        ),
                    )
                current += timedelta(days=1)
            conn.commit()
            conn.close()

            history_path = data_dir / "history.json"
            history_meta_path = data_dir / "history_meta.json"
            latest_path = data_dir / "latest.json"
            data_js_path = root / "data.js"
            history_path.write_text(
                json.dumps(latest["history"], ensure_ascii=False), encoding="utf-8"
            )
            latest_path.write_text(json.dumps(latest, ensure_ascii=False), encoding="utf-8")

            core_originals = (
                update_index.HISTORY_PATH,
                update_index.HISTORY_META_PATH,
                update_index.LATEST_PATH,
                update_index.DATA_JS_PATH,
            )
            validator_originals = (validate_project.ROOT, validate_project.DATA_DIR)
            try:
                update_index.HISTORY_PATH = history_path
                update_index.HISTORY_META_PATH = history_meta_path
                update_index.LATEST_PATH = latest_path
                update_index.DATA_JS_PATH = data_js_path
                args = SimpleNamespace(
                    db=str(db_path),
                    start=None,
                    end=None,
                    build_report=str(data_dir / "build_report.json"),
                    dry_run=False,
                    no_backup=True,
                )
                self.assertEqual(build_history_60d.build(args), 0)

                history = json.loads(history_path.read_text(encoding="utf-8"))
                expected_estimated = sum(
                    is_trading_day(date(2026, 6, 7) + timedelta(days=offset))
                    for offset in range((date(2026, 8, 4) - date(2026, 6, 7)).days + 1)
                )
                expected_total = expected_estimated + 1
                self.assertEqual(len(history), expected_total)
                self.assertEqual(sum(item["type"] == "estimated" for item in history), expected_estimated)
                self.assertEqual(
                    sum(item["qualityStatus"] == history_engine.QUALITY_PROVISIONAL for item in history),
                    expected_estimated,
                )
                self.assertEqual(history[-1]["type"], "measured")
                self.assertEqual(history[-1]["composite"], 61)
                history_meta = json.loads(history_meta_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    history_meta["retrospectiveArchive"]["partialCollectorDateExcluded"],
                    "2026-08-05",
                )
                self.assertEqual(history_meta["usableEstimatedCount"], 0)
                self.assertEqual(history_meta["provisionalEstimatedCount"], expected_estimated)

                validate_project.ROOT = root
                validate_project.DATA_DIR = data_dir
                for filename in ("dashboard.html", "dashboard.js", "styles.css", "一键更新指数.cmd"):
                    (root / filename).write_text("placeholder", encoding="utf-8")
                self.assertEqual(
                    validate_project.validate(expected_total, "2026-06-08"),
                    [],
                )
            finally:
                (
                    update_index.HISTORY_PATH,
                    update_index.HISTORY_META_PATH,
                    update_index.LATEST_PATH,
                    update_index.DATA_JS_PATH,
                ) = core_originals
                validate_project.ROOT, validate_project.DATA_DIR = validator_originals


if __name__ == "__main__":
    unittest.main()
