from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import scoring_experiments as experiments
from history_engine import CN_TZ


def sample_post(
    post_id: str,
    title: str,
    *,
    day: int = 1,
    author: str = "author",
) -> dict[str, object]:
    published = datetime(2026, 6, day, 10, tzinfo=CN_TZ)
    return {
        "id": post_id,
        "title": title,
        "published": published,
        "publishTime": published.isoformat(),
        "authorKey": author,
        "sourceKey": "eastmoney:test",
    }


class ScoringExperimentTests(unittest.TestCase):
    def test_stable_cap_is_deterministic_and_respects_author_limit(self) -> None:
        posts = [
            sample_post(str(index), f"帖子{index}", author="same" if index < 6 else f"a{index}")
            for index in range(12)
        ]
        results = {
            "eastmoney:test": {"platform": "eastmoney", "posts": posts},
        }
        first, first_meta = experiments.stable_source_day_cap(results, cap=5)
        second, second_meta = experiments.stable_source_day_cap(results, cap=5)
        first_ids = [post["id"] for post in first["eastmoney:test"]["posts"]]
        second_ids = [post["id"] for post in second["eastmoney:test"]["posts"]]
        self.assertEqual(first_ids, second_ids)
        self.assertLessEqual(len(first_ids), 5)
        selected_same_author = sum(
            post["authorKey"] == "same" for post in first["eastmoney:test"]["posts"]
        )
        self.assertLessEqual(selected_same_author, 3)
        self.assertEqual(first_meta, second_meta)

    def test_persona_classifier_uses_evidence_and_exclusive_priority(self) -> None:
        self.assertEqual(
            experiments.classify_persona("孩子的学费是闲钱，准备长期持有"),
            ("baoma", "explicit_household"),
        )
        self.assertEqual(
            experiments.classify_persona("新手请问这个位置能买吗？"),
            ("novice", "explicit_question_or_learning"),
        )
        self.assertEqual(
            experiments.classify_persona("突破压力位后加仓，跌破均线止损"),
            ("retail", "active_trading_language"),
        )
        self.assertEqual(experiments.classify_persona("公司发布年度报告"), (None, None))

    def test_evidence_weight_only_shrinks_toward_market(self) -> None:
        point = {
            "samplePosts": 120, "sourceCount": 8,
            "platformCount": 2, "confidence": 80,
        }
        weight = experiments.evidence_weight(point)
        self.assertGreater(weight, 0.0)
        self.assertLessEqual(weight, 0.65)
        market = 50.0
        cohort = 80.0
        final = market + weight * (cohort - market)
        self.assertGreaterEqual(final, market)
        self.assertLessEqual(final, cohort)

    def test_evidence_weight_is_capped_by_confidence(self) -> None:
        point = {
            "samplePosts": 1000, "sourceCount": 50,
            "platformCount": 3, "confidence": 27,
        }
        self.assertEqual(experiments.evidence_weight(point), 0.27)

    def test_masking_removes_classifier_terms_without_touching_original(self) -> None:
        original = sample_post("1", "新手请问突破后能买吗？公司业绩持续增长")
        results = {"eastmoney:test": {"platform": "eastmoney", "posts": [original]}}
        masked, stats = experiments.mask_persona_evidence_for_scoring(results, "novice")
        posts = masked["eastmoney:test"]["posts"]
        self.assertEqual(stats["maskedPosts"], 1)
        self.assertEqual(original["title"], "新手请问突破后能买吗？公司业绩持续增长")
        self.assertEqual(posts[0]["evidenceTitle"], original["title"])
        self.assertNotEqual(posts[0]["title"], original["title"])

    def test_revision_stability_reports_absolute_changes(self) -> None:
        earlier = [
            {"date": "2026-06-01", "composite": 50},
            {"date": "2026-06-02", "composite": 52},
        ]
        later = [
            {"date": "2026-06-01", "composite": 51},
            {"date": "2026-06-02", "composite": 49},
        ]
        report = experiments.revision_stability(earlier, later)
        self.assertEqual(report["comparedDays"], 2)
        self.assertEqual(report["changedDays"], 2)
        self.assertEqual(report["meanAbsoluteRevision"], 2.0)
        self.assertEqual(report["maxAbsoluteRevision"], 3.0)


if __name__ == "__main__":
    unittest.main()
