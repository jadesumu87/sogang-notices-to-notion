import argparse
import copy
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional, cast
from urllib.parse import parse_qs, urlparse

from common import (
    extract_verified_detail_url_identity,
    rich_text_plain_text,
)
from notion_client import list_block_children, notion_request, retrieve_page
from settings import (
    NOTICE_ID_PROPERTY,
    SOURCE_KEY_PROPERTY,
    SYNC_GENERATION_PROPERTY,
    SYNC_OPERATION_PROPERTY,
    SYNC_OWNER_PROPERTY,
    SYNC_OWNER_VALUE,
    SYNC_STATUS_PROPERTY,
    URL_PROPERTY,
    build_detail_url,
    is_writer_context_confirmed,
    load_dotenv,
)
from sync import (
    body_generation_property_payload,
    extract_body_generation_manifest,
    extract_rich_text_value,
    extract_title,
    has_current_sync_marker,
    has_sync_marker,
    is_body_generation_current,
    is_managed_page,
    sync_container_actual_hash,
    sync_container_body_rich_text,
)
from utils import build_rich_text_chunks

JsonObject = dict[str, Any]
PLAN_VERSION = 3
CONFIRMATION_PREFIX = "APPLY EXISTING PAGE MIGRATION"
LOCAL_WRITE_CONFIRMATION = "ALLOW EXISTING PAGE METADATA MIGRATION"
SYNC_PROPERTY_NAMES = (
    SYNC_OWNER_PROPERTY,
    SOURCE_KEY_PROPERTY,
    NOTICE_ID_PROPERTY,
    SYNC_GENERATION_PROPERTY,
    SYNC_STATUS_PROPERTY,
    SYNC_OPERATION_PROPERTY,
)
MIGRATED_STATUS = "committed"
SAFE_ID_RE = re.compile(r"[A-Za-z0-9-]{1,200}")


class MigrationError(RuntimeError):
    pass


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _safe_id(value: object, label: str) -> str:
    normalized = str(value or "").strip()
    if not SAFE_ID_RE.fullmatch(normalized):
        raise MigrationError(f"{label}가 유효하지 않습니다")
    return normalized


def _same_notion_id(left: object, right: object) -> bool:
    return str(left or "").replace("-", "").lower() == str(right or "").replace(
        "-", ""
    ).lower()


def _page_fingerprint(page: JsonObject) -> str:
    protected = copy.deepcopy(page)
    protected.pop("last_edited_time", None)
    protected.pop("last_edited_by", None)
    properties = protected.get("properties")
    if not isinstance(properties, dict):
        raise MigrationError("페이지 속성 형식이 올바르지 않습니다")
    for name in SYNC_PROPERTY_NAMES:
        properties.pop(name, None)
    return _canonical_hash(protected)


def _root_fingerprint(blocks: list[JsonObject]) -> str:
    return _canonical_hash(blocks)


def _is_target_parent(page: JsonObject, data_source_id: str) -> bool:
    parent = page.get("parent")
    return bool(
        isinstance(parent, dict)
        and parent.get("type") == "data_source_id"
        and _same_notion_id(parent.get("data_source_id"), data_source_id)
    )


def _page_url(page: JsonObject) -> str:
    properties = page.get("properties")
    if not isinstance(properties, dict):
        return ""
    value = properties.get(URL_PROPERTY)
    if not isinstance(value, dict):
        return ""
    return str(value.get("url") or "").strip()


def _page_title(page: JsonObject) -> str:
    properties = page.get("properties")
    if not isinstance(properties, dict):
        raise MigrationError("페이지 속성 형식이 올바르지 않습니다")
    return extract_title(properties)


def _official_identity(page: JsonObject) -> Optional[tuple[str, str]]:
    url = _page_url(page)
    parsed = urlparse(url)
    match = re.fullmatch(r"/ko/detail/([0-9]+)", parsed.path)
    query = parse_qs(parsed.query, keep_blank_values=True)
    source_values = query.get("bbsConfigFk", [])
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.sogang.ac.kr"
        or parsed.params
        or parsed.fragment
        or not match
        or set(query) != {"bbsConfigFk"}
        or len(source_values) != 1
        or not re.fullmatch(r"[0-9]+", source_values[0])
    ):
        return None
    source_id = source_values[0]
    notice_id = match.group(1)
    if (
        url != build_detail_url(notice_id, source_id)
        or extract_verified_detail_url_identity(url, source_id) != notice_id
    ):
        return None
    return source_id, notice_id


def _sync_values(page: JsonObject) -> dict[str, str]:
    properties = page.get("properties")
    if not isinstance(properties, dict):
        raise MigrationError("페이지 속성 형식이 올바르지 않습니다")
    return {
        name: extract_rich_text_value(properties, name)
        for name in SYNC_PROPERTY_NAMES
    }


def _all_sync_fields_empty(page: JsonObject) -> bool:
    return not any(_sync_values(page).values())


def _sync_schema_snapshot(token: str, data_source_id: str) -> JsonObject:
    safe_data_source_id = _safe_id(data_source_id, "데이터 소스 ID")
    response = notion_request(
        "GET",
        f"https://api.notion.com/v1/data_sources/{safe_data_source_id}",
        token,
    )
    properties = response.get("properties")
    if not isinstance(properties, dict):
        raise MigrationError(
            "이관 전에 Notion 동기화 속성 스키마를 조회할 수 없습니다"
        )
    issues: list[str] = []
    snapshot: JsonObject = {}
    seen_property_ids: set[str] = set()
    for name in SYNC_PROPERTY_NAMES:
        prop = properties.get(name)
        if not isinstance(prop, dict):
            issues.append(f"{name}:누락")
            continue
        property_type = str(prop.get("type") or "")
        if property_type != "rich_text":
            issues.append(f"{name}:{property_type or '유형 없음'}")
            continue
        property_id = str(prop.get("id") or "").strip()
        if not property_id:
            issues.append(f"{name}:ID 누락")
            continue
        if property_id in seen_property_ids:
            issues.append(f"{name}:ID 중복")
            continue
        seen_property_ids.add(property_id)
        snapshot[name] = {
            "id": property_id,
            "type": property_type,
        }
    if issues:
        raise MigrationError(
            "스키마 이관을 먼저 완료해야 합니다: " + ", ".join(issues)
        )
    return snapshot


def _sync_schema_fingerprint(token: str, data_source_id: str) -> str:
    return _canonical_hash(_sync_schema_snapshot(token, data_source_id))


def _query_all_pages(token: str, data_source_id: str) -> list[JsonObject]:
    safe_data_source_id = _safe_id(data_source_id, "데이터 소스 ID")
    url = f"https://api.notion.com/v1/data_sources/{safe_data_source_id}/query"
    results: list[JsonObject] = []
    cursor = ""
    seen_cursors: set[str] = set()
    while True:
        payload: JsonObject = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        response = notion_request("POST", url, token, payload)
        page_values = response.get("results")
        if not isinstance(page_values, list) or not all(
            isinstance(page, dict) for page in page_values
        ):
            raise MigrationError("데이터 소스 조회 응답이 올바르지 않습니다")
        results.extend(cast(list[JsonObject], page_values))
        if not response.get("has_more"):
            break
        next_cursor = str(response.get("next_cursor") or "").strip()
        if not next_cursor or next_cursor in seen_cursors:
            raise MigrationError("데이터 소스 조회 커서가 누락되거나 반복되었습니다")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    seen_page_ids: set[str] = set()
    for page in results:
        page_id = _safe_id(page.get("id"), "페이지 ID")
        if page_id in seen_page_ids:
            raise MigrationError(f"데이터 소스 조회에 페이지가 중복되었습니다: {page_id}")
        seen_page_ids.add(page_id)
    return results


def _assert_no_identity_duplicates(
    pages: list[JsonObject],
    data_source_id: str,
) -> None:
    identities: dict[tuple[str, str], list[str]] = {}
    for page in pages:
        if (
            page.get("in_trash")
            or not _is_target_parent(page, data_source_id)
        ):
            continue
        identity = _official_identity(page)
        if identity is None:
            continue
        page_id = _safe_id(page.get("id"), "페이지 ID")
        identities.setdefault(identity, []).append(page_id)
    duplicates = {
        identity: page_ids
        for identity, page_ids in identities.items()
        if len(page_ids) > 1
    }
    if duplicates:
        details = ", ".join(
            f"{source_id}/{notice_id}={','.join(sorted(page_ids))}"
            for (source_id, notice_id), page_ids in sorted(duplicates.items())
        )
        raise MigrationError(f"동일 공지 페이지가 중복되었습니다: {details}")


def _quote_snapshot(
    token: str,
    page_id: str,
    blocks: list[JsonObject],
) -> tuple[Optional[str], Optional[str], str, str]:
    quotes = [block for block in blocks if block.get("type") == "quote"]
    if len(quotes) > 1:
        raise MigrationError(
            f"최상위 인용 블록이 2개 이상이라 이관할 수 없습니다: {page_id}"
        )
    if not quotes:
        return None, None, "none", ""
    quote_payload = quotes[0].get("quote")
    rich_text = (
        quote_payload.get("rich_text")
        if isinstance(quote_payload, dict)
        else None
    )
    if (
        isinstance(rich_text, list)
        and has_current_sync_marker(rich_text)
    ):
        raise MigrationError(
            f"현재 동기화 표식이 있는 인용 블록은 이관할 수 없습니다: {page_id}"
        )
    marker = (
        "legacy"
        if isinstance(rich_text, list) and has_sync_marker(rich_text)
        else "unmarked"
    )
    if marker == "unmarked" and len(blocks) != 1:
        raise MigrationError(
            f"표식 없는 인용 블록과 다른 최상위 블록이 함께 있어 이관할 수 없습니다: {page_id}"
        )
    quote_id = _safe_id(quotes[0].get("id"), "인용 블록 ID")
    quote_hash = sync_container_actual_hash(token, quotes[0])
    if not re.fullmatch(r"[0-9a-f]{64}", quote_hash):
        raise MigrationError(f"인용 블록 해시를 계산할 수 없습니다: {page_id}")
    body_rich_text = sync_container_body_rich_text(quotes[0])
    if body_rich_text is None:
        raise MigrationError(f"인용 블록 본문을 확인할 수 없습니다: {page_id}")
    preview = " ".join(rich_text_plain_text(body_rich_text).split())[:160]
    return quote_id, quote_hash, marker, preview


def _plan_hash_payload(plan: JsonObject) -> JsonObject:
    return {
        key: copy.deepcopy(value)
        for key, value in plan.items()
        if key not in {"plan_sha256", "confirmation"}
    }


def _finalize_plan(plan: JsonObject) -> JsonObject:
    digest = _canonical_hash(_plan_hash_payload(plan))
    finalized = copy.deepcopy(plan)
    finalized["plan_sha256"] = digest
    finalized["confirmation"] = f"{CONFIRMATION_PREFIX} {digest}"
    return finalized


def build_migration_plan(
    token: str,
    data_source_id: str,
    page_ids: Optional[list[str]] = None,
) -> JsonObject:
    data_source_id = _safe_id(data_source_id, "데이터 소스 ID")
    requested_ids = [_safe_id(page_id, "페이지 ID") for page_id in page_ids or []]
    if not requested_ids:
        raise MigrationError(
            "계획 생성에는 --page-id를 하나 이상 명시해야 합니다"
        )
    if len(requested_ids) != len(set(requested_ids)):
        raise MigrationError("요청한 페이지 ID가 중복되었습니다")
    schema_fingerprint = _sync_schema_fingerprint(token, data_source_id)
    queried_pages = _query_all_pages(token, data_source_id)
    _assert_no_identity_duplicates(queried_pages, data_source_id)
    queried_by_id = {
        _safe_id(page.get("id"), "페이지 ID"): page
        for page in queried_pages
    }
    missing = sorted(set(requested_ids) - set(queried_by_id))
    if missing:
        raise MigrationError(
            f"데이터 소스에서 요청한 페이지를 찾을 수 없습니다: {','.join(missing)}"
        )
    entries: list[JsonObject] = []
    blockers: dict[str, list[str]] = {}
    for page_id in sorted(requested_ids):
        page = retrieve_page(token, page_id)
        page_blockers: list[str] = []
        if page.get("in_trash"):
            page_blockers.append("휴지통 상태")
        if not _is_target_parent(page, data_source_id):
            page_blockers.append("대상 데이터 소스의 페이지가 아님")
        identity = _official_identity(page)
        if identity is None:
            page_blockers.append("공식 공지 상세 URL이 정확하지 않음")
        if not _page_title(page):
            page_blockers.append("페이지 제목이 비어 있음")
        populated = [
            name
            for name, value in _sync_values(page).items()
            if value
        ]
        if populated:
            page_blockers.append(
                "새 동기화 필드가 비어 있지 않음("
                + ",".join(populated)
                + ")"
            )
        blocks = list_block_children(token, page_id)
        quote_id: Optional[str] = None
        quote_hash: Optional[str] = None
        quote_marker = "none"
        quote_preview = ""
        try:
            (
                quote_id,
                quote_hash,
                quote_marker,
                quote_preview,
            ) = _quote_snapshot(token, page_id, blocks)
        except MigrationError as exc:
            page_blockers.append(str(exc))
        if page_blockers:
            blockers[page_id] = page_blockers
            continue
        if identity is None:
            raise AssertionError("검증된 공지 식별자가 누락되었습니다")
        source_id, notice_id = identity
        entries.append(
            {
                "page_id": page_id,
                "title": _page_title(page),
                "url": _page_url(page),
                "source_id": source_id,
                "notice_id": notice_id,
                "page_fingerprint": _page_fingerprint(page),
                "root_fingerprint": _root_fingerprint(blocks),
                "quote_id": quote_id,
                "quote_hash": quote_hash,
                "quote_marker": quote_marker,
                "quote_preview": quote_preview,
            }
        )
    if blockers:
        details = "; ".join(
            f"{page_id}=" + ", ".join(reasons)
            for page_id, reasons in sorted(blockers.items())
        )
        raise MigrationError(f"이관 차단 항목: {details}")
    plan: JsonObject = {
        "version": PLAN_VERSION,
        "data_source_id": data_source_id,
        "sync_schema_fingerprint": schema_fingerprint,
        "page_id_allowlist": [str(entry["page_id"]) for entry in entries],
        "pages": entries,
    }
    return _finalize_plan(plan)


def _validated_plan_entries(plan: JsonObject) -> list[JsonObject]:
    if plan.get("version") != PLAN_VERSION:
        raise MigrationError("지원하지 않는 이관 계획 버전입니다")
    _safe_id(plan.get("data_source_id"), "데이터 소스 ID")
    if not re.fullmatch(
        r"[0-9a-f]{64}",
        str(plan.get("sync_schema_fingerprint") or ""),
    ):
        raise MigrationError("이관 계획의 스키마 지문이 유효하지 않습니다")
    page_values = plan.get("pages")
    allowlist = plan.get("page_id_allowlist")
    if not isinstance(page_values, list) or not all(
        isinstance(entry, dict) for entry in page_values
    ):
        raise MigrationError("이관 계획의 페이지 목록이 올바르지 않습니다")
    if not isinstance(allowlist, list) or not all(
        isinstance(page_id, str) for page_id in allowlist
    ):
        raise MigrationError("이관 계획의 페이지 허용 목록이 올바르지 않습니다")
    entries = cast(list[JsonObject], page_values)
    if not entries:
        raise MigrationError("이관 계획에는 페이지가 하나 이상 있어야 합니다")
    page_ids: list[str] = []
    identities: set[tuple[str, str]] = set()
    for entry in entries:
        page_id = _safe_id(entry.get("page_id"), "페이지 ID")
        source_id = str(entry.get("source_id") or "").strip()
        notice_id = str(entry.get("notice_id") or "").strip()
        title = entry.get("title")
        url = entry.get("url")
        if not isinstance(title, str):
            raise MigrationError(f"계획의 제목이 유효하지 않습니다: {page_id}")
        if (
            not re.fullmatch(r"[0-9]+", source_id)
            or not re.fullmatch(r"[0-9]+", notice_id)
        ):
            raise MigrationError(f"계획의 공지 식별자가 유효하지 않습니다: {page_id}")
        if url != build_detail_url(notice_id, source_id):
            raise MigrationError(f"계획의 공지 URL이 유효하지 않습니다: {page_id}")
        for key in ("page_fingerprint", "root_fingerprint"):
            if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get(key) or "")):
                raise MigrationError(f"계획의 {key}가 유효하지 않습니다: {page_id}")
        quote_id = entry.get("quote_id")
        quote_hash = entry.get("quote_hash")
        quote_marker = entry.get("quote_marker")
        quote_preview = entry.get("quote_preview")
        if (quote_id is None) != (quote_hash is None):
            raise MigrationError(f"계획의 인용 블록 정보가 불완전합니다: {page_id}")
        if (
            quote_marker not in {"none", "legacy", "unmarked"}
            or not isinstance(quote_preview, str)
            or len(quote_preview) > 160
        ):
            raise MigrationError(f"계획의 인용 블록 검토 정보가 유효하지 않습니다: {page_id}")
        if quote_id is not None:
            _safe_id(quote_id, "인용 블록 ID")
            if not re.fullmatch(r"[0-9a-f]{64}", str(quote_hash)):
                raise MigrationError(f"계획의 인용 블록 해시가 유효하지 않습니다: {page_id}")
            if quote_marker == "none":
                raise MigrationError(f"계획의 인용 블록 표식 정보가 누락되었습니다: {page_id}")
        elif quote_marker != "none" or quote_preview:
            raise MigrationError(f"계획의 빈 본문 정보가 일치하지 않습니다: {page_id}")
        page_ids.append(page_id)
        identity = (source_id, notice_id)
        if identity in identities:
            raise MigrationError("이관 계획에 동일한 공지 식별자가 중복되었습니다")
        identities.add(identity)
    if len(page_ids) != len(set(page_ids)):
        raise MigrationError("이관 계획에 페이지 ID가 중복되었습니다")
    if page_ids != sorted(page_ids) or allowlist != page_ids:
        raise MigrationError("페이지 허용 목록이 정렬된 계획 항목과 일치하지 않습니다")
    expected_digest = _canonical_hash(_plan_hash_payload(plan))
    if plan.get("plan_sha256") != expected_digest:
        raise MigrationError("이관 계획 무결성 검증에 실패했습니다")
    if plan.get("confirmation") != f"{CONFIRMATION_PREFIX} {expected_digest}":
        raise MigrationError("이관 계획 확인 문자열이 올바르지 않습니다")
    return entries


def _expected_manifest(entry: JsonObject) -> Optional[JsonObject]:
    quote_id = entry.get("quote_id")
    quote_hash = entry.get("quote_hash")
    if quote_id is None or quote_hash is None:
        return None
    return {
        "v": 2,
        "g": quote_hash,
        "s": MIGRATED_STATUS,
        "op": "",
        "t": 1,
        "p": [{"i": quote_id, "n": 1, "h": quote_hash}],
        "o": [],
    }


def _desired_properties(entry: JsonObject) -> JsonObject:
    properties: JsonObject = {
        SYNC_OWNER_PROPERTY: {
            "rich_text": build_rich_text_chunks(SYNC_OWNER_VALUE)
        },
        SOURCE_KEY_PROPERTY: {
            "rich_text": build_rich_text_chunks(str(entry["source_id"]))
        },
        NOTICE_ID_PROPERTY: {
            "rich_text": build_rich_text_chunks(str(entry["notice_id"]))
        },
        SYNC_STATUS_PROPERTY: {
            "rich_text": build_rich_text_chunks(MIGRATED_STATUS)
        },
    }
    manifest = _expected_manifest(entry)
    if manifest is not None:
        properties[SYNC_GENERATION_PROPERTY] = body_generation_property_payload(
            manifest
        )
    return properties


def _is_exactly_applied(page: JsonObject, entry: JsonObject) -> bool:
    values = _sync_values(page)
    if (
        values[SYNC_OWNER_PROPERTY] != SYNC_OWNER_VALUE
        or values[SOURCE_KEY_PROPERTY] != entry["source_id"]
        or values[NOTICE_ID_PROPERTY] != entry["notice_id"]
        or values[SYNC_STATUS_PROPERTY] != MIGRATED_STATUS
        or values[SYNC_OPERATION_PROPERTY]
    ):
        return False
    properties = page.get("properties")
    if not isinstance(properties, dict):
        return False
    expected_manifest = _expected_manifest(entry)
    if expected_manifest is None:
        return not values[SYNC_GENERATION_PROPERTY]
    return extract_body_generation_manifest(properties) == expected_manifest


def _current_entry_state(
    token: str,
    data_source_id: str,
    entry: JsonObject,
) -> str:
    page_id = str(entry["page_id"])
    page = retrieve_page(token, page_id)
    if page.get("in_trash"):
        raise MigrationError(f"페이지가 휴지통에 있습니다: {page_id}")
    if not _is_target_parent(page, data_source_id):
        raise MigrationError(f"페이지의 데이터 소스가 변경되었습니다: {page_id}")
    if _page_title(page) != entry["title"]:
        raise MigrationError(f"페이지 제목이 계획 이후 변경되었습니다: {page_id}")
    if _page_url(page) != entry["url"]:
        raise MigrationError(f"페이지 URL이 계획 이후 변경되었습니다: {page_id}")
    if _official_identity(page) != (entry["source_id"], entry["notice_id"]):
        raise MigrationError(f"페이지의 공식 공지 URL이 변경되었습니다: {page_id}")
    if _page_fingerprint(page) != entry["page_fingerprint"]:
        raise MigrationError(f"페이지 속성이 계획 이후 변경되었습니다: {page_id}")
    blocks = list_block_children(token, page_id)
    if _root_fingerprint(blocks) != entry["root_fingerprint"]:
        raise MigrationError(f"최상위 블록이 계획 이후 변경되었습니다: {page_id}")
    (
        quote_id,
        quote_hash,
        quote_marker,
        quote_preview,
    ) = _quote_snapshot(token, page_id, blocks)
    if (
        quote_id != entry.get("quote_id")
        or quote_hash != entry.get("quote_hash")
        or quote_marker != entry.get("quote_marker")
        or quote_preview != entry.get("quote_preview")
    ):
        raise MigrationError(f"인용 블록이 계획 이후 변경되었습니다: {page_id}")
    if _all_sync_fields_empty(page):
        return "pending"
    if _is_exactly_applied(page, entry):
        return "applied"
    raise MigrationError(f"동기화 메타데이터가 예상과 다릅니다: {page_id}")


def _full_preflight(
    token: str,
    data_source_id: str,
    entries: list[JsonObject],
) -> dict[str, str]:
    queried_pages = _query_all_pages(token, data_source_id)
    _assert_no_identity_duplicates(queried_pages, data_source_id)
    queried_ids = {
        _safe_id(page.get("id"), "페이지 ID")
        for page in queried_pages
    }
    missing = [
        str(entry["page_id"])
        for entry in entries
        if entry["page_id"] not in queried_ids
    ]
    if missing:
        raise MigrationError(
            f"데이터 소스에서 계획 페이지를 찾을 수 없습니다: {','.join(missing)}"
        )
    return {
        str(entry["page_id"]): _current_entry_state(
            token,
            data_source_id,
            entry,
        )
        for entry in entries
    }


def _patch_properties(token: str, page_id: str, properties: JsonObject) -> None:
    safe_page_id = _safe_id(page_id, "페이지 ID")
    notion_request(
        "PATCH",
        f"https://api.notion.com/v1/pages/{safe_page_id}",
        token,
        {"properties": properties},
    )


def _rollback_properties(
    token: str,
    data_source_id: str,
    entry: JsonObject,
) -> None:
    state = _current_entry_state(token, data_source_id, entry)
    if state == "pending":
        return
    if state != "applied":
        raise MigrationError(
            f"도구가 기록한 상태가 아니어서 되돌리지 않습니다: {entry['page_id']}"
        )
    names = [
        SYNC_OWNER_PROPERTY,
        SOURCE_KEY_PROPERTY,
        NOTICE_ID_PROPERTY,
        SYNC_STATUS_PROPERTY,
    ]
    if entry.get("quote_id") is not None:
        names.append(SYNC_GENERATION_PROPERTY)
    _patch_properties(
        token,
        str(entry["page_id"]),
        {name: {"rich_text": []} for name in names},
    )
    if _current_entry_state(token, data_source_id, entry) != "pending":
        raise MigrationError(
            f"되돌리기 후 상태 검증에 실패했습니다: {entry['page_id']}"
        )


def _verify_applied(
    token: str,
    data_source_id: str,
    entry: JsonObject,
) -> None:
    if _current_entry_state(token, data_source_id, entry) != "applied":
        raise MigrationError(f"적용 후 상태 검증에 실패했습니다: {entry['page_id']}")
    page = retrieve_page(token, str(entry["page_id"]))
    if not is_managed_page(page, str(entry["source_id"]), str(entry["notice_id"])):
        raise MigrationError(f"관리 페이지 검증에 실패했습니다: {entry['page_id']}")
    quote_hash = entry.get("quote_hash")
    if quote_hash is not None and not is_body_generation_current(
        token,
        str(entry["page_id"]),
        str(quote_hash),
    ):
        raise MigrationError(f"본문 세대 검증에 실패했습니다: {entry['page_id']}")


def apply_migration_plan(
    token: str,
    plan: JsonObject,
    confirmation: str,
    write_authorization: str = "",
) -> JsonObject:
    entries = _validated_plan_entries(plan)
    expected_confirmation = str(plan["confirmation"])
    if confirmation != expected_confirmation:
        raise MigrationError("확인 문자열이 정확히 일치하지 않습니다")
    if (
        write_authorization != LOCAL_WRITE_CONFIRMATION
        and not is_writer_context_confirmed()
    ):
        raise MigrationError(
            "허용된 쓰기 문맥이 아닙니다. 로컬 적용에는 --allow-write를 명시해야 합니다"
        )
    data_source_id = str(plan["data_source_id"])
    current_schema_fingerprint = _sync_schema_fingerprint(
        token,
        data_source_id,
    )
    if current_schema_fingerprint != plan["sync_schema_fingerprint"]:
        raise MigrationError(
            "Notion 동기화 속성 스키마가 계획 생성 이후 변경되었습니다"
        )
    first_states = _full_preflight(token, data_source_id, entries)
    second_states = _full_preflight(token, data_source_id, entries)
    if first_states != second_states:
        raise MigrationError("전체 사전 검증 사이에 페이지 상태가 변경되었습니다")
    applied_now: list[JsonObject] = []
    attempted_now: list[JsonObject] = []
    already_applied: list[str] = []
    try:
        for entry in entries:
            page_id = str(entry["page_id"])
            current_state = _current_entry_state(token, data_source_id, entry)
            if current_state == "applied":
                already_applied.append(page_id)
                continue
            if current_state != "pending":
                raise MigrationError(
                    f"페이지가 이관 대기 상태가 아닙니다: {page_id}"
                )
            attempted_now.append(entry)
            _patch_properties(token, page_id, _desired_properties(entry))
            applied_now.append(entry)
            _verify_applied(token, data_source_id, entry)
    except Exception as exc:
        rollback_failures: list[str] = []
        for applied_entry in reversed(attempted_now):
            try:
                _rollback_properties(
                    token,
                    data_source_id,
                    applied_entry,
                )
            except Exception as rollback_exc:
                rollback_failures.append(
                    f"{applied_entry['page_id']}({rollback_exc})"
                )
        suffix = (
            f"; 메타데이터 롤백 실패={','.join(rollback_failures)}"
            if rollback_failures
            else ""
        )
        raise MigrationError(f"이관 적용에 실패했습니다: {exc}{suffix}") from exc
    return {
        "applied": [str(entry["page_id"]) for entry in applied_now],
        "already_applied": already_applied,
        "total": len(entries),
    }


def _read_plan(path: Path) -> JsonObject:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(f"이관 계획을 읽을 수 없습니다: {exc}") from exc
    if not isinstance(value, dict):
        raise MigrationError("이관 계획의 최상위 값은 객체여야 합니다")
    return cast(JsonObject, value)


def _write_plan(plan: JsonObject, output: Optional[Path]) -> None:
    text = json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(text)
        return
    try:
        with output.open("x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError as exc:
        raise MigrationError(f"출력 파일이 이미 존재합니다: {output}") from exc


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="기존 Notion 공지 페이지의 동기화 메타데이터 이관 계획을 생성하거나 적용합니다"
    )
    parser.add_argument("--data-source-id")
    parser.add_argument("--page-id", action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--confirm", default="")
    parser.add_argument("--allow-write", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv()
    args = parse_args(argv)
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not token:
        raise MigrationError("NOTION_TOKEN을 환경 변수나 .env에 설정해야 합니다")
    if args.apply:
        if args.plan is None:
            raise MigrationError("--apply에는 --plan이 필요합니다")
        if args.output is not None or args.page_id or args.data_source_id:
            raise MigrationError(
                "--apply에는 --output, --page-id 또는 --data-source-id를 사용할 수 없습니다"
            )
        plan = _read_plan(args.plan)
        result = apply_migration_plan(
            token,
            plan,
            args.confirm,
            (
                LOCAL_WRITE_CONFIRMATION
                if args.allow_write
                else ""
            ),
        )
        sys.stdout.write(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        return 0
    if args.plan is not None or args.confirm or args.allow_write:
        raise MigrationError(
            "계획 생성에는 --plan, --confirm 또는 --allow-write를 사용할 수 없습니다"
        )
    data_source_id = str(
        args.data_source_id
        or os.environ.get("NOTION_DATA_SOURCE_ID", "")
    ).strip()
    if not data_source_id:
        raise MigrationError(
            "--data-source-id 또는 NOTION_DATA_SOURCE_ID가 필요합니다"
        )
    if not args.page_id:
        raise MigrationError("계획 생성에는 --page-id를 하나 이상 지정해야 합니다")
    plan = build_migration_plan(token, data_source_id, list(args.page_id))
    _write_plan(plan, args.output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MigrationError as exc:
        sys.stderr.write(f"오류: {exc}\n")
        raise SystemExit(1) from exc
