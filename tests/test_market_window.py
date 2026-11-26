from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
import urllib.parse

import community_archive as archive
import update_index as core

AT = datetime(2026, 11, 26, 16, tzinfo=core.CN_TZ)
KEY = "sina:sh000001"
SPEC = {"key": KEY, "code": "sh000001", "platform": "sina", "name": "上证指数", "marketWindow": True}


def post(identity, day=26, author=None, **extra):
    published = AT.replace(day=day, hour=10)
    return core.make_post(
        post_id=identity, text="普通用户讨论看好后市" + identity, published=published,
        comments=0, clicks=0, likes=0, source_key=KEY, source_code="sh000001",
        source_name="上证指数", platform="sina", author_key=author or identity,
    ) | extra


def sina_row(day):
    return {"tid": str(day), "title": "普通用户讨论今天怎么办" + str(day),
            "timestamp": AT.replace(day=day, hour=10).isoformat(),
            "user": {"uid": str(day), "nick": "普通用户", "verified": False}}


class MarketPaginationTests(unittest.TestCase):
    def test_market_sina_reads_past_third_day_start(self):
        pages = [{"data": {"threads": [sina_row(day)]}} for day in (26, 25, 24, 23)]
        with patch.object(core, "NOW", AT), patch.object(core, "discover_sina_bid", return_value="9279"), patch.object(core, "request_json", side_effect=pages) as req, patch.object(core.time, "sleep"):
            result = core.fetch_sina(SPEC)
        self.assertEqual(req.call_count, 4)
        self.assertTrue(result["thirdTradingDayReached"])
        self.assertTrue(result["windowStartCovered"])
        self.assertEqual(result["coverageStatus"], "window_start_covered")

    def test_third_day_partial_is_not_complete(self):
        pages = [{"data": {"threads": [sina_row(day)]}} for day in (26, 25, 24)]
        with patch.object(core, "NOW", AT), patch.object(core, "MARKET_PAGE_LIMIT", 3), patch.object(core, "discover_sina_bid", return_value="9279"), patch.object(core, "request_json", side_effect=pages), patch.object(core.time, "sleep"):
            result = core.fetch_sina(SPEC)
        self.assertTrue(result["thirdTradingDayReached"])
        self.assertFalse(result["windowStartCovered"])
        self.assertEqual(result["coverageStatus"], "page_cap_reached_before_third_trading_day")

    def test_repeated_market_page_stops_without_completeness(self):
        payload = {"data": {"threads": [sina_row(26)]}}
        with patch.object(core, "NOW", AT), patch.object(core, "discover_sina_bid", return_value="9279"), patch.object(core, "request_json", return_value=payload) as req, patch.object(core.time, "sleep"):
            result = core.fetch_sina(SPEC)
        self.assertEqual(req.call_count, 2)
        self.assertEqual(result["coverageStatus"], "repeated_page")
        self.assertFalse(result["windowStartCovered"])

    def test_partial_network_failure_keeps_market_posts(self):
        with patch.object(core, "NOW", AT), patch.object(core, "discover_sina_bid", return_value="9279"), patch.object(core, "request_json", side_effect=[{"data": {"threads": [sina_row(26)]}}, RuntimeError("offline")]), patch.object(core.time, "sleep"):
            result = core.fetch_sina(SPEC)
        self.assertEqual(len(result["posts"]), 1)
        self.assertEqual(result["coverageStatus"], "partial_fetch_failure")

    def test_time_budget_keeps_partial_rows(self):
        clock = [0.0]
        def request(*args, **kwargs):
            clock[0] = 50.0
            return {"data": {"threads": [sina_row(26)]}}
        with patch.object(core, "NOW", AT), patch.object(core, "discover_sina_bid", return_value="9279"), patch.object(core, "request_json", side_effect=request) as req, patch.object(core.time, "monotonic", side_effect=lambda: clock[0]), patch.object(core.time, "sleep"):
            result = core.fetch_sina(SPEC)
        self.assertEqual(req.call_count, 1)
        self.assertEqual(result["coverageStatus"], "time_budget_exhausted")
        self.assertEqual(len(result["posts"]), 1)

    def test_eastmoney_market_reads_short_pages_through_boundary(self):
        requested = []
        def request(url, *args, **kwargs):
            number = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["p"][0])
            requested.append(number)
            return {"me": "操作成功", "count": 10000, "re": [{
                "post_id": str(number), "post_type": 0, "post_source_id": "",
                "post_title": "普通用户讨论后市" + str(number), "user_id": str(number),
                "user_nickname": "普通用户", "post_publish_time": AT.replace(day=27-number, hour=10).isoformat(),
            }]}
        spec = {"key": "eastmoney:zssh000001", "code": "zssh000001", "name": "上证指数吧", "marketWindow": True}
        with patch.object(core, "NOW", AT), patch.object(core, "request_json", side_effect=request), patch.object(core.time, "sleep"):
            result = core.fetch_eastmoney(spec)
        self.assertEqual(requested, [1, 2, 3, 4])
        self.assertTrue(result["windowStartCovered"])

    def test_old_pinned_timestamp_cannot_prove_zero(self):
        source = {"platform": "sina", "chronological": True, "rawRowsEstimated": False,
                  "marketWindowPolicyVersion": 1, "thirdTradingDayReached": True,
                  "oldestSeen": AT.replace(day=1).isoformat(), "fetchedAt": AT.isoformat(),
                  "chronologicalBoundary": AT.replace(hour=10).isoformat()}
        self.assertEqual(core.verified_zero_days_for_source(source, core.scoring_trading_days(AT), AT), set())
        self.assertEqual(core.page_boundary([AT.replace(day=1)] + [AT] * 9, 10), AT)


    def test_observed_day_is_not_labelled_verified_zero(self):
        source = {**SPEC, "chronological": True, "fetchedAt": AT.isoformat(),
                  "marketWindowPolicyVersion": 1, "windowStartCovered": True,
                  "chronologicalBoundary": AT.replace(day=23).isoformat(), "rawRowsEstimated": False}
        daily = {KEY: {AT.date().isoformat(): {"status": "observed", "sampleCount": 12}}}
        result = core.market_window_diagnostics({KEY: source}, [KEY], daily, AT)
        today = result["sources"][0]["days"][0]
        self.assertTrue(today["streamCovered"])
        self.assertFalse(today["verifiedZero"])


class WindowArchiveTests(unittest.TestCase):
    def test_repeat_merge_is_idempotent_and_author_limit_reapplied(self):
        live = {KEY: {**SPEC, "posts": [post("new"), post("new")], "fresh": True}}
        saved = {KEY: {**SPEC, "posts": [post("new"), post("old", 25)]}}
        for p in saved[KEY]["posts"]:
            p["firstCollectedAt"] = AT.replace(day=25, hour=15).isoformat()
        first = core.merge_window_evidence(live, saved, {KEY: SPEC}, AT)
        second = core.merge_window_evidence(first, saved, {KEY: SPEC}, AT)
        self.assertEqual([p["id"] for p in first[KEY]["posts"]], [p["id"] for p in second[KEY]["posts"]])
        self.assertEqual(len(first[KEY]["posts"]), 2)
        spam = [post(str(i), 25, author="same") for i in range(8)]
        for p in spam:
            p["firstCollectedAt"] = AT.replace(day=25).isoformat()
        limited = core.merge_window_evidence({}, {KEY: {"posts": spam}}, {KEY: SPEC}, AT)
        self.assertEqual(len(limited[KEY]["posts"]), core.AUTHOR_DAILY_LIMIT)

    def test_archive_rolls_to_previous_day_and_excludes_future_discovery(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            archive.archive_results({KEY: {**SPEC, "posts": [post("yesterday", 25)]}}, path, collected_at=AT.replace(day=25))
            archive.archive_results({KEY: {**SPEC, "posts": [post("later-discovery", 25)]}}, path, collected_at=AT + timedelta(hours=1))
            saved = archive.load_archived_results(path, start=AT.replace(day=24, hour=0), end=AT, observed_at=AT)
            merged = core.merge_window_evidence({KEY: {**SPEC, "posts": [post("today")], "fresh": True}}, saved, {KEY: SPEC}, AT)
            self.assertEqual({p["id"] for p in merged[KEY]["posts"]}, {"sina:today", "sina:yesterday"})
            analysis = core.source_analysis(merged[KEY]["posts"], AT)
            self.assertEqual({p["tradingDay"] for p in analysis["posts"]}, {"2026-11-25", "2026-11-26"})

    def test_archive_keeps_filter_flags_and_source_first_seen(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            marked = post("same", 25, originalLength=900, stockTagCount=8, certified=False, timeInherited=True)
            archive.archive_results({KEY: {**SPEC, "posts": [marked]}}, path, collected_at=AT.replace(day=25))
            other = "sina:sz399001"
            archive.archive_results({other: {**SPEC, "key": other, "posts": [{**marked, "sourceKey": other}]}}, path, collected_at=AT + timedelta(hours=1))
            loaded = archive.load_archived_results(path, start=AT.replace(day=24, hour=0), end=AT, observed_at=AT)
            self.assertIn(KEY, loaded)
            self.assertNotIn(other, loaded)
            self.assertEqual(loaded[KEY]["posts"][0]["originalLength"], 900)
            self.assertTrue(loaded[KEY]["posts"][0]["timeInherited"])

    def test_archive_samples_do_not_claim_stream_completeness(self):
        p = post("old", 25, firstCollectedAt=AT.replace(day=25).isoformat())
        merged = core.merge_window_evidence({}, {KEY: {"posts": [p], "chronological": True, "windowStartCovered": True}}, {KEY: SPEC}, AT)
        self.assertFalse(merged[KEY]["fresh"])
        self.assertFalse(merged[KEY]["chronological"])
        self.assertEqual(core.verified_zero_days_for_source(merged[KEY], core.scoring_trading_days(AT), AT), set())

    def test_legacy_ths_quality_unknown_is_not_promoted(self):
        key = "ths:market:1A0001"
        p = post("legacy", 25, firstCollectedAt=AT.replace(day=25).isoformat())
        spec = {"key": key, "platform": "ths", "code": "1A0001", "name": "上证讨论"}
        merged = core.merge_window_evidence({}, {key: {"posts": [p]}}, {key: spec}, AT)
        self.assertEqual(merged[key]["posts"], [])
        self.assertEqual(merged[key]["archiveQualityUnknownSkipped"], 1)


if __name__ == "__main__":
    unittest.main()
