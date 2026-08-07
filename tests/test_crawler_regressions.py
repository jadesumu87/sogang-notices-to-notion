import json
import os
import socket
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import crawler
import main as crawler_main
import notion_client
import run_state
import sync_engine
from models import (
    CrawlReport,
    DestinationConsistencyError,
    FallbackDetailResult,
    FallbackPageResult,
    FailureCategory,
    ListPageResult,
    MutationKind,
    MutationPlan,
    SiteFetchResult,
    SourceCrawlResult,
    SourceSpec,
    SourceStatus,
    SyncCounters,
)
from run_state import (
    build_incident,
    default_run_state,
    update_state_from_report,
)
from validation import validate_crawl_report


SOURCE = SourceSpec(
    config_fk="141",
    classification="장학공지",
    list_url="https://www.sogang.ac.kr/ko/scholarship-notice",
)
DATE = "20260727120000"
BODY = [
    {
        "type": "paragraph",
        "paragraph": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {"content": "본문"},
                }
            ]
        },
    }
]


def complete_notice(
    notice_id: str,
    *,
    url: str | None = None,
    top: bool = False,
) -> dict:
    return {
        "source_id": SOURCE.config_fk,
        "notice_id": notice_id,
        "title": f"공지 {notice_id}",
        "url": url
        or (
            "https://www.sogang.ac.kr/ko/detail/"
            f"{notice_id}?bbsConfigFk={SOURCE.config_fk}"
        ),
        "date": "2026-07-27T12:00:00+09:00",
        "classification": SOURCE.classification,
        "top": top,
        "completeness": "complete",
        "body_status": "present",
        "body_blocks": BODY,
        "attachments_status": "known",
        "attachments_truncated": False,
    }


def api_entry(notice_id: str, top: bool = False) -> dict:
    return {
        "pkId": notice_id,
        "title": f"공지 {notice_id}",
        "regDate": DATE,
        "isTop": "Y" if top else "N",
        "userName": "교무처",
        "viewCount": 1,
    }


def api_detail(notice_id: str) -> dict:
    return {
        "title": f"공지 {notice_id}",
        "regDate": DATE,
        "userName": "교무처",
        "viewCount": 1,
        "content": "<p>본문</p>",
        "fileValue1": "",
    }


def api_page(
    entries: list[dict],
    *,
    terminal_verified: bool = False,
    total_count: int | None = None,
) -> ListPageResult:
    return ListPageResult(
        ok=True,
        entries=list(entries),
        valid_empty=not entries,
        requested_page=0,
        total_count=total_count,
        has_more=False if terminal_verified else None,
        terminal_verified=terminal_verified,
    )


def crawl_result(
    *,
    items: list[dict] | None = None,
    top_present: bool = False,
    top_verified: bool = False,
    coverage_complete: bool = False,
    reconcile_complete: bool = False,
    full_snapshot: bool = False,
    termination_reason: str = "natural_end",
) -> SourceCrawlResult:
    values = list(items or [])
    return SourceCrawlResult(
        source=SOURCE,
        status=SourceStatus.SUCCESS,
        items=values,
        observed_count=len(values),
        observed_ids=[
            str(item.get("notice_id") or "") for item in values
        ],
        top_urls=(
            [
                "https://www.sogang.ac.kr/ko/detail/"
                "1001?bbsConfigFk=141"
            ]
            if top_present
            else []
        ),
        terminal_reached=True,
        termination_reason=termination_reason,
        coverage_complete=coverage_complete,
        reconcile_complete=reconcile_complete,
        full_snapshot=full_snapshot,
        top_snapshot_verified=top_verified,
    )


def fresh_state() -> dict:
    state = default_run_state()
    state["state_checksum"] = ""
    return state


class FakeBrowserResponse:
    def __init__(self, status: int, retry_after: str = ""):
        self.status = status
        self.headers = (
            {"retry-after": retry_after}
            if retry_after
            else {}
        )


class FakeBrowserPage:
    def __init__(self, status: int):
        self.status = status
        self.url = SOURCE.list_url

    def goto(self, url, **kwargs):
        self.url = url
        return FakeBrowserResponse(self.status)

    def wait_for_load_state(self, *args, **kwargs):
        return None


class FakeBrowserContext:
    def __init__(self, status: int):
        self.page = FakeBrowserPage(status)

    def route(self, *args, **kwargs):
        return None

    def new_page(self):
        return self.page


class FakeBrowser:
    def __init__(self, status: int):
        self.status = status
        self.closed = False

    def new_context(self, **kwargs):
        return FakeBrowserContext(self.status)

    def close(self):
        self.closed = True


class FakeLauncher:
    def __init__(self, status: int):
        self.browser = FakeBrowser(status)

    def launch(self, **kwargs):
        return self.browser


class FakePlaywright:
    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, traceback):
        return False


class CrawlerRegressionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "API_MAX_REQUESTS": "100",
                "API_MAX_SECONDS": "60",
                "BACKFILL_DETAIL_LIMIT": "100",
                "BBS_PAGE_SIZE": "2",
                "CRAWLER_ACTIONS_ANNOTATIONS": "0",
                "CRAWL_HARD_PAGE_LIMIT": "10",
                "FALLBACK_MAX_REQUESTS": "100",
                "FALLBACK_MAX_SECONDS": "60",
                "FALLBACK_MIN_INTERVAL_SECONDS": "0",
                "FALLBACK_JITTER_SECONDS": "0",
                "SITE_MIN_REQUEST_INTERVAL_SECONDS": "0",
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_actions_annotations_respect_explicit_gate(self):
        incident = {"category": "internal", "count": 2}
        with (
            patch.dict(
                os.environ,
                {
                    "CRAWLER_ACTIONS_ANNOTATIONS": "0",
                    "GITHUB_ACTIONS": "true",
                },
            ),
            patch("builtins.print") as print_mock,
        ):
            crawler_main.report_destination_safety_hold("대기")
            crawler_main.report_deduplicated_failure(incident)
        print_mock.assert_not_called()

        with (
            patch.dict(
                os.environ,
                {
                    "CRAWLER_ACTIONS_ANNOTATIONS": "1",
                    "GITHUB_ACTIONS": "true",
                },
            ),
            patch("builtins.print") as print_mock,
        ):
            crawler_main.report_destination_safety_hold("대기")
            crawler_main.report_deduplicated_failure(incident)
        self.assertEqual(print_mock.call_count, 2)

    def test_default_backfill_detail_limit_is_bounded_for_short_runs(self):
        with patch.dict(
            os.environ,
            {"BACKFILL_DETAIL_LIMIT": ""},
        ):
            self.assertEqual(crawler.get_backfill_detail_limit(), 20)

    def test_missing_configured_source_result_fails_closed(self):
        report = validate_crawl_report(
            CrawlReport(sources=[crawl_result()]),
            {},
            expected_source_ids=["141", "2"],
        )

        self.assertFalse(report.write_safe)
        self.assertIn(
            "missing_source_result",
            {issue.code for issue in report.issues},
        )

    def test_duplicate_notice_identity_blocks_all_destination_access(self):
        duplicate = complete_notice("1001")
        report = validate_crawl_report(
            CrawlReport(
                sources=[
                    crawl_result(items=[duplicate, dict(duplicate)])
                ]
            ),
            fresh_state(),
        )

        with patch.object(
            sync_engine,
            "prepare_destination",
        ) as prepare_destination:
            counters = sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
            )

        self.assertFalse(report.write_safe)
        self.assertEqual(
            {
                issue.code
                for issue in report.issues
                if issue.fatal
            },
            {"duplicate_notice_id", "duplicate_source_url"},
        )
        prepare_destination.assert_not_called()
        self.assertEqual(counters.writes, 0)

    def test_same_url_with_different_notice_id_fails_closed(self):
        url = (
            "https://www.sogang.ac.kr/ko/detail/"
            "1001?bbsConfigFk=141"
        )
        report = validate_crawl_report(
            CrawlReport(
                sources=[
                    crawl_result(
                        items=[
                            complete_notice("1001", url=url),
                            complete_notice("1002", url=url),
                        ]
                    )
                ]
            ),
            fresh_state(),
        )

        self.assertFalse(report.write_safe)
        self.assertIn(
            "notice_identity_mismatch",
            {issue.code for issue in report.issues},
        )
        self.assertIn(
            "duplicate_source_url",
            {issue.code for issue in report.issues},
        )

    def test_invalid_verified_top_snapshot_only_revokes_top_decision(self):
        result = crawl_result(
            items=[complete_notice("1001")],
            top_verified=True,
        )
        result.top_urls = [
            "https://www.sogang.ac.kr/ko/detail/"
            "1001?bbsConfigFk=999"
        ]

        report = validate_crawl_report(
            CrawlReport(sources=[result]),
            fresh_state(),
        )

        self.assertTrue(report.write_safe)
        self.assertFalse(result.top_snapshot_verified)
        issue = next(
            item
            for item in report.issues
            if item.code == "invalid_top_snapshot_url"
        )
        self.assertFalse(issue.fatal)

    def test_top_snapshot_duplicate_and_item_mismatch_block_top_decision(self):
        result = crawl_result(
            items=[complete_notice("1002", top=True)],
            top_verified=True,
        )
        result.top_urls = [
            "https://www.sogang.ac.kr/ko/detail/"
            "1001?bbsConfigFk=141",
            "https://www.sogang.ac.kr/ko/detail/"
            "1001?bbsConfigFk=141",
        ]

        report = validate_crawl_report(
            CrawlReport(sources=[result]),
            fresh_state(),
        )

        self.assertTrue(report.write_safe)
        self.assertFalse(result.top_snapshot_verified)
        self.assertEqual(
            {
                issue.code
                for issue in report.issues
                if not issue.fatal
            },
            {
                "duplicate_top_snapshot_id",
                "top_snapshot_item_mismatch",
            },
        )

    def test_top_identity_defense_rejects_invalid_verified_payload(self):
        result = crawl_result(top_verified=True)
        result.top_urls = [
            "https://www.sogang.ac.kr/ko/detail/"
            "1001?bbsConfigFk=999"
        ]

        with self.assertRaises(DestinationConsistencyError):
            sync_engine.current_top_notice_ids(result)

    def test_external_download_circuit_produces_recoverable_incident_summary(self):
        counters = SyncCounters(
            external_download_requests=7,
            external_download_stopped_reason="http_429",
            external_download_status_code=429,
            external_download_retry_after="30",
            external_download_retry_after_seconds=30.0,
        )

        summary = crawler_main.external_download_incident_summary(
            counters
        )

        self.assertIn("상태 코드=429", summary)
        self.assertIn("중단 사유=http_429", summary)
        self.assertIn("요청 수=7", summary)
        self.assertIn("재시도 대기=30", summary)
        self.assertEqual(
            crawler_main.external_download_incident_summary(
                SyncCounters(
                    external_download_stopped_reason="request_cap",
                )
            ),
            "",
        )

    def test_destination_401_then_429_create_distinct_failure_notifications(self):
        report = CrawlReport(sources=[crawl_result()])
        state = default_run_state()

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.dict(os.environ, {}, clear=True),
        ):
            state_path = Path(temp_dir) / "run-state.json"
            incident_path = Path(temp_dir) / "incident.json"
            auth_record = crawler_main.create_run_record(False, False)

            crawler_main.persist_failed_run(
                state,
                auth_record,
                notion_client.NotionRequestError(
                    "Notion unauthorized",
                    status_code=401,
                ),
                report,
                state_path,
                incident_path,
            )
            auth_incident = json.loads(
                incident_path.read_text(encoding="utf-8")
            )
            rate_limit_record = crawler_main.create_run_record(False, False)

            crawler_main.persist_failed_run(
                state,
                rate_limit_record,
                notion_client.NotionRequestError(
                    "Notion rate limited",
                    status_code=429,
                ),
                report,
                state_path,
                incident_path,
            )
            rate_limit_incident = json.loads(
                incident_path.read_text(encoding="utf-8")
            )

        self.assertEqual(
            state["runs"][-2]["failure_category"],
            FailureCategory.DESTINATION_AUTH.value,
        )
        self.assertEqual(
            state["runs"][-1]["failure_category"],
            FailureCategory.DESTINATION_RATE_LIMIT.value,
        )
        self.assertEqual(
            auth_incident["category"],
            FailureCategory.DESTINATION_AUTH.value,
        )
        self.assertEqual(
            rate_limit_incident["category"],
            FailureCategory.DESTINATION_RATE_LIMIT.value,
        )
        self.assertNotEqual(
            auth_incident["fingerprint"],
            rate_limit_incident["fingerprint"],
        )
        self.assertNotEqual(
            auth_incident["occurrence_id"],
            rate_limit_incident["occurrence_id"],
        )
        self.assertTrue(auth_incident["should_signal_failure"])
        self.assertTrue(rate_limit_incident["should_signal_failure"])
        self.assertEqual(rate_limit_incident["count"], 1)

    def test_write_failure_preserves_all_report_items_for_retry(self):
        state = default_run_state()
        state["sources"]["141"] = {
            "pending_notice_ids": ["999"],
        }
        report = CrawlReport(
            sources=[
                crawl_result(
                    items=[
                        complete_notice("100"),
                        complete_notice("101"),
                    ]
                )
            ]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            crawler_main.persist_failed_run(
                state,
                crawler_main.create_run_record(False, False),
                RuntimeError("destination failure"),
                report,
                Path(temp_dir) / "run-state.json",
                Path(temp_dir) / "incident.json",
            )

        self.assertEqual(
            state["sources"]["141"]["pending_notice_ids"],
            ["100", "101", "999"],
        )

    def test_dry_run_failure_does_not_schedule_report_items_for_retry(self):
        state = default_run_state()
        report = CrawlReport(
            sources=[crawl_result(items=[complete_notice("100")])]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            crawler_main.persist_failed_run(
                state,
                crawler_main.create_run_record(False, True),
                RuntimeError("dry run failure"),
                report,
                Path(temp_dir) / "run-state.json",
                Path(temp_dir) / "incident.json",
            )

        self.assertNotIn("141", state["sources"])

    def test_scheduled_duplicate_failure_notice_is_deduplicated_until_repeat(self):
        state = default_run_state()
        fixed_now = datetime.now(timezone.utc).replace(
            microsecond=0
        ).isoformat()
        environment = {
            "FAILURE_REPEAT_SECONDS": "21600",
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "schedule",
            "GITHUB_RUN_ATTEMPT": "1",
        }

        with (
            patch.dict(os.environ, environment, clear=False),
            patch.object(run_state, "utc_now_iso", return_value=fixed_now),
        ):
            first = build_incident(
                state,
                FailureCategory.SOURCE_UPSTREAM,
                "출처 실패",
                "동일 장애",
            )
            first_deduplicated = (
                crawler_main.apply_failure_signal_policy(state, first)
            )
            second = build_incident(
                state,
                FailureCategory.SOURCE_UPSTREAM,
                "출처 실패",
                "동일 장애",
            )
            second_deduplicated = (
                crawler_main.apply_failure_signal_policy(state, second)
            )

        self.assertFalse(first_deduplicated)
        self.assertTrue(second_deduplicated)
        self.assertTrue(first["should_signal_failure"])
        self.assertFalse(second["should_signal_failure"])
        self.assertEqual(second["count"], 2)

    def test_manual_duplicate_failure_notice_is_never_deduplicated(self):
        state = default_run_state()
        fixed_now = datetime.now(timezone.utc).replace(
            microsecond=0
        ).isoformat()

        with (
            patch.dict(
                os.environ,
                {
                    "FAILURE_REPEAT_SECONDS": "21600",
                    "GITHUB_ACTIONS": "true",
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                },
                clear=False,
            ),
            patch.object(run_state, "utc_now_iso", return_value=fixed_now),
        ):
            first = build_incident(
                state,
                FailureCategory.SOURCE_UPSTREAM,
                "출처 실패",
                "동일 장애",
            )
            crawler_main.apply_failure_signal_policy(state, first)
            second = build_incident(
                state,
                FailureCategory.SOURCE_UPSTREAM,
                "출처 실패",
                "동일 장애",
            )
            deduplicated = crawler_main.apply_failure_signal_policy(
                state,
                second,
            )

        self.assertFalse(deduplicated)
        self.assertTrue(second["should_signal_failure"])

    def test_scheduled_job_rerun_notice_is_never_deduplicated(self):
        state = default_run_state()
        fixed_now = datetime.now(timezone.utc).replace(
            microsecond=0
        ).isoformat()
        common_environment = {
            "FAILURE_REPEAT_SECONDS": "21600",
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "schedule",
        }

        with (
            patch.dict(
                os.environ,
                {**common_environment, "GITHUB_RUN_ATTEMPT": "1"},
                clear=False,
            ),
            patch.object(run_state, "utc_now_iso", return_value=fixed_now),
        ):
            first = build_incident(
                state,
                FailureCategory.SOURCE_UPSTREAM,
                "출처 실패",
                "동일 장애",
            )
            crawler_main.apply_failure_signal_policy(state, first)

        with (
            patch.dict(
                os.environ,
                {**common_environment, "GITHUB_RUN_ATTEMPT": "2"},
                clear=False,
            ),
            patch.object(run_state, "utc_now_iso", return_value=fixed_now),
        ):
            rerun = build_incident(
                state,
                FailureCategory.SOURCE_UPSTREAM,
                "출처 실패",
                "동일 장애",
            )
            deduplicated = crawler_main.apply_failure_signal_policy(
                state,
                rerun,
            )

        self.assertFalse(deduplicated)
        self.assertTrue(rerun["should_signal_failure"])

    def test_scheduled_duplicate_failure_still_raises_from_main(self):
        state = default_run_state()
        error = RuntimeError("scheduled failure sentinel")
        first = build_incident(
            state,
            FailureCategory.INTERNAL,
            "서강대 공지 동기화 실패",
            str(error),
            exception=error,
        )
        self.assertTrue(run_state.mark_failure_signaled(state, first))
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)
        temp_dir = Path(temp_directory.name)

        with (
            patch.dict(
                os.environ,
                {
                    "NOTION_TOKEN": "token",
                    "NOTION_DB_ID": "database",
                    "GITHUB_ACTIONS": "true",
                    "GITHUB_EVENT_NAME": "schedule",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "FAILURE_REPEAT_SECONDS": "21600",
                },
            ),
            patch.multiple(
                crawler_main,
                setup_logging=Mock(),
                load_dotenv=Mock(),
                log_environment_info=Mock(),
                install_run_control=Mock(),
                is_writer_context_confirmed=Mock(return_value=True),
                should_run_dry_run=Mock(return_value=False),
                should_run_notion_schema_migration_only=Mock(
                    return_value=False
                ),
                should_use_incremental_crawl=Mock(return_value=True),
                get_bbs_config_fks=Mock(return_value=["141"]),
                should_full_reconcile=Mock(return_value=False),
                get_run_state_path=Mock(
                    return_value=temp_dir / "run-state.json"
                ),
                get_snapshot_path=Mock(
                    return_value=temp_dir / "snapshot.json"
                ),
                get_incident_path=Mock(
                    return_value=temp_dir / "incident.json"
                ),
                load_run_state=Mock(return_value=state),
                refresh_destination_pending_notice_state=Mock(
                    return_value=0
                ),
                collect_report=Mock(side_effect=error),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "scheduled failure sentinel",
            ),
        ):
            crawler_main.main()

        incident = json.loads(
            (temp_dir / "incident.json").read_text(encoding="utf-8")
        )
        recorded_state = json.loads(
            (temp_dir / "run-state.json").read_text(encoding="utf-8")
        )
        self.assertFalse(incident["should_signal_failure"])
        self.assertEqual(recorded_state["runs"][-1]["status"], "failed")

    def test_main_passes_configured_sources_to_validation(self):
        collected = CrawlReport(sources=[crawl_result()])
        validate = Mock(side_effect=RuntimeError("validation sentinel"))
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.dict(
                os.environ,
                {
                    "NOTION_TOKEN": "token",
                    "NOTION_DB_ID": "database",
                },
            ),
            patch.object(crawler_main, "setup_logging"),
            patch.object(crawler_main, "load_dotenv"),
            patch.object(crawler_main, "log_environment_info"),
            patch.multiple(
                crawler_main,
                install_run_control=Mock(),
                is_writer_context_confirmed=Mock(
                    return_value=True
                ),
            ),
            patch.object(crawler_main, "should_run_dry_run", return_value=False),
            patch.object(
                crawler_main,
                "should_run_notion_schema_migration_only",
                return_value=False,
            ),
            patch.object(
                crawler_main,
                "should_use_incremental_crawl",
                return_value=True,
            ),
            patch.object(
                crawler_main,
                "get_bbs_config_fks",
                return_value=["141", "2"],
            ),
            patch.object(
                crawler_main,
                "get_run_state_path",
                return_value=Path(temp_dir) / "run-state.json",
            ),
            patch.object(
                crawler_main,
                "get_snapshot_path",
                return_value=Path(temp_dir) / "snapshot.json",
            ),
            patch.object(
                crawler_main,
                "get_incident_path",
                return_value=Path(temp_dir) / "incident.json",
            ),
            patch.object(
                crawler_main,
                "load_run_state",
                return_value=default_run_state(),
            ),
            patch.object(
                crawler_main,
                "collect_report",
                return_value=collected,
            ),
            patch.object(
                crawler_main,
                "validate_crawl_report",
                validate,
            ),
            self.assertRaisesRegex(RuntimeError, "validation sentinel"),
        ):
            crawler_main.main()

        self.assertEqual(
            validate.call_args.kwargs["expected_source_ids"],
            ["141", "2"],
        )

    def _run_main_with_sync_result(
        self,
        temp_dir: str,
        report: CrawlReport,
        counters: SyncCounters | None,
        state: dict,
        *,
        dry_run: bool = False,
        plan: MutationPlan | None = None,
    ) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "NOTION_TOKEN": "token",
                    "NOTION_DB_ID": "database",
                    "GITHUB_RUN_ID": "current-run",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "CRAWLER_RUN_ID": "current-run",
                    "CRAWLER_RUN_ATTEMPT": "1",
                },
            ),
            patch.object(crawler_main, "setup_logging"),
            patch.object(crawler_main, "load_dotenv"),
            patch.object(crawler_main, "log_environment_info"),
            patch.multiple(
                crawler_main,
                install_run_control=Mock(),
                is_writer_context_confirmed=Mock(
                    return_value=True
                ),
            ),
            patch.object(
                crawler_main,
                "should_run_dry_run",
                return_value=dry_run,
            ),
            patch.object(
                crawler_main,
                "should_run_notion_schema_migration_only",
                return_value=False,
            ),
            patch.object(
                crawler_main,
                "should_use_incremental_crawl",
                return_value=True,
            ),
            patch.object(
                crawler_main,
                "get_bbs_config_fks",
                return_value=[
                    result.source.config_fk
                    for result in report.sources
                ],
            ),
            patch.object(
                crawler_main,
                "should_full_reconcile",
                return_value=False,
            ),
            patch.object(
                crawler_main,
                "get_run_state_path",
                return_value=Path(temp_dir) / "run-state.json",
            ),
            patch.object(
                crawler_main,
                "get_snapshot_path",
                return_value=Path(temp_dir) / "snapshot.json",
            ),
            patch.object(
                crawler_main,
                "get_incident_path",
                return_value=Path(temp_dir) / "incident.json",
            ),
            patch.object(
                crawler_main,
                "load_run_state",
                return_value=state,
            ),
            patch.object(
                crawler_main,
                "collect_report",
                return_value=report,
            ),
            patch.object(
                crawler_main,
                "validate_crawl_report",
                return_value=report,
            ),
            patch.object(
                crawler_main,
                "require_destination_state_reserve",
            ),
            patch.object(
                crawler_main,
                "apply_report",
                return_value=counters,
            ),
            patch.object(
                crawler_main,
                "build_dry_run_plan",
                return_value=plan,
            ),
        ):
            crawler_main.main()

    def test_main_records_first_quarantine_as_safety_deferred_success(self):
        healthy = crawl_result()
        quarantined = SourceCrawlResult(
            source=SourceSpec(
                config_fk="2",
                classification="학사공지",
                list_url=(
                    "https://www.sogang.ac.kr/ko/academic-notice"
                ),
            ),
            status=SourceStatus.SUCCESS,
            terminal_reached=True,
            termination_reason="natural_end",
        )
        report = CrawlReport(sources=[healthy, quarantined])
        hold_key = sync_engine.destination_hold_key("2", "548926")
        counters = SyncCounters(
            quarantined_source_ids=["2"],
            unresolved_pending_page_ids=["pending-1"],
            pending_seen=1,
            destination_hold_observations={
                hold_key: {
                    "candidate_id": "",
                    "reason": "pending_refresh",
                }
            },
            destination_hold_count=1,
            observation_run_id="current-run:1",
            observation_logical_run_id="current-run",
        )
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)
        temp_dir = temp_directory.name

        self._run_main_with_sync_result(
            temp_dir,
            report,
            counters,
            default_run_state(),
        )

        state = json.loads(
            (Path(temp_dir) / "run-state.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            state["runs"][-1]["status"],
            "safety_deferred",
        )
        self.assertEqual(
            state["runs"][-1]["failure_category"],
            FailureCategory.NONE.value,
        )
        self.assertIn(
            "last_success_at",
            state["sources"]["141"],
        )
        self.assertNotIn(
            "last_success_at",
            state["sources"]["2"],
        )
        self.assertIsNone(state["last_success_at"])
        self.assertIsNotNone(state["last_partial_success_at"])
        self.assertFalse(
            (Path(temp_dir) / "incident.json").exists()
        )
        self.assertFalse(report.issues[-1].fatal)
        self.assertEqual(
            state["destination_holds"][hold_key]["observations"],
            1,
        )

    def test_main_rejects_unpersisted_destination_hold(self):
        report = CrawlReport(sources=[crawl_result()])
        counters = SyncCounters(
            quarantined_source_ids=["141"],
            unresolved_pending_page_ids=["pending-1"],
            pending_seen=1,
        )
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)

        with self.assertRaisesRegex(
            DestinationConsistencyError,
            "집계의 일관성",
        ):
            self._run_main_with_sync_result(
                temp_directory.name,
                report,
                counters,
                default_run_state(),
            )

    def test_main_escalates_repeated_quarantine_to_failure(self):
        report = CrawlReport(sources=[crawl_result()])
        hold_key = sync_engine.destination_hold_key("141", "548926")
        counters = SyncCounters(
            quarantined_source_ids=["141"],
            unresolved_pending_page_ids=["pending-1"],
            pending_seen=1,
            destination_hold_observations={
                hold_key: {
                    "candidate_id": "",
                    "reason": "pending_refresh",
                }
            },
            destination_hold_count=1,
            repeated_destination_hold_count=1,
            observation_run_id="current-run:1",
            observation_logical_run_id="current-run",
        )
        state = default_run_state()
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)
        temp_dir = temp_directory.name

        with self.assertRaises(DestinationConsistencyError):
            self._run_main_with_sync_result(
                temp_dir,
                report,
                counters,
                state,
            )

        saved_state = json.loads(
            (Path(temp_dir) / "run-state.json").read_text(
                encoding="utf-8"
            )
        )
        incident = json.loads(
            (Path(temp_dir) / "incident.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            saved_state["runs"][-1]["status"],
            "partial_failed",
        )
        self.assertEqual(
            saved_state["runs"][-1]["failure_category"],
            FailureCategory.DESTINATION_CONTRACT.value,
        )
        self.assertEqual(
            incident["category"],
            FailureCategory.DESTINATION_CONTRACT.value,
        )
        self.assertTrue(report.issues[-1].fatal)

    def test_main_dry_run_reports_quarantine_without_failure(self):
        report = CrawlReport(sources=[crawl_result()])
        plan = MutationPlan(
            run_id="dry-run",
            quarantined_source_ids=["141"],
        )
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)
        temp_dir = temp_directory.name

        self._run_main_with_sync_result(
            temp_dir,
            report,
            None,
            default_run_state(),
            dry_run=True,
            plan=plan,
        )

        saved_state = json.loads(
            (Path(temp_dir) / "run-state.json").read_text(
                encoding="utf-8"
            )
        )
        snapshot = json.loads(
            (Path(temp_dir) / "snapshot.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            saved_state["runs"][-1]["status"],
            "dry_run_deferred",
        )
        self.assertEqual(
            saved_state["runs"][-1]["failure_category"],
            FailureCategory.NONE.value,
        )
        self.assertFalse(
            (Path(temp_dir) / "incident.json").exists()
        )
        self.assertTrue(snapshot["report"]["write_safe"])
        self.assertFalse(report.issues[-1].fatal)

    def test_main_dry_run_quarantine_does_not_mask_source_failure(self):
        failed = crawl_result()
        failed.status = SourceStatus.FAILED
        failed.category = FailureCategory.SOURCE_UPSTREAM
        failed.error = "source unavailable"
        failed.terminal_reached = False
        failed.termination_reason = ""
        report = CrawlReport(sources=[failed])
        plan = MutationPlan(
            run_id="dry-run",
            quarantined_source_ids=["141"],
        )
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)
        temp_dir = temp_directory.name

        with self.assertRaisesRegex(
            RuntimeError,
            "source unavailable",
        ):
            self._run_main_with_sync_result(
                temp_dir,
                report,
                None,
                default_run_state(),
                dry_run=True,
                plan=plan,
            )

        saved_state = json.loads(
            (Path(temp_dir) / "run-state.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            saved_state["runs"][-1]["status"],
            "dry_run_failed",
        )
        self.assertEqual(
            saved_state["runs"][-1]["failure_category"],
            FailureCategory.SOURCE_UPSTREAM.value,
        )

    def test_main_safety_hold_does_not_mask_external_download_failure(self):
        report = CrawlReport(sources=[crawl_result()])
        hold_key = sync_engine.destination_hold_key("141", "548926")
        counters = SyncCounters(
            quarantined_source_ids=["141"],
            unresolved_pending_page_ids=["pending-1"],
            pending_seen=1,
            destination_hold_observations={
                hold_key: {
                    "candidate_id": "",
                    "reason": "pending_refresh",
                }
            },
            destination_hold_count=1,
            observation_run_id="current-run:1",
            observation_logical_run_id="current-run",
            external_download_status_code=403,
            external_download_stopped_reason="http_403",
            external_download_requests=1,
        )
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)
        temp_dir = temp_directory.name

        with self.assertRaisesRegex(
            RuntimeError,
            "외부 파일 다운로드 안전 회로",
        ):
            self._run_main_with_sync_result(
                temp_dir,
                report,
                counters,
                default_run_state(),
            )

        saved_state = json.loads(
            (Path(temp_dir) / "run-state.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            saved_state["runs"][-1]["status"],
            "partial_failed",
        )
        self.assertEqual(
            saved_state["runs"][-1]["failure_category"],
            FailureCategory.SECURITY_POLICY.value,
        )

    def test_main_repeated_hold_does_not_mask_external_download_failure(
        self,
    ):
        report = CrawlReport(sources=[crawl_result()])
        hold_key = sync_engine.destination_hold_key("141", "548926")
        counters = SyncCounters(
            quarantined_source_ids=["141"],
            unresolved_pending_page_ids=["pending-1"],
            pending_seen=1,
            destination_hold_observations={
                hold_key: {
                    "candidate_id": "",
                    "reason": "pending_refresh",
                }
            },
            destination_hold_count=1,
            repeated_destination_hold_count=1,
            observation_run_id="current-run:1",
            observation_logical_run_id="current-run",
            external_download_status_code=403,
            external_download_stopped_reason="http_403",
            external_download_requests=1,
        )
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)

        with self.assertRaisesRegex(
            RuntimeError,
            "외부 파일.*Notion 안전 보류",
        ):
            self._run_main_with_sync_result(
                temp_directory.name,
                report,
                counters,
                default_run_state(),
            )

        incident = json.loads(
            (
                Path(temp_directory.name) / "incident.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            incident["category"],
            FailureCategory.SECURITY_POLICY.value,
        )
        self.assertIn("외부 파일:", incident["summary"])
        self.assertIn("Notion 안전 보류:", incident["summary"])

    def test_main_repeated_hold_does_not_mask_source_failure(self):
        healthy = crawl_result()
        failed = SourceCrawlResult(
            source=SourceSpec(
                config_fk="2",
                classification="학사공지",
                list_url=(
                    "https://www.sogang.ac.kr/ko/academic-notice"
                ),
            ),
            status=SourceStatus.FAILED,
            category=FailureCategory.SOURCE_UPSTREAM,
            error="source unavailable",
        )
        report = CrawlReport(sources=[healthy, failed])
        hold_key = sync_engine.destination_hold_key("141", "548926")
        counters = SyncCounters(
            quarantined_source_ids=["141"],
            unresolved_pending_page_ids=["pending-1"],
            unresolved_pending_notices={"141": ["548926"]},
            pending_seen=1,
            destination_hold_observations={
                hold_key: {
                    "candidate_id": "",
                    "reason": "pending_refresh",
                }
            },
            destination_hold_count=1,
            repeated_destination_hold_count=1,
            observation_run_id="current-run:1",
            observation_logical_run_id="current-run",
        )
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)

        with self.assertRaisesRegex(
            RuntimeError,
            "수집 검증.*Notion 안전 보류",
        ):
            self._run_main_with_sync_result(
                temp_directory.name,
                report,
                counters,
                default_run_state(),
            )

        incident = json.loads(
            (
                Path(temp_directory.name) / "incident.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            incident["category"],
            FailureCategory.SOURCE_UPSTREAM.value,
        )
        self.assertIn("수집 검증:", incident["summary"])
        self.assertIn("Notion 안전 보류:", incident["summary"])

    def test_main_external_download_circuit_blocks_success_state_advancement(
        self,
    ):
        result = crawl_result(
            items=[complete_notice("1001")],
            termination_reason="backfill_window",
        )
        result.reconcile_requested = True
        result.refreshed_known_ids = ["1001"]
        result.refresh_window_end_id = "1001"
        result.backfill_resume_page = 7
        result.backfill_anchor_ids = ["1000"]
        report = CrawlReport(sources=[result])
        counters = SyncCounters(
            observation_run_id="run-429:1",
            observation_logical_run_id="run-429",
            external_download_requests=1,
            external_download_stopped_reason="http_429",
            external_download_status_code=429,
            external_download_retry_after="45",
            external_download_retry_after_seconds=45.0,
        )
        previous_success = "2026-07-26T00:00:00+00:00"
        initial_state = default_run_state()
        initial_state["last_success_at"] = previous_success
        initial_state["consecutive_failures"] = 2
        initial_state["sources"]["141"] = {
            "last_success_at": previous_success,
            "observed_ids": ["1000"],
            "detail_refresh_cursor_id": "1000",
            "backfill_active": True,
            "backfill_resume_page": 4,
            "backfill_anchor_ids": ["999"],
        }
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)
        temp_dir = Path(temp_directory.name)

        with (
            patch.dict(
                os.environ,
                {
                    "NOTION_TOKEN": "token",
                    "NOTION_DB_ID": "database",
                },
            ),
            patch.multiple(
                crawler_main,
                setup_logging=Mock(),
                load_dotenv=Mock(),
                log_environment_info=Mock(),
                install_run_control=Mock(),
                is_writer_context_confirmed=Mock(
                    return_value=True
                ),
                should_run_dry_run=Mock(return_value=False),
                should_run_notion_schema_migration_only=Mock(
                    return_value=False
                ),
                should_use_incremental_crawl=Mock(
                    return_value=True
                ),
                get_bbs_config_fks=Mock(return_value=["141"]),
                should_full_reconcile=Mock(return_value=False),
                get_run_state_path=Mock(
                    return_value=temp_dir / "run-state.json"
                ),
                get_snapshot_path=Mock(
                    return_value=temp_dir / "snapshot.json"
                ),
                get_incident_path=Mock(
                    return_value=temp_dir / "incident.json"
                ),
                load_run_state=Mock(return_value=initial_state),
                collect_report=Mock(return_value=report),
                validate_crawl_report=Mock(return_value=report),
                require_destination_state_reserve=Mock(),
                apply_report=Mock(return_value=counters),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "외부 파일 다운로드 안전 회로",
            ),
        ):
            crawler_main.main()

        state = json.loads(
            (temp_dir / "run-state.json").read_text(encoding="utf-8")
        )
        incident = json.loads(
            (temp_dir / "incident.json").read_text(encoding="utf-8")
        )
        source_state = state["sources"]["141"]
        self.assertEqual(state["runs"][-1]["status"], "partial_failed")
        self.assertEqual(
            state["runs"][-1]["failure_category"],
            FailureCategory.SECURITY_POLICY.value,
        )
        self.assertEqual(state["last_success_at"], previous_success)
        self.assertEqual(state["consecutive_failures"], 3)
        self.assertEqual(
            source_state["last_success_at"],
            previous_success,
        )
        self.assertEqual(source_state["observed_ids"], ["1000"])
        self.assertEqual(
            source_state["detail_refresh_cursor_id"],
            "1000",
        )
        self.assertEqual(source_state["backfill_resume_page"], 4)
        self.assertEqual(source_state["backfill_anchor_ids"], ["999"])
        self.assertEqual(
            incident["category"],
            FailureCategory.SECURITY_POLICY.value,
        )
        self.assertTrue(
            any(
                issue.code == "external_download_circuit"
                and issue.fatal
                and not issue.source_config_fk
                for issue in report.issues
            )
        )

    def test_detail_429_stops_before_fallback_and_preserves_retry_after(self):
        list_fetch = Mock(return_value=api_page([api_entry("1001")]))
        detail_fetch = Mock(
            side_effect=crawler.SourceAccessBlocked(
                "rate_limited",
                429,
                30.0,
            )
        )
        fallback = Mock(
            side_effect=AssertionError("fallback must not run")
        )

        with (
            patch.object(
                crawler,
                "fetch_bbs_list_result",
                list_fetch,
            ),
            patch.object(
                crawler,
                "fetch_bbs_detail",
                detail_fetch,
            ),
            patch.object(
                crawler,
                "crawl_top_items_playwright_result",
                fallback,
            ),
        ):
            result = crawler.SogangSourceAdapter().crawl(
                SOURCE,
                known_ids=set(),
                incremental=False,
            )

        self.assertFalse(result.write_safe)
        self.assertEqual(result.category, FailureCategory.SECURITY_POLICY)
        self.assertEqual(result.error, "rate_limited")
        self.assertEqual(result.method, "api_source_circuit_open")
        self.assertEqual(result.retry_after_seconds, 30.0)
        self.assertEqual(list_fetch.call_count, 1)
        self.assertEqual(detail_fetch.call_count, 1)
        self.assertEqual(fallback.call_count, 0)

    def test_retry_after_http_date_is_converted_to_seconds(self):
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=120)
        raw_value = format_datetime(retry_at, usegmt=True)

        seconds = crawler.parse_retry_after_seconds(raw_value)

        self.assertIsNotNone(seconds)
        self.assertGreaterEqual(seconds, 118.0)
        self.assertLessEqual(seconds, 120.0)

    def test_security_block_on_one_source_opens_same_host_circuit(self):
        sources = {
            "141": SOURCE,
            "2": SourceSpec(
                config_fk="2",
                classification="학사공지",
                list_url=(
                    "https://www.sogang.ac.kr/ko/"
                    "academic-notice"
                ),
            ),
        }
        first = SourceCrawlResult(
            source=SOURCE,
            status=SourceStatus.FAILED,
            method="api_source_circuit_open",
            category=FailureCategory.SECURITY_POLICY,
            error="rate_limited",
            termination_reason="detail_error",
            retry_after_seconds=30.0,
        )
        crawl = Mock(return_value=first)

        with (
            patch.object(
                crawler,
                "get_bbs_config_fks",
                return_value=["141", "2"],
            ),
            patch.object(
                crawler,
                "build_source_spec",
                side_effect=lambda source_id: sources[source_id],
            ),
            patch.object(
                crawler.SogangSourceAdapter,
                "crawl",
                crawl,
            ),
        ):
            report = crawler.crawl_sources()

        self.assertEqual(crawl.call_count, 1)
        self.assertEqual(len(report.sources), 2)
        self.assertEqual(
            report.sources[1].method,
            "host_circuit_open",
        )
        self.assertEqual(
            report.sources[1].category,
            FailureCategory.SECURITY_POLICY,
        )
        self.assertEqual(
            report.sources[1].retry_after_seconds,
            30.0,
        )

    def test_source_local_failures_do_not_open_same_host_circuit(self):
        sources = {
            "141": SOURCE,
            "2": SourceSpec(
                config_fk="2",
                classification="학사공지",
                list_url=(
                    "https://www.sogang.ac.kr/ko/"
                    "academic-notice"
                ),
            ),
        }
        cases = (
            (FailureCategory.SECURITY_POLICY, "access_forbidden"),
            (
                FailureCategory.SECURITY_POLICY,
                "response_too_large:2048>1024",
            ),
            (
                FailureCategory.SECURITY_POLICY,
                "unsafe_redirect_target",
            ),
            (FailureCategory.SOURCE_UPSTREAM, "HTTP 503"),
        )
        for category, error in cases:
            with self.subTest(category=category, error=error):
                calls: list[str] = []

                def crawl_source(source, **_kwargs):
                    calls.append(source.config_fk)
                    if source.config_fk == "141":
                        return SourceCrawlResult(
                            source=source,
                            status=SourceStatus.FAILED,
                            method="api_source_circuit_open",
                            category=category,
                            error=error,
                            termination_reason="detail_error",
                        )
                    return SourceCrawlResult(
                        source=source,
                        status=SourceStatus.SUCCESS,
                        method="api",
                        terminal_reached=True,
                        termination_reason="natural_end",
                    )

                with (
                    patch.object(
                        crawler,
                        "get_bbs_config_fks",
                        return_value=["141", "2"],
                    ),
                    patch.object(
                        crawler,
                        "build_source_spec",
                        side_effect=lambda source_id: sources[source_id],
                    ),
                    patch.object(
                        crawler.SogangSourceAdapter,
                        "crawl",
                        side_effect=crawl_source,
                    ),
                ):
                    report = crawler.crawl_sources()

                self.assertEqual(calls, ["141", "2"])
                self.assertEqual(report.sources[1].method, "api")
                self.assertTrue(report.sources[1].write_safe)

    def test_confirmed_access_challenge_opens_same_host_circuit(self):
        sources = {
            "141": SOURCE,
            "2": SourceSpec(
                config_fk="2",
                classification="학사공지",
                list_url=(
                    "https://www.sogang.ac.kr/ko/"
                    "academic-notice"
                ),
            ),
        }
        first = SourceCrawlResult(
            source=SOURCE,
            status=SourceStatus.FAILED,
            method="fallback_playwright",
            category=FailureCategory.SECURITY_POLICY,
            error="fallback_browser_access_challenge",
            termination_reason="page_error",
        )
        crawl = Mock(return_value=first)

        with (
            patch.object(
                crawler,
                "get_bbs_config_fks",
                return_value=["141", "2"],
            ),
            patch.object(
                crawler,
                "build_source_spec",
                side_effect=lambda source_id: sources[source_id],
            ),
            patch.object(
                crawler.SogangSourceAdapter,
                "crawl",
                crawl,
            ),
        ):
            report = crawler.crawl_sources()

        self.assertEqual(crawl.call_count, 1)
        self.assertEqual(report.sources[1].method, "host_circuit_open")

    def test_playwright_403_stops_before_http_fallback(self):
        original = SourceCrawlResult(
            source=SOURCE,
            status=SourceStatus.FAILED,
            method="api",
            category=FailureCategory.SOURCE_UPSTREAM,
            error="api_failed",
        )
        http_fallback = Mock(
            side_effect=AssertionError("HTTP fallback must not run")
        )
        playwright_package = types.ModuleType("playwright")
        playwright_api = types.ModuleType("playwright.sync_api")
        playwright_api.TimeoutError = TimeoutError
        playwright_api.sync_playwright = lambda: FakePlaywright()
        playwright_package.sync_api = playwright_api

        with (
            patch.dict(
                sys.modules,
                {
                    "playwright": playwright_package,
                    "playwright.sync_api": playwright_api,
                },
            ),
            patch.object(
                crawler,
                "get_browser_launcher",
                return_value=FakeLauncher(403),
            ),
            patch.object(
                crawler,
                "resolve_public_network_address_info",
                return_value=(
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("93.184.216.34", 443),
                    ),
                ),
            ),
            patch.object(
                crawler,
                "crawl_top_items_http_result",
                http_fallback,
            ),
        ):
            result = crawler.crawl_top_items_playwright_result(
                SOURCE,
                True,
                0,
                set(),
                False,
                original,
            )

        self.assertFalse(result.write_safe)
        self.assertEqual(result.method, "fallback_playwright")
        self.assertEqual(result.category, FailureCategory.SECURITY_POLICY)
        self.assertEqual(result.error, "fallback_browser_http_403")
        self.assertEqual(http_fallback.call_count, 0)

    def test_playwright_internal_type_error_is_not_classified_as_network(self):
        original = SourceCrawlResult(
            source=SOURCE,
            status=SourceStatus.FAILED,
            method="api",
            category=FailureCategory.SOURCE_UPSTREAM,
            error="api_failed",
        )
        launcher = FakeLauncher(200)
        launcher.browser.new_context = Mock(
            side_effect=TypeError("browser contract regression")
        )
        playwright_package = types.ModuleType("playwright")
        playwright_api = types.ModuleType("playwright.sync_api")
        playwright_api.Error = RuntimeError
        playwright_api.TimeoutError = TimeoutError
        playwright_api.sync_playwright = lambda: FakePlaywright()
        playwright_package.sync_api = playwright_api

        with (
            patch.dict(
                sys.modules,
                {
                    "playwright": playwright_package,
                    "playwright.sync_api": playwright_api,
                },
            ),
            patch.object(
                crawler,
                "get_browser_launcher",
                return_value=launcher,
            ),
            patch.object(
                crawler,
                "resolve_public_network_address_info",
                return_value=(
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("93.184.216.34", 443),
                    ),
                ),
            ),
            patch.object(
                crawler,
                "crawl_top_items_http_result",
                side_effect=AssertionError("HTTP fallback must not run"),
            ),
            self.assertRaisesRegex(
                TypeError,
                "browser contract regression",
            ),
        ):
            crawler.crawl_top_items_playwright_result(
                SOURCE,
                True,
                0,
                set(),
                False,
                original,
            )

    def test_http_200_waf_is_security_failure_for_list_and_detail(self):
        challenge = (
            b"<html><title>Access Denied</title>"
            b"<body>Cloudflare captcha</body></html>"
        )
        detail_url = (
            "https://www.sogang.ac.kr/ko/detail/"
            "1001?bbsConfigFk=141"
        )

        def fetch_result(url, label):
            return SiteFetchResult(
                ok=True,
                status_code=200,
                body=challenge,
                content_type="text/html",
                final_url=url,
            )

        with patch.object(
            crawler,
            "fetch_site_result",
            side_effect=fetch_result,
        ):
            page = crawler.fetch_fallback_http_page(SOURCE, 1)
            detail = crawler.fetch_fallback_http_detail(
                SOURCE,
                {
                    "url": detail_url,
                    "detail_url": detail_url,
                },
                1,
            )

        self.assertFalse(page.ok)
        self.assertEqual(page.category, FailureCategory.SECURITY_POLICY)
        self.assertEqual(page.error, "fallback_http_access_challenge")
        self.assertFalse(detail.ok)
        self.assertEqual(
            detail.category,
            FailureCategory.SECURITY_POLICY,
        )
        self.assertEqual(
            detail.error,
            "fallback_http_access_challenge",
        )

    def test_total_none_multi_page_grants_coverage_not_atomic_snapshot(self):
        pages = {
            1: api_page([api_entry("1003"), api_entry("1002")]),
            2: api_page([api_entry("1001")]),
            3: api_page([], terminal_verified=True),
        }

        with (
            patch.object(
                crawler,
                "fetch_bbs_list_result",
                side_effect=lambda page, *args, **kwargs: pages[page],
            ),
            patch.object(
                crawler,
                "fetch_bbs_detail",
                side_effect=lambda notice_id, **kwargs: api_detail(
                    notice_id
                ),
            ),
            patch.object(
                crawler,
                "get_detail_html_fallback_reason",
                return_value=None,
            ),
            patch.object(
                crawler,
                "extract_body_blocks_from_html",
                return_value=BODY,
            ),
        ):
            result = crawler.crawl_top_items_api_result(
                SOURCE,
                include_non_top=True,
                non_top_max_pages=0,
                reconcile_mode=True,
            )

        report = validate_crawl_report(
            CrawlReport([result]),
            fresh_state(),
            full_reconcile=True,
        )
        atomic_issues = [
            issue
            for issue in report.issues
            if issue.code == "atomic_snapshot_unavailable"
        ]

        self.assertTrue(result.write_safe)
        self.assertEqual(result.observed_count, 3)
        self.assertTrue(result.coverage_complete)
        self.assertFalse(result.reconcile_complete)
        self.assertFalse(result.full_snapshot)
        self.assertEqual(len(atomic_issues), 1)
        self.assertFalse(atomic_issues[0].fatal)
        self.assertTrue(report.write_safe)

    def test_backfill_window_is_write_safe_without_complete_coverage(self):
        pages = {
            1: api_page([api_entry("1002"), api_entry("1001")]),
        }

        with (
            patch.dict(
                os.environ,
                {"BACKFILL_DETAIL_LIMIT": "1"},
            ),
            patch.object(
                crawler,
                "fetch_bbs_list_result",
                side_effect=lambda page, *args, **kwargs: pages[page],
            ),
            patch.object(
                crawler,
                "fetch_bbs_detail",
                side_effect=lambda notice_id, **kwargs: api_detail(
                    notice_id
                ),
            ),
            patch.object(
                crawler,
                "get_detail_html_fallback_reason",
                return_value=None,
            ),
            patch.object(
                crawler,
                "extract_body_blocks_from_html",
                return_value=BODY,
            ),
            self.assertLogs(crawler.LOGGER, level="INFO") as logs,
        ):
            result = crawler.crawl_top_items_api_result(
                SOURCE,
                include_non_top=True,
                non_top_max_pages=0,
                reconcile_mode=True,
            )

        state = fresh_state()
        report = validate_crawl_report(
            CrawlReport([result]),
            state,
            full_reconcile=True,
        )
        update_state_from_report(
            state,
            report,
            True,
            {"141"},
        )

        self.assertTrue(result.write_safe)
        self.assertEqual(result.observed_count, 2)
        self.assertEqual(result.termination_reason, "backfill_window")
        self.assertFalse(result.coverage_complete)
        self.assertFalse(result.reconcile_complete)
        self.assertFalse(result.full_snapshot)
        self.assertTrue(state["sources"]["141"]["backfill_active"])
        self.assertIsNone(state["last_coverage_reconcile_at"])
        self.assertTrue(
            any(
                "상세 수집 시작(API): 출처=141, 페이지=1, "
                "항목=1/2, 공지=1002" in message
                for message in logs.output
            )
        )

    def crawl_resumed_burst(self, total_count: int | None):
        known_ids = {
            "106",
            "105",
            "104",
            "103",
            "102",
            "101",
            "100",
            "99",
        }
        pages = {
            1: api_page(
                [api_entry("110"), api_entry("109")],
                total_count=total_count,
            ),
            2: api_page(
                [api_entry("108"), api_entry("107")],
                total_count=total_count,
            ),
            3: api_page(
                [api_entry("106"), api_entry("105")],
                total_count=total_count,
            ),
            4: api_page(
                [api_entry("104"), api_entry("103")],
                total_count=total_count,
            ),
            5: api_page(
                [api_entry("102"), api_entry("101")],
                total_count=total_count,
            ),
            6: api_page(
                [api_entry("100"), api_entry("99")],
                total_count=total_count,
            ),
            7: api_page(
                [],
                terminal_verified=True,
                total_count=total_count,
            ),
        }
        page_calls = []

        def fetch_page(page, *args, **kwargs):
            page_calls.append(page)
            return pages[page]

        with (
            patch.object(
                crawler,
                "fetch_bbs_list_result",
                side_effect=fetch_page,
            ),
            patch.object(
                crawler,
                "fetch_bbs_detail",
                side_effect=lambda notice_id, **kwargs: api_detail(
                    notice_id
                ),
            ),
            patch.object(
                crawler,
                "get_detail_html_fallback_reason",
                return_value=None,
            ),
            patch.object(
                crawler,
                "extract_body_blocks_from_html",
                return_value=BODY,
            ),
        ):
            result = crawler.crawl_top_items_api_result(
                SOURCE,
                include_non_top=True,
                non_top_max_pages=0,
                known_ids=known_ids,
                incremental=True,
                reconcile_mode=True,
                resume_page=6,
                resume_anchor_ids={"102", "101"},
            )
        return result, page_calls

    def test_resume_scans_new_burst_beyond_first_page_before_jump(self):
        result, page_calls = self.crawl_resumed_burst(None)

        self.assertTrue(result.write_safe)
        self.assertIn(2, page_calls)
        self.assertEqual(
            [
                crawler.extract_detail_id_from_text(item["url"])
                for item in result.items
            ],
            ["110", "109", "108", "107"],
        )

    def test_resume_with_total_count_reaches_natural_end(self):
        result, page_calls = self.crawl_resumed_burst(12)

        self.assertTrue(result.write_safe)
        self.assertIn(2, page_calls)
        self.assertEqual(result.error, "")
        self.assertEqual(result.termination_reason, "natural_end")
        self.assertEqual(result.observed_count, 12)

    def test_resume_search_window_tracks_large_new_prefix_shift(self):
        new_ids = [str(2000 - index) for index in range(81)]
        prefix_known_ids = [str(1500 - index) for index in range(19)]
        deep_known_ids = [
            str(10000 + page * 100 + offset)
            for page in range(98, 105)
            for offset in range(20)
        ]
        anchor_ids = {
            str(10000 + 104 * 100),
            str(10000 + 104 * 100 + 1),
        }
        known_ids = set(prefix_known_ids) | set(deep_known_ids)
        pages = {
            page: api_page(
                [
                    api_entry(notice_id)
                    for notice_id in new_ids[
                        (page - 1) * 20 : page * 20
                    ]
                ]
            )
            for page in range(1, 5)
        }
        pages[5] = api_page(
            [
                api_entry(new_ids[80]),
                *[
                    api_entry(notice_id)
                    for notice_id in prefix_known_ids
                ],
            ]
        )
        for page in range(98, 105):
            pages[page] = api_page(
                [
                    api_entry(
                        str(10000 + page * 100 + offset)
                    )
                    for offset in range(20)
                ]
            )
        pages[105] = api_page([], terminal_verified=True)
        page_calls = []

        def fetch_page(page, *args, **kwargs):
            page_calls.append(page)
            return pages[page]

        with (
            patch.dict(
                os.environ,
                {
                    "API_MAX_REQUESTS": "500",
                    "BBS_PAGE_SIZE": "20",
                    "CRAWL_HARD_PAGE_LIMIT": "100",
                },
            ),
            patch.object(
                crawler,
                "fetch_bbs_list_result",
                side_effect=fetch_page,
            ),
            patch.object(
                crawler,
                "fetch_bbs_detail",
                side_effect=lambda notice_id, **kwargs: api_detail(
                    notice_id
                ),
            ),
            patch.object(
                crawler,
                "get_detail_html_fallback_reason",
                return_value=None,
            ),
            patch.object(
                crawler,
                "extract_body_blocks_from_html",
                return_value=BODY,
            ),
        ):
            result = crawler.crawl_top_items_api_result(
                SOURCE,
                include_non_top=True,
                non_top_max_pages=0,
                known_ids=known_ids,
                incremental=True,
                reconcile_mode=True,
                resume_page=100,
                resume_anchor_ids=anchor_ids,
            )

        self.assertTrue(
            result.write_safe,
            (
                result.status,
                result.error,
                result.termination_reason,
                page_calls,
            ),
        )
        self.assertEqual(result.termination_reason, "natural_end")
        self.assertIn(104, page_calls)
        self.assertEqual(len(result.items), 81)

    def test_offset_shift_without_total_never_becomes_atomic_snapshot(self):
        shifted_pages = {
            1: api_page([api_entry("5"), api_entry("4")]),
            2: api_page([api_entry("2"), api_entry("1")]),
            3: api_page([], terminal_verified=True),
        }

        with (
            patch.object(
                crawler,
                "fetch_bbs_list_result",
                side_effect=lambda page, *args, **kwargs: (
                    shifted_pages[page]
                ),
            ),
            patch.object(
                crawler,
                "fetch_bbs_detail",
                side_effect=lambda notice_id, **kwargs: api_detail(
                    notice_id
                ),
            ),
            patch.object(
                crawler,
                "get_detail_html_fallback_reason",
                return_value=None,
            ),
            patch.object(
                crawler,
                "extract_body_blocks_from_html",
                return_value=BODY,
            ),
        ):
            shifted = crawler.crawl_top_items_api_result(
                SOURCE,
                include_non_top=True,
                non_top_max_pages=0,
                reconcile_mode=True,
            )

        state = fresh_state()
        state["sources"]["141"] = {
            "observed_ids": ["5", "4", "3", "2", "1"],
        }
        report = validate_crawl_report(
            CrawlReport([shifted]),
            state,
            full_reconcile=True,
        )
        update_state_from_report(
            state,
            report,
            True,
            {"141"},
        )

        self.assertTrue(shifted.write_safe)
        self.assertTrue(shifted.coverage_complete)
        self.assertFalse(shifted.full_snapshot)
        self.assertFalse(shifted.reconcile_complete)
        self.assertIn(
            "3",
            state["sources"]["141"]["observed_ids"],
        )

        stable_pages = {
            1: api_page([api_entry("5"), api_entry("4")]),
            2: api_page([api_entry("3"), api_entry("2")]),
            3: api_page([api_entry("1")]),
            4: api_page([], terminal_verified=True),
        }

        with (
            patch.object(
                crawler,
                "fetch_bbs_list_result",
                side_effect=lambda page, *args, **kwargs: (
                    stable_pages[page]
                ),
            ),
            patch.object(
                crawler,
                "fetch_bbs_detail",
                side_effect=lambda notice_id, **kwargs: api_detail(
                    notice_id
                ),
            ),
            patch.object(
                crawler,
                "get_detail_html_fallback_reason",
                return_value=None,
            ),
            patch.object(
                crawler,
                "extract_body_blocks_from_html",
                return_value=BODY,
            ),
        ):
            recovered = crawler.crawl_top_items_api_result(
                SOURCE,
                include_non_top=True,
                non_top_max_pages=0,
                known_ids={"5", "4", "2", "1"},
                incremental=True,
            )

        self.assertTrue(recovered.write_safe)
        self.assertEqual(
            [
                crawler.extract_detail_id_from_text(item["url"])
                for item in recovered.items
            ],
            ["3"],
        )

    def test_targeted_refresh_skips_incomplete_backfill_history(self):
        known_ids = {"1009", "1008", "1007", "1006", "1003"}
        pages = {
            1: api_page(
                [api_entry("1010"), api_entry("1009")],
                total_count=10,
            ),
            2: api_page(
                [api_entry("1008"), api_entry("1007")],
                total_count=10,
            ),
            3: api_page(
                [api_entry("1006"), api_entry("1005")],
                total_count=10,
            ),
            4: api_page(
                [
                    api_entry("1004"),
                    api_entry("1003"),
                    api_entry("1002"),
                ],
                total_count=10,
            ),
        }
        page_calls = []
        detail_calls = []

        def fetch_page(page, *args, **kwargs):
            page_calls.append(page)
            if page not in pages:
                raise AssertionError(f"unexpected page:{page}")
            return pages[page]

        def fetch_detail(notice_id, **kwargs):
            detail_calls.append(notice_id)
            return api_detail(notice_id)

        with (
            patch.object(
                crawler,
                "fetch_bbs_list_result",
                side_effect=fetch_page,
            ),
            patch.object(
                crawler,
                "fetch_bbs_detail",
                side_effect=fetch_detail,
            ),
            patch.object(
                crawler,
                "get_detail_html_fallback_reason",
                return_value=None,
            ),
            patch.object(
                crawler,
                "extract_body_blocks_from_html",
                return_value=BODY,
            ),
        ):
            result = crawler.crawl_top_items_api_result(
                SOURCE,
                include_non_top=True,
                non_top_max_pages=0,
                known_ids=known_ids,
                incremental=True,
                refresh_known_ids={"1003"},
                targeted_refresh_ids={"1003"},
            )

        self.assertTrue(result.write_safe)
        self.assertEqual(
            result.termination_reason,
            "incremental_checkpoint",
        )
        self.assertNotIn(5, page_calls)
        self.assertEqual(set(detail_calls), {"1010", "1003"})
        self.assertEqual(
            [
                crawler.extract_detail_id_from_text(item["url"])
                for item in result.items
            ],
            ["1010", "1003"],
        )
        self.assertTrue(
            {"1005", "1004", "1002"}.isdisjoint(
                result.observed_ids
            )
        )

    def test_targeted_refresh_missing_is_not_write_safe(self):
        pages = {
            1: api_page([api_entry("1010"), api_entry("1009")]),
            2: api_page([], terminal_verified=True),
        }

        with (
            patch.object(
                crawler,
                "fetch_bbs_list_result",
                side_effect=lambda page, *args, **kwargs: pages[page],
            ),
            patch.object(
                crawler,
                "fetch_bbs_detail",
                side_effect=lambda notice_id, **kwargs: api_detail(
                    notice_id
                ),
            ),
            patch.object(
                crawler,
                "get_detail_html_fallback_reason",
                return_value=None,
            ),
            patch.object(
                crawler,
                "extract_body_blocks_from_html",
                return_value=BODY,
            ),
        ):
            result = crawler.crawl_top_items_api_result(
                SOURCE,
                include_non_top=True,
                non_top_max_pages=0,
                known_ids={"1009", "1003"},
                incremental=True,
                refresh_known_ids={"1003"},
                targeted_refresh_ids={"1003"},
            )

        self.assertFalse(result.write_safe)
        self.assertEqual(result.status, SourceStatus.DEGRADED)
        self.assertEqual(
            result.termination_reason,
            "targeted_refresh_missing",
        )
        self.assertEqual(result.error, "targeted_refresh_missing:1003")

    def test_incremental_checkpoint_stops_after_verified_overlap(self):
        known_ids = {"1004", "1003", "1002", "1001", "1000"}
        pages = {
            1: api_page([api_entry("1005"), api_entry("1004")]),
            2: api_page([api_entry("1003"), api_entry("1002")]),
            3: api_page([api_entry("1001"), api_entry("1000")]),
        }
        page_calls = []

        def fetch_page(page, *args, **kwargs):
            page_calls.append(page)
            if page not in pages:
                raise AssertionError(f"unexpected page:{page}")
            return pages[page]

        with (
            patch.dict(
                os.environ,
                {"INCREMENTAL_CHECKPOINT_OVERLAP_PAGES": "2"},
            ),
            patch.object(
                crawler,
                "fetch_bbs_list_result",
                side_effect=fetch_page,
            ),
            patch.object(
                crawler,
                "fetch_bbs_detail",
                side_effect=lambda notice_id, **kwargs: api_detail(
                    notice_id
                ),
            ),
            patch.object(
                crawler,
                "get_detail_html_fallback_reason",
                return_value=None,
            ),
            patch.object(
                crawler,
                "extract_body_blocks_from_html",
                return_value=BODY,
            ),
        ):
            result = crawler.crawl_top_items_api_result(
                SOURCE,
                include_non_top=True,
                non_top_max_pages=0,
                known_ids=known_ids,
                incremental=True,
            )

        self.assertTrue(result.write_safe)
        self.assertEqual(
            result.termination_reason,
            "incremental_checkpoint",
        )
        self.assertEqual(result.pages_scanned, 3)
        self.assertNotIn(4, page_calls)
        self.assertEqual(
            [
                crawler.extract_detail_id_from_text(item["url"])
                for item in result.items
            ],
            ["1005"],
        )

    def test_refresh_policy_scans_full_list_and_refreshes_changed_history(self):
        known_ids = {"1004", "1003", "1002", "1001", "1000", "900"}
        changed_entry = api_entry("900")
        changed_entry["title"] = "수정된 과거 공지"
        changed_entry["regDate"] = "20250101090000"
        pages = {
            1: api_page([api_entry("1005"), api_entry("1004")]),
            2: api_page([api_entry("1003"), api_entry("1002")]),
            3: api_page([api_entry("1001"), api_entry("1000")]),
            4: api_page([api_entry("800"), changed_entry]),
            5: api_page([], terminal_verified=True),
        }
        last_detail_at = datetime.now(timezone.utc).isoformat()
        notice_refresh_state = {}
        for notice_id in known_ids - {"900"}:
            observation = crawler.build_notice_observation(
                notice_id,
                f"공지 {notice_id}",
                DATE,
                False,
            )
            notice_refresh_state[notice_id] = {
                **observation,
                "last_detail_at": last_detail_at,
            }
        notice_refresh_state["900"] = {
            **crawler.build_notice_observation(
                "900",
                "이전 과거 공지",
                "20250101090000",
                False,
            ),
            "last_detail_at": last_detail_at,
        }
        detail_calls = []

        def fetch_detail(notice_id, **kwargs):
            detail_calls.append(notice_id)
            if notice_id == "900":
                return {
                    **api_detail(notice_id),
                    "title": "수정된 과거 공지",
                    "regDate": "20250101090000",
                }
            return api_detail(notice_id)

        with (
            patch.dict(
                os.environ,
                {"INCREMENTAL_CHECKPOINT_OVERLAP_PAGES": "2"},
            ),
            patch.object(
                crawler,
                "fetch_bbs_list_result",
                side_effect=lambda page, *args, **kwargs: pages[page],
            ),
            patch.object(
                crawler,
                "fetch_bbs_detail",
                side_effect=fetch_detail,
            ),
            patch.object(
                crawler,
                "get_detail_html_fallback_reason",
                return_value=None,
            ),
            patch.object(
                crawler,
                "extract_body_blocks_from_html",
                return_value=BODY,
            ),
        ):
            result = crawler.crawl_top_items_api_result(
                SOURCE,
                include_non_top=True,
                non_top_max_pages=0,
                known_ids=known_ids,
                incremental=True,
                source_state={
                    "notice_refresh_state": notice_refresh_state,
                },
            )

        self.assertTrue(result.write_safe, result.to_dict(include_items=True))
        self.assertTrue(result.notice_index_complete)
        self.assertEqual(result.termination_reason, "natural_end")
        self.assertEqual(set(detail_calls), {"1005", "900"})
        self.assertNotIn("800", result.observed_ids)
        self.assertIn("800", result.notice_observations)
        self.assertEqual(
            set(result.detailed_notice_ids),
            {"1005", "900"},
        )

    def test_scheduled_refresh_does_not_consume_backfill_detail_limit(self):
        observation = crawler.build_notice_observation(
            "1006",
            "공지 1006",
            DATE,
            False,
        )
        pages = {
            1: api_page([api_entry("1006"), api_entry("1005")]),
            2: api_page([api_entry("1004")]),
            3: api_page([], terminal_verified=True),
        }
        detail_calls = []

        def fetch_detail(notice_id, **kwargs):
            detail_calls.append(notice_id)
            return api_detail(notice_id)

        with (
            patch.dict(os.environ, {"BACKFILL_DETAIL_LIMIT": "1"}),
            patch.object(
                crawler,
                "fetch_bbs_list_result",
                side_effect=lambda page, *args, **kwargs: pages[page],
            ),
            patch.object(
                crawler,
                "fetch_bbs_detail",
                side_effect=fetch_detail,
            ),
            patch.object(
                crawler,
                "get_detail_html_fallback_reason",
                return_value=None,
            ),
            patch.object(
                crawler,
                "extract_body_blocks_from_html",
                return_value=BODY,
            ),
        ):
            result = crawler.crawl_top_items_api_result(
                SOURCE,
                include_non_top=True,
                non_top_max_pages=0,
                known_ids={"1006"},
                incremental=True,
                reconcile_mode=True,
                source_state={
                    "notice_refresh_state": {
                        "1006": {
                            **observation,
                            "last_detail_at": (
                                "2020-01-01T00:00:00+00:00"
                            ),
                        }
                    }
                },
            )

        self.assertTrue(result.write_safe, result.to_dict(include_items=True))
        self.assertEqual(result.termination_reason, "backfill_window")
        self.assertTrue(result.notice_index_complete)
        self.assertEqual(set(detail_calls), {"1006", "1005"})
        self.assertNotIn("1004", result.observed_ids)
        self.assertIn("1004", result.notice_observations)

    def test_refresh_policy_caps_unknown_details_inside_large_page(self):
        entries = [api_entry(str(2000 - index)) for index in range(8)]
        pages = {
            1: api_page(entries),
            2: api_page([], terminal_verified=True),
        }
        detail_calls = []

        def fetch_detail(notice_id, **kwargs):
            detail_calls.append(notice_id)
            return api_detail(notice_id)

        with (
            patch.dict(os.environ, {"BACKFILL_DETAIL_LIMIT": "2"}),
            patch.object(
                crawler,
                "fetch_bbs_list_result",
                side_effect=lambda page, *args, **kwargs: pages[page],
            ),
            patch.object(
                crawler,
                "fetch_bbs_detail",
                side_effect=fetch_detail,
            ),
            patch.object(
                crawler,
                "get_detail_html_fallback_reason",
                return_value=None,
            ),
            patch.object(
                crawler,
                "extract_body_blocks_from_html",
                return_value=BODY,
            ),
        ):
            result = crawler.crawl_top_items_api_result(
                SOURCE,
                include_non_top=True,
                non_top_max_pages=0,
                known_ids={"1000"},
                incremental=True,
                source_state={},
            )

        self.assertTrue(result.write_safe, result.to_dict(include_items=True))
        self.assertEqual(set(detail_calls), {"2000", "1999"})
        self.assertEqual(detail_calls.count("2000"), 2)
        self.assertEqual(detail_calls.count("1999"), 2)
        self.assertEqual(result.termination_reason, "backfill_window")
        self.assertEqual(result.backfill_resume_page, 1)
        self.assertTrue(result.notice_index_complete)
        self.assertEqual(len(result.notice_observations), len(entries))
        self.assertEqual(result.observed_ids, ["2000", "1999"])

    def test_refresh_policy_closes_incremental_detail_window_at_checkpoint(self):
        entries = [
            api_entry("2000"),
            api_entry("1000"),
            api_entry("1999"),
            api_entry("1998"),
        ]
        pages = {
            1: api_page(entries),
            2: api_page([], terminal_verified=True),
        }
        known_observation = crawler.build_notice_observation(
            "1000",
            "공지 1000",
            DATE,
            False,
        )
        detail_calls = []

        def fetch_detail(notice_id, **kwargs):
            detail_calls.append(notice_id)
            return api_detail(notice_id)

        with (
            patch.object(
                crawler,
                "fetch_bbs_list_result",
                side_effect=lambda page, *args, **kwargs: pages[page],
            ),
            patch.object(
                crawler,
                "fetch_bbs_detail",
                side_effect=fetch_detail,
            ),
            patch.object(
                crawler,
                "get_detail_html_fallback_reason",
                return_value=None,
            ),
            patch.object(
                crawler,
                "extract_body_blocks_from_html",
                return_value=BODY,
            ),
        ):
            result = crawler.crawl_top_items_api_result(
                SOURCE,
                include_non_top=True,
                non_top_max_pages=0,
                known_ids={"1000"},
                incremental=True,
                source_state={
                    "notice_refresh_state": {
                        "1000": {
                            **known_observation,
                            "last_detail_at": datetime.now(
                                timezone.utc
                            ).isoformat(),
                        }
                    }
                },
            )

        self.assertTrue(result.write_safe, result.to_dict(include_items=True))
        self.assertEqual(set(detail_calls), {"2000"})
        self.assertEqual(result.detailed_notice_ids, ["2000"])
        self.assertNotIn("1999", result.observed_ids)
        self.assertNotIn("1998", result.observed_ids)
        self.assertIn("1999", result.notice_observations)
        self.assertIn("1998", result.notice_observations)

    def test_refresh_policy_accepts_total_that_includes_first_page_top(self):
        pages = {
            1: api_page(
                [api_entry("9000", top=True), api_entry("1000")],
                total_count=3,
            ),
            2: api_page(
                [api_entry("999")],
                terminal_verified=True,
                total_count=3,
            ),
        }
        page_calls = []

        def fetch_page(page, *args, **kwargs):
            page_calls.append(page)
            return pages[page]

        with (
            patch.object(
                crawler,
                "fetch_bbs_list_result",
                side_effect=fetch_page,
            ),
            patch.object(
                crawler,
                "fetch_bbs_detail",
                side_effect=lambda notice_id, **kwargs: api_detail(
                    notice_id
                ),
            ),
            patch.object(
                crawler,
                "get_detail_html_fallback_reason",
                return_value=None,
            ),
            patch.object(
                crawler,
                "extract_body_blocks_from_html",
                return_value=BODY,
            ),
        ):
            result = crawler.crawl_top_items_api_result(
                SOURCE,
                include_non_top=True,
                non_top_max_pages=0,
                incremental=False,
                source_state={},
            )
            for page in pages.values():
                page.total_count = 4
            mismatch = crawler.crawl_top_items_api_result(
                SOURCE,
                include_non_top=True,
                non_top_max_pages=0,
                incremental=False,
                source_state={},
            )

        self.assertTrue(result.write_safe, result.to_dict(include_items=True))
        self.assertTrue(result.notice_index_complete)
        self.assertEqual(len(result.notice_observations), 3)
        self.assertFalse(mismatch.write_safe)
        self.assertEqual(mismatch.error, "pagination_total_mismatch")
        self.assertEqual(page_calls, [1, 2, 1, 2, 1, 2])

    def test_refresh_policy_finds_shifted_resume_anchor_during_full_sweep(self):
        known_ids = {"1000", "900"}
        last_detail_at = datetime.now(timezone.utc).isoformat()
        notice_refresh_state = {
            notice_id: {
                **crawler.build_notice_observation(
                    notice_id,
                    f"공지 {notice_id}",
                    DATE,
                    False,
                ),
                "last_detail_at": last_detail_at,
            }
            for notice_id in known_ids
        }
        pages = {
            1: api_page([api_entry("1000")]),
            2: api_page([api_entry("999")]),
            3: api_page([api_entry("998")]),
            4: api_page([api_entry("997")]),
            5: api_page([api_entry("996")]),
            6: api_page([api_entry("900"), api_entry("895")]),
            7: api_page([], terminal_verified=True),
        }

        with (
            patch.object(
                crawler,
                "fetch_bbs_list_result",
                side_effect=lambda page, *args, **kwargs: pages[page],
            ),
            patch.object(
                crawler,
                "fetch_bbs_detail",
                side_effect=lambda notice_id, **kwargs: api_detail(notice_id),
            ),
            patch.object(
                crawler,
                "get_detail_html_fallback_reason",
                return_value=None,
            ),
            patch.object(
                crawler,
                "extract_body_blocks_from_html",
                return_value=BODY,
            ),
        ):
            result = crawler.crawl_top_items_api_result(
                SOURCE,
                include_non_top=True,
                non_top_max_pages=0,
                known_ids=known_ids,
                incremental=True,
                reconcile_mode=True,
                resume_page=3,
                resume_anchor_ids={"900"},
                source_state={
                    "notice_refresh_state": notice_refresh_state,
                },
            )

        self.assertTrue(result.write_safe, result.to_dict(include_items=True))
        self.assertTrue(result.notice_index_complete)
        self.assertEqual(
            [
                crawler.extract_detail_id_from_text(item["url"])
                for item in result.items
            ],
            ["895"],
        )
        self.assertTrue(
            {"999", "998", "997", "996"}.isdisjoint(
                result.observed_ids
            )
        )

    def test_repeated_new_pinned_top_does_not_dirty_api_overlap(self):
        pinned = api_entry("9000", top=True)
        known_ids = {"1004", "1003", "1002", "1001", "1000"}
        pages = {
            1: api_page([pinned, api_entry("1004")]),
            2: api_page(
                [pinned, api_entry("1003"), api_entry("1002")]
            ),
            3: api_page(
                [pinned, api_entry("1001"), api_entry("1000")]
            ),
        }
        page_calls = []

        def fetch_page(page, *args, **kwargs):
            page_calls.append(page)
            if page not in pages:
                raise AssertionError(f"unexpected page:{page}")
            return pages[page]

        with (
            patch.dict(
                os.environ,
                {"INCREMENTAL_CHECKPOINT_OVERLAP_PAGES": "2"},
            ),
            patch.object(
                crawler,
                "fetch_bbs_list_result",
                side_effect=fetch_page,
            ),
            patch.object(
                crawler,
                "fetch_bbs_detail",
                side_effect=lambda notice_id, **kwargs: api_detail(
                    notice_id
                ),
            ),
            patch.object(
                crawler,
                "get_detail_html_fallback_reason",
                return_value=None,
            ),
            patch.object(
                crawler,
                "extract_body_blocks_from_html",
                return_value=BODY,
            ),
        ):
            result = crawler.crawl_top_items_api_result(
                SOURCE,
                include_non_top=True,
                non_top_max_pages=0,
                known_ids=known_ids,
                incremental=True,
            )

        self.assertTrue(result.write_safe)
        self.assertEqual(
            result.termination_reason,
            "incremental_checkpoint",
        )
        self.assertNotIn(4, page_calls)
        self.assertEqual(
            [
                crawler.extract_detail_id_from_text(item["url"])
                for item in result.items
            ],
            ["9000"],
        )

    def test_known_pinned_top_ids_do_not_satisfy_non_top_total(self):
        pinned_one = api_entry("9000", top=True)
        pinned_two = api_entry("9001", top=True)
        known_ids = {
            "9000",
            "9001",
            "1005",
            "1004",
            "1003",
            "1002",
        }
        pages = {
            1: api_page(
                [
                    pinned_one,
                    pinned_two,
                    api_entry("1005"),
                    api_entry("1004"),
                ],
                total_count=6,
            ),
            2: api_page(
                [pinned_one, pinned_two, api_entry("1003")],
                total_count=6,
            ),
            3: api_page(
                [pinned_one, pinned_two, api_entry("1002")],
                total_count=6,
            ),
            4: api_page(
                [pinned_one, pinned_two, api_entry("1001"), api_entry("1000")],
                total_count=6,
            ),
            5: api_page(
                [],
                terminal_verified=True,
                total_count=6,
            ),
        }
        page_calls = []

        def fetch_page(page, *args, **kwargs):
            page_calls.append(page)
            return pages[page]

        with (
            patch.dict(
                os.environ,
                {
                    "BBS_PAGE_SIZE": "2",
                    "INCREMENTAL_CHECKPOINT_OVERLAP_PAGES": "2",
                },
            ),
            patch.object(
                crawler,
                "fetch_bbs_list_result",
                side_effect=fetch_page,
            ),
            patch.object(
                crawler,
                "fetch_bbs_detail",
                side_effect=lambda notice_id, **kwargs: api_detail(
                    notice_id
                ),
            ),
            patch.object(
                crawler,
                "get_detail_html_fallback_reason",
                return_value=None,
            ),
            patch.object(
                crawler,
                "extract_body_blocks_from_html",
                return_value=BODY,
            ),
        ):
            result = crawler.crawl_top_items_api_result(
                SOURCE,
                include_non_top=True,
                non_top_max_pages=0,
                known_ids=known_ids,
                incremental=True,
            )

        self.assertTrue(result.write_safe)
        self.assertEqual(result.termination_reason, "natural_end")
        self.assertIn(4, page_calls)
        self.assertEqual(
            [
                crawler.extract_detail_id_from_text(item["url"])
                for item in result.items
            ],
            ["1001", "1000"],
        )

    def test_fallback_incremental_checkpoint_stops_after_verified_overlap(
        self,
    ):
        known_ids = {"1004", "1003", "1002", "1001", "1000"}
        date = "2026-07-27T12:00:00+09:00"

        def entry(notice_id):
            return {
                "title": f"공지 {notice_id}",
                "date": date,
                "top": False,
                "url": (
                    "https://www.sogang.ac.kr/ko/detail/"
                    f"{notice_id}?bbsConfigFk=141"
                ),
            }

        def page(number, notice_ids):
            values = [entry(notice_id) for notice_id in notice_ids]
            return FallbackPageResult(
                ok=True,
                requested_page=number,
                effective_page=number,
                source_config_fk="141",
                entries=values,
                final_url=f"{SOURCE.list_url}?page={number}",
                contract_verified=True,
                raw_entry_count=len(values),
            )

        pages = {
            1: page(1, ["1005", "1004"]),
            2: page(2, ["1003", "1002"]),
            3: page(3, ["1001", "1000"]),
        }
        page_calls = []

        def fetch_page(number):
            page_calls.append(number)
            if number not in pages:
                raise AssertionError(f"unexpected page:{number}")
            return pages[number]

        def fetch_detail(item, number):
            notice_id = (
                crawler.extract_detail_id_from_text(item["url"]) or ""
            )
            return FallbackDetailResult(
                ok=True,
                notice_id=notice_id,
                url=item["url"],
                title=item["title"],
                date=date,
                body_blocks=BODY,
                body_status=crawler.BODY_STATUS_PRESENT,
                attachments=[
                    {
                        "name": "attachment.pdf",
                        "type": "external",
                        "external": {
                            "url": (
                                "https://www.sogang.ac.kr/"
                                "file-fe-prd/board/attachment.pdf"
                            )
                        },
                    }
                ],
                attachments_status=(
                    crawler.ATTACHMENTS_STATUS_KNOWN
                ),
            )

        original = SourceCrawlResult(
            source=SOURCE,
            status=SourceStatus.FAILED,
            method="api",
            category=FailureCategory.SOURCE_UPSTREAM,
            error="api_failed",
        )

        with patch.dict(
            os.environ,
            {"INCREMENTAL_CHECKPOINT_OVERLAP_PAGES": "2"},
        ):
            result = crawler.crawl_fallback_with_fetchers(
                SOURCE,
                True,
                0,
                known_ids,
                True,
                "fallback_http",
                original,
                fetch_page,
                fetch_detail,
            )

        self.assertTrue(result.write_safe)
        self.assertEqual(
            result.termination_reason,
            "incremental_checkpoint",
        )
        self.assertEqual(result.pages_scanned, 3)
        self.assertNotIn(4, page_calls)
        self.assertEqual(
            [item["notice_id"] for item in result.items],
            ["1005"],
        )

    def test_fallback_targeted_refresh_skips_backfill_history(self):
        known_ids = {"1009", "1008", "1007", "1006", "1003"}
        date = "2026-07-27T12:00:00+09:00"

        def entry(notice_id):
            return {
                "title": f"공지 {notice_id}",
                "date": date,
                "top": False,
                "url": (
                    "https://www.sogang.ac.kr/ko/detail/"
                    f"{notice_id}?bbsConfigFk=141"
                ),
            }

        def page(number, notice_ids):
            values = [entry(notice_id) for notice_id in notice_ids]
            return FallbackPageResult(
                ok=True,
                requested_page=number,
                effective_page=number,
                source_config_fk="141",
                entries=values,
                final_url=f"{SOURCE.list_url}?page={number}",
                contract_verified=True,
                raw_entry_count=len(values),
            )

        pages = {
            1: page(1, ["1010", "1009"]),
            2: page(2, ["1008", "1007"]),
            3: page(3, ["1006", "1005"]),
            4: page(4, ["1004", "1003", "1002"]),
        }
        page_calls = []
        detail_calls = []

        def fetch_page(number):
            page_calls.append(number)
            if number not in pages:
                raise AssertionError(f"unexpected page:{number}")
            return pages[number]

        def fetch_detail(item, _number):
            notice_id = (
                crawler.extract_detail_id_from_text(item["url"]) or ""
            )
            detail_calls.append(notice_id)
            return FallbackDetailResult(
                ok=True,
                notice_id=notice_id,
                url=item["url"],
                title=item["title"],
                date=date,
                body_blocks=BODY,
                body_status=crawler.BODY_STATUS_PRESENT,
                attachments=[
                    {
                        "name": "attachment.pdf",
                        "type": "external",
                        "external": {
                            "url": (
                                "https://www.sogang.ac.kr/"
                                "file-fe-prd/board/attachment.pdf"
                            )
                        },
                    }
                ],
                attachments_status=(
                    crawler.ATTACHMENTS_STATUS_KNOWN
                ),
            )

        original = SourceCrawlResult(
            source=SOURCE,
            status=SourceStatus.FAILED,
            method="api",
            category=FailureCategory.SOURCE_UPSTREAM,
            error="api_failed",
        )
        result = crawler.crawl_fallback_with_fetchers(
            SOURCE,
            True,
            0,
            known_ids,
            True,
            "fallback_http",
            original,
            fetch_page,
            fetch_detail,
            refresh_known_ids={"1003"},
            targeted_refresh_ids={"1003"},
        )

        self.assertTrue(
            result.write_safe,
            result.to_dict(include_items=True),
        )
        self.assertEqual(
            result.termination_reason,
            "incremental_checkpoint",
        )
        self.assertNotIn(5, page_calls)
        self.assertEqual(set(detail_calls), {"1010", "1003"})
        self.assertEqual(
            [item["notice_id"] for item in result.items],
            ["1010", "1003"],
        )
        self.assertTrue(
            {"1005", "1004", "1002"}.isdisjoint(
                result.observed_ids
            )
        )

    def test_fallback_targeted_refresh_missing_is_not_write_safe(self):
        date = "2026-07-27T12:00:00+09:00"

        def entry(notice_id):
            return {
                "title": f"공지 {notice_id}",
                "date": date,
                "top": False,
                "url": (
                    "https://www.sogang.ac.kr/ko/detail/"
                    f"{notice_id}?bbsConfigFk=141"
                ),
            }

        def page(number, notice_ids):
            values = [entry(notice_id) for notice_id in notice_ids]
            return FallbackPageResult(
                ok=True,
                requested_page=number,
                effective_page=number,
                source_config_fk="141",
                entries=values,
                final_url=f"{SOURCE.list_url}?page={number}",
                contract_verified=True,
                explicit_empty=not values,
                raw_entry_count=len(values),
            )

        pages = {
            1: page(1, ["1010", "1009"]),
            2: page(2, []),
        }

        def fetch_detail(item, _number):
            notice_id = (
                crawler.extract_detail_id_from_text(item["url"]) or ""
            )
            return FallbackDetailResult(
                ok=True,
                notice_id=notice_id,
                url=item["url"],
                title=item["title"],
                date=date,
                body_blocks=BODY,
                body_status=crawler.BODY_STATUS_PRESENT,
                attachments=[
                    {
                        "name": "attachment.pdf",
                        "type": "external",
                        "external": {
                            "url": (
                                "https://www.sogang.ac.kr/"
                                "file-fe-prd/board/attachment.pdf"
                            )
                        },
                    }
                ],
                attachments_status=crawler.ATTACHMENTS_STATUS_KNOWN,
            )

        original = SourceCrawlResult(
            source=SOURCE,
            status=SourceStatus.FAILED,
            method="api",
            category=FailureCategory.SOURCE_UPSTREAM,
            error="api_failed",
        )
        result = crawler.crawl_fallback_with_fetchers(
            SOURCE,
            True,
            0,
            {"1009", "1003"},
            True,
            "fallback_http",
            original,
            lambda number: pages[number],
            fetch_detail,
            refresh_known_ids={"1003"},
            targeted_refresh_ids={"1003"},
        )

        self.assertFalse(result.write_safe)
        self.assertEqual(result.status, SourceStatus.DEGRADED)
        self.assertEqual(
            result.termination_reason,
            "targeted_refresh_missing",
        )
        self.assertEqual(result.error, "targeted_refresh_missing:1003")

    def test_repeated_new_pinned_top_does_not_dirty_fallback_overlap(
        self,
    ):
        known_ids = {"1004", "1003", "1002", "1001", "1000"}
        date = "2026-07-27T12:00:00+09:00"

        def entry(notice_id, top=False):
            return {
                "title": f"공지 {notice_id}",
                "date": date,
                "top": top,
                "url": (
                    "https://www.sogang.ac.kr/ko/detail/"
                    f"{notice_id}?bbsConfigFk=141"
                ),
            }

        pinned = entry("9000", top=True)

        def page(number, entries):
            return FallbackPageResult(
                ok=True,
                requested_page=number,
                effective_page=number,
                source_config_fk="141",
                entries=list(entries),
                final_url=f"{SOURCE.list_url}?page={number}",
                contract_verified=True,
                raw_entry_count=len(entries),
            )

        pages = {
            1: page(1, [pinned, entry("1004")]),
            2: page(
                2,
                [pinned, entry("1003"), entry("1002")],
            ),
            3: page(
                3,
                [pinned, entry("1001"), entry("1000")],
            ),
        }
        page_calls = []

        def fetch_page(number):
            page_calls.append(number)
            if number not in pages:
                raise AssertionError(f"unexpected page:{number}")
            return pages[number]

        def fetch_detail(item, number):
            notice_id = (
                crawler.extract_detail_id_from_text(item["url"]) or ""
            )
            return FallbackDetailResult(
                ok=True,
                notice_id=notice_id,
                url=item["url"],
                title=item["title"],
                date=date,
                body_blocks=BODY,
                body_status=crawler.BODY_STATUS_PRESENT,
                attachments=[
                    {
                        "name": "attachment.pdf",
                        "type": "external",
                        "external": {
                            "url": (
                                "https://www.sogang.ac.kr/"
                                "file-fe-prd/board/attachment.pdf"
                            )
                        },
                    }
                ],
                attachments_status=(
                    crawler.ATTACHMENTS_STATUS_KNOWN
                ),
            )

        original = SourceCrawlResult(
            source=SOURCE,
            status=SourceStatus.FAILED,
            method="api",
            category=FailureCategory.SOURCE_UPSTREAM,
            error="api_failed",
        )

        with patch.dict(
            os.environ,
            {"INCREMENTAL_CHECKPOINT_OVERLAP_PAGES": "2"},
        ):
            result = crawler.crawl_fallback_with_fetchers(
                SOURCE,
                True,
                0,
                known_ids,
                True,
                "fallback_http",
                original,
                fetch_page,
                fetch_detail,
            )

        self.assertTrue(result.write_safe)
        self.assertEqual(
            result.termination_reason,
            "incremental_checkpoint",
        )
        self.assertNotIn(4, page_calls)
        self.assertEqual(
            [item["notice_id"] for item in result.items],
            ["9000"],
        )


class SourceSchedulingRegressionTests(unittest.TestCase):
    def test_failed_run_refreshes_pending_notice_before_next_crawl(self):
        state = fresh_state()
        state["runs"].append({"status": "failed"})
        state["active_incidents"] = {"incident": {"count": 1}}
        state["sources"]["2"] = {
            "observed_ids": ["550000"],
        }
        context = sync_engine.DestinationContext(
            "token",
            "database",
            pending_page_ids=("pending-page",),
            pending_page_sources={"pending-page": "2"},
            pending_page_notices={"pending-page": "548926"},
        )

        with patch.object(
            crawler_main,
            "inspect_destination_pending_context",
            return_value=context,
        ):
            count = (
                crawler_main.refresh_destination_pending_notice_state(
                    state,
                    "token",
                    "database",
                    {"141", "2"},
                )
            )

        self.assertEqual(count, 1)
        self.assertEqual(
            state["sources"]["2"]["pending_notice_ids"],
            ["548926"],
        )
        self.assertTrue(
            crawler_main.should_refresh_destination_pending_state(state)
        )

    def test_collect_report_forces_pending_notice_detail_refresh(self):
        state = fresh_state()
        state["sources"]["2"] = {
            "observed_ids": ["550000"],
            "pending_notice_ids": ["548926"],
        }
        captured = {}

        def crawl_sources(**kwargs):
            captured.update(kwargs)
            return CrawlReport(
                [
                    SourceCrawlResult(
                        source=SourceSpec(
                            config_fk="2",
                            classification="학사공지",
                            list_url=(
                                "https://www.sogang.ac.kr/ko/"
                                "academic-notice"
                            ),
                        ),
                        status=SourceStatus.SUCCESS,
                    )
                ]
            )

        with (
            patch.object(
                crawler_main,
                "resolve_html_path",
                return_value=None,
            ),
            patch.object(
                crawler_main,
                "get_bbs_config_fks",
                return_value=["2"],
            ),
            patch.object(
                crawler_main,
                "crawl_sources",
                side_effect=crawl_sources,
            ),
        ):
            crawler_main.collect_report(
                state,
                full_reconcile=False,
            )

        self.assertEqual(
            captured["known_ids_by_source"]["2"],
            {"550000", "548926"},
        )
        self.assertEqual(
            captured["refresh_ids_by_source"]["2"],
            {"548926"},
        )
        self.assertEqual(
            captured["targeted_refresh_ids_by_source"]["2"],
            {"548926"},
        )

    def test_collect_report_targets_manual_recovery_notice_ids(self):
        state = fresh_state()
        state["sources"]["2"] = {
            "observed_ids": ["550000"],
        }
        captured = {}

        def crawl_sources(**kwargs):
            captured.update(kwargs)
            return CrawlReport([])

        with (
            patch.dict(
                os.environ,
                {
                    "MANUAL_NOTICE_RECOVERY_IDS": (
                        "2:546129,2:546127,2:546129"
                    )
                },
            ),
            patch.object(
                crawler_main,
                "resolve_html_path",
                return_value=None,
            ),
            patch.object(
                crawler_main,
                "get_bbs_config_fks",
                return_value=["2"],
            ),
            patch.object(
                crawler_main,
                "crawl_sources",
                side_effect=crawl_sources,
            ),
        ):
            crawler_main.collect_report(
                state,
                full_reconcile=False,
            )

        self.assertEqual(
            captured["known_ids_by_source"]["2"],
            {"550000", "546129", "546127"},
        )
        self.assertEqual(
            captured["refresh_ids_by_source"]["2"],
            {"546129", "546127"},
        )
        self.assertEqual(
            captured["targeted_refresh_ids_by_source"]["2"],
            {"546129", "546127"},
        )

    def test_collect_report_rejects_invalid_manual_recovery_id(self):
        state = fresh_state()
        state["sources"]["2"] = {"observed_ids": ["546129"]}

        with (
            patch.dict(
                os.environ,
                {"MANUAL_NOTICE_RECOVERY_IDS": "2-546127"},
            ),
            patch.object(
                crawler_main,
                "resolve_html_path",
                return_value=None,
            ),
            patch.object(
                crawler_main,
                "get_bbs_config_fks",
                return_value=["2"],
            ),
            self.assertRaisesRegex(
                crawler_main.LocalConfigurationError,
                "출처ID:공지ID",
            ),
        ):
            crawler_main.collect_report(
                state,
                full_reconcile=False,
            )

    def test_manual_recovery_request_is_bounded_before_crawl(self):
        with (
            patch.dict(
                os.environ,
                {"MANUAL_NOTICE_RECOVERY_IDS": "2:1,2:2"},
            ),
            patch.object(
                crawler_main,
                "get_backfill_detail_limit",
                return_value=1,
            ),
            self.assertRaisesRegex(
                crawler_main.LocalConfigurationError,
                "처리 한도",
            ),
        ):
            crawler_main.manual_recovery_notice_ids(["2"])

    def test_collect_report_schedules_due_refresh_during_incremental_run(self):
        state = fresh_state()
        observation = crawler.build_notice_observation(
            "550000",
            "오래된 공지",
            "2020-01-01T00:00:00+09:00",
            False,
        )
        state["sources"]["2"] = {
            "observed_ids": ["550000"],
            "notice_refresh_state": {
                "550000": {
                    **observation,
                    "last_detail_at": "2020-01-01T00:00:00+00:00",
                }
            },
        }
        captured = {}

        def crawl_sources(**kwargs):
            captured.update(kwargs)
            return CrawlReport([])

        with (
            patch.object(
                crawler_main,
                "resolve_html_path",
                return_value=None,
            ),
            patch.object(
                crawler_main,
                "get_bbs_config_fks",
                return_value=["2"],
            ),
            patch.object(
                crawler_main,
                "crawl_sources",
                side_effect=crawl_sources,
            ),
        ):
            crawler_main.collect_report(
                state,
                full_reconcile=False,
            )

        self.assertEqual(
            captured["refresh_ids_by_source"]["2"],
            {"550000"},
        )
        self.assertEqual(
            captured["targeted_refresh_ids_by_source"]["2"],
            set(),
        )

    def test_backfill_source_runs_first_with_fair_budget(self):
        sources = {
            "141": SOURCE,
            "2": SourceSpec(
                config_fk="2",
                classification="학사공지",
                list_url=(
                    "https://www.sogang.ac.kr/ko/"
                    "academic-notice"
                ),
            ),
        }
        execution_order = []
        budgets = {}

        def crawl(_adapter, source, **kwargs):
            execution_order.append(source.config_fk)
            budget = crawler.CURRENT_SOURCE_REQUEST_BUDGET.get()
            budgets[source.config_fk] = (
                budget.max_actual_requests,
                budget.max_seconds,
            )
            return SourceCrawlResult(
                source=source,
                status=SourceStatus.SUCCESS,
                termination_reason="incremental_checkpoint",
            )

        with (
            patch.dict(
                os.environ,
                {
                    "SOURCE_MAX_REQUESTS": "100",
                    "SOURCE_MAX_SECONDS": "480",
                },
            ),
            patch.object(
                crawler,
                "remaining_run_seconds",
                return_value=900.0,
            ),
            patch.object(
                crawler,
                "get_destination_state_reserve_seconds",
                return_value=300.0,
            ),
            patch.object(
                crawler,
                "get_bbs_config_fks",
                return_value=["141", "2"],
            ),
            patch.object(
                crawler,
                "build_source_spec",
                side_effect=lambda source_id: sources[source_id],
            ),
            patch.object(
                crawler.SogangSourceAdapter,
                "crawl",
                autospec=True,
                side_effect=crawl,
            ),
        ):
            report = crawler.crawl_sources(
                source_state_by_source={
                    "141": {},
                    "2": {"backfill_active": True},
                },
                reconcile_mode_by_source={
                    "141": False,
                    "2": True,
                },
            )

        self.assertEqual(execution_order, ["2", "141"])
        self.assertEqual(
            [result.source.config_fk for result in report.sources],
            ["141", "2"],
        )
        self.assertEqual(budgets["2"], (50, 300.0))
        self.assertEqual(budgets["141"], (50, 480.0))
        self.assertFalse(report.sources[0].reconcile_requested)
        self.assertTrue(report.sources[1].reconcile_requested)

    def test_each_source_receives_independent_request_budget(self):
        sources = {
            "141": SOURCE,
            "2": SourceSpec(
                config_fk="2",
                classification="학사공지",
                list_url=(
                    "https://www.sogang.ac.kr/ko/"
                    "academic-notice"
                ),
            ),
        }
        observations = {}

        def crawl(_adapter, source, **kwargs):
            budget = crawler.CURRENT_SOURCE_REQUEST_BUDGET.get()
            first = budget.consume_actual()
            second = budget.consume_actual()
            observations[source.config_fk] = (
                first,
                second,
                budget.actual_requests,
            )
            return SourceCrawlResult(
                source=source,
                status=SourceStatus.SUCCESS,
            )

        with (
            patch.dict(
                os.environ,
                {"SOURCE_MAX_REQUESTS": "2"},
            ),
            patch.object(
                crawler,
                "remaining_run_seconds",
                return_value=None,
            ),
            patch.object(
                crawler,
                "get_bbs_config_fks",
                return_value=["141", "2"],
            ),
            patch.object(
                crawler,
                "build_source_spec",
                side_effect=lambda source_id: sources[source_id],
            ),
            patch.object(
                crawler.SogangSourceAdapter,
                "crawl",
                autospec=True,
                side_effect=crawl,
            ),
        ):
            crawler.crawl_sources()

        self.assertEqual(
            observations,
            {
                "141": (True, False, 1),
                "2": (True, False, 1),
            },
        )

    def test_collect_report_reconciles_only_due_source(self):
        state = fresh_state()
        now = datetime.now(timezone.utc).isoformat()
        state["sources"] = {
            "141": {
                "observed_ids": ["300"],
                "last_coverage_reconcile_at": now,
            },
            "2": {
                "observed_ids": ["200"],
                "backfill_active": True,
                "backfill_resume_page": 5,
                "backfill_anchor_ids": ["190"],
            },
        }
        captured = {}

        def crawl_sources(**kwargs):
            captured.update(kwargs)
            return CrawlReport(
                [
                    crawl_result(),
                    SourceCrawlResult(
                        source=SourceSpec(
                            config_fk="2",
                            classification="학사공지",
                            list_url=(
                                "https://www.sogang.ac.kr/ko/"
                                "academic-notice"
                            ),
                        ),
                        status=SourceStatus.SUCCESS,
                    ),
                ]
            )

        with (
            patch.object(
                crawler_main,
                "resolve_html_path",
                return_value=None,
            ),
            patch.object(
                crawler_main,
                "get_bbs_config_fks",
                return_value=["141", "2"],
            ),
            patch.object(
                crawler_main,
                "crawl_sources",
                side_effect=crawl_sources,
            ),
            self.assertLogs(crawler_main.LOGGER, level="INFO") as logs,
        ):
            crawler_main.collect_report(
                state,
                full_reconcile=True,
                record_reconcile_attempts=True,
            )

        self.assertEqual(
            captured["reconcile_mode_by_source"],
            {"141": False, "2": True},
        )
        self.assertEqual(
            captured["resume_page_by_source"],
            {"141": 1, "2": 5},
        )
        self.assertEqual(
            captured["resume_anchor_ids_by_source"],
            {"141": set(), "2": {"190"}},
        )
        self.assertNotIn(
            "last_reconcile_attempt_at",
            state["sources"]["141"],
        )
        self.assertIn(
            "last_reconcile_attempt_at",
            state["sources"]["2"],
        )
        self.assertTrue(
            any(
                "수집 계획: 출처=2, 모드=과거 보강, "
                "상세 한도=20, 시작 페이지=5" in message
                for message in logs.output
            )
        )
        self.assertTrue(
            any(
                "보강 판정=보강 이력 없음, 다음 보강 가능=즉시"
                in message
                for message in logs.output
            )
        )

    def test_recent_backfill_attempt_stays_incremental(self):
        now = datetime.now(timezone.utc).isoformat()
        state = fresh_state()
        state["sources"]["141"] = {
            "observed_ids": ["300"],
            "backfill_active": True,
            "backfill_resume_page": 5,
            "backfill_anchor_ids": ["290"],
            "last_reconcile_attempt_at": now,
            "last_success_at": now,
        }
        captured = {}

        def crawl_sources(**kwargs):
            captured.update(kwargs)
            return CrawlReport([crawl_result()])

        with (
            patch.object(
                crawler_main,
                "resolve_html_path",
                return_value=None,
            ),
            patch.object(
                crawler_main,
                "get_bbs_config_fks",
                return_value=["141"],
            ),
            patch.object(
                crawler_main,
                "crawl_sources",
                side_effect=crawl_sources,
            ),
            self.assertLogs(crawler_main.LOGGER, level="INFO") as logs,
        ):
            crawler_main.collect_report(
                state,
                full_reconcile=True,
                record_reconcile_attempts=True,
            )

        self.assertFalse(
            captured["reconcile_mode_by_source"]["141"]
        )
        self.assertTrue(
            captured["incremental_by_source"]["141"]
        )
        self.assertEqual(
            captured["resume_page_by_source"]["141"],
            1,
        )
        self.assertEqual(
            state["sources"]["141"]["last_reconcile_attempt_at"],
            now,
        )
        self.assertTrue(
            any(
                "수집 계획: 출처=141, 모드=증분, "
                "상세 한도=-, 시작 페이지=1, 백필=대기" in message
                for message in logs.output
            )
        )
        self.assertTrue(
            any(
                "보강 판정=오늘 보강 완료" in message
                for message in logs.output
            )
        )

class SourceEmptySafetyRegressionTests(unittest.TestCase):
    def empty_report(self, source_id: str) -> CrawlReport:
        return CrawlReport(
            [
                SourceCrawlResult(
                    source=SourceSpec(
                        config_fk=source_id,
                        classification="공지",
                        list_url=(
                            "https://www.sogang.ac.kr/ko/"
                            f"source-{source_id}"
                        ),
                    ),
                    status=SourceStatus.VALID_EMPTY,
                    observed_count=0,
                    terminal_reached=True,
                    termination_reason="natural_end",
                    full_snapshot=True,
                    reconcile_complete=True,
                    coverage_complete=True,
                )
            ]
        )

    def test_empty_source_is_always_excluded_from_writes(self):
        report = validate_crawl_report(
            self.empty_report("141"),
            fresh_state(),
            full_reconcile=True,
        )

        self.assertIn(
            "unexpected_empty_source",
            [issue.code for issue in report.issues],
        )


class DestructiveMutationRegressionTests(unittest.TestCase):
    item = {
        "source_id": "141",
        "notice_id": "2001",
        "title": "공지 2001",
        "url": (
            "https://www.sogang.ac.kr/ko/detail/"
            "2001?bbsConfigFk=141"
        ),
        "date": "2026-07-27T12:00:00+09:00",
        "top": False,
        "completeness": "complete",
        "body_status": "present",
        "attachments_status": "known",
    }
    shrink_candidate = {
        "candidate_id": "same-shrink",
        "reasons": ["body_text:10>1"],
    }

    def run_top_observation(
        self,
        state: dict,
        run_id: str,
        *,
        present: bool = False,
        verified: bool = True,
    ):
        result = crawl_result(
            top_present=present,
            top_verified=verified,
        )
        report = CrawlReport([result])
        disable_calls = []

        def plan_missing(_token, _database, _source, current_ids):
            candidate = {"notice_id": "1001"}
            return (
                [candidate],
                (
                    []
                    if "1001" in current_ids
                    else [candidate]
                ),
            )

        def disable(
            _token,
            _database,
            _source,
            current_ids,
            eligible_notice_ids=None,
            planned_candidates=None,
            total_top_count=None,
        ):
            eligible = set(eligible_notice_ids or set())
            disable_calls.append((set(current_ids), eligible))
            return len(eligible)

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=sync_engine.DestinationContext(
                    "token",
                    "database",
                ),
            ),
            patch.object(
                sync_engine,
                "resolve_destination_preflight",
                return_value=[],
            ),
            patch.object(
                sync_engine,
                "inspect_missing_top",
                side_effect=plan_missing,
            ),
            patch.object(
                sync_engine,
                "top_candidate_ids",
                side_effect=lambda pages: {
                    str(page["notice_id"]) for page in pages
                },
            ),
            patch.object(
                sync_engine,
                "disable_missing_top",
                side_effect=disable,
            ),
            patch.object(
                sync_engine,
                "validate_top_disable_candidates",
                return_value=None,
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
        ):
            counters = sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                previous_state=state,
                run_id=run_id,
            )

        update_state_from_report(
            state,
            report,
            False,
            {"141"},
            counters,
        )
        state["runs"].append({"run_id": run_id})
        return counters, disable_calls

    def run_shrink_observation(
        self,
        state: dict,
        run_id: str,
        candidate,
    ):
        result = crawl_result(items=[self.item])
        report = CrawlReport([result])
        applied = []
        preflight = sync_engine.DestinationPreflight(
            item=self.item,
            existing_page={"id": "page", "properties": {}},
            operation_id="operation",
            shrink_key="141:2001",
            shrink_candidate=candidate,
        )

        with (
            patch.object(
                sync_engine,
                "prepare_source_items",
                return_value=[self.item],
            ),
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=sync_engine.DestinationContext(
                    "token",
                    "database",
                ),
            ),
            patch.object(
                sync_engine,
                "resolve_destination_preflight",
                return_value=[preflight],
            ),
            patch.object(
                sync_engine,
                "validate_destination_preflight_entries",
                return_value=None,
            ),
            patch.object(
                sync_engine,
                "validate_destination_preflight_entry",
                return_value=preflight.existing_page,
            ),
            patch.object(
                sync_engine,
                "retrieve_page",
                return_value=preflight.existing_page,
            ),
            patch.object(
                sync_engine,
                "apply_item",
                side_effect=lambda *args, **kwargs: applied.append(
                    "applied"
                ),
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
        ):
            counters = sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                previous_state=state,
                run_id=run_id,
            )

        update_state_from_report(
            state,
            report,
            False,
            {"141"},
            counters,
        )
        state["runs"].append({"run_id": run_id})
        return counters, applied

    def test_pinned_top_requires_two_consecutive_verified_absences(self):
        state = fresh_state()

        first, first_calls = self.run_top_observation(state, "R1")
        second, second_calls = self.run_top_observation(state, "R2")

        self.assertEqual(first.top_disabled, 0)
        self.assertEqual(first_calls, [(set(), set())])
        self.assertEqual(second.top_disabled, 1)
        self.assertEqual(second_calls, [(set(), {"1001"})])

    def test_pinned_top_failed_gap_and_present_snapshot_reset_absence(self):
        failed_gap_state = fresh_state()
        self.run_top_observation(failed_gap_state, "R1")
        failed_gap_state["runs"].append({"run_id": "FAILED"})

        after_gap, after_gap_calls = self.run_top_observation(
            failed_gap_state,
            "R2",
        )

        present_state = fresh_state()
        self.run_top_observation(present_state, "R1")
        self.run_top_observation(
            present_state,
            "PRESENT",
            present=True,
        )
        after_present, after_present_calls = self.run_top_observation(
            present_state,
            "R2",
        )

        self.assertEqual(after_gap.top_disabled, 0)
        self.assertEqual(after_gap_calls, [(set(), set())])
        self.assertEqual(after_present.top_disabled, 0)
        self.assertEqual(after_present_calls, [(set(), set())])

    def test_shrink_requires_same_candidate_on_next_successful_run(self):
        state = fresh_state()

        first, first_applied = self.run_shrink_observation(
            state,
            "R1",
            self.shrink_candidate,
        )
        _second, second_applied = self.run_shrink_observation(
            state,
            "R2",
            self.shrink_candidate,
        )

        self.assertEqual(first.unchanged, 1)
        self.assertEqual(first_applied, [])
        self.assertEqual(second_applied, ["applied"])
        self.assertEqual(state["shrink_candidates"], {})

    def test_shrink_failed_gap_ttl_and_normal_body_prevent_stale_apply(self):
        failed_gap_state = fresh_state()
        self.run_shrink_observation(
            failed_gap_state,
            "R1",
            self.shrink_candidate,
        )
        failed_gap_state["runs"].append({"run_id": "FAILED"})

        after_gap, after_gap_applied = self.run_shrink_observation(
            failed_gap_state,
            "R2",
            self.shrink_candidate,
        )

        expired_state = fresh_state()
        self.run_shrink_observation(
            expired_state,
            "R1",
            self.shrink_candidate,
        )
        expired_state["shrink_candidates"]["141:2001"][
            "last_observed_at"
        ] = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).isoformat()

        after_expiry, after_expiry_applied = (
            self.run_shrink_observation(
                expired_state,
                "R2",
                self.shrink_candidate,
            )
        )

        restored_state = fresh_state()
        self.run_shrink_observation(
            restored_state,
            "R1",
            self.shrink_candidate,
        )
        _restored, restored_applied = self.run_shrink_observation(
            restored_state,
            "NORMAL",
            None,
        )

        self.assertEqual(after_gap.unchanged, 1)
        self.assertEqual(after_gap_applied, [])
        self.assertEqual(after_expiry.unchanged, 1)
        self.assertEqual(after_expiry_applied, [])
        self.assertEqual(restored_applied, ["applied"])
        self.assertEqual(restored_state["shrink_candidates"], {})

    def test_dry_run_uses_same_ttl_gate_as_apply(self):
        old_time = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).isoformat()
        state = fresh_state()
        state["runs"] = [{"run_id": "R1"}]
        state["shrink_candidates"] = {
            "141:2001": {
                "candidate_id": "same-shrink",
                "observations": 1,
                "last_observed_run_id": "R1",
                "last_observed_at": old_time,
            }
        }
        result = crawl_result(items=[self.item])
        report = CrawlReport([result])
        preflight = sync_engine.DestinationPreflight(
            item=self.item,
            existing_page={"id": "page", "properties": {}},
            operation_id="operation",
            shrink_key="141:2001",
            shrink_candidate=self.shrink_candidate,
        )

        with (
            patch.object(
                sync_engine,
                "prepare_source_items",
                return_value=[self.item],
            ),
            patch.object(
                sync_engine,
                "fetch_database",
                return_value={},
            ),
            patch.object(
                sync_engine,
                "validate_destination_schema",
                return_value=None,
            ),
            patch.object(
                sync_engine,
                "resolve_destination_preflight",
                return_value=[preflight],
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
        ):
            plan = sync_engine.build_dry_run_plan(
                "DRY",
                report,
                "token",
                "database",
                False,
                state,
            )

        self.assertEqual(len(plan.actions), 1)
        self.assertEqual(plan.actions[0].kind, MutationKind.CONFLICT)
        self.assertEqual(
            plan.actions[0].reason,
            "shrink_candidate_pending",
        )

    def test_pending_shrink_ids_are_refreshed_during_incremental_run(self):
        state = fresh_state()
        state["sources"]["141"] = {
            "observed_ids": ["2001", "2000"],
        }
        state["shrink_candidates"]["141:2001"] = {
            "candidate_id": "same-shrink",
            "observations": 1,
        }
        captured = {}

        def crawl_sources(**kwargs):
            captured.update(kwargs)
            return CrawlReport(
                [
                    crawl_result(
                        top_verified=False,
                    )
                ]
            )

        with (
            patch.object(
                crawler_main,
                "resolve_html_path",
                return_value=None,
            ),
            patch.object(
                crawler_main,
                "get_bbs_config_fks",
                return_value=["141"],
            ),
            patch.object(
                crawler_main,
                "crawl_sources",
                side_effect=crawl_sources,
            ),
        ):
            crawler_main.collect_report(
                state,
                full_reconcile=False,
            )

        self.assertTrue(captured["incremental"])
        self.assertTrue(captured["incremental_by_source"]["141"])
        self.assertEqual(
            captured["refresh_ids_by_source"]["141"],
            {"2001"},
        )

    def test_full_reconcile_passes_backfill_resume_state_to_crawler(self):
        state = fresh_state()
        state["sources"]["141"] = {
            "observed_ids": ["106", "105"],
            "backfill_active": True,
            "backfill_resume_page": 6,
            "backfill_anchor_ids": ["102", "101"],
        }
        captured = {}

        def crawl_sources(**kwargs):
            captured.update(kwargs)
            return CrawlReport([crawl_result()])

        with (
            patch.object(
                crawler_main,
                "resolve_html_path",
                return_value=None,
            ),
            patch.object(
                crawler_main,
                "get_bbs_config_fks",
                return_value=["141"],
            ),
            patch.object(
                crawler_main,
                "crawl_sources",
                side_effect=crawl_sources,
            ),
        ):
            crawler_main.collect_report(
                state,
                full_reconcile=True,
            )

        self.assertEqual(
            captured["resume_page_by_source"]["141"],
            6,
        )
        self.assertEqual(
            captured["resume_anchor_ids_by_source"]["141"],
            {"102", "101"},
        )

    def test_force_all_reconcile_disables_all_incremental_paths(self):
        known_ids = [str(1000 + index) for index in range(300)]
        state = fresh_state()
        state["sources"]["141"] = {
            "observed_ids": known_ids,
            "backfill_active": True,
            "backfill_resume_page": 6,
            "backfill_anchor_ids": ["999"],
        }
        captured_runs = []

        def crawl_sources(**kwargs):
            captured_runs.append(kwargs)
            return CrawlReport([crawl_result()])

        with (
            patch.object(
                crawler_main,
                "resolve_html_path",
                return_value=None,
            ),
            patch.object(
                crawler_main,
                "get_bbs_config_fks",
                return_value=["141"],
            ),
            patch.object(
                crawler_main,
                "crawl_sources",
                side_effect=crawl_sources,
            ),
        ):
            crawler_main.collect_report(
                state,
                full_reconcile=True,
                force_all_reconcile=True,
            )
            state["sources"]["141"]["backfill_resume_page"] = 11
            state["sources"]["141"]["backfill_anchor_ids"] = ["1999"]
            crawler_main.collect_report(
                state,
                full_reconcile=True,
                force_all_reconcile=True,
            )

        first, second = captured_runs
        self.assertFalse(first["incremental"])
        self.assertFalse(
            first["incremental_by_source"]["141"]
        )
        self.assertTrue(
            first["reconcile_mode_by_source"]["141"]
        )
        self.assertEqual(
            first["known_ids_by_source"]["141"],
            set(known_ids),
        )
        self.assertEqual(
            first["resume_page_by_source"]["141"],
            6,
        )
        self.assertEqual(
            first["resume_anchor_ids_by_source"]["141"],
            {"999"},
        )
        self.assertFalse(
            second["incremental_by_source"]["141"]
        )
        self.assertEqual(
            second["resume_page_by_source"]["141"],
            11,
        )
        self.assertEqual(
            second["resume_anchor_ids_by_source"]["141"],
            {"1999"},
        )


if __name__ == "__main__":
    unittest.main()
