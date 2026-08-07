from __future__ import annotations

import unittest
from datetime import date, datetime

import data_quality_report as quality
from history_engine import CN_TZ


class DataQualityReportTests(unittest.TestCase):
    def test_describe_and_pearson_are_deterministic(self) -> None:
        summary = quality.describe([0, 10, 20, 30])
        self.assertEqual(summary["count"], 4)
        self.assertEqual(summary["median"], 15.0)
        self.assertEqual(summary["p25"], 7.5)
        self.assertEqual(summary["max"], 30.0)
        self.assertEqual(quality.pearson([1, 2, 3], [2, 4, 6]), 1.0)
        self.assertIsNone(quality.pearson([1], [2]))

    def test_raw_coverage_keeps_zero_days_and_platform_gaps(self) -> None:
        results = {
            "eastmoney:a": {
                "platform": "eastmoney",
                "posts": [
                    {"published": datetime(2026, 6, 1, 9, tzinfo=CN_TZ)},
                    {"published": datetime(2026, 6, 3, 9, tzinfo=CN_TZ)},
                ],
            },
            "sina:a": {
                "platform": "sina",
                "posts": [
                    {"published": datetime(2026, 6, 3, 10, tzinfo=CN_TZ)},
                ],
            },
        }
        report = quality.analyze_raw_coverage(
            results,
            {"agriculture": ["eastmoney:a", "sina:a"]},
            date(2026, 6, 1),
            date(2026, 6, 3),
        )
        self.assertEqual(report["expectedDays"], 3)
        self.assertEqual(report["zeroRawDays"], ["2026-06-02"])
        self.assertEqual(report["singlePlatformRawDays"], 1)
        self.assertEqual(report["multiPlatformRawDays"], 1)
        self.assertEqual(report["platformSummary"]["eastmoney"]["postsInWindow"], 2)
        self.assertEqual(report["platformSummary"]["sina"]["postsInWindow"], 1)

    def test_persona_audit_detects_low_separation(self) -> None:
        points = [
            {"baoma": 50, "novice": 50, "retail": 50},
            {"baoma": 51, "novice": 50, "retail": 50},
            {"baoma": 49, "novice": 50, "retail": 50},
        ]
        report = quality.analyze_persona_differentiation(points)
        self.assertEqual(report["exactlyEqualDays"], 1)
        self.assertEqual(report["rangeAtMostOneDays"], 3)
        self.assertEqual(report["dailyRange"]["max"], 1.0)
        self.assertEqual(report["uniqueTriples"], 3)

    def test_iqr_outlier_marks_extreme_day(self) -> None:
        points = [
            {"date": f"2026-06-{day:02d}", "composite": value}
            for day, value in enumerate([49, 50, 50, 50, 51, 80], start=1)
        ]
        self.assertEqual(
            quality.outlier_dates(points, "composite"),
            [{"date": "2026-06-06", "value": 80, "fence": "high"}],
        )


if __name__ == "__main__":
    unittest.main()
