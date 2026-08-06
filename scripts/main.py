import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from bbs_parser import parse_rows
from common import extract_detail_id_from_text
from crawler import (
    build_source_spec,
    crawl_sources,
    get_backfill_detail_limit,
)
from log import LOGGER, log_environment_info, setup_logging
from models import (
    CrawlReport,
    DestinationConsistencyError,
    FailureCategory,
    LocalConfigurationError,
    MutationKind,
    RunRecord,
    SourceCrawlResult,
    SourceStatus,
    SyncCounters,
    ValidationIssue,
    utc_now_iso,
)
from refresh_policy import select_due_notice_ids
from run_control import (
    install_run_control,
    require_destination_state_reserve,
)
from run_lock import exclusive_run_lock
from run_state import (
    append_run_record,
    build_incident,
    classify_exception,
    clear_active_incidents,
    create_run_record,
    known_ids_for_source,
    load_run_state,
    materialize_reconcile_local_dates,
    mark_failure_signaled,
    mark_exception_failure,
    should_full_reconcile,
    source_reconcile_schedule,
    update_state_from_report,
    write_json_atomic,
    write_run_state_atomic,
)
from settings import (
    get_bbs_config_fk,
    get_bbs_config_fks,
    get_full_reconcile_local_hour,
    get_incident_path,
    get_run_state_path,
    get_snapshot_path,
    is_writer_context_confirmed,
    load_dotenv,
    resolve_html_path,
    should_allow_notion_schema_migration,
    should_run_dry_run,
    should_run_notion_schema_migration_only,
    should_use_incremental_crawl,
)
from sync_engine import (
    apply_report,
    build_dry_run_plan,
    inspect_destination_pending_context,
    prepare_destination,
    safe_source_results,
)
from validation import validate_crawl_report


def select_refresh_ids(
    source_state: dict[str, Any],
    known_ids: set[str],
    now: Optional[datetime] = None,
) -> list[str]:
    return select_due_notice_ids(source_state, known_ids, now)


def pending_shrink_ids(
    state: dict[str, Any],
    source_id: str,
) -> list[str]:
    candidates = state.get("shrink_candidates", {})
    if not isinstance(candidates, dict):
        return []
    prefix = f"{source_id}:"
    shrink_ids = [
        key[len(prefix):]
        for key in candidates
        if isinstance(key, str)
        and key.startswith(prefix)
        and key[len(prefix):]
    ]
    source_state = state.get("sources", {}).get(source_id, {})
    pending_ids = (
        source_state.get("pending_notice_ids", [])
        if isinstance(source_state, dict)
        else []
    )
    if not isinstance(pending_ids, list):
        pending_ids = []
    return list(
        dict.fromkeys(
            [
                *(
                    str(value)
                    for value in pending_ids
                    if str(value).strip()
                ),
                *shrink_ids,
            ]
        )
    )[:get_backfill_detail_limit()]


def pending_notice_ids(
    state: dict[str, Any],
    source_id: str,
) -> list[str]:
    source_state = state.get("sources", {}).get(source_id, {})
    if not isinstance(source_state, dict):
        return []
    values = source_state.get("pending_notice_ids", [])
    if not isinstance(values, list):
        return []
    return list(
        dict.fromkeys(
            str(value).strip()
            for value in values
            if str(value).strip()
        )
    )[:get_backfill_detail_limit()]


def should_refresh_destination_pending_state(
    state: dict[str, Any],
) -> bool:
    active_incidents = state.get("active_incidents", {})
    if isinstance(active_incidents, dict) and active_incidents:
        return True
    runs = state.get("runs", [])
    if not isinstance(runs, list) or not runs:
        return False
    latest = runs[-1]
    return bool(
        isinstance(latest, dict)
        and str(latest.get("status") or "")
        in {"failed", "partial_failed"}
    )


def refresh_destination_pending_notice_state(
    state: dict[str, Any],
    token: str,
    database_id: str,
    configured_source_ids: set[str],
) -> int:
    context = inspect_destination_pending_context(
        token,
        database_id,
    )
    pending_page_ids = set(context.pending_page_ids)
    if (
        set(context.pending_page_sources) != pending_page_ids
        or set(context.pending_page_notices) != pending_page_ids
    ):
        raise DestinationConsistencyError(
            "대기 페이지 식별 정보가 완전하지 않습니다"
        )
    unknown_sources = (
        set(context.pending_page_sources.values())
        - configured_source_ids
    )
    if unknown_sources:
        raise DestinationConsistencyError(
            "설정에 없는 출처의 대기 페이지가 있습니다"
        )
    sources = state.get("sources")
    if not isinstance(sources, dict):
        raise DestinationConsistencyError(
            "실행 상태의 출처 정보를 신뢰할 수 없습니다"
        )
    for page_id in sorted(pending_page_ids):
        source_id = context.pending_page_sources[page_id]
        notice_id = context.pending_page_notices[page_id]
        source_state = sources.setdefault(source_id, {})
        if not isinstance(source_state, dict):
            raise DestinationConsistencyError(
                "실행 상태의 출처 정보를 신뢰할 수 없습니다"
            )
        existing = source_state.get("pending_notice_ids", [])
        if not isinstance(existing, list):
            raise DestinationConsistencyError(
                "실행 상태의 대기 공지 ID를 신뢰할 수 없습니다"
            )
        pending_notice_ids = sorted(
            {
                *(
                    str(value)
                    for value in existing
                    if str(value).strip()
                ),
                notice_id,
            }
        )
        if len(pending_notice_ids) > 1000:
            raise DestinationConsistencyError(
                "출처별 대기 공지 상태가 보존 한도를 초과했습니다"
            )
        source_state["pending_notice_ids"] = pending_notice_ids
    if pending_page_ids:
        LOGGER.info(
            "이전 실패의 Notion 대기 공지를 수집 계획에 반영했습니다: %s",
            len(pending_page_ids),
        )
    return len(pending_page_ids)


def backfill_resume_page(source_state: object) -> int:
    if not isinstance(source_state, dict):
        return 1
    try:
        return max(1, int(source_state.get("backfill_resume_page") or 1))
    except (TypeError, ValueError):
        return 1


def external_download_incident_summary(
    counters: Optional[SyncCounters],
) -> str:
    if (
        counters is None
        or counters.external_download_status_code not in {403, 429}
    ):
        return ""
    retry_after = (
        counters.external_download_retry_after
        or (
            str(counters.external_download_retry_after_seconds)
            if counters.external_download_retry_after_seconds is not None
            else "-"
        )
    )
    return (
        "외부 파일 다운로드 안전 회로가 열렸습니다: "
        f"상태 코드={counters.external_download_status_code}, "
        f"중단 사유={counters.external_download_stopped_reason}, "
        f"요청 수={counters.external_download_requests}, "
        f"재시도 대기={retry_after}"
    )


def destination_contract_summary(
    counters: Optional[SyncCounters],
) -> str:
    if counters is None or (
        not counters.quarantined_source_ids
        and not counters.unresolved_pending_page_ids
    ):
        return ""
    sources = ",".join(counters.quarantined_source_ids) or "-"
    return (
        "복구되지 않은 Notion 대기 페이지를 출처별로 격리했습니다: "
        f"출처={sources}, "
        f"대기 페이지={len(counters.unresolved_pending_page_ids)}"
    )


def validate_destination_hold_counters(
    counters: Optional[SyncCounters],
) -> None:
    if counters is None:
        return
    unresolved_count = len(counters.unresolved_pending_page_ids)
    observation_count = len(
        counters.destination_hold_observations
    )
    hold_count = counters.destination_hold_count
    repeated_count = counters.repeated_destination_hold_count
    if (
        bool(counters.quarantined_source_ids) != bool(unresolved_count)
        or hold_count != unresolved_count
        or hold_count != observation_count
        or repeated_count < 0
        or repeated_count > hold_count
    ):
        raise DestinationConsistencyError(
            "목적지 안전 보류 집계의 일관성을 확인할 수 없습니다"
        )


def add_destination_quarantine_issues(
    report: CrawlReport,
    source_ids: list[str],
    *,
    fatal: bool = True,
) -> None:
    existing = {
        issue.source_config_fk
        for issue in report.issues
        if issue.code == "destination_pending_quarantine"
    }
    for source_id in source_ids:
        if source_id in existing:
            continue
        report.issues.append(
            ValidationIssue(
                code="destination_pending_quarantine",
                message=(
                    "복구되지 않은 Notion 대기 페이지 때문에 "
                    f"출처를 격리했습니다: {source_id}"
                ),
                source_config_fk=source_id,
                fatal=fatal,
            )
        )


def should_emit_github_annotation() -> bool:
    enabled = os.environ.get(
        "CRAWLER_ACTIONS_ANNOTATIONS",
        "1",
    ).strip().lower()
    return (
        os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"
        and enabled not in {"0", "false", "no", "off"}
    )


def report_destination_safety_hold(
    summary: str,
    *,
    dry_run: bool = False,
) -> None:
    mode = "드라이런" if dry_run else "쓰기 실행"
    LOGGER.warning(
        "Notion 안전 재확인 대기: 모드=%s, %s",
        mode,
        summary,
    )
    if should_emit_github_annotation():
        print(
            "::warning title=Notion 안전 재확인 대기::"
            f"{summary} 다음 독립 실행에서 다시 확인합니다."
        )


def collect_report(
    state: dict[str, Any],
    full_reconcile: bool,
    force_all_reconcile: bool = False,
    record_reconcile_attempts: bool = False,
) -> CrawlReport:
    html_path = resolve_html_path()
    if html_path is None:
        config_fks = get_bbs_config_fks()
        materialize_reconcile_local_dates(state, config_fks)
        reconcile_local_hour = get_full_reconcile_local_hour()
        known_ids_by_source = {
            config_fk: known_ids_for_source(state, config_fk)
            for config_fk in config_fks
        }
        source_states = state.get("sources", {})
        reconcile_schedule_by_source = {
            config_fk: source_reconcile_schedule(
                state,
                config_fk,
                reconcile_local_hour,
            )
            for config_fk in config_fks
        }
        reconcile_by_source = {
            config_fk: bool(
                full_reconcile
                and (
                    force_all_reconcile
                    or reconcile_schedule_by_source[config_fk][0]
                )
            )
            for config_fk in config_fks
        }
        if record_reconcile_attempts:
            reconcile_attempt_at = utc_now_iso()
            for config_fk, reconcile_requested in reconcile_by_source.items():
                if not reconcile_requested:
                    continue
                source_state = source_states.setdefault(config_fk, {})
                if isinstance(source_state, dict):
                    source_state["last_reconcile_attempt_at"] = (
                        reconcile_attempt_at
                    )
        incremental_by_source = {
            config_fk: bool(
                not force_all_reconcile
                and (
                    not reconcile_by_source[config_fk]
                    or known_ids_by_source[config_fk]
                    or (
                        isinstance(source_states.get(config_fk), dict)
                        and source_states[config_fk].get("backfill_active")
                    )
                )
            )
            for config_fk in config_fks
        }
        scheduled_refresh_ids_by_source = {
            config_fk: select_refresh_ids(
                source_states.get(config_fk, {}),
                known_ids_by_source[config_fk],
            )
            if (
                known_ids_by_source[config_fk]
                and isinstance(source_states.get(config_fk), dict)
            )
            else []
            for config_fk in config_fks
        }
        refresh_ids_by_source = {
            config_fk: set(
                [
                    *pending_shrink_ids(state, config_fk),
                    *scheduled_refresh_ids_by_source[config_fk],
                ]
            )
            for config_fk in config_fks
        }
        targeted_refresh_ids_by_source = {
            config_fk: set(
                pending_notice_ids(state, config_fk)
            )
            for config_fk in config_fks
        }
        resume_pages_by_source = {
            config_fk: (
                backfill_resume_page(
                    source_states.get(config_fk, {})
                )
                if reconcile_by_source[config_fk]
                else 1
            )
            for config_fk in config_fks
        }
        for config_fk in config_fks:
            source_state = source_states.get(config_fk, {})
            backfill_active = bool(
                isinstance(source_state, dict)
                and source_state.get("backfill_active")
            )
            LOGGER.info(
                "수집 계획: 출처=%s, 모드=%s, 상세 한도=%s, "
                "시작 페이지=%s, 백필=%s, 최근 조정 시도=%s, "
                "보강 판정=%s, 다음 보강 가능=%s, 재확인 대상=%s",
                config_fk,
                (
                    "과거 보강"
                    if reconcile_by_source[config_fk]
                    else "증분"
                ),
                (
                    get_backfill_detail_limit()
                    if reconcile_by_source[config_fk]
                    else "-"
                ),
                resume_pages_by_source[config_fk],
                (
                    "진행"
                    if reconcile_by_source[config_fk] and backfill_active
                    else ("대기" if backfill_active else "-")
                ),
                (
                    str(
                        source_state.get("last_reconcile_attempt_at")
                        or source_state.get("last_success_at")
                        or "-"
                    )
                    if isinstance(source_state, dict)
                    else "-"
                ),
                (
                    "강제 전체 조정"
                    if force_all_reconcile
                    else reconcile_schedule_by_source[config_fk][2]
                ),
                (
                    "강제 실행"
                    if force_all_reconcile
                    else reconcile_schedule_by_source[config_fk][1]
                ),
                len(refresh_ids_by_source[config_fk]),
            )
        report: CrawlReport = crawl_sources(
            known_ids_by_source=known_ids_by_source,
            source_state_by_source=source_states,
            incremental=not full_reconcile,
            incremental_by_source=incremental_by_source,
            reconcile_mode=full_reconcile,
            reconcile_mode_by_source=reconcile_by_source,
            refresh_ids_by_source=refresh_ids_by_source,
            resume_page_by_source=resume_pages_by_source,
            resume_anchor_ids_by_source={
                config_fk: (
                    {
                        str(value)
                        for value in source_states.get(
                            config_fk,
                            {},
                        ).get("backfill_anchor_ids", [])
                        if str(value)
                    }
                    if (
                        reconcile_by_source[config_fk]
                        and isinstance(
                            source_states.get(config_fk),
                            dict,
                        )
                        and isinstance(
                            source_states[config_fk].get(
                                "backfill_anchor_ids"
                            ),
                            list,
                        )
                    )
                    else set()
                )
                for config_fk in config_fks
            },
            targeted_refresh_ids_by_source=(
                targeted_refresh_ids_by_source
            ),
        )
        return report
    if not html_path.exists():
        raise RuntimeError(f"HTML 파일을 찾을 수 없습니다: {html_path}")
    config_fk = get_bbs_config_fk()
    source = build_source_spec(config_fk)
    html_text = html_path.read_text(encoding="utf-8", errors="replace")
    items = parse_rows(html_text, config_fk)
    if not items:
        return CrawlReport(
            sources=[
                SourceCrawlResult(
                    source=source,
                    status=SourceStatus.FAILED,
                    method="local_html",
                    category=FailureCategory.SOURCE_CONTRACT,
                    error="local_html_no_rows",
                )
            ]
        )
    observed_ids = [
        detail_id
        for item in items
        if (
            detail_id := extract_detail_id_from_text(
                str(item.get("url") or "")
            )
        )
    ]
    return CrawlReport(
        sources=[
            SourceCrawlResult(
                source=source,
                status=SourceStatus.PARTIAL,
                items=[
                    {
                        **item,
                        "completeness": "partial",
                    }
                    for item in items
                ],
                method="local_html",
                observed_count=len(items),
                observed_ids=observed_ids,
                category=FailureCategory.SOURCE_PARTIAL,
                error="local_html_detail_unverified",
                detail_failures=len(items),
            )
        ]
    )


def snapshot_payload(
    report: CrawlReport,
    run_id: str,
    full_reconcile: bool,
    dry_run: bool,
    plan: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": utc_now_iso(),
        "full_reconcile": full_reconcile,
        "dry_run": dry_run,
        "report": report.to_dict(),
    }
    if plan is not None:
        payload["plan"] = plan
    return payload


def report_failure_summary(report: CrawlReport) -> str:
    fragments = [
        (
            f"{result.source.config_fk}:{result.status.value}:"
            f"{result.category.value}:{result.error or '-'}"
        )
        for result in report.sources
        if not result.write_safe
    ]
    fragments.extend(
        f"{issue.source_config_fk or 'global'}:{issue.code}:{issue.message}"
        for issue in report.issues
        if issue.fatal
    )
    return "; ".join(fragments) or "수집 결과가 쓰기 안전 기준을 충족하지 못했습니다"


def source_run_payload(result: SourceCrawlResult) -> dict[str, Any]:
    return {
        "source_id": result.source.config_fk,
        "classification": result.source.classification,
        "status": result.status.value,
        "category": result.category.value,
        "method": result.method,
        "pages_scanned": result.pages_scanned,
        "observed_count": result.observed_count,
        "item_count": len(result.items),
        "notice_observation_count": len(result.notice_observations),
        "detailed_notice_count": len(result.detailed_notice_ids),
        "refreshed_known_count": len(result.refreshed_known_ids),
        "notice_index_complete": result.notice_index_complete,
        "detail_failures": result.detail_failures,
        "rejected_count": result.rejected_count,
        "checkpoint_found": result.checkpoint_found,
        "terminal_reached": result.terminal_reached,
        "termination_reason": result.termination_reason,
        "full_snapshot": result.full_snapshot,
        "reconcile_requested": result.reconcile_requested,
        "error": result.error,
    }


def persist_failed_run(
    state: dict[str, Any],
    record: RunRecord,
    exc: BaseException,
    report: Optional[CrawlReport],
    state_path: Path,
    incident_path: Path,
) -> tuple[dict[str, Any], bool]:
    mark_exception_failure(state, exc)
    category = classify_exception(exc)
    record.finished_at = utc_now_iso()
    record.status = "failed"
    record.failure_category = category
    if report is not None:
        record.source_results = [
            source_run_payload(result) for result in report.sources
        ]
    incident = build_incident(
        state,
        category,
        "서강대 공지 동기화 실패",
        str(exc),
        report,
        exception=exc,
    )
    deduplicated = apply_failure_signal_policy(state, incident)
    append_run_record(state, record)
    write_run_state_atomic(state_path, state)
    write_json_atomic(incident_path, incident)
    return incident, deduplicated


def should_deduplicate_scheduled_failure_notice(
    incident: dict[str, Any],
) -> bool:
    return (
        os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"
        and os.environ.get("GITHUB_EVENT_NAME", "").strip() == "schedule"
        and os.environ.get("GITHUB_RUN_ATTEMPT", "").strip() == "1"
        and not bool(incident.get("should_signal_failure", True))
    )


def apply_failure_signal_policy(
    state: dict[str, Any],
    incident: dict[str, Any],
) -> bool:
    deduplicated = should_deduplicate_scheduled_failure_notice(incident)
    if not deduplicated and not mark_failure_signaled(state, incident):
        raise RuntimeError("반복 실패 상태를 기록할 수 없습니다")
    return deduplicated


def report_deduplicated_failure(incident: dict[str, Any]) -> None:
    category = str(incident.get("category") or "unknown")
    count = int(incident.get("count") or 1)
    LOGGER.warning(
        "동일한 예약 실행 실패 알림을 중복 기록하지 않습니다. "
        "실행 결과는 실패로 유지됩니다: "
        "유형=%s, 누적=%s",
        category,
        count,
    )
    if should_emit_github_annotation():
        print(
            "::warning title=반복 실패 지속::"
            "동일 장애 알림 지문은 유지했으며 Actions 실행 결과는 실패로 기록합니다. "
            f"유형={category}, 누적={count}"
        )


def validate_destination_write_authorization(
    dry_run: bool,
    schema_migration_only: bool,
) -> None:
    if dry_run:
        return
    if not is_writer_context_confirmed():
        raise LocalConfigurationError(
            "허용된 GitHub Actions 쓰기 실행 문맥이 아니어서 "
            "Notion 변경을 차단합니다",
            "destination_contract",
        )
    if (
        schema_migration_only
        and os.environ.get("GITHUB_EVENT_NAME", "").strip()
        != "workflow_dispatch"
    ):
        raise LocalConfigurationError(
            "스키마 변경은 수동 GitHub Actions 실행에서만 허용됩니다",
            "destination_contract",
        )
    if schema_migration_only:
        return


def main() -> None:
    setup_logging()
    load_dotenv()
    log_environment_info()
    install_run_control()
    dry_run = should_run_dry_run()
    schema_migration_only = should_run_notion_schema_migration_only()
    incremental_enabled = should_use_incremental_crawl()
    state_path = get_run_state_path()
    snapshot_path = get_snapshot_path()
    incident_path = get_incident_path()
    lock_path = state_path.with_name("run.lock")
    deferred_error: Optional[RuntimeError] = None
    deferred_incident: Optional[dict[str, Any]] = None
    deferred_failure_deduplicated = False

    with exclusive_run_lock(lock_path):
        state = load_run_state(state_path)
        configured_source_ids = get_bbs_config_fks()
        materialize_reconcile_local_dates(state, configured_source_ids)
        full_reconcile = (
            not incremental_enabled
            or should_full_reconcile(
                state,
                get_full_reconcile_local_hour(),
                configured_source_ids,
            )
        )
        record = create_run_record(full_reconcile, dry_run)
        report: Optional[CrawlReport] = None
        try:
            validate_destination_write_authorization(
                dry_run,
                schema_migration_only,
            )
            if schema_migration_only:
                if dry_run:
                    raise LocalConfigurationError(
                        "스키마 마이그레이션 전용 모드와 드라이런을 동시에 실행할 수 없습니다",
                        "destination_contract",
                    )
                if not should_allow_notion_schema_migration():
                    raise LocalConfigurationError(
                        "스키마 마이그레이션 전용 모드에는 "
                        "NOTION_SCHEMA_MIGRATION=1이 필요합니다",
                        "destination_contract",
                    )
                notion_token = os.environ.get("NOTION_TOKEN")
                database_id = os.environ.get("NOTION_DB_ID")
                if not notion_token or not database_id:
                    raise LocalConfigurationError(
                        "NOTION_TOKEN과 NOTION_DB_ID를 환경 변수나 .env에 설정해야 합니다",
                        "destination_auth",
                    )
                prepare_destination(
                    notion_token,
                    database_id,
                    [],
                    recover_pending=False,
                )
                record.finished_at = utc_now_iso()
                record.status = "schema_migration_succeeded"
                append_run_record(state, record)
                write_run_state_atomic(state_path, state)
                LOGGER.info("Notion 스키마 마이그레이션 완료")
                return
            notion_token = (
                os.environ.get("NOTION_TOKEN") or ""
            ).strip()
            database_id = (
                os.environ.get("NOTION_DB_ID") or ""
            ).strip()
            if not dry_run and (not notion_token or not database_id):
                raise LocalConfigurationError(
                    "NOTION_TOKEN과 NOTION_DB_ID를 환경 변수나 .env에 설정해야 합니다",
                    "destination_auth",
                )
            if (
                not dry_run
                and should_refresh_destination_pending_state(state)
            ):
                refresh_destination_pending_notice_state(
                    state,
                    notion_token,
                    database_id,
                    set(configured_source_ids),
                )
            report = validate_crawl_report(
                collect_report(
                    state,
                    full_reconcile,
                    force_all_reconcile=not incremental_enabled,
                    record_reconcile_attempts=not dry_run,
                ),
                state,
                full_reconcile=full_reconcile,
                expected_source_ids=configured_source_ids,
            )
            record.source_results = [
                source_run_payload(result) for result in report.sources
            ]
            write_json_atomic(
                snapshot_path,
                snapshot_payload(
                    report,
                    record.run_id,
                    full_reconcile,
                    dry_run,
                ),
            )

            if dry_run:
                require_destination_state_reserve()
                plan = build_dry_run_plan(
                    record.execution_id,
                    report,
                    notion_token,
                    database_id,
                    full_reconcile,
                    state,
                    logical_run_id=record.run_id,
                )
                dry_run_source_write_safe = report.write_safe
                dry_run_destination_summary = ""
                if plan.quarantined_source_ids:
                    add_destination_quarantine_issues(
                        report,
                        plan.quarantined_source_ids,
                        fatal=False,
                    )
                    dry_run_destination_summary = (
                        "Notion 대기 페이지 격리 계획이 있습니다: "
                        f"출처={','.join(plan.quarantined_source_ids)}"
                    )
                record.planned_writes = plan.write_count
                write_actions = [
                    action
                    for action in plan.actions
                    if action.kind
                    not in {MutationKind.CONFLICT, MutationKind.NOOP}
                ]
                for index, action in enumerate(write_actions[:50], start=1):
                    LOGGER.info(
                        "드라이런 쓰기 계획: %s/%s, 종류=%s, 출처=%s, "
                        "공지=%s, 사유=%s",
                        index,
                        len(write_actions),
                        action.kind.value,
                        action.source_id,
                        action.notice_id,
                        action.reason or "-",
                    )
                if len(write_actions) > 50:
                    LOGGER.info(
                        "드라이런 쓰기 계획 생략: %s건",
                        len(write_actions) - 50,
                    )
                record.finished_at = utc_now_iso()
                record.status = (
                    "dry_run_deferred"
                    if dry_run_destination_summary
                    and dry_run_source_write_safe
                    else (
                        "dry_run_succeeded"
                        if report.write_safe
                        else "dry_run_failed"
                    )
                )
                record.failure_category = (
                    report.failure_category
                )
                append_run_record(state, record)
                write_run_state_atomic(state_path, state)
                write_json_atomic(
                    snapshot_path,
                    snapshot_payload(
                        report,
                        record.run_id,
                        full_reconcile,
                        dry_run,
                        plan.to_dict(),
                    ),
                )
                LOGGER.info(
                    "드라이런 완료: 계획 쓰기=%s, 출처=%s",
                    plan.write_count,
                    len(report.sources),
                )
                if dry_run_destination_summary:
                    report_destination_safety_hold(
                        dry_run_destination_summary,
                        dry_run=True,
                    )
                if not dry_run_source_write_safe:
                    incident = build_incident(
                        state,
                        report.failure_category,
                        "서강대 공지 수집 검증 실패",
                        report_failure_summary(report),
                        report,
                    )
                    deferred_incident = incident
                    write_run_state_atomic(state_path, state)
                    write_json_atomic(incident_path, incident)
                    deferred_error = RuntimeError(
                        report_failure_summary(report)
                    )
            else:
                safe_results = safe_source_results(report)
                counters = None
                if safe_results:
                    require_destination_state_reserve()
                    counters = apply_report(
                        notion_token,
                        database_id,
                        report,
                        full_reconcile,
                        previous_state=state,
                        run_id=record.execution_id,
                        logical_run_id=record.run_id,
                    )
                elif report.write_safe:
                    raise RuntimeError("동기화할 출처가 없습니다")
                source_report_write_safe = report.write_safe
                source_failure_category = report.failure_category
                source_failure_summary = (
                    ""
                    if source_report_write_safe
                    else report_failure_summary(report)
                )
                validate_destination_hold_counters(counters)
                destination_summary = destination_contract_summary(
                    counters
                )
                repeated_destination_hold_count = (
                    counters.repeated_destination_hold_count
                    if counters is not None
                    else 0
                )
                repeated_destination_hold = bool(
                    destination_summary
                    and repeated_destination_hold_count
                )
                if destination_summary and counters is not None:
                    add_destination_quarantine_issues(
                        report,
                        counters.quarantined_source_ids,
                        fatal=repeated_destination_hold,
                    )
                external_download_summary = (
                    external_download_incident_summary(counters)
                )
                if external_download_summary:
                    report.issues.append(
                        ValidationIssue(
                            code="external_download_circuit",
                            message=external_download_summary,
                        )
                    )
                destination_safety_deferred = bool(
                    destination_summary
                    and not repeated_destination_hold
                    and source_report_write_safe
                    and not external_download_summary
                )
                quarantined_source_ids = (
                    set(counters.quarantined_source_ids)
                    if counters is not None
                    else set()
                )
                update_state_from_report(
                    state,
                    report,
                    full_reconcile,
                    (
                        set()
                        if external_download_summary
                        else {
                            result.source.config_fk
                            for result in safe_results
                            if result.source.config_fk
                            not in quarantined_source_ids
                        }
                    ),
                    counters,
                    safety_deferred=destination_safety_deferred,
                )
                record.finished_at = utc_now_iso()
                record.status = (
                    "safety_deferred"
                    if destination_safety_deferred
                    else (
                        "succeeded"
                        if report.write_safe
                        and not destination_summary
                        and not external_download_summary
                        else "partial_failed"
                    )
                )
                record.failure_category = (
                    FailureCategory.SECURITY_POLICY
                    if external_download_summary
                    else (
                        source_failure_category
                        if source_failure_summary
                        else (
                            FailureCategory.DESTINATION_CONTRACT
                            if repeated_destination_hold
                            else report.failure_category
                        )
                    )
                )
                append_run_record(state, record, counters)
                if destination_summary and not repeated_destination_hold:
                    report_destination_safety_hold(
                        destination_summary
                    )
                repeated_summary = (
                    (
                        f"{destination_summary}; "
                        "다음 독립 실행에도 남은 대기 항목="
                        f"{repeated_destination_hold_count}"
                    )
                    if repeated_destination_hold
                    else ""
                )
                failure_components = [
                    (label, summary)
                    for label, summary in (
                        ("수집 검증", source_failure_summary),
                        ("외부 파일", external_download_summary),
                        ("Notion 안전 보류", repeated_summary),
                    )
                    if summary
                ]
                if failure_components:
                    combined_failure_summary = (
                        "; ".join(
                            f"{label}: {summary}"
                            for label, summary in failure_components
                        )
                        if len(failure_components) > 1
                        else failure_components[0][1]
                    )
                    if len(failure_components) > 1:
                        incident_title = "서강대 공지 동기화 복합 실패"
                    elif external_download_summary:
                        incident_title = (
                            "서강대 외부 파일 다운로드 안전 차단"
                        )
                    elif source_failure_summary:
                        incident_title = "서강대 공지 부분 동기화 실패"
                    else:
                        incident_title = (
                            "Notion 대기 페이지 안전 보류 지속"
                        )
                    incident = build_incident(
                        state,
                        record.failure_category,
                        incident_title,
                        combined_failure_summary,
                        report,
                    )
                    deferred_incident = incident
                    write_json_atomic(incident_path, incident)
                    deferred_error = (
                        DestinationConsistencyError(
                            combined_failure_summary
                        )
                        if repeated_summary
                        and not source_failure_summary
                        and not external_download_summary
                        else RuntimeError(combined_failure_summary)
                    )
                else:
                    recovered_count = clear_active_incidents(state)
                    incident_path.unlink(missing_ok=True)
                    if recovered_count:
                        LOGGER.info(
                            "이전 실패 상태 %s건이 정상 실행으로 해소됐습니다",
                            recovered_count,
                        )
                write_json_atomic(
                    snapshot_path,
                    snapshot_payload(
                        report,
                        record.run_id,
                        full_reconcile,
                        dry_run,
                    ),
                )
                write_run_state_atomic(state_path, state)
                if counters is not None:
                    LOGGER.info(
                        "동기화 완료: 생성=%s, 속성=%s, 본문=%s, TOP해제=%s, "
                        "무변경=%s, 미디어보류=%s, 전체쓰기=%s",
                        counters.created,
                        counters.property_updates,
                        counters.body_updates,
                        counters.top_disabled,
                        counters.unchanged,
                        counters.media_deferred,
                        counters.writes,
                    )
            if deferred_error is not None and deferred_incident is not None:
                deferred_failure_deduplicated = apply_failure_signal_policy(
                    state,
                    deferred_incident,
                )
                write_run_state_atomic(state_path, state)
                write_json_atomic(incident_path, deferred_incident)
        except Exception as exc:
            incident, failure_deduplicated = persist_failed_run(
                state,
                record,
                exc,
                report,
                state_path,
                incident_path,
            )
            if failure_deduplicated:
                LOGGER.warning(
                    "전체 동기화 실패가 반복됐습니다",
                    exc_info=True,
                )
                report_deduplicated_failure(incident)
            LOGGER.exception("전체 동기화 실패")
            raise

    if deferred_error is not None:
        if deferred_failure_deduplicated and deferred_incident is not None:
            report_deduplicated_failure(deferred_incident)
        LOGGER.error("동기화 안전 차단: %s", deferred_error)
        raise deferred_error


if __name__ == "__main__":
    main()
