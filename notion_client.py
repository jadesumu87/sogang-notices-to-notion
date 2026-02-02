import json
import mimetypes
import os
import re
import socket
import time
import urllib.error
import urllib.request
import uuid
from io import BytesIO
from typing import Optional
from urllib.parse import urlencode

from log import LOGGER
from settings import (
    ATTACHMENT_PROPERTY,
    AUTHOR_PROPERTY,
    BODY_HASH_PROPERTY,
    CLASSIFICATION_PROPERTY,
    DATE_PROPERTY,
    FALLBACK_TYPE,
    PAGE_ICON_EMOJI,
    TITLE_PROPERTY,
    TOP_PROPERTY,
    TYPE_TAGS,
    TYPE_PROPERTY,
    URL_PROPERTY,
    VIEWS_PROPERTY,
    get_notion_api_version,
    should_upload_files_to_notion,
)
from utils import (
    build_file_block,
    build_pdf_block,
    build_site_headers,
    derive_filename_from_url,
    extract_attachment_name,
    is_embed_file_candidate,
    is_image_name_or_url,
    is_pdf_name_or_url,
    normalize_content_type,
    sanitize_filename,
)

FILE_UPLOAD_CACHE: dict[str, str] = {}
WORKSPACE_UPLOAD_LIMIT: Optional[int] = None


class NotionRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason

def download_file_bytes(url: str) -> tuple[Optional[bytes], Optional[str]]:
    req = urllib.request.Request(url, headers=build_site_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip()
            data = resp.read()
            return data, content_type or None
    except urllib.error.HTTPError as exc:
        LOGGER.info("파일 다운로드 실패: %s (HTTP %s)", url, exc.code)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, socket.timeout):
            LOGGER.info("파일 다운로드 실패: %s (timeout)", url)
        else:
            LOGGER.info("파일 다운로드 실패: %s (%s)", url, exc.reason)
    except socket.timeout:
        LOGGER.info("파일 다운로드 실패: %s (timeout)", url)
    return None, None


def compress_image_to_limit(
    payload: bytes,
    content_type: str,
    max_bytes: int,
) -> Optional[tuple[bytes, str]]:
    if max_bytes <= 0:
        return None
    try:
        from PIL import Image
    except ImportError:
        LOGGER.info("이미지 압축 스킵: Pillow 미설치")
        return None
    try:
        with Image.open(BytesIO(payload)) as image:
            image.load()
            working = image.copy()
    except Exception as exc:
        LOGGER.info("이미지 압축 실패: 열기 실패 (%s)", exc)
        return None
    if working.size[0] <= 0 or working.size[1] <= 0:
        return None
    if working.mode in {"RGBA", "LA"}:
        background = Image.new("RGB", working.size, (255, 255, 255))
        background.paste(working, mask=working.split()[-1])
        working = background
    elif working.mode != "RGB":
        working = working.convert("RGB")
    quality_steps = [85, 75, 65, 55, 45]
    scale_steps = [1.0, 0.9, 0.8, 0.7, 0.6]
    original_size = len(payload)
    width, height = working.size
    for scale in scale_steps:
        if scale < 1.0:
            resized = working.resize(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                Image.LANCZOS,
            )
        else:
            resized = working
        for quality in quality_steps:
            buffer = BytesIO()
            try:
                resized.save(buffer, format="JPEG", quality=quality, optimize=True)
            except Exception as exc:
                LOGGER.info("이미지 압축 실패: 저장 실패 (%s)", exc)
                return None
            data = buffer.getvalue()
            if len(data) <= max_bytes:
                LOGGER.info(
                    "이미지 압축 적용: %s -> %s bytes (q=%s, scale=%.2f)",
                    original_size,
                    len(data),
                    quality,
                    scale,
                )
                return data, "image/jpeg"
    LOGGER.info("이미지 압축 실패: %s bytes -> limit %s bytes", original_size, max_bytes)
    return None


def get_workspace_upload_limit(token: str) -> Optional[int]:
    global WORKSPACE_UPLOAD_LIMIT
    if WORKSPACE_UPLOAD_LIMIT is not None:
        return WORKSPACE_UPLOAD_LIMIT
    try:
        data = notion_request("GET", "https://api.notion.com/v1/users/me", token)
    except NotionRequestError as exc:
        LOGGER.info("업로드 제한 조회 실패: %s", exc)
        WORKSPACE_UPLOAD_LIMIT = None
        return None
    limit = data.get("bot", {}).get("workspace_limits", {}).get(
        "max_file_upload_size_in_bytes"
    )
    if isinstance(limit, int):
        WORKSPACE_UPLOAD_LIMIT = limit
        return limit
    WORKSPACE_UPLOAD_LIMIT = None
    return None


def encode_multipart_form_data(
    filename: str,
    content_type: str,
    payload: bytes,
    part_number: Optional[int] = None,
) -> tuple[bytes, str]:
    boundary = f"----NotionUpload{uuid.uuid4().hex}"
    lines: list[bytes] = []
    if part_number is not None:
        lines.append(
            f"--{boundary}\r\n"
            "Content-Disposition: form-data; name=\"part_number\"\r\n\r\n"
            f"{part_number}\r\n".encode("utf-8")
        )
    safe_name = re.sub(r"[^ -~]", "_", filename)
    lines.append(
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"; filename=\"{safe_name}\"\r\n"
        f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
    )
    lines.append(payload)
    lines.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(lines)
    return body, f"multipart/form-data; boundary={boundary}"

def notion_request(
    method: str,
    url: str,
    token: str,
    payload: Optional[dict] = None,
) -> dict:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    max_retries = 3
    backoff = 1.0

    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Notion-Version", get_notion_api_version())
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code in {429, 500, 502, 503, 504}
            if retryable and attempt < max_retries:
                retry_after = exc.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    sleep_s = float(retry_after)
                else:
                    sleep_s = backoff
                LOGGER.info(
                    "Notion API 재시도(%s/%s): HTTP %s",
                    attempt + 1,
                    max_retries,
                    exc.code,
                )
                time.sleep(sleep_s)
                backoff = min(backoff * 2, 8.0)
                continue
            raise NotionRequestError(
                f"Notion API error: HTTP {exc.code}: {body}",
                status_code=exc.code,
                reason=body,
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            if attempt < max_retries:
                LOGGER.info(
                    "Notion API 재시도(%s/%s): timeout",
                    attempt + 1,
                    max_retries,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue
            raise NotionRequestError(
                "Notion API error: timeout",
                reason="timeout",
            ) from exc
        except urllib.error.URLError as exc:
            is_timeout = isinstance(exc.reason, socket.timeout)
            if attempt < max_retries:
                LOGGER.info(
                    "Notion API 재시도(%s/%s): %s",
                    attempt + 1,
                    max_retries,
                    "timeout" if is_timeout else exc.reason,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue
            if is_timeout:
                raise NotionRequestError(
                    "Notion API error: timeout",
                    reason="timeout",
                ) from exc
            raise NotionRequestError(
                f"Notion API error: {exc.reason}",
                reason=str(exc.reason),
            ) from exc


def create_file_upload(
    token: str,
    filename: str,
    content_type: str,
    mode: str = "single_part",
) -> Optional[dict]:
    payload = {"mode": mode, "filename": filename, "content_type": content_type}
    try:
        return notion_request("POST", "https://api.notion.com/v1/file_uploads", token, payload)
    except NotionRequestError as exc:
        LOGGER.info("파일 업로드 생성 실패: %s (%s)", filename, exc)
        return None


def send_file_upload(
    token: str,
    upload_url: str,
    filename: str,
    content_type: str,
    payload: bytes,
    part_number: Optional[int] = None,
) -> Optional[dict]:
    body, content_header = encode_multipart_form_data(
        filename, content_type, payload, part_number=part_number
    )
    req = urllib.request.Request(upload_url, data=body, method="POST")
    req.add_header("Content-Type", content_header)
    req.add_header("Content-Length", str(len(body)))
    if "api.notion.com" in upload_url:
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Notion-Version", get_notion_api_version())
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        LOGGER.info("파일 업로드 전송 실패: HTTP %s (%s)", exc.code, body_text)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, socket.timeout):
            LOGGER.info("파일 업로드 전송 실패: timeout")
        else:
            LOGGER.info("파일 업로드 전송 실패: %s", exc.reason)
    except socket.timeout:
        LOGGER.info("파일 업로드 전송 실패: timeout")
    return None


def upload_external_file_to_notion(
    token: str,
    url: str,
    filename_hint: Optional[str] = None,
    expect_image: bool = True,
) -> Optional[str]:
    if not url:
        return None
    cached = FILE_UPLOAD_CACHE.get(url)
    if cached:
        return cached

    payload, content_type = download_file_bytes(url)
    if not payload:
        return None
    filename = sanitize_filename(
        filename_hint or derive_filename_from_url(url, fallback="file")
    )
    content_type = normalize_content_type(content_type, filename, url)
    if expect_image and not content_type.lower().startswith("image/"):
        LOGGER.info("이미지 업로드 스킵: content_type=%s (%s)", content_type, url)
        return None
    file_size = len(payload)
    max_bytes = get_workspace_upload_limit(token)
    if max_bytes and file_size > max_bytes and expect_image:
        compressed = compress_image_to_limit(payload, content_type, max_bytes)
        if compressed:
            payload, content_type = compressed
            file_size = len(payload)
    if max_bytes and file_size > max_bytes:
        LOGGER.info("업로드 용량 초과: %s bytes (limit=%s)", file_size, max_bytes)
        return None
    if file_size > 20 * 1024 * 1024:
        LOGGER.info("업로드 스킵(멀티파트 필요): %s bytes", file_size)
        return None

    if not filename:
        filename = sanitize_filename(derive_filename_from_url(url, fallback="file"))
    if "." not in filename:
        ext = mimetypes.guess_extension(content_type) or ""
        if ext:
            filename = f"{filename}{ext}"
    if content_type.lower() == "image/jpeg":
        stem, ext = os.path.splitext(filename)
        if ext.lower() not in {".jpg", ".jpeg"}:
            filename = f"{stem}.jpg"

    created = create_file_upload(token, filename, content_type)
    if not created:
        return None
    upload_id = created.get("id")
    upload_url = created.get("upload_url")
    if isinstance(upload_url, str):
        upload_url = upload_url.strip("`")
    upload_url = upload_url or (
        f"https://api.notion.com/v1/file_uploads/{upload_id}/send"
        if upload_id
        else None
    )
    if not upload_id or not upload_url:
        LOGGER.info("파일 업로드 응답 누락: id=%s url=%s", upload_id, upload_url)
        return None
    sent = send_file_upload(
        token, upload_url, filename, content_type, payload, part_number=None
    )
    if not sent or sent.get("status") != "uploaded":
        LOGGER.info(
            "파일 업로드 상태 이상: %s (%s)", url, sent.get("status") if sent else "no_response"
        )
        return None
    FILE_UPLOAD_CACHE[url] = upload_id
    return upload_id


def prepare_attachments_for_sync(token: str, attachments: list[dict]) -> list[dict]:
    if not attachments or not should_upload_files_to_notion():
        return attachments
    updated: list[dict] = []
    for attachment in attachments:
        if attachment.get("type") != "external":
            updated.append(attachment)
            continue
        url = attachment.get("external", {}).get("url") or ""
        name = attachment.get("name") or extract_attachment_name(attachment)
        if not is_image_name_or_url(name, url):
            updated.append(attachment)
            continue
        upload_id = upload_external_file_to_notion(token, url, name, expect_image=True)
        if upload_id:
            updated.append(
                {"name": name, "type": "file_upload", "file_upload": {"id": upload_id}}
            )
        else:
            updated.append(attachment)
    return updated


def prepare_body_blocks_for_sync(token: str, blocks: list[dict]) -> list[dict]:
    if not blocks or not should_upload_files_to_notion():
        return blocks
    updated: list[dict] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type == "image":
            image = block.get("image", {})
            if image.get("type") != "external":
                updated.append(block)
                continue
            url = image.get("external", {}).get("url") or ""
            if not url:
                updated.append(block)
                continue
            filename = derive_filename_from_url(url, fallback="image")
            upload_id = upload_external_file_to_notion(
                token, url, filename, expect_image=True
            )
            if not upload_id:
                updated.append(block)
                continue
            new_block = {
                "object": "block",
                "type": "image",
                "image": {"type": "file_upload", "file_upload": {"id": upload_id}},
            }
            if image.get("caption"):
                new_block["image"]["caption"] = image["caption"]
            updated.append(new_block)
            continue
        if block_type == "embed":
            embed = block.get("embed", {})
            url = embed.get("url") or ""
            if not url or not is_embed_file_candidate(url):
                updated.append(block)
                continue
            filename = derive_filename_from_url(url, fallback="file")
            upload_id = upload_external_file_to_notion(
                token, url, filename, expect_image=False
            )
            if not upload_id:
                updated.append(block)
                continue
            if is_pdf_name_or_url(filename, url):
                updated.append(build_pdf_block(upload_id))
            else:
                updated.append(build_file_block(upload_id))
            continue
        updated.append(block)
    return updated
def fetch_database(token: str, database_id: str) -> dict:
    url = f"https://api.notion.com/v1/databases/{database_id}"
    return notion_request("GET", url, token)


def update_database(token: str, database_id: str, properties: dict) -> dict:
    url = f"https://api.notion.com/v1/databases/{database_id}"
    payload = {"properties": properties}
    return notion_request("PATCH", url, token, payload)


def ensure_title_property(token: str, database_id: str, database: dict) -> dict:
    properties = database.get("properties", {})
    if TITLE_PROPERTY in properties:
        prop = properties.get(TITLE_PROPERTY) or {}
        if prop.get("type") != "title":
            raise RuntimeError(
                f"Notion 속성 타입 불일치: {TITLE_PROPERTY} (title 아님)"
            )
        return database
    title_name = None
    for name, prop in properties.items():
        if prop.get("type") == "title":
            title_name = name
            break
    if not title_name:
        raise RuntimeError("Notion title 속성을 찾을 수 없습니다")
    LOGGER.info("Notion 속성 이름 변경: %s -> %s", title_name, TITLE_PROPERTY)
    return update_database(token, database_id, {title_name: {"name": TITLE_PROPERTY}})


def ensure_top_property(token: str, database_id: str, database: dict) -> dict:
    prop = database.get("properties", {}).get(TOP_PROPERTY)
    if prop:
        if prop.get("type") != "checkbox":
            raise RuntimeError(
                f"Notion 속성 타입 불일치: {TOP_PROPERTY} (checkbox 아님)"
            )
        return database
    LOGGER.info("Notion 속성 추가: %s", TOP_PROPERTY)
    return update_database(token, database_id, {TOP_PROPERTY: {"checkbox": {}}})


def ensure_date_property(token: str, database_id: str, database: dict) -> dict:
    prop = database.get("properties", {}).get(DATE_PROPERTY)
    if prop:
        if prop.get("type") != "date":
            raise RuntimeError(
                f"Notion 속성 타입 불일치: {DATE_PROPERTY} (date 아님)"
            )
        return database
    LOGGER.info("Notion 속성 추가: %s", DATE_PROPERTY)
    return update_database(token, database_id, {DATE_PROPERTY: {"date": {}}})


def ensure_author_property(token: str, database_id: str, database: dict) -> dict:
    prop = database.get("properties", {}).get(AUTHOR_PROPERTY)
    if prop:
        if prop.get("type") != "select":
            raise RuntimeError(
                f"Notion 속성 타입 불일치: {AUTHOR_PROPERTY} (select 아님)"
            )
        return database
    LOGGER.info("Notion 속성 추가: %s", AUTHOR_PROPERTY)
    return update_database(
        token, database_id, {AUTHOR_PROPERTY: {"select": {"options": []}}}
    )


def ensure_classification_property(token: str, database_id: str, database: dict) -> dict:
    prop = database.get("properties", {}).get(CLASSIFICATION_PROPERTY)
    if prop:
        if prop.get("type") != "select":
            raise RuntimeError(
                f"Notion 속성 타입 불일치: {CLASSIFICATION_PROPERTY} (select 아님)"
            )
        return database
    LOGGER.info("Notion 속성 추가: %s", CLASSIFICATION_PROPERTY)
    return update_database(
        token, database_id, {CLASSIFICATION_PROPERTY: {"select": {"options": []}}}
    )


def ensure_views_property(token: str, database_id: str, database: dict) -> dict:
    prop = database.get("properties", {}).get(VIEWS_PROPERTY)
    if prop:
        if prop.get("type") != "number":
            raise RuntimeError(
                f"Notion 속성 타입 불일치: {VIEWS_PROPERTY} (number 아님)"
            )
        return database
    LOGGER.info("Notion 속성 추가: %s", VIEWS_PROPERTY)
    return update_database(token, database_id, {VIEWS_PROPERTY: {"number": {}}})


def ensure_url_property(token: str, database_id: str, database: dict) -> dict:
    prop = database.get("properties", {}).get(URL_PROPERTY)
    if prop:
        if prop.get("type") != "url":
            raise RuntimeError(f"Notion 속성 타입 불일치: {URL_PROPERTY} (url 아님)")
        return database
    LOGGER.info("Notion 속성 추가: %s", URL_PROPERTY)
    return update_database(token, database_id, {URL_PROPERTY: {"url": {}}})


def ensure_type_property(token: str, database_id: str, database: dict) -> dict:
    prop = database.get("properties", {}).get(TYPE_PROPERTY)
    if prop:
        if prop.get("type") != "select":
            raise RuntimeError(f"Notion 속성 타입 불일치: {TYPE_PROPERTY} (select 아님)")
        return database
    LOGGER.info("Notion 속성 추가: %s", TYPE_PROPERTY)
    options = [{"name": name} for name in (*TYPE_TAGS, FALLBACK_TYPE)]
    return update_database(token, database_id, {TYPE_PROPERTY: {"select": {"options": options}}})


def ensure_attachment_property(token: str, database_id: str, database: dict) -> dict:
    prop = database.get("properties", {}).get(ATTACHMENT_PROPERTY)
    if prop:
        if prop.get("type") != "files":
            raise RuntimeError(
                f"Notion 속성 타입 불일치: {ATTACHMENT_PROPERTY} (files 아님)"
            )
        return database
    LOGGER.info("Notion 속성 추가: %s", ATTACHMENT_PROPERTY)
    return update_database(token, database_id, {ATTACHMENT_PROPERTY: {"files": {}}})


def ensure_body_hash_property(token: str, database_id: str, database: dict) -> dict:
    prop = database.get("properties", {}).get(BODY_HASH_PROPERTY)
    if prop:
        if prop.get("type") != "rich_text":
            raise RuntimeError(
                f"Notion 속성 타입 불일치: {BODY_HASH_PROPERTY} (rich_text 아님)"
            )
        return database
    LOGGER.info("Notion 속성 추가: %s", BODY_HASH_PROPERTY)
    return update_database(token, database_id, {BODY_HASH_PROPERTY: {"rich_text": {}}})


def ensure_required_properties(token: str, database_id: str, database: dict) -> dict:
    database = ensure_title_property(token, database_id, database)
    database = ensure_top_property(token, database_id, database)
    database = ensure_date_property(token, database_id, database)
    database = ensure_author_property(token, database_id, database)
    database = ensure_url_property(token, database_id, database)
    database = ensure_type_property(token, database_id, database)
    return database
def validate_optional_property_type(
    database: dict,
    property_name: str,
    expected_type: str,
) -> bool:
    prop = database.get("properties", {}).get(property_name)
    if not prop:
        return False
    actual = prop.get("type")
    if actual != expected_type:
        LOGGER.info(
            "Notion 속성 타입 불일치: %s (기대 %s, 실제 %s) -> 업데이트 생략",
            property_name,
            expected_type,
            actual,
        )
        return False
    return True
def get_select_options(database: dict, property_name: str) -> list[dict]:
    prop = database.get("properties", {}).get(property_name)
    if not prop:
        raise RuntimeError(f"Notion 속성 누락: {property_name}")
    if prop.get("type") != "select":
        raise RuntimeError(f"Notion 속성 타입 오류: {property_name} (select 아님)")
    return prop.get("select", {}).get("options", [])


def sanitize_select_options(options: list[dict]) -> list[dict]:
    sanitized: list[dict] = []
    for option in options:
        name = option.get("name")
        if not name:
            continue
        item = {"name": name}
        if option.get("id"):
            item["id"] = option["id"]
        color = option.get("color")
        if color:
            item["color"] = color
        sanitized.append(item)
    return sanitized


def ensure_select_option(
    token: str,
    database_id: str,
    property_name: str,
    option_name: str,
    options_cache: list[dict],
) -> list[dict]:
    if not option_name:
        return options_cache
    sanitized_options = sanitize_select_options(options_cache)
    existing = {opt.get("name") for opt in sanitized_options}
    if option_name in existing:
        return options_cache
    updated_options = sanitized_options + [{"name": option_name}]
    LOGGER.info("Notion 옵션 추가: %s=%s", property_name, option_name)
    data = update_database(
        token,
        database_id,
        {property_name: {"select": {"options": updated_options}}},
    )
    return get_select_options(data, property_name)


def ensure_select_options_batch(
    token: str,
    database_id: str,
    property_name: str,
    options_cache: list[dict],
    desired_names: set[str],
) -> list[dict]:
    sanitized_options = sanitize_select_options(options_cache)
    existing = {opt.get("name") for opt in sanitized_options}
    missing = sorted(name for name in desired_names if name and name not in existing)
    if not missing:
        return options_cache
    updated_options = sanitized_options + [{"name": name} for name in missing]
    LOGGER.info("Notion 옵션 일괄 추가: %s=%s", property_name, ", ".join(missing))
    data = update_database(
        token,
        database_id,
        {property_name: {"select": {"options": updated_options}}},
    )
    return get_select_options(data, property_name)


def query_database(token: str, database_id: str, filter_payload: dict) -> list[dict]:
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    payload = {"filter": filter_payload}
    data = notion_request("POST", url, token, payload)
    return data.get("results", [])


def append_block_children(token: str, block_id: str, children: list[dict]) -> dict:
    url = f"https://api.notion.com/v1/blocks/{block_id}/children"
    payload = {"children": children}
    return notion_request("PATCH", url, token, payload)


def list_block_children(token: str, block_id: str) -> list[dict]:
    base_url = f"https://api.notion.com/v1/blocks/{block_id}/children"
    results: list[dict] = []
    cursor: Optional[str] = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        url = f"{base_url}?{urlencode(params)}"
        data = notion_request("GET", url, token)
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


def delete_block(token: str, block_id: str) -> None:
    url = f"https://api.notion.com/v1/blocks/{block_id}"
    fallback_statuses = {403, 405, 409, 429, 500, 502, 503, 504}
    try:
        notion_request("DELETE", url, token)
    except NotionRequestError as exc:
        if exc.status_code == 404:
            LOGGER.info("블록 이미 삭제됨: %s", block_id)
            return
        if exc.status_code in fallback_statuses:
            LOGGER.info(
                "블록 DELETE 실패 -> archived 폴백: %s (HTTP %s)",
                block_id,
                exc.status_code,
            )
            notion_request("PATCH", url, token, {"archived": True})
            return
        raise
def archive_page(token: str, page_id: str) -> None:
    notion_request("PATCH", f"https://api.notion.com/v1/pages/{page_id}", token, {"archived": True})

def build_icon() -> dict:
    return {"type": "emoji", "emoji": PAGE_ICON_EMOJI}


def create_page(token: str, database_id: str, properties: dict) -> str:
    payload = {
        "parent": {"database_id": database_id},
        "properties": properties,
        "icon": build_icon(),
    }
    data = notion_request("POST", "https://api.notion.com/v1/pages", token, payload)
    return data.get("id")


def update_page(token: str, page_id: str, properties: dict) -> None:
    payload = {"properties": properties, "icon": build_icon()}
    notion_request("PATCH", f"https://api.notion.com/v1/pages/{page_id}", token, payload)
