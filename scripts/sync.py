import copy
import hashlib
import json
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from common import (
    ATTACHMENTS_STATUS_KNOWN,
    ATTACHMENTS_STATUS_UNKNOWN,
    extract_detail_id_from_text,
    is_empty_paragraph_block,
    rich_text_plain_text,
)
from log import LOGGER
from models import DestinationConsistencyError
from notion_client import (
    NotionRequestError,
    append_block_children,
    delete_block,
    encode_notion_payload,
    list_block_children,
    notion_request,
    query_database,
    query_database_page,
    retrieve_page,
    update_page,
)
from run_control import check_run_control, sleep_with_run_control
from settings import (
    ATTACHMENT_PROPERTY,
    ATTACHMENT_STATE_PROPERTY,
    AUTHOR_PROPERTY,
    BODY_HASH_PROPERTY,
    BODY_MEDIA_STATE_PROPERTY,
    CLASSIFICATION_PROPERTY,
    DATE_PROPERTY,
    FALLBACK_TYPE,
    LEGACY_SYNC_CONTAINER_MARKER,
    NOTICE_ID_PROPERTY,
    SOURCE_KEY_PROPERTY,
    SYNC_CONTAINER_MARKER,
    SYNC_GENERATION_PROPERTY,
    SYNC_OPERATION_PROPERTY,
    SYNC_OWNER_PROPERTY,
    SYNC_OWNER_VALUE,
    SYNC_STATUS_PROPERTY,
    TITLE_PROPERTY,
    TOP_PROPERTY,
    TYPE_PROPERTY,
    URL_PROPERTY,
    VIEWS_PROPERTY,
    get_top_disable_max_count,
    get_top_disable_max_ratio,
)
from utils import (
    DEFAULT_ANNOTATIONS,
    build_container_block,
    build_file_block,
    build_pdf_block,
    build_rich_text_chunks,
    build_space_rich_text,
    chunks,
    normalize_attachment_identity_url,
    normalize_attachment_name,
    normalize_content_sha256,
)


TOP_COMMIT_READBACK_DELAYS = (0.0, 0.25, 0.75)

JsonObject = dict[str, Any]
BODY_GENERATION_MANIFEST_VERSION = 2
BODY_GENERATION_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
BODY_GENERATION_HASH_RE = re.compile(r"[0-9a-f]{64}")
DEFAULT_COLOR_BLOCK_TYPES = frozenset(
    {
        "bulleted_list_item",
        "callout",
        "heading_1",
        "heading_2",
        "heading_3",
        "heading_4",
        "numbered_list_item",
        "paragraph",
        "quote",
        "table_of_contents",
        "to_do",
        "toggle",
    }
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
    return str(FALLBACK_TYPE)


def is_image_only_blocks(blocks: list[JsonObject]) -> bool:
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


def has_sync_marker(rich_text: list[JsonObject]) -> bool:
    if not rich_text:
        return False
    plain = str(rich_text_plain_text(rich_text))
    if not plain:
        return False
    first_line = plain.splitlines()[0].strip()
    return first_line in {
        SYNC_CONTAINER_MARKER,
        LEGACY_SYNC_CONTAINER_MARKER,
    }


def has_current_sync_marker(rich_text: list[JsonObject]) -> bool:
    if not rich_text:
        return False
    plain = str(rich_text_plain_text(rich_text))
    if not plain:
        return False
    return plain.splitlines()[0].strip() == str(SYNC_CONTAINER_MARKER)


def extract_sync_generation(rich_text: list[JsonObject]) -> tuple[str, int, int]:
    if not has_current_sync_marker(rich_text):
        if has_sync_marker(rich_text):
            return "legacy", 1, 1
        return "", 0, 0
    plain = str(rich_text_plain_text(rich_text))
    lines = plain.splitlines()
    if len(lines) < 2:
        return "legacy", 1, 1
    match = re.fullmatch(r"GENERATION:([A-Za-z0-9._-]{1,128}):(\d+)/(\d+)", lines[1].strip())
    if not match:
        return "legacy", 1, 1
    part = int(match.group(2))
    total = int(match.group(3))
    if part < 1 or total < 1 or part > total:
        return "", 0, 0
    return match.group(1), part, total


def first_rich_text_content(rich_text: list[JsonObject]) -> str:
    if not rich_text or not isinstance(rich_text[0], dict):
        return ""
    first = rich_text[0]
    text = first.get("text")
    if isinstance(text, dict):
        content = text.get("content")
        if isinstance(content, str):
            return content
    plain_text = first.get("plain_text")
    return plain_text if isinstance(plain_text, str) else ""


def extract_sync_content_hash(rich_text: list[JsonObject]) -> str:
    if not has_current_sync_marker(rich_text):
        return ""
    lines = first_rich_text_content(rich_text).splitlines()
    if (
        len(lines) != 3
        or lines[0].strip() != str(SYNC_CONTAINER_MARKER)
    ):
        return ""
    generation_match = re.fullmatch(
        r"GENERATION:([A-Za-z0-9._-]{1,128}):(\d+)/(\d+)",
        lines[1].strip(),
    )
    content_match = re.fullmatch(
        r"CONTENT_SHA256:([0-9a-f]{64})",
        lines[2].strip(),
    )
    if not generation_match or not content_match:
        return ""
    return content_match.group(1)


def ensure_sync_marker_in_rich_text(
    rich_text: list[JsonObject],
    generation_id: str = "legacy",
    part: int = 1,
    total: int = 1,
    content_hash: str = "",
) -> list[JsonObject]:
    if content_hash and not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        raise RuntimeError("본문 콘텐츠 해시가 유효하지 않습니다")
    if (
        has_sync_marker(rich_text)
        and extract_sync_generation(rich_text)[0] == generation_id
        and extract_sync_content_hash(rich_text) == content_hash
    ):
        return rich_text
    content_line = (
        f"CONTENT_SHA256:{content_hash}\n"
        if content_hash
        else ""
    )
    marker_segment = {
        "type": "text",
        "text": {
            "content": (
                f"{SYNC_CONTAINER_MARKER}\n"
                f"GENERATION:{generation_id}:{part}/{total}\n"
                f"{content_line}"
            )
        },
        "annotations": dict(DEFAULT_ANNOTATIONS),
    }
    if has_sync_marker(rich_text):
        plain = str(rich_text_plain_text(rich_text))
        lines = plain.splitlines()
        body_lines = (
            lines[3:]
            if extract_sync_content_hash(rich_text)
            else lines[2:]
            if has_current_sync_marker(rich_text)
            else lines[1:]
        )
        body_text = "\n".join(body_lines)
        rich_text = (
            [
                {
                    "type": "text",
                    "text": {"content": body_text},
                    "annotations": dict(DEFAULT_ANNOTATIONS),
                }
            ]
            if body_text
            else []
        )
    if rich_text:
        return [marker_segment] + rich_text
    return [marker_segment]


def is_managed_sync_rich_text(rich_text: list[JsonObject]) -> bool:
    generation_id, _, _ = extract_sync_generation(rich_text)
    return bool(generation_id) and has_sync_marker(rich_text)


def list_sync_container_blocks(token: str, page_id: str) -> list[JsonObject]:
    manifest = load_body_generation_manifest(token, page_id)
    if not manifest:
        return []
    if manifest.get("v") == 1:
        generation_id = str(manifest.get("g") or "")
        return [
            block
            for block in list_block_children(token, page_id)
            if (
                block.get("type") == "quote"
                and extract_sync_generation(
                    block.get("quote", {}).get("rich_text", [])
                )[0]
                == generation_id
            )
        ]
    if (
        manifest.get("v") != BODY_GENERATION_MANIFEST_VERSION
        or manifest.get("s") not in {"pending", "committed"}
    ):
        return []
    return [
        block
        for _, block in body_generation_blocks_from_manifest(
            token,
            page_id,
            manifest,
        )
    ]


def group_sync_containers(blocks: list[JsonObject]) -> dict[str, list[tuple[int, int, JsonObject]]]:
    grouped: dict[str, list[tuple[int, int, JsonObject]]] = {}
    for block in blocks:
        generation_id, part, total = extract_sync_generation(
            block.get("quote", {}).get("rich_text", [])
        )
        if not generation_id:
            continue
        grouped.setdefault(generation_id, []).append((part, total, block))
    return grouped


def normalize_body_generation_id(value: object) -> str:
    generation_id = str(value or "").strip()
    return (
        generation_id
        if BODY_GENERATION_ID_RE.fullmatch(generation_id)
        else ""
    )


def parse_body_generation_manifest(raw: object) -> Optional[JsonObject]:
    text = str(raw or "").strip()
    if not text:
        return None
    if not text.startswith("{"):
        generation_id = normalize_body_generation_id(text)
        return (
            {
                "v": 1,
                "g": generation_id,
                "s": "legacy",
                "op": "",
                "t": 0,
                "p": [],
                "o": [],
            }
            if generation_id
            else None
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("v") != BODY_GENERATION_MANIFEST_VERSION:
        return None
    generation_id = normalize_body_generation_id(payload.get("g"))
    status = str(payload.get("s") or "").strip()
    operation_id = normalize_body_generation_id(payload.get("op"))
    total = payload.get("t")
    if (
        not generation_id
        or status not in {"pending", "committed"}
        or not isinstance(total, int)
        or isinstance(total, bool)
        or total < 1
        or total > 100
    ):
        return None

    def normalize_parts(
        values: object,
        *,
        require_part: bool,
    ) -> Optional[list[JsonObject]]:
        if not isinstance(values, list) or len(values) > 100:
            return None
        normalized: list[JsonObject] = []
        seen_ids: set[str] = set()
        seen_parts: set[int] = set()
        for value in values:
            if not isinstance(value, dict):
                return None
            block_id = str(value.get("i") or "").strip()
            content_hash = str(value.get("h") or "").strip()
            part = value.get("n")
            if (
                not block_id
                or len(block_id) > 200
                or not BODY_GENERATION_HASH_RE.fullmatch(content_hash)
            ):
                return None
            if block_id in seen_ids:
                return None
            normalized_entry: JsonObject = {
                "i": block_id,
                "h": content_hash,
            }
            if require_part:
                if (
                    not isinstance(part, int)
                    or isinstance(part, bool)
                    or part < 1
                    or part > total
                    or part in seen_parts
                ):
                    return None
                normalized_entry["n"] = part
                seen_parts.add(part)
            normalized.append(normalized_entry)
            seen_ids.add(block_id)
        return normalized

    parts = normalize_parts(payload.get("p"), require_part=True)
    old_parts = normalize_parts(payload.get("o", []), require_part=False)
    if parts is None or old_parts is None:
        return None
    if status == "committed" and sorted(
        int(part["n"]) for part in parts
    ) != list(range(1, total + 1)):
        return None
    normalized_manifest: JsonObject = {
        "v": BODY_GENERATION_MANIFEST_VERSION,
        "g": generation_id,
        "s": status,
        "op": operation_id,
        "t": total,
        "p": sorted(parts, key=lambda part: int(part["n"])),
        "o": old_parts,
    }
    return normalized_manifest


def serialize_body_generation_manifest(manifest: JsonObject) -> str:
    normalized = parse_body_generation_manifest(
        json.dumps(
            manifest,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    if not normalized or normalized["v"] != BODY_GENERATION_MANIFEST_VERSION:
        raise RuntimeError("본문 세대 매니페스트가 유효하지 않습니다")
    payload: JsonObject = {
        "v": normalized["v"],
        "g": normalized["g"],
        "s": normalized["s"],
        "op": normalized["op"],
        "t": normalized["t"],
        "p": normalized["p"],
    }
    if normalized["o"]:
        payload["o"] = normalized["o"]
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def extract_body_generation_manifest(
    properties: JsonObject,
) -> Optional[JsonObject]:
    raw = rich_text_value_from_payload(
        properties.get(SYNC_GENERATION_PROPERTY, {})
    )
    return parse_body_generation_manifest(raw)


def extract_body_generation_id(properties: JsonObject) -> str:
    manifest = extract_body_generation_manifest(properties)
    return str(manifest.get("g") or "") if manifest else ""


def untracked_top_level_quote_ids(
    token: str,
    page: JsonObject,
) -> list[str]:
    return [
        str(entry["id"])
        for entry in top_level_quote_state(token, page)
        if not bool(entry["managed"])
    ]


def top_level_quote_state(
    token: str,
    page: JsonObject,
) -> list[JsonObject]:
    page_id = str(page.get("id") or "").strip()
    properties = page.get("properties", {})
    if not page_id or not isinstance(properties, dict):
        raise RuntimeError("Notion 페이지 식별 정보가 올바르지 않습니다")
    manifest = extract_body_generation_manifest(properties)
    tracked_hashes = {
        str(part.get("i") or "").strip(): str(part.get("h") or "")
        for part in [
            *(manifest.get("p", []) if manifest else []),
            *(manifest.get("o", []) if manifest else []),
        ]
        if isinstance(part, dict)
        and str(part.get("i") or "").strip()
    }
    legacy_generation_id = (
        str(manifest.get("g") or "")
        if manifest and manifest.get("v") == 1
        else ""
    )
    state: list[JsonObject] = []
    for block in list_block_children(token, page_id):
        if block.get("type") != "quote":
            continue
        block_id = str(block.get("id") or "").strip()
        if not block_id:
            raise RuntimeError("최상위 인용 블록 ID를 확인할 수 없습니다")
        expected_hash = tracked_hashes.get(block_id, "")
        rich_text = block.get("quote", {}).get("rich_text", [])
        legacy_authenticated = bool(
            legacy_generation_id
            and isinstance(rich_text, list)
            and extract_sync_generation(rich_text)[0]
            == legacy_generation_id
        )
        content_hash = (
            sync_container_actual_hash(
                token,
                block,
                marker_authenticated=True,
            )
            if legacy_authenticated
            else sync_container_actual_hash_for_expected(
                token,
                block,
                expected_hash,
            )
        )
        if not BODY_GENERATION_HASH_RE.fullmatch(content_hash):
            raise RuntimeError("최상위 인용 블록 내용을 검증할 수 없습니다")
        state.append(
            {
                "id": block_id,
                "managed": bool(
                    block_id in tracked_hashes
                    or legacy_authenticated
                ),
                "content_hash": content_hash,
            }
        )
    return state


def body_generation_property_payload(manifest: JsonObject) -> JsonObject:
    return {
        "rich_text": build_rich_text_chunks(
            serialize_body_generation_manifest(manifest)
        )
    }


def load_body_generation_manifest(
    token: str,
    page_id: str,
) -> Optional[JsonObject]:
    page = retrieve_page(token, page_id)
    properties = page.get("properties", {})
    return (
        extract_body_generation_manifest(properties)
        if isinstance(properties, dict)
        else None
    )


def body_generation_blocks_from_manifest(
    token: str,
    page_id: str,
    manifest: JsonObject,
) -> list[tuple[int, JsonObject]]:
    if manifest.get("v") != BODY_GENERATION_MANIFEST_VERSION:
        return []
    top_blocks = list_block_children(token, page_id)
    blocks_by_id = {
        str(block.get("id") or "").strip(): block
        for block in top_blocks
        if str(block.get("id") or "").strip()
    }
    resolved: list[tuple[int, JsonObject]] = []
    for part in manifest.get("p", []):
        block = blocks_by_id.get(str(part.get("i") or ""))
        if not block or block.get("type") != "quote":
            return []
        resolved.append((int(part["n"]), block))
    return sorted(resolved, key=lambda entry: entry[0])


def find_sync_container_id(token: str, page_id: str) -> Optional[str]:
    block = find_sync_container_block(token, page_id)
    block_id = str(block.get("id") or "").strip() if block else ""
    return block_id or None


def find_body_generation_blocks(
    token: str,
    page_id: str,
    generation_id: str = "",
) -> list[JsonObject]:
    manifest = load_body_generation_manifest(token, page_id)
    if (
        manifest
        and manifest.get("v") == 1
        and (
            not generation_id
            or manifest.get("g") == generation_id
        )
    ):
        legacy_generation_id = str(manifest.get("g") or "")
        return [
            block
            for block in list_block_children(token, page_id)
            if (
                block.get("type") == "quote"
                and extract_sync_generation(
                    block.get("quote", {}).get("rich_text", [])
                )[0]
                == legacy_generation_id
            )
        ]
    if (
        manifest
        and manifest.get("v") == BODY_GENERATION_MANIFEST_VERSION
        and manifest.get("s") in {"pending", "committed"}
        and (
            not generation_id
            or manifest.get("g") == generation_id
        )
    ):
        resolved = body_generation_blocks_from_manifest(
            token,
            page_id,
            manifest,
        )
        if resolved:
            return [block for _, block in resolved]
    return []


def find_sync_container_block(token: str, page_id: str) -> Optional[JsonObject]:
    blocks = find_body_generation_blocks(token, page_id)
    return blocks[0] if blocks else None


def is_notion_hosted_media_block(block: JsonObject) -> bool:
    block_type = block.get("type")
    if block_type == "image":
        return block.get("image", {}).get("type") in {"file", "file_upload"}
    if block_type in {"file", "pdf"}:
        return block.get(block_type, {}).get("type") in {"file", "file_upload"}
    return False


def sanitize_uploaded_media_block(block: JsonObject, upload_id: str) -> Optional[JsonObject]:
    block_type = block.get("type")
    clean_upload_id = str(upload_id or "").strip()
    if not clean_upload_id:
        return None
    if block_type == "image":
        image = block.get("image", {})
        if image.get("type") not in {"file", "file_upload"}:
            return None
        sanitized: JsonObject = {
            "object": "block",
            "type": "image",
            "image": {"type": "file_upload", "file_upload": {"id": clean_upload_id}},
        }
        caption = image.get("caption")
        if caption:
            sanitized["image"]["caption"] = copy.deepcopy(caption)
        return sanitized
    if block_type == "file":
        payload = block.get("file", {})
        if payload.get("type") not in {"file", "file_upload"}:
            return None
        sanitized_file: JsonObject = build_file_block(clean_upload_id)
        return sanitized_file
    if block_type == "pdf":
        payload = block.get("pdf", {})
        if payload.get("type") not in {"file", "file_upload"}:
            return None
        sanitized_pdf: JsonObject = build_pdf_block(clean_upload_id)
        return sanitized_pdf
    return None


def extract_body_media_state(properties: JsonObject) -> list[JsonObject]:
    raw = extract_rich_text_value(properties, BODY_MEDIA_STATE_PROPERTY)
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        LOGGER.info("본문 미디어 상태 파싱 실패: JSON 해석 오류")
        return []
    if not isinstance(payload, list):
        LOGGER.info("본문 미디어 상태 파싱 실패: 배열이 아님")
        return []
    items: list[JsonObject] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        media_type = str(entry.get("type") or "").strip()
        source_url = str(entry.get("source_url") or "").strip()
        upload_id = str(entry.get("upload_id") or "").strip()
        block_id = str(entry.get("block_id") or "").strip()
        hosted_file_key = str(entry.get("hosted_file_key") or "").strip()
        generation_id = str(entry.get("generation_id") or "").strip()
        content_sha256 = normalize_content_sha256(
            entry.get("content_sha256")
        )
        if media_type not in {"image", "file", "pdf"} or not source_url:
            continue
        normalized_entry = {"type": media_type, "source_url": source_url}
        if upload_id:
            normalized_entry["upload_id"] = upload_id
        if block_id:
            normalized_entry["block_id"] = block_id
        if hosted_file_key:
            normalized_entry["hosted_file_key"] = hosted_file_key
        if generation_id:
            normalized_entry["generation_id"] = generation_id
        if content_sha256:
            normalized_entry["content_sha256"] = content_sha256
        items.append(normalized_entry)
    return items


def normalize_attachment_state_entries(entries: list[JsonObject]) -> list[JsonObject]:
    name_counts: dict[str, int] = {}
    normalized: list[JsonObject] = []
    for entry in entries:
        source_url = str(entry.get("source_url") or "").strip()
        name = str(entry.get("name") or "").strip()
        upload_id = str(entry.get("upload_id") or "").strip()
        content_sha256 = normalize_content_sha256(
            entry.get("content_sha256")
        )
        name_key = normalize_attachment_name(name)
        occurrence = name_counts.get(name_key, 0) + 1
        name_counts[name_key] = occurrence
        normalized_entry = {
            "source_url": source_url,
            "name": name,
            "upload_id": upload_id,
            "occurrence": occurrence,
        }
        hosted_file_key = str(entry.get("hosted_file_key") or "").strip()
        generation_id = str(entry.get("generation_id") or "").strip()
        if content_sha256:
            normalized_entry["content_sha256"] = content_sha256
        if hosted_file_key:
            normalized_entry["hosted_file_key"] = hosted_file_key
        if generation_id:
            normalized_entry["generation_id"] = generation_id
        normalized.append(normalized_entry)
    return normalized


def extract_attachment_state(properties: JsonObject) -> list[JsonObject]:
    raw = extract_rich_text_value(properties, ATTACHMENT_STATE_PROPERTY)
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        LOGGER.info("첨부 상태 파싱 실패: JSON 해석 오류")
        return []
    if not isinstance(payload, list):
        LOGGER.info("첨부 상태 파싱 실패: 배열이 아님")
        return []
    items: list[JsonObject] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        source_url = str(entry.get("source_url") or "").strip()
        upload_id = str(entry.get("upload_id") or "").strip()
        name = str(entry.get("name") or "").strip()
        hosted_file_key = str(entry.get("hosted_file_key") or "").strip()
        generation_id = str(entry.get("generation_id") or "").strip()
        content_sha256 = normalize_content_sha256(
            entry.get("content_sha256")
        )
        if not source_url or not upload_id:
            continue
        normalized_entry = {
            "source_url": source_url,
            "name": name,
            "upload_id": upload_id,
        }
        if hosted_file_key:
            normalized_entry["hosted_file_key"] = hosted_file_key
        if generation_id:
            normalized_entry["generation_id"] = generation_id
        if content_sha256:
            normalized_entry["content_sha256"] = content_sha256
        items.append(normalized_entry)
    return normalize_attachment_state_entries(items)


def normalize_item_attachments(item: JsonObject) -> None:
    status = str(item.get("attachments_status") or "").strip()
    if status == ATTACHMENTS_STATUS_UNKNOWN:
        item.pop("attachments", None)
        return
    if "attachments" in item:
        item["attachments"] = list(item.get("attachments") or [])
        item["attachments_status"] = ATTACHMENTS_STATUS_KNOWN
        return
    if status == ATTACHMENTS_STATUS_KNOWN:
        item["attachments"] = []


def normalize_notion_hosted_file_key(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.path:
        return ""
    return f"{parsed.netloc}{parsed.path}"


def extract_notion_hosted_file_key_from_block(block: JsonObject) -> str:
    block_type = str(block.get("type") or "").strip()
    if block_type not in {"image", "file", "pdf"}:
        return ""
    payload = block.get(block_type, {})
    if payload.get("type") != "file":
        return ""
    return normalize_notion_hosted_file_key(payload.get("file", {}).get("url") or "")


def uploaded_attachment_entries_from_properties(properties: JsonObject) -> list[JsonObject]:
    files_prop = properties.get(ATTACHMENT_PROPERTY, {})
    files = files_prop.get("files", [])
    if not isinstance(files, list):
        return []
    name_counts: dict[str, int] = {}
    uploaded: list[JsonObject] = []
    for file_info in files:
        if not isinstance(file_info, dict):
            return []
        file_type = str(file_info.get("type") or "").strip()
        if file_type == "external":
            continue
        name = str(file_info.get("name") or "").strip()
        name_key = normalize_attachment_name(name)
        if not name_key:
            return []
        occurrence = name_counts.get(name_key, 0) + 1
        name_counts[name_key] = occurrence
        entry = {
            "type": file_type,
            "name": name,
            "name_key": name_key,
            "occurrence": occurrence,
        }
        if file_type == "file":
            hosted_file_key = normalize_notion_hosted_file_key(
                file_info.get("file", {}).get("url") or ""
            )
            if not hosted_file_key:
                return []
            entry["hosted_file_key"] = hosted_file_key
        elif file_type == "file_upload":
            upload_id = str(
                file_info.get("file_upload", {}).get("id") or ""
            ).strip()
            if not upload_id:
                return []
            entry["upload_id"] = upload_id
        else:
            return []
        uploaded.append(entry)
    return uploaded


def enrich_attachment_state_with_properties(
    properties: JsonObject,
    attachment_state: list[JsonObject],
    allow_opaque_binding: bool = False,
) -> list[JsonObject]:
    if not attachment_state:
        return attachment_state
    normalized_state = normalize_attachment_state_entries(attachment_state)
    current_uploaded_entries = uploaded_attachment_entries_from_properties(
        properties
    )
    if len(current_uploaded_entries) != len(normalized_state):
        return normalized_state
    enriched: list[JsonObject] = []
    for state_entry, current_entry in zip(
        normalized_state,
        current_uploaded_entries,
        strict=True,
    ):
        state_name_key = normalize_attachment_name(
            str(state_entry.get("name") or "")
        )
        state_occurrence = int(state_entry.get("occurrence") or 0)
        if (
            not state_name_key
            or state_name_key != current_entry["name_key"]
            or state_occurrence != current_entry["occurrence"]
        ):
            return normalized_state
        enriched_entry = dict(state_entry)
        if current_entry["type"] == "file":
            upload_id = str(state_entry.get("upload_id") or "").strip()
            hosted_file_key = str(
                state_entry.get("hosted_file_key") or ""
            ).strip()
            if not upload_id:
                return normalized_state
            if hosted_file_key:
                if hosted_file_key != current_entry["hosted_file_key"]:
                    return normalized_state
            elif (
                not allow_opaque_binding
                and upload_id not in current_entry["hosted_file_key"]
            ):
                return normalized_state
            enriched_entry["hosted_file_key"] = current_entry["hosted_file_key"]
        elif (
            str(state_entry.get("upload_id") or "").strip()
            != current_entry["upload_id"]
        ):
            return normalized_state
        enriched.append(enriched_entry)
    return enriched


def enrich_attachment_state_with_page(
    token: str,
    page_id: str,
    attachment_state: list[JsonObject],
) -> list[JsonObject]:
    if not page_id or not attachment_state:
        return attachment_state
    try:
        page = notion_request("GET", f"https://api.notion.com/v1/pages/{page_id}", token)
    except NotionRequestError as exc:
        LOGGER.info("첨부 상태의 호스팅 파일 식별자 보강 생략: 페이지 조회 실패 (%s)", exc)
        return attachment_state
    return enrich_attachment_state_with_properties(
        page.get("properties", {}),
        attachment_state,
        allow_opaque_binding=True,
    )


def extract_existing_uploaded_attachment_ids(
    properties: JsonObject,
    attachment_state: list[JsonObject],
) -> dict[str, list[JsonObject]]:
    if not attachment_state:
        return {}
    normalized_state = normalize_attachment_state_entries(attachment_state)
    current_uploaded_entries = uploaded_attachment_entries_from_properties(
        properties
    )
    if len(current_uploaded_entries) != len(normalized_state):
        LOGGER.info(
            "기존 첨부 재사용 생략: 업로드 첨부 개수 불일치 (상태=%s, 현재=%s)",
            len(normalized_state),
            len(current_uploaded_entries),
        )
        return {}
    reusable: dict[str, list[JsonObject]] = {}
    for entry, current_entry in zip(
        normalized_state,
        current_uploaded_entries,
        strict=True,
    ):
        source_url = str(entry.get("source_url") or "").strip()
        upload_id = str(entry.get("upload_id") or "").strip()
        name = str(entry.get("name") or "").strip()
        hosted_file_key = str(entry.get("hosted_file_key") or "").strip()
        name_key = normalize_attachment_name(name)
        occurrence = int(entry.get("occurrence") or 0)
        content_sha256 = normalize_content_sha256(
            entry.get("content_sha256")
        )
        if (
            not source_url
            or not upload_id
            or not name_key
            or name_key != current_entry["name_key"]
            or occurrence != current_entry["occurrence"]
        ):
            LOGGER.info("기존 첨부 재사용 생략: 상태 값 누락")
            return {}
        if current_entry["type"] == "file":
            if (
                not hosted_file_key
                or hosted_file_key != current_entry["hosted_file_key"]
            ):
                LOGGER.info("기존 첨부 재사용 생략: 호스팅 파일 식별자 불일치")
                return {}
        elif upload_id != current_entry["upload_id"]:
            LOGGER.info(
                "기존 첨부 재사용 생략: 현재 첨부 업로드 ID 불일치 (%s)",
                upload_id,
            )
            return {}
        reusable.setdefault(
            normalize_attachment_identity_url(source_url),
            [],
        ).append(
            {
                "upload_id": upload_id,
                "content_sha256": content_sha256,
            }
        )
    return reusable


def body_media_container_blocks(
    token: str,
    page_id: str,
    media_state: list[JsonObject],
    generation_id: str = "",
) -> tuple[list[JsonObject], str]:
    state_generations = {
        normalize_body_generation_id(entry.get("generation_id"))
        for entry in media_state
        if normalize_body_generation_id(entry.get("generation_id"))
    }
    requested_generation = normalize_body_generation_id(generation_id)
    if not requested_generation and len(state_generations) == 1:
        requested_generation = next(iter(state_generations))
    if len(state_generations) > 1:
        return [], "drift"
    try:
        if requested_generation:
            containers = find_body_generation_blocks(
                token,
                page_id,
                requested_generation,
            )
        else:
            container = find_sync_container_block(token, page_id)
            containers = [container] if container else []
    except NotionRequestError:
        return [], "unavailable"
    if not containers:
        return [], "drift"
    return containers, "valid"


def hosted_media_blocks_from_containers(
    token: str,
    containers: list[JsonObject],
) -> tuple[list[JsonObject], str]:
    hosted_blocks: list[JsonObject] = []
    for container in containers:
        container_id = str(container.get("id") or "").strip()
        if not container_id:
            return [], "unavailable"
        try:
            children = list_block_children(token, container_id)
        except NotionRequestError:
            return [], "unavailable"
        hosted_blocks.extend(
            block
            for block in children
            if is_notion_hosted_media_block(block)
        )
    return hosted_blocks, "valid"


def inspect_existing_uploaded_media_blocks(
    token: str,
    page_id: str,
    media_state: list[JsonObject],
) -> tuple[dict[tuple[str, str], list[JsonObject]], str]:
    if not page_id or not media_state:
        return {}, "valid"
    containers, container_status = body_media_container_blocks(
        token,
        page_id,
        media_state,
    )
    if container_status != "valid":
        LOGGER.info(
            "기존 본문 컨테이너 검증 실패: %s (%s)",
            page_id,
            container_status,
        )
        return {}, container_status
    hosted_blocks_in_order, media_status = hosted_media_blocks_from_containers(
        token,
        containers,
    )
    if media_status != "valid":
        LOGGER.info(
            "기존 본문 미디어 조회 실패: %s (%s)",
            page_id,
            media_status,
        )
        return {}, media_status
    if len(hosted_blocks_in_order) != len(media_state):
        LOGGER.info(
            "기존 본문 미디어 재사용 생략: 미디어 개수 불일치 (상태=%s, 블록=%s)",
            len(media_state),
            len(hosted_blocks_in_order),
        )
        return {}, "drift"
    blocks_by_id: dict[str, JsonObject] = {}
    for block in hosted_blocks_in_order:
        block_id = str(block.get("id") or "").strip()
        if not block_id:
            LOGGER.info("기존 본문 미디어 재사용 생략: 현재 블록 ID 누락")
            return {}, "unavailable"
        if block_id in blocks_by_id:
            LOGGER.info("기존 본문 미디어 재사용 생략: 현재 블록 ID 중복 (%s)", block_id)
            return {}, "unavailable"
        blocks_by_id[block_id] = block
    reusable: dict[tuple[str, str], list[JsonObject]] = {}
    seen_upload_ids: set[str] = set()
    state_has_block_ids = all(str(meta.get("block_id") or "").strip() for meta in media_state)
    if state_has_block_ids:
        for meta in media_state:
            upload_id = str(meta.get("upload_id") or "").strip()
            block_id = str(meta.get("block_id") or "").strip()
            hosted_file_key = str(meta.get("hosted_file_key") or "").strip()
            if not block_id:
                LOGGER.info("기존 본문 미디어 재사용 생략: 상태 값 누락")
                return {}, "unavailable"
            if upload_id and upload_id in seen_upload_ids:
                LOGGER.info(
                    "기존 본문 미디어 재사용 생략: 상태에서 중복 업로드 ID 감지 (%s)",
                    upload_id,
                )
                return {}, "unavailable"
            if upload_id:
                seen_upload_ids.add(upload_id)
            matched_block = blocks_by_id.get(block_id)
            if not matched_block:
                LOGGER.info(
                    "기존 본문 미디어 재사용 생략: 현재 컨테이너에 없는 블록 ID (%s)",
                    block_id,
                )
                return {}, "drift"
            if str(matched_block.get("type") or "") != meta["type"]:
                LOGGER.info(
                    "기존 본문 미디어 재사용 생략: 블록 ID의 타입 불일치 (%s, 상태=%s, 블록=%s)",
                    block_id,
                    meta["type"],
                    matched_block.get("type"),
                )
                return {}, "drift"
            if hosted_file_key:
                current_hosted_file_key = extract_notion_hosted_file_key_from_block(
                    matched_block
                )
                if not current_hosted_file_key:
                    LOGGER.info(
                        "기존 본문 미디어 재사용 생략: 현재 블록의 호스팅 파일 식별자 누락 (%s)",
                        block_id,
                    )
                    return {}, "unavailable"
                if current_hosted_file_key != hosted_file_key:
                    LOGGER.info(
                        "기존 본문 미디어 재사용 생략: 호스팅 파일 식별자 불일치 (%s)",
                        block_id,
                    )
                    return {}, "drift"
            if not upload_id:
                if not hosted_file_key:
                    LOGGER.info(
                        "기존 본문 미디어 재사용 생략: 과거 상태의 호스팅 파일 식별자 누락"
                    )
                    return {}, "unavailable"
                LOGGER.info(
                    "기존 본문 미디어 재사용 생략: 과거 상태의 업로드 ID 누락"
                )
                return {}, "drift"
            sanitized = sanitize_uploaded_media_block(
                matched_block,
                upload_id,
            )
            if not sanitized:
                LOGGER.info(
                    "기존 본문 미디어 재사용 생략: 생성용 블록 정리 실패 (%s)",
                    matched_block.get("type"),
                )
                return {}, "unavailable"
            key = (
                meta["type"],
                normalize_attachment_identity_url(meta["source_url"]),
            )
            reusable.setdefault(key, []).append(
                {
                    "block": copy.deepcopy(sanitized),
                    "content_sha256": normalize_content_sha256(
                        meta.get("content_sha256")
                    ),
                }
            )
        return reusable, "valid"
    state_types = [str(meta.get("type") or "").strip() for meta in media_state]
    block_types = [str(block.get("type") or "").strip() for block in hosted_blocks_in_order]
    if state_types != block_types:
        LOGGER.info(
            "기존 본문 미디어 재사용 생략: 타입 순서 불일치 (상태=%s, 블록=%s)",
            state_types,
            block_types,
        )
        return {}, "drift"
    if len(set(state_types)) != len(state_types):
        LOGGER.info("기존 본문 미디어 재사용 생략: 블록 ID 없는 동일 타입 반복 상태")
        return {}, "unavailable"
    for meta, block in zip(
        media_state,
        hosted_blocks_in_order,
        strict=True,
    ):
        upload_id = str(meta.get("upload_id") or "").strip()
        hosted_file_key = str(meta.get("hosted_file_key") or "").strip()
        if not upload_id:
            LOGGER.info("기존 본문 미디어 재사용 생략: 상태의 업로드 ID 누락")
            return {}, "unavailable"
        if upload_id in seen_upload_ids:
            LOGGER.info(
                "기존 본문 미디어 재사용 생략: 상태에서 중복 업로드 ID 감지 (%s)",
                upload_id,
            )
            return {}, "unavailable"
        seen_upload_ids.add(upload_id)
        if hosted_file_key:
            current_hosted_file_key = extract_notion_hosted_file_key_from_block(block)
            if not current_hosted_file_key:
                LOGGER.info("기존 본문 미디어 재사용 생략: 현재 블록의 호스팅 파일 식별자 누락")
                return {}, "unavailable"
            if current_hosted_file_key != hosted_file_key:
                LOGGER.info("기존 본문 미디어 재사용 생략: 호스팅 파일 식별자 불일치")
                return {}, "drift"
        sanitized = sanitize_uploaded_media_block(block, upload_id)
        if not sanitized:
            LOGGER.info(
                "기존 본문 미디어 재사용 생략: 생성용 블록 정리 실패 (%s)",
                block.get("type"),
            )
            return {}, "unavailable"
        key = (
            meta["type"],
            normalize_attachment_identity_url(meta["source_url"]),
        )
        reusable.setdefault(key, []).append(
            {
                "block": copy.deepcopy(sanitized),
                "content_sha256": normalize_content_sha256(
                    meta.get("content_sha256")
                ),
            }
        )
    return reusable, "valid"


def extract_existing_uploaded_media_blocks(
    token: str,
    page_id: str,
    media_state: list[JsonObject],
) -> dict[tuple[str, str], list[JsonObject]]:
    reusable, _ = inspect_existing_uploaded_media_blocks(
        token,
        page_id,
        media_state,
    )
    return reusable


def enrich_body_media_state_with_block_ids(
    token: str,
    page_id: str,
    media_state: list[JsonObject],
    generation_id: str = "",
) -> list[JsonObject]:
    if not page_id or not media_state:
        return media_state
    containers, container_status = body_media_container_blocks(
        token,
        page_id,
        media_state,
        generation_id,
    )
    if container_status != "valid":
        return media_state
    hosted_blocks_in_order, media_status = hosted_media_blocks_from_containers(
        token,
        containers,
    )
    if media_status != "valid":
        return media_state
    if len(hosted_blocks_in_order) != len(media_state):
        return media_state
    state_types = [str(meta.get("type") or "").strip() for meta in media_state]
    block_types = [str(block.get("type") or "").strip() for block in hosted_blocks_in_order]
    if state_types != block_types:
        return media_state
    enriched: list[JsonObject] = []
    seen_block_ids: set[str] = set()
    for meta, block in zip(
        media_state,
        hosted_blocks_in_order,
        strict=True,
    ):
        block_id = str(block.get("id") or "").strip()
        if not block_id or block_id in seen_block_ids:
            return media_state
        enriched_entry = dict(meta)
        enriched_entry["block_id"] = block_id
        hosted_file_key = extract_notion_hosted_file_key_from_block(block)
        if hosted_file_key:
            enriched_entry["hosted_file_key"] = hosted_file_key
        enriched.append(enriched_entry)
        seen_block_ids.add(block_id)
    return enriched


def update_quote_block(token: str, block_id: str, rich_text: list[JsonObject]) -> None:
    url = f"https://api.notion.com/v1/blocks/{block_id}"
    payload = {"quote": {"rich_text": rich_text, "color": "default"}}
    notion_request("PATCH", url, token, payload)


def normalize_notion_link_identity(raw_link: object) -> str:
    link = str(raw_link or "")
    if not link:
        return ""
    try:
        parsed = urlsplit(link)
    except ValueError:
        return link
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
    ):
        return link
    query = parsed.query
    if query:
        try:
            query = urlencode(
                parse_qsl(
                    query,
                    keep_blank_values=True,
                    encoding="utf-8",
                    errors="strict",
                ),
                doseq=True,
            )
        except (UnicodeError, ValueError):
            return link
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path or "/",
            query,
            parsed.fragment,
        )
    )


def rich_text_signature(
    parts: object,
    *,
    normalize_links: bool = False,
) -> list[JsonObject]:
    if not isinstance(parts, list):
        return []
    signature: list[JsonObject] = []
    for part in parts:
        if not isinstance(part, dict):
            signature.append({"type": "invalid"})
            continue
        text = part.get("text", {})
        content = part.get("plain_text")
        if not isinstance(content, str):
            content = (
                text.get("content", "")
                if isinstance(text, dict)
                else ""
            )
        link = part.get("href")
        if not isinstance(link, str) and isinstance(text, dict):
            link_value = text.get("link")
            link = (
                link_value.get("url")
                if isinstance(link_value, dict)
                else None
            )
        annotations = dict(DEFAULT_ANNOTATIONS)
        supplied_annotations = part.get("annotations")
        if isinstance(supplied_annotations, dict):
            annotations.update(
                {
                    key: supplied_annotations[key]
                    for key in DEFAULT_ANNOTATIONS
                    if key in supplied_annotations
                }
            )
        normalized_link = str(link or "")
        if normalize_links:
            normalized_link = normalize_notion_link_identity(
                normalized_link
            )
        signature.append(
            {
                "type": str(part.get("type") or "text"),
                "content": str(content or ""),
                "link": normalized_link,
                "annotations": annotations,
            }
        )
    return signature


def media_signature(
    payload: JsonObject,
    *,
    normalize_links: bool = False,
) -> JsonObject:
    media_type = str(payload.get("type") or "")
    if media_type == "external":
        external = payload.get("external", {})
        identity = (
            str(external.get("url") or "")
            if isinstance(external, dict)
            else ""
        )
        normalized_type = "external"
    elif media_type in {"file", "file_upload"}:
        identity = ""
        normalized_type = "hosted"
    else:
        identity = str(payload.get("url") or "")
        normalized_type = media_type
    return {
        "type": normalized_type,
        "identity": identity,
        "caption": rich_text_signature(
            payload.get("caption"),
            normalize_links=normalize_links,
        ),
    }


def block_content_signature(
    token: str,
    block: JsonObject,
    resolve_children: bool,
    *,
    normalize_links: bool = False,
) -> JsonObject:
    block_type = str(block.get("type") or "")
    payload = block.get(block_type, {})
    if not isinstance(payload, dict):
        return {"type": block_type, "payload": "invalid"}
    signature: JsonObject = {"type": block_type}
    if "rich_text" in payload:
        signature["rich_text"] = rich_text_signature(
            payload.get("rich_text"),
            normalize_links=normalize_links,
        )
    if "cells" in payload:
        cells = payload.get("cells")
        signature["cells"] = (
            [
                rich_text_signature(
                    cell,
                    normalize_links=normalize_links,
                )
                for cell in cells
            ]
            if isinstance(cells, list)
            else "invalid"
        )
    if block_type in DEFAULT_COLOR_BLOCK_TYPES:
        signature["color"] = str(payload.get("color") or "default")
    for key in (
        "language",
        "checked",
        "table_width",
        "has_column_header",
        "has_row_header",
    ):
        if key in payload:
            signature[key] = payload[key]
    if block_type in {"image", "file", "pdf", "video", "audio"}:
        signature["media"] = media_signature(
            payload,
            normalize_links=normalize_links,
        )
    elif block_type in {"embed", "bookmark"}:
        signature["url"] = str(payload.get("url") or "")
        signature["caption"] = rich_text_signature(
            payload.get("caption"),
            normalize_links=normalize_links,
        )
    expected_children = payload.get("children")
    actual_children: object = None
    if isinstance(expected_children, list):
        actual_children = expected_children
    elif resolve_children and block.get("has_children"):
        block_id = str(block.get("id") or "").strip()
        actual_children = (
            list_block_children(token, block_id)
            if block_id
            else "invalid"
        )
    if isinstance(actual_children, list):
        signature["children"] = [
            block_content_signature(
                token,
                child,
                resolve_children,
                normalize_links=normalize_links,
            )
            for child in actual_children
        ]
    elif actual_children is not None:
        signature["children"] = actual_children
    return signature


def sync_child_signature(
    token: str,
    blocks: list[JsonObject],
    resolve_children: bool,
    *,
    normalize_links: bool = False,
) -> list[JsonObject]:
    return [
        block_content_signature(
            token,
            block,
            resolve_children,
            normalize_links=normalize_links,
        )
        for block in blocks
    ]


def sync_container_content_hash(
    token: str,
    rich_text: list[JsonObject],
    children: list[JsonObject],
    resolve_children: bool,
) -> str:
    payload = {
        "rich_text": rich_text_signature(rich_text),
        "children": sync_child_signature(
            token,
            children,
            resolve_children,
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sync_container_body_rich_text(
    block: JsonObject,
    *,
    marker_authenticated: bool = False,
) -> Optional[list[JsonObject]]:
    quote = block.get("quote")
    if not isinstance(quote, dict):
        return None
    rich_text = quote.get("rich_text")
    if not isinstance(rich_text, list):
        return None
    if not all(isinstance(part, dict) for part in rich_text):
        return None
    if not marker_authenticated or not has_sync_marker(rich_text):
        return copy.deepcopy(rich_text)
    if not rich_text:
        return []
    first = copy.deepcopy(rich_text[0])
    text_payload = first.get("text")
    if not isinstance(text_payload, dict):
        return None
    content = text_payload.get("content")
    if not isinstance(content, str):
        content = first.get("plain_text")
    if not isinstance(content, str):
        return None
    lines = content.splitlines(keepends=True)
    header_lines = (
        3
        if extract_sync_content_hash(rich_text)
        else 2
        if has_current_sync_marker(rich_text)
        else 1
    )
    remaining = "".join(lines[header_lines:])
    body = copy.deepcopy(rich_text[1:])
    if remaining:
        first["text"]["content"] = remaining
        if "plain_text" in first:
            first["plain_text"] = remaining
        body.insert(0, first)
    return body


def is_sync_container_content_current(
    token: str,
    block: JsonObject,
    expected_hash: str = "",
) -> bool:
    quote = block.get("quote")
    if not isinstance(quote, dict):
        return False
    rich_text = quote.get("rich_text")
    if not isinstance(rich_text, list):
        return False
    stored_hash = expected_hash
    body_rich_text = sync_container_body_rich_text(block)
    block_id = str(block.get("id") or "").strip()
    if (
        not BODY_GENERATION_HASH_RE.fullmatch(stored_hash)
        or body_rich_text is None
        or not block_id
    ):
        return False
    actual_children = list_block_children(token, block_id)
    actual_hash = sync_container_content_hash(
        token,
        body_rich_text,
        actual_children,
        True,
    )
    if stored_hash == actual_hash:
        return True
    legacy_body = sync_container_body_rich_text(
        block,
        marker_authenticated=True,
    )
    return bool(
        has_sync_marker(rich_text)
        and legacy_body is not None
        and stored_hash
        == sync_container_content_hash(
            token,
            legacy_body,
            actual_children,
            True,
        )
    )


def verify_sync_container_part(
    token: str,
    block: JsonObject,
    expected_children: list[JsonObject],
    expected_rich_text: Optional[list[JsonObject]] = None,
    expected_content_hash: str = "",
) -> bool:
    block_id = str(block.get("id") or "").strip()
    if not block_id:
        return False
    quote = block.get("quote")
    if not isinstance(quote, dict):
        return False
    rich_text = quote.get("rich_text")
    if not isinstance(rich_text, list):
        return False
    stored_hash = expected_content_hash
    body_rich_text = sync_container_body_rich_text(block)
    if (
        not BODY_GENERATION_HASH_RE.fullmatch(stored_hash)
        or body_rich_text is None
    ):
        return False
    actual_children = list_block_children(token, block_id)
    expected_body = expected_rich_text or []
    expected_hash = sync_container_content_hash(
        token,
        expected_body,
        expected_children,
        False,
    )
    body_candidates = [body_rich_text]
    if has_sync_marker(rich_text):
        legacy_body = sync_container_body_rich_text(
            block,
            marker_authenticated=True,
        )
        if legacy_body is not None and legacy_body != body_rich_text:
            legacy_hash = sync_container_content_hash(
                token,
                legacy_body,
                actual_children,
                True,
            )
            if legacy_hash == stored_hash:
                body_candidates.append(legacy_body)
    expected_child_signature = sync_child_signature(
        token,
        expected_children,
        False,
        normalize_links=True,
    )
    actual_child_signature = sync_child_signature(
        token,
        actual_children,
        True,
        normalize_links=True,
    )
    return any(
        stored_hash
        in {
            expected_hash,
            sync_container_content_hash(
                token,
                candidate_body,
                actual_children,
                True,
            ),
        }
        and rich_text_signature(
            candidate_body,
            normalize_links=True,
        )
        == rich_text_signature(
            expected_body,
            normalize_links=True,
        )
        and actual_child_signature == expected_child_signature
        for candidate_body in body_candidates
    )


def sync_container_prefix_length(
    token: str,
    block: JsonObject,
    expected_rich_text: list[JsonObject],
    expected_children: list[JsonObject],
) -> Optional[int]:
    block_id = str(block.get("id") or "").strip()
    quote = block.get("quote")
    if not block_id or not isinstance(quote, dict):
        return None
    body_rich_text = sync_container_body_rich_text(block)
    if (
        body_rich_text is None
        or rich_text_signature(
            body_rich_text,
            normalize_links=True,
        )
        != rich_text_signature(
            expected_rich_text,
            normalize_links=True,
        )
    ):
        return None
    actual_children = list_block_children(token, block_id)
    actual_count = len(actual_children)
    if actual_count > len(expected_children):
        return None
    if sync_child_signature(
        token,
        actual_children,
        True,
        normalize_links=True,
    ) != sync_child_signature(
        token,
        expected_children[:actual_count],
        False,
        normalize_links=True,
    ):
        return None
    return actual_count


def find_generation_parts(
    token: str,
    page_id: str,
    generation_id: str,
) -> list[tuple[int, int, JsonObject]]:
    manifest = load_body_generation_manifest(token, page_id)
    if (
        not manifest
        or manifest.get("v") != BODY_GENERATION_MANIFEST_VERSION
        or manifest.get("g") != generation_id
    ):
        return []
    resolved = body_generation_blocks_from_manifest(
        token,
        page_id,
        manifest,
    )
    total = int(manifest.get("t") or 0)
    return [
        (part, total, block)
        for part, block in resolved
    ]


def is_body_generation_current(
    token: str,
    page_id: str,
    generation_id: str,
) -> bool:
    if not page_id or not generation_id:
        return False
    manifest = load_body_generation_manifest(token, page_id)
    if (
        manifest
        and manifest.get("v") == BODY_GENERATION_MANIFEST_VERSION
    ):
        if (
            manifest.get("g") != generation_id
            or manifest.get("s") != "committed"
            or int(manifest.get("t") or 0) != 1
            or len(manifest.get("p", [])) != 1
        ):
            return False
        matching = body_generation_blocks_from_manifest(
            token,
            page_id,
            manifest,
        )
        if len(matching) != int(manifest["t"]):
            return False
        top_level_quotes = [
            block
            for block in list_block_children(token, page_id)
            if block.get("type") == "quote"
        ]
        if (
            len(top_level_quotes) != 1
            or str(top_level_quotes[0].get("id") or "").strip()
            != str(matching[0][1].get("id") or "").strip()
        ):
            return False
        hashes = {
            int(part["n"]): str(part["h"])
            for part in manifest["p"]
        }
        return all(
            is_sync_container_content_current(
                token,
                block,
                hashes.get(part, ""),
            )
            for part, block in matching
        )
    return False


def delete_managed_containers(token: str, blocks: list[JsonObject]) -> None:
    for block in blocks:
        block_id = str(block.get("id") or "").strip()
        if not block_id:
            raise RuntimeError("관리 컨테이너 ID가 없습니다")
        delete_block(token, block_id)


def split_body_container_parts(
    blocks: list[JsonObject],
) -> tuple[list[JsonObject], list[list[JsonObject]]]:
    idx = 0
    while idx < len(blocks) and is_empty_paragraph_block(blocks[idx]):
        idx += 1
    container_rich_text: list[JsonObject] = []
    if idx < len(blocks) and blocks[idx].get("type") == "paragraph":
        container_rich_text = copy.deepcopy(
            blocks[idx].get("paragraph", {}).get("rich_text", [])
        )
        idx += 1
    remaining_blocks = copy.deepcopy(blocks[idx:])
    if is_image_only_blocks(remaining_blocks):
        remaining_blocks = [
            block
            for block in remaining_blocks
            if not is_empty_paragraph_block(block)
        ]
        if not container_rich_text:
            container_rich_text = build_space_rich_text()
    if not container_rich_text:
        container_rich_text = build_space_rich_text()
    body_chunks = chunks(remaining_blocks, 50) or [[]]
    if len(body_chunks) > 100:
        raise RuntimeError("본문 블록 세대가 100개 파트를 초과합니다")
    return container_rich_text, body_chunks


def validate_body_write_payloads(blocks: list[JsonObject]) -> None:
    if not blocks:
        return
    container_rich_text, body_chunks = split_body_container_parts(blocks)
    expected_children = [
        child
        for child_chunk in body_chunks
        for child in child_chunk
    ]
    first_chunk = body_chunks[0]
    container_payload = build_container_block(
        copy.deepcopy(container_rich_text)
    )
    if first_chunk:
        container_payload["quote"]["children"] = copy.deepcopy(
            first_chunk
        )
    encode_notion_payload({"children": [container_payload]})
    for offset in range(
        len(first_chunk),
        len(expected_children),
        50,
    ):
        encode_notion_payload(
            {
                "children": copy.deepcopy(
                    expected_children[offset : offset + 50]
                )
            }
        )


def sync_container_actual_hash(
    token: str,
    block: JsonObject,
    *,
    marker_authenticated: bool = False,
) -> str:
    block_id = str(block.get("id") or "").strip()
    body_rich_text = sync_container_body_rich_text(
        block,
        marker_authenticated=marker_authenticated,
    )
    if not block_id or body_rich_text is None:
        return ""
    return sync_container_content_hash(
        token,
        body_rich_text,
        list_block_children(token, block_id),
        True,
    )


def sync_container_actual_hash_for_expected(
    token: str,
    block: JsonObject,
    expected_hash: str,
) -> str:
    actual_hash = sync_container_actual_hash(token, block)
    if actual_hash == expected_hash:
        return actual_hash
    quote = block.get("quote")
    rich_text = quote.get("rich_text") if isinstance(quote, dict) else None
    if (
        BODY_GENERATION_HASH_RE.fullmatch(expected_hash)
        and isinstance(rich_text, list)
        and has_sync_marker(rich_text)
    ):
        legacy_hash = sync_container_actual_hash(
            token,
            block,
            marker_authenticated=True,
        )
        if legacy_hash == expected_hash:
            return legacy_hash
    return actual_hash


def write_body_generation_manifest(
    token: str,
    page_id: str,
    manifest: JsonObject,
) -> None:
    update_page(
        token,
        page_id,
        {
            SYNC_GENERATION_PROPERTY: body_generation_property_payload(
                manifest
            )
        },
    )


def sync_page_body_blocks(
    token: str,
    page_id: str,
    blocks: list[JsonObject],
    generation_id: Optional[str] = None,
    operation_id: str = "",
    manifest_out: Optional[JsonObject] = None,
    defer_manifest_commit: bool = False,
    allow_untracked_recovery: bool = False,
) -> str:
    if not blocks:
        return ""
    container_rich_text, body_chunks = split_body_container_parts(blocks)
    expected_children = [
        child
        for child_chunk in body_chunks
        for child in child_chunk
    ]
    expected_hash = sync_container_content_hash(
        token,
        container_rich_text,
        expected_children,
        False,
    )
    generation_id = normalize_body_generation_id(generation_id)
    if not generation_id:
        generation_id = expected_hash
    if not generation_id:
        raise RuntimeError("본문 세대 ID가 유효하지 않습니다")
    operation_id = (
        normalize_body_generation_id(operation_id)
        or generation_id
    )
    current_manifest = load_body_generation_manifest(token, page_id)
    candidate_refs: list[JsonObject] = []
    old_refs: list[JsonObject] = []
    legacy_untracked_recovery = bool(
        allow_untracked_recovery and current_manifest is None
    )
    resume_untracked = False
    if (
        current_manifest
        and current_manifest.get("v") == BODY_GENERATION_MANIFEST_VERSION
        and current_manifest.get("g") == generation_id
    ):
        if (
            current_manifest.get("s") == "pending"
            and current_manifest.get("op") != operation_id
        ):
            raise RuntimeError("다른 작업의 미완료 본문 세대가 존재합니다")
        old_refs = copy.deepcopy(current_manifest.get("o", []))
        if (
            int(current_manifest.get("t") or 0) == 1
            and len(current_manifest.get("p", [])) <= 1
        ):
            candidate_refs = copy.deepcopy(
                current_manifest.get("p", [])
            )
        else:
            old_refs.extend(
                {
                    "i": str(part["i"]),
                    "h": str(part["h"]),
                }
                for part in current_manifest.get("p", [])
            )
        resume_untracked = (
            resume_untracked
            or current_manifest.get("s") == "pending"
        )
    elif (
        current_manifest
        and current_manifest.get("v") == BODY_GENERATION_MANIFEST_VERSION
    ):
        old_refs.extend(
            {"i": str(part["i"]), "h": str(part["h"])}
            for part in current_manifest.get("p", [])
        )
        old_refs.extend(copy.deepcopy(current_manifest.get("o", [])))
    initial_root_blocks = list_block_children(token, page_id)
    if current_manifest and current_manifest.get("v") == 1:
        legacy_generation_id = str(current_manifest.get("g") or "")
        for block in initial_root_blocks:
            rich_text = block.get("quote", {}).get("rich_text", [])
            if (
                block.get("type") != "quote"
                or not isinstance(rich_text, list)
                or extract_sync_generation(rich_text)[0]
                != legacy_generation_id
            ):
                continue
            block_id = str(block.get("id") or "").strip()
            actual_hash = sync_container_actual_hash(
                token,
                block,
                marker_authenticated=True,
            )
            if (
                block_id
                and BODY_GENERATION_HASH_RE.fullmatch(actual_hash)
            ):
                old_refs.append({"i": block_id, "h": actual_hash})
    tracked_ids = {
        str(ref.get("i") or "").strip()
        for ref in [*candidate_refs, *old_refs]
    }
    untracked_root_blocks = [
        block
        for block in initial_root_blocks
        if (
            block.get("type") == "quote"
            and str(block.get("id") or "").strip() not in tracked_ids
        )
    ]
    if legacy_untracked_recovery and untracked_root_blocks:
        if (
            len(untracked_root_blocks) != 1
            or not verify_sync_container_part(
                token,
                untracked_root_blocks[0],
                expected_children,
                container_rich_text,
                expected_hash,
            )
        ):
            raise RuntimeError(
                "기존 미완료 본문이 현재 작업과 정확히 일치하지 않습니다"
            )
        recovered_id = str(
            untracked_root_blocks[0].get("id") or ""
        ).strip()
        candidate_refs = [
            {
                "i": recovered_id,
                "n": 1,
                "h": expected_hash,
            }
        ]
        tracked_ids.add(recovered_id)
    if any(
        block.get("type") == "quote"
        and str(block.get("id") or "").strip() not in tracked_ids
        for block in initial_root_blocks
    ):
        raise RuntimeError("관리되지 않은 최상위 인용 블록이 존재합니다")
    pending_manifest: JsonObject = {
        "v": BODY_GENERATION_MANIFEST_VERSION,
        "g": generation_id,
        "s": "pending",
        "op": operation_id,
        "t": 1,
        "p": candidate_refs,
        "o": old_refs,
    }
    write_body_generation_manifest(token, page_id, pending_manifest)

    def current_root_blocks() -> list[JsonObject]:
        return list_block_children(token, page_id)

    def append_old_ref(block: JsonObject) -> None:
        block_id = str(block.get("id") or "").strip()
        actual_hash = sync_container_actual_hash(token, block)
        if (
            not block_id
            or not BODY_GENERATION_HASH_RE.fullmatch(actual_hash)
        ):
            raise RuntimeError("기존 본문 블록을 검증할 수 없습니다")
        if all(
            str(ref.get("i") or "") != block_id
            for ref in pending_manifest["o"]
        ):
            pending_manifest["o"].append(
                {"i": block_id, "h": actual_hash}
            )

    def candidate_from_manifest() -> Optional[JsonObject]:
        entries = pending_manifest.get("p", [])
        if len(entries) > 1:
            raise RuntimeError("단일 본문 세대 후보가 중복되었습니다")
        if not entries:
            return None
        root_by_id = {
            str(block.get("id") or "").strip(): block
            for block in current_root_blocks()
            if str(block.get("id") or "").strip()
        }
        candidate_id = str(entries[0].get("i") or "").strip()
        candidate = root_by_id.get(candidate_id)
        if candidate is None:
            pending_manifest["p"] = []
            write_body_generation_manifest(
                token,
                page_id,
                pending_manifest,
            )
            return None
        if sync_container_prefix_length(
            token,
            candidate,
            container_rich_text,
            expected_children,
        ) is None:
            append_old_ref(candidate)
            pending_manifest["p"] = []
            write_body_generation_manifest(
                token,
                page_id,
                pending_manifest,
            )
            return None
        return candidate

    def untracked_candidates(
        *,
        require_complete: bool,
        include_initial: bool = False,
    ) -> list[JsonObject]:
        excluded_ids = {
            str(ref.get("i") or "").strip()
            for ref in [
                *pending_manifest.get("p", []),
                *pending_manifest.get("o", []),
            ]
        }
        candidates: list[JsonObject] = []
        for block in current_root_blocks():
            block_id = str(block.get("id") or "").strip()
            if (
                not block_id
                or block_id in excluded_ids
                or (
                    not include_initial
                    and any(
                        str(initial.get("id") or "").strip()
                        == block_id
                        for initial in initial_root_blocks
                    )
                )
                or block.get("type") != "quote"
            ):
                continue
            prefix_length = sync_container_prefix_length(
                token,
                block,
                container_rich_text,
                expected_children,
            )
            if prefix_length is None:
                continue
            if (
                require_complete
                and prefix_length != len(expected_children)
            ):
                continue
            candidates.append(block)
        return candidates

    candidate = candidate_from_manifest()
    if candidate is None and resume_untracked:
        recovered = untracked_candidates(
            require_complete=False,
        )
        if len(recovered) > 1:
            raise RuntimeError("본문 세대 후보가 중복되었습니다")
        candidate = recovered[0] if recovered else None
    if candidate is None:
        first_chunk = body_chunks[0]
        container_payload = build_container_block(
            copy.deepcopy(container_rich_text)
        )
        if first_chunk:
            container_payload["quote"]["children"] = copy.deepcopy(
                first_chunk
            )
        response: object = None
        append_error: Optional[Exception] = None
        try:
            response = append_block_children(
                token,
                page_id,
                [container_payload],
            )
        except Exception as exc:
            append_error = exc
        results = (
            response.get("results", [])
            if isinstance(response, dict)
            else []
        )
        candidate = results[0] if results else None
        if not isinstance(candidate, dict):
            recovered = untracked_candidates(require_complete=False)
            if len(recovered) > 1:
                raise RuntimeError("본문 세대 생성 후보가 중복되었습니다")
            candidate = recovered[0] if recovered else None
        if not isinstance(candidate, dict):
            if append_error is not None:
                raise append_error
            raise RuntimeError("본문 세대 생성 응답이 유효하지 않습니다")
        if sync_container_prefix_length(
            token,
            candidate,
            container_rich_text,
            expected_children,
        ) is None:
            append_old_ref(candidate)
            write_body_generation_manifest(
                token,
                page_id,
                pending_manifest,
            )
            raise RuntimeError(
                "본문 세대 검증 실패: "
                f"generation={generation_id}"
            )
    candidate_id = str(candidate.get("id") or "").strip()
    if not candidate_id:
        raise RuntimeError("본문 세대 후보 ID가 없습니다")

    def persist_candidate() -> int:
        root_by_id = {
            str(block.get("id") or "").strip(): block
            for block in current_root_blocks()
            if str(block.get("id") or "").strip()
        }
        current_candidate = root_by_id.get(candidate_id)
        if current_candidate is None:
            raise RuntimeError("본문 세대 후보가 사라졌습니다")
        prefix_length = sync_container_prefix_length(
            token,
            current_candidate,
            container_rich_text,
            expected_children,
        )
        if prefix_length is None:
            raise RuntimeError("본문 세대 자식 접두부가 변경되었습니다")
        actual_hash = sync_container_actual_hash(
            token,
            current_candidate,
        )
        if not BODY_GENERATION_HASH_RE.fullmatch(actual_hash):
            raise RuntimeError("본문 세대 후보 해시를 확인할 수 없습니다")
        pending_manifest["p"] = [
            {
                "i": candidate_id,
                "n": 1,
                "h": actual_hash,
            }
        ]
        write_body_generation_manifest(token, page_id, pending_manifest)
        return prefix_length

    prefix_length = persist_candidate()
    while prefix_length < len(expected_children):
        batch_end = min(
            len(expected_children),
            ((prefix_length // 50) + 1) * 50,
        )
        child_batch = copy.deepcopy(
            expected_children[prefix_length:batch_end]
        )
        target_length = prefix_length + len(child_batch)
        append_error = None
        try:
            append_block_children(
                token,
                candidate_id,
                child_batch,
            )
        except Exception as exc:
            append_error = exc
        updated_prefix = persist_candidate()
        if updated_prefix < target_length:
            if append_error is not None:
                raise append_error
            raise RuntimeError(
                "본문 세대 자식 배치 추가를 검증하지 못했습니다"
            )
        prefix_length = updated_prefix

    root_by_id = {
        str(block.get("id") or "").strip(): block
        for block in current_root_blocks()
        if str(block.get("id") or "").strip()
    }
    completed = root_by_id.get(candidate_id)
    if (
        completed is None
        or not verify_sync_container_part(
            token,
            completed,
            expected_children,
            container_rich_text,
            expected_hash,
        )
    ):
        raise RuntimeError("본문 세대 최종 검증 실패")
    completed_hash = sync_container_actual_hash(token, completed)
    if not BODY_GENERATION_HASH_RE.fullmatch(completed_hash):
        raise RuntimeError("본문 세대 최종 해시를 확인할 수 없습니다")
    pending_manifest["p"] = [
        {
            "i": candidate_id,
            "n": 1,
            "h": completed_hash,
        }
    ]
    write_body_generation_manifest(token, page_id, pending_manifest)
    old_ids = {
        str(old_ref.get("i") or "").strip()
        for old_ref in pending_manifest.get("o", [])
    }
    unexpected_quotes = [
        block
        for block_id, block in root_by_id.items()
        if (
            block.get("type") == "quote"
            and block_id != candidate_id
            and block_id not in old_ids
        )
    ]
    if unexpected_quotes:
        raise RuntimeError("관리되지 않은 최상위 인용 블록이 존재합니다")
    for old_ref in pending_manifest.get("o", []):
        old_id = str(old_ref.get("i") or "").strip()
        if not old_id or old_id == candidate_id or old_id not in root_by_id:
            continue
        if (
            sync_container_actual_hash_for_expected(
                token,
                root_by_id[old_id],
                str(old_ref.get("h") or ""),
            )
            != str(old_ref.get("h") or "")
        ):
            raise RuntimeError("기존 본문이 교체 직전에 변경되었습니다")
        delete_block(token, old_id)
    committed_manifest: JsonObject = {
        "v": BODY_GENERATION_MANIFEST_VERSION,
        "g": generation_id,
        "s": "committed",
        "op": operation_id,
        "t": 1,
        "p": copy.deepcopy(pending_manifest["p"]),
        "o": [],
    }
    if manifest_out is not None:
        manifest_out.clear()
        manifest_out.update(copy.deepcopy(committed_manifest))
    if not defer_manifest_commit:
        write_body_generation_manifest(
            token,
            page_id,
            committed_manifest,
        )
    return generation_id


def is_empty_body_generation_current(
    token: str,
    page_id: str,
    generation_id: str,
) -> bool:
    manifest = load_body_generation_manifest(token, page_id)
    if (
        manifest
        and manifest.get("v") == BODY_GENERATION_MANIFEST_VERSION
    ):
        if (
            manifest.get("g") != generation_id
            or manifest.get("s") != "committed"
            or int(manifest.get("t") or 0) != 1
            or not is_body_generation_current(
                token,
                page_id,
                generation_id,
            )
        ):
            return False
        blocks = body_generation_blocks_from_manifest(
            token,
            page_id,
            manifest,
        )
        if len(blocks) != 1:
            return False
        block_id = str(blocks[0][1].get("id") or "").strip()
        body_rich_text = sync_container_body_rich_text(blocks[0][1])
        return (
            bool(block_id)
            and body_rich_text is not None
            and rich_text_signature(body_rich_text)
            == rich_text_signature(build_space_rich_text())
            and not list_block_children(token, block_id)
        )
    return False


def build_properties(
    item: JsonObject,
    has_views_property: bool,
    has_attachments_property: bool,
    has_classification_property: bool,
) -> JsonObject:
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
        props[ATTACHMENT_PROPERTY] = {"files": item.get("attachments") or []}
    if has_views_property and item.get("views") is not None:
        props[VIEWS_PROPERTY] = {"number": item["views"]}
    if has_classification_property and item.get("classification"):
        props[CLASSIFICATION_PROPERTY] = {
            "select": {"name": item["classification"]}
        }
    if item.get("url"):
        props[URL_PROPERTY] = {"url": item["url"]}
    source_id = str(item.get("source_id") or "").strip()
    notice_id = str(item.get("notice_id") or "").strip()
    if not notice_id:
        notice_id = extract_detail_id_from_text(str(item.get("url") or "")) or ""
    if source_id and notice_id:
        props[SYNC_OWNER_PROPERTY] = {
            "rich_text": build_rich_text_chunks(SYNC_OWNER_VALUE)
        }
        props[SOURCE_KEY_PROPERTY] = {
            "rich_text": build_rich_text_chunks(source_id)
        }
        props[NOTICE_ID_PROPERTY] = {
            "rich_text": build_rich_text_chunks(notice_id)
        }
    generation_id = str(item.get("generation_id") or "").strip()
    if generation_id:
        props[SYNC_GENERATION_PROPERTY] = {
            "rich_text": build_rich_text_chunks(generation_id)
        }
    operation_id = str(item.get("operation_id") or "").strip()
    if operation_id:
        props[SYNC_OPERATION_PROPERTY] = {
            "rich_text": build_rich_text_chunks(operation_id)
        }
    sync_status = str(item.get("sync_status") or "").strip()
    if sync_status:
        props[SYNC_STATUS_PROPERTY] = {
            "rich_text": build_rich_text_chunks(sync_status)
        }
    return props


def rich_text_value_from_payload(value: JsonObject) -> str:
    rich_text = value.get("rich_text") or value.get("title") or []
    parts: list[str] = []
    for part in rich_text:
        plain_text = part.get("plain_text")
        if isinstance(plain_text, str):
            parts.append(plain_text)
            continue
        text = part.get("text", {})
        content = text.get("content")
        if isinstance(content, str):
            parts.append(content)
    return "".join(parts).strip()


def canonical_date_value(value: object) -> str:
    text = str(value or "").strip()
    if not text or re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return text
    parsed = parsed.replace(second=0, microsecond=0)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="minutes")


def canonical_property_value(value: JsonObject) -> object:
    property_type = value.get("type")
    if property_type in {"title", "rich_text"}:
        return rich_text_value_from_payload(value)
    if "title" in value:
        return rich_text_value_from_payload({"title": value.get("title", [])})
    if "rich_text" in value:
        return rich_text_value_from_payload(
            {"rich_text": value.get("rich_text", [])}
        )
    if "checkbox" in value:
        return bool(value.get("checkbox"))
    if "date" in value:
        date_value = value.get("date")
        return (
            canonical_date_value(date_value.get("start"))
            if isinstance(date_value, dict)
            else ""
        )
    if "select" in value:
        select = value.get("select")
        return str(select.get("name") or "") if isinstance(select, dict) else ""
    if "number" in value:
        return value.get("number")
    if "url" in value:
        return str(value.get("url") or "")
    if "files" in value:
        normalized: list[tuple[str, str, str]] = []
        for entry in value.get("files") or []:
            entry_type = str(entry.get("type") or "")
            payload = entry.get(entry_type, {}) if entry_type else {}
            identity = ""
            if isinstance(payload, dict):
                identity = str(payload.get("url") or payload.get("id") or "")
            normalized.append(
                (str(entry.get("name") or ""), entry_type, identity)
            )
        return normalized
    return value


def filter_changed_properties(
    existing_properties: JsonObject,
    desired_properties: JsonObject,
) -> JsonObject:
    return {
        name: value
        for name, value in desired_properties.items()
        if canonical_property_value(existing_properties.get(name, {}))
        != canonical_property_value(value)
    }


def extract_title(properties: JsonObject) -> str:
    title_prop = properties.get(TITLE_PROPERTY, {})
    title_parts = title_prop.get("title", [])
    text = "".join(part.get("plain_text", "") for part in title_parts).strip()
    return text


def extract_date(properties: JsonObject) -> Optional[str]:
    date_prop = properties.get(DATE_PROPERTY, {})
    date_data = date_prop.get("date")
    if not date_data:
        return None
    start = date_data.get("start")
    return start if isinstance(start, str) and start else None


def extract_rich_text_value(properties: JsonObject, property_name: str) -> str:
    prop = properties.get(property_name, {})
    rich_text = prop.get("rich_text", [])
    return "".join(part.get("plain_text", "") for part in rich_text).strip()


def is_managed_page(
    page: JsonObject,
    source_id: str = "",
    notice_id: str = "",
) -> bool:
    properties = page.get("properties", {})
    if extract_rich_text_value(properties, SYNC_OWNER_PROPERTY) != SYNC_OWNER_VALUE:
        return False
    if source_id and extract_rich_text_value(properties, SOURCE_KEY_PROPERTY) != source_id:
        return False
    if notice_id and extract_rich_text_value(properties, NOTICE_ID_PROPERTY) != notice_id:
        return False
    return True


def managed_page_fingerprint(
    page: Optional[dict[str, Any]],
) -> str:
    if not page:
        return ""
    properties = page.get("properties", {})
    fingerprint_properties = (
        TITLE_PROPERTY,
        TOP_PROPERTY,
        DATE_PROPERTY,
        AUTHOR_PROPERTY,
        URL_PROPERTY,
        TYPE_PROPERTY,
        CLASSIFICATION_PROPERTY,
        VIEWS_PROPERTY,
        SYNC_OWNER_PROPERTY,
        SOURCE_KEY_PROPERTY,
        NOTICE_ID_PROPERTY,
        SYNC_GENERATION_PROPERTY,
        SYNC_STATUS_PROPERTY,
        SYNC_OPERATION_PROPERTY,
        ATTACHMENT_STATE_PROPERTY,
        BODY_HASH_PROPERTY,
        BODY_MEDIA_STATE_PROPERTY,
    )
    payload = {
        "page_id": str(page.get("id") or "").strip(),
        "last_edited_time": str(page.get("last_edited_time") or "").strip(),
        "in_trash": bool(page.get("in_trash")),
        "properties": {
            name: canonical_property_value(properties.get(name, {}))
            for name in fingerprint_properties
        },
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def top_candidate_fingerprints(
    candidates: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    return sorted(
        (
            str(page.get("id") or "").strip(),
            managed_page_fingerprint(page),
        )
        for page in candidates
    )


# 조회 단계명을 함께 남겨서 기존 페이지 탐색이 어디에서 실패했는지 바로 구분한다.
def query_existing_pages_with_stage_log(
    token: str,
    database_id: str,
    filter_payload: JsonObject,
    stage_name: str,
    detail_url: Optional[str],
    title: str,
    date_iso: Optional[str],
) -> list[JsonObject]:
    try:
        results: list[JsonObject] = query_database(
            token,
            database_id,
            filter_payload,
        )
        return results
    except NotionRequestError as exc:
        LOGGER.error(
            "기존 페이지 조회 실패: 단계=%s, 제목=%s, 작성일=%s, URL=%s (%s)",
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
    source_id: str = "",
    notice_id: str = "",
) -> Optional[JsonObject]:
    source_id = str(source_id or "").strip()
    notice_id = str(notice_id or "").strip()
    if source_id and notice_id:
        results = query_existing_pages_with_stage_log(
            token,
            database_id,
            {
                "and": [
                    {
                        "property": SYNC_OWNER_PROPERTY,
                        "rich_text": {"equals": SYNC_OWNER_VALUE},
                    },
                    {
                        "property": SOURCE_KEY_PROPERTY,
                        "rich_text": {"equals": source_id},
                    },
                    {
                        "property": NOTICE_ID_PROPERTY,
                        "rich_text": {"equals": notice_id},
                    },
                ]
            },
            "관리 페이지 식별자 조회",
            detail_url,
            title,
            date_iso,
        )
        if len(results) == 1:
            return results[0]
        if len(results) > 1:
            raise RuntimeError(
                f"관리 페이지 식별자 충돌: 출처={source_id}, 공지 ID={notice_id}, 개수={len(results)}"
            )
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
            if is_managed_page(results[0]):
                if not is_managed_page(
                    results[0],
                    source_id,
                    notice_id,
                ):
                    raise RuntimeError("다른 출처가 소유한 URL과 충돌했습니다")
                return results[0]
            raise RuntimeError("동일 URL의 비관리 페이지가 존재합니다")
        if len(results) > 1:
            raise RuntimeError(
                f"동일 URL 페이지가 중복되었습니다: {detail_url}, 개수={len(results)}"
            )
    return None


def iter_top_pages(
    token: str,
    database_id: str,
    source_id: str,
) -> Iterator[JsonObject]:
    payload: JsonObject = {
        "filter": {
            "and": [
                {"property": TOP_PROPERTY, "checkbox": {"equals": True}},
                {
                    "property": SYNC_OWNER_PROPERTY,
                    "rich_text": {"equals": SYNC_OWNER_VALUE},
                },
                {
                    "property": SOURCE_KEY_PROPERTY,
                    "rich_text": {"equals": source_id},
                },
            ]
        },
        "page_size": 100,
    }
    seen_cursors: set[str] = set()

    while True:
        check_run_control()
        data: JsonObject = query_database_page(
            token,
            database_id,
            payload,
        )
        results: list[JsonObject] = data.get("results", [])
        for page in results:
            if is_managed_page(page, source_id):
                yield page
        if not data.get("has_more"):
            break
        next_cursor = str(data.get("next_cursor") or "").strip()
        if not next_cursor or next_cursor in seen_cursors:
            raise RuntimeError(
                "TOP 페이지 페이지네이션 커서가 누락되거나 반복되었습니다"
            )
        seen_cursors.add(next_cursor)
        payload["start_cursor"] = next_cursor


def inspect_pending_pages(
    token: str,
    database_id: str,
) -> list[JsonObject]:
    payload: JsonObject = {
        "filter": {
            "and": [
                {
                    "property": SYNC_OWNER_PROPERTY,
                    "rich_text": {"equals": SYNC_OWNER_VALUE},
                },
                {
                    "property": SYNC_STATUS_PROPERTY,
                    "rich_text": {"equals": "pending"},
                },
            ]
        },
        "page_size": 100,
    }
    pending_pages: list[JsonObject] = []
    seen_page_ids: set[str] = set()
    seen_cursors: set[str] = set()
    while True:
        check_run_control()
        data = query_database_page(token, database_id, payload)
        results: list[JsonObject] = data.get("results", [])
        for page in results:
            if not is_managed_page(page):
                continue
            properties = page.get("properties", {})
            if (
                extract_rich_text_value(
                    properties,
                    SYNC_STATUS_PROPERTY,
                )
                != "pending"
            ):
                continue
            page_id = str(page.get("id") or "").strip()
            if not page_id:
                raise RuntimeError("대기 중인 관리 페이지의 ID가 누락되었습니다")
            if page_id in seen_page_ids:
                raise RuntimeError(
                    f"대기 중인 관리 페이지가 중복 조회되었습니다: {page_id}"
                )
            seen_page_ids.add(page_id)
            pending_pages.append(page)
        if not data.get("has_more"):
            break
        next_cursor = str(data.get("next_cursor") or "").strip()
        if not next_cursor or next_cursor in seen_cursors:
            raise RuntimeError(
                "대기 페이지 페이지네이션 커서가 누락되거나 반복되었습니다"
            )
        seen_cursors.add(next_cursor)
        payload["start_cursor"] = next_cursor
    return pending_pages


def verify_top_disabled(
    token: str,
    page_id: str,
    source_id: str,
    notice_id: str,
) -> None:
    last_reasons: list[str] = ["unread"]
    for delay in TOP_COMMIT_READBACK_DELAYS:
        if delay:
            sleep_with_run_control(delay)
        check_run_control()
        page = retrieve_page(token, page_id)
        reasons: list[str] = []
        if bool(page.get("in_trash")):
            reasons.append("in_trash")
        if not is_managed_page(page, source_id, notice_id):
            reasons.append("managed_identity")
        properties = page.get("properties", {})
        top_value = properties.get(TOP_PROPERTY, {}).get("checkbox")
        if top_value is not False:
            reasons.append("top")
        if not reasons:
            return
        last_reasons = reasons
    raise DestinationConsistencyError(
        "Notion TOP 해제 재조회 검증에 실패했습니다: "
        f"출처={source_id}, 공지 ID={notice_id}, "
        f"불일치 항목={','.join(last_reasons)}"
    )


def disable_missing_top(
    token: str,
    database_id: str,
    source_id: str,
    current_notice_ids: set[str],
    eligible_notice_ids: Optional[set[str]] = None,
    planned_candidates: Optional[list[dict[str, Any]]] = None,
    total_top_count: Optional[int] = None,
) -> int:
    if planned_candidates is None:
        planned_pages, candidates = inspect_missing_top(
            token,
            database_id,
            source_id,
            current_notice_ids,
        )
        if total_top_count is None:
            total_top_count = len(planned_pages)
    else:
        candidates = list(planned_candidates)
    if eligible_notice_ids is not None:
        candidates = [
            page
            for page in candidates
            if extract_rich_text_value(
                page.get("properties", {}),
                NOTICE_ID_PROPERTY,
            )
            in eligible_notice_ids
        ]
    if total_top_count is not None:
        validate_top_disable_candidates(
            source_id,
            total_top_count,
            candidates,
        )
    expected_fingerprints = top_candidate_fingerprints(candidates)
    current_top_pages, current_candidates = inspect_missing_top(
        token,
        database_id,
        source_id,
        current_notice_ids,
    )
    if eligible_notice_ids is not None:
        current_candidates = [
            page
            for page in current_candidates
            if extract_rich_text_value(
                page.get("properties", {}),
                NOTICE_ID_PROPERTY,
            )
            in eligible_notice_ids
        ]
    if (
        total_top_count is not None
        and len(current_top_pages) != total_top_count
    ) or top_candidate_fingerprints(
        current_candidates
    ) != expected_fingerprints:
        raise RuntimeError(
            f"TOP 적용 직전 대상이 변경되었습니다: {source_id}"
        )
    if total_top_count is not None:
        validate_top_disable_candidates(
            source_id,
            len(current_top_pages),
            current_candidates,
        )
    candidates = current_candidates
    candidate_ids = [
        str(page.get("id") or "").strip()
        for page in candidates
    ]
    if any(not page_id for page_id in candidate_ids):
        raise DestinationConsistencyError(
            f"TOP 해제 대상 페이지 ID가 누락되었습니다: {source_id}"
        )
    if len(set(candidate_ids)) != len(candidate_ids):
        raise DestinationConsistencyError(
            f"TOP 해제 대상 페이지 ID가 중복되었습니다: {source_id}"
        )
    for page in candidates:
        page_id = str(page.get("id") or "").strip()
        expected_fingerprint = managed_page_fingerprint(page)
        current_page = retrieve_page(token, page_id)
        if managed_page_fingerprint(current_page) != expected_fingerprint:
            raise DestinationConsistencyError(
                f"TOP 해제 대상이 적용 직전에 변경되었습니다: "
                f"출처={source_id}, 페이지={page_id}"
            )
        props = current_page.get("properties", {})
        title = extract_title(props) or "제목없음"
        date_iso = extract_date(props)
        notice_id = extract_rich_text_value(
            props,
            NOTICE_ID_PROPERTY,
        )
        if (
            bool(current_page.get("in_trash"))
            or not is_managed_page(current_page, source_id, notice_id)
            or not notice_id
            or notice_id in current_notice_ids
            or props.get(TOP_PROPERTY, {}).get("checkbox") is not True
        ):
            raise DestinationConsistencyError(
                f"TOP 해제 대상이 적용 직전에 변경되었습니다: "
                f"출처={source_id}, 페이지={page_id}"
            )
        update_page(
            token,
            page_id,
            {TOP_PROPERTY: {"checkbox": False}},
        )
        verify_top_disabled(
            token,
            page_id,
            source_id,
            notice_id,
        )
        LOGGER.info("TOP 해제: %s (%s)", title, date_iso or "날짜없음")
    return len(candidates)


def plan_missing_top(
    token: str,
    database_id: str,
    source_id: str,
    current_notice_ids: set[str],
    enforce_safety: bool = True,
) -> list[dict[str, Any]]:
    pages, candidates = inspect_missing_top(
        token,
        database_id,
        source_id,
        current_notice_ids,
    )
    if enforce_safety:
        validate_top_disable_candidates(
            source_id,
            len(pages),
            candidates,
        )
    return candidates


def inspect_missing_top(
    token: str,
    database_id: str,
    source_id: str,
    current_notice_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pages = list(iter_top_pages(token, database_id, source_id))
    candidates: list[dict[str, Any]] = []
    for page in pages:
        props = page.get("properties", {})
        notice_id = extract_rich_text_value(props, NOTICE_ID_PROPERTY)
        if not notice_id or notice_id in current_notice_ids:
            continue
        candidates.append(page)
    return pages, candidates


def validate_top_disable_candidates(
    source_id: str,
    total_top_count: int,
    candidates: list[dict[str, Any]],
) -> None:
    max_count = get_top_disable_max_count()
    max_ratio = get_top_disable_max_ratio()
    ratio = (
        len(candidates) / total_top_count
        if total_top_count
        else 0.0
    )
    if (
        len(candidates) > max_count
        or (total_top_count >= 5 and ratio > max_ratio)
    ):
        raise RuntimeError(
            f"TOP 해제 안전 한도 초과: 출처={source_id}, "
            f"해제={len(candidates)}, 전체={total_top_count}, 비율={ratio:.3f}"
        )
