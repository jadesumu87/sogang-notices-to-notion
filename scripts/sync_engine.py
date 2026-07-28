import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from common import (
    ATTACHMENTS_STATUS_KNOWN,
    ensure_item_title,
    extract_detail_id_from_text,
    extract_verified_detail_url_identity,
)
from log import LOGGER
from models import (
    CrawlReport,
    DestinationConsistencyError,
    ItemCompleteness,
    MutationAction,
    MutationKind,
    MutationPlan,
    SourceCrawlResult,
    SyncCounters,
)
from notion_client import (
    collect_attachment_content_state,
    collect_body_media_content_state,
    create_page,
    ensure_destination_schema,
    external_download_run_scope,
    fetch_database,
    prepare_attachments_for_sync,
    prepare_body_blocks_for_sync,
    retrieve_page,
    update_page,
    validate_destination_schema,
)
from run_control import check_run_control, sleep_with_run_control
from settings import (
    ATTACHMENT_PROPERTY,
    ATTACHMENT_STATE_PROPERTY,
    BODY_HASH_IMAGE_MODE_UPLOAD,
    BODY_HASH_PROPERTY,
    BODY_MEDIA_STATE_PROPERTY,
    NOTICE_ID_PROPERTY,
    SOURCE_KEY_PROPERTY,
    SYNC_GENERATION_PROPERTY,
    SYNC_OPERATION_PROPERTY,
    SYNC_STATUS_PROPERTY,
    TOP_PROPERTY,
    should_upload_files_to_notion,
)
from sync import (
    body_generation_property_payload,
    build_properties,
    disable_missing_top,
    enrich_attachment_state_with_page,
    enrich_attachment_state_with_properties,
    enrich_body_media_state_with_block_ids,
    extract_attachment_state,
    extract_body_generation_id,
    extract_body_generation_manifest,
    extract_body_media_state,
    extract_existing_uploaded_attachment_ids,
    extract_rich_text_value,
    extract_type_from_title,
    filter_changed_properties,
    find_existing_page,
    is_body_generation_current,
    is_empty_body_generation_current,
    is_managed_page,
    inspect_pending_pages,
    inspect_missing_top,
    inspect_existing_uploaded_media_blocks,
    managed_page_fingerprint,
    normalize_notion_hosted_file_key,
    normalize_attachment_state_entries,
    normalize_item_attachments,
    split_body_container_parts,
    sync_container_content_hash,
    sync_page_body_blocks,
    top_level_quote_state,
    top_candidate_fingerprints,
    validate_body_write_payloads,
    validate_top_disable_candidates,
)
from utils import (
    build_rich_text_chunks,
    compute_body_hash,
    has_image_blocks,
    normalize_attachment_name,
    normalize_attachment_identity_url,
    normalize_body_blocks_for_hash,
    normalize_content_sha256,
    normalize_detail_url,
)


@dataclass
class DestinationContext:
    token: str
    database_id: str
    has_views_property: bool = True
    has_attachments_property: bool = True
    has_classification_property: bool = True
    has_attachment_state_property: bool = True
    has_body_hash_property: bool = True
    pending_page_ids: tuple[str, ...] = ()
    pending_page_sources: dict[str, str] = field(default_factory=dict)
    pending_page_notices: dict[str, str] = field(default_factory=dict)


@dataclass
class PendingOperation:
    page_id: str = ""
    operation_id: str = ""
    active: bool = False


COMMIT_READBACK_DELAYS = (0.0, 0.25, 0.75)


@dataclass
class DestinationPreflight:
    item: dict[str, Any]
    existing_page: Optional[dict[str, Any]]
    operation_id: str
    shrink_key: str
    shrink_candidate: Optional[dict[str, Any]]
    page_fingerprint: str = ""
    attachment_content_state: list[dict[str, Any]] = field(
        default_factory=list
    )
    body_media_content_state: list[dict[str, Any]] = field(
        default_factory=list
    )
    body_media_reuse_status: str = "valid"
    quote_fingerprint: str = ""
    allow_untracked_body_recovery: bool = False

    def __post_init__(self) -> None:
        if not self.page_fingerprint:
            self.page_fingerprint = managed_page_fingerprint(
                self.existing_page
            )


def log_destination_progress(
    stage: str,
    index: int,
    total: int,
    item: dict[str, Any],
) -> None:
    LOGGER.info(
        "목적지 %s 진행: %s/%s, 출처=%s, 공지=%s",
        stage,
        index,
        total,
        str(item.get("source_id") or "-"),
        str(item.get("notice_id") or "-"),
    )


def body_sync_requested(item: dict[str, Any]) -> bool:
    return bool(
        item.get("body_blocks")
        or str(item.get("body_status") or "") == "confirmed_empty"
    )


def body_blocks_for_quote_safety(
    item: dict[str, Any],
) -> list[dict[str, Any]]:
    blocks = item.get("body_blocks")
    if isinstance(blocks, list) and blocks:
        return blocks
    if str(item.get("body_status") or "") == "confirmed_empty":
        return [
            {
                "type": "paragraph",
                "paragraph": {"rich_text": []},
            }
        ]
    return []


def untracked_body_recovery_allowed(
    page: Optional[dict[str, Any]],
    operation_id: str,
) -> bool:
    if not page:
        return False
    properties = page.get("properties")
    if not isinstance(properties, dict):
        return False
    return bool(
        extract_rich_text_value(
            properties,
            SYNC_STATUS_PROPERTY,
        )
        == "pending"
        and extract_rich_text_value(
            properties,
            SYNC_OPERATION_PROPERTY,
        )
        == operation_id
        and extract_body_generation_manifest(properties) is None
    )


def destination_quote_fingerprint(
    token: str,
    page: dict[str, Any],
    *,
    allow_untracked_recovery: bool,
    expected_body_blocks: list[dict[str, Any]],
) -> str:
    state = top_level_quote_state(token, page)
    untracked = [
        entry
        for entry in state
        if not bool(entry.get("managed"))
    ]
    if untracked:
        if not allow_untracked_recovery:
            raise DestinationConsistencyError(
                "관리되지 않는 최상위 인용 블록이 있어 본문 동기화를 중단합니다"
            )
        container_rich_text, body_chunks = split_body_container_parts(
            expected_body_blocks
        )
        expected_hash = sync_container_content_hash(
            token,
            container_rich_text,
            [
                child
                for chunk in body_chunks
                for child in chunk
            ],
            False,
        )
        if (
            len(untracked) != 1
            or str(untracked[0].get("content_hash") or "")
            != expected_hash
        ):
            raise DestinationConsistencyError(
                "기존 미완료 본문이 현재 작업과 정확히 일치하지 않습니다"
            )
    return hashlib.sha256(
        json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def desired_attachment_identity(
    entries: list[Any],
) -> list[tuple[str, int, str]]:
    identities: list[tuple[str, int, str]] = []
    name_counts: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("첨부 항목 형식이 올바르지 않습니다")
        name = str(entry.get("name") or "").strip()
        name_key = normalize_attachment_name(name)
        occurrence = name_counts.get(name_key, 0) + 1
        name_counts[name_key] = occurrence
        payload = entry.get("external")
        source_url = (
            str(payload.get("url") or "").strip()
            if isinstance(payload, dict)
            else str(entry.get("source_url") or "").strip()
        )
        identities.append(
            (
                name_key,
                occurrence,
                normalize_attachment_identity_url(source_url),
            )
        )
    return identities


def existing_attachment_identity(
    properties: dict[str, Any],
    entries: list[Any],
) -> tuple[list[tuple[str, int, str]], bool]:
    state = extract_attachment_state(properties)
    if state and len(state) == len(entries):
        normalized_state = normalize_attachment_state_entries(state)
        state_identities: list[tuple[str, int, str]] = []
        state_name_counts: dict[str, int] = {}
        reliable = True
        for state_entry, current_entry in zip(
            normalized_state,
            entries,
            strict=True,
        ):
            if not isinstance(current_entry, dict):
                reliable = False
                break
            current_name_key = normalize_attachment_name(
                str(current_entry.get("name") or "")
            )
            occurrence = state_name_counts.get(current_name_key, 0) + 1
            state_name_counts[current_name_key] = occurrence
            state_name_key = normalize_attachment_name(
                str(state_entry.get("name") or "")
            )
            state_occurrence = int(
                state_entry.get("occurrence") or 0
            )
            state_source_identity = normalize_attachment_identity_url(
                str(state_entry.get("source_url") or "")
            )
            if (
                not current_name_key
                or current_name_key != state_name_key
                or occurrence != state_occurrence
                or not state_source_identity
            ):
                reliable = False
                break
            current_type = str(
                current_entry.get("type") or ""
            ).strip()
            if current_type == "external":
                current_payload = current_entry.get("external")
                current_source_identity = (
                    normalize_attachment_identity_url(
                        str(current_payload.get("url") or "")
                    )
                    if isinstance(current_payload, dict)
                    else ""
                )
                if not current_source_identity:
                    reliable = False
                    break
                identity = current_source_identity
            elif current_type == "file":
                current_payload = current_entry.get("file")
                current_hosted_key = (
                    normalize_notion_hosted_file_key(
                        str(current_payload.get("url") or "")
                    )
                    if isinstance(current_payload, dict)
                    else ""
                )
                state_hosted_key = str(
                    state_entry.get("hosted_file_key") or ""
                ).strip()
                if not state_hosted_key or not current_hosted_key:
                    reliable = False
                    break
                identity = (
                    state_source_identity
                    if state_hosted_key == current_hosted_key
                    else f"notion-hosted:{current_hosted_key}"
                )
            elif current_type == "file_upload":
                current_payload = current_entry.get("file_upload")
                current_upload_id = (
                    str(current_payload.get("id") or "").strip()
                    if isinstance(current_payload, dict)
                    else ""
                )
                state_upload_id = str(
                    state_entry.get("upload_id") or ""
                ).strip()
                state_hosted_key = str(
                    state_entry.get("hosted_file_key") or ""
                ).strip()
                if (
                    not current_upload_id
                    or not state_upload_id
                    or not state_hosted_key
                ):
                    reliable = False
                    break
                identity = (
                    state_source_identity
                    if current_upload_id == state_upload_id
                    else f"notion-upload:{current_upload_id}"
                )
            else:
                reliable = False
                break
            state_identities.append(
                (
                    current_name_key,
                    occurrence,
                    identity,
                )
            )
        if reliable and len(state_identities) == len(entries):
            return state_identities, True
        return state_identities, False
    identities: list[tuple[str, int, str]] = []
    name_counts: dict[str, int] = {}
    reliable = True
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("Notion 첨부 속성 형식이 올바르지 않습니다")
        name = str(entry.get("name") or "").strip()
        name_key = normalize_attachment_name(name)
        occurrence = name_counts.get(name_key, 0) + 1
        name_counts[name_key] = occurrence
        payload = entry.get("external")
        source_url = (
            str(payload.get("url") or "").strip()
            if isinstance(payload, dict)
            else ""
        )
        if not source_url:
            reliable = False
        identities.append(
            (
                name_key,
                occurrence,
                normalize_attachment_identity_url(source_url),
            )
        )
    return identities, reliable


def should_preserve_existing_top(
    existing_page: Optional[dict[str, Any]],
    item: dict[str, Any],
) -> bool:
    if not existing_page or bool(item.get("top")):
        return False
    top_property = existing_page.get("properties", {}).get(
        TOP_PROPERTY,
        {},
    )
    return top_property.get("checkbox") is True


def shrink_candidate_for_item(
    _token: str,
    item: dict[str, Any],
    existing_page: Optional[dict[str, Any]],
    body_media_content_state: Optional[
        list[dict[str, Any]]
    ] = None,
) -> Optional[dict[str, Any]]:
    if not existing_page:
        return None
    properties = existing_page.get("properties", {})
    attachment_property = properties.get(ATTACHMENT_PROPERTY, {})
    current_attachments = attachment_property.get("files", [])
    if not isinstance(current_attachments, list):
        raise RuntimeError("Notion 첨부 속성 형식이 올바르지 않습니다")
    desired_attachments_raw = item.get("attachments") or []
    if not isinstance(desired_attachments_raw, list):
        raise RuntimeError("수집 첨부 형식이 올바르지 않습니다")
    desired_attachments = desired_attachments_raw
    reasons: list[str] = []
    current_attachment_identity: list[tuple[str, int, str]] = []
    current_attachment_identity_reliable = True
    if "attachments" in item:
        desired_identity = desired_attachment_identity(
            desired_attachments
        )
        (
            current_attachment_identity,
            current_attachment_identity_reliable,
        ) = existing_attachment_identity(properties, current_attachments)
        if desired_identity != current_attachment_identity:
            reasons.append(
                "attachment_identity_changed"
                if current_attachment_identity_reliable
                else "attachment_identity_unverified"
            )
    body_blocks = item.get("body_blocks") or []
    desired_body_hash = compute_body_hash(
        (
            normalize_body_blocks_for_hash(
                body_blocks,
                should_upload_files_to_notion(),
                media_content_state=body_media_content_state,
            )
            if body_blocks
            else []
        ),
        image_mode=(
            BODY_HASH_IMAGE_MODE_UPLOAD
            if should_upload_files_to_notion()
            and has_image_blocks(body_blocks)
            else ""
        ),
    )
    existing_body_hash = extract_rich_text_value(
        properties,
        BODY_HASH_PROPERTY,
    )
    if (
        body_blocks
        or str(item.get("body_status") or "") == "confirmed_empty"
    ) and desired_body_hash != existing_body_hash:
        reasons.append("body_hash_changed")
    if not reasons:
        return None
    candidate_payload = {
        "source_id": str(item.get("source_id") or ""),
        "notice_id": str(item.get("notice_id") or ""),
        "body_status": str(item.get("body_status") or ""),
        "body_hash": desired_body_hash,
        "existing_body_hash": existing_body_hash,
        "existing_attachment_identity": current_attachment_identity,
        "attachments": [
            {
                "name": str(attachment.get("name") or ""),
                "url": str(
                    normalize_attachment_identity_url(
                        attachment.get("external", {}).get("url") or ""
                    )
                ),
            }
            for attachment in desired_attachments
        ],
        "reasons": sorted(set(reasons)),
    }
    return {
        "candidate_id": hashlib.sha256(
            json.dumps(
                candidate_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "reasons": sorted(set(reasons)),
    }


def source_issue_codes(report: CrawlReport, source_id: str) -> set[str]:
    return {
        issue.code
        for issue in report.issues
        if issue.fatal and issue.source_config_fk == source_id
    }


def safe_source_results(report: CrawlReport) -> list[SourceCrawlResult]:
    global_codes = {"duplicate_source", "cross_source_url_collision"}
    if any(issue.fatal and issue.code in global_codes for issue in report.issues):
        return []
    return [
        result
        for result in report.sources
        if result.write_safe
        and not source_issue_codes(report, result.source.config_fk)
    ]


def notice_id_for_item(item: dict[str, Any]) -> str:
    explicit = str(item.get("notice_id") or "").strip()
    if explicit:
        return explicit
    return extract_detail_id_from_text(str(item.get("url") or "")) or ""


def prepare_source_items(
    result: SourceCrawlResult,
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for raw_item in result.items:
        item = dict(raw_item)
        completeness = str(item.get("completeness") or "").strip()
        if completeness != ItemCompleteness.COMPLETE.value:
            raise RuntimeError(
                f"불완전한 공지 동기화 차단: 출처={result.source.config_fk}"
            )
        body_status = str(item.get("body_status") or "")
        body_blocks = item.get("body_blocks") or []
        if (
            body_status == "present"
            and not body_blocks
        ) or (
            body_status == "confirmed_empty"
            and body_blocks
        ) or body_status not in {"present", "confirmed_empty"}:
            raise RuntimeError(
                f"본문 완전성 검사 실패: 출처={result.source.config_fk}"
            )
        if body_blocks:
            validate_body_write_payloads(body_blocks)
        if (
            item.get("attachments_status")
            != ATTACHMENTS_STATUS_KNOWN
            or item.get("attachments_truncated")
        ):
            raise RuntimeError(
                f"첨부 완전성 검사 실패: 출처={result.source.config_fk}"
            )
        item["source_id"] = result.source.config_fk
        item["notice_id"] = notice_id_for_item(item)
        if not item["notice_id"]:
            raise RuntimeError(
                f"공지 ID 누락: 출처={result.source.config_fk}, URL={item.get('url') or '-'}"
            )
        if not item.get("classification"):
            item["classification"] = result.source.classification
        if item.get("url"):
            item["url"] = normalize_detail_url(str(item["url"]))
        if not item.get("url"):
            raise RuntimeError(
                f"공지 URL 누락: 출처={result.source.config_fk}, 공지 ID={item['notice_id']}"
            )
        ensure_item_title(item, item.get("body_blocks", []), item["url"])
        item["type"] = extract_type_from_title(item["title"])
        prepared.append(item)
    return prepared


def operation_id_for_item(
    item: dict[str, Any],
    attachment_state: Optional[list[dict[str, Any]]] = None,
    body_media_state: Optional[list[dict[str, Any]]] = None,
    attachment_entries: Optional[list[dict[str, Any]]] = None,
) -> str:
    attachments = [
        {
            "name": entry.get("name"),
            "type": entry.get("type"),
            "url": (
                normalize_attachment_identity_url(
                    entry.get("external", {}).get("url")
                )
                if entry.get("type") == "external"
                else ""
            ),
        }
        for entry in (
            attachment_entries
            if attachment_entries is not None
            else item.get("attachments") or []
        )
    ]
    payload = {
        "source_id": item.get("source_id"),
        "notice_id": item.get("notice_id"),
        "title": item.get("title"),
        "date": item.get("date"),
        "author": item.get("author"),
        "views": item.get("views"),
        "top": bool(item.get("top")),
        "classification": item.get("classification"),
        "url": item.get("url"),
        "attachments": attachments,
        "attachment_content": [
            {
                "source_url": normalize_attachment_identity_url(
                    str(entry.get("source_url") or "")
                ),
                "name": str(entry.get("name") or ""),
                "occurrence": int(entry.get("occurrence") or 0),
                "content_sha256": normalize_content_sha256(
                    entry.get("content_sha256")
                ),
            }
            for entry in normalize_attachment_state_entries(
                attachment_state or []
            )
            if normalize_content_sha256(
                entry.get("content_sha256")
            )
        ],
        "body": normalize_body_blocks_for_hash(
            item.get("body_blocks") or [],
            True,
        ),
        "body_media_content": [
            {
                "type": str(entry.get("type") or ""),
                "source_url": normalize_attachment_identity_url(
                    str(entry.get("source_url") or "")
                ),
                "content_sha256": normalize_content_sha256(
                    entry.get("content_sha256")
                ),
            }
            for entry in body_media_state or []
            if normalize_content_sha256(
                entry.get("content_sha256")
            )
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def state_without_generation(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            key: value
            for key, value in entry.items()
            if key != "generation_id"
        }
        for entry in entries
    ]


def next_body_generation_id(
    operation_id: str,
    desired_body_hash: str,
    existing_manifest: Optional[dict[str, Any]],
    existing_body_hash: str,
) -> str:
    if (
        existing_manifest
        and existing_manifest.get("v") == 2
        and existing_manifest.get("s") == "pending"
        and existing_manifest.get("op") == operation_id
    ):
        pending_generation = str(
            existing_manifest.get("g") or ""
        ).strip()
        if pending_generation:
            return pending_generation
    existing_generation = (
        str(existing_manifest.get("g") or "").strip()
        if existing_manifest
        else ""
    )
    seed = "\0".join(
        (
            "body-generation-v2",
            operation_id,
            desired_body_hash,
            existing_generation or existing_body_hash,
        )
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()




def attachment_state_identity(
    entries: list[dict[str, Any]],
) -> list[tuple[str, str, int, str, str]]:
    return [
        (
            normalize_attachment_identity_url(
                str(entry.get("source_url") or "")
            ),
            str(entry.get("name") or ""),
            int(entry.get("occurrence") or 0),
            str(entry.get("upload_id") or ""),
            normalize_content_sha256(entry.get("content_sha256")),
        )
        for entry in normalize_attachment_state_entries(entries)
    ]


def attachment_content_identity(
    entries: list[dict[str, Any]],
) -> list[tuple[str, str, int, str]]:
    return [
        (
            normalize_attachment_identity_url(
                str(entry.get("source_url") or "")
            ),
            str(entry.get("name") or ""),
            int(entry.get("occurrence") or 0),
            normalize_content_sha256(entry.get("content_sha256")),
        )
        for entry in normalize_attachment_state_entries(entries)
    ]


def body_media_state_identity(
    entries: list[dict[str, Any]],
) -> list[tuple[str, str, str, str]]:
    return [
        (
            str(entry.get("type") or ""),
            normalize_attachment_identity_url(
                str(entry.get("source_url") or "")
            ),
            str(entry.get("upload_id") or ""),
            normalize_content_sha256(entry.get("content_sha256")),
        )
        for entry in entries
    ]


def body_media_content_identity(
    entries: list[dict[str, Any]],
) -> list[tuple[str, str, str]]:
    return [
        (
            str(entry.get("type") or ""),
            normalize_attachment_identity_url(
                str(entry.get("source_url") or "")
            ),
            normalize_content_sha256(entry.get("content_sha256")),
        )
        for entry in entries
    ]


def external_attachment_signature(
    entries: list[dict[str, Any]],
) -> list[tuple[int, str, int, str]]:
    signature: list[tuple[int, str, int, str]] = []
    name_counts: dict[str, int] = {}
    for position, entry in enumerate(entries):
        name = str(entry.get("name") or "")
        name_key = normalize_attachment_name(name)
        occurrence = name_counts.get(name_key, 0) + 1
        name_counts[name_key] = occurrence
        if str(entry.get("type") or "") != "external":
            continue
        payload = entry.get("external")
        if not isinstance(payload, dict):
            continue
        signature.append(
            (
                position,
                name,
                occurrence,
                str(payload.get("url") or ""),
            )
        )
    return signature


def pending_page_identity(
    page: dict[str, Any],
) -> tuple[str, str, str]:
    page_id = str(page.get("id") or "").strip()
    properties = page.get("properties")
    if not page_id or not isinstance(properties, dict):
        raise DestinationConsistencyError(
            "대기 페이지의 식별 정보를 신뢰할 수 없습니다"
        )
    source_id = extract_rich_text_value(
        properties,
        SOURCE_KEY_PROPERTY,
    )
    notice_id = extract_rich_text_value(
        properties,
        NOTICE_ID_PROPERTY,
    )
    if not source_id or not notice_id:
        raise DestinationConsistencyError(
            "대기 페이지의 출처 또는 공지 ID가 누락되었습니다"
        )
    return page_id, source_id, notice_id


def pending_page_context(
    pages: list[dict[str, Any]],
) -> tuple[tuple[str, ...], dict[str, str], dict[str, str]]:
    page_ids: list[str] = []
    sources: dict[str, str] = {}
    notices: dict[str, str] = {}
    identities: set[tuple[str, str]] = set()
    for page in pages:
        page_id, source_id, notice_id = pending_page_identity(page)
        identity = (source_id, notice_id)
        if page_id in sources or identity in identities:
            raise DestinationConsistencyError(
                "대기 페이지 식별자가 중복되어 출처별로 격리할 수 없습니다"
            )
        page_ids.append(page_id)
        sources[page_id] = source_id
        notices[page_id] = notice_id
        identities.add(identity)
    return tuple(page_ids), sources, notices


def prepare_destination(
    token: str,
    database_id: str,
    items: list[dict[str, Any]],
    *,
    recover_pending: bool = True,
) -> DestinationContext:
    database = fetch_database(token, database_id)
    database = ensure_destination_schema(
        token,
        database_id,
        database,
    )
    validate_destination_schema(database)
    pending_pages = (
        inspect_pending_pages(token, database_id)
        if recover_pending
        else []
    )
    pending_ids, pending_sources, pending_notices = pending_page_context(
        pending_pages
    )
    return DestinationContext(
        token=token,
        database_id=database_id,
        pending_page_ids=pending_ids,
        pending_page_sources=pending_sources,
        pending_page_notices=pending_notices,
    )


def existing_state(
    token: str,
    page: Optional[dict[str, Any]],
) -> tuple[
    str,
    list[dict[str, Any]],
    str,
    list[dict[str, Any]],
    str,
]:
    if not page:
        return "", [], "", [], ""
    properties = page.get("properties", {})
    page_id = str(page.get("id") or "").strip()
    body_hash = extract_rich_text_value(properties, BODY_HASH_PROPERTY)
    media_raw = extract_rich_text_value(properties, BODY_MEDIA_STATE_PROPERTY)
    media_state = extract_body_media_state(properties)
    if page_id and media_state and any(
        not str(entry.get("block_id") or "").strip()
        or not str(entry.get("hosted_file_key") or "").strip()
        for entry in media_state
    ):
        media_state = enrich_body_media_state_with_block_ids(
            token,
            page_id,
            media_state,
        )
    attachment_raw = extract_rich_text_value(
        properties,
        ATTACHMENT_STATE_PROPERTY,
    )
    attachment_state = extract_attachment_state(properties)
    if attachment_state and any(
        not str(entry.get("hosted_file_key") or "").strip()
        for entry in attachment_state
    ):
        attachment_state = enrich_attachment_state_with_properties(
            properties,
            attachment_state,
        )
    return (
        body_hash,
        media_state,
        media_raw,
        attachment_state,
        attachment_raw,
    )


def committed_item_readback_reasons(
    token: str,
    page: dict[str, Any],
    item: dict[str, Any],
    context: DestinationContext,
    page_id: str,
    operation_id: str,
    generation_id: str,
    desired_body_hash: str,
    attachment_state: list[dict[str, Any]],
    body_media_state: list[dict[str, Any]],
    preserve_top: bool,
) -> list[str]:
    reasons: list[str] = []
    source_id = str(item["source_id"])
    notice_id = str(item["notice_id"])
    if str(page.get("id") or "").strip() != page_id:
        reasons.append("page_id")
    if bool(page.get("in_trash")):
        reasons.append("in_trash")
    if not is_managed_page(page, source_id, notice_id):
        reasons.append("managed_identity")
    properties = page.get("properties", {})
    if not isinstance(properties, dict):
        return [*reasons, "properties"]
    expected_item = dict(item)
    expected_item["operation_id"] = operation_id
    expected_item["generation_id"] = generation_id
    expected_item["sync_status"] = "committed"
    expected_properties = build_properties(
        expected_item,
        context.has_views_property,
        context.has_attachments_property,
        context.has_classification_property,
    )
    expected_properties.pop(ATTACHMENT_PROPERTY, None)
    expected_properties.pop(SYNC_GENERATION_PROPERTY, None)
    if preserve_top:
        expected_properties.pop(TOP_PROPERTY, None)
    if filter_changed_properties(properties, expected_properties):
        reasons.append("properties")
    body_blocks = item.get("body_blocks") or []
    body_confirmed_empty = (
        str(item.get("body_status") or "") == "confirmed_empty"
    )
    if body_blocks or body_confirmed_empty:
        if (
            extract_rich_text_value(properties, BODY_HASH_PROPERTY)
            != desired_body_hash
        ):
            reasons.append("body_hash")
        if (
            body_blocks
            and not is_body_generation_current(
                token,
                page_id,
                generation_id,
            )
        ):
            reasons.append("body_generation")
        if (
            body_confirmed_empty
            and not is_empty_body_generation_current(
                token,
                page_id,
                generation_id,
            )
        ):
            reasons.append("empty_body_generation")
        actual_body_media_state = extract_body_media_state(properties)
        if (
            body_media_state_identity(actual_body_media_state)
            != body_media_state_identity(body_media_state)
        ):
            reasons.append("body_media_state")
        if any(
            str(entry.get("generation_id") or "") != generation_id
            for entry in actual_body_media_state
        ):
            reasons.append("body_media_generation")
    if "attachments" in item:
        actual_attachment_state = extract_attachment_state(properties)
        if (
            attachment_state_identity(actual_attachment_state)
            != attachment_state_identity(attachment_state)
        ):
            reasons.append("attachment_state")
        if any(
            str(entry.get("generation_id") or "") != generation_id
            for entry in actual_attachment_state
        ):
            reasons.append("attachment_generation")
        desired_files = item.get("attachments") or []
        if attachment_state:
            if not extract_existing_uploaded_attachment_ids(
                properties,
                actual_attachment_state,
            ):
                reasons.append("attachment_binding")
        elif filter_changed_properties(
            properties,
            {ATTACHMENT_PROPERTY: {"files": desired_files}},
        ):
            reasons.append("attachments")
    return reasons


def verify_committed_item(
    token: str,
    item: dict[str, Any],
    context: DestinationContext,
    page_id: str,
    operation_id: str,
    generation_id: str,
    desired_body_hash: str,
    attachment_state: list[dict[str, Any]],
    body_media_state: list[dict[str, Any]],
    preserve_top: bool,
) -> dict[str, Any]:
    last_reasons: list[str] = ["unread"]
    for delay in COMMIT_READBACK_DELAYS:
        if delay:
            sleep_with_run_control(delay)
        check_run_control()
        page = retrieve_page(token, page_id)
        last_reasons = committed_item_readback_reasons(
            token,
            page,
            item,
            context,
            page_id,
            operation_id,
            generation_id,
            desired_body_hash,
            attachment_state,
            body_media_state,
            preserve_top,
        )
        if not last_reasons:
            return page
    raise DestinationConsistencyError(
        "Notion 확정 상태 재조회 검증에 실패했습니다: "
        f"출처={item['source_id']}, 공지 ID={item['notice_id']}, "
        f"불일치 항목={','.join(last_reasons)}"
    )




def _apply_item(
    context: DestinationContext,
    item: dict[str, Any],
    counters: SyncCounters,
    existing_page: Optional[dict[str, Any]] = None,
    existing_page_resolved: bool = False,
    *,
    pending_operation: PendingOperation,
    expected_operation_id: str = "",
    expected_body_media_reuse_status: str = "valid",
    force_commit_readback: bool = False,
    pre_write_validation: Optional[Callable[[], object]] = None,
) -> None:
    check_run_control()
    writes_before = counters.writes
    pre_write_validated = False

    def validate_before_first_write() -> None:
        nonlocal pre_write_validated
        if pre_write_validated:
            return
        check_run_control()
        if pre_write_validation is not None:
            pre_write_validation()
        pre_write_validated = True

    token = context.token
    database_id = context.database_id
    source_id = str(item["source_id"])
    notice_id = str(item["notice_id"])
    if context.has_attachments_property:
        normalize_item_attachments(item)
    body_blocks = item.get("body_blocks") or []
    body_confirmed_empty = (
        str(item.get("body_status") or "") == "confirmed_empty"
    )
    if body_blocks:
        validate_body_write_payloads(body_blocks)
    if not existing_page_resolved:
        existing_page = find_existing_page(
            token,
            database_id,
            item.get("url"),
            item["title"],
            item.get("date"),
            source_id=source_id,
            notice_id=notice_id,
        )
    page_id = str(existing_page.get("id") or "").strip() if existing_page else ""
    if existing_page and not is_managed_page(
        existing_page,
        source_id,
        notice_id,
    ):
        raise RuntimeError("비관리 페이지는 자동으로 동기화할 수 없습니다")
    (
        existing_hash,
        existing_media_state,
        existing_media_state_raw,
        existing_attachment_state,
        existing_attachment_state_raw,
    ) = existing_state(token, existing_page)
    existing_properties = (
        existing_page.get("properties", {})
        if existing_page
        else {}
    )
    existing_generation_manifest = (
        extract_body_generation_manifest(existing_properties)
        if isinstance(existing_properties, dict)
        else None
    )
    existing_generation_id = (
        extract_body_generation_id(existing_properties)
        if isinstance(existing_properties, dict)
        else ""
    )
    attachment_state: list[dict[str, Any]] = []
    operation_attachment_entries = list(
        item.get("attachments") or []
    )
    if (
        should_upload_files_to_notion()
        and context.has_attachments_property
        and "attachments" in item
    ):
        reusable = (
            extract_existing_uploaded_attachment_ids(
                existing_page.get("properties", {}) if existing_page else {},
                existing_attachment_state,
            )
            if existing_page
            else {}
        )
        item["attachments"], attachment_state = prepare_attachments_for_sync(
            token,
            item["attachments"],
            reusable_uploaded_attachments=reusable or None,
        )
        attachment_state = normalize_attachment_state_entries(attachment_state)
        if (
            existing_page
            and attachment_state_identity(attachment_state)
            == attachment_state_identity(existing_attachment_state)
        ):
            attachment_state = existing_attachment_state
    upload_files = should_upload_files_to_notion()
    image_mode = (
        BODY_HASH_IMAGE_MODE_UPLOAD
        if upload_files and has_image_blocks(body_blocks)
        else ""
    )
    blocks_for_sync: list[dict[str, Any]] = []
    actual_hash_blocks: list[dict[str, Any]] = []
    actual_media_state: list[dict[str, Any]] = []
    body_media_reuse_status = "valid"
    if body_confirmed_empty:
        blocks_for_sync = [
            {
                "type": "paragraph",
                "paragraph": {"rich_text": []},
            }
        ]
    elif body_blocks:
        if upload_files:
            if existing_page:
                (
                    reusable_media,
                    body_media_reuse_status,
                ) = inspect_existing_uploaded_media_blocks(
                    token,
                    page_id,
                    existing_media_state,
                )
                if body_media_reuse_status == "unavailable":
                    raise RuntimeError(
                        "기존 본문 미디어 상태를 검증할 수 없습니다"
                    )
            else:
                reusable_media = {}
            if (
                expected_operation_id
                and body_media_reuse_status
                != expected_body_media_reuse_status
            ):
                raise RuntimeError(
                    "본문 미디어 상태가 사전검증 이후 변경되었습니다"
                )
            (
                blocks_for_sync,
                actual_hash_blocks,
                actual_media_state,
            ) = prepare_body_blocks_for_sync(
                token,
                body_blocks,
                reusable_uploaded_media=reusable_media or None,
            )
        else:
            blocks_for_sync = body_blocks
            actual_hash_blocks = normalize_body_blocks_for_hash(
                body_blocks,
                False,
            )
    if blocks_for_sync:
        validate_body_write_payloads(blocks_for_sync)
    desired_body_hash = (
        compute_body_hash(
            actual_hash_blocks,
            image_mode=image_mode,
        )
        if body_blocks or body_confirmed_empty
        else ""
    )
    operation_id = operation_id_for_item(
        item,
        attachment_state=attachment_state,
        body_media_state=actual_media_state,
        attachment_entries=operation_attachment_entries,
    )
    if (
        expected_operation_id
        and operation_id != expected_operation_id
    ):
        raise RuntimeError(
            "외부 파일 콘텐츠가 사전검증 이후 변경되었습니다"
        )
    desired_item = dict(item)
    existing_operation_id = (
        extract_rich_text_value(
            existing_page.get("properties", {}),
            SYNC_OPERATION_PROPERTY,
        )
        if existing_page
        else ""
    )
    existing_sync_status = (
        extract_rich_text_value(
            existing_page.get("properties", {}),
            SYNC_STATUS_PROPERTY,
        )
        if existing_page
        else ""
    )
    if existing_page and existing_sync_status == "pending":
        pending_operation.page_id = page_id
        pending_operation.operation_id = existing_operation_id
        pending_operation.active = True
    if (
        not existing_page
        or existing_operation_id != operation_id
        or existing_sync_status != "committed"
    ):
        desired_item["operation_id"] = operation_id
        desired_item["sync_status"] = "pending"
    desired_properties = build_properties(
        desired_item,
        context.has_views_property,
        context.has_attachments_property,
        context.has_classification_property,
    )
    preserve_top = should_preserve_existing_top(existing_page, item)
    if preserve_top:
        desired_properties.pop(TOP_PROPERTY, None)
    if existing_page and "attachments" in item:
        if state_without_generation(
            attachment_state
        ) == state_without_generation(
            existing_attachment_state
        ) and external_attachment_signature(
            item.get("attachments") or []
        ) == external_attachment_signature(
            existing_page.get("properties", {})
            .get(ATTACHMENT_PROPERTY, {})
            .get("files", [])
        ):
            desired_properties.pop(ATTACHMENT_PROPERTY, None)
    if existing_page:
        changed = filter_changed_properties(
            existing_page.get("properties", {}),
            desired_properties,
        )
        if changed:
            changed[SYNC_STATUS_PROPERTY] = {
                "rich_text": build_rich_text_chunks("pending")
            }
            changed[SYNC_OPERATION_PROPERTY] = {
                "rich_text": build_rich_text_chunks(operation_id)
            }
        if changed:
            validate_before_first_write()
            update_page(token, page_id, changed)
            pending_operation.page_id = page_id
            pending_operation.operation_id = operation_id
            pending_operation.active = True
            if (
                ATTACHMENT_PROPERTY in changed
                and attachment_state
            ):
                attachment_state = enrich_attachment_state_with_page(
                    token,
                    page_id,
                    attachment_state,
                )
            counters.property_updates += 1
            counters.writes += 1
        base_changed = bool(changed)
    else:
        validate_before_first_write()
        page_id = create_page(token, database_id, desired_properties)
        if not page_id:
            raise RuntimeError(
                f"Notion 페이지 생성 응답 ID 누락: 출처={source_id}, 공지 ID={notice_id}"
            )
        counters.created += 1
        counters.writes += 1
        base_changed = True
        pending_operation.page_id = page_id
        pending_operation.operation_id = operation_id
        pending_operation.active = True

    post_properties: dict[str, Any] = {}
    normalized_existing_media_raw = json.dumps(
        existing_media_state,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if existing_media_state and normalized_existing_media_raw != existing_media_state_raw:
        post_properties[BODY_MEDIA_STATE_PROPERTY] = {
            "rich_text": build_rich_text_chunks(normalized_existing_media_raw)
        }
    normalized_existing_attachment_raw = json.dumps(
        existing_attachment_state,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if (
        existing_attachment_state
        and normalized_existing_attachment_raw != existing_attachment_state_raw
    ):
        post_properties[ATTACHMENT_STATE_PROPERTY] = {
            "rich_text": build_rich_text_chunks(
                normalized_existing_attachment_raw
            )
        }
    if "attachments" in item:
        if attachment_state and not existing_page:
            attachment_state = enrich_attachment_state_with_page(
                token,
                page_id,
                attachment_state,
            )

    body_changed = False
    completed_generation = existing_generation_id or operation_id
    if body_blocks or body_confirmed_empty:
        current_generation = (
            existing_generation_id
            or desired_body_hash
        )
        completed_generation = current_generation
        needs_body_sync = (
            desired_body_hash != existing_hash
            or body_media_reuse_status == "drift"
            or body_media_state_identity(actual_media_state)
            != body_media_state_identity(existing_media_state)
        )
        if (
            body_blocks
            and not needs_body_sync
            and existing_page
        ):
            needs_body_sync = not is_body_generation_current(
                token,
                page_id,
                current_generation,
            )
        if (
            body_confirmed_empty
            and not needs_body_sync
            and existing_page
        ):
            needs_body_sync = not is_empty_body_generation_current(
                token,
                page_id,
                current_generation,
            )
        if needs_body_sync:
            if not pending_operation.active:
                validate_before_first_write()
                update_page(
                    token,
                    page_id,
                    {
                        SYNC_STATUS_PROPERTY: {
                            "rich_text": build_rich_text_chunks(
                                "pending"
                            )
                        },
                        SYNC_OPERATION_PROPERTY: {
                            "rich_text": build_rich_text_chunks(
                                operation_id
                            )
                        },
                    },
                )
                counters.property_updates += 1
                counters.writes += 1
                pending_operation.page_id = page_id
                pending_operation.operation_id = operation_id
                pending_operation.active = True
            validate_before_first_write()
            generation_id = next_body_generation_id(
                operation_id,
                desired_body_hash,
                existing_generation_manifest,
                existing_hash,
            )
            committed_manifest: dict[str, Any] = {}
            completed_generation = sync_page_body_blocks(
                token,
                page_id,
                blocks_for_sync,
                generation_id=generation_id,
                operation_id=operation_id,
                manifest_out=committed_manifest,
                defer_manifest_commit=True,
                allow_untracked_recovery=bool(
                    existing_page
                    and existing_sync_status == "pending"
                    and existing_operation_id == operation_id
                    and existing_generation_manifest is None
                ),
            )
            if completed_generation != generation_id:
                raise RuntimeError("본문 세대 커밋 결과가 요청과 다릅니다")
            actual_media_state = enrich_body_media_state_with_block_ids(
                token,
                page_id,
                actual_media_state,
                generation_id,
            )
            actual_media_state = [
                {**entry, "generation_id": generation_id}
                for entry in actual_media_state
            ]
            post_properties[BODY_HASH_PROPERTY] = {
                "rich_text": build_rich_text_chunks(
                    desired_body_hash
                )
            }
            post_properties[BODY_MEDIA_STATE_PROPERTY] = {
                "rich_text": build_rich_text_chunks(
                    json.dumps(
                        actual_media_state,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
            }
            post_properties[SYNC_GENERATION_PROPERTY] = {
                **body_generation_property_payload(
                    committed_manifest
                )
            }
            counters.body_updates += 1
            counters.writes += 1
            body_changed = True
    if existing_media_state and not body_changed:
        existing_media_state = [
            {**entry, "generation_id": completed_generation}
            for entry in existing_media_state
        ]
        media_state_raw = json.dumps(
            existing_media_state,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if media_state_raw != existing_media_state_raw:
            post_properties[BODY_MEDIA_STATE_PROPERTY] = {
                "rich_text": build_rich_text_chunks(media_state_raw)
            }
    if "attachments" in item:
        attachment_state = [
            {**entry, "generation_id": completed_generation}
            for entry in attachment_state
        ]
        attachment_state_raw = json.dumps(
            attachment_state,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if attachment_state_raw != existing_attachment_state_raw:
            post_properties[ATTACHMENT_STATE_PROPERTY] = {
                "rich_text": build_rich_text_chunks(attachment_state_raw)
            }
    post_properties[SYNC_STATUS_PROPERTY] = {
        "rich_text": build_rich_text_chunks("committed")
    }
    post_properties[SYNC_OPERATION_PROPERTY] = {
        "rich_text": build_rich_text_chunks(operation_id)
    }
    if existing_page:
        post_properties = filter_changed_properties(
            existing_page.get("properties", {}),
            post_properties,
        )
    if pending_operation.active:
        post_properties[SYNC_STATUS_PROPERTY] = {
            "rich_text": build_rich_text_chunks("committed")
        }
        post_properties[SYNC_OPERATION_PROPERTY] = {
            "rich_text": build_rich_text_chunks(operation_id)
        }
    if post_properties and not pending_operation.active:
        validate_before_first_write()
        update_page(
            token,
            page_id,
            {
                SYNC_STATUS_PROPERTY: {
                    "rich_text": build_rich_text_chunks("pending")
                },
                SYNC_OPERATION_PROPERTY: {
                    "rich_text": build_rich_text_chunks(operation_id)
                },
            },
        )
        counters.property_updates += 1
        counters.writes += 1
        pending_operation.page_id = page_id
        pending_operation.operation_id = operation_id
        pending_operation.active = True
        post_properties[SYNC_STATUS_PROPERTY] = {
            "rich_text": build_rich_text_chunks("committed")
        }
        post_properties[SYNC_OPERATION_PROPERTY] = {
            "rich_text": build_rich_text_chunks(operation_id)
        }
    if post_properties:
        validate_before_first_write()
        update_page(token, page_id, post_properties)
        counters.property_updates += 1
        counters.writes += 1
    if counters.writes > writes_before or force_commit_readback:
        verify_committed_item(
            token,
            item,
            context,
            page_id,
            operation_id,
            completed_generation,
            desired_body_hash,
            attachment_state,
            (
                actual_media_state
                if body_changed
                else existing_media_state
            ),
            preserve_top,
        )
        pending_operation.active = False
    if existing_page and not base_changed and not body_changed and not post_properties:
        counters.unchanged += 1


def apply_item(
    context: DestinationContext,
    item: dict[str, Any],
    counters: SyncCounters,
    existing_page: Optional[dict[str, Any]] = None,
    existing_page_resolved: bool = False,
    expected_operation_id: str = "",
    expected_body_media_reuse_status: str = "valid",
    force_commit_readback: bool = False,
    pre_write_validation: Optional[Callable[[], object]] = None,
) -> None:
    pending_operation = PendingOperation(
        operation_id=operation_id_for_item(item)
    )
    _apply_item(
        context,
        item,
        counters,
        existing_page=existing_page,
        existing_page_resolved=existing_page_resolved,
        pending_operation=pending_operation,
        expected_operation_id=expected_operation_id,
        expected_body_media_reuse_status=(
            expected_body_media_reuse_status
        ),
        force_commit_readback=force_commit_readback,
        pre_write_validation=pre_write_validation,
    )


def resolve_destination_preflight(
    context: DestinationContext,
    items: list[dict[str, Any]],
    *,
    atomic_recheck: bool = True,
) -> list[DestinationPreflight]:
    resolved: list[DestinationPreflight] = []
    page_owners: dict[str, str] = {}
    total_items = len(items)
    for item_index, item in enumerate(items, start=1):
        log_destination_progress(
            "사전검증 준비",
            item_index,
            total_items,
            item,
        )
        if context.has_attachments_property:
            normalize_item_attachments(item)
        source_id = str(item["source_id"])
        notice_id = str(item["notice_id"])
        existing_page = find_existing_page(
            context.token,
            context.database_id,
            item.get("url"),
            item["title"],
            item.get("date"),
            source_id=source_id,
            notice_id=notice_id,
        )
        page_id = (
            str(existing_page.get("id") or "").strip()
            if existing_page
            else ""
        )
        attachment_content_state = (
            collect_attachment_content_state(
                item.get("attachments") or []
            )
            if context.has_attachments_property
            else []
        )
        body_blocks = item.get("body_blocks") or []
        body_media_content_state = collect_body_media_content_state(
            body_blocks
        )
        body_media_reuse_status = "valid"
        if (
            existing_page
            and should_upload_files_to_notion()
            and body_blocks
        ):
            _, body_media_reuse_status = (
                inspect_existing_uploaded_media_blocks(
                    context.token,
                    page_id,
                    extract_body_media_state(
                        existing_page.get("properties", {})
                    ),
                )
            )
            if body_media_reuse_status == "unavailable":
                raise RuntimeError(
                    "기존 본문 미디어 상태를 사전검증할 수 없습니다"
                )
        operation_id = operation_id_for_item(
            item,
            attachment_state=attachment_content_state,
            body_media_state=body_media_content_state,
        )
        allow_untracked_recovery = (
            body_sync_requested(item)
            and untracked_body_recovery_allowed(
                existing_page,
                operation_id,
            )
        )
        quote_fingerprint = (
            destination_quote_fingerprint(
                context.token,
                existing_page,
                allow_untracked_recovery=(
                    allow_untracked_recovery
                ),
                expected_body_blocks=body_blocks_for_quote_safety(
                    item
                ),
            )
            if existing_page and body_sync_requested(item)
            else ""
        )
        owner = f"{source_id}:{notice_id}"
        if page_id and page_id in page_owners and page_owners[page_id] != owner:
            raise RuntimeError(
                f"목적지 페이지 대상 충돌: {page_id}"
            )
        if page_id:
            page_owners[page_id] = owner
        shrink_key = owner
        resolved.append(
            DestinationPreflight(
                item=item,
                existing_page=existing_page,
                operation_id=operation_id,
                shrink_key=shrink_key,
                shrink_candidate=shrink_candidate_for_item(
                    context.token,
                    item,
                    existing_page,
                    body_media_content_state=(
                        body_media_content_state
                    ),
                ),
                page_fingerprint=managed_page_fingerprint(existing_page),
                attachment_content_state=attachment_content_state,
                body_media_content_state=body_media_content_state,
                body_media_reuse_status=body_media_reuse_status,
                quote_fingerprint=quote_fingerprint,
                allow_untracked_body_recovery=(
                    allow_untracked_recovery
                ),
            )
        )
    LOGGER.info("목적지 사전검증 준비 완료: 항목=%s", len(resolved))
    plan_hash = hashlib.sha256(
        json.dumps(
            [
                {
                    "source_id": entry.item["source_id"],
                    "notice_id": entry.item["notice_id"],
                    "operation_id": entry.operation_id,
                    "page_id": (
                        str(entry.existing_page.get("id") or "")
                        if entry.existing_page
                        else ""
                    ),
                }
                for entry in resolved
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if atomic_recheck:
        for entry_index, entry in enumerate(resolved, start=1):
            log_destination_progress(
                "원자성 재확인",
                entry_index,
                len(resolved),
                entry.item,
            )
            current = find_existing_page(
                context.token,
                context.database_id,
                entry.item.get("url"),
                entry.item["title"],
                entry.item.get("date"),
                source_id=str(entry.item["source_id"]),
                notice_id=str(entry.item["notice_id"]),
            )
            expected_page_id = (
                str(entry.existing_page.get("id") or "").strip()
                if entry.existing_page
                else ""
            )
            current_page_id = (
                str(current.get("id") or "").strip()
                if current
                else ""
            )
            if current_page_id != expected_page_id:
                raise RuntimeError(
                    "목적지 사전검증 이후 대상이 변경되었습니다: "
                    f"{entry.shrink_key}"
                )
            if managed_page_fingerprint(current) != entry.page_fingerprint:
                raise RuntimeError(
                    "목적지 사전검증 이후 관리 상태가 변경되었습니다: "
                    f"{entry.shrink_key}"
                )
        LOGGER.info(
            "목적지 원자적 사전검증 완료: 항목=%s, 계획=%s",
            len(resolved),
            plan_hash[:16],
        )
    else:
        LOGGER.info(
            "목적지 원자성 재확인 통합: 항목=%s, 계획=%s",
            len(resolved),
            plan_hash[:16],
        )
    return resolved


def validate_destination_preflight_entry(
    context: DestinationContext,
    entry: DestinationPreflight,
) -> Optional[dict[str, Any]]:
    raw_current = find_existing_page(
        context.token,
        context.database_id,
        entry.item.get("url"),
        entry.item["title"],
        entry.item.get("date"),
        source_id=str(entry.item["source_id"]),
        notice_id=str(entry.item["notice_id"]),
    )
    if raw_current is not None and not isinstance(raw_current, dict):
        raise RuntimeError("목적지 조회 응답 형식이 올바르지 않습니다.")
    current: Optional[dict[str, Any]] = raw_current
    expected_page_id = (
        str(entry.existing_page.get("id") or "").strip()
        if entry.existing_page
        else ""
    )
    current_page_id = (
        str(current.get("id") or "").strip()
        if current
        else ""
    )
    if (
        current_page_id != expected_page_id
        or managed_page_fingerprint(current) != entry.page_fingerprint
    ):
        raise RuntimeError(
            "목적지 적용 직전 대상이 변경되었습니다: "
            f"{entry.shrink_key}"
        )
    if current and body_sync_requested(entry.item):
        current_quote_fingerprint = destination_quote_fingerprint(
            context.token,
            current,
            allow_untracked_recovery=(
                entry.allow_untracked_body_recovery
                and untracked_body_recovery_allowed(
                    current,
                    entry.operation_id,
                )
            ),
            expected_body_blocks=body_blocks_for_quote_safety(
                entry.item
            ),
        )
        if current_quote_fingerprint != entry.quote_fingerprint:
            raise RuntimeError(
                "목적지 적용 직전 본문 블록이 변경되었습니다: "
                f"{entry.shrink_key}"
            )
    return current


def refresh_destination_preflight_entry(
    context: DestinationContext,
    entry: DestinationPreflight,
) -> Optional[dict[str, Any]]:
    expected_page_id = (
        str(entry.existing_page.get("id") or "").strip()
        if entry.existing_page
        else ""
    )
    if not expected_page_id:
        return validate_destination_preflight_entry(context, entry)
    current = retrieve_page(context.token, expected_page_id)
    if managed_page_fingerprint(current) != entry.page_fingerprint:
        raise RuntimeError(
            "목적지 적용 직전 대상이 변경되었습니다: "
            f"{entry.shrink_key}"
        )
    return current


def validate_destination_preflight_entries(
    context: DestinationContext,
    entries: list[DestinationPreflight],
) -> None:
    total_entries = len(entries)
    for entry_index, entry in enumerate(entries, start=1):
        log_destination_progress(
            "적용 전 일괄검증",
            entry_index,
            total_entries,
            entry.item,
        )
        validate_destination_preflight_entry(context, entry)
    LOGGER.info(
        "목적지 적용 전 일괄검증 완료: 항목=%s",
        total_entries,
    )


def current_top_notice_ids(result: SourceCrawlResult) -> set[str]:
    notice_ids: set[str] = set()
    for url in result.top_urls:
        notice_id = extract_verified_detail_url_identity(
            url,
            result.source.config_fk,
        )
        if not notice_id:
            raise DestinationConsistencyError(
                "검증된 TOP 공지 URL이 올바르지 않습니다: "
                f"{result.source.config_fk}"
            )
        if notice_id in notice_ids:
            raise DestinationConsistencyError(
                "검증된 TOP 스냅샷에 중복 공지 ID가 있습니다: "
                f"{result.source.config_fk}, {notice_id}"
            )
        notice_ids.add(notice_id)
    return notice_ids


def top_candidate_ids(
    candidates: list[dict[str, Any]],
) -> set[str]:
    return {
        notice_id
        for page in candidates
        if (
            notice_id := extract_rich_text_value(
                page.get("properties", {}),
                NOTICE_ID_PROPERTY,
            )
        )
    }


def nonnegative_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(str(value)))
    except (TypeError, ValueError):
        return 0


def destructive_candidate_ttl_seconds() -> float:
    raw = os.environ.get(
        "DESTRUCTIVE_CANDIDATE_TTL_SECONDS",
        "10800",
    ).strip()
    try:
        value = float(raw)
    except ValueError:
        return 10800.0
    return min(86400.0, max(300.0, value))


def latest_recorded_run_id(state: dict[str, Any]) -> str:
    runs = state.get("runs", [])
    if not isinstance(runs, list) or not runs:
        return ""
    latest = runs[-1]
    if not isinstance(latest, dict):
        return ""
    execution_id = str(latest.get("execution_id") or "").strip()
    if execution_id:
        return execution_id
    run_id = str(latest.get("run_id") or "").strip()
    run_attempt = str(latest.get("run_attempt") or "").strip()
    if run_id and run_attempt:
        return f"{run_id}:{run_attempt}"
    return run_id


def latest_recorded_logical_run_id(state: dict[str, Any]) -> str:
    runs = state.get("runs", [])
    if not isinstance(runs, list) or not runs:
        return ""
    latest = runs[-1]
    return (
        str(latest.get("run_id") or "").strip()
        if isinstance(latest, dict)
        else ""
    )


def recent_consecutive_observation(
    state: dict[str, Any],
    observation: object,
    run_id_key: str,
    observed_at_key: str,
    *,
    current_logical_run_id: str = "",
    logical_run_id_key: str = "",
) -> bool:
    if not isinstance(observation, dict):
        return False
    previous_run_id = str(observation.get(run_id_key) or "")
    if (
        not previous_run_id
        or previous_run_id != latest_recorded_run_id(state)
    ):
        return False
    if current_logical_run_id:
        previous_logical_run_id = str(
            observation.get(
                logical_run_id_key
                or run_id_key.replace(
                    "_run_id",
                    "_logical_run_id",
                )
            )
            or ""
        )
        if (
            not previous_logical_run_id
            or previous_logical_run_id
            != latest_recorded_logical_run_id(state)
            or previous_logical_run_id == current_logical_run_id
        ):
            return False
    try:
        observed_at = datetime.fromisoformat(
            str(observation.get(observed_at_key) or "").replace(
                "Z",
                "+00:00",
            )
        )
    except ValueError:
        return False
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    age = (
        datetime.now(timezone.utc)
        - observed_at.astimezone(timezone.utc)
    ).total_seconds()
    return 0 <= age <= destructive_candidate_ttl_seconds()


def validated_pending_context(
    context: DestinationContext,
) -> tuple[set[str], dict[str, str], dict[str, str]]:
    page_ids = set(context.pending_page_ids)
    sources = dict(context.pending_page_sources)
    notices = dict(context.pending_page_notices)
    if (
        len(page_ids) != len(context.pending_page_ids)
        or set(sources) != page_ids
        or set(notices) != page_ids
    ):
        raise DestinationConsistencyError(
            "대기 페이지 출처 정보를 신뢰할 수 없어 전역 차단합니다"
        )
    identities: set[tuple[str, str]] = set()
    for page_id in page_ids:
        source_id = str(sources.get(page_id) or "").strip()
        notice_id = str(notices.get(page_id) or "").strip()
        identity = (source_id, notice_id)
        if not source_id or not notice_id or identity in identities:
            raise DestinationConsistencyError(
                "대기 페이지 식별 정보가 누락되거나 중복되었습니다"
            )
        identities.add(identity)
    return page_ids, sources, notices


def shrink_candidate_confirmed(
    entry: DestinationPreflight,
    state: dict[str, Any],
    shrink_candidates: dict[str, Any],
    current_logical_run_id: str = "",
) -> bool:
    previous_candidate = shrink_candidates.get(entry.shrink_key, {})
    return bool(
        entry.shrink_candidate
        and isinstance(previous_candidate, dict)
        and previous_candidate.get("candidate_id")
        == entry.shrink_candidate.get("candidate_id")
        and nonnegative_count(
            previous_candidate.get("observations")
        )
        >= 1
        and recent_consecutive_observation(
            state,
            previous_candidate,
            "last_observed_run_id",
            "last_observed_at",
            current_logical_run_id=current_logical_run_id,
            logical_run_id_key="last_observed_logical_run_id",
        )
    )


def pending_quarantined_sources(
    context: DestinationContext,
    preflight: list[DestinationPreflight],
    state: dict[str, Any],
    shrink_candidates: dict[str, Any],
    configured_source_ids: set[str],
    current_logical_run_id: str = "",
) -> set[str]:
    page_ids, sources, notices = validated_pending_context(context)
    unknown_sources = set(sources.values()) - configured_source_ids
    if unknown_sources:
        raise DestinationConsistencyError(
            "설정에 없는 출처의 대기 페이지가 있어 전역 차단합니다"
        )
    entries_by_page: dict[str, DestinationPreflight] = {}
    for entry in preflight:
        if not entry.existing_page:
            continue
        page_id = str(entry.existing_page.get("id") or "").strip()
        if page_id not in page_ids:
            continue
        if page_id in entries_by_page:
            raise DestinationConsistencyError(
                "대기 페이지가 여러 적용 항목과 연결되었습니다"
            )
        entry_source = str(entry.item.get("source_id") or "").strip()
        entry_notice = str(entry.item.get("notice_id") or "").strip()
        if (
            entry_source != sources[page_id]
            or entry_notice != notices[page_id]
        ):
            raise DestinationConsistencyError(
                "대기 페이지와 수집 항목의 출처 식별자가 일치하지 않습니다"
            )
        entries_by_page[page_id] = entry
    quarantined = {
        sources[page_id]
        for page_id in page_ids - set(entries_by_page)
    }
    for page_id, entry in entries_by_page.items():
        if (
            entry.shrink_candidate
            and not shrink_candidate_confirmed(
                entry,
                state,
                shrink_candidates,
                current_logical_run_id,
            )
        ):
            quarantined.add(sources[page_id])
    return quarantined


def _apply_report(
    token: str,
    database_id: str,
    report: CrawlReport,
    full_reconcile: bool,
    previous_state: Optional[dict[str, Any]] = None,
    run_id: str = "",
    logical_run_id: str = "",
) -> SyncCounters:
    results = safe_source_results(report)
    if not results:
        return SyncCounters(
            observation_run_id=run_id,
            observation_logical_run_id=logical_run_id,
        )
    items = [
        item
        for result in results
        for item in prepare_source_items(result)
    ]
    context = prepare_destination(token, database_id, items)
    preflight = resolve_destination_preflight(
        context,
        items,
        atomic_recheck=False,
    )
    state = previous_state or {}
    source_states = state.get("sources", {})
    shrink_candidates = state.get("shrink_candidates", {})
    if not isinstance(shrink_candidates, dict):
        shrink_candidates = {}
    configured_source_ids = {
        result.source.config_fk
        for result in report.sources
    }
    state_pending_identities: set[tuple[str, str]] = set()
    if isinstance(source_states, dict):
        for source_id, source_state in source_states.items():
            if not isinstance(source_state, dict):
                continue
            pending_notice_ids = source_state.get(
                "pending_notice_ids",
                [],
            )
            if not isinstance(pending_notice_ids, list):
                raise DestinationConsistencyError(
                    "실행 상태의 대기 공지 ID를 신뢰할 수 없습니다"
                )
            if pending_notice_ids and source_id not in configured_source_ids:
                raise DestinationConsistencyError(
                    "설정에 없는 출처의 대기 복구 상태가 남아 있습니다"
                )
            for notice_id in pending_notice_ids:
                normalized_notice_id = str(notice_id).strip()
                if not normalized_notice_id:
                    raise DestinationConsistencyError(
                        "실행 상태의 대기 공지 ID를 신뢰할 수 없습니다"
                    )
                state_pending_identities.add(
                    (str(source_id), normalized_notice_id)
                )
    quarantined_sources = pending_quarantined_sources(
        context,
        preflight,
        state,
        shrink_candidates,
        configured_source_ids,
        logical_run_id,
    )
    pending_page_ids = set(context.pending_page_ids)
    counters = SyncCounters(
        observation_run_id=run_id,
        observation_logical_run_id=logical_run_id,
    )
    counters.pending_seen = len(pending_page_ids)
    counters.quarantined_source_ids = sorted(quarantined_sources)
    for entry in preflight:
        page_id = (
            str(entry.existing_page.get("id") or "").strip()
            if entry.existing_page
            else ""
        )
        if (
            page_id in pending_page_ids
            and entry.shrink_candidate
            and not shrink_candidate_confirmed(
                entry,
                state,
                shrink_candidates,
                logical_run_id,
            )
        ):
            counters.shrink_candidate_observations[
                entry.shrink_key
            ] = entry.shrink_candidate
    preflight.sort(
        key=lambda entry: (
            str(entry.existing_page.get("id") or "").strip()
            not in pending_page_ids
            if entry.existing_page
            else True
        )
    )
    active_preflight = [
        entry
        for entry in preflight
        if str(entry.item.get("source_id") or "").strip()
        not in quarantined_sources
    ]
    validate_destination_preflight_entries(
        context,
        active_preflight,
    )
    verified_pending_page_ids: set[str] = set()
    total_active_entries = len(active_preflight)
    for entry_index, entry in enumerate(active_preflight, start=1):
        log_destination_progress(
            "항목 처리",
            entry_index,
            total_active_entries,
            entry.item,
        )
        candidate_confirmed = shrink_candidate_confirmed(
            entry,
            state,
            shrink_candidates,
            logical_run_id,
        )
        if entry.shrink_candidate and not candidate_confirmed:
            counters.shrink_candidate_observations[
                entry.shrink_key
            ] = entry.shrink_candidate
            counters.unchanged += 1
            continue
        counters.shrink_candidate_clears.append(entry.shrink_key)
        current_page = refresh_destination_preflight_entry(
            context,
            entry,
        )

        def validate_before_entry_write(
            entry_to_validate: DestinationPreflight = entry,
        ) -> object:
            return validate_destination_preflight_entry(
                context,
                entry_to_validate,
            )

        apply_item(
            context,
            entry.item,
            counters,
            existing_page=current_page,
            existing_page_resolved=True,
            expected_operation_id=entry.operation_id,
            expected_body_media_reuse_status=(
                entry.body_media_reuse_status
            ),
            force_commit_readback=(
                (
                    str(entry.item.get("source_id") or ""),
                    str(entry.item.get("notice_id") or ""),
                )
                in state_pending_identities
            ),
            pre_write_validation=validate_before_entry_write,
        )
        entry_source_id = str(
            entry.item.get("source_id") or ""
        )
        entry_notice_id = str(
            entry.item.get("notice_id") or ""
        )
        if (
            entry_source_id,
            entry_notice_id,
        ) in state_pending_identities:
            recovered_ids = counters.recovered_pending_notices.setdefault(
                entry_source_id,
                [],
            )
            if entry_notice_id not in recovered_ids:
                recovered_ids.append(entry_notice_id)
        entry_page_id = (
            str(entry.existing_page.get("id") or "").strip()
            if entry.existing_page
            else ""
        )
        if entry_page_id in pending_page_ids:
            verified_pending_page_ids.add(entry_page_id)
    LOGGER.info(
        "목적지 항목 처리 완료: 항목=%s",
        total_active_entries,
    )
    remaining_pending = inspect_pending_pages(token, database_id)
    (
        remaining_ids,
        remaining_sources,
        remaining_notices,
    ) = pending_page_context(remaining_pending)
    remaining_id_set = set(remaining_ids)
    unknown_remaining_sources = (
        set(remaining_sources.values()) - configured_source_ids
    )
    if unknown_remaining_sources:
        raise DestinationConsistencyError(
            "설정에 없는 출처의 대기 페이지가 최종 검증에서 발견되었습니다"
        )
    recovered_page_ids = pending_page_ids - remaining_id_set
    if not recovered_page_ids.issubset(verified_pending_page_ids):
        raise DestinationConsistencyError(
            "최종 상태 재확인 없이 대기 페이지가 사라졌습니다"
        )
    counters.pending_recovered = len(recovered_page_ids)
    counters.unresolved_pending_page_ids = sorted(remaining_id_set)
    for page_id in sorted(recovered_page_ids):
        source_id = context.pending_page_sources[page_id]
        notice_id = context.pending_page_notices[page_id]
        counters.recovered_pending_notices.setdefault(
            source_id,
            [],
        )
        if (
            notice_id
            not in counters.recovered_pending_notices[source_id]
        ):
            counters.recovered_pending_notices[source_id].append(
                notice_id
            )
    for page_id in sorted(remaining_id_set):
        source_id = remaining_sources[page_id]
        notice_id = remaining_notices[page_id]
        pending_ids = counters.unresolved_pending_notices.setdefault(
            source_id,
            [],
        )
        pending_ids.append(notice_id)
        if len(pending_ids) > 1000:
            raise DestinationConsistencyError(
                "출처별 대기 공지 상태가 보존 한도를 초과했습니다"
            )
    quarantined_sources.update(remaining_sources.values())
    counters.quarantined_source_ids = sorted(quarantined_sources)
    for result in results:
        source_id = result.source.config_fk
        if (
            source_id in quarantined_sources
            or not result.top_snapshot_verified
        ):
            continue
        current_ids = current_top_notice_ids(result)
        first_top_pages, first_candidates = inspect_missing_top(
            token,
            database_id,
            source_id,
            current_ids,
        )
        missing_ids = top_candidate_ids(first_candidates)
        source_state = (
            source_states.get(source_id, {})
            if isinstance(source_states, dict)
            else {}
        )
        prior_absences = (
            source_state.get("top_absence_counts", {})
            if isinstance(source_state, dict)
            else {}
        )
        if not isinstance(prior_absences, dict):
            prior_absences = {}
        eligible = {
            notice_id
            for notice_id in missing_ids
            if (
                nonnegative_count(prior_absences.get(notice_id)) >= 1
                and recent_consecutive_observation(
                    state,
                    source_state,
                    "top_absence_last_run_id",
                    "top_absence_last_observed_at",
                    current_logical_run_id=logical_run_id,
                    logical_run_id_key=(
                        "top_absence_last_logical_run_id"
                    ),
                )
            )
        }
        counters.top_present_ids[source_id] = sorted(current_ids)
        counters.top_absence_observations[source_id] = sorted(
            missing_ids
        )
        first_eligible_candidates = [
            page
            for page in first_candidates
            if extract_rich_text_value(
                page.get("properties", {}),
                NOTICE_ID_PROPERTY,
            )
            in eligible
        ]
        validate_top_disable_candidates(
            source_id,
            len(first_top_pages),
            first_eligible_candidates,
        )
        second_top_pages, second_candidates = inspect_missing_top(
            token,
            database_id,
            source_id,
            current_ids,
        )
        if (
            top_candidate_fingerprints(second_top_pages)
            != top_candidate_fingerprints(first_top_pages)
            or top_candidate_fingerprints(second_candidates)
            != top_candidate_fingerprints(first_candidates)
        ):
            raise RuntimeError(
                f"TOP 연속 검증 중 대상이 변경되었습니다: {source_id}"
            )
        second_eligible_candidates = [
            page
            for page in second_candidates
            if extract_rich_text_value(
                page.get("properties", {}),
                NOTICE_ID_PROPERTY,
            )
            in eligible
        ]
        validate_top_disable_candidates(
            source_id,
            len(second_top_pages),
            second_eligible_candidates,
        )
        disabled = disable_missing_top(
            token,
            database_id,
            source_id,
            current_ids,
            eligible_notice_ids=eligible,
            planned_candidates=second_candidates,
            total_top_count=len(second_top_pages),
        )
        counters.top_disabled += disabled
        counters.writes += disabled
    return counters


def apply_report(
    token: str,
    database_id: str,
    report: CrawlReport,
    full_reconcile: bool,
    previous_state: Optional[dict[str, Any]] = None,
    run_id: str = "",
    logical_run_id: str = "",
) -> SyncCounters:
    with external_download_run_scope(force_new=True) as download_run:
        counters = _apply_report(
            token,
            database_id,
            report,
            full_reconcile,
            previous_state=previous_state,
            run_id=run_id,
            logical_run_id=logical_run_id,
        )
        snapshot = download_run.snapshot()
    counters.external_download_requests = int(snapshot["requests"])
    counters.external_download_stopped_reason = str(
        snapshot["stopped_reason"]
    )
    status_code = snapshot["status_code"]
    counters.external_download_status_code = (
        int(status_code) if status_code is not None else None
    )
    retry_after = snapshot["retry_after"]
    counters.external_download_retry_after = (
        str(retry_after) if retry_after is not None else None
    )
    retry_after_seconds = snapshot["retry_after_seconds"]
    counters.external_download_retry_after_seconds = (
        float(retry_after_seconds)
        if retry_after_seconds is not None
        else None
    )
    counters.external_download_elapsed_seconds = float(
        snapshot["elapsed_seconds"]
    )
    return counters


def destination_schema_ready(database: dict[str, Any]) -> bool:
    try:
        validate_destination_schema(database)
    except RuntimeError:
        return False
    return True


def build_dry_run_plan(
    run_id: str,
    report: CrawlReport,
    token: Optional[str] = None,
    database_id: Optional[str] = None,
    full_reconcile: bool = False,
    previous_state: Optional[dict[str, Any]] = None,
    *,
    logical_run_id: str = "",
) -> MutationPlan:
    plan = MutationPlan(run_id=run_id)
    results = safe_source_results(report)
    prepared = [
        (result, prepare_source_items(result))
        for result in results
    ]
    if not prepared:
        plan.conflicts.extend(
            issue.message for issue in report.issues if issue.fatal
        )
        return plan
    if not token or not database_id:
        raise RuntimeError(
            "드라이런 대상 검증에 NOTION_TOKEN과 NOTION_DB_ID가 필요합니다"
    )
    database = fetch_database(token, database_id)
    validate_destination_schema(database)
    pending_pages = inspect_pending_pages(token, database_id)
    pending_ids, pending_sources, pending_notices = (
        pending_page_context(pending_pages)
    )
    context = DestinationContext(
        token=token,
        database_id=database_id,
        pending_page_ids=pending_ids,
        pending_page_sources=pending_sources,
        pending_page_notices=pending_notices,
    )
    with external_download_run_scope(force_new=True):
        preflight_entries = resolve_destination_preflight(
            context,
            [
                item
                for _, source_items in prepared
                for item in source_items
            ],
        )
    preflight_by_key = {
        entry.shrink_key: entry for entry in preflight_entries
    }
    state = previous_state or {}
    shrink_candidates = state.get("shrink_candidates", {})
    if not isinstance(shrink_candidates, dict):
        shrink_candidates = {}
    quarantined_sources = pending_quarantined_sources(
        context,
        preflight_entries,
        state,
        shrink_candidates,
        {
            result.source.config_fk
            for result in report.sources
        },
        logical_run_id,
    )
    plan.quarantined_source_ids = sorted(quarantined_sources)
    source_states = state.get("sources", {})
    if not isinstance(source_states, dict):
        source_states = {}
    for result, items in prepared:
        for item in items:
            action_count_before_item = len(plan.actions)
            key = (
                f"{result.source.config_fk}:"
                f"{str(item['notice_id'])}"
            )
            preflight = preflight_by_key[key]
            operation_id = preflight.operation_id
            existing = preflight.existing_page
            if result.source.config_fk in quarantined_sources:
                reason = "source_pending_quarantine"
                plan.actions.append(
                    MutationAction(
                        kind=MutationKind.CONFLICT,
                        source_id=result.source.config_fk,
                        notice_id=str(item["notice_id"]),
                        page_id=(
                            str(existing.get("id") or "")
                            if existing
                            else ""
                        ),
                        operation_id=operation_id,
                        reason=reason,
                    )
                )
                plan.conflicts.append(f"{key}:{reason}")
                continue
            candidate_confirmed = shrink_candidate_confirmed(
                preflight,
                state,
                shrink_candidates,
                logical_run_id,
            )
            if preflight.shrink_candidate and not candidate_confirmed:
                reason = "shrink_candidate_pending"
                plan.actions.append(
                    MutationAction(
                        kind=MutationKind.CONFLICT,
                        source_id=result.source.config_fk,
                        notice_id=str(item["notice_id"]),
                        page_id=(
                            str(existing.get("id") or "")
                            if existing
                            else ""
                        ),
                        operation_id=operation_id,
                        reason=reason,
                    )
                )
                plan.conflicts.append(f"{key}:{reason}")
                continue
            if not existing:
                plan.actions.append(
                    MutationAction(
                        kind=MutationKind.CREATE,
                        source_id=result.source.config_fk,
                        notice_id=str(item["notice_id"]),
                        operation_id=operation_id,
                        reason="managed_page_missing",
                    )
                )
                continue
            body_blocks = item.get("body_blocks") or []
            body_confirmed_empty = (
                str(item.get("body_status") or "")
                == "confirmed_empty"
            )
            desired_hash = (
                compute_body_hash(
                    (
                        normalize_body_blocks_for_hash(
                            body_blocks,
                            should_upload_files_to_notion(),
                            media_content_state=(
                                preflight.body_media_content_state
                            ),
                        )
                        if body_blocks
                        else []
                    ),
                    image_mode=(
                        BODY_HASH_IMAGE_MODE_UPLOAD
                        if should_upload_files_to_notion()
                        and has_image_blocks(body_blocks)
                        else ""
                    ),
                )
                if body_blocks or body_confirmed_empty
                else ""
            )
            existing_properties = existing.get("properties", {})
            existing_generation_manifest = (
                extract_body_generation_manifest(
                    existing_properties
                )
            )
            existing_generation_id = extract_body_generation_id(
                existing_properties
            )
            existing_hash = extract_rich_text_value(
                existing_properties,
                BODY_HASH_PROPERTY,
            )
            existing_body_media_state = extract_body_media_state(
                existing_properties
            )
            body_needs_update = bool(
                (body_blocks or body_confirmed_empty)
                and (
                    desired_hash != existing_hash
                    or preflight.body_media_reuse_status == "drift"
                    or body_media_content_identity(
                        preflight.body_media_content_state
                    )
                    != body_media_content_identity(
                        existing_body_media_state
                    )
                )
            )
            if body_blocks and not body_needs_update:
                body_needs_update = not is_body_generation_current(
                    token,
                    str(existing.get("id") or ""),
                    existing_generation_id or desired_hash,
                )
            if body_confirmed_empty and not body_needs_update:
                body_needs_update = (
                    not is_empty_body_generation_current(
                        token,
                        str(existing.get("id") or ""),
                        existing_generation_id or desired_hash,
                    )
                )
            completed_generation = (
                next_body_generation_id(
                    operation_id,
                    desired_hash,
                    existing_generation_manifest,
                    existing_hash,
                )
                if body_needs_update
                else existing_generation_id
                or desired_hash
                or operation_id
            )
            desired_item = dict(item)
            desired_item["operation_id"] = operation_id
            desired_item["sync_status"] = "committed"
            desired = build_properties(desired_item, True, True, True)
            if should_preserve_existing_top(existing, item):
                desired.pop(TOP_PROPERTY, None)
            attachment_state_changed = False
            if (
                "attachments" in item
                and should_upload_files_to_notion()
            ):
                existing_attachment_state = extract_attachment_state(
                    existing_properties
                )
                desired_attachment_content = attachment_content_identity(
                    preflight.attachment_content_state
                )
                existing_attachment_content = attachment_content_identity(
                    existing_attachment_state
                )
                reusable_attachment_binding = bool(
                    not desired_attachment_content
                    or extract_existing_uploaded_attachment_ids(
                        existing_properties,
                        existing_attachment_state,
                    )
                )
                uploaded_source_urls = {
                    identity[0]
                    for identity in desired_attachment_content
                }
                planned_attachment_entries = [
                    (
                        {
                            **entry,
                            "type": "file_upload",
                            "external": {},
                            "file_upload": {"id": "reused"},
                        }
                        if (
                            str(entry.get("type") or "") == "external"
                            and normalize_attachment_identity_url(
                                str(
                                    entry.get("external", {}).get("url")
                                    or ""
                                )
                            )
                            in uploaded_source_urls
                        )
                        else entry
                    )
                    for entry in item.get("attachments") or []
                ]
                attachment_property_reusable = bool(
                    desired_attachment_content
                    == existing_attachment_content
                    and reusable_attachment_binding
                    and external_attachment_signature(
                        planned_attachment_entries
                    )
                    == external_attachment_signature(
                        existing_properties
                        .get(ATTACHMENT_PROPERTY, {})
                        .get("files", [])
                    )
                )
                if attachment_property_reusable:
                    desired.pop(ATTACHMENT_PROPERTY, None)
                    expected_attachment_state = [
                        {
                            **entry,
                            "generation_id": completed_generation,
                        }
                        for entry in existing_attachment_state
                    ]
                    attachment_state_changed = (
                        json.dumps(
                            expected_attachment_state,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        != extract_rich_text_value(
                            existing_properties,
                            ATTACHMENT_STATE_PROPERTY,
                        )
                    )
                elif (
                    desired_attachment_content
                    or existing_attachment_state
                ):
                    attachment_state_changed = True
            changed = filter_changed_properties(
                existing_properties,
                desired,
            )
            if attachment_state_changed:
                changed[ATTACHMENT_STATE_PROPERTY] = {
                    "rich_text": []
                }
            if (
                existing_body_media_state
                and not body_needs_update
            ):
                expected_body_media_state = [
                    {
                        **entry,
                        "generation_id": completed_generation,
                    }
                    for entry in existing_body_media_state
                ]
                if json.dumps(
                    expected_body_media_state,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ) != extract_rich_text_value(
                    existing_properties,
                    BODY_MEDIA_STATE_PROPERTY,
                ):
                    changed[BODY_MEDIA_STATE_PROPERTY] = {
                        "rich_text": []
                    }
            if changed:
                plan.actions.append(
                    MutationAction(
                        kind=MutationKind.UPDATE_PROPERTIES,
                        source_id=result.source.config_fk,
                        notice_id=str(item["notice_id"]),
                        page_id=str(existing.get("id") or ""),
                        operation_id=operation_id,
                        reason=",".join(sorted(changed)),
                    )
                )
            if body_needs_update:
                plan.actions.append(
                    MutationAction(
                        kind=MutationKind.REPLACE_BODY,
                        source_id=result.source.config_fk,
                        notice_id=str(item["notice_id"]),
                        page_id=str(existing.get("id") or ""),
                        operation_id=operation_id,
                        reason="body_hash_changed",
                    )
                )
            if len(plan.actions) == action_count_before_item:
                plan.actions.append(
                    MutationAction(
                        kind=MutationKind.NOOP,
                        source_id=result.source.config_fk,
                        notice_id=str(item["notice_id"]),
                        page_id=str(existing.get("id") or ""),
                        operation_id=operation_id,
                        reason="unchanged",
                    )
                )
    covered_page_ids = {
        str(entry.existing_page.get("id") or "").strip()
        for entry in preflight_entries
        if entry.existing_page
    }
    item_conflict_keys = {
        (action.source_id, action.notice_id, action.page_id)
        for action in plan.actions
        if action.kind == MutationKind.CONFLICT
    }
    for page_id in sorted(set(context.pending_page_ids) - covered_page_ids):
        source_id = context.pending_page_sources[page_id]
        notice_id = context.pending_page_notices[page_id]
        reason = "pending_page_outside_current_scope"
        conflict = f"{source_id}:{notice_id}:{reason}:{page_id}"
        plan.conflicts.append(conflict)
        action_identity = (source_id, notice_id, page_id)
        if action_identity not in item_conflict_keys:
            plan.actions.append(
                MutationAction(
                    kind=MutationKind.CONFLICT,
                    source_id=source_id,
                    notice_id=notice_id,
                    page_id=page_id,
                    reason=reason,
                )
            )
    configured_source_ids = {
        result.source.config_fk for result in report.sources
    }
    final_pending_pages = inspect_pending_pages(token, database_id)
    (
        final_pending_ids,
        final_pending_sources,
        final_pending_notices,
    ) = pending_page_context(final_pending_pages)
    unknown_final_sources = (
        set(final_pending_sources.values()) - configured_source_ids
    )
    if unknown_final_sources:
        raise DestinationConsistencyError(
            "설정에 없는 출처의 대기 페이지가 최종 검증에서 발견되었습니다"
        )
    late_pending_ids = set(final_pending_ids) - set(
        context.pending_page_ids
    )
    for page_id in sorted(late_pending_ids):
        source_id = final_pending_sources[page_id]
        notice_id = final_pending_notices[page_id]
        quarantined_sources.add(source_id)
        reason = "pending_page_detected_after_preflight"
        plan.conflicts.append(
            f"{source_id}:{notice_id}:{reason}:{page_id}"
        )
        plan.actions.append(
            MutationAction(
                kind=MutationKind.CONFLICT,
                source_id=source_id,
                notice_id=notice_id,
                page_id=page_id,
                reason=reason,
            )
        )
    plan.quarantined_source_ids = sorted(quarantined_sources)
    for result in results:
        source_id = result.source.config_fk
        if (
            source_id in quarantined_sources
            or not result.top_snapshot_verified
        ):
            continue
        current_ids = current_top_notice_ids(result)
        first_top_pages, first_candidates = inspect_missing_top(
            token,
            database_id,
            source_id,
            current_ids,
        )
        second_top_pages, second_candidates = inspect_missing_top(
            token,
            database_id,
            source_id,
            current_ids,
        )
        if (
            top_candidate_fingerprints(second_top_pages)
            != top_candidate_fingerprints(first_top_pages)
            or top_candidate_fingerprints(second_candidates)
            != top_candidate_fingerprints(first_candidates)
        ):
            raise RuntimeError(
                f"TOP 연속 검증 중 대상이 변경되었습니다: {source_id}"
            )
        source_state = source_states.get(source_id, {})
        prior_absences = (
            source_state.get("top_absence_counts", {})
            if isinstance(source_state, dict)
            else {}
        )
        if not isinstance(prior_absences, dict):
            prior_absences = {}
        eligible_candidates: list[dict[str, Any]] = []
        for page in second_candidates:
            notice_id = extract_rich_text_value(
                page.get("properties", {}),
                NOTICE_ID_PROPERTY,
            )
            if nonnegative_count(
                prior_absences.get(notice_id)
            ) < 1 or not recent_consecutive_observation(
                state,
                source_state,
                "top_absence_last_run_id",
                "top_absence_last_observed_at",
                current_logical_run_id=logical_run_id,
                logical_run_id_key=(
                    "top_absence_last_logical_run_id"
                ),
            ):
                continue
            eligible_candidates.append(page)
        validate_top_disable_candidates(
            source_id,
            len(second_top_pages),
            eligible_candidates,
        )
        for page in eligible_candidates:
            notice_id = extract_rich_text_value(
                page.get("properties", {}),
                NOTICE_ID_PROPERTY,
            )
            plan.actions.append(
                MutationAction(
                    kind=MutationKind.DISABLE_TOP,
                    source_id=source_id,
                    notice_id=notice_id,
                    page_id=str(page.get("id") or ""),
                    reason="missing_from_confirmed_snapshot",
                )
            )
    return plan
