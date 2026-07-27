import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import crawler
import notion_client
import run_control


class RunControlTests(unittest.TestCase):
    def tearDown(self):
        run_control._stop_requested = False
        run_control._deadline_monotonic = None

    def test_retry_after_is_capped(self):
        self.assertEqual(
            crawler.get_site_retry_sleep_seconds(0, "900"),
            60.0,
        )
        self.assertEqual(
            notion_client.get_retry_sleep_seconds(0, 429, "900"),
            60.0,
        )
        self.assertEqual(
            notion_client.get_external_retry_sleep_seconds(0, "900"),
            60.0,
        )

    def test_expired_deadline_blocks_before_sleep(self):
        run_control._deadline_monotonic = 10.0
        with (
            patch.object(run_control.time, "monotonic", return_value=10.0),
            patch.object(run_control.time, "sleep") as sleep,
            self.assertRaisesRegex(RuntimeError, "마감시간"),
        ):
            run_control.sleep_with_run_control(60.0)
        sleep.assert_not_called()

    def test_stop_signal_interrupts_retry_sleep(self):
        def request_stop(_seconds):
            run_control._stop_requested = True

        with (
            patch.object(run_control.time, "monotonic", return_value=1.0),
            patch.object(
                run_control.time,
                "sleep",
                side_effect=request_stop,
            ) as sleep,
            self.assertRaisesRegex(RuntimeError, "종료 신호"),
        ):
            run_control.sleep_with_run_control(1.0)
        sleep.assert_called_once_with(0.25)

    def test_install_resets_previous_stop_request(self):
        run_control._stop_requested = True
        with (
            patch.dict(os.environ, {"INTERNAL_DEADLINE_SECONDS": "120"}),
            patch.object(run_control.time, "monotonic", return_value=10.0),
            patch.object(run_control.signal, "signal"),
        ):
            run_control.install_run_control()
        self.assertFalse(run_control._stop_requested)
        self.assertEqual(run_control._deadline_monotonic, 130.0)

    def test_internal_deadline_is_clamped_to_minimum(self):
        with (
            patch.dict(
                os.environ,
                {"INTERNAL_DEADLINE_SECONDS": "1"},
            ),
            patch.object(run_control.time, "monotonic", return_value=10.0),
            patch.object(run_control.signal, "signal"),
        ):
            run_control.install_run_control()

        self.assertEqual(run_control._deadline_monotonic, 70.0)

    def test_invalid_internal_deadline_uses_safe_default(self):
        with (
            patch.dict(
                os.environ,
                {"INTERNAL_DEADLINE_SECONDS": "invalid"},
            ),
            patch.object(run_control.time, "monotonic", return_value=10.0),
            patch.object(run_control.signal, "signal"),
        ):
            run_control.install_run_control()

        self.assertEqual(run_control._deadline_monotonic, 1210.0)

    def test_source_budget_preserves_destination_reserve(self):
        with (
            patch.object(
                crawler,
                "remaining_run_seconds",
                return_value=250.0,
            ),
            patch.object(
                crawler,
                "get_destination_state_reserve_seconds",
                return_value=300.0,
            ),
        ):
            budget = crawler.SourceRequestBudget()

        self.assertFalse(budget.consume_actual())
        self.assertEqual(
            budget.exhausted_reason,
            "destination_reserve_reached",
        )
        self.assertEqual(budget.actual_requests, 0)

    def test_source_budget_clamps_to_time_before_reserve(self):
        with (
            patch.dict(
                os.environ,
                {"SOURCE_MAX_SECONDS": "480"},
            ),
            patch.object(
                crawler,
                "remaining_run_seconds",
                return_value=420.0,
            ),
            patch.object(
                crawler,
                "get_destination_state_reserve_seconds",
                return_value=300.0,
            ),
        ):
            budget = crawler.SourceRequestBudget()

        self.assertEqual(budget.max_seconds, 120.0)

    def test_destination_reserve_requirement_fails_closed(self):
        run_control._deadline_monotonic = 250.0
        with (
            patch.dict(
                os.environ,
                {"DESTINATION_STATE_RESERVE_SECONDS": "300"},
            ),
            patch.object(
                run_control.time,
                "monotonic",
                return_value=0.0,
            ),
            self.assertRaisesRegex(RuntimeError, "실행 시간이 부족"),
        ):
            run_control.require_destination_state_reserve()


if __name__ == "__main__":
    unittest.main()
