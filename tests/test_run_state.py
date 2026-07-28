import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_state
import crawler
import notion_client
from models import (
    CrawlReport,
    DestinationConsistencyError,
    FailureCategory,
    LocalConfigurationError,
    SourceCrawlResult,
    SourceSpec,
    SourceStatus,
    SyncCounters,
    ValidationIssue,
    RunRecord,
)


def source_result(
    config_fk: str,
    status: SourceStatus,
    observed_ids,
    *,
    error: str = "",
    category: FailureCategory = FailureCategory.NONE,
) -> SourceCrawlResult:
    return SourceCrawlResult(
        source=SourceSpec(
            config_fk=config_fk,
            classification=f"분류-{config_fk}",
            list_url=f"https://www.sogang.ac.kr/{config_fk}",
        ),
        status=status,
        method="api",
        observed_count=len(observed_ids),
        observed_ids=list(observed_ids),
        category=category,
        error=error,
        terminal_reached=status in {
            SourceStatus.SUCCESS,
            SourceStatus.VALID_EMPTY,
        },
        termination_reason=(
            "natural_end"
            if status
            in {SourceStatus.SUCCESS, SourceStatus.VALID_EMPTY}
            else ""
        ),
        full_snapshot=status in {
            SourceStatus.SUCCESS,
            SourceStatus.VALID_EMPTY,
        },
    )


class RunStateTests(unittest.TestCase):
    def test_unknown_top_level_state_is_discarded(self):
        state = run_state.default_run_state()
        state["obsolete_internal_state"] = {"page_id": "example"}
        state["state_checksum"] = run_state.state_checksum(state)

        validated = run_state.validate_run_state_payload(state)

        self.assertNotIn("obsolete_internal_state", validated)
        self.assertEqual(
            validated["state_checksum"],
            run_state.state_checksum(validated),
        )

    def test_run_attempt_identity_and_record_upsert_are_idempotent(self):
        with patch.dict(
            os.environ,
            {
                "GITHUB_RUN_ID": "900",
                "GITHUB_RUN_ATTEMPT": "2",
            },
            clear=False,
        ):
            record = run_state.create_run_record(False, False)

        self.assertEqual(record.run_id, "900")
        self.assertEqual(record.run_attempt, "2")
        self.assertEqual(record.execution_id, "900:2")
        state = run_state.default_run_state()
        run_state.append_run_record(state, record)
        record.status = "failed"
        run_state.append_run_record(state, record)
        self.assertEqual(len(state["runs"]), 1)
        self.assertEqual(state["runs"][0]["status"], "failed")
        next_attempt = RunRecord(
            run_id="900",
            run_attempt="3",
            execution_id="900:3",
            scheduled_at=record.scheduled_at,
            started_at=record.started_at,
        )
        run_state.append_run_record(state, next_attempt)
        self.assertEqual(
            [value["execution_id"] for value in state["runs"]],
            ["900:2", "900:3"],
        )

    def test_pending_notice_ids_persist_in_known_set_until_recovered(self):
        state = run_state.default_run_state()
        report = CrawlReport(
            sources=[
                source_result(
                    "2",
                    SourceStatus.SUCCESS,
                    ["100"],
                )
            ]
        )
        run_state.update_state_from_report(
            state,
            report,
            False,
            {"2"},
            SyncCounters(
                unresolved_pending_notices={"2": ["99"]},
            ),
        )

        self.assertEqual(
            state["sources"]["2"]["pending_notice_ids"],
            ["99"],
        )
        self.assertEqual(
            run_state.known_ids_for_source(state, "2"),
            {"99", "100"},
        )

        run_state.update_state_from_report(
            state,
            report,
            False,
            {"2"},
            SyncCounters(
                recovered_pending_notices={"2": ["99"]},
            ),
        )
        self.assertNotIn(
            "pending_notice_ids",
            state["sources"]["2"],
        )

    def test_same_logical_run_attempt_does_not_confirm_empty_source(self):
        state = run_state.default_run_state()
        state["sources"]["2"] = {
            "observed_ids": ["100"],
            "empty_observation_count": 1,
            "empty_last_run_id": "900:1",
            "empty_last_logical_run_id": "900",
        }
        state["runs"] = [
            {
                "run_id": "900",
                "run_attempt": "1",
                "execution_id": "900:1",
            }
        ]
        report = CrawlReport(
            sources=[
                source_result(
                    "2",
                    SourceStatus.CONFIRMED_EMPTY,
                    [],
                )
            ]
        )

        run_state.update_state_from_report(
            state,
            report,
            True,
            {"2"},
            SyncCounters(
                observation_run_id="900:2",
                observation_logical_run_id="900",
            ),
        )

        self.assertEqual(
            state["sources"]["2"]["observed_ids"],
            ["100"],
        )
        self.assertTrue(
            state["sources"]["2"]["empty_confirmation_pending"]
        )

    def test_destination_consistency_error_has_distinct_category(self):
        self.assertEqual(
            run_state.classify_exception(
                DestinationConsistencyError("목적지 커밋 검증 실패")
            ),
            FailureCategory.DESTINATION_CONTRACT,
        )

    def test_local_destination_configuration_is_not_notion_outage(self):
        auth_error = LocalConfigurationError(
            "NOTION_TOKEN과 NOTION_DB_ID를 설정해야 합니다",
            "destination_auth",
        )
        contract_error = LocalConfigurationError(
            "NOTION_SCHEMA_MIGRATION=1이 필요합니다",
            "destination_contract",
        )

        self.assertEqual(
            run_state.classify_exception(auth_error),
            FailureCategory.DESTINATION_AUTH,
        )
        self.assertEqual(
            run_state.classify_exception(contract_error),
            FailureCategory.DESTINATION_CONTRACT,
        )
        signature = run_state.stable_exception_signature(auth_error)
        self.assertEqual(signature["origin"], "local_config")
        self.assertEqual(signature["kind"], "destination_auth")

    def test_exception_classification_preserves_failure_origin(self):
        cases = [
            (
                crawler.SourceAccessBlocked(
                    "upstream blocked",
                    status_code=403,
                ),
                FailureCategory.SECURITY_POLICY,
            ),
            (
                notion_client.NotionRequestError(
                    "Notion unavailable",
                    status_code=503,
                ),
                FailureCategory.NOTION,
            ),
            (
                notion_client.NotionRequestError(
                    "DNS lookup failed",
                    reason="dns",
                ),
                FailureCategory.NOTION,
            ),
            (
                notion_client.NotionSchemaMigrationRequired(
                    "migration required"
                ),
                FailureCategory.DESTINATION_CONTRACT,
            ),
            (
                notion_client.NotionDataSourceResolutionError(
                    "multiple data sources"
                ),
                FailureCategory.DESTINATION_CONTRACT,
            ),
        ]

        for exc, expected in cases:
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(
                    run_state.classify_exception(exc),
                    expected,
                )

    def test_incident_fingerprint_separates_exception_causes(self):
        state = run_state.default_run_state()
        first = run_state.build_incident(
            state,
            FailureCategory.INTERNAL,
            "동기화 실패",
            "첫 실패",
            exception=notion_client.NotionRequestError(
                "Notion HTTP 503",
                status_code=503,
            ),
        )
        second = run_state.build_incident(
            state,
            FailureCategory.INTERNAL,
            "동기화 실패",
            "두 번째 실패",
            exception=TypeError("sync engine contract mismatch"),
        )

        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertTrue(second["should_signal_failure"])
        self.assertEqual(second["count"], 1)

    def test_exception_signature_normalizes_dynamic_identifiers(self):
        first = run_state.stable_exception_signature(
            RuntimeError(
                "page 123456 failed at "
                "https://example.com/item/123456?token=secret"
            )
        )
        second = run_state.stable_exception_signature(
            RuntimeError(
                "page 987654 failed at "
                "https://example.com/item/987654?token=other"
            )
        )

        self.assertEqual(first, second)

    def test_incident_text_removes_signed_url_secrets(self):
        text = run_state.sanitize_incident_text(
            "실패 https://www.sogang.ac.kr/file.pdf"
            "?token=secret&signature=private#fragment"
        )

        self.assertIn("www.sogang.ac.kr/file.pdf", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("signature", text)
        self.assertNotIn("private", text)

    def test_external_download_circuit_is_included_in_run_metrics(self):
        state = run_state.default_run_state()
        record = run_state.create_run_record(False, False)
        counters = SyncCounters(
            external_download_requests=1,
            external_download_stopped_reason="http_429",
            external_download_status_code=429,
            external_download_retry_after="45",
            external_download_retry_after_seconds=45.0,
            external_download_elapsed_seconds=1.25,
        )

        run_state.append_run_record(state, record, counters)

        metrics = state["runs"][-1]["metrics"]
        self.assertEqual(metrics["external_download_requests"], 1)
        self.assertEqual(
            metrics["external_download_stopped_reason"],
            "http_429",
        )
        self.assertEqual(metrics["external_download_status_code"], 429)
        self.assertEqual(
            metrics["external_download_retry_after"],
            "45",
        )
        self.assertEqual(
            metrics["external_download_retry_after_seconds"],
            45.0,
        )

    def test_new_source_reconciles_even_with_recent_global_watermark(self):
        state = run_state.default_run_state()
        state["last_coverage_reconcile_at"] = (
            datetime.now(timezone.utc).isoformat()
        )

        self.assertTrue(
            run_state.source_reconcile_due(state, "new-source", 24)
        )

    def test_recent_reconcile_attempt_throttles_incomplete_backfill(self):
        now = datetime.now(timezone.utc)
        state = run_state.default_run_state()
        state["sources"]["141"] = {
            "backfill_active": True,
            "last_reconcile_attempt_at": now.isoformat(),
            "last_coverage_reconcile_at": (
                now - timedelta(days=7)
            ).isoformat(),
        }

        self.assertFalse(
            run_state.source_reconcile_due(state, "141", 24)
        )

        state["sources"]["141"]["last_reconcile_attempt_at"] = (
            now - timedelta(hours=25)
        ).isoformat()

        self.assertTrue(
            run_state.source_reconcile_due(state, "141", 24)
        )

    def test_legacy_backfill_uses_recent_success_as_attempt_watermark(self):
        now = datetime.now(timezone.utc)
        state = run_state.default_run_state()
        state["sources"]["141"] = {
            "backfill_active": True,
            "last_success_at": now.isoformat(),
        }

        self.assertFalse(
            run_state.source_reconcile_due(state, "141", 24)
        )

    def test_reconcile_attempt_is_recorded_even_when_source_fails(self):
        fixed_now = "2026-07-28T00:00:00+00:00"
        state = run_state.default_run_state()
        state["sources"]["141"] = {
            "backfill_active": True,
            "observed_ids": ["old"],
        }
        failed = source_result(
            "141",
            SourceStatus.FAILED,
            [],
            error="temporary_source_error",
            category=FailureCategory.SOURCE_UPSTREAM,
        )
        failed.reconcile_requested = True

        with patch.object(
            run_state,
            "utc_now_iso",
            return_value=fixed_now,
        ):
            run_state.update_state_from_report(
                state,
                CrawlReport([failed]),
                full_reconcile=True,
                applied_source_ids=set(),
            )

        self.assertEqual(
            state["sources"]["141"]["last_reconcile_attempt_at"],
            fixed_now,
        )

    def test_incremental_run_materializes_legacy_backfill_watermark(self):
        previous_success = "2026-07-27T00:00:00+00:00"
        state = run_state.default_run_state()
        state["sources"]["141"] = {
            "backfill_active": True,
            "last_success_at": previous_success,
            "observed_ids": ["old"],
        }
        result = source_result(
            "141",
            SourceStatus.SUCCESS,
            ["new"],
        )
        result.reconcile_requested = False

        run_state.update_state_from_report(
            state,
            CrawlReport([result]),
            full_reconcile=False,
            applied_source_ids={"141"},
        )

        self.assertEqual(
            state["sources"]["141"]["last_reconcile_attempt_at"],
            previous_success,
        )

    def test_only_successful_source_checkpoint_advances(self):
        state = run_state.default_run_state()
        state["sources"] = {
            "141": {
                "observed_ids": ["141-old"],
                "last_success_at": "2026-07-01T00:00:00+00:00",
            },
            "2": {
                "observed_ids": ["2-old"],
                "last_success_at": "2026-07-01T00:00:00+00:00",
            },
        }
        report = CrawlReport(
            sources=[
                source_result("141", SourceStatus.SUCCESS, ["141-new"]),
                source_result(
                    "2",
                    SourceStatus.PARTIAL,
                    ["2-new"],
                    error="incremental_checkpoint_not_found",
                    category=FailureCategory.SOURCE_PARTIAL,
                ),
            ]
        )
        fixed_now = "2026-07-27T00:00:00+00:00"

        with patch.object(run_state, "utc_now_iso", return_value=fixed_now):
            updated = run_state.update_state_from_report(
                state,
                report,
                full_reconcile=True,
            )

        self.assertEqual(updated["sources"]["141"]["observed_ids"], ["141-new"])
        self.assertEqual(updated["sources"]["141"]["last_success_at"], fixed_now)
        self.assertEqual(updated["sources"]["2"]["observed_ids"], ["2-old"])
        self.assertEqual(
            updated["sources"]["2"]["last_success_at"],
            "2026-07-01T00:00:00+00:00",
        )
        self.assertEqual(
            updated["sources"]["2"]["error"],
            "incremental_checkpoint_not_found",
        )
        self.assertEqual(updated["last_partial_success_at"], fixed_now)
        self.assertIsNone(updated["last_success_at"])
        self.assertIsNone(updated["last_full_reconcile_at"])
        self.assertEqual(updated["consecutive_failures"], 1)

    def test_global_validation_block_advances_no_source_checkpoint(self):
        state = run_state.default_run_state()
        state["sources"] = {
            "141": {"observed_ids": ["141-old"]},
            "2": {"observed_ids": ["2-old"]},
        }
        report = CrawlReport(
            sources=[
                source_result("141", SourceStatus.SUCCESS, ["141-new"]),
                source_result("2", SourceStatus.SUCCESS, ["2-new"]),
            ],
            issues=[
                ValidationIssue(
                    code="cross_source_url_collision",
                    message="출처 URL 충돌",
                    source_config_fk="2",
                )
            ],
        )

        updated = run_state.update_state_from_report(
            state,
            report,
            full_reconcile=True,
        )

        self.assertEqual(
            updated["sources"]["141"]["observed_ids"],
            ["141-old"],
        )
        self.assertEqual(
            updated["sources"]["2"]["observed_ids"],
            ["2-old"],
        )
        self.assertIsNone(updated["last_partial_success_at"])
        self.assertEqual(updated["consecutive_failures"], 1)

    def test_incident_fingerprint_deduplicates_repeated_failure(self):
        state = run_state.default_run_state()
        report = CrawlReport(
            sources=[
                source_result(
                    "141",
                    SourceStatus.PARTIAL,
                    ["1001"],
                    error="repeated_page:2",
                    category=FailureCategory.SOURCE_PARTIAL,
                )
            ]
        )
        fixed_now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

        with (
            patch.dict(os.environ, {"FAILURE_REPEAT_SECONDS": "21600"}),
            patch.object(run_state, "utc_now_iso", return_value=fixed_now),
        ):
            first = run_state.build_incident(
                state,
                FailureCategory.SOURCE_PARTIAL,
                "출처 부분 실패",
                "첫 번째 요약",
                report,
            )
            self.assertTrue(run_state.mark_failure_signaled(state, first))
            second = run_state.build_incident(
                state,
                FailureCategory.SOURCE_PARTIAL,
                "출처 부분 실패",
                "표현만 달라진 두 번째 요약",
                report,
            )

        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertTrue(first["should_signal_failure"])
        self.assertFalse(second["should_signal_failure"])
        self.assertEqual(second["count"], 2)
        self.assertEqual(first["first_seen_at"], second["first_seen_at"])

    def test_undelivered_repeated_incident_is_not_suppressed(self):
        state = run_state.default_run_state()
        first = run_state.build_incident(
            state,
            FailureCategory.NOTION,
            "Notion 실패",
            "첫 번째",
        )
        second = run_state.build_incident(
            state,
            FailureCategory.NOTION,
            "Notion 실패",
            "두 번째",
        )

        self.assertTrue(first["should_signal_failure"])
        self.assertTrue(second["should_signal_failure"])

    def test_incident_fingerprint_changes_when_source_failure_changes(self):
        state = run_state.default_run_state()
        first_report = CrawlReport(
            sources=[
                source_result(
                    "141",
                    SourceStatus.PARTIAL,
                    [],
                    error="repeated_page:2",
                    category=FailureCategory.SOURCE_PARTIAL,
                )
            ]
        )
        second_report = CrawlReport(
            sources=[
                source_result(
                    "141",
                    SourceStatus.FAILED,
                    [],
                    error="HTTP 404",
                    category=FailureCategory.SOURCE_UPSTREAM,
                )
            ]
        )

        first = run_state.build_incident(
            state,
            FailureCategory.SOURCE_PARTIAL,
            "수집 실패",
            "첫 번째",
            first_report,
        )
        second = run_state.build_incident(
            state,
            FailureCategory.SOURCE_UPSTREAM,
            "수집 실패",
            "두 번째",
            second_report,
        )

        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertTrue(second["should_signal_failure"])
        self.assertEqual(second["count"], 1)

    def test_report_fingerprint_normalizes_dynamic_notice_ids(self):
        first_report = CrawlReport(
            sources=[
                source_result(
                    "141",
                    SourceStatus.PARTIAL,
                    [],
                    error="detail_snapshot_changed:123456",
                    category=FailureCategory.SOURCE_PARTIAL,
                )
            ]
        )
        second_report = CrawlReport(
            sources=[
                source_result(
                    "141",
                    SourceStatus.PARTIAL,
                    [],
                    error="detail_snapshot_changed:987654",
                    category=FailureCategory.SOURCE_PARTIAL,
                )
            ]
        )

        first = run_state.report_fingerprint(
            FailureCategory.SOURCE_PARTIAL,
            "수집 실패",
            first_report,
        )
        second = run_state.report_fingerprint(
            FailureCategory.SOURCE_PARTIAL,
            "수집 실패",
            second_report,
        )

        self.assertEqual(first, second)

    def test_report_fingerprint_preserves_distinct_http_causes(self):
        forbidden = CrawlReport(
            sources=[
                source_result(
                    "141",
                    SourceStatus.FAILED,
                    [],
                    error="fallback_browser_http_403",
                    category=FailureCategory.SOURCE_UPSTREAM,
                )
            ]
        )
        unavailable = CrawlReport(
            sources=[
                source_result(
                    "141",
                    SourceStatus.FAILED,
                    [],
                    error="fallback_browser_http_503",
                    category=FailureCategory.SOURCE_UPSTREAM,
                )
            ]
        )
        changed = CrawlReport(
            sources=[
                source_result(
                    "141",
                    SourceStatus.PARTIAL,
                    [],
                    error="detail_snapshot_changed:123456",
                    category=FailureCategory.SOURCE_PARTIAL,
                )
            ]
        )
        repeated = CrawlReport(
            sources=[
                source_result(
                    "141",
                    SourceStatus.PARTIAL,
                    [],
                    error="repeated_page_ids:123456",
                    category=FailureCategory.SOURCE_PARTIAL,
                )
            ]
        )

        self.assertNotEqual(
            run_state.report_fingerprint(
                FailureCategory.SOURCE_UPSTREAM,
                "수집 실패",
                forbidden,
            ),
            run_state.report_fingerprint(
                FailureCategory.SOURCE_UPSTREAM,
                "수집 실패",
                unavailable,
            ),
        )
        self.assertNotEqual(
            run_state.report_fingerprint(
                FailureCategory.SOURCE_PARTIAL,
                "수집 실패",
                changed,
            ),
            run_state.report_fingerprint(
                FailureCategory.SOURCE_PARTIAL,
                "수집 실패",
                repeated,
            ),
        )

    def test_report_fingerprint_separates_fatal_issue_causes(self):
        source = source_result("141", SourceStatus.SUCCESS, ["1001"])
        forbidden = CrawlReport(
            sources=[source],
            issues=[
                ValidationIssue(
                    code="external_download_circuit",
                    message=(
                        "상태 코드=403, 중단 사유=http_403"
                    ),
                )
            ],
        )
        rate_limited = CrawlReport(
            sources=[source],
            issues=[
                ValidationIssue(
                    code="external_download_circuit",
                    message=(
                        "상태 코드=429, 중단 사유=http_429"
                    ),
                )
            ],
        )
        first_quarantine = CrawlReport(
            sources=[source],
            issues=[
                ValidationIssue(
                    code="destination_pending_quarantine",
                    message="출처 격리: 141",
                    source_config_fk="141",
                )
            ],
        )
        second_quarantine = CrawlReport(
            sources=[source],
            issues=[
                ValidationIssue(
                    code="destination_pending_quarantine",
                    message="출처 격리: 2",
                    source_config_fk="2",
                )
            ],
        )

        self.assertNotEqual(
            run_state.report_fingerprint(
                FailureCategory.SECURITY_POLICY,
                "외부 파일 다운로드 안전 차단",
                forbidden,
            ),
            run_state.report_fingerprint(
                FailureCategory.SECURITY_POLICY,
                "외부 파일 다운로드 안전 차단",
                rate_limited,
            ),
        )
        self.assertNotEqual(
            run_state.report_fingerprint(
                FailureCategory.DESTINATION_CONTRACT,
                "대기 페이지 출처 격리",
                first_quarantine,
            ),
            run_state.report_fingerprint(
                FailureCategory.DESTINATION_CONTRACT,
                "대기 페이지 출처 격리",
                second_quarantine,
            ),
        )

    def test_success_clears_all_active_incidents(self):
        state = run_state.default_run_state()
        run_state.build_incident(
            state,
            FailureCategory.SOURCE_UPSTREAM,
            "출처 실패",
            "출처 장애",
        )
        run_state.build_incident(
            state,
            FailureCategory.DESTINATION_AUTH,
            "대상 실패",
            "인증 장애",
        )

        cleared = run_state.clear_active_incidents(state)

        self.assertEqual(cleared, 2)
        self.assertEqual(state["active_incidents"], {})
        self.assertEqual(state["last_incident"], {})

    def test_public_cache_preserves_only_failure_throttle_fields(self):
        state = run_state.default_run_state()
        incident = run_state.build_incident(
            state,
            FailureCategory.SOURCE_UPSTREAM,
            "출처 실패",
            "출처 장애",
        )
        self.assertTrue(run_state.mark_failure_signaled(state, incident))
        state["active_incidents"][incident["fingerprint"]][
            "private_summary"
        ] = "노출되면 안 되는 값"
        state["sources"]["141"] = {
            "backfill_active": True,
            "last_reconcile_attempt_at": "2026-07-28T00:00:00+00:00",
        }
        state["state_checksum"] = run_state.state_checksum(state)

        projected = run_state.build_public_cache_state(state)
        active = projected["active_incidents"][incident["fingerprint"]]

        self.assertEqual(
            set(active),
            run_state.ACTIVE_INCIDENT_FIELDS,
        )
        self.assertNotIn("private_summary", json.dumps(projected))
        self.assertEqual(projected["last_incident"], active)
        self.assertEqual(
            projected["sources"]["141"]["last_reconcile_attempt_at"],
            "2026-07-28T00:00:00+00:00",
        )

    def test_atomic_json_write_replaces_complete_document(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run-state.json"
            path.write_text('{"old": true}\\n', encoding="utf-8")
            payload = {
                "schema_version": 1,
                "sources": {"141": {"observed_ids": ["1001"]}},
            }
            replace_impl = os.replace

            with patch.object(
                run_state.os,
                "replace",
                wraps=replace_impl,
            ) as replace:
                run_state.write_json_atomic(path, payload)

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)
            self.assertTrue(path.read_text(encoding="utf-8").endswith("\n"))
            replace.assert_called_once()
            self.assertEqual(
                list(Path(temp_dir).glob(".run-state.json.*.tmp")),
                [],
            )

    def test_atomic_json_write_preserves_previous_state_on_serialization_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run-state.json"
            original = '{"stable": true}\n'
            path.write_text(original, encoding="utf-8")

            with self.assertRaises(TypeError):
                run_state.write_json_atomic(path, {"bad": object()})

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(
                list(Path(temp_dir).glob(".run-state.json.*.tmp")),
                [],
            )

    def test_run_state_round_trip_is_checksummed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run-state.json"
            state = run_state.default_run_state()
            state["sources"]["141"] = {"observed_ids": ["1001"]}

            run_state.write_run_state_atomic(path, state)
            loaded = run_state.load_run_state(path)

            self.assertEqual(
                loaded["state_checksum"],
                run_state.state_checksum(loaded),
            )
            self.assertEqual(
                loaded["sources"]["141"]["observed_ids"],
                ["1001"],
            )

    def test_run_state_tampering_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run-state.json"
            state = run_state.default_run_state()
            run_state.write_run_state_atomic(path, state)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["consecutive_failures"] = 999
            path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )

            with (
                patch.dict(os.environ, {"RUN_STATE_REQUIRED": "1"}),
                self.assertRaises(run_state.RunStateIntegrityError),
            ):
                run_state.load_run_state(path)

    def test_optional_invalid_run_state_starts_full_reconcile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run-state.json"
            path.write_text('{"schema_version": 2}', encoding="utf-8")

            loaded = run_state.load_run_state(path)

            self.assertTrue(
                run_state.should_full_reconcile(
                    loaded,
                    24,
                    ["141"],
                )
            )

    def test_v1_run_state_migrates_without_private_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run-state.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "consecutive_failures": 2,
                        "last_exception": {
                            "message": "private-error",
                        },
                        "operations": {
                            "operation": {
                                "page_id": "private-page-id",
                            }
                        },
                        "sources": {
                            "141": {
                                "observed_ids": ["1001"],
                                "last_full_reconcile_at": (
                                    "2026-07-01T00:00:00+00:00"
                                ),
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            loaded = run_state.load_run_state(path)

            self.assertEqual(loaded["schema_version"], 2)
            self.assertEqual(
                loaded["sources"]["141"]["observed_ids"],
                ["1001"],
            )
            self.assertNotIn(
                "last_full_reconcile_at",
                loaded["sources"]["141"],
            )
            self.assertNotIn("operations", loaded)
            self.assertNotIn("last_exception", loaded)
            self.assertTrue(
                run_state.should_full_reconcile(
                    loaded,
                    24,
                    ["141"],
                )
            )

    def test_required_missing_run_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "missing.json"
            with (
                patch.dict(os.environ, {"RUN_STATE_REQUIRED": "1"}),
                self.assertRaises(run_state.RunStateIntegrityError),
            ):
                run_state.load_run_state(path)

    def test_empty_source_requires_two_consecutive_successful_runs(self):
        state = run_state.default_run_state()
        state["sources"]["141"] = {
            "observed_ids": ["old-1", "old-2"],
            "last_item_count": 2,
        }
        empty = source_result(
            "141",
            SourceStatus.VALID_EMPTY,
            [],
        )
        empty.reconcile_requested = True
        empty.coverage_complete = True
        empty.reconcile_complete = True
        report = CrawlReport([empty])

        run_state.update_state_from_report(
            state,
            report,
            full_reconcile=True,
            applied_source_ids={"141"},
            counters=SyncCounters(
                observation_run_id="run-1",
            ),
        )

        source_state = state["sources"]["141"]
        self.assertEqual(
            source_state["observed_ids"],
            ["old-1", "old-2"],
        )
        self.assertEqual(source_state["last_item_count"], 2)
        self.assertEqual(source_state["empty_observation_count"], 1)
        self.assertNotIn(
            "last_coverage_reconcile_at",
            source_state,
        )
        self.assertTrue(
            run_state.source_reconcile_due(
                state,
                "141",
                24,
            )
        )

        state["runs"] = [{"run_id": "run-1"}]
        run_state.update_state_from_report(
            state,
            report,
            full_reconcile=True,
            applied_source_ids={"141"},
            counters=SyncCounters(
                observation_run_id="run-2",
            ),
        )

        source_state = state["sources"]["141"]
        self.assertEqual(source_state["observed_ids"], [])
        self.assertEqual(source_state["last_item_count"], 0)
        self.assertEqual(source_state["empty_observation_count"], 2)
        self.assertIn(
            "last_coverage_reconcile_at",
            source_state,
        )
        self.assertFalse(
            run_state.source_reconcile_due(
                state,
                "141",
                24,
            )
        )

    def test_reconcile_watermark_advances_per_source(self):
        old = "2026-07-20T00:00:00+00:00"
        state = run_state.default_run_state()
        state["sources"] = {
            "141": {
                "observed_ids": ["141-old"],
                "last_coverage_reconcile_at": old,
            },
            "2": {
                "observed_ids": ["2-old"],
            },
        }
        incremental = source_result(
            "141",
            SourceStatus.SUCCESS,
            ["141-new"],
        )
        incremental.reconcile_requested = False
        reconciled = source_result(
            "2",
            SourceStatus.SUCCESS,
            ["2-new"],
        )
        reconciled.reconcile_requested = True
        reconciled.coverage_complete = True
        reconciled.reconcile_complete = False
        fixed_now = "2026-07-27T00:00:00+00:00"

        with patch.object(
            run_state,
            "utc_now_iso",
            return_value=fixed_now,
        ):
            run_state.update_state_from_report(
                state,
                CrawlReport([incremental, reconciled]),
                full_reconcile=True,
                applied_source_ids={"141", "2"},
            )

        self.assertEqual(
            state["sources"]["141"]["last_coverage_reconcile_at"],
            old,
        )
        self.assertEqual(
            state["sources"]["2"]["last_coverage_reconcile_at"],
            fixed_now,
        )
        self.assertEqual(state["last_coverage_reconcile_at"], old)

    def test_host_circuit_result_does_not_extend_existing_expiration(self):
        expires_at = "2026-07-28T06:00:00+00:00"
        state = run_state.default_run_state()
        state["sources"]["141"] = {
            "source_circuit_open_until": expires_at,
            "source_circuit_reason": "rate_limited",
        }
        result = source_result(
            "141",
            SourceStatus.FAILED,
            [],
            error="rate_limited",
            category=FailureCategory.SECURITY_POLICY,
        )
        result.method = "host_circuit_open"
        result.termination_reason = "circuit_open"
        result.retry_after_seconds = 18000.0

        run_state.update_state_from_report(
            state,
            CrawlReport([result]),
            full_reconcile=False,
            applied_source_ids=set(),
        )

        self.assertEqual(
            state["sources"]["141"]["source_circuit_open_until"],
            expires_at,
        )
        self.assertEqual(
            state["sources"]["141"]["source_circuit_reason"],
            "rate_limited",
        )


if __name__ == "__main__":
    unittest.main()
