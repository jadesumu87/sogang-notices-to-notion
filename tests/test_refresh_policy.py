import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import refresh_policy


class RefreshPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)

    def observation(self, notice_id: str, age: timedelta) -> dict[str, str]:
        return refresh_policy.build_notice_observation(
            notice_id,
            f"공지 {notice_id}",
            (self.now - age).isoformat(),
            False,
        )

    def test_age_tiers_use_hourly_daily_and_weekly_intervals(self):
        self.assertEqual(
            refresh_policy.refresh_interval_for_notice(
                (self.now - timedelta(days=7, hours=23)).isoformat(),
                self.now,
            ),
            timedelta(hours=1),
        )
        self.assertEqual(
            refresh_policy.refresh_interval_for_notice(
                (self.now - timedelta(days=8)).isoformat(),
                self.now,
            ),
            timedelta(days=1),
        )
        self.assertEqual(
            refresh_policy.refresh_interval_for_notice(
                (self.now - timedelta(days=31)).isoformat(),
                self.now,
            ),
            timedelta(days=7),
        )

    def test_refresh_is_due_only_after_each_age_interval(self):
        cases = (
            (timedelta(days=2), timedelta(minutes=59), False),
            (timedelta(days=2), timedelta(hours=1), True),
            (timedelta(days=10), timedelta(hours=23), False),
            (timedelta(days=10), timedelta(days=1), True),
            (timedelta(days=40), timedelta(days=6), False),
            (timedelta(days=40), timedelta(days=7), True),
        )
        for index, (age, elapsed, expected) in enumerate(cases):
            with self.subTest(index=index):
                current = self.observation(str(1000 + index), age)
                previous = {
                    **current,
                    "last_detail_at": (self.now - elapsed).isoformat(),
                }
                self.assertEqual(
                    refresh_policy.notice_refresh_due(
                        str(1000 + index),
                        current,
                        previous,
                        self.now,
                    ),
                    expected,
                )

    def test_list_fingerprint_change_forces_immediate_refresh(self):
        previous = self.observation("2000", timedelta(days=100))
        previous["last_detail_at"] = self.now.isoformat()
        current = refresh_policy.build_notice_observation(
            "2000",
            "수정된 제목",
            previous["published_at"],
            False,
        )

        self.assertTrue(
            refresh_policy.notice_refresh_due(
                "2000",
                current,
                previous,
                self.now,
            )
        )

    def test_old_notice_initialization_is_spread_and_overdue_work_retries(self):
        notice_ids_by_offset = {}
        for value in range(3000, 3100):
            notice_id = str(value)
            offset = refresh_policy.initial_archive_refresh_offset_days(
                notice_id
            )
            notice_ids_by_offset.setdefault(offset, notice_id)
        self.assertEqual(set(notice_ids_by_offset), set(range(7)))
        for offset, notice_id in notice_ids_by_offset.items():
            current = self.observation(notice_id, timedelta(days=100))
            previous = {
                **current,
                "first_seen_at": self.now.isoformat(),
            }
            if offset:
                self.assertFalse(
                    refresh_policy.notice_refresh_due(
                        notice_id,
                        current,
                        previous,
                        self.now + timedelta(days=offset - 1),
                    )
                )
            self.assertTrue(
                refresh_policy.notice_refresh_due(
                    notice_id,
                    current,
                    previous,
                    self.now + timedelta(days=offset),
                )
            )
            self.assertTrue(
                refresh_policy.notice_refresh_due(
                    notice_id,
                    current,
                    previous,
                    self.now + timedelta(days=offset + 1),
                )
            )

    def test_due_selection_ignores_unknown_and_not_yet_due_notices(self):
        due = self.observation("4000", timedelta(days=10))
        due["last_detail_at"] = (
            self.now - timedelta(days=1)
        ).isoformat()
        not_due = self.observation("4001", timedelta(days=10))
        not_due["last_detail_at"] = (
            self.now - timedelta(hours=23)
        ).isoformat()
        state = {
            "notice_refresh_state": {
                "4000": due,
                "4001": not_due,
                "4999": due,
            }
        }

        self.assertEqual(
            refresh_policy.select_due_notice_ids(
                state,
                {"4000", "4001"},
                self.now,
            ),
            ["4000"],
        )


if __name__ == "__main__":
    unittest.main()
