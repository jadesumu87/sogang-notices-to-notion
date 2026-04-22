import copy
import json
import re
from typing import Optional

from common import (
    ensure_item_title,
    is_empty_paragraph_block,
    normalize_body_blocks,
    rich_text_plain_text,
)
from log import LOGGER
from notion_client import (
    NotionRequestError,
    append_block_children,
    archive_page,
    delete_block,
    list_block_children,
    notion_request,
    query_database,
    query_database_page,
    update_page,
)
from settings import (
    ATTACHMENT_PROPERTY,
    ATTACHMENT_STATE_PROPERTY,
    AUTHOR_PROPERTY,
    BODY_MEDIA_STATE_PROPERTY,
    CLASSIFICATION_PROPERTY,
    DATE_PROPERTY,
    FALLBACK_TYPE,
    SYNC_CONTAINER_MARKER,
    TITLE_PROPERTY,
    TOP_PROPERTY,
    TYPE_PROPERTY,
    URL_PROPERTY,
    VIEWS_PROPERTY,
    should_allow_title_only_match,
)
from utils import (
    DEFAULT_ANNOTATIONS,
    build_container_block,
    build_file_block,
    build_pdf_block,
    build_space_rich_text,
    chunks,
    normalize_date_key,
    normalize_detail_url,
)

def extract_type_from_title(title: str) -> str:
    def normalize_type_label(raw: str) -> str:
        cleaned = (raw or "").strip()
        if not cleaned:
            return ""
        cleaned = cleaned.replace(",", "/")
        cleaned = re.sub(r"\s*/\s*", "/", cleaned)
        cleaned = re.sub(r"/{2,}", "/", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip()

    match = re.match(r"\s*\[([^\]]+)\]", title)
    if match:
        label = normalize_type_label(match.group(1))
        if label:
            return label
    return FALLBACK_TYPE


def is_image_only_blocks(blocks: list[dict]) -> bool:
    if not blocks:
        return False
    has_image = False
    for block in blocks:
        if is_empty_paragraph_block(block):
            continue
        if block.get("type") != "image":
            return False
        has_image = True
    return has_image


def has_sync_marker(rich_text: list[dict]) -> bool:
    if not rich_text:
        return False
    plain = rich_text_plain_text(rich_text)
    if not plain:
        return False
    first_line = plain.splitlines()[0].strip()
    return first_line == SYNC_CONTAINER_MARKER


def ensure_sync_marker_in_rich_text(rich_text: list[dict]) -> list[dict]:
    if has_sync_marker(rich_text):
        return rich_text
    marker_segment = {
        "type": "text",
        "text": {"content": f"{SYNC_CONTAINER_MARKER}\n"},
        "annotations": dict(DEFAULT_ANNOTATIONS),
    }
    if rich_text:
        return [marker_segment] + rich_text
    return [marker_segment]


def find_sync_container_id(token: str, page_id: str) -> Optional[str]:
    queue = list_block_children(token, page_id)
    while queue:
        block = queue.pop(0)
        if block.get("type") == "quote":
            rich_text = block.get("quote", {}).get("rich_text", [])
            if has_sync_marker(rich_text):
                return block.get("id")
        if block.get("has_children"):
            block_id = block.get("id")
            if not block_id:
                continue
            try:
                queue.extend(list_block_children(token, block_id))
            except NotionRequestError as exc:
                LOGGER.info("하위 블록 조회 실패: %s (%s)", block_id, exc)
    return None


# preserve/overwrite 공통으로 지금 페이지가 관리 중인 컨테이너를 찾는다.
def find_sync_container_block(token: str, page_id: str) -> Optional[dict]:
    top_blocks = list_block_children(token, page_id)
    quote_blocks: list[dict] = []
    for block in top_blocks:
        if block.get("type") != "quote":
            continue
        quote_blocks.append(block)
        rich_text = block.get("quote", {}).get("rich_text", [])
        if has_sync_marker(rich_text):
            return block
    # overwrite 모드에는 마커가 없으므로, 최상위 블록이 quote 하나뿐일 때만 컨테이너로 간주한다.
    # 여러 최상위 블록이 섞여 있으면 사용자가 수동으로 추가한 quote를 잘못 재사용할 수 있으니 보수적으로 포기한다.
    if len(top_blocks) == 1 and len(quote_blocks) == 1:
        return quote_blocks[0]
    return None


def is_uploaded_media_block(block: dict) -> bool:
    block_type = block.get("type")
    if block_type == "image":
        return block.get("image", {}).get("type") == "file_upload"
    if block_type in {"file", "pdf"}:
        return block.get(block_type, {}).get("type") == "file_upload"
    return False


def sanitize_uploaded_media_block(block: dict) -> Optional[dict]:
    block_type = block.get("type")
    # list_block_children 응답에는 id, created_time 같은 읽기 전용 필드가 섞여 오므로,
    # 재사용할 때는 append 가능한 최소 payload만 다시 구성해야 잘못된 블록 상태가 전파되지 않는다.
    if block_type == "image":
        image = block.get("image", {})
        if image.get("type") != "file_upload":
            return None
        upload_id = str(image.get("file_upload", {}).get("id") or "").strip()
        if not upload_id:
            return None
        sanitized = {
            "object": "block",
            "type": "image",
            "image": {"type": "file_upload", "file_upload": {"id": upload_id}},
        }
        caption = image.get("caption")
        if caption:
            sanitized["image"]["caption"] = copy.deepcopy(caption)
        return sanitized
    if block_type == "file":
        payload = block.get("file", {})
        if payload.get("type") != "file_upload":
            return None
        upload_id = str(payload.get("file_upload", {}).get("id") or "").strip()
        if not upload_id:
            return None
        return build_file_block(upload_id)
    if block_type == "pdf":
        payload = block.get("pdf", {})
        if payload.get("type") != "file_upload":
            return None
        upload_id = str(payload.get("file_upload", {}).get("id") or "").strip()
        if not upload_id:
            return None
        return build_pdf_block(upload_id)
    return None


def extract_file_upload_id_from_sanitized_block(block: dict) -> str:
    block_type = str(block.get("type") or "").strip()
    if block_type not in {"image", "file", "pdf"}:
        return ""
    payload = block.get(block_type, {})
    if payload.get("type") != "file_upload":
        return ""
    return str(payload.get("file_upload", {}).get("id") or "").strip()


def extract_body_media_state(properties: dict) -> list[dict]:
    raw = extract_rich_text_value(properties, BODY_MEDIA_STATE_PROPERTY)
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        LOGGER.info("본문 미디어 상태 파싱 실패: JSON decode error")
        return []
    if not isinstance(payload, list):
        LOGGER.info("본문 미디어 상태 파싱 실패: list 아님")
        return []
    items: list[dict] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        media_type = str(entry.get("type") or "").strip()
        source_url = str(entry.get("source_url") or "").strip()
        upload_id = str(entry.get("upload_id") or "").strip()
        if media_type not in {"image", "file", "pdf"} or not source_url:
            continue
        normalized_entry = {"type": media_type, "source_url": source_url}
        if upload_id:
            normalized_entry["upload_id"] = upload_id
        items.append(normalized_entry)
    return items


def extract_attachment_state(properties: dict) -> list[dict]:
    raw = extract_rich_text_value(properties, ATTACHMENT_STATE_PROPERTY)
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        LOGGER.info("첨부 상태 파싱 실패: JSON decode error")
        return []
    if not isinstance(payload, list):
        LOGGER.info("첨부 상태 파싱 실패: list 아님")
        return []
    items: list[dict] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        source_url = str(entry.get("source_url") or "").strip()
        upload_id = str(entry.get("upload_id") or "").strip()
        name = str(entry.get("name") or "").strip()
        if not source_url or not upload_id:
            continue
        normalized_entry = {"source_url": source_url, "upload_id": upload_id}
        if name:
            normalized_entry["name"] = name
        items.append(normalized_entry)
    return items


def normalize_item_attachments(item: dict) -> None:
    # 수집기마다 첨부가 없을 때 attachments 키를 생략할 수 있으므로,
    # 동기화 직전에는 항상 list로 정규화해 files=[] clear payload가 실제 런타임에서도 빠지지 않게 한다.
    item["attachments"] = list(item.get("attachments") or [])


def extract_existing_uploaded_attachment_ids(
    properties: dict,
    attachment_state: list[dict],
) -> dict[str, list[str]]:
    if not attachment_state:
        return {}
    files_prop = properties.get(ATTACHMENT_PROPERTY, {})
    files = files_prop.get("files", [])
    if not isinstance(files, list):
        LOGGER.info("기존 첨부 재사용 스킵: files 속성 형식 불일치")
        return {}
    current_upload_ids: set[str] = set()
    for file_info in files:
        if not isinstance(file_info, dict):
            LOGGER.info("기존 첨부 재사용 스킵: files 항목 형식 불일치")
            return {}
        file_type = str(file_info.get("type") or "").strip()
        # 현재 정책상 이미지 첨부만 file_upload로 바꾸고 나머지는 external로 남길 수 있으므로,
        # mixed attachment 페이지에서도 업로드된 첨부만 부분 재사용할 수 있게 external은 무시한다.
        if file_type == "external":
            continue
        if file_type != "file_upload":
            LOGGER.info(
                "기존 첨부 재사용 스킵: 알 수 없는 첨부 타입 감지 (%s)",
                file_type or "unknown",
            )
            return {}
        upload_id = str(file_info.get("file_upload", {}).get("id") or "").strip()
        if not upload_id:
            LOGGER.info("기존 첨부 재사용 스킵: 현재 첨부 upload_id 누락")
            return {}
        if upload_id in current_upload_ids:
            LOGGER.info(
                "기존 첨부 재사용 스킵: 현재 첨부 중복 upload_id 감지 (%s)",
                upload_id,
            )
            return {}
        current_upload_ids.add(upload_id)
    reusable: dict[str, list[str]] = {}
    seen_upload_ids: set[str] = set()
    for entry in attachment_state:
        source_url = str(entry.get("source_url") or "").strip()
        upload_id = str(entry.get("upload_id") or "").strip()
        if not source_url or not upload_id:
            LOGGER.info("기존 첨부 재사용 스킵: 상태 값 누락")
            return {}
        if upload_id in seen_upload_ids:
            LOGGER.info(
                "기존 첨부 재사용 스킵: 상태 중복 upload_id 감지 (%s)",
                upload_id,
            )
            return {}
        if upload_id not in current_upload_ids:
            LOGGER.info(
                "기존 첨부 재사용 스킵: 현재 첨부 속성에 없는 upload_id (%s)",
                upload_id,
            )
            return {}
        seen_upload_ids.add(upload_id)
        reusable.setdefault(source_url, []).append(upload_id)
    return reusable


# 이전 sync에서 이미 성공한 업로드 블록을 실제 upload_id까지 확인해 재사용해,
# 부분 성공 뒤 다음 실행에서 같은 파일을 또 올리지 않으면서도 수동 편집 오매핑을 막는다.
def extract_existing_uploaded_media_blocks(
    token: str,
    page_id: str,
    media_state: list[dict],
) -> dict[tuple[str, str], list[dict]]:
    if not page_id or not media_state:
        return {}
    try:
        container = find_sync_container_block(token, page_id)
    except NotionRequestError as exc:
        LOGGER.info("기존 본문 컨테이너 조회 실패: %s (%s)", page_id, exc)
        return {}
    if not container:
        return {}
    container_id = container.get("id")
    if not container_id:
        return {}
    try:
        children = list_block_children(token, container_id)
    except NotionRequestError as exc:
        LOGGER.info("기존 본문 미디어 조회 실패: %s (%s)", container_id, exc)
        return {}
    uploaded_blocks_by_id: dict[str, dict] = {}
    for block in children:
        if not is_uploaded_media_block(block):
            continue
        sanitized = sanitize_uploaded_media_block(block)
        if not sanitized:
            LOGGER.info(
                "기존 본문 미디어 재사용 스킵: 생성용 블록 정리 실패 (%s)",
                block.get("type"),
            )
            return {}
        upload_id = extract_file_upload_id_from_sanitized_block(sanitized)
        if not upload_id:
            LOGGER.info("기존 본문 미디어 재사용 스킵: upload_id 누락")
            return {}
        if upload_id in uploaded_blocks_by_id:
            LOGGER.info(
                "기존 본문 미디어 재사용 스킵: 중복 upload_id 감지 (%s)",
                upload_id,
            )
            return {}
        uploaded_blocks_by_id[upload_id] = sanitized
    # media_state와 현재 컨테이너의 실제 upload_id 집합이 조금이라도 다르면 재사용을 포기해,
    # 순서가 같아 보여도 수동 편집으로 다른 업로드 블록이 들어온 경우를 안전하게 차단한다.
    if len(uploaded_blocks_by_id) != len(media_state):
        LOGGER.info(
            "기존 본문 미디어 재사용 스킵: 미디어 개수 불일치 (state=%s, blocks=%s)",
            len(media_state),
            len(uploaded_blocks_by_id),
        )
        return {}
    reusable: dict[tuple[str, str], list[dict]] = {}
    seen_upload_ids: set[str] = set()
    for meta in media_state:
        upload_id = str(meta.get("upload_id") or "").strip()
        if not upload_id:
            LOGGER.info("기존 본문 미디어 재사용 스킵: 상태 upload_id 누락")
            return {}
        if upload_id in seen_upload_ids:
            LOGGER.info(
                "기존 본문 미디어 재사용 스킵: 상태 중복 upload_id 감지 (%s)",
                upload_id,
            )
            return {}
        seen_upload_ids.add(upload_id)
        block = uploaded_blocks_by_id.get(upload_id)
        if not block:
            LOGGER.info(
                "기존 본문 미디어 재사용 스킵: 현재 컨테이너에 없는 upload_id (%s)",
                upload_id,
            )
            return {}
        if str(block.get("type") or "") != meta["type"]:
            LOGGER.info(
                "기존 본문 미디어 재사용 스킵: upload_id 타입 불일치 (%s, state=%s, block=%s)",
                upload_id,
                meta["type"],
                block.get("type"),
            )
            return {}
        key = (meta["type"], meta["source_url"])
        reusable.setdefault(key, []).append(copy.deepcopy(block))
    return reusable


def update_quote_block(token: str, block_id: str, rich_text: list[dict]) -> None:
    url = f"https://api.notion.com/v1/blocks/{block_id}"
    payload = {"quote": {"rich_text": rich_text, "color": "default"}}
    notion_request("PATCH", url, token, payload)


def sync_page_body_blocks(
    token: str,
    page_id: str,
    blocks: list[dict],
    sync_mode: str = "overwrite",
) -> None:
    if not blocks:
        return
    idx = 0
    while idx < len(blocks) and is_empty_paragraph_block(blocks[idx]):
        idx += 1
    container_rich_text: list[dict] = []
    if idx < len(blocks) and blocks[idx].get("type") == "paragraph":
        container_rich_text = blocks[idx].get("paragraph", {}).get("rich_text", [])
        idx += 1
    remaining_blocks = blocks[idx:]
    if is_image_only_blocks(remaining_blocks):
        remaining_blocks = [
            block for block in remaining_blocks if not is_empty_paragraph_block(block)
        ]
        if not container_rich_text:
            container_rich_text = build_space_rich_text()
    if (sync_mode or "overwrite").strip().lower() == "preserve":
        container_rich_text = ensure_sync_marker_in_rich_text(container_rich_text)
    sync_mode = (sync_mode or "overwrite").strip().lower()

    if sync_mode == "preserve":
        container_payload = build_container_block(container_rich_text)
        container_id = find_sync_container_id(token, page_id)
        if container_id:
            update_quote_block(token, container_id, container_payload["quote"]["rich_text"])
        else:
            response = append_block_children(token, page_id, [container_payload])
            results = response.get("results", []) if isinstance(response, dict) else []
            container_id = results[0].get("id") if results else None
        if not container_id:
            LOGGER.info("컨테이너 생성 실패: %s", page_id)
            return
        for block in list_block_children(token, container_id):
            block_id = block.get("id")
            if block_id:
                try:
                    delete_block(token, block_id)
                except RuntimeError as exc:
                    LOGGER.info("블록 삭제 실패: %s (%s)", block_id, exc)
        for chunk in chunks(remaining_blocks, 80):
            append_block_children(token, container_id, chunk)
        return

    if not container_rich_text:
        container_rich_text = build_space_rich_text()
    container_payload = build_container_block(container_rich_text)
    children = list_block_children(token, page_id)
    for block in children:
        block_id = block.get("id")
        if block_id:
            try:
                delete_block(token, block_id)
            except RuntimeError as exc:
                LOGGER.info("블록 삭제 실패: %s (%s)", block_id, exc)
    response = append_block_children(token, page_id, [container_payload])
    container_id = None
    results = response.get("results", []) if isinstance(response, dict) else []
    if results:
        container_id = results[0].get("id")
    if not container_id:
        LOGGER.info("컨테이너 생성 실패: %s", page_id)
        return
    for chunk in chunks(remaining_blocks, 80):
        append_block_children(token, container_id, chunk)


def build_properties(
    item: dict,
    has_views_property: bool,
    has_attachments_property: bool,
    has_classification_property: bool,
) -> dict:
    title_text = {"content": item["title"]}
    if item.get("url"):
        title_text["link"] = {"url": item["url"]}
    props = {
        TITLE_PROPERTY: {"title": [{"type": "text", "text": title_text}]},
        TOP_PROPERTY: {"checkbox": item["top"]},
    }

    if item.get("date"):
        props[DATE_PROPERTY] = {"date": {"start": item["date"]}}
    if item.get("author"):
        props[AUTHOR_PROPERTY] = {"select": {"name": item["author"]}}
    if item.get("type"):
        props[TYPE_PROPERTY] = {"select": {"name": item["type"]}}
    if has_attachments_property and "attachments" in item:
        # 원본에서 첨부가 사라진 경우에도 files=[]를 명시적으로 보내야,
        # 예전 실행에서 남은 Notion 첨부파일 속성이 그대로 잔존하지 않는다.
        props[ATTACHMENT_PROPERTY] = {"files": item.get("attachments") or []}
    if has_views_property and item.get("views") is not None:
        props[VIEWS_PROPERTY] = {"number": item["views"]}
    if has_classification_property and item.get("classification"):
        props[CLASSIFICATION_PROPERTY] = {
            "select": {"name": item["classification"]}
        }
    if item.get("url"):
        props[URL_PROPERTY] = {"url": item["url"]}
    return props


def extract_title(properties: dict) -> str:
    title_prop = properties.get(TITLE_PROPERTY, {})
    title_parts = title_prop.get("title", [])
    text = "".join(part.get("plain_text", "") for part in title_parts).strip()
    return text


def extract_date(properties: dict) -> Optional[str]:
    date_prop = properties.get(DATE_PROPERTY, {})
    date_data = date_prop.get("date")
    if not date_data:
        return None
    start = date_data.get("start")
    if not start:
        return None
    return start


def extract_url(properties: dict) -> Optional[str]:
    url_prop = properties.get(URL_PROPERTY, {})
    url_value = url_prop.get("url")
    if not url_value:
        return None
    return normalize_detail_url(url_value)


def extract_rich_text_value(properties: dict, property_name: str) -> str:
    prop = properties.get(property_name, {})
    rich_text = prop.get("rich_text", [])
    return "".join(part.get("plain_text", "") for part in rich_text).strip()


def pick_primary_page(pages: list[dict]) -> Optional[dict]:
    if not pages:
        return None
    return max(
        pages,
        key=lambda page: (
            page.get("last_edited_time") or "",
            page.get("created_time") or "",
            page.get("id") or "",
        ),
    )


def dedupe_pages(
    token: str,
    pages: list[dict],
    reason: str,
    archive_duplicates: bool = True,
) -> Optional[dict]:
    primary = pick_primary_page(pages)
    if not primary:
        return None
    keep_id = primary.get("id")
    archived = 0
    if archive_duplicates:
        for page in pages:
            page_id = page.get("id")
            if not page_id or page_id == keep_id:
                continue
            if page.get("archived"):
                continue
            try:
                archive_page(token, page_id)
                archived += 1
            except NotionRequestError as exc:
                LOGGER.info("중복 페이지 아카이브 실패: %s (%s)", page_id, exc)
    LOGGER.info(
        "중복 페이지 정리: %s (유지=%s, 제거=%s, 총=%s)",
        reason,
        keep_id,
        archived,
        len(pages),
    )
    return primary
def iter_database_pages(token: str, database_id: str) -> list[dict]:
    payload: dict = {"page_size": 100}
    results: list[dict] = []
    while True:
        # 시작 중복 정리도 실제 운영 쿼리와 같은 재확인 경로를 타도록 맞춘다.
        data = query_database_page(token, database_id, payload)
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data.get("next_cursor")
    return results
def dedupe_database_by_url(token: str, database_id: str) -> int:
    pages = iter_database_pages(token, database_id)
    grouped: dict[str, list[dict]] = {}
    for page in pages:
        props = page.get("properties", {})
        url = extract_url(props)
        if not url:
            continue
        grouped.setdefault(url, []).append(page)
    archived = 0
    for url, group in grouped.items():
        if len(group) < 2:
            continue
        primary = pick_primary_page(group)
        if not primary:
            continue
        keep_id = primary.get("id")
        for page in group:
            page_id = page.get("id")
            if not page_id or page_id == keep_id:
                continue
            if page.get("archived"):
                continue
            try:
                archive_page(token, page_id)
                archived += 1
            except NotionRequestError as exc:
                LOGGER.info("중복 페이지 아카이브 실패: %s (%s)", page_id, exc)
        LOGGER.info("URL 중복 정리: %s (유지=%s, 중복=%s)", url, keep_id, len(group) - 1)
    return archived


# 조회 단계명을 함께 남겨서 기존 페이지 탐색이 어디에서 실패했는지 바로 구분한다.
def query_existing_pages_with_stage_log(
    token: str,
    database_id: str,
    filter_payload: dict,
    stage_name: str,
    detail_url: Optional[str],
    title: str,
    date_iso: Optional[str],
) -> list[dict]:
    try:
        return query_database(token, database_id, filter_payload)
    except NotionRequestError as exc:
        LOGGER.error(
            "기존 페이지 조회 실패: 단계=%s, 제목=%s, 작성일=%s, url=%s (%s)",
            stage_name,
            title or "제목없음",
            date_iso or "날짜없음",
            detail_url or "없음",
            exc,
        )
        raise


def find_existing_page(
    token: str,
    database_id: str,
    detail_url: Optional[str],
    title: str,
    date_iso: Optional[str],
) -> Optional[dict]:
    if detail_url:
        results = query_existing_pages_with_stage_log(
            token,
            database_id,
            {"property": URL_PROPERTY, "url": {"equals": detail_url}},
            "URL 일치 조회",
            detail_url,
            title,
            date_iso,
        )
        if len(results) == 1:
            return results[0]
        if len(results) > 1:
            return dedupe_pages(token, results, f"URL={detail_url}", archive_duplicates=True)

    if title and date_iso:
        results = query_existing_pages_with_stage_log(
            token,
            database_id,
            {
                "and": [
                    {"property": TITLE_PROPERTY, "title": {"equals": title}},
                    {"property": DATE_PROPERTY, "date": {"equals": date_iso}},
                ]
            },
            "제목+작성일 조회",
            detail_url,
            title,
            date_iso,
        )
        if len(results) == 1:
            return results[0]
        if len(results) > 1:
            return dedupe_pages(
                token,
                results,
                f"제목+작성일={title} ({date_iso})",
                archive_duplicates=True,
            )

    # 제목 단독 매칭은 오탐 업데이트 위험이 커서 설정으로 명시적으로 켠 경우에만 허용한다.
    if title and should_allow_title_only_match():
        results = query_existing_pages_with_stage_log(
            token,
            database_id,
            {"property": TITLE_PROPERTY, "title": {"equals": title}},
            "제목 단독 조회",
            detail_url,
            title,
            date_iso,
        )
        if len(results) == 1:
            return results[0]
        if len(results) > 1:
            primary = pick_primary_page(results)
            if primary:
                LOGGER.info(
                    "제목 중복 감지(삭제 생략): %s (유지=%s, 총=%s)",
                    title,
                    primary.get("id"),
                    len(results),
                )
                return primary
    return None
def iter_top_pages(token: str, database_id: str):
    payload = {
        "filter": {"property": TOP_PROPERTY, "checkbox": {"equals": True}},
        "page_size": 100,
    }

    while True:
        # TOP 정리도 DB 조회 실패 유형을 동일한 기준으로 해석할 수 있게 공통 helper를 사용한다.
        data = query_database_page(token, database_id, payload)
        for page in data.get("results", []):
            yield page
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data.get("next_cursor")


def disable_missing_top(
    token: str,
    database_id: str,
    current_top_urls: set[str],
    current_top_dates: dict[str, set[str]],
) -> int:
    disabled = 0
    for page in iter_top_pages(token, database_id):
        props = page.get("properties", {})
        page_url = extract_url(props)
        if page_url and current_top_urls:
            if page_url in current_top_urls:
                continue
        title = extract_title(props)
        if not title:
            continue
        date_iso = extract_date(props)
        date_key = normalize_date_key(date_iso)
        title_dates = current_top_dates.get(title)
        if title_dates is not None and date_key in title_dates:
            continue
        update_page(token, page["id"], {TOP_PROPERTY: {"checkbox": False}})
        disabled += 1
        LOGGER.info("TOP 해제: %s (%s)", title, date_iso or "날짜없음")
    return disabled
