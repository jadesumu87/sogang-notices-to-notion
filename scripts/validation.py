import os
from collections import Counter
from typing import Any

from common import (
    ATTACHMENTS_STATUS_KNOWN,
    extract_verified_detail_url_identity,
)
from models import CrawlReport, ItemCompleteness, SourceStatus, ValidationIssue
from utils import normalize_detail_url


def validate_crawl_report(
    report: CrawlReport,
    previous_state: dict[str, Any],
    full_reconcile: bool = False,
    expected_source_ids: list[str] | None = None,
) -> CrawlReport:
    issues: list[ValidationIssue] = []
    min_ratio_raw = os.environ.get("SOURCE_MIN_COUNT_RATIO", "0.25")
    try:
        min_ratio = min(1.0, max(0.0, float(min_ratio_raw)))
    except ValueError:
        min_ratio = 0.25

    configured = [result.source.config_fk for result in report.sources]
    expected = list(dict.fromkeys(expected_source_ids or configured))
    configured_set = set(configured)
    expected_set = set(expected)
    for config_fk in expected:
        if config_fk not in configured_set:
            issues.append(
                ValidationIssue(
                    code="missing_source_result",
                    message=f"설정된 출처의 수집 결과가 없습니다: {config_fk}",
                    source_config_fk=config_fk,
                )
            )
    for config_fk in configured:
        if config_fk not in expected_set:
            issues.append(
                ValidationIssue(
                    code="unexpected_source_result",
                    message=f"설정에 없는 출처의 수집 결과가 있습니다: {config_fk}",
                    source_config_fk=config_fk,
                )
            )
    duplicates = [key for key, count in Counter(configured).items() if count > 1]
    for config_fk in duplicates:
        issues.append(
            ValidationIssue(
                code="duplicate_source",
                message=f"중복 출처 설정: {config_fk}",
                source_config_fk=config_fk,
            )
        )

    all_urls: dict[str, str] = {}
    for result in report.sources:
        config_fk = result.source.config_fk
        seen_notice_ids: set[str] = set()
        seen_source_urls: set[str] = set()
        reconcile_requested = (
            full_reconcile
            if result.reconcile_requested is None
            else result.reconcile_requested
        )
        if result.source.required and not result.write_safe:
            issues.append(
                ValidationIssue(
                    code="required_source_unsafe",
                    message=(
                        f"필수 출처 수집 불완전: {config_fk} "
                        f"({result.status.value}, {result.category.value})"
                    ),
                    source_config_fk=config_fk,
                )
            )
        if (
            reconcile_requested
            and result.write_safe
            and not result.coverage_complete
            and result.termination_reason != "backfill_window"
        ):
            issues.append(
                ValidationIssue(
                    code="reconcile_incomplete",
                    message=(
                        f"전체 조정 순회가 끝나지 않았습니다: "
                        f"{config_fk}"
                    ),
                    source_config_fk=config_fk,
                )
            )
        if (
            reconcile_requested
            and result.write_safe
            and result.coverage_complete
            and not result.full_snapshot
        ):
            issues.append(
                ValidationIssue(
                    code="atomic_snapshot_unavailable",
                    message=(
                        f"오프셋 페이지는 목록 전체 확인 기준으로 사용하지 않습니다: "
                        f"{config_fk}"
                    ),
                    source_config_fk=config_fk,
                    fatal=False,
                )
            )
        previous_source = previous_state.get("sources", {}).get(config_fk, {})
        previous_count = (
            int(previous_source.get("last_item_count") or 0)
            if isinstance(previous_source, dict)
            else 0
        )
        current_count = result.observed_count
        if result.status == SourceStatus.VALID_EMPTY:
            issues.append(
                ValidationIssue(
                    code="unexpected_empty_source",
                    message=(
                        f"빈 출처는 명시적 허용 전까지 쓰기에서 제외됩니다: "
                        f"{config_fk} (이전 관측={previous_count})"
                    ),
                    source_config_fk=config_fk,
                )
            )
        if (
            result.status == SourceStatus.SUCCESS
            and result.coverage_complete
            and previous_count >= 5
            and current_count < previous_count * min_ratio
        ):
            issues.append(
                ValidationIssue(
                    code="source_count_collapse",
                    message=(
                        f"출처 항목 수 급감: {config_fk} "
                        f"{previous_count} -> {current_count}"
                    ),
                    source_config_fk=config_fk,
                )
            )
        for item in result.items:
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            explicit_notice_id = str(
                item.get("notice_id") or ""
            ).strip()
            classification = str(item.get("classification") or "").strip()
            completeness = str(item.get("completeness") or "").strip()
            if completeness != ItemCompleteness.COMPLETE.value:
                issues.append(
                    ValidationIssue(
                        code="incomplete_item",
                        message=f"불완전한 공지 항목: {config_fk}",
                        source_config_fk=config_fk,
                    )
                )
            body_status = str(item.get("body_status") or "")
            body_blocks = item.get("body_blocks") or []
            body_contract_valid = (
                body_status == "present"
                and bool(body_blocks)
            ) or (
                body_status == "confirmed_empty"
                and not body_blocks
            )
            if not body_contract_valid:
                issues.append(
                    ValidationIssue(
                        code="body_completeness_invalid",
                        message=f"본문 상태가 불완전합니다: {config_fk}",
                        source_config_fk=config_fk,
                    )
                )
            if (
                item.get("attachments_status")
                != ATTACHMENTS_STATUS_KNOWN
                or item.get("attachments_truncated")
            ):
                issues.append(
                    ValidationIssue(
                        code="attachment_completeness_invalid",
                        message=f"첨부파일 상태가 불완전합니다: {config_fk}",
                        source_config_fk=config_fk,
                    )
                )
            if not title or not url:
                issues.append(
                    ValidationIssue(
                        code="invalid_item_identity",
                        message=f"제목 또는 URL이 없는 항목: {config_fk}",
                        source_config_fk=config_fk,
                    )
                )
                continue
            verified_notice_id = extract_verified_detail_url_identity(
                url,
                config_fk,
            )
            notice_id = explicit_notice_id or verified_notice_id or ""
            if (
                verified_notice_id is None
                or (
                    explicit_notice_id
                    and verified_notice_id != explicit_notice_id
                )
            ):
                issues.append(
                    ValidationIssue(
                        code="notice_identity_mismatch",
                        message=(
                            f"공지 식별자와 출처 URL이 일치하지 않습니다: "
                            f"{config_fk}, {notice_id or '-'}"
                        ),
                        source_config_fk=config_fk,
                    )
                )
            if notice_id in seen_notice_ids:
                issues.append(
                    ValidationIssue(
                        code="duplicate_notice_id",
                        message=(
                            f"같은 출처가 중복 공지 ID를 반환했습니다: "
                            f"{config_fk}, {notice_id or '-'}"
                        ),
                        source_config_fk=config_fk,
                    )
                )
            else:
                seen_notice_ids.add(notice_id)
            canonical_url = normalize_detail_url(url) or url
            if canonical_url in seen_source_urls:
                issues.append(
                    ValidationIssue(
                        code="duplicate_source_url",
                        message=(
                            f"같은 출처가 중복 공지 URL을 반환했습니다: "
                            f"{config_fk}, {url}"
                        ),
                        source_config_fk=config_fk,
                    )
                )
            else:
                seen_source_urls.add(canonical_url)
            if (
                result.source.classification
                and classification != result.source.classification
            ):
                issues.append(
                    ValidationIssue(
                        code="classification_mismatch",
                        message=(
                            f"출처 분류 불일치: {config_fk} "
                            f"{classification or '-'}"
                        ),
                        source_config_fk=config_fk,
                    )
                )
            previous_config = all_urls.get(canonical_url)
            if previous_config and previous_config != config_fk:
                issues.append(
                    ValidationIssue(
                        code="cross_source_url_collision",
                        message=(
                            f"서로 다른 출처가 같은 URL을 반환: "
                            f"{previous_config}, {config_fk}, {url}"
                        ),
                        source_config_fk=config_fk,
                    )
                )
            all_urls[canonical_url] = config_fk
        if result.top_snapshot_verified:
            seen_top_ids: set[str] = set()
            top_snapshot_valid = True
            for url in result.top_urls:
                top_notice_id = extract_verified_detail_url_identity(
                    str(url),
                    config_fk,
                )
                if top_notice_id is None:
                    issues.append(
                        ValidationIssue(
                            code="invalid_top_snapshot_url",
                            message=(
                                f"TOP 공지 URL이 올바르지 않습니다: "
                                f"{config_fk}"
                            ),
                            source_config_fk=config_fk,
                            fatal=False,
                        )
                    )
                    top_snapshot_valid = False
                    continue
                if top_notice_id in seen_top_ids:
                    issues.append(
                        ValidationIssue(
                            code="duplicate_top_snapshot_id",
                            message=(
                                f"TOP 스냅샷 중복 공지 ID: "
                                f"{config_fk}, {top_notice_id}"
                            ),
                            source_config_fk=config_fk,
                            fatal=False,
                        )
                    )
                    top_snapshot_valid = False
                else:
                    seen_top_ids.add(top_notice_id)
            for item in result.items:
                if not item.get("top"):
                    continue
                notice_id = (
                    str(item.get("notice_id") or "").strip()
                    or extract_verified_detail_url_identity(
                        str(item.get("url") or ""),
                        config_fk,
                    )
                    or ""
                )
                if notice_id not in seen_top_ids:
                    issues.append(
                        ValidationIssue(
                            code="top_snapshot_item_mismatch",
                            message=(
                                f"TOP 항목이 검증 스냅샷에 없습니다: "
                                f"{config_fk}, {notice_id or '-'}"
                            ),
                            source_config_fk=config_fk,
                            fatal=False,
                        )
                    )
                    top_snapshot_valid = False
            if not top_snapshot_valid:
                result.top_snapshot_verified = False

    report.issues = issues
    return report
