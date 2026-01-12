import hashlib
import json
import logging
import mimetypes
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
import importlib.util
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.parse import (
    urlencode,
    urlparse,
    parse_qs,
    urlunparse,
    urljoin,
    urlsplit,
    urlunsplit,
    quote,
    unquote,
)

DEFAULT_NOTION_API_VERSION = "2022-06-28"
BASE_URL = "https://www.sogang.ac.kr/ko/scholarship-notice"
ACADEMIC_BASE_URL = "https://www.sogang.ac.kr/ko/academic-support/notices"
DEFAULT_QUERY = {"introPkId": "All", "option": "TITLE"}
USER_AGENT = "Mozilla/5.0 (compatible; ScholarshipCrawler/1.0)"
PAGE_ICON_EMOJI = "📢"
TITLE_PROPERTY = "공지사항"
AUTHOR_PROPERTY = "작성자"
DATE_PROPERTY = "작성일"
TOP_PROPERTY = "TOP"
URL_PROPERTY = "URL"
VIEWS_PROPERTY = "조회수"
ATTACHMENT_PROPERTY = "첨부파일"
TYPE_PROPERTY = "유형"
CLASSIFICATION_PROPERTY = "분류"
BODY_HASH_PROPERTY = "본문 해시"
BODY_HASH_IMAGE_MODE_UPLOAD = "upload-files-v1"
SYNC_CONTAINER_MARKER = "[SYNC_CONTAINER]"
LOGGER = logging.getLogger("scholarship-crawler")
BASE_SITE = "https://www.sogang.ac.kr"
BBS_API_BASE = f"{BASE_SITE}/api/api/v1/mainKo/BbsData"
BBS_LIST_API_URL = f"{BBS_API_BASE}/boardListMultiConfigId"
DATE_PATTERN = re.compile(
    r"\d{4}[.\-]\d{2}[.\-]\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?"
)
DATE_TIME_PATTERN = re.compile(r"\d{4}[.\-]\d{2}[.\-]\d{2}\s+\d{2}:\d{2}(?::\d{2})?")
DATE_TIME_JS_PATTERN = r"\d{4}[.\-]\d{2}[.\-]\d{2}\s+\d{2}:\d{2}(?::\d{2})?"
DETAIL_PATH_PATTERN = re.compile(r"/detail/\d+")
DETAIL_ID_CAPTURE_PATTERN = re.compile(r"/detail/(\d+)")
DETAIL_ID_FUNCTION_PATTERN = re.compile(
    r"(?:view|detail|article)\s*\(\s*'?(\d{5,})'?",
    re.IGNORECASE,
)
DETAIL_ID_PARAM_PATTERN = re.compile(
    r"(?:detailId|detail_id|articleId|article_id|boardNo|board_no|contentId|content_id)\D{0,5}(\d{5,})",
    re.IGNORECASE,
)
DETAIL_ID_DATA_ATTR_PATTERN = re.compile(
    r"data-(?:id|no|board-id|board-no|article-id|article-no|detail-id|detail-no)=['\"](\d{5,})['\"]",
    re.IGNORECASE,
)
LIST_ROW_SELECTOR = "tr[data-v-6debbb14], table tbody tr"
ATTACHMENT_EXT_PATTERN = re.compile(
    r"\.(pdf|hwp|hwpx|docx?|xlsx?|pptx?|zip|rar|7z|txt|csv|jpg|jpeg|png|gif|bmp)(?:$|\\?)",
    re.IGNORECASE,
)
IMAGE_EXT_PATTERN = re.compile(
    r"\.(jpg|jpeg|png|gif|bmp|webp|svg)(?:$|\\?)",
    re.IGNORECASE,
)
CONTENT_TYPE_OVERRIDES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".hwp": "application/vnd.hancom.hwp",
    ".hwpx": "application/vnd.hancom.hwpx",
    ".zip": "application/zip",
    ".rar": "application/vnd.rar",
    ".7z": "application/x-7z-compressed",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}
ATTACHMENT_HINTS = (
    "download",
    "filedown",
    "filedownload",
    "fileid",
    "fileno",
    "bbsfile",
    "attach",
    "file-fe-prd/board",
    "sg=",
)
ATTACHMENT_LINK_PATTERN = re.compile(
    r"(file-fe-prd/board|filedown|filedownload|bbsfile|download)",
    re.IGNORECASE,
)
ATTACHMENT_QUERY_KEYS = {
    "sg",
    "fileid",
    "file_id",
    "fileno",
    "file_no",
    "fileseq",
    "file_seq",
    "attachid",
    "attach_id",
    "attachno",
    "attach_no",
}
BODY_CONTAINER_PATTERN = re.compile(r"\b(tiptap|custom-css-tag-a)\b", re.IGNORECASE)
TYPE_TAGS = (
    "교내/국가",
    "교외",
    "국가근로",
    "학자금대출",
    "대청교",
    "발전기금",
    "동문회",
    "주거지원",
)
FALLBACK_TYPE = "공통"
DEFAULT_CONFIG_CLASSIFICATIONS = {"141": "장학공지", "2": "학사공지"}
DEFAULT_CONFIG_LIST_URLS = {"141": BASE_URL, "2": ACADEMIC_BASE_URL}
DEFAULT_BBS_CONFIG_FKS = ["141", "2"]

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


def load_dotenv(path: str = ".env") -> None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if not key:
                    continue
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
                    value = value[1:-1]
                os.environ.setdefault(key, value)
    except FileNotFoundError:
        return


def get_notion_api_version() -> str:
    return os.environ.get("NOTION_API_VERSION", DEFAULT_NOTION_API_VERSION)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def clean_text(html_text: str) -> str:
    text = re.sub(r"<[^>]+>", "", html_text)
    text = unescape(text).replace("\u00a0", " ")
    return text.strip()


def normalize_title_key(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_datetime(date_text: str) -> Optional[str]:
    match = re.search(r"(\d{4})[.\-](\d{2})[.\-](\d{2})", date_text)
    if not match:
        return None
    year, month, day = match.groups()
    time_match = re.search(r"(\d{2}):(\d{2})(?::(\d{2}))?", date_text)
    if time_match:
        hour, minute, second = time_match.groups()
        if not second:
            second = "00"
        return f"{year}-{month}-{day}T{hour}:{minute}:{second}+09:00"
    return f"{year}-{month}-{day}T00:00:00+09:00"


def parse_compact_datetime(date_text: Optional[str]) -> Optional[str]:
    if not date_text:
        return None
    digits = re.sub(r"[^0-9]", "", str(date_text))
    if len(digits) >= 14:
        year, month, day = digits[0:4], digits[4:6], digits[6:8]
        hour, minute, second = digits[8:10], digits[10:12], digits[12:14]
        return f"{year}-{month}-{day}T{hour}:{minute}:{second}+09:00"
    if len(digits) >= 8:
        year, month, day = digits[0:4], digits[4:6], digits[6:8]
        return f"{year}-{month}-{day}T00:00:00+09:00"
    return parse_datetime(str(date_text))


def normalize_date_key(date_text: Optional[str]) -> str:
    if not date_text:
        return ""
    match = re.search(r"\d{4}-\d{2}-\d{2}", date_text)
    if match:
        return match.group(0)
    return date_text[:10]


def compute_body_hash(blocks: list[dict], image_mode: str = "") -> str:
    payload_value: object
    if image_mode:
        payload_value = {"image_mode": image_mode, "blocks": blocks}
    else:
        payload_value = blocks
    payload = json.dumps(
        payload_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def has_image_blocks(blocks: list[dict]) -> bool:
    if not blocks:
        return False
    for block in blocks:
        if block.get("type") == "image":
            return True
    return False


def normalize_body_blocks_for_hash(
    blocks: list[dict], upload_files: bool
) -> list[dict]:
    if not blocks:
        return []
    normalized: list[dict] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type == "embed":
            embed = block.get("embed", {})
            url = embed.get("url") or ""
            if upload_files and is_embed_file_candidate(url):
                filename = derive_filename_from_url(url, fallback="file")
                marker_type = "pdf" if is_pdf_name_or_url(filename, url) else "file"
                normalized.append({ "type": marker_type, marker_type: {"source_url": url}})
            else:
                normalized.append(block)
            continue
        normalized.append(block)
    return normalized


def normalize_detail_url(raw_url: Optional[str]) -> Optional[str]:
    if not raw_url:
        return None
    raw_url = raw_url.strip()
    lowered = raw_url.lower()
    if lowered in {"#", "#/", "javascript:void(0)", "javascript:void(0);"}:
        return None
    if lowered.startswith(("javascript:", "mailto:", "tel:", "data:")):
        return None
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    parsed = urlparse(raw_url)
    if parsed.scheme in {"javascript", "mailto", "tel", "data"}:
        return None
    if not parsed.scheme or not parsed.netloc:
        if raw_url.startswith("/"):
            base = urlparse(BASE_URL)
            parsed = urlparse(f"{base.scheme}://{base.netloc}{raw_url}")
        else:
            return None
    query = parse_qs(parsed.query)
    drop_keys = {"introPkId", "option", "page"}
    query_items: list[tuple[str, str]] = []
    for key in sorted(query):
        if key in drop_keys:
            continue
        for value in query[key]:
            query_items.append((key, value))
    new_query = urlencode(query_items, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", new_query, ""))


def normalize_file_url(raw_url: Optional[str]) -> Optional[str]:
    if not raw_url:
        return None
    raw_url = raw_url.strip()
    lowered = raw_url.lower()
    if lowered in {"#", "#/", "javascript:void(0)", "javascript:void(0);"}:
        return None
    if lowered.startswith(("javascript:", "mailto:", "tel:", "data:")):
        return None
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    absolute = urljoin(BASE_SITE, raw_url)
    parsed = urlsplit(absolute)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        return None
    if parsed.scheme in {"javascript", "mailto", "tel", "data"}:
        return None
    encoded = encode_url(absolute)
    encoded_parts = urlsplit(encoded)
    return urlunsplit(
        (encoded_parts.scheme, encoded_parts.netloc, encoded_parts.path, encoded_parts.query, "")
    )


# Attachment policy:
# - ATTACHMENT_ALLOWED_DOMAINS: comma-separated allowed hosts (default: sogang.ac.kr)
# - ATTACHMENT_MAX_COUNT: per-page cap for attachments (default: 15)
# - ATTACHMENT_SELFTEST: run attachment policy selftest and exit (1/true/on)
def get_attachment_allowed_domains() -> tuple[str, ...]:
    raw = os.environ.get("ATTACHMENT_ALLOWED_DOMAINS", "sogang.ac.kr")
    domains = [part.strip().lower() for part in raw.split(",") if part.strip()]
    return tuple(domains)


def get_attachment_max_count() -> int:
    raw = os.environ.get("ATTACHMENT_MAX_COUNT", "15").strip()
    try:
        value = int(raw)
    except ValueError:
        return 15
    return max(1, value)


def has_attachment_query_key(url: str) -> bool:
    params = parse_qs(urlparse(url).query)
    for key in params.keys():
        if key.lower() in ATTACHMENT_QUERY_KEYS:
            return True
    return False


def is_allowed_attachment_host(host: str, allowed_domains: tuple[str, ...]) -> bool:
    if not host:
        return False
    host = host.split(":", 1)[0]
    for domain in allowed_domains:
        if host == domain or host.endswith(f".{domain}"):
            return True
    return False


def is_attachment_candidate(
    url: str,
    text: str,
    allow_domain_only: bool = False,
) -> tuple[bool, bool]:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    allowed_domains = get_attachment_allowed_domains()
    lowered_url = url.lower()
    ext_match = bool(
        ATTACHMENT_EXT_PATTERN.search(url) or ATTACHMENT_EXT_PATTERN.search(text)
    )
    hint_match = any(hint in lowered_url for hint in ATTACHMENT_HINTS)
    link_match = bool(ATTACHMENT_LINK_PATTERN.search(url))
    path_match = "/file-fe-prd/board/" in parsed.path
    strong_match = ext_match or hint_match or link_match or path_match
    text_hint = "첨부" in text or "다운로드" in text
    query_hint = has_attachment_query_key(url)
    minimal_signal = strong_match or text_hint or query_hint
    allowed_host = is_allowed_attachment_host(host, allowed_domains)

    if not allowed_host:
        return False, False

    if allow_domain_only:
        if not minimal_signal:
            return False, False
        return True, not strong_match

    if strong_match:
        return True, False
    return False, False


def run_attachment_policy_selftest() -> None:
    LOGGER.info("첨부파일 정책 셀프테스트 시작")
    keys = ("ATTACHMENT_ALLOWED_DOMAINS",)
    original_env = {key: os.environ.get(key) for key in keys}
    os.environ["ATTACHMENT_ALLOWED_DOMAINS"] = "sogang.ac.kr"
    try:
        html = (
            '<div>첨부파일</div>'
            '<a href="https://example.com/file.pdf">file.pdf</a>'
        )
        html_attachments = extract_attachments_from_detail(html)
        api_attachments = extract_attachments_from_api_data(
            {"fileValue1": "https://example.com/file.pdf"}
        )
        page_candidates = [("https://example.com/file.pdf", "file.pdf")]
        page_attachments = []
        for href, text in page_candidates:
            url = normalize_file_url(href)
            if not url:
                continue
            allowed, _ = is_attachment_candidate(
                url, text, allow_domain_only=True
            )
            if allowed:
                page_attachments.append(url)
        strict_allowed, _ = is_attachment_candidate(
            "https://example.com/file.pdf",
            "file.pdf",
            allow_domain_only=True,
        )
        if html_attachments or api_attachments or strict_allowed or page_attachments:
            LOGGER.info(
                "첨부파일 정책 셀프테스트 실패: html=%s, api=%s, strict_allowed=%s, page=%s",
                len(html_attachments),
                len(api_attachments),
                int(strict_allowed),
                len(page_attachments),
            )
            raise RuntimeError("첨부파일 정책 셀프테스트 실패")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            LOGGER.info("Playwright 미설치: 셀프테스트(Playwright) 스킵")
        else:
            pw_attachments: list[dict] = []
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=True)
                except Exception as exc:
                    LOGGER.info(
                        "Playwright 브라우저 실행 실패: %s (셀프테스트 스킵)",
                        exc,
                    )
                    browser = None
                if browser:
                    try:
                        page = browser.new_page()
                        page.set_content(html, wait_until="domcontentloaded")
                        pw_attachments = extract_attachments_from_page(page)
                    finally:
                        browser.close()
            if pw_attachments:
                LOGGER.info(
                    "첨부파일 정책 셀프테스트 실패(Playwright): %s개",
                    len(pw_attachments),
                )
                raise RuntimeError("첨부파일 정책 셀프테스트 실패(Playwright)")
        LOGGER.info("첨부파일 정책 셀프테스트 통과")
    finally:
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def should_run_attachment_selftest() -> bool:
    raw = os.environ.get("ATTACHMENT_SELFTEST", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


# Notion file upload:
# - NOTION_UPLOAD_FILES: enable uploading image files to Notion (default: true)
def should_upload_files_to_notion() -> bool:
    raw = os.environ.get("NOTION_UPLOAD_FILES", "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


# Crawl policy:
# - INCLUDE_NON_TOP: include non-top posts when true (default: true)
# - NON_TOP_MAX_PAGES: max pages to scan when including non-top (default: 3, 0=unlimited)
def should_include_non_top() -> bool:
    raw = os.environ.get("INCLUDE_NON_TOP", "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def get_non_top_max_pages() -> int:
    raw = os.environ.get("NON_TOP_MAX_PAGES", "3").strip()
    try:
        value = int(raw)
    except ValueError:
        return 3
    return max(0, value)


def log_attachments(label: str, attachments: list[dict]) -> None:
    if not attachments:
        return
    LOGGER.info("첨부파일 추출: %s (총 %s개)", label, len(attachments))
    for attachment in attachments:
        url = attachment.get("external", {}).get("url") or ""
        name = attachment.get("name") or ""
        LOGGER.info("첨부파일 링크: %s (%s)", url, name)


def cap_attachments(attachments: list[dict], label: str) -> list[dict]:
    max_count = get_attachment_max_count()
    if max_count <= 0:
        return attachments
    if len(attachments) > max_count:
        LOGGER.info(
            "첨부파일 상한 적용: %s (원본 %s개 -> %s개)",
            label,
            len(attachments),
            max_count,
        )
        return attachments[:max_count]
    return attachments


def normalize_attachment_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "")).strip().lower()


def extract_attachment_name(attachment: dict) -> str:
    name = attachment.get("name") or ""
    if name:
        return name
    url = attachment.get("external", {}).get("url") or ""
    if not url:
        return ""
    params = parse_qs(urlparse(url).query)
    name = params.get("sg", [""])[0].strip()
    if name:
        return name
    return Path(urlparse(url).path).name


def strip_dataview_prefix(filename: str) -> str:
    if re.match(r"^\d{10}", filename):
        return filename[10:]
    return filename


def replace_body_image_urls(body_blocks: list[dict], attachments: list[dict]) -> list[dict]:
    if not body_blocks or not attachments:
        return body_blocks
    name_map: dict[str, str] = {}
    for attachment in attachments:
        name = extract_attachment_name(attachment)
        key = normalize_attachment_name(name)
        url = attachment.get("external", {}).get("url") or ""
        if key and url and key not in name_map:
            name_map[key] = url
    if not name_map:
        return body_blocks
    replaced = 0
    for block in body_blocks:
        if block.get("type") != "image":
            continue
        image = block.get("image", {})
        if image.get("type") != "external":
            continue
        url = image.get("external", {}).get("url") or ""
        if not url:
            continue
        parsed = urlparse(url)
        if "/dataview/board/" not in parsed.path:
            continue
        filename = unquote(Path(parsed.path).name)
        if not filename:
            continue
        normalized = normalize_attachment_name(strip_dataview_prefix(filename))
        replacement = name_map.get(normalized)
        if replacement and replacement != url:
            image["external"]["url"] = replacement
            replaced += 1
    if replaced:
        LOGGER.info("본문 이미지 URL 치환: %s개", replaced)
    return body_blocks


def build_site_headers() -> dict:
    return {"User-Agent": USER_AGENT, "Referer": BASE_URL}


def is_image_name_or_url(name: str, url: str) -> bool:
    if IMAGE_EXT_PATTERN.search(name or ""):
        return True
    return bool(IMAGE_EXT_PATTERN.search(url or ""))


def is_pdf_name_or_url(name: str, url: str) -> bool:
    if re.search(r"\.pdf(?:$|\\?)", name or "", re.IGNORECASE):
        return True
    return bool(re.search(r"\.pdf(?:$|\\?)", url or "", re.IGNORECASE))


def is_embed_file_candidate(url: str) -> bool:
    if not url:
        return False
    if ATTACHMENT_EXT_PATTERN.search(url):
        return True
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    for key in ("filename", "fileName", "file_name", "sg"):
        value = params.get(key)
        if not value:
            continue
        candidate = value[0]
        if candidate and ATTACHMENT_EXT_PATTERN.search(candidate):
            return True
    allowed, _ = is_attachment_candidate(url, url, allow_domain_only=True)
    return allowed


def truncate_utf8(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes]
    while truncated and (truncated[-1] & 0xC0) == 0x80:
        truncated = truncated[:-1]
    return truncated.decode("utf-8", errors="ignore")


def sanitize_filename(name: str, fallback: str = "file") -> str:
    cleaned = re.sub(r"[\r\n]+", " ", (name or "")).strip()
    if not cleaned:
        return fallback
    cleaned = cleaned.replace("\"", "'")
    max_bytes = 900
    if len(cleaned.encode("utf-8")) <= max_bytes:
        return cleaned
    stem, ext = os.path.splitext(cleaned)
    if ext:
        ext_bytes = len(ext.encode("utf-8"))
        trimmed_stem = truncate_utf8(stem, max_bytes - ext_bytes)
        return f"{trimmed_stem}{ext}" if trimmed_stem else truncate_utf8(cleaned, max_bytes)
    return truncate_utf8(cleaned, max_bytes)


def derive_filename_from_url(url: str, fallback: str = "file") -> str:
    parsed = urlparse(url)
    name = unquote(Path(parsed.path).name)
    params = parse_qs(parsed.query)
    query_name = ""
    for key in ("filename", "fileName", "file_name", "sg", "name"):
        value = params.get(key)
        if value:
            candidate = value[0].strip()
            if candidate:
                query_name = candidate
                break
    if query_name:
        if not name:
            return query_name
        if "." not in name and "." in query_name:
            return query_name
        if name.lower() in {"download", "download3", "file"}:
            return query_name
    if name:
        return name
    return fallback


def guess_content_type_from_filename(filename: str) -> Optional[str]:
    ext = os.path.splitext(filename)[1].lower()
    if ext in CONTENT_TYPE_OVERRIDES:
        return CONTENT_TYPE_OVERRIDES[ext]
    return mimetypes.guess_type(filename)[0]


def normalize_content_type(
    raw_content_type: Optional[str],
    filename: str,
    url: str,
) -> str:
    cleaned = (raw_content_type or "").split(";", 1)[0].strip().lower()
    if cleaned and cleaned not in {"application/octet-stream", "binary/octet-stream"}:
        return cleaned
    guessed = guess_content_type_from_filename(filename)
    if not guessed:
        guessed = mimetypes.guess_type(url)[0]
    if guessed:
        return guessed
    return cleaned or "application/octet-stream"


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


def fetch_site_json(url: str) -> Optional[dict]:
    req = urllib.request.Request(url, headers=build_site_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        text = raw.decode("utf-8", errors="replace")
        return json.loads(text)
    except urllib.error.HTTPError as exc:
        LOGGER.info("API 요청 실패: %s (HTTP %s)", url, exc.code)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, socket.timeout):
            LOGGER.info("API 요청 실패: %s (timeout)", url)
        else:
            LOGGER.info("API 요청 실패: %s (%s)", url, exc.reason)
    except socket.timeout:
        LOGGER.info("API 요청 실패: %s (timeout)", url)
    except json.JSONDecodeError:
        LOGGER.info("API 응답 파싱 실패: %s", url)
    return None


def fetch_bbs_list(
    page_num: int,
    page_size: int = 20,
    config_fk: Optional[str] = None,
) -> list[dict]:
    config_fk = (config_fk or get_bbs_config_fk()).strip()
    params = {
        "pageNum": str(page_num),
        "pageSize": str(page_size),
        "bbsConfigFks": config_fk,
        "title": "",
        "content": "",
        "username": "",
        "category": "",
    }
    url = f"{BBS_LIST_API_URL}?{urlencode(params)}"
    data = fetch_site_json(url)
    if not data:
        return []
    return data.get("data", {}).get("list", []) or []


def fetch_bbs_detail(pk_id: str, config_fk: Optional[str] = None) -> Optional[dict]:
    config_fk = (config_fk or get_bbs_config_fk()).strip()
    params = {"pkId": pk_id}
    if config_fk:
        params["bbsConfigFk"] = config_fk
    url = f"{BBS_API_BASE}?{urlencode(params)}"
    data = fetch_site_json(url)
    if not data:
        return None
    detail = data.get("data")
    if not isinstance(detail, dict):
        return None
    return detail


def extract_attachments_from_api_data(data: dict) -> list[dict]:
    attachments: list[dict] = []
    seen: set[str] = set()
    allowed_domains = get_attachment_allowed_domains()
    for idx in range(1, 6):
        raw = data.get(f"fileValue{idx}")
        if not raw:
            continue
        url = normalize_file_url(str(raw))
        if not url or url in seen:
            continue
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        if not is_allowed_attachment_host(host, allowed_domains):
            continue
        seen.add(url)
        params = parse_qs(urlparse(url).query)
        name = params.get("sg", [""])[0].strip()
        if not name:
            name = Path(urlparse(url).path).name or "첨부파일"
        attachments.append({"name": name, "type": "external", "external": {"url": url}})
    return attachments


DEFAULT_ANNOTATIONS = {
    "bold": False,
    "italic": False,
    "strikethrough": False,
    "underline": False,
    "code": False,
    "color": "default",
}

CSS_COLOR_MAP = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "blue": (0, 0, 255),
    "green": (0, 128, 0),
    "yellow": (255, 255, 0),
    "orange": (255, 165, 0),
    "purple": (128, 0, 128),
    "pink": (255, 192, 203),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
    "brown": (165, 42, 42),
}
URL_TEXT_PATTERN = re.compile(
    r"(https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+|"
    r"www\.[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+)"
)
TRAILING_URL_PUNCTUATION = ").,;]"


def parse_css_color(value: str) -> Optional[tuple[int, int, int]]:
    if not value:
        return None
    raw = value.strip().lower()
    if raw in {"inherit", "transparent", "currentcolor"}:
        return None
    if raw in CSS_COLOR_MAP:
        return CSS_COLOR_MAP[raw]
    if raw.startswith("#"):
        hex_value = raw[1:]
        if len(hex_value) == 3:
            try:
                r = int(hex_value[0] * 2, 16)
                g = int(hex_value[1] * 2, 16)
                b = int(hex_value[2] * 2, 16)
                return r, g, b
            except ValueError:
                return None
        if len(hex_value) == 6:
            try:
                r = int(hex_value[0:2], 16)
                g = int(hex_value[2:4], 16)
                b = int(hex_value[4:6], 16)
                return r, g, b
            except ValueError:
                return None
        return None
    match = re.match(r"rgba?\(([^)]+)\)", raw)
    if match:
        parts = re.split(r"[,\\s/]+", match.group(1).strip())
        if len(parts) >= 3:
            rgb: list[int] = []
            for part in parts[:3]:
                if part.endswith("%"):
                    try:
                        rgb.append(int(float(part[:-1]) * 2.55))
                    except ValueError:
                        return None
                else:
                    try:
                        rgb.append(int(float(part)))
                    except ValueError:
                        return None
            return tuple(max(0, min(255, val)) for val in rgb)
    return None


def rgb_to_hsl(r: int, g: int, b: int) -> tuple[float, float, float]:
    rf = r / 255.0
    gf = g / 255.0
    bf = b / 255.0
    max_c = max(rf, gf, bf)
    min_c = min(rf, gf, bf)
    l = (max_c + min_c) / 2.0
    if max_c == min_c:
        return 0.0, 0.0, l
    d = max_c - min_c
    s = d / (2.0 - max_c - min_c) if l > 0.5 else d / (max_c + min_c)
    if max_c == rf:
        h = (gf - bf) / d + (6.0 if gf < bf else 0.0)
    elif max_c == gf:
        h = (bf - rf) / d + 2.0
    else:
        h = (rf - gf) / d + 4.0
    h *= 60.0
    return h, s, l


def notion_color_from_rgb(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    h, s, l = rgb_to_hsl(r, g, b)
    if s < 0.15:
        if l < 0.35:
            return "default"
        return "gray"
    if h < 20 or h >= 345:
        return "red"
    if h < 45:
        return "orange"
    if h < 65:
        return "yellow"
    if h < 150:
        return "green"
    if h < 250:
        return "blue"
    if h < 290:
        return "purple"
    return "pink"


def extract_inline_color(style: str) -> Optional[str]:
    if not style:
        return None
    found = False
    color_value: Optional[str] = None
    for chunk in style.split(";"):
        if ":" not in chunk:
            continue
        prop, value = chunk.split(":", 1)
        if prop.strip().lower() != "color":
            continue
        found = True
        rgb = parse_css_color(value)
        if not rgb:
            color_value = None
            continue
        mapped = notion_color_from_rgb(rgb)
        color_value = mapped if mapped != "default" else None
    if not found:
        return None
    return color_value


def normalize_inline_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def build_rich_text_from_segments(segments: list[dict]) -> list[dict]:
    rich_text: list[dict] = []
    has_content = False
    for segment in segments:
        text = segment.get("text", "")
        if not text:
            continue
        is_whitespace_only = text.isspace() and "\u00a0" not in text
        if is_whitespace_only and "\n" not in text:
            continue
        if is_whitespace_only and "\n" in text and not has_content:
            continue
        annotations = segment.get("annotations", DEFAULT_ANNOTATIONS)
        link = segment.get("link")
        if link and not is_valid_notion_url(link, allow_mailto=True):
            link = None
        remaining = text
        while remaining:
            chunk = remaining[:2000]
            remaining = remaining[2000:]
            text_payload = {"content": chunk}
            if link:
                text_payload["link"] = {"url": link}
            rich_text.append(
                {
                    "type": "text",
                    "text": text_payload,
                    "annotations": annotations,
                }
            )
            has_content = True
    return rich_text


def build_paragraph_block_from_rich_text(rich_text: list[dict]) -> Optional[dict]:
    if not rich_text:
        return None
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": rich_text},
    }


def build_bulleted_block_from_rich_text(rich_text: list[dict]) -> Optional[dict]:
    if not rich_text:
        return None
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": rich_text},
    }


def build_empty_paragraph_block() -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {"content": "\u00a0"},
                    "annotations": dict(DEFAULT_ANNOTATIONS),
                }
            ]
        },
    }


def normalize_content_url(raw_url: Optional[str]) -> Optional[str]:
    if not raw_url:
        return None
    raw_url = raw_url.strip()
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    parsed = urlparse(raw_url)
    if parsed.scheme in {"javascript", "mailto", "tel", "data"}:
        return None
    if not parsed.scheme:
        raw_url = urljoin(BASE_SITE, raw_url)
    return encode_url(raw_url)


QUERY_SAFE_CHARS = "/?:@-._~!$&'()*+,;=%"


def encode_url(raw_url: str) -> str:
    parsed = urlsplit(raw_url)
    path = quote(parsed.path, safe="/%")
    query = quote(parsed.query, safe=QUERY_SAFE_CHARS)
    fragment = quote(parsed.fragment, safe=QUERY_SAFE_CHARS)
    return urlunsplit((parsed.scheme, parsed.netloc, path, query, fragment))


def is_valid_notion_url(url: Optional[str], allow_mailto: bool = True) -> bool:
    if not url or any(ch.isspace() for ch in url):
        return False
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https"}:
        return bool(parsed.netloc)
    if allow_mailto and scheme in {"mailto", "tel"}:
        return bool(parsed.path)
    return False


def resolve_iframe_embed_url(raw_url: Optional[str]) -> Optional[str]:
    normalized = normalize_content_url(raw_url)
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    qs = parse_qs(parsed.query)
    file_values = qs.get("file")
    if file_values:
        candidate = file_values[0]
        if candidate:
            file_url = normalize_content_url(candidate)
            if file_url:
                return file_url
    return normalized


def normalize_link_url(raw_url: Optional[str]) -> Optional[str]:
    if not raw_url:
        return None
    cleaned = raw_url.strip()
    lowered = cleaned.lower()
    if lowered.startswith(("mailto:", "tel:")):
        return cleaned
    return normalize_content_url(cleaned)


def split_text_with_links(text: str) -> list[tuple[str, Optional[str]]]:
    if not text:
        return []
    parts: list[tuple[str, Optional[str]]] = []
    last_index = 0
    for match in URL_TEXT_PATTERN.finditer(text):
        start, end = match.span()
        if start > last_index:
            parts.append((text[last_index:start], None))
        url_text = match.group(0)
        trimmed = url_text.rstrip(TRAILING_URL_PUNCTUATION)
        suffix = url_text[len(trimmed) :]
        if trimmed:
            link = trimmed
            if link.lower().startswith("www."):
                link = "https://" + link
            normalized = normalize_content_url(link)
            link = normalized if normalized else None
            parts.append((trimmed, link))
        if suffix:
            parts.append((suffix, None))
        last_index = end
    if last_index < len(text):
        parts.append((text[last_index:], None))
    return parts


def build_image_block(url: str) -> dict:
    return {
        "object": "block",
        "type": "image",
        "image": {"type": "external", "external": {"url": url}},
    }


def build_embed_block(url: str) -> dict:
    return {
        "object": "block",
        "type": "embed",
        "embed": {"url": url},
    }


def build_file_block(upload_id: str) -> dict:
    return {
        "object": "block",
        "type": "file",
        "file": {"type": "file_upload", "file_upload": {"id": upload_id}},
    }


def build_pdf_block(upload_id: str) -> dict:
    return {
        "object": "block",
        "type": "pdf",
        "pdf": {"type": "file_upload", "file_upload": {"id": upload_id}},
    }


def build_space_rich_text() -> list[dict]:
    return [
        {
            "type": "text",
            "text": {"content": "\u00a0"},
            "annotations": dict(DEFAULT_ANNOTATIONS),
        }
    ]


def build_container_block(rich_text: Optional[list[dict]] = None) -> dict:
    return {
        "object": "block",
        "type": "quote",
        "quote": {
            "rich_text": rich_text or [],
            "color": "default",
        },
    }


def build_table_row_block(cells: list[list[dict]]) -> dict:
    return {
        "object": "block",
        "type": "table_row",
        "table_row": {"cells": cells},
    }


def build_table_block(
    rows: list[list[list[dict]]],
    has_column_header: bool,
    has_row_header: bool,
) -> Optional[dict]:
    if not rows:
        return None
    table_width = max((len(row) for row in rows), default=0)
    if table_width <= 0:
        return None
    normalized_rows: list[dict] = []
    for row in rows:
        if len(row) < table_width:
            row = row + [[] for _ in range(table_width - len(row))]
        normalized_rows.append(build_table_row_block(row))
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": table_width,
            "has_column_header": has_column_header,
            "has_row_header": has_row_header,
            "children": normalized_rows,
        },
    }


class TiptapBlockParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_tiptap = False
        self.tiptap_depth = 0
        self.seen_tiptap = False
        self.in_list_item = False
        self.current_block_type: Optional[str] = None
        self.rich_text: list[dict] = []
        self.bold_depth = 0
        self.italic_depth = 0
        self.underline_depth = 0
        self.strike_depth = 0
        self.code_depth = 0
        self.link_stack: list[Optional[str]] = []
        self.color_stack: list[str] = ["default"]
        self.color_push_stack: list[bool] = []
        self.blocks: list[dict] = []
        self.in_table = False
        self.table_depth = 0
        self.in_table_row = False
        self.in_table_cell = False
        self.table_rows: list[list[list[dict]]] = []
        self.table_cells: list[list[dict]] = []
        self.table_cell_segments: list[dict] = []
        self.table_cell_is_header = False
        self.table_row_index = -1
        self.table_cell_index = 0
        self.table_has_column_header = False
        self.table_has_row_header = False
        self.void_tags = {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "iframe",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        if not self.in_tiptap and tag == "div":
            classes = attrs_dict.get("class", "")
            if "tiptap" in classes.split():
                self.seen_tiptap = True
                self.in_tiptap = True
                self.tiptap_depth = 1
                return
        if not self.in_tiptap:
            return
        if tag not in self.void_tags:
            self.tiptap_depth += 1
            color = extract_inline_color(attrs_dict.get("style", ""))
            if color:
                self.color_stack.append(color)
                self.color_push_stack.append(True)
            else:
                self.color_push_stack.append(False)
        if tag == "table":
            if not self.in_table:
                self.flush_block()
                self.in_table = True
                self.table_depth = 1
                self.table_rows = []
                self.table_cells = []
                self.table_cell_segments = []
                self.table_cell_is_header = False
                self.table_row_index = -1
                self.table_cell_index = 0
                self.table_has_column_header = False
                self.table_has_row_header = False
            else:
                self.table_depth += 1
            return
        if self.in_table:
            if tag == "tr":
                self.in_table_row = True
                self.table_row_index += 1
                self.table_cell_index = 0
                self.table_cells = []
                return
            if tag in {"td", "th"}:
                self.in_table_cell = True
                self.table_cell_segments = []
                self.table_cell_is_header = tag == "th"
                return
            if tag == "p":
                if self.in_table_cell and self.table_cell_segments:
                    self.append_line_break()
                return
            if tag == "li":
                if self.in_table_cell and self.table_cell_segments:
                    self.append_line_break()
                return
            if tag in {"img", "iframe"}:
                return
        if tag == "li":
            if not self.in_list_item:
                self.flush_block()
                self.in_list_item = True
                self.current_block_type = "li"
        elif tag == "p":
            if not self.in_list_item and self.current_block_type != "p":
                self.flush_block()
                self.current_block_type = "p"
        elif tag in {"strong", "b"}:
            self.bold_depth += 1
        elif tag in {"em", "i"}:
            self.italic_depth += 1
        elif tag == "u":
            self.underline_depth += 1
        elif tag in {"s", "del", "strike"}:
            self.strike_depth += 1
        elif tag == "code":
            self.code_depth += 1
        elif tag == "a":
            href = attrs_dict.get("href") or ""
            link = normalize_link_url(href)
            if link and not is_valid_notion_url(link, allow_mailto=True):
                link = None
            self.link_stack.append(link)
        elif tag == "iframe":
            if self.in_table:
                return
            src = attrs_dict.get("src") or ""
            url = resolve_iframe_embed_url(src)
            if url and is_valid_notion_url(url, allow_mailto=False):
                self.flush_block()
                self.blocks.append(build_embed_block(url))
        elif tag == "img":
            src = attrs_dict.get("src") or ""
            url = normalize_content_url(src)
            if url:
                self.flush_block()
                self.blocks.append(build_image_block(url))
        elif tag == "br":
            self.append_line_break()

    def handle_endtag(self, tag: str) -> None:
        if not self.in_tiptap:
            return
        if tag not in self.void_tags and self.color_push_stack:
            pushed = self.color_push_stack.pop()
            if pushed and len(self.color_stack) > 1:
                self.color_stack.pop()
        if self.in_table:
            if tag in {"td", "th"}:
                self.flush_table_cell()
            elif tag == "tr":
                self.flush_table_row()
            elif tag == "table":
                self.table_depth = max(0, self.table_depth - 1)
                if self.table_depth == 0:
                    self.flush_table()
        if tag == "li" and self.in_list_item:
            self.flush_block()
            self.in_list_item = False
            self.current_block_type = None
        elif (
            tag == "p"
            and not self.in_list_item
            and self.current_block_type == "p"
            and not self.in_table
        ):
            if self.rich_text:
                self.flush_block()
            else:
                self.blocks.append(build_empty_paragraph_block())
            self.current_block_type = None
        elif tag in {"strong", "b"}:
            self.bold_depth = max(0, self.bold_depth - 1)
        elif tag in {"em", "i"}:
            self.italic_depth = max(0, self.italic_depth - 1)
        elif tag == "u":
            self.underline_depth = max(0, self.underline_depth - 1)
        elif tag in {"s", "del", "strike"}:
            self.strike_depth = max(0, self.strike_depth - 1)
        elif tag == "code":
            self.code_depth = max(0, self.code_depth - 1)
        elif tag == "a":
            if self.link_stack:
                self.link_stack.pop()
        if tag not in self.void_tags:
            self.tiptap_depth -= 1
            if self.tiptap_depth <= 0:
                self.flush_block()
                self.in_tiptap = False
                self.tiptap_depth = 0
                self.in_list_item = False
                self.current_block_type = None
                self.bold_depth = 0
                self.italic_depth = 0
                self.underline_depth = 0
                self.strike_depth = 0
                self.code_depth = 0
                self.link_stack.clear()
                self.color_stack = ["default"]
                self.color_push_stack = []
                self.in_table = False
                self.table_depth = 0
                self.in_table_row = False
                self.in_table_cell = False
                self.table_rows = []
                self.table_cells = []
                self.table_cell_segments = []
                self.table_cell_is_header = False
                self.table_row_index = -1
                self.table_cell_index = 0
                self.table_has_column_header = False
                self.table_has_row_header = False

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if not self.in_tiptap:
            return
        if tag == "br":
            self.append_line_break()
        elif tag == "iframe":
            if self.in_table:
                return
            attrs_dict = {key: value or "" for key, value in attrs}
            src = attrs_dict.get("src") or ""
            url = resolve_iframe_embed_url(src)
            if url and is_valid_notion_url(url, allow_mailto=False):
                self.flush_block()
                self.blocks.append(build_embed_block(url))
        elif tag == "img":
            if self.in_table:
                return
            attrs_dict = {key: value or "" for key, value in attrs}
            src = attrs_dict.get("src") or ""
            url = normalize_content_url(src)
            if url:
                self.flush_block()
                self.blocks.append(build_image_block(url))

    def handle_data(self, data: str) -> None:
        if not self.in_tiptap:
            return
        self.append_text(data)

    def append_text(self, data: str) -> None:
        if self.in_table and not self.in_table_cell:
            return
        text = normalize_inline_text(data)
        if not text:
            return
        annotations = dict(DEFAULT_ANNOTATIONS)
        annotations["bold"] = self.bold_depth > 0
        annotations["italic"] = self.italic_depth > 0
        annotations["underline"] = self.underline_depth > 0
        annotations["strikethrough"] = self.strike_depth > 0
        annotations["code"] = self.code_depth > 0
        annotations["color"] = self.color_stack[-1] if self.color_stack else "default"
        link = self.link_stack[-1] if self.link_stack else None
        if link:
            self.append_segment(text, annotations, link)
            return
        for segment_text, segment_link in split_text_with_links(text):
            self.append_segment(segment_text, annotations, segment_link)

    def append_segment(self, text: str, annotations: dict, link: Optional[str]) -> None:
        if not text:
            return
        segments = self.table_cell_segments if self.in_table_cell else self.rich_text
        if text.isspace() and "\u00a0" not in text:
            if not segments:
                return
            if not segments[-1]["text"].endswith((" ", "\n")):
                segments[-1]["text"] += " "
            return
        if segments:
            last = segments[-1]
            if last.get("annotations") == annotations and last.get("link") == link:
                last["text"] += text
                return
        segments.append({"text": text, "annotations": annotations, "link": link})

    def append_line_break(self) -> None:
        if self.in_table and not self.in_table_cell:
            return
        if (
            self.current_block_type is None
            and not self.in_list_item
            and not self.in_table
        ):
            self.current_block_type = "p"
        annotations = dict(DEFAULT_ANNOTATIONS)
        annotations["bold"] = self.bold_depth > 0
        annotations["italic"] = self.italic_depth > 0
        annotations["underline"] = self.underline_depth > 0
        annotations["strikethrough"] = self.strike_depth > 0
        annotations["code"] = self.code_depth > 0
        annotations["color"] = self.color_stack[-1] if self.color_stack else "default"
        link = self.link_stack[-1] if self.link_stack else None
        segments = self.table_cell_segments if self.in_table_cell else self.rich_text
        if segments:
            last = segments[-1]
            if last.get("annotations") == annotations and last.get("link") == link:
                last["text"] += "\n"
                return
        segments.append({"text": "\n", "annotations": annotations, "link": link})

    def flush_block(self) -> None:
        if not self.rich_text:
            return
        rich_text = build_rich_text_from_segments(self.rich_text)
        self.rich_text = []
        if self.in_list_item or self.current_block_type == "li":
            block = build_bulleted_block_from_rich_text(rich_text)
        else:
            block = build_paragraph_block_from_rich_text(rich_text)
        if block:
            self.blocks.append(block)

    def flush_table_cell(self) -> None:
        if not self.in_table_cell:
            return
        rich_text = build_rich_text_from_segments(self.table_cell_segments)
        self.table_cells.append(rich_text)
        if self.table_cell_is_header:
            if self.table_row_index == 0:
                self.table_has_column_header = True
            if self.table_cell_index == 0:
                self.table_has_row_header = True
        self.table_cell_segments = []
        self.table_cell_is_header = False
        self.in_table_cell = False
        self.table_cell_index += 1

    def flush_table_row(self) -> None:
        if not self.in_table_row:
            return
        if self.in_table_cell:
            self.flush_table_cell()
        if self.table_cells:
            self.table_rows.append(self.table_cells)
        self.table_cells = []
        self.in_table_row = False

    def flush_table(self) -> None:
        if self.in_table_cell:
            self.flush_table_cell()
        if self.in_table_row:
            self.flush_table_row()
        table_block = build_table_block(
            self.table_rows, self.table_has_column_header, self.table_has_row_header
        )
        if table_block:
            self.blocks.append(table_block)
        self.in_table = False
        self.table_depth = 0
        self.in_table_row = False
        self.in_table_cell = False
        self.table_rows = []
        self.table_cells = []
        self.table_cell_segments = []
        self.table_cell_is_header = False
        self.table_row_index = -1
        self.table_cell_index = 0
        self.table_has_column_header = False
        self.table_has_row_header = False


def extract_body_blocks_from_html(html_text: str) -> list[dict]:
    if not html_text:
        return []
    parser = TiptapBlockParser()
    parser.feed(html_text)
    parser.close()
    if parser.blocks:
        return normalize_body_blocks(parser.blocks)
    lowered = html_text.lower()
    looks_like_fragment = "<html" not in lowered and "<body" not in lowered
    if not parser.seen_tiptap and looks_like_fragment:
        wrapped = f'<div class="tiptap">{html_text}</div>'
        fallback = TiptapBlockParser()
        fallback.feed(wrapped)
        fallback.close()
        return normalize_body_blocks(fallback.blocks)
    return normalize_body_blocks(parser.blocks)


def chunks(items: list[dict], size: int) -> list[list[dict]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


class BodyContentDetector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_container = False
        self.depth = 0
        self.has_content = False
        self.void_tags = {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "iframe",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        if not self.in_container and tag == "div":
            classes = attrs_dict.get("class", "")
            if "tiptap" in classes.split() or "custom-css-tag-a" in classes.split():
                self.in_container = True
                self.depth = 1
                return
        if not self.in_container:
            return
        if tag in {"img", "a", "iframe"}:
            self.has_content = True
        if tag not in self.void_tags:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if not self.in_container:
            return
        if tag not in self.void_tags:
            self.depth -= 1
        if self.depth <= 0:
            self.in_container = False
            self.depth = 0

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if not self.in_container:
            return
        if tag in {"img", "iframe"}:
            self.has_content = True

    def handle_data(self, data: str) -> None:
        if not self.in_container:
            return
        text = unescape(data).replace("\u00a0", " ").strip()
        if text:
            self.has_content = True


def detect_body_has_content(html_text: str) -> bool:
    detector = BodyContentDetector()
    detector.feed(html_text)
    detector.close()
    return detector.has_content


def is_detail_url(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    path = parsed.path or ""
    if DETAIL_PATH_PATTERN.search(path):
        return True
    qs = parse_qs(parsed.query)
    return "bbsConfigFk" in qs


def is_detail_path_url(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    path = parsed.path or ""
    return bool(DETAIL_PATH_PATTERN.search(path))


def parse_config_map(raw: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not raw:
        return mapping
    for chunk in re.split(r"[;,]+", raw):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            key, value = chunk.split(":", 1)
        elif "=" in chunk:
            key, value = chunk.split("=", 1)
        else:
            continue
        key = key.strip()
        value = value.strip()
        if key and value:
            mapping[key] = value
    return mapping


def get_bbs_config_fk() -> str:
    raw = os.environ.get("BBS_CONFIG_FK", "").strip()
    if raw:
        return raw
    raw_list = os.environ.get("BBS_CONFIG_FKS", "").strip()
    if raw_list:
        parts = re.split(r"[,\s]+", raw_list)
        for part in parts:
            if part:
                return part
    return DEFAULT_BBS_CONFIG_FKS[0]


def get_bbs_config_fks() -> list[str]:
    raw = os.environ.get("BBS_CONFIG_FKS", "").strip()
    if raw:
        parts = re.split(r"[,\\s]+", raw)
        return [part for part in parts if part]
    single = os.environ.get("BBS_CONFIG_FK", "").strip()
    if single:
        return [single]
    return list(DEFAULT_BBS_CONFIG_FKS)


def get_config_classification_map() -> dict[str, str]:
    mapping = dict(DEFAULT_CONFIG_CLASSIFICATIONS)
    raw = os.environ.get("BBS_CONFIG_CLASSIFY", "").strip()
    if raw:
        mapping.update(parse_config_map(raw))
    return mapping


def get_classification_for_config(config_fk: str) -> Optional[str]:
    key = str(config_fk or "").strip()
    if not key:
        return None
    return get_config_classification_map().get(key)


def get_config_list_url_map() -> dict[str, str]:
    mapping = dict(DEFAULT_CONFIG_LIST_URLS)
    raw = os.environ.get("BBS_CONFIG_LIST_URLS", "").strip()
    if raw:
        mapping.update(parse_config_map(raw))
    return mapping


def get_list_base_url(config_fk: str) -> str:
    key = str(config_fk or "").strip()
    return get_config_list_url_map().get(key, BASE_URL)


def build_detail_url(detail_id: str, config_fk: Optional[str] = None) -> str:
    config_fk = (config_fk or get_bbs_config_fk()).strip()
    return f"{BASE_SITE}/ko/detail/{detail_id}?bbsConfigFk={config_fk}"


def parse_int(value: str) -> Optional[int]:
    digits = re.sub(r"[^0-9]", "", value)
    if not digits:
        return None
    return int(digits)


class TableRowParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_tr = False
        self.in_td = False
        self.current_cells: list[str] = []
        self.current_parts: list[str] = []
        self.current_meta: list[str] = []
        self.rows: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        if tag == "tr":
            self.in_tr = True
            self.current_cells = []
            self.current_meta = []
            onclick = attrs_dict.get("onclick")
            if onclick:
                self.current_meta.append(onclick)
            for key, value in attrs:
                if key.startswith("data-") and value:
                    self.current_meta.append(f"{key}={value}")
        if not self.in_tr:
            return
        onclick = attrs_dict.get("onclick")
        if onclick:
            self.current_meta.append(onclick)
        if tag == "td":
            self.in_td = True
            self.current_parts = []
        if tag == "a":
            href = attrs_dict.get("href") or ""
            if href:
                self.current_meta.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self.in_td:
            text = "".join(self.current_parts)
            text = unescape(text).replace("\u00a0", " ")
            self.current_cells.append(text.strip())
            self.in_td = False
            self.current_parts = []
        if tag == "tr" and self.in_tr:
            if self.current_cells:
                self.rows.append({"cells": self.current_cells, "meta": self.current_meta})
            self.in_tr = False
            self.current_cells = []
            self.current_meta = []

    def handle_data(self, data: str) -> None:
        if self.in_tr and self.in_td:
            self.current_parts.append(data)


def parse_rows(html_text: str, config_fk: Optional[str] = None) -> list[dict]:
    parser = TableRowParser()
    parser.feed(html_text)
    parser.close()
    items: list[dict] = []

    for row in parser.rows:
        cells = row.get("cells", [])
        if len(cells) < 5:
            continue
        num_or_top = cells[0]
        title = cells[1]
        author = cells[2]
        date_text = cells[-2]
        views_text = cells[-1]

        date_iso = parse_datetime(date_text)
        views = parse_int(views_text)
        if not date_iso or views is None:
            continue

        top = num_or_top.strip().upper() == "TOP"
        detail_url = None
        for meta in row.get("meta", []):
            candidate = normalize_detail_url(meta)
            if candidate and is_detail_url(candidate):
                detail_url = candidate
                break
            detail_id = extract_detail_id_from_text(meta)
            if detail_id:
                detail_url = normalize_detail_url(build_detail_url(detail_id, config_fk))
                break

        items.append(
            {
                "title": title,
                "author": author,
                "date": date_iso,
                "views": views,
                "top": top,
                "url": detail_url,
            }
        )

    return items


def extract_written_at_from_detail(html_text: str) -> Optional[str]:
    matches = re.findall(
        r"(작성일|등록일).*?(\d{4}[.\-]\d{2}[.\-]\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?)",
        html_text,
        re.DOTALL,
    )
    if not matches:
        return None
    for _, value in matches:
        if DATE_TIME_PATTERN.search(value):
            return parse_datetime(value)
    return parse_datetime(matches[0][1])


def extract_attachments_from_detail(html_text: str) -> list[dict]:
    attachments: list[dict] = []
    seen_urls: set[str] = set()
    allowlist_only_urls: list[str] = []

    def add_attachment(href: str, text: str, allow_domain_only: bool) -> None:
        url = normalize_file_url(href)
        if not url or url in seen_urls:
            return
        allowed, allowlist_only = is_attachment_candidate(
            url, text, allow_domain_only=allow_domain_only
        )
        if not allowed:
            return
        seen_urls.add(url)
        if allowlist_only:
            allowlist_only_urls.append(url)
        if text:
            name = text
        else:
            params = parse_qs(urlparse(url).query)
            name = params.get("sg", [""])[0]
        if not name:
            name = Path(urlparse(url).path).name or "첨부파일"
        attachments.append({"name": name, "type": "external", "external": {"url": url}})

    def extract_from_chunk(chunk: str, strict: bool) -> None:
        for match in re.finditer(
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            chunk,
            re.IGNORECASE | re.DOTALL,
        ):
            href = unescape(match.group(1)).strip()
            if not href:
                continue
            text = clean_text(match.group(2))
            if not strict:
                snippet = chunk[max(0, match.start() - 200) : match.end() + 200]
                lowered_href = href.lower()
                if (
                    "첨부" not in text
                    and "첨부" not in snippet
                    and "다운로드" not in text
                    and "다운로드" not in snippet
                    and not ATTACHMENT_EXT_PATTERN.search(href)
                    and not ATTACHMENT_EXT_PATTERN.search(text)
                    and not any(hint in lowered_href for hint in ATTACHMENT_HINTS)
                ):
                    continue
            add_attachment(href, text, allow_domain_only=strict)

    label_matches = list(re.finditer(r"첨부파일", html_text))
    has_label = bool(label_matches)
    if has_label:
        for match in label_matches:
            start = max(0, match.start() - 800)
            end = min(len(html_text), match.end() + 6000)
            extract_from_chunk(html_text[start:end], strict=True)
    if not attachments:
        extract_from_chunk(html_text, strict=has_label)
    if allowlist_only_urls:
        sample = ", ".join(allowlist_only_urls[:3])
        LOGGER.info(
            "allow_domain_only 첨부: %s개 (샘플=%s)",
            len(allowlist_only_urls),
            sample or "없음",
        )

    return attachments


def extract_attachments_from_page(page) -> list[dict]:
    result = page.evaluate(
        """
        () => {
            const results = [];
            const seen = new Set();
            let labelCount = 0;
            let labelLinkCount = 0;
            let labelCandidateCount = 0;
            const labelCandidateSamples = [];
            const extPattern = /\\.(pdf|hwp|hwpx|docx?|xlsx?|pptx?|zip|rar|7z|txt|csv|jpg|jpeg|png|gif|bmp)(?:$|\\?)/i;
            const hintPattern = /(file-fe-prd\\/board|filedown|filedownload|bbsfile|download|attach)/i;
            const queryKeyPattern = /(sg=|fileid=|file_id=|fileno=|file_no=|fileseq=|file_seq=|attachid=|attach_id=|attachno=|attach_no=)/i;
            const textHintPattern = /(첨부|다운로드)/;
            const isCandidate = (href, text) => (
                extPattern.test(href) ||
                hintPattern.test(href) ||
                queryKeyPattern.test(href) ||
                textHintPattern.test(text || "")
            );
            const collectLabelNodes = (root) => Array.from(root.querySelectorAll("*"))
                .filter(el => el.textContent && el.textContent.includes("첨부파일"));
            const containers = Array.from(document.querySelectorAll(".tiptap, .custom-css-tag-a"));
            let labels = [];
            if (containers.length) {
                const labelSet = new Set();
                containers.forEach(container => {
                    collectLabelNodes(container).forEach(label => labelSet.add(label));
                });
                labels = Array.from(labelSet);
            }
            if (!labels.length) {
                labels = collectLabelNodes(document.body);
            }
            labelCount = labels.length;
            const collectLinks = (root, trackCandidates) => {
                const links = root.querySelectorAll("a[href]");
                links.forEach(a => {
                    const href = a.getAttribute("href") || "";
                    const text = (a.textContent || "").trim();
                    if (!href) return;
                    const key = href + "|" + text;
                    if (seen.has(key)) return;
                    seen.add(key);
                    results.push({href, text});
                    if (trackCandidates && isCandidate(href, text)) {
                        labelCandidateCount += 1;
                        if (labelCandidateSamples.length < 3) {
                            labelCandidateSamples.push(href);
                        }
                    }
                });
                return links.length;
            };
            for (const label of labels) {
                let node = label;
                for (let i = 0; i < 6 && node; i += 1) {
                    const count = collectLinks(node, true);
                    if (count) {
                        labelLinkCount += count;
                        break;
                    }
                    node = node.parentElement;
                }
            }
            if (!results.length) {
                const links = document.querySelectorAll("a[href]");
                links.forEach(a => {
                    const href = a.getAttribute("href") || "";
                    const text = (a.textContent || "").trim();
                    if (!href) return;
                    const key = href + "|" + text;
                    if (seen.has(key)) return;
                    seen.add(key);
                    results.push({href, text});
                });
            }
            return {
                links: results,
                labelCount,
                labelLinkCount,
                labelCandidateCount,
                labelCandidateSamples,
            };
        }
        """
    )
    candidates = result.get("links", []) if isinstance(result, dict) else []
    label_count = result.get("labelCount", 0) if isinstance(result, dict) else 0
    label_link_count = result.get("labelLinkCount", 0) if isinstance(result, dict) else 0
    label_candidate_count = (
        result.get("labelCandidateCount", 0) if isinstance(result, dict) else 0
    )
    allow_domain_only = label_count > 0
    def build_attachments(candidate_list: list[dict]) -> tuple[list[dict], list[str]]:
        attachments: list[dict] = []
        seen_urls: set[str] = set()
        allowlist_only_urls: list[str] = []
        for candidate in candidate_list:
            href = candidate.get("href", "")
            text = candidate.get("text", "")
            url = normalize_file_url(href)
            if not url or url in seen_urls:
                continue
            allowed, allowlist_only = is_attachment_candidate(
                url, text, allow_domain_only=allow_domain_only
            )
            if not allowed:
                continue
            seen_urls.add(url)
            if allowlist_only:
                allowlist_only_urls.append(url)
            name = text
            if not name:
                params = parse_qs(urlparse(url).query)
                name = params.get("sg", [""])[0]
            if not name:
                name = Path(urlparse(url).path).name or "첨부파일"
            attachments.append(
                {"name": name, "type": "external", "external": {"url": url}}
            )
        return attachments, allowlist_only_urls

    attachments, allowlist_only_urls = build_attachments(candidates)
    if allow_domain_only and label_link_count > 0 and not attachments:
        all_candidates = page.evaluate(
            """
            () => {
                const results = [];
                const seen = new Set();
                const links = document.querySelectorAll("a[href]");
                links.forEach(a => {
                    const href = a.getAttribute("href") || "";
                    const text = (a.textContent || "").trim();
                    if (!href) return;
                    const key = href + "|" + text;
                    if (seen.has(key)) return;
                    seen.add(key);
                    results.push({href, text});
                });
                return results;
            }
            """
        )
        if isinstance(all_candidates, list) and all_candidates:
            LOGGER.info("첨부파일 폴백: 라벨 있음, 전체 링크 재스캔")
            candidates = all_candidates
            attachments, allowlist_only_urls = build_attachments(candidates)
    if allowlist_only_urls:
        sample = ", ".join(allowlist_only_urls[:3])
        LOGGER.info(
            "allow_domain_only 첨부: %s개 (샘플=%s)",
            len(allowlist_only_urls),
            sample or "없음",
        )
    if not attachments:
        if not candidates:
            LOGGER.info("첨부파일 후보 링크 없음 (라벨=%s)", label_count)
        else:
            sample = ", ".join(
                f"{c.get('href','')}" for c in candidates[:3] if c.get("href")
            )
            LOGGER.info(
                "첨부파일 필터링 결과 0개 (라벨=%s, 라벨링크=%s, 후보=%s, 샘플=%s)",
                label_count,
                label_link_count,
                len(candidates),
                sample or "없음",
            )
    return attachments


def build_list_url(page: int, base_url: Optional[str] = None) -> str:
    query = dict(DEFAULT_QUERY)
    query["page"] = str(page)
    base_url = base_url or BASE_URL
    return f"{base_url}?{urlencode(query)}"


def extract_detail_url_from_row_html(
    row_html: str,
    config_fk: Optional[str] = None,
) -> Optional[str]:
    for match in re.finditer(r'href="([^"]+)"', row_html):
        href = unescape(match.group(1))
        candidate = normalize_detail_url(href)
        if candidate and is_detail_url(candidate):
            return candidate
        detail_id = extract_detail_id_from_text(href)
        if detail_id:
            return normalize_detail_url(build_detail_url(detail_id, config_fk))
    match = re.search(r"/detail/(\d+)", row_html)
    if match:
        return normalize_detail_url(build_detail_url(match.group(1), config_fk))
    return None


def get_browser_launcher(playwright, browser: str):
    browser = browser.lower()
    if browser in {"chromium", "chrome", "edge"}:
        return playwright.chromium
    if browser == "firefox":
        return playwright.firefox
    if browser in {"webkit", "safari"}:
        return playwright.webkit
    raise RuntimeError(f"Unsupported BROWSER: {browser}")


def extract_list_rows(page, config_fk: Optional[str] = None) -> list[dict]:
    rows = page.locator(LIST_ROW_SELECTOR)
    count = rows.count()
    items = []

    for index in range(count):
        row = rows.nth(index)
        cells = row.locator("td")
        cell_count = cells.count()
        if cell_count < 5:
            continue

        num_or_top = cells.nth(0).inner_text().strip()
        title = cells.nth(1).inner_text().strip()
        author = cells.nth(2).inner_text().strip()
        date_text = cells.nth(cell_count - 2).inner_text().strip()
        views_text = cells.nth(cell_count - 1).inner_text().strip()

        date_iso = parse_datetime(date_text)
        views = parse_int(views_text)
        if views is None:
            continue

        top = num_or_top.strip().upper() == "TOP"
        detail_url = None
        link = row.locator("a[href]")
        link_count = link.count()
        if link_count:
            for idx in range(link_count):
                href = link.nth(idx).get_attribute("href")
                if not href:
                    continue
                candidate = normalize_detail_url(href)
                if candidate and is_detail_url(candidate):
                    detail_url = candidate
                    break
                detail_id = extract_detail_id_from_text(href)
                if detail_id:
                    detail_url = normalize_detail_url(build_detail_url(detail_id, config_fk))
                    break
        if not detail_url:
            onclick = row.get_attribute("onclick") or ""
            detail_id = extract_detail_id_from_text(onclick)
            if detail_id:
                detail_url = normalize_detail_url(build_detail_url(detail_id, config_fk))
            else:
                try:
                    row_html = row.evaluate("row => row.outerHTML")
                except Exception:
                    row_html = ""
                detail_id = extract_detail_id_from_text(row_html or "")
                if detail_id:
                    detail_url = normalize_detail_url(build_detail_url(detail_id, config_fk))
        items.append(
            {
                "title": title,
                "author": author,
                "date": date_iso,
                "views": views,
                "top": top,
                "row_index": index,
                "detail_url": detail_url,
            }
        )

    return items


def return_to_list_page(page, list_url: str) -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        page.go_back()
        page.wait_for_selector(LIST_ROW_SELECTOR, timeout=30000)
    except PlaywrightTimeoutError:
        if not goto_list_page(page, list_url):
            LOGGER.info("목록 복귀 실패: %s", list_url)


def wait_for_written_at(page, timeout_ms: int = 30000) -> bool:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        page.wait_for_function(
            "pattern => new RegExp(pattern).test(document.body.innerText)",
            arg=DATE_TIME_JS_PATTERN,
            timeout=timeout_ms,
        )
        return True
    except PlaywrightTimeoutError:
        return False


def wait_for_detail_url(page, list_url: str) -> Optional[str]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        page.wait_for_url(lambda url: is_detail_url(url) and url != list_url, timeout=30000)
    except PlaywrightTimeoutError:
        return None
    return page.url


def extract_detail_id_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    match = DETAIL_ID_CAPTURE_PATTERN.search(text)
    if match:
        return match.group(1)
    match = DETAIL_ID_FUNCTION_PATTERN.search(text)
    if match:
        return match.group(1)
    match = DETAIL_ID_PARAM_PATTERN.search(text)
    if match:
        return match.group(1)
    match = DETAIL_ID_DATA_ATTR_PATTERN.search(text)
    if match:
        return match.group(1)
    return None


def extract_detail_id_from_row(row) -> Optional[str]:
    for key in ("data-id", "data-no", "data-board-id", "data-article-id", "data-detail-id"):
        value = row.get_attribute(key)
        if value and value.isdigit():
            return value
    onclick = row.get_attribute("onclick") or ""
    detail_id = extract_detail_id_from_text(onclick)
    if detail_id:
        return detail_id
    try:
        dataset = row.evaluate("row => ({...row.dataset})")
        for value in dataset.values():
            if isinstance(value, str) and value.isdigit():
                return value
    except Exception:
        dataset = {}
    try:
        row_html = row.evaluate("row => row.outerHTML")
    except Exception:
        return None
    detail_id = extract_detail_id_from_text(row_html or "")
    if detail_id:
        return detail_id
    return None


def extract_written_at_from_page(page) -> Optional[str]:
    for label_text in ("작성일", "등록일"):
        locator = page.locator(f"text={label_text}")
        for idx in range(locator.count()):
            label_node = locator.nth(idx)
            try:
                container_text = label_node.locator("xpath=..").inner_text()
            except Exception:
                container_text = ""
            match = DATE_TIME_PATTERN.search(container_text)
            if match:
                return parse_datetime(match.group(0))
            try:
                sibling_texts = label_node.locator(
                    "xpath=following-sibling::*"
                ).all_inner_texts()
            except Exception:
                sibling_texts = []
            for text in sibling_texts:
                match = DATE_TIME_PATTERN.search(text)
                if match:
                    return parse_datetime(match.group(0))
    body_text = page.locator("body").inner_text()
    match = re.search(
        rf"(작성일|등록일).*?({DATE_TIME_PATTERN.pattern})",
        body_text,
    )
    if match:
        return parse_datetime(match.group(2))
    match = DATE_TIME_PATTERN.search(body_text)
    if match:
        return parse_datetime(match.group(0))
    match = DATE_PATTERN.search(body_text)
    if match:
        return parse_datetime(match.group(0))
    return None


def fetch_detail_metadata_via_playwright(
    page,
    list_url: str,
    detail_url: str,
) -> tuple[Optional[str], list[dict], list[dict]]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    written_at = None
    attachments: list[dict] = []
    body_blocks: list[dict] = []
    try:
        page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
        if not wait_for_written_at(page):
            LOGGER.info("작성일 로드 대기 실패: %s", detail_url)
        try:
            page.wait_for_selector("text=첨부파일", timeout=5000)
        except PlaywrightTimeoutError:
            pass
        label_visible = page.locator("text=첨부파일").count()
        if not label_visible:
            try:
                label_visible = page.wait_for_selector(
                    "text=첨부파일", timeout=10000, state="attached"
                )
                label_visible = 1 if label_visible else 0
            except PlaywrightTimeoutError:
                label_visible = 0
        LOGGER.info("첨부파일 라벨 감지: %s (%s)", label_visible, detail_url)
        written_at = extract_written_at_from_page(page)
        if not written_at:
            written_at = extract_written_at_from_detail(page.content())
        attachments = extract_attachments_from_page(page)
        if not attachments:
            attachments = extract_attachments_from_detail(page.content())
        body_blocks = extract_body_blocks_from_html(page.content())
        if attachments and body_blocks:
            body_blocks = replace_body_image_urls(body_blocks, attachments)
    except PlaywrightTimeoutError:
        LOGGER.info("상세 페이지 로드 실패: %s", detail_url)
    finally:
        return_to_list_page(page, list_url)
    return written_at, attachments, body_blocks


def fetch_detail_for_row(
    page,
    list_url: str,
    row_index: int,
    detail_url: Optional[str],
    config_fk: Optional[str] = None,
) -> tuple[Optional[str], Optional[str], list[dict], list[dict]]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    if detail_url:
        detail_url = normalize_detail_url(detail_url) or detail_url
        if detail_url and not is_detail_url(detail_url):
            LOGGER.info("상세 URL 경로 아님: %s", detail_url)
            detail_url = None
    if detail_url:
        written_at, attachments, body_blocks, signals = fetch_detail_metadata_from_url(
            detail_url
        )
        if should_retry_detail_fetch(written_at, attachments, body_blocks, signals):
            pw_written_at, pw_attachments, pw_body_blocks = fetch_detail_metadata_via_playwright(
                page, list_url, detail_url
            )
            if not written_at and pw_written_at:
                written_at = pw_written_at
            if pw_attachments:
                attachments = pw_attachments
            if pw_body_blocks:
                body_blocks = pw_body_blocks
        return written_at, detail_url, attachments, body_blocks

    rows = page.locator(LIST_ROW_SELECTOR)
    if row_index >= rows.count():
        return None, None, [], []

    row = rows.nth(row_index)
    row.scroll_into_view_if_needed()
    detail_id = extract_detail_id_from_row(row)
    if detail_id:
        detail_url = normalize_detail_url(build_detail_url(detail_id, config_fk))
        written_at, attachments, body_blocks, signals = fetch_detail_metadata_from_url(
            detail_url
        )
        if should_retry_detail_fetch(written_at, attachments, body_blocks, signals):
            pw_written_at, pw_attachments, pw_body_blocks = fetch_detail_metadata_via_playwright(
                page, list_url, detail_url
            )
            if not written_at and pw_written_at:
                written_at = pw_written_at
            if pw_attachments:
                attachments = pw_attachments
            if pw_body_blocks:
                body_blocks = pw_body_blocks
        if written_at or attachments or body_blocks:
            return written_at, detail_url, attachments, body_blocks
    row.click()

    detail_url = wait_for_detail_url(page, list_url)
    if not detail_url:
        LOGGER.info("상세 URL 전환 실패: row %s", row_index)
        return_to_list_page(page, list_url)
        return None, None, [], []

    normalized_detail_url = normalize_detail_url(detail_url) or detail_url
    written_at, attachments, body_blocks, _signals = fetch_detail_metadata_from_url(
        normalized_detail_url
    )
    if not wait_for_written_at(page):
        LOGGER.info("작성일 로드 대기 실패: %s", detail_url)
    if not written_at:
        written_at = extract_written_at_from_page(page)
        if not written_at:
            written_at = extract_written_at_from_detail(page.content())
    page_attachments = extract_attachments_from_page(page)
    if page_attachments:
        attachments = page_attachments
    elif not attachments:
        attachments = extract_attachments_from_detail(page.content())
    page_blocks = extract_body_blocks_from_html(page.content())
    if page_blocks:
        body_blocks = page_blocks
    if attachments and body_blocks:
        body_blocks = replace_body_image_urls(body_blocks, attachments)
    return_to_list_page(page, list_url)
    return written_at, normalized_detail_url, attachments, body_blocks


def goto_list_page(page, url: str) -> bool:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except PlaywrightTimeoutError:
        LOGGER.info("페이지 로드 타임아웃: %s", url)
        return False
    if response is not None and response.status >= 400:
        LOGGER.info("페이지 응답 코드: %s (%s)", response.status, url)
    try:
        page.wait_for_selector(LIST_ROW_SELECTOR, timeout=30000)
    except PlaywrightTimeoutError:
        LOGGER.info("목록 셀렉터 미검출: %s", url)
        return False
    return True


def crawl_top_items_api(
    config_fk: str,
    include_non_top: bool,
    non_top_max_pages: int,
) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    page_number = 1
    classification = get_classification_for_config(config_fk)
    page_size_raw = os.environ.get("BBS_PAGE_SIZE", "20")
    try:
        page_size = max(1, int(page_size_raw))
    except ValueError:
        page_size = 20

    while True:
        if include_non_top and non_top_max_pages > 0 and page_number > non_top_max_pages:
            LOGGER.info("비TOP 페이지 상한 도달(API): %s", non_top_max_pages)
            break
        LOGGER.info("페이지 로드 시작(API): %s", page_number)
        page_entries = fetch_bbs_list(page_number, page_size, config_fk=config_fk)
        LOGGER.info("페이지 %s 항목 수(API): %s", page_number, len(page_entries))
        if not page_entries:
            break

        if include_non_top:
            entries_to_process = page_entries
        else:
            entries_to_process = [
                entry
                for entry in page_entries
                if str(entry.get("isTop", "")).upper() == "Y"
            ]
        new_count = 0

        for entry in entries_to_process:
            pk_id = str(entry.get("pkId") or "").strip()
            if not pk_id:
                continue
            detail_url = normalize_detail_url(
                build_detail_url(pk_id, config_fk)
            ) or build_detail_url(pk_id, config_fk)
            detail = fetch_bbs_detail(pk_id, config_fk=config_fk)
            if detail is None:
                LOGGER.info("상세 API 로드 실패: %s", pk_id)
                detail = {}

            title = normalize_title_key(detail.get("title") or entry.get("title") or "")
            author = detail.get("userName") or entry.get("userName") or entry.get("userNickName") or ""
            written_at = parse_compact_datetime(detail.get("regDate") or entry.get("regDate"))
            views_raw = detail.get("viewCount", entry.get("viewCount"))
            views = parse_int(str(views_raw)) if views_raw is not None else None
            top = str(entry.get("isTop", "")).upper() == "Y"
            if not include_non_top and not top:
                continue

            attachments = extract_attachments_from_api_data(detail or entry)
            content_html = detail.get("content") or ""
            body_blocks = extract_body_blocks_from_html(content_html) if content_html else []
            if attachments and body_blocks:
                body_blocks = replace_body_image_urls(body_blocks, attachments)

            item = {
                "title": title,
                "author": author,
                "date": written_at,
                "views": views,
                "top": top,
                "url": detail_url,
            }
            if body_blocks:
                item["body_blocks"] = body_blocks
            if classification:
                item["classification"] = classification
            ensure_item_title(item, body_blocks, detail_url)
            if attachments:
                attachments = cap_attachments(attachments, item["title"])
                item["attachments"] = attachments
                log_attachments(item["title"], attachments)

            key = detail_url or f"{item['title']}|{written_at or ''}"
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
            new_count += 1

        LOGGER.info("페이지 %s 신규 수집 수(API): %s", page_number, new_count)
        if not include_non_top:
            has_non_top = any(
                str(entry.get("isTop", "")).upper() != "Y" for entry in page_entries
            )
            if has_non_top:
                LOGGER.info("페이지 %s에서 비TOP 발견, 다음 페이지 탐색 중단(API)", page_number)
                break
        page_number += 1

    return items


def crawl_top_items_playwright(
    config_fk: str,
    include_non_top: bool,
    non_top_max_pages: int,
) -> list[dict]:
    base_url = get_list_base_url(config_fk)
    classification = get_classification_for_config(config_fk)
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    except ImportError:
        LOGGER.info("Playwright 미설치: HTTP 모드로 전환")
        return crawl_top_items_http(config_fk, include_non_top, non_top_max_pages)

    items = []
    seen = set()
    browser_name = os.environ.get("BROWSER", "chromium")
    headless_raw = os.environ.get("HEADLESS", "1").strip().lower()
    headless = headless_raw not in {"0", "false", "no", "off"}
    user_agent = os.environ.get("USER_AGENT", USER_AGENT)
    fallback_to_http = False

    with sync_playwright() as playwright:
        try:
            launcher = get_browser_launcher(playwright, browser_name)
            browser = launcher.launch(headless=headless)
        except Exception as exc:
            LOGGER.info("Playwright 브라우저 실행 실패: %s (HTTP 모드로 전환)", exc)
            return crawl_top_items_http(config_fk, include_non_top, non_top_max_pages)
        try:
            context = browser.new_context(
                user_agent=user_agent,
                viewport={"width": 1920, "height": 1080},
            )
            page = context.new_page()

            page_number = 1
            while True:
                if include_non_top and non_top_max_pages > 0 and page_number > non_top_max_pages:
                    LOGGER.info("비TOP 페이지 상한 도달: %s", non_top_max_pages)
                    break
                url = build_list_url(page_number, base_url)
                LOGGER.info("페이지 로드 시작: %s", url)
                if not goto_list_page(page, url):
                    LOGGER.info("페이지 %s 로드 실패", page_number)
                    if page_number == 1:
                        LOGGER.info("Playwright 페이지 로드 실패: HTTP 모드로 전환")
                        fallback_to_http = True
                    break

                page_items = extract_list_rows(page, config_fk)
                LOGGER.info("페이지 %s 항목 수: %s", page_number, len(page_items))
                if not page_items:
                    break

                if include_non_top:
                    items_to_process = page_items
                else:
                    items_to_process = [item for item in page_items if item.get("top")]
                new_count = 0
                for item in items_to_process:
                    body_blocks: list[dict] = []
                    attachments: list[dict] = []
                    written_at, detail_url, attachments, body_blocks = fetch_detail_for_row(
                        page,
                        url,
                        item["row_index"],
                        item.get("detail_url"),
                        config_fk,
                    )
                    if written_at:
                        item["date"] = written_at
                    if detail_url:
                        item["url"] = normalize_detail_url(detail_url)
                    if body_blocks:
                        item["body_blocks"] = body_blocks
                    if classification:
                        item["classification"] = classification
                    ensure_item_title(item, body_blocks, detail_url or item.get("url"))
                    if not detail_url:
                        LOGGER.info("상세 URL 미확보: %s", item["title"])
                    if not written_at:
                        LOGGER.info(
                            "작성일 미검출: %s (%s)",
                            item["title"],
                            detail_url or "URL없음",
                        )
                    if attachments:
                        attachments = cap_attachments(attachments, item["title"])
                        item["attachments"] = attachments
                        log_attachments(item["title"], attachments)
                    key = item.get("url") or f"{item['title']}|{item.get('date') or ''}"
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append(item)
                    new_count += 1

                LOGGER.info("페이지 %s 신규 수집 수: %s", page_number, new_count)
                if not include_non_top:
                    has_non_top = any(not item.get("top") for item in page_items)
                    if has_non_top:
                        LOGGER.info("페이지 %s에서 비TOP 발견, 다음 페이지 탐색 중단", page_number)
                        break
                page_number += 1
        finally:
            browser.close()

    if fallback_to_http:
        return crawl_top_items_http(config_fk, include_non_top, non_top_max_pages)
    return items


def crawl_top_items() -> list[dict]:
    include_non_top = should_include_non_top()
    non_top_max_pages = get_non_top_max_pages()
    if include_non_top:
        limit_label = "제한없음" if non_top_max_pages <= 0 else str(non_top_max_pages)
        LOGGER.info("비TOP 포함 모드: 최대 페이지=%s", limit_label)

    config_fks = get_bbs_config_fks()
    if not config_fks:
        return []

    items: list[dict] = []
    seen: set[str] = set()
    for config_fk in config_fks:
        classification = get_classification_for_config(config_fk)
        LOGGER.info(
            "수집 설정: bbsConfigFk=%s, 분류=%s",
            config_fk,
            classification or "없음",
        )
        config_items = crawl_top_items_api(config_fk, include_non_top, non_top_max_pages)
        if not config_items:
            config_items = crawl_top_items_playwright(
                config_fk, include_non_top, non_top_max_pages
            )
        for item in config_items:
            key = item.get("url") or f"{item.get('title','')}|{item.get('date') or ''}"
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
    return items


def crawl_top_items_http(
    config_fk: str,
    include_non_top: bool,
    non_top_max_pages: int,
) -> list[dict]:
    items = []
    seen = set()
    page_number = 1
    base_url = get_list_base_url(config_fk)
    classification = get_classification_for_config(config_fk)

    while True:
        if include_non_top and non_top_max_pages > 0 and page_number > non_top_max_pages:
            LOGGER.info("비TOP 페이지 상한 도달(HTTP): %s", non_top_max_pages)
            break
        url = build_list_url(page_number, base_url)
        LOGGER.info("페이지 로드 시작(HTTP): %s", url)
        html_text = fetch_html(url)
        if not html_text:
            LOGGER.info("페이지 %s 로드 실패(HTTP)", page_number)
            break
        page_items = parse_rows(html_text, config_fk)
        LOGGER.info("페이지 %s 항목 수(HTTP): %s", page_number, len(page_items))
        if not page_items:
            break

        if include_non_top:
            items_to_process = page_items
        else:
            items_to_process = [item for item in page_items if item.get("top")]
        new_count = 0
        for item in items_to_process:
            body_blocks: list[dict] = []
            attachments: list[dict] = []
            if item.get("url"):
                written_at, attachments, body_blocks, _signals = fetch_detail_metadata_from_url(
                    item["url"]
                )
                if written_at:
                    item["date"] = written_at
                if body_blocks:
                    item["body_blocks"] = body_blocks
            if classification:
                item["classification"] = classification
            ensure_item_title(item, body_blocks, item.get("url"))
            if attachments:
                attachments = cap_attachments(attachments, item["title"])
                item["attachments"] = attachments
                log_attachments(item["title"], attachments)
            key = item.get("url") or f"{item['title']}|{item.get('date') or ''}"
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
            new_count += 1

        LOGGER.info("페이지 %s 신규 수집 수(HTTP): %s", page_number, new_count)
        if not include_non_top:
            has_non_top = any(not item.get("top") for item in page_items)
            if has_non_top:
                LOGGER.info("페이지 %s에서 비TOP 발견, 다음 페이지 탐색 중단(HTTP)", page_number)
                break
        page_number += 1

    return items


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


def fetch_html(url: str) -> Optional[str]:
    req = urllib.request.Request(url, headers=build_site_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        LOGGER.info("상세 HTML 요청 실패: %s (HTTP %s)", url, exc.code)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, socket.timeout):
            LOGGER.info("상세 HTML 요청 실패: %s (timeout)", url)
        else:
            LOGGER.info("상세 HTML 요청 실패: %s (%s)", url, exc.reason)
    except socket.timeout:
        LOGGER.info("상세 HTML 요청 실패: %s (timeout)", url)
    return None


def build_detail_signals(html_text: str) -> dict:
    return {
        "has_html": True,
        "has_attachment_label": "첨부파일" in html_text,
        "has_attachment_link": bool(ATTACHMENT_LINK_PATTERN.search(html_text)),
        "has_body_container": bool(BODY_CONTAINER_PATTERN.search(html_text)),
        "body_has_content": detect_body_has_content(html_text),
    }


def should_retry_detail_fetch(
    written_at: Optional[str],
    attachments: list[dict],
    body_blocks: list[dict],
    signals: dict,
) -> bool:
    reasons: list[str] = []
    if not written_at:
        reasons.append("작성일")
    if (signals.get("has_attachment_label") or signals.get("has_attachment_link")) and not attachments:
        reasons.append("첨부파일")
    if (
        signals.get("has_body_container")
        and signals.get("body_has_content")
        and not body_blocks
    ):
        reasons.append("본문")
    retry = bool(reasons)
    LOGGER.info(
        "상세 재시도 판단: %s (reasons=%s, written_at=%s, attachments=%s, body_blocks=%s, signals=label=%s,link=%s,body_container=%s,body_content=%s)",
        "Y" if retry else "N",
        ",".join(reasons) if reasons else "-",
        "Y" if written_at else "N",
        len(attachments),
        len(body_blocks),
        int(bool(signals.get("has_attachment_label"))),
        int(bool(signals.get("has_attachment_link"))),
        int(bool(signals.get("has_body_container"))),
        int(bool(signals.get("body_has_content"))),
    )
    return retry


def fetch_detail_metadata_from_url(
    detail_url: str,
) -> tuple[Optional[str], list[dict], list[dict], dict]:
    html_text = fetch_html(detail_url)
    if not html_text:
        return None, [], [], {
            "has_html": False,
            "has_attachment_label": False,
            "has_attachment_link": False,
            "has_body_container": False,
            "body_has_content": False,
        }
    signals = build_detail_signals(html_text)
    if signals.get("has_attachment_label"):
        LOGGER.info("첨부파일 HTML 감지: %s", detail_url)
    written_at = extract_written_at_from_detail(html_text)
    attachments = extract_attachments_from_detail(html_text)
    body_blocks = extract_body_blocks_from_html(html_text)
    if attachments and body_blocks:
        body_blocks = replace_body_image_urls(body_blocks, attachments)
    return written_at, attachments, body_blocks, signals


def fetch_database(token: str, database_id: str) -> dict:
    url = f"https://api.notion.com/v1/databases/{database_id}"
    return notion_request("GET", url, token)


def update_database(token: str, database_id: str, properties: dict) -> dict:
    url = f"https://api.notion.com/v1/databases/{database_id}"
    payload = {"properties": properties}
    return notion_request("PATCH", url, token, payload)


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


def require_property_type(database: dict, property_name: str, expected_type: str) -> None:
    prop = database.get("properties", {}).get(property_name)
    if not prop:
        raise RuntimeError(
            f"Notion 속성 누락: {property_name} (필수 타입: {expected_type})"
        )
    actual = prop.get("type")
    if actual != expected_type:
        raise RuntimeError(
            f"Notion 속성 타입 불일치: {property_name} (기대 {expected_type}, 실제 {actual})"
        )


def validate_required_properties(database: dict) -> None:
    require_property_type(database, TITLE_PROPERTY, "title")
    require_property_type(database, TOP_PROPERTY, "checkbox")
    require_property_type(database, DATE_PROPERTY, "date")
    require_property_type(database, AUTHOR_PROPERTY, "select")
    require_property_type(database, URL_PROPERTY, "url")
    require_property_type(database, TYPE_PROPERTY, "select")


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


def log_environment_info() -> None:
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    playwright_installed = importlib.util.find_spec("playwright") is not None
    browser = os.environ.get("BROWSER", "chromium")
    headless_raw = os.environ.get("HEADLESS", "1").strip().lower()
    headless = headless_raw not in {"0", "false", "no", "off"}
    sync_mode = get_sync_mode()
    upload_files = should_upload_files_to_notion()
    LOGGER.info(
        "환경: Python=%s, Playwright=%s",
        python_version,
        "설치됨" if playwright_installed else "미설치",
    )
    config_fks = get_bbs_config_fks()
    config_label = ",".join(config_fks) if config_fks else "없음"
    class_map = get_config_classification_map()
    class_label = ", ".join(
        f"{key}:{value}" for key, value in class_map.items() if key in config_fks
    )
    LOGGER.info(
        "환경: BROWSER=%s, HEADLESS=%s, BBS_CONFIG_FKS=%s, SYNC_MODE=%s",
        browser,
        "1" if headless else "0",
        config_label,
        sync_mode,
    )
    if class_label:
        LOGGER.info("환경: BBS_CONFIG_CLASSIFY=%s", class_label)
    LOGGER.info(
        "환경: NOTION_VERSION=%s, NOTION_UPLOAD_FILES=%s",
        get_notion_api_version(),
        "1" if upload_files else "0",
    )


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


def is_empty_paragraph_block(block: dict) -> bool:
    if block.get("type") != "paragraph":
        return False
    rich_text = block.get("paragraph", {}).get("rich_text", [])
    if not rich_text:
        return True
    content = "".join(
        item.get("text", {}).get("content", "") for item in rich_text
    )
    return content.replace("\u00a0", "").strip() == ""


def strip_trailing_empty_paragraphs(blocks: list[dict]) -> list[dict]:
    if not blocks:
        return blocks
    end = len(blocks)
    while end > 0 and is_empty_paragraph_block(blocks[end - 1]):
        end -= 1
    if end == len(blocks):
        return blocks
    return blocks[:end]


def trim_trailing_whitespace_rich_text(rich_text: list[dict]) -> None:
    idx = len(rich_text) - 1
    while idx >= 0:
        item = rich_text[idx]
        if item.get("type") != "text":
            break
        text_payload = item.get("text", {})
        content = text_payload.get("content", "")
        trimmed = content.rstrip()
        if trimmed == content:
            break
        if trimmed:
            text_payload["content"] = trimmed
            break
        rich_text.pop()
        idx -= 1


def normalize_body_blocks(blocks: list[dict]) -> list[dict]:
    normalized = strip_trailing_empty_paragraphs(blocks or [])
    if not normalized:
        return normalized
    last = normalized[-1]
    block_type = last.get("type")
    if block_type in {"paragraph", "bulleted_list_item"}:
        rich_text = last.get(block_type, {}).get("rich_text", [])
        if rich_text:
            trim_trailing_whitespace_rich_text(rich_text)
            if not rich_text:
                normalized = strip_trailing_empty_paragraphs(normalized[:-1])
    return normalized


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


def rich_text_plain_text(rich_text: list[dict]) -> str:
    return "".join(item.get("text", {}).get("content", "") for item in rich_text)


def extract_first_nonempty_line(text: str) -> str:
    if not text:
        return ""
    for line in text.splitlines():
        cleaned = line.replace("\u00a0", " ").strip()
        if cleaned:
            return cleaned
    return text.replace("\u00a0", " ").strip()


def derive_title_from_blocks(blocks: list[dict]) -> str:
    for block in blocks or []:
        block_type = block.get("type")
        if block_type not in {"paragraph", "bulleted_list_item"}:
            continue
        rich_text = block.get(block_type, {}).get("rich_text", [])
        if not rich_text:
            continue
        text = rich_text_plain_text(rich_text)
        candidate = extract_first_nonempty_line(text)
        if candidate:
            return candidate
    return ""


def build_fallback_title(detail_url: Optional[str], date_iso: Optional[str]) -> str:
    detail_id = extract_detail_id_from_text(detail_url or "")
    if detail_id:
        return f"제목없음-{detail_id}"
    date_key = normalize_date_key(date_iso)
    if date_key:
        return f"제목없음-{date_key}"
    return "제목없음"


def ensure_item_title(
    item: dict,
    body_blocks: list[dict],
    detail_url: Optional[str] = None,
) -> None:
    title = normalize_title_key(item.get("title", ""))
    if title:
        item["title"] = title
        return
    derived = derive_title_from_blocks(body_blocks)
    if derived:
        item["title"] = normalize_title_key(derived)
        return
    item["title"] = build_fallback_title(detail_url or item.get("url"), item.get("date"))


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


def get_sync_mode() -> str:
    raw = os.environ.get("SYNC_MODE", "overwrite").strip().lower()
    if raw in {"overwrite", "preserve"}:
        return raw
    return "overwrite"


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
    if has_attachments_property and item.get("attachments"):
        props[ATTACHMENT_PROPERTY] = {"files": item["attachments"]}
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


def find_existing_page(
    token: str,
    database_id: str,
    detail_url: Optional[str],
    title: str,
    date_iso: Optional[str],
) -> Optional[dict]:
    if detail_url:
        results = query_database(
            token,
            database_id,
            {"property": URL_PROPERTY, "url": {"equals": detail_url}},
        )
        if len(results) == 1:
            return results[0]
        if len(results) > 1:
            LOGGER.info("URL 중복 감지: %s", detail_url)
            return None

    if title and date_iso:
        results = query_database(
            token,
            database_id,
            {
                "and": [
                    {"property": TITLE_PROPERTY, "title": {"equals": title}},
                    {"property": DATE_PROPERTY, "date": {"equals": date_iso}},
                ]
            },
        )
        if len(results) == 1:
            return results[0]
        if len(results) > 1:
            LOGGER.info("제목+작성일 중복 감지: %s (%s)", title, date_iso)
            return None

    if title:
        results = query_database(
            token,
            database_id,
            {"property": TITLE_PROPERTY, "title": {"equals": title}},
        )
        if len(results) == 1:
            return results[0]
    return None


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


def iter_top_pages(token: str, database_id: str):
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    payload = {
        "filter": {"property": TOP_PROPERTY, "checkbox": {"equals": True}},
        "page_size": 100,
    }

    while True:
        data = notion_request("POST", url, token, payload)
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


def resolve_html_path() -> Optional[Path]:
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    env_path = os.environ.get("HTML_PATH")
    if env_path:
        return Path(env_path)
    return None


def main() -> None:
    setup_logging()
    load_dotenv()
    log_environment_info()
    if should_run_attachment_selftest():
        run_attachment_policy_selftest()
        return

    notion_token = os.environ.get("NOTION_TOKEN")
    database_id = os.environ.get("NOTION_DB_ID")

    if not notion_token or not database_id:
        raise RuntimeError("NOTION_TOKEN and NOTION_DB_ID must be set (env or .env)")

    html_path = resolve_html_path()
    if html_path is not None:
        if not html_path.exists():
            raise RuntimeError(f"HTML file not found: {html_path}")
        html_text = html_path.read_text(encoding="utf-8", errors="replace")
        items = parse_rows(html_text, get_bbs_config_fk())
    else:
        items = crawl_top_items()

    if not items:
        raise RuntimeError("No items parsed from source")

    author_values: set[str] = set()
    type_values: set[str] = set()
    classification_values: set[str] = set()
    default_classification = get_classification_for_config(get_bbs_config_fk())
    for item in items:
        ensure_item_title(item, item.get("body_blocks", []), item.get("url"))
        if not item.get("classification") and default_classification:
            item["classification"] = default_classification
        item["type"] = extract_type_from_title(item["title"])
        if item.get("author"):
            author_values.add(item["author"])
        if item.get("type"):
            type_values.add(item["type"])
        if item.get("classification"):
            classification_values.add(item["classification"])

    database = fetch_database(notion_token, database_id)
    database = ensure_url_property(notion_token, database_id, database)
    database = ensure_type_property(notion_token, database_id, database)
    database = ensure_attachment_property(notion_token, database_id, database)
    database = ensure_body_hash_property(notion_token, database_id, database)
    validate_required_properties(database)
    author_options = get_select_options(database, AUTHOR_PROPERTY)
    type_options = get_select_options(database, TYPE_PROPERTY)
    has_classification_property = validate_optional_property_type(
        database, CLASSIFICATION_PROPERTY, "select"
    )
    author_options = ensure_select_options_batch(
        notion_token, database_id, AUTHOR_PROPERTY, author_options, author_values
    )
    type_options = ensure_select_options_batch(
        notion_token, database_id, TYPE_PROPERTY, type_options, type_values
    )
    if has_classification_property and classification_values:
        classification_options = get_select_options(database, CLASSIFICATION_PROPERTY)
        classification_options = ensure_select_options_batch(
            notion_token,
            database_id,
            CLASSIFICATION_PROPERTY,
            classification_options,
            classification_values,
        )
    has_views_property = validate_optional_property_type(database, VIEWS_PROPERTY, "number")
    has_attachments_property = validate_optional_property_type(
        database, ATTACHMENT_PROPERTY, "files"
    )
    has_body_hash_property = validate_optional_property_type(
        database, BODY_HASH_PROPERTY, "rich_text"
    )
    sync_mode = get_sync_mode()
    upload_files = should_upload_files_to_notion()

    created = 0
    updated = 0
    body_updated = 0

    current_top_urls: set[str] = set()
    current_top_dates: dict[str, set[str]] = {}
    for item in items:
        is_top = bool(item.get("top"))
        if item.get("url"):
            normalized_url = normalize_detail_url(item["url"])
            if normalized_url:
                item["url"] = normalized_url
                if is_top:
                    current_top_urls.add(normalized_url)
        label = f"{item['title']} ({item.get('date') or '날짜없음'})"
        date_key = normalize_date_key(item.get("date"))
        if is_top:
            current_top_dates.setdefault(item["title"], set()).add(date_key)
        LOGGER.info("처리 시작: %s", label)
        if upload_files and has_attachments_property and item.get("attachments"):
            item["attachments"] = prepare_attachments_for_sync(
                notion_token, item["attachments"]
            )
        properties = build_properties(
            item,
            has_views_property,
            has_attachments_property,
            has_classification_property,
        )
        existing_page = find_existing_page(
            notion_token,
            database_id,
            item.get("url"),
            item["title"],
            item.get("date"),
        )
        page_id = existing_page.get("id") if existing_page else None
        existing_hash = ""
        if has_body_hash_property and existing_page:
            existing_hash = extract_rich_text_value(
                existing_page.get("properties", {}), BODY_HASH_PROPERTY
            )
        if page_id:
            update_page(notion_token, page_id, properties)
            updated += 1
            LOGGER.info("업데이트 완료: %s", label)
        else:
            page_id = create_page(notion_token, database_id, properties)
            created += 1
            LOGGER.info("생성 완료: %s", label)
        body_blocks = item.get("body_blocks", [])
        if page_id and body_blocks:
            if has_body_hash_property:
                image_mode = ""
                if upload_files and has_image_blocks(body_blocks):
                    image_mode = BODY_HASH_IMAGE_MODE_UPLOAD
                hash_blocks = normalize_body_blocks_for_hash(body_blocks, upload_files)
                body_hash = compute_body_hash(hash_blocks, image_mode=image_mode)
                if body_hash != existing_hash:
                    blocks_for_sync = prepare_body_blocks_for_sync(
                        notion_token, body_blocks
                    )
                    sync_page_body_blocks(
                        notion_token, page_id, blocks_for_sync, sync_mode=sync_mode
                    )
                    body_updated += 1
                    update_page(
                        notion_token,
                        page_id,
                        {
                            BODY_HASH_PROPERTY: {
                                "rich_text": [
                                    {"type": "text", "text": {"content": body_hash}}
                                ]
                            }
                        },
                    )
                else:
                    LOGGER.info("본문 변경 없음: %s", label)
            else:
                blocks_for_sync = prepare_body_blocks_for_sync(
                    notion_token, body_blocks
                )
                sync_page_body_blocks(
                    notion_token, page_id, blocks_for_sync, sync_mode=sync_mode
                )
                body_updated += 1

    LOGGER.info("기존 TOP 정리 시작")
    disabled = disable_missing_top(notion_token, database_id, current_top_urls, current_top_dates)
    LOGGER.info("TOP 해제 수: %s", disabled)

    LOGGER.info("수집 항목 수: %s", len(items))
    LOGGER.info("생성: %s", created)
    LOGGER.info("업데이트: %s", updated)
    LOGGER.info("본문 변경: %s", body_updated)


if __name__ == "__main__":
    main()
