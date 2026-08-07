import json
import unittest
from datetime import date, datetime, timedelta

import collect_history_60d as collector


class SinaScoreWindowPriorityTests(unittest.TestCase):
    def setUp(self):
        self.conn = collector.connect_db(":memory:")
        collector.init_schema(self.conn)
        self.spec = collector.SourceSpec(
            key="sina:test",
            platform="sina",
            code="999999",
            name="测试来源",
            sectors=("__market__",),
        )
        collector.initialize_sources(self.conn, [self.spec], retry_blocked=False)
        self.raw_cutoff = datetime(2026, 5, 8, tzinfo=collector.CN_TZ)
        self.score_start = date(2026, 6, 7)
        self.as_of = datetime(2026, 8, 5, 23, 59, 59, tzinfo=collector.CN_TZ)

    def tearDown(self):
        self.conn.close()

    def _first_scoring_index(self):
        targets = collector.sina_target_dates(
            self.spec, self.raw_cutoff, self.as_of, spacing_days=2
        )
        return next(index for index, target in enumerate(targets) if target >= self.score_start)

    def test_advances_only_the_target_cursor_and_records_audit_metadata(self):
        self.conn.execute(
            "UPDATE sources SET sample_index = 3, status = 'running' WHERE source_key = ?",
            (self.spec.key,),
        )
        self.conn.execute(
            """
            INSERT INTO posts(
                platform, source_key, post_id, source_code, source_name,
                published_at, observed_at, title, author_hash, kind
            ) VALUES('sina', ?, 'p1', '999999', '测试来源', ?, ?, '保留的上下文', 'hash', 'post')
            """,
            (
                self.spec.key,
                self.raw_cutoff.isoformat(),
                (self.raw_cutoff + timedelta(days=1)).isoformat(),
            ),
        )
        self.conn.commit()

        event = collector.prioritize_sina_score_window(
            self.conn,
            [self.spec],
            raw_cutoff=self.raw_cutoff,
            score_start=self.score_start,
            as_of=self.as_of,
            spacing_days=2,
        )

        state = self.conn.execute(
            "SELECT sample_index, status FROM sources WHERE source_key = ?",
            (self.spec.key,),
        ).fetchone()
        self.assertEqual(state["sample_index"], self._first_scoring_index())
        self.assertEqual(state["status"], "running")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 1
        )
        self.assertEqual(event["changedSources"], 1)
        stored = json.loads(
            collector.get_meta(self.conn, "sina_score_window_priority")
        )
        self.assertEqual(stored["scoreStart"], "2026-06-07")
        self.assertEqual(stored["changes"][0]["source"], self.spec.key)
        stored_log = json.loads(
            collector.get_meta(self.conn, "sina_score_window_priority_log")
        )
        self.assertEqual(len(stored_log), 1)
        self.assertEqual(stored_log[0]["changedSources"], 1)

    def test_is_idempotent_after_source_has_reached_scoring_window(self):
        first_index = self._first_scoring_index()
        self.conn.execute(
            "UPDATE sources SET sample_index = ?, status = 'running' WHERE source_key = ?",
            (first_index + 2, self.spec.key),
        )
        self.conn.commit()

        event = collector.prioritize_sina_score_window(
            self.conn,
            [self.spec],
            raw_cutoff=self.raw_cutoff,
            score_start=self.score_start,
            as_of=self.as_of,
            spacing_days=2,
        )

        state = self.conn.execute(
            "SELECT sample_index FROM sources WHERE source_key = ?", (self.spec.key,)
        ).fetchone()
        self.assertEqual(state["sample_index"], first_index + 2)
        self.assertEqual(event["changedSources"], 0)

    def test_reopens_a_capped_source_when_advancing_it(self):
        self.conn.execute(
            "UPDATE sources SET sample_index = 0, status = 'capped' WHERE source_key = ?",
            (self.spec.key,),
        )
        self.conn.commit()

        collector.prioritize_sina_score_window(
            self.conn,
            [self.spec],
            raw_cutoff=self.raw_cutoff,
            score_start=self.score_start,
            as_of=self.as_of,
            spacing_days=2,
        )

        status = self.conn.execute(
            "SELECT status FROM sources WHERE source_key = ?", (self.spec.key,)
        ).fetchone()[0]
        self.assertEqual(status, "running")


if __name__ == "__main__":
    unittest.main()
