import csv
import errno
import http.client
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import warnings
import xml.etree.ElementTree as ElementTree
import zipfile
import zlib
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO, StringIO
from typing import Any, Callable, Iterator, Optional, TypeVar, cast
from urllib.parse import quote, urlencode, urlsplit

from log import LOGGER, summarize_url_for_log
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
    NOTICE_ID_PROPERTY,
    PAGE_ICON_EMOJI,
    SOURCE_KEY_PROPERTY,
    SYNC_GENERATION_PROPERTY,
    SYNC_OPERATION_PROPERTY,
    SYNC_OWNER_PROPERTY,
    SYNC_STATUS_PROPERTY,
    TITLE_PROPERTY,
    TOP_PROPERTY,
    TYPE_TAGS,
    TYPE_PROPERTY,
    URL_PROPERTY,
    VIEWS_PROPERTY,
    get_notion_data_source_id,
    get_notion_api_version,
    should_allow_notion_schema_migration,
    should_upload_files_to_notion,
)
from utils import (
    build_file_block,
    build_pdf_block,
    build_site_headers,
    build_uploaded_file_hash_block,
    build_uploaded_image_hash_block,
    compute_content_sha256,
    derive_filename_from_url,
    extract_attachment_name,
    is_allowed_external_download_url,
    is_embed_file_candidate,
    is_image_name_or_url,
    is_pdf_name_or_url,
    is_safe_external_download_target,
    normalize_attachment_identity_url,
    normalize_content_sha256,
    normalize_content_type,
    resolve_public_network_address_info,
    sanitize_filename,
)

FILE_UPLOAD_CACHE: dict[tuple[str, str], str] = {}
WORKSPACE_UPLOAD_LIMIT: Optional[int] = None
NOTION_MIN_REQUEST_INTERVAL_SECONDS = 0.35
NOTION_MAX_RETRIES = 5
NOTION_RATE_LIMIT_BASE_DELAY_SECONDS = 3.0
NOTION_TRANSIENT_BASE_DELAY_SECONDS = 1.0
NEXT_NOTION_REQUEST_AT = 0.0
EXTERNAL_FETCH_MAX_RETRIES = 3
EXTERNAL_UPLOAD_MAX_RETRIES = 3
EXTERNAL_DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024
EXTERNAL_DOWNLOAD_READ_CHUNK_BYTES = 64 * 1024
EXTERNAL_DOWNLOAD_MAX_REQUESTS = 300
EXTERNAL_DOWNLOAD_MAX_SECONDS = 600.0
EXTERNAL_DOWNLOAD_MIN_REQUEST_INTERVAL_SECONDS = 1.0
EXTERNAL_DOWNLOAD_MAX_CONNECT_ADDRESSES = 8
EXTERNAL_PREFLIGHT_CACHE_MAX_BYTES = 128 * 1024 * 1024
IMAGE_MAX_PIXELS = 40_000_000
IMAGE_MAX_DIMENSION = 16_384
ZIP_MAX_ENTRIES = 10_000
ZIP_MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
ZIP_MAX_COMPRESSION_RATIO = 200
NOTION_MAX_REQUEST_BYTES = 500 * 1024
NOTION_MAX_ARRAY_ITEMS = 100
NOTION_MAX_URL_LENGTH = 2000
NOTION_DATA_SOURCE_ID_CACHE: dict[tuple[str, str], str] = {}
NOTION_DATABASE_OBJECT_NOT_FOUND_MAX_ATTEMPTS = 3
DATABASE_OBJECT_NOT_FOUND_BASE_DELAY_SECONDS = 1.0

_T = TypeVar("_T")
JsonObject = dict[str, Any]
SocketAddress = tuple[Any, ...]

UPLOAD_FORMAT_CONTENT_TYPES = {
    "jpeg": frozenset({"image/jpeg", "image/jpg", "image/pjpeg"}),
    "png": frozenset({"image/png"}),
    "gif": frozenset({"image/gif"}),
    "bmp": frozenset({"image/bmp", "image/x-ms-bmp"}),
    "webp": frozenset({"image/webp"}),
    "pdf": frozenset({"application/pdf"}),
    "doc": frozenset({"application/msword"}),
    "docx": frozenset(
        {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
    ),
    "xls": frozenset({"application/vnd.ms-excel"}),
    "xlsx": frozenset(
        {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
    ),
    "ppt": frozenset({"application/vnd.ms-powerpoint"}),
    "pptx": frozenset(
        {
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }
    ),
    "hwp": frozenset(
        {
            "application/vnd.hancom.hwp",
            "application/x-hwp",
            "application/haansofthwp",
        }
    ),
    "hwpx": frozenset(
        {
            "application/vnd.hancom.hwpx",
            "application/hwp+zip",
        }
    ),
    "zip": frozenset({"application/zip", "application/x-zip-compressed"}),
    "rar": frozenset(
        {
            "application/vnd.rar",
            "application/x-rar",
            "application/x-rar-compressed",
        }
    ),
    "7z": frozenset({"application/x-7z-compressed"}),
    "txt": frozenset({"text/plain"}),
    "csv": frozenset({"text/csv", "application/csv"}),
}
UPLOAD_EXTENSION_FORMATS = {
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".png": "png",
    ".gif": "gif",
    ".bmp": "bmp",
    ".webp": "webp",
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "docx",
    ".xls": "xls",
    ".xlsx": "xlsx",
    ".ppt": "ppt",
    ".pptx": "pptx",
    ".hwp": "hwp",
    ".hwpx": "hwpx",
    ".zip": "zip",
    ".rar": "rar",
    ".7z": "7z",
    ".txt": "txt",
    ".csv": "csv",
}
UPLOAD_FORMAT_CANONICAL_CONTENT_TYPES = {
    name: next(iter(content_types))
    for name, content_types in UPLOAD_FORMAT_CONTENT_TYPES.items()
}
UPLOAD_FORMAT_CANONICAL_CONTENT_TYPES.update(
    {
        "jpeg": "image/jpeg",
        "bmp": "image/bmp",
        "docx": (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        "xlsx": (
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        "pptx": (
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation"
        ),
        "hwp": "application/vnd.hancom.hwp",
        "hwpx": "application/vnd.hancom.hwpx",
        "zip": "application/zip",
        "rar": "application/vnd.rar",
        "txt": "text/plain",
        "csv": "text/csv",
    }
)
UPLOAD_FORMAT_CANONICAL_EXTENSIONS = {
    "jpeg": ".jpg",
    "png": ".png",
    "gif": ".gif",
    "bmp": ".bmp",
    "webp": ".webp",
    "pdf": ".pdf",
    "doc": ".doc",
    "docx": ".docx",
    "xls": ".xls",
    "xlsx": ".xlsx",
    "ppt": ".ppt",
    "pptx": ".pptx",
    "hwp": ".hwp",
    "hwpx": ".hwpx",
    "zip": ".zip",
    "rar": ".rar",
    "7z": ".7z",
    "txt": ".txt",
    "csv": ".csv",
}
GENERIC_UPLOAD_CONTENT_TYPES = frozenset(
    {
        "application/octet-stream",
        "binary/octet-stream",
        "application/download",
        "application/force-download",
    }
)
IMAGE_UPLOAD_FORMATS = frozenset({"jpeg", "png", "gif", "bmp", "webp"})


class NotionRequestError(RuntimeError):
    failure_origin = "notion"

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        reason: Optional[str] = None,
        method: Optional[str] = None,
        target: Optional[str] = None,
        notion_code: Optional[str] = None,
        request_id: Optional[str] = None,
        hint: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason
        self.method = method
        self.target = target
        self.notion_code = notion_code
        self.request_id = request_id
        self.hint = hint


class NotionPayloadError(ValueError):
    failure_origin = "notion"
    failure_kind = "contract"


class NotionDataSourceResolutionError(RuntimeError):
    failure_origin = "notion"
    failure_kind = "contract"


class NotionSchemaMigrationRequired(RuntimeError):
    failure_origin = "notion"
    failure_kind = "contract"


class NoNotionRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def build_notion_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(NoNotionRedirectHandler())


def open_notion_request(
    request: urllib.request.Request,
    timeout: float,
) -> Any:
    return build_notion_opener().open(request, timeout=timeout)


def is_database_object_not_found_error(exc: NotionRequestError) -> bool:
    return exc.status_code == 404 and exc.notion_code == "object_not_found"


def run_database_request_with_object_not_found_retry(
    request_fn: Callable[[], _T],
    *,
    method: str,
    database_id: str,
    action_name: str,
) -> _T:
    total_attempts = NOTION_DATABASE_OBJECT_NOT_FOUND_MAX_ATTEMPTS
    last_exc: Optional[NotionRequestError] = None
    for attempt in range(total_attempts):
        check_run_control()
        try:
            return request_fn()
        except NotionRequestError as exc:
            if not is_database_object_not_found_error(exc):
                raise
            last_exc = exc
            if attempt + 1 >= total_attempts:
                raise
            sleep_s = get_database_object_not_found_retry_sleep_seconds(attempt)
            LOGGER.warning(
                "Notion 데이터베이스 재확인: 동작=%s, 방식=%s, 다음 시도=%s/%s, 대기=%.1fs",
                action_name,
                method,
                attempt + 2,
                total_attempts,
                sleep_s,
            )
            sleep_with_run_control(sleep_s)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Notion 데이터베이스 재확인 로직이 비정상 종료되었습니다")


def summarize_request_target(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path or "/"
    return re.sub(
        r"(?i)(?<![0-9a-f])(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})(?![0-9a-f])",
        "[ID]",
        path,
    )


def truncate_error_text(text: str, limit: int = 240) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3]}..."


def parse_notion_error_payload(body_text: str) -> JsonObject:
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def build_notion_error_hint(
    status_code: Optional[int],
    notion_code: Optional[str],
) -> str:
    if status_code == 401:
        return "토큰 값과 만료 여부를 확인"
    if status_code == 403:
        return "연동 권한과 데이터베이스 공유 상태를 확인"
    if status_code == 404 and notion_code == "object_not_found":
        return "대상 ID, 연동 공유 상태, 토큰이 연결된 워크스페이스를 확인"
    if status_code == 400:
        return "속성 이름, 속성 타입, 요청 데이터를 확인"
    if status_code == 409:
        return "동시 수정 충돌 가능성이 있어 잠시 후 재시도"
    if status_code == 429:
        return "요청량 제한이 걸려 재시도가 필요"
    return ""


def format_notion_error_message(
    method: str,
    target: str,
    status_code: Optional[int],
    notion_code: Optional[str],
    reason: str,
    request_id: Optional[str],
    hint: Optional[str],
) -> str:
    parts = [f"Notion API 요청 실패: {method} {target}"]
    if status_code is not None:
        parts.append(f"HTTP {status_code}")
    if notion_code:
        parts.append(notion_code)
    if reason:
        parts.append(f"원인={truncate_error_text(reason)}")
    if hint:
        parts.append(f"확인={hint}")
    return " | ".join(parts)


def parse_retry_after_seconds(raw_value: Optional[str]) -> Optional[float]:
    if raw_value is None:
        return None
    normalized = str(raw_value).strip()
    try:
        seconds = float(normalized)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(normalized)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (
            retry_at.astimezone(timezone.utc)
            - datetime.now(timezone.utc)
        ).total_seconds()
    return min(604800.0, max(0.0, seconds))


# 요청 시작 시점을 일정 간격으로 벌려서 연속 조회와 본문 동기화가 한꺼번에 몰리지 않게 한다.
def wait_for_notion_request_slot() -> None:
    global NEXT_NOTION_REQUEST_AT
    now = time.monotonic()
    sleep_s = NEXT_NOTION_REQUEST_AT - now
    if sleep_s > 0:
        sleep_with_run_control(sleep_s)
        now = time.monotonic()
    NEXT_NOTION_REQUEST_AT = now + NOTION_MIN_REQUEST_INTERVAL_SECONDS


def get_retry_sleep_seconds(
    attempt: int,
    status_code: Optional[int] = None,
    retry_after: Optional[str] = None,
) -> float:
    if status_code == 429:
        header_delay = parse_retry_after_seconds(retry_after) or 0.0
        backoff_delay = min(
            NOTION_RATE_LIMIT_BASE_DELAY_SECONDS * (2**attempt),
            30.0,
        )
        return cast(float, min(max(header_delay, backoff_delay), 60.0))
    return cast(
        float,
        min(NOTION_TRANSIENT_BASE_DELAY_SECONDS * (2**attempt), 8.0),
    )


def get_database_object_not_found_retry_sleep_seconds(attempt: int) -> float:
    return cast(
        float,
        min(
            DATABASE_OBJECT_NOT_FOUND_BASE_DELAY_SECONDS * (2**attempt),
            4.0,
        ),
    )


def is_notion_api_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == "api.notion.com"
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and not parsed.fragment
    )


def is_notion_file_upload_send_url(
    url: str,
    upload_id: str = "",
) -> bool:
    if not is_notion_api_url(url):
        return False
    parsed = urlsplit(url)
    match = re.fullmatch(
        r"/v1/file_uploads/([A-Za-z0-9_-]{1,128})/send",
        parsed.path,
    )
    if (
        match is None
        or parsed.query
        or parsed.fragment
    ):
        return False
    return not upload_id or match.group(1) == upload_id


def summarize_external_request_target(url: str) -> str:
    summarized: str = summarize_url_for_log(url)
    return summarized


def get_response_url(response: Any) -> str:
    geturl = getattr(response, "geturl", None)
    if not callable(geturl):
        return ""
    try:
        value = geturl()
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def is_retryable_http_status(status_code: int) -> bool:
    return status_code in {429, 500, 502, 503, 504}


def get_external_retry_sleep_seconds(
    attempt: int,
    retry_after: Optional[str] = None,
) -> float:
    header_delay = parse_retry_after_seconds(retry_after) or 0.0
    backoff_delay = min(1.0 * (2**attempt), 8.0)
    return cast(float, min(max(header_delay, backoff_delay), 60.0))


class UnsafeExternalDownloadError(ValueError):
    pass


class ExternalDownloadRunStoppedError(RuntimeError):
    pass


class ExternalDownloadRunPolicy:
    def __init__(self) -> None:
        self.max_requests = self._integer_env(
            "EXTERNAL_DOWNLOAD_MAX_REQUESTS",
            EXTERNAL_DOWNLOAD_MAX_REQUESTS,
            1,
            5000,
        )
        self.max_seconds = self._float_env(
            "EXTERNAL_DOWNLOAD_MAX_SECONDS",
            EXTERNAL_DOWNLOAD_MAX_SECONDS,
            1.0,
            1200.0,
        )
        self.min_interval_seconds = self._float_env(
            "EXTERNAL_DOWNLOAD_MIN_REQUEST_INTERVAL_SECONDS",
            self._float_env(
                "SITE_MIN_REQUEST_INTERVAL_SECONDS",
                EXTERNAL_DOWNLOAD_MIN_REQUEST_INTERVAL_SECONDS,
                0.1,
                60.0,
            ),
            0.1,
            60.0,
        )
        self.actual_requests = 0
        self.stopped_reason = ""
        self.status_code: Optional[int] = None
        self.retry_after: Optional[str] = None
        self.retry_after_seconds: Optional[float] = None
        self.next_request_at_by_host: dict[str, float] = {}
        self.active_seconds = 0.0
        self.active_started_at: Optional[float] = None
        self.active_depth = 0
        self._lock = threading.Lock()

    @staticmethod
    def _integer_env(
        name: str,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        raw = os.environ.get(name, str(default)).strip()
        try:
            value = int(raw)
        except ValueError:
            return default
        return min(maximum, max(minimum, value))

    @staticmethod
    def _float_env(
        name: str,
        default: float,
        minimum: float,
        maximum: float,
    ) -> float:
        raw = os.environ.get(name, str(default)).strip()
        try:
            value = float(raw)
        except ValueError:
            return default
        return min(maximum, max(minimum, value))

    def _elapsed_seconds(self, now: Optional[float] = None) -> float:
        current = time.monotonic() if now is None else now
        elapsed = self.active_seconds
        if self.active_started_at is not None:
            elapsed += max(0.0, current - self.active_started_at)
        return max(0.0, elapsed)

    @contextmanager
    def activity(self) -> Iterator[None]:
        with self._lock:
            if self.active_depth == 0:
                self.active_started_at = time.monotonic()
            self.active_depth += 1
        try:
            yield
        finally:
            with self._lock:
                self.active_depth = max(0, self.active_depth - 1)
                if self.active_depth == 0:
                    now = time.monotonic()
                    if self.active_started_at is not None:
                        self.active_seconds += max(
                            0.0,
                            now - self.active_started_at,
                        )
                    self.active_started_at = None

    def _has_time_locked(self, now: float) -> bool:
        if self._elapsed_seconds(now) < self.max_seconds:
            return True
        if not self.stopped_reason:
            self.stopped_reason = "time_cap"
        return False

    def can_continue(self) -> bool:
        with self._lock:
            if self.stopped_reason:
                return False
            return self._has_time_locked(time.monotonic())

    def has_time_remaining(self) -> bool:
        with self._lock:
            return self._has_time_locked(time.monotonic())

    def remaining_seconds(self) -> float:
        with self._lock:
            return max(
                0.0,
                self.max_seconds - self._elapsed_seconds(),
            )

    def wait_for_retry(self, seconds: float) -> bool:
        delay = max(0.0, seconds)
        with self._lock:
            now = time.monotonic()
            if self.stopped_reason or not self._has_time_locked(now):
                return False
            remaining = self.max_seconds - self._elapsed_seconds(now)
            if delay >= remaining:
                self.stopped_reason = "time_cap"
                return False
        sleep_with_run_control(delay)
        return self.can_continue()

    def reserve_request(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            return False
        while True:
            check_run_control()
            with self._lock:
                now = time.monotonic()
                if self.stopped_reason or not self._has_time_locked(now):
                    return False
                if self.actual_requests >= self.max_requests:
                    self.stopped_reason = "request_cap"
                    return False
                ready_at = self.next_request_at_by_host.get(host, now)
                delay = max(0.0, ready_at - now)
                if delay <= 0:
                    self.actual_requests += 1
                    self.next_request_at_by_host[host] = (
                        now + self.min_interval_seconds
                    )
                    return True
                remaining = self.max_seconds - self._elapsed_seconds(now)
                if delay >= remaining:
                    self.stopped_reason = "time_cap"
                    return False
            sleep_with_run_control(delay)

    def open_circuit(
        self,
        status_code: int,
        retry_after: Optional[str],
    ) -> None:
        with self._lock:
            if self.status_code is not None:
                return
            normalized_retry_after = (
                str(retry_after).strip() if retry_after is not None else ""
            )
            self.status_code = status_code
            self.retry_after = normalized_retry_after or None
            self.retry_after_seconds = parse_retry_after_seconds(
                self.retry_after
            )
            self.stopped_reason = f"http_{status_code}"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if not self.stopped_reason:
                self._has_time_locked(time.monotonic())
            if (
                not self.stopped_reason
                and self.actual_requests >= self.max_requests
            ):
                self.stopped_reason = "request_cap"
            return {
                "requests": self.actual_requests,
                "stopped_reason": self.stopped_reason,
                "status_code": self.status_code,
                "retry_after": self.retry_after,
                "retry_after_seconds": self.retry_after_seconds,
                "elapsed_seconds": self._elapsed_seconds(),
            }


class ExternalPreflightDownloadCache:
    def __init__(
        self,
        max_bytes: int = EXTERNAL_PREFLIGHT_CACHE_MAX_BYTES,
    ) -> None:
        self.max_bytes = max_bytes
        self.total_bytes = 0
        self.files: dict[
            tuple[str, bool],
            list[tuple[bytes, Optional[str]]],
        ] = {}

    def add(
        self,
        url: str,
        require_file_hint: bool,
        downloaded_file: tuple[bytes, Optional[str]],
    ) -> None:
        next_total = self.total_bytes + len(downloaded_file[0])
        if next_total > self.max_bytes:
            raise RuntimeError(
                "외부 파일 사전검증 캐시 용량을 초과했습니다"
            )
        self.files.setdefault(
            (str(url or "").strip(), require_file_hint),
            [],
        ).append(downloaded_file)
        self.total_bytes = next_total

    def pop(
        self,
        url: str,
        require_file_hint: bool,
    ) -> Optional[tuple[bytes, Optional[str]]]:
        key = (str(url or "").strip(), require_file_hint)
        candidates = self.files.get(key) or []
        if not candidates:
            return None
        downloaded_file = candidates.pop(0)
        self.total_bytes -= len(downloaded_file[0])
        if not candidates:
            self.files.pop(key, None)
        return downloaded_file


_EXTERNAL_DOWNLOAD_RUN_POLICY: ContextVar[
    Optional[ExternalDownloadRunPolicy]
] = ContextVar(
    "external_download_run_policy",
    default=None,
)
_EXTERNAL_PREFLIGHT_DOWNLOADS: ContextVar[
    Optional[ExternalPreflightDownloadCache]
] = ContextVar(
    "external_preflight_downloads",
    default=None,
)


@contextmanager
def external_download_run_scope(
    force_new: bool = False,
) -> Iterator[ExternalDownloadRunPolicy]:
    current = _EXTERNAL_DOWNLOAD_RUN_POLICY.get()
    if current is not None and not force_new:
        yield current
        return
    policy = ExternalDownloadRunPolicy()
    policy_token = _EXTERNAL_DOWNLOAD_RUN_POLICY.set(policy)
    downloads_token = _EXTERNAL_PREFLIGHT_DOWNLOADS.set(
        ExternalPreflightDownloadCache(
            EXTERNAL_PREFLIGHT_CACHE_MAX_BYTES
        )
    )
    try:
        yield policy
    finally:
        _EXTERNAL_PREFLIGHT_DOWNLOADS.reset(downloads_token)
        _EXTERNAL_DOWNLOAD_RUN_POLICY.reset(policy_token)


def current_external_download_run_policy() -> Optional[ExternalDownloadRunPolicy]:
    return _EXTERNAL_DOWNLOAD_RUN_POLICY.get()


def cache_external_preflight_download(
    url: str,
    require_file_hint: bool,
    downloaded_file: tuple[bytes, Optional[str]],
) -> None:
    cache = _EXTERNAL_PREFLIGHT_DOWNLOADS.get()
    if cache is None:
        return
    cache.add(url, require_file_hint, downloaded_file)


def pop_external_preflight_download(
    url: str,
    require_file_hint: bool,
) -> Optional[tuple[bytes, Optional[str]]]:
    cache = _EXTERNAL_PREFLIGHT_DOWNLOADS.get()
    if not cache:
        return None
    return cache.pop(url, require_file_hint)


class ValidatedExternalRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(
        self,
        before_redirect: Optional[Callable[[str], bool]] = None,
    ) -> None:
        super().__init__()
        self.before_redirect = before_redirect

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Optional[urllib.request.Request]:
        policy = current_external_download_run_policy()
        if policy is not None and not policy.can_continue():
            fp.close()
            raise ExternalDownloadRunStoppedError(
                policy.stopped_reason or "external_download_stopped"
            )
        if not is_safe_external_download_target(newurl):
            fp.close()
            raise UnsafeExternalDownloadError(newurl)
        redirected = super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            newurl,
        )
        if (
            redirected is not None
            and policy is not None
            and not policy.reserve_request(newurl)
        ):
            fp.close()
            raise ExternalDownloadRunStoppedError(
                policy.stopped_reason or "external_download_stopped"
            )
        if (
            redirected is not None
            and self.before_redirect is not None
            and not self.before_redirect(newurl)
        ):
            fp.close()
            raise ExternalDownloadRunStoppedError(
                "external_redirect_stopped"
            )
        return redirected


def create_public_network_socket(
    hostname: str,
    port: int,
    timeout: object,
    source_address: Optional[SocketAddress] = None,
) -> socket.socket:
    started_at = time.monotonic()
    address_info = resolve_public_network_address_info(hostname, port)
    if not address_info:
        raise OSError(f"외부 다운로드 대상이 차단되었습니다: {hostname}")
    unique_address_info: list[tuple[Any, ...]] = []
    seen_addresses: set[tuple[Any, ...]] = set()
    for entry in address_info:
        family, socktype, proto, _, socket_address = entry
        key = (family, socktype, proto, socket_address)
        if key in seen_addresses:
            continue
        seen_addresses.add(key)
        unique_address_info.append(entry)
        if (
            len(unique_address_info)
            >= EXTERNAL_DOWNLOAD_MAX_CONNECT_ADDRESSES
        ):
            break
    default_timeout = getattr(socket, "_GLOBAL_DEFAULT_TIMEOUT")
    explicit_timeout: Optional[float] = None
    if timeout is not default_timeout and timeout is not None:
        explicit_timeout = max(0.0, float(cast(float, timeout)))
    deadline = (
        started_at + explicit_timeout
        if explicit_timeout is not None
        else None
    )
    policy = current_external_download_run_policy()
    last_error: Optional[OSError] = None
    for family, socktype, proto, _, socket_address in unique_address_info:
        check_run_control()
        if policy is not None and not policy.can_continue():
            raise external_download_stopped_error(policy)
        remaining: Optional[float] = None
        if deadline is not None:
            remaining = deadline - time.monotonic()
        if policy is not None:
            policy_remaining = policy.remaining_seconds()
            remaining = (
                policy_remaining
                if remaining is None
                else min(remaining, policy_remaining)
            )
        if remaining is not None and remaining <= 0:
            if policy is not None and not policy.can_continue():
                raise external_download_stopped_error(policy)
            raise socket.timeout("외부 연결 시간 한도를 초과했습니다")
        connection = None
        try:
            connection = socket.socket(family, socktype, proto)
            if remaining is not None:
                connection.settimeout(remaining)
            elif timeout is not default_timeout:
                connection.settimeout(cast(Optional[float], timeout))
            if source_address:
                connection.bind(source_address)
            connection.connect(socket_address)
            return connection
        except OSError as exc:
            last_error = exc
            if connection is not None:
                connection.close()
    if last_error is not None:
        raise last_error
    raise OSError(f"사용할 수 있는 외부 다운로드 주소가 없습니다: {hostname}")


class ValidatedExternalHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        sys.audit("http.client.connect", self, self.host, self.port)
        source_address = cast(
            Optional[SocketAddress],
            getattr(self, "source_address", None),
        )
        self.sock = create_public_network_socket(
            self.host,
            self.port,
            self.timeout,
            source_address,
        )
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            if exc.errno != errno.ENOPROTOOPT:
                raise
        tunnel_host = cast(
            Optional[str],
            getattr(self, "_tunnel_host", None),
        )
        if tunnel_host:
            tunnel = cast(Callable[[], None], getattr(self, "_tunnel"))
            tunnel()
        server_hostname = tunnel_host or self.host
        context = getattr(self, "_context")
        self.sock = context.wrap_socket(
            self.sock,
            server_hostname=server_hostname,
        )


class ValidatedExternalHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(
            ValidatedExternalHTTPSConnection,
            req,
            context=getattr(self, "_context"),
        )


def build_external_download_opener(
    before_redirect: Optional[Callable[[str], bool]] = None,
) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        ValidatedExternalRedirectHandler(before_redirect),
        ValidatedExternalHTTPSHandler(),
    )


def read_external_response_bytes(
    response: Any,
    max_bytes: int,
    time_check: Optional[Callable[[], bool]] = None,
) -> Optional[bytes]:
    if max_bytes <= 0:
        return None
    raw_content_length = response.headers.get("Content-Length")
    if raw_content_length is not None:
        try:
            declared_size = int(raw_content_length)
        except (TypeError, ValueError):
            return None
        if declared_size < 0 or declared_size > max_bytes:
            return None
    chunks: list[bytes] = []
    total_size = 0
    while True:
        if time_check is not None and not time_check():
            return None
        remaining_with_sentinel = max_bytes - total_size + 1
        chunk = response.read(
            min(EXTERNAL_DOWNLOAD_READ_CHUNK_BYTES, remaining_with_sentinel)
        )
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def external_download_stopped_error(
    policy: ExternalDownloadRunPolicy,
) -> ExternalDownloadRunStoppedError:
    snapshot = policy.snapshot()
    return ExternalDownloadRunStoppedError(
        "외부 파일 다운로드 실행 안전 한도에 도달했습니다: "
        f"중단 사유={snapshot['stopped_reason'] or 'unknown'}, "
        f"요청={snapshot['requests']}, "
        f"활성 시간={float(snapshot['elapsed_seconds']):.1f}초"
    )


def raise_if_external_download_stopped() -> None:
    policy = current_external_download_run_policy()
    if policy is None:
        return
    snapshot = policy.snapshot()
    if snapshot["stopped_reason"]:
        raise external_download_stopped_error(policy)


def download_file_bytes(
    url: str,
    require_file_hint: bool = False,
    max_bytes: int = EXTERNAL_DOWNLOAD_MAX_BYTES,
) -> tuple[Optional[bytes], Optional[str]]:
    policy = current_external_download_run_policy()
    if policy is None:
        with external_download_run_scope():
            return download_file_bytes(
                url,
                require_file_hint=require_file_hint,
                max_bytes=max_bytes,
            )
    with policy.activity():
        return _download_file_bytes_with_policy(
            policy,
            url,
            require_file_hint=require_file_hint,
            max_bytes=max_bytes,
        )


def _download_file_bytes_with_policy(
    policy: ExternalDownloadRunPolicy,
    url: str,
    require_file_hint: bool = False,
    max_bytes: int = EXTERNAL_DOWNLOAD_MAX_BYTES,
) -> tuple[Optional[bytes], Optional[str]]:
    request_target = summarize_external_request_target(url)
    if not is_allowed_external_download_url(url, require_file_hint=require_file_hint):
        LOGGER.warning("외부 파일 다운로드 차단: %s", request_target)
        return None, None
    for attempt in range(EXTERNAL_FETCH_MAX_RETRIES + 1):
        check_run_control()
        if not policy.can_continue():
            return None, None
        if not is_safe_external_download_target(
            url,
            require_file_hint=require_file_hint,
        ):
            LOGGER.warning("외부 파일 다운로드 대상 차단: %s", request_target)
            return None, None
        if not policy.reserve_request(url):
            return None, None
        req = urllib.request.Request(url, headers=build_site_headers())
        try:
            opener = build_external_download_opener()
            timeout_seconds = min(30.0, policy.remaining_seconds())
            if timeout_seconds <= 0:
                return None, None
            with opener.open(req, timeout=timeout_seconds) as resp:
                content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip()
                data = read_external_response_bytes(
                    resp,
                    max_bytes,
                    time_check=policy.has_time_remaining,
                )
                if data is None:
                    snapshot = policy.snapshot()
                    if snapshot["stopped_reason"] == "time_cap":
                        LOGGER.warning(
                            "외부 파일 다운로드 활성 시간 차단: %s "
                            "(limit=%ss, requests=%s)",
                            request_target,
                            policy.max_seconds,
                            snapshot["requests"],
                        )
                    else:
                        LOGGER.warning(
                            "외부 파일 다운로드 용량 차단: %s (limit=%s)",
                            request_target,
                            max_bytes,
                        )
                    return None, None
                return data, content_type or None
        except UnsafeExternalDownloadError:
            LOGGER.warning("외부 파일 리다이렉트 차단: %s", request_target)
            return None, None
        except ExternalDownloadRunStoppedError:
            return None, None
        except urllib.error.HTTPError as exc:
            retry_after = (
                exc.headers.get("Retry-After")
                if exc.headers is not None
                else None
            )
            exc.close()
            if exc.code in {403, 429}:
                policy.open_circuit(exc.code, retry_after)
                LOGGER.warning(
                    "외부 파일 다운로드 회로 차단: %s (HTTP %s, Retry-After=%s)",
                    request_target,
                    exc.code,
                    policy.retry_after or "-",
                )
                return None, None
            if is_retryable_http_status(exc.code) and attempt < EXTERNAL_FETCH_MAX_RETRIES:
                sleep_s = get_external_retry_sleep_seconds(
                    attempt,
                    retry_after=retry_after,
                )
                LOGGER.info(
                    "외부 파일 다운로드 재시도(%s/%s): %s -> HTTP %s, 대기=%.1fs",
                    attempt + 1,
                    EXTERNAL_FETCH_MAX_RETRIES,
                    request_target,
                    exc.code,
                    sleep_s,
                )
                if not policy.wait_for_retry(sleep_s):
                    return None, None
                continue
            LOGGER.info(
                "파일 다운로드 실패: %s (HTTP %s)",
                request_target,
                exc.code,
            )
        except urllib.error.URLError as exc:
            is_timeout = isinstance(exc.reason, socket.timeout)
            if attempt < EXTERNAL_FETCH_MAX_RETRIES and is_timeout:
                sleep_s = get_external_retry_sleep_seconds(attempt)
                LOGGER.info(
                    "외부 파일 다운로드 재시도(%s/%s): %s -> 타임아웃, 대기=%.1fs",
                    attempt + 1,
                    EXTERNAL_FETCH_MAX_RETRIES,
                    request_target,
                    sleep_s,
                )
                if not policy.wait_for_retry(sleep_s):
                    return None, None
                continue
            if is_timeout:
                LOGGER.info(
                    "파일 다운로드 실패: %s (타임아웃)",
                    request_target,
                )
            else:
                LOGGER.info(
                    "파일 다운로드 실패: %s (%s)",
                    request_target,
                    exc.reason,
                )
        except socket.timeout:
            if attempt < EXTERNAL_FETCH_MAX_RETRIES:
                sleep_s = get_external_retry_sleep_seconds(attempt)
                LOGGER.info(
                    "외부 파일 다운로드 재시도(%s/%s): %s -> 타임아웃, 대기=%.1fs",
                    attempt + 1,
                    EXTERNAL_FETCH_MAX_RETRIES,
                    request_target,
                    sleep_s,
                )
                if not policy.wait_for_retry(sleep_s):
                    return None, None
                continue
            LOGGER.info(
                "파일 다운로드 실패: %s (타임아웃)",
                request_target,
            )
    return None, None


def get_image_validation_limits() -> tuple[int, int]:
    raw_max_pixels = os.environ.get(
        "IMAGE_MAX_PIXELS",
        str(IMAGE_MAX_PIXELS),
    ).strip()
    raw_max_dimension = os.environ.get(
        "IMAGE_MAX_DIMENSION",
        str(IMAGE_MAX_DIMENSION),
    ).strip()
    try:
        max_pixels = min(100_000_000, max(1, int(raw_max_pixels)))
    except ValueError:
        max_pixels = IMAGE_MAX_PIXELS
    try:
        max_dimension = min(32_768, max(1, int(raw_max_dimension)))
    except ValueError:
        max_dimension = IMAGE_MAX_DIMENSION
    return max_pixels, max_dimension


def png_payload_has_exact_boundary(payload: bytes) -> bool:
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    saw_iend = False
    while offset + 12 <= len(payload):
        chunk_size = struct.unpack(">I", payload[offset : offset + 4])[0]
        chunk_type = payload[offset + 4 : offset + 8]
        chunk_end = offset + 12 + chunk_size
        if chunk_end > len(payload):
            return False
        chunk_data = payload[offset + 8 : offset + 8 + chunk_size]
        expected_crc = struct.unpack(
            ">I",
            payload[offset + 8 + chunk_size : chunk_end],
        )[0]
        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            return False
        offset = chunk_end
        if chunk_type == b"IEND":
            if chunk_size != 0 or offset != len(payload):
                return False
            saw_iend = True
            break
    return saw_iend


def image_payload_has_exact_boundary(payload: bytes, image_format: str) -> bool:
    if image_format == "jpeg":
        return payload.startswith(b"\xff\xd8\xff") and payload.endswith(b"\xff\xd9")
    if image_format == "png":
        return png_payload_has_exact_boundary(payload)
    if image_format == "gif":
        return (
            payload.startswith((b"GIF87a", b"GIF89a"))
            and payload.endswith(b"\x3b")
        )
    if image_format == "bmp":
        if len(payload) < 14 or not payload.startswith(b"BM"):
            return False
        return int(struct.unpack("<I", payload[2:6])[0]) == len(payload)
    if image_format == "webp":
        if len(payload) < 12:
            return False
        return (
            payload.startswith(b"RIFF")
            and payload[8:12] == b"WEBP"
            and struct.unpack("<I", payload[4:8])[0] + 8 == len(payload)
        )
    return False


def inspect_image_payload(payload: bytes) -> Optional[str]:
    try:
        from PIL import Image
    except ImportError:
        LOGGER.info("이미지 형식 검증 실패: Pillow 미설치")
        return None
    pillow_formats = {
        "JPEG": "jpeg",
        "PNG": "png",
        "GIF": "gif",
        "BMP": "bmp",
        "WEBP": "webp",
    }
    max_pixels, max_dimension = get_image_validation_limits()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as image:
                image_format = pillow_formats.get(str(image.format or "").upper())
                width, height = image.size
                if (
                    image_format is None
                    or width <= 0
                    or height <= 0
                    or width > max_dimension
                    or height > max_dimension
                    or width * height > max_pixels
                ):
                    return None
                image.verify()
            with Image.open(BytesIO(payload)) as decoded:
                if pillow_formats.get(str(decoded.format or "").upper()) != image_format:
                    return None
                decoded.load()
    except Exception as exc:
        LOGGER.info("이미지 형식 검증 실패: %s", exc)
        return None
    if not image_payload_has_exact_boundary(payload, image_format):
        return None
    return image_format


def payload_looks_like_svg(payload: bytes) -> bool:
    prefix = payload[:8192].lstrip(b"\xef\xbb\xbf\x00\t\n\r ")
    lowered = prefix.lower()
    if lowered.startswith(b"<svg"):
        return True
    if lowered.startswith(b"<?xml"):
        declaration_end = lowered.find(b"?>")
        if declaration_end >= 0:
            lowered = lowered[declaration_end + 2 :].lstrip()
            return lowered.startswith(b"<svg")
    return False


def payload_contains_archive_marker(payload: bytes) -> bool:
    return any(
        marker in payload
        for marker in (
            b"PK\x03\x04",
            b"PK\x05\x06",
            b"Rar!\x1a\x07\x00",
            b"Rar!\x1a\x07\x01\x00",
            b"7z\xbc\xaf\x27\x1c",
        )
    )


def inspect_pdf_payload(payload: bytes) -> bool:
    if not re.match(rb"%PDF-[12]\.[0-9](?:\r\n|\r|\n)", payload[:16]):
        return False
    if payload_contains_archive_marker(payload):
        return False
    if payload.find(b"%PDF-", 1) >= 0:
        return False
    stripped = payload.rstrip(b"\x00\t\n\x0c\r ")
    return stripped.endswith(b"%%EOF")


def read_compound_sector(
    payload: bytes,
    sector_id: int,
    sector_size: int,
) -> Optional[bytes]:
    if sector_id < 0:
        return None
    offset = (sector_id + 1) * sector_size
    end = offset + sector_size
    if offset < sector_size or end > len(payload):
        return None
    return payload[offset:end]


def collect_compound_directory_names(payload: bytes) -> Optional[set[str]]:
    if len(payload) < 1536 or not payload.startswith(
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    ):
        return None
    if payload[28:30] != b"\xfe\xff":
        return None
    major_version = struct.unpack("<H", payload[26:28])[0]
    sector_shift = struct.unpack("<H", payload[30:32])[0]
    if (major_version, sector_shift) not in {(3, 9), (4, 12)}:
        return None
    sector_size = 1 << sector_shift
    if len(payload) % sector_size != 0:
        return None
    max_sector_count = len(payload) // sector_size - 1
    if max_sector_count <= 0:
        return None
    fat_sector_count = struct.unpack("<I", payload[44:48])[0]
    first_directory_sector = struct.unpack("<I", payload[48:52])[0]
    first_difat_sector = struct.unpack("<I", payload[68:72])[0]
    difat_sector_count = struct.unpack("<I", payload[72:76])[0]
    if (
        fat_sector_count == 0
        or fat_sector_count > max_sector_count
        or difat_sector_count > max_sector_count
    ):
        return None
    free_sector = 0xFFFFFFFF
    end_of_chain = 0xFFFFFFFE
    fat_sector = 0xFFFFFFFD
    difat_sector = 0xFFFFFFFC
    fat_sector_ids: list[int] = []
    seen_fat_sector_ids: set[int] = set()

    def add_fat_sector_id(sector_id: int) -> bool:
        if sector_id in {free_sector, end_of_chain}:
            return True
        if (
            sector_id >= max_sector_count
            or sector_id in seen_fat_sector_ids
            or len(fat_sector_ids) >= fat_sector_count
        ):
            return False
        seen_fat_sector_ids.add(sector_id)
        fat_sector_ids.append(sector_id)
        return True

    for sector_id in struct.unpack("<109I", payload[76:512]):
        if not add_fat_sector_id(sector_id):
            return None
    next_difat_sector = first_difat_sector
    seen_difat: set[int] = set()
    for _ in range(difat_sector_count):
        if next_difat_sector in seen_difat:
            return None
        seen_difat.add(next_difat_sector)
        sector = read_compound_sector(payload, next_difat_sector, sector_size)
        if sector is None:
            return None
        values = struct.unpack(f"<{sector_size // 4}I", sector)
        for sector_id in values[:-1]:
            if not add_fat_sector_id(sector_id):
                return None
        next_difat_sector = values[-1]
    if difat_sector_count == 0 and first_difat_sector not in {
        free_sector,
        end_of_chain,
    }:
        return None
    if len(fat_sector_ids) < fat_sector_count:
        return None
    fat_entries: list[int] = []
    for sector_id in fat_sector_ids:
        sector = read_compound_sector(payload, sector_id, sector_size)
        if sector is None:
            return None
        remaining_entries = max_sector_count - len(fat_entries)
        if remaining_entries <= 0:
            break
        fat_entries.extend(
            struct.unpack(f"<{sector_size // 4}I", sector)[
                :remaining_entries
            ]
        )
    for sector_id in fat_sector_ids:
        if sector_id >= len(fat_entries) or fat_entries[sector_id] != fat_sector:
            return None
    directory_bytes = bytearray()
    directory_sector = first_directory_sector
    seen_directory: set[int] = set()
    while directory_sector != end_of_chain:
        if (
            directory_sector in seen_directory
            or directory_sector in {free_sector, fat_sector, difat_sector}
            or directory_sector >= len(fat_entries)
            or len(seen_directory) > max_sector_count
        ):
            return None
        seen_directory.add(directory_sector)
        sector = read_compound_sector(payload, directory_sector, sector_size)
        if sector is None:
            return None
        directory_bytes.extend(sector)
        directory_sector = fat_entries[directory_sector]
    names: set[str] = set()
    saw_root = False
    for offset in range(0, len(directory_bytes), 128):
        entry = directory_bytes[offset : offset + 128]
        if len(entry) < 128:
            return None
        entry_type = entry[66]
        if entry_type == 0:
            continue
        if entry_type not in {1, 2, 5}:
            return None
        name_size = struct.unpack("<H", entry[64:66])[0]
        if name_size < 2 or name_size > 64 or name_size % 2:
            return None
        try:
            name = bytes(entry[: name_size - 2]).decode("utf-16le")
        except UnicodeDecodeError:
            return None
        if not name:
            return None
        names.add(name)
        if entry_type == 5 and name == "Root Entry":
            saw_root = True
    if not saw_root:
        return None
    return names


def inspect_compound_payload(payload: bytes) -> Optional[str]:
    names = collect_compound_directory_names(payload)
    if names is None:
        return None
    detected: list[str] = []
    if "WordDocument" in names:
        detected.append("doc")
    if names.intersection({"Workbook", "Book"}):
        detected.append("xls")
    if "PowerPoint Document" in names:
        detected.append("ppt")
    if {"FileHeader", "BodyText"}.issubset(names):
        detected.append("hwp")
    if len(detected) != 1:
        return None
    return detected[0]


def zip_payload_has_exact_boundary(payload: bytes) -> bool:
    search_start = max(0, len(payload) - 65_557)
    eocd_offset = payload.rfind(b"PK\x05\x06", search_start)
    if eocd_offset < 0 or eocd_offset + 22 > len(payload):
        return False
    (
        disk_number,
        central_disk,
        disk_entries,
        total_entries,
        central_size,
        central_offset,
        comment_size,
    ) = struct.unpack("<4H2IH", payload[eocd_offset + 4 : eocd_offset + 22])
    if disk_number != 0 or central_disk != 0 or disk_entries != total_entries:
        return False
    if total_entries == 0xFFFF or central_size == 0xFFFFFFFF:
        return False
    if central_offset == 0xFFFFFFFF:
        return False
    if eocd_offset + 22 + comment_size != len(payload):
        return False
    if central_offset + central_size != eocd_offset:
        return False
    if total_entries == 0:
        return payload.startswith(b"PK\x05\x06")
    return payload.startswith(b"PK\x03\x04")


def read_validated_zip_member(
    archive: zipfile.ZipFile,
    name: str,
    max_bytes: int = EXTERNAL_DOWNLOAD_MAX_BYTES,
) -> Optional[bytes]:
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.is_dir() or info.file_size <= 0 or info.file_size > max_bytes:
        return None
    try:
        data = archive.read(info)
    except (OSError, RuntimeError, zipfile.BadZipFile):
        return None
    if len(data) != info.file_size:
        return None
    return data


def validated_xml_root_name(payload: bytes) -> Optional[str]:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return None
    return root.tag.rsplit("}", 1)[-1]


def inspect_zip_payload(payload: bytes) -> Optional[str]:
    if not zip_payload_has_exact_boundary(payload):
        return None
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            infos = archive.infolist()
            if len(infos) > ZIP_MAX_ENTRIES:
                return None
            names: set[str] = set()
            total_uncompressed = 0
            for info in infos:
                original_name = info.orig_filename
                normalized_name = original_name.replace("\\", "/")
                path_parts = [part for part in normalized_name.split("/") if part]
                if (
                    "\x00" in original_name
                    or original_name.startswith(("/", "\\"))
                    or any(part == ".." for part in path_parts)
                    or normalized_name in names
                    or info.flag_bits & 0x1
                ):
                    return None
                names.add(normalized_name)
                if info.is_dir():
                    continue
                total_uncompressed += info.file_size
                if total_uncompressed > ZIP_MAX_UNCOMPRESSED_BYTES:
                    return None
                if (
                    info.compress_size == 0
                    and info.file_size > 0
                    or info.compress_size > 0
                    and info.file_size
                    > info.compress_size * ZIP_MAX_COMPRESSION_RATIO
                ):
                    return None
            if archive.testzip() is not None:
                return None
            common_ooxml = {"[Content_Types].xml", "_rels/.rels"}
            ooxml_candidates = {
                "docx": "word/document.xml",
                "xlsx": "xl/workbook.xml",
                "pptx": "ppt/presentation.xml",
            }
            detected: list[str] = []
            if common_ooxml.issubset(names):
                content_types = read_validated_zip_member(
                    archive,
                    "[Content_Types].xml",
                )
                relationships = read_validated_zip_member(
                    archive,
                    "_rels/.rels",
                )
                if (
                    content_types is None
                    or relationships is None
                    or validated_xml_root_name(content_types) != "Types"
                    or validated_xml_root_name(relationships) != "Relationships"
                ):
                    return None
                for format_name, required_name in ooxml_candidates.items():
                    if required_name not in names:
                        continue
                    required_payload = read_validated_zip_member(
                        archive,
                        required_name,
                    )
                    expected_root = {
                        "docx": "document",
                        "xlsx": "workbook",
                        "pptx": "presentation",
                    }[format_name]
                    if (
                        required_payload is None
                        or validated_xml_root_name(required_payload)
                        != expected_root
                    ):
                        return None
                    detected.append(format_name)
            hwpx_sections = {
                name
                for name in names
                if re.fullmatch(
                    r"Contents/section[0-9]+\.(?:xml|xhtml)",
                    name,
                )
            }
            hwpx_required = {
                "mimetype",
                "version.xml",
                "Contents/content.hpf",
                "META-INF/manifest.xml",
            }
            if hwpx_required.issubset(names) and hwpx_sections:
                mimetype_payload = read_validated_zip_member(
                    archive,
                    "mimetype",
                    max_bytes=128,
                )
                version_payload = read_validated_zip_member(
                    archive,
                    "version.xml",
                )
                content_hpf = read_validated_zip_member(
                    archive,
                    "Contents/content.hpf",
                )
                manifest = read_validated_zip_member(
                    archive,
                    "META-INF/manifest.xml",
                )
                if (
                    mimetype_payload is None
                    or mimetype_payload.strip() != b"application/hwp+zip"
                    or version_payload is None
                    or content_hpf is None
                    or manifest is None
                    or validated_xml_root_name(version_payload) is None
                    or validated_xml_root_name(content_hpf) is None
                    or validated_xml_root_name(manifest) is None
                ):
                    return None
                for section_name in sorted(hwpx_sections):
                    section_payload = read_validated_zip_member(
                        archive,
                        section_name,
                    )
                    if (
                        section_payload is None
                        or validated_xml_root_name(section_payload) is None
                    ):
                        return None
                detected.append("hwpx")
            if len(detected) > 1:
                return None
            if detected:
                return detected[0]
            return "zip"
    except (
        EOFError,
        NotImplementedError,
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
    ):
        return None


def read_rar5_vint(
    payload: bytes,
    offset: int,
    end: int,
) -> Optional[tuple[int, int]]:
    value = 0
    shift = 0
    while offset < end and shift <= 63:
        current = payload[offset]
        offset += 1
        value |= (current & 0x7F) << shift
        if current & 0x80 == 0:
            return value, offset
        shift += 7
    return None


def inspect_rar4_payload(payload: bytes) -> bool:
    offset = 7
    saw_main = False
    while offset + 7 <= len(payload):
        header_type = payload[offset + 2]
        flags = struct.unpack("<H", payload[offset + 3 : offset + 5])[0]
        header_size = struct.unpack("<H", payload[offset + 5 : offset + 7])[0]
        if header_size < 7 or offset + header_size > len(payload):
            return False
        expected_crc = struct.unpack("<H", payload[offset : offset + 2])[0]
        actual_crc = zlib.crc32(payload[offset + 2 : offset + header_size]) & 0xFFFF
        if expected_crc != actual_crc:
            return False
        if not saw_main:
            if header_type != 0x73:
                return False
            saw_main = True
        data_size = 0
        if flags & 0x8000:
            if header_size < 11:
                return False
            data_size = struct.unpack("<I", payload[offset + 7 : offset + 11])[0]
        block_end = offset + header_size + data_size
        if block_end > len(payload):
            return False
        if header_type == 0x7B:
            return saw_main and block_end == len(payload)
        offset = block_end
    return False


def inspect_rar5_payload(payload: bytes) -> bool:
    offset = 8
    saw_main = False
    while offset + 6 <= len(payload):
        expected_crc = struct.unpack("<I", payload[offset : offset + 4])[0]
        header_size_result = read_rar5_vint(payload, offset + 4, len(payload))
        if header_size_result is None:
            return False
        header_size, header_start = header_size_result
        header_end = header_start + header_size
        if header_size <= 0 or header_end > len(payload):
            return False
        actual_crc = zlib.crc32(payload[offset + 4 : header_end]) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            return False
        type_result = read_rar5_vint(payload, header_start, header_end)
        if type_result is None:
            return False
        header_type, cursor = type_result
        flags_result = read_rar5_vint(payload, cursor, header_end)
        if flags_result is None:
            return False
        flags, cursor = flags_result
        if not saw_main:
            if header_type != 1:
                return False
            saw_main = True
        if flags & 0x1:
            extra_result = read_rar5_vint(payload, cursor, header_end)
            if extra_result is None:
                return False
            extra_size, cursor = extra_result
            if extra_size > header_end - cursor:
                return False
        data_size = 0
        if flags & 0x2:
            data_result = read_rar5_vint(payload, cursor, header_end)
            if data_result is None:
                return False
            data_size, cursor = data_result
        block_end = header_end + data_size
        if block_end > len(payload):
            return False
        if header_type == 5:
            return saw_main and block_end == len(payload)
        offset = block_end
    return False


def inspect_rar_payload(payload: bytes) -> bool:
    if payload.startswith(b"Rar!\x1a\x07\x00"):
        return inspect_rar4_payload(payload)
    if payload.startswith(b"Rar!\x1a\x07\x01\x00"):
        return inspect_rar5_payload(payload)
    return False


def inspect_7z_payload(payload: bytes) -> bool:
    if len(payload) < 34 or not payload.startswith(b"7z\xbc\xaf\x27\x1c"):
        return False
    if payload[6] != 0:
        return False
    expected_start_crc = struct.unpack("<I", payload[8:12])[0]
    if zlib.crc32(payload[12:32]) & 0xFFFFFFFF != expected_start_crc:
        return False
    next_header_offset = int(struct.unpack("<Q", payload[12:20])[0])
    next_header_size = int(struct.unpack("<Q", payload[20:28])[0])
    next_header_crc = int(struct.unpack("<I", payload[28:32])[0])
    next_header_start = 32 + next_header_offset
    next_header_end = next_header_start + next_header_size
    if (
        next_header_size == 0
        or next_header_start < 32
        or next_header_end != len(payload)
    ):
        return False
    return int(
        zlib.crc32(payload[next_header_start:next_header_end]) & 0xFFFFFFFF
    ) == next_header_crc


def decode_text_payload(payload: bytes) -> Optional[str]:
    if b"\x00" in payload:
        return None
    decoded: Optional[str] = None
    for encoding in ("utf-8-sig", "cp949"):
        try:
            decoded = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if decoded is None:
        return None
    for character in decoded:
        codepoint = ord(character)
        if codepoint < 32 and character not in {"\t", "\n", "\r", "\x0c"}:
            return None
        if 0x7F <= codepoint < 0xA0:
            return None
    return decoded


def inspect_upload_payload(payload: bytes) -> Optional[str]:
    if payload_looks_like_svg(payload):
        return None
    if payload.startswith(
        (
            b"\xff\xd8\xff",
            b"\x89PNG\r\n\x1a\n",
            b"GIF87a",
            b"GIF89a",
            b"BM",
        )
    ) or payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        image_format = inspect_image_payload(payload)
        if image_format is None:
            return None
        if payload_contains_archive_marker(payload):
            return None
        return image_format
    if payload.startswith(b"%PDF-"):
        return "pdf" if inspect_pdf_payload(payload) else None
    if payload.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        if payload_contains_archive_marker(payload) or b"%PDF-" in payload[8:]:
            return None
        return inspect_compound_payload(payload)
    if payload.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return inspect_zip_payload(payload)
    if payload.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")):
        return "rar" if inspect_rar_payload(payload) else None
    if payload.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z" if inspect_7z_payload(payload) else None
    return "txt" if decode_text_payload(payload) is not None else None


def validate_external_upload_payload(
    payload: bytes,
    filename: str,
    content_type: str,
    expect_image: bool,
) -> Optional[tuple[str, str]]:
    extension = os.path.splitext(filename)[1].lower()
    normalized_content_type = content_type.split(";", 1)[0].strip().lower()
    if (
        extension == ".svg"
        or normalized_content_type == "image/svg+xml"
        or payload_looks_like_svg(payload)
    ):
        return None
    expected_format = UPLOAD_EXTENSION_FORMATS.get(extension)
    if extension and expected_format is None:
        return None
    detected_format = inspect_upload_payload(payload)
    if detected_format is None:
        return None
    if detected_format == "txt" and expected_format == "csv":
        try:
            for _ in csv.reader(StringIO(payload.decode("utf-8-sig"))):
                pass
        except (UnicodeDecodeError, csv.Error):
            try:
                for _ in csv.reader(StringIO(payload.decode("cp949"))):
                    pass
            except (UnicodeDecodeError, csv.Error):
                return None
        detected_format = "csv"
    if expected_format is not None and expected_format != detected_format:
        return None
    if expected_format is None:
        if detected_format == "txt" and normalized_content_type in (
            UPLOAD_FORMAT_CONTENT_TYPES["csv"]
        ):
            detected_format = "csv"
        expected_format = detected_format
    if expect_image and expected_format not in IMAGE_UPLOAD_FORMATS:
        return None
    allowed_content_types = UPLOAD_FORMAT_CONTENT_TYPES[expected_format]
    if (
        normalized_content_type not in GENERIC_UPLOAD_CONTENT_TYPES
        and normalized_content_type not in allowed_content_types
    ):
        return None
    return (
        UPLOAD_FORMAT_CANONICAL_CONTENT_TYPES[expected_format],
        UPLOAD_FORMAT_CANONICAL_EXTENSIONS[expected_format],
    )


def validated_external_upload_metadata(
    payload: bytes,
    filename_hint: Optional[str],
    content_type: Optional[str],
    url: str,
    expect_image: bool,
) -> Optional[tuple[str, str]]:
    filename = sanitize_filename(
        filename_hint or derive_filename_from_url(url, fallback="file")
    )
    normalized_content_type = normalize_content_type(
        content_type,
        filename,
        url,
    )
    validated_format = validate_external_upload_payload(
        payload,
        filename,
        normalized_content_type,
        expect_image,
    )
    if validated_format is None:
        return None
    canonical_content_type, canonical_extension = validated_format
    if not os.path.splitext(filename)[1]:
        filename = f"{filename}{canonical_extension}"
    return filename, canonical_content_type


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
        LOGGER.info("이미지 압축 생략: Pillow 미설치")
        return None
    try:
        max_pixels, max_dimension = get_image_validation_limits()
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as image:
                width, height = image.size
                if (
                    width <= 0
                    or height <= 0
                    or width > max_dimension
                    or height > max_dimension
                    or width * height > max_pixels
                ):
                    LOGGER.warning(
                        "이미지 압축 차단: dimensions=%sx%s, max_pixels=%s, max_dimension=%s",
                        width,
                        height,
                        max_pixels,
                        max_dimension,
                    )
                    return None
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
                Image.Resampling.LANCZOS,
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


def validate_notion_payload_value(value: Any, path: str = "$") -> None:
    if isinstance(value, (list, tuple)):
        if len(value) > NOTION_MAX_ARRAY_ITEMS:
            raise NotionPayloadError(
                f"Notion 요청 배열 한도 초과: {path} ({len(value)} > {NOTION_MAX_ARRAY_ITEMS})"
            )
        for index, item in enumerate(value):
            validate_notion_payload_value(item, f"{path}[{index}]")
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        item_path = f"{path}.{key}"
        normalized_key = str(key).lower()
        if (
            isinstance(item, str)
            and (normalized_key == "url" or normalized_key.endswith("_url"))
            and len(item) > NOTION_MAX_URL_LENGTH
        ):
            raise NotionPayloadError(
                f"Notion 요청 URL 길이 한도 초과: {item_path} ({len(item)} > {NOTION_MAX_URL_LENGTH})"
            )
        validate_notion_payload_value(item, item_path)


def encode_notion_payload(
    payload: Optional[JsonObject],
) -> Optional[bytes]:
    if payload is None:
        return None
    validate_notion_payload_value(payload)
    data = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(data) > NOTION_MAX_REQUEST_BYTES:
        raise NotionPayloadError(
            f"Notion 요청 크기 한도 초과: {len(data)} > {NOTION_MAX_REQUEST_BYTES}"
        )
    return data


def is_notion_request_retry_safe(method: str, url: str) -> bool:
    normalized_method = method.upper()
    path = urlsplit(url).path.rstrip("/")
    if normalized_method == "GET":
        return True
    if normalized_method == "POST":
        return path == "/v1/search" or bool(
            re.fullmatch(r"/v1/data_sources/[^/]+/query", path)
        )
    if normalized_method == "PATCH":
        return bool(
            re.fullmatch(r"/v1/(?:pages|blocks|data_sources)/[^/]+", path)
        )
    return False


def should_retry_notion_http_error(method: str, url: str, status_code: int) -> bool:
    if status_code == 429:
        return True
    return is_notion_request_retry_safe(method, url) and status_code in {
        500,
        502,
        503,
        504,
    }


def notion_request(
    method: str,
    url: str,
    token: str,
    payload: Optional[JsonObject] = None,
) -> JsonObject:
    if not is_notion_api_url(url):
        request_target = summarize_request_target(url)
        raise NotionRequestError(
            format_notion_error_message(
                method,
                request_target,
                None,
                None,
                "unsafe_target",
                None,
                "",
            ),
            reason="unsafe_target",
            method=method,
            target=request_target,
        )
    data = encode_notion_payload(payload)
    max_retries = NOTION_MAX_RETRIES
    request_target = summarize_request_target(url)
    retry_safe = is_notion_request_retry_safe(method, url)

    for attempt in range(max_retries + 1):
        check_run_control()
        wait_for_notion_request_slot()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_unredirected_header("Authorization", f"Bearer {token}")
        req.add_header("Notion-Version", get_notion_api_version())
        req.add_header("Content-Type", "application/json")

        try:
            with open_notion_request(req, timeout=30) as resp:
                response_url = get_response_url(resp)
                if not is_notion_api_url(response_url):
                    raise NotionRequestError(
                        format_notion_error_message(
                            method,
                            request_target,
                            None,
                            None,
                            "unsafe_response_target",
                            None,
                            "",
                        ),
                        reason="unsafe_response_target",
                        method=method,
                        target=request_target,
                    )
                return cast(JsonObject, json.load(resp))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            exc.close()
            error_payload = parse_notion_error_payload(body)
            notion_code = str(error_payload.get("code") or "").strip() or None
            notion_message = str(error_payload.get("message") or "").strip()
            reason_text = notion_message or body
            request_id = (
                exc.headers.get("x-request-id")
                or exc.headers.get("X-Request-Id")
                or str(error_payload.get("request_id") or "").strip()
                or None
            )
            hint = build_notion_error_hint(exc.code, notion_code)
            retryable = should_retry_notion_http_error(method, url, exc.code)
            if retryable and attempt < max_retries:
                retry_after = exc.headers.get("Retry-After")
                sleep_s = get_retry_sleep_seconds(
                    attempt,
                    status_code=exc.code,
                    retry_after=retry_after,
                )
                LOGGER.info(
                    "Notion API 재시도(%s/%s): %s %s -> HTTP %s%s, 대기=%.1fs",
                    attempt + 1,
                    max_retries,
                    method,
                    request_target,
                    exc.code,
                    f" {notion_code}" if notion_code else "",
                    sleep_s,
                )
                sleep_with_run_control(sleep_s)
                continue
            raise NotionRequestError(
                format_notion_error_message(
                    method,
                    request_target,
                    exc.code,
                    notion_code,
                    reason_text,
                    request_id,
                    hint,
                ),
                status_code=exc.code,
                reason=reason_text,
                method=method,
                target=request_target,
                notion_code=notion_code,
                request_id=request_id,
                hint=hint,
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            if retry_safe and attempt < max_retries:
                sleep_s = get_retry_sleep_seconds(attempt)
                LOGGER.info(
                    "Notion API 재시도(%s/%s): %s %s -> 타임아웃, 대기=%.1fs",
                    attempt + 1,
                    max_retries,
                    method,
                    request_target,
                    sleep_s,
                )
                sleep_with_run_control(sleep_s)
                continue
            raise NotionRequestError(
                format_notion_error_message(
                    method,
                    request_target,
                    None,
                    None,
                    "timeout",
                    None,
                    "",
                ),
                reason="timeout",
                method=method,
                target=request_target,
            ) from exc
        except urllib.error.URLError as exc:
            is_timeout = isinstance(exc.reason, socket.timeout)
            if retry_safe and attempt < max_retries:
                sleep_s = get_retry_sleep_seconds(attempt)
                LOGGER.info(
                    "Notion API 재시도(%s/%s): %s %s -> %s, 대기=%.1fs",
                    attempt + 1,
                    max_retries,
                    method,
                    request_target,
                    "timeout" if is_timeout else exc.reason,
                    sleep_s,
                )
                sleep_with_run_control(sleep_s)
                continue
            if is_timeout:
                raise NotionRequestError(
                    format_notion_error_message(
                        method,
                        request_target,
                        None,
                        None,
                        "timeout",
                        None,
                        "",
                    ),
                    reason="timeout",
                    method=method,
                    target=request_target,
                ) from exc
            raise NotionRequestError(
                format_notion_error_message(
                    method,
                    request_target,
                    None,
                    None,
                    str(exc.reason),
                    None,
                    "",
                ),
                reason=str(exc.reason),
                method=method,
                target=request_target,
            ) from exc
    raise RuntimeError(f"Notion API 요청 종료 상태 불명: {method} {request_target}")


def create_file_upload(
    token: str,
    filename: str,
    content_type: str,
    mode: str = "single_part",
) -> Optional[JsonObject]:
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
) -> Optional[JsonObject]:
    if not is_notion_file_upload_send_url(upload_url):
        LOGGER.warning(
            "파일 업로드 전송 대상 차단: %s",
            summarize_external_request_target(upload_url),
        )
        return None
    upload_id = urlsplit(upload_url).path.split("/")[-2]
    body, content_header = encode_multipart_form_data(
        filename, content_type, payload, part_number=part_number
    )
    request_target = summarize_external_request_target(upload_url)
    for attempt in range(EXTERNAL_UPLOAD_MAX_RETRIES + 1):
        check_run_control()
        req = urllib.request.Request(upload_url, data=body, method="POST")
        req.add_header("Content-Type", content_header)
        req.add_header("Content-Length", str(len(body)))
        req.add_unredirected_header("Authorization", f"Bearer {token}")
        req.add_header("Notion-Version", get_notion_api_version())
        try:
            with open_notion_request(req, timeout=60) as resp:
                response_url = get_response_url(resp)
                if not is_notion_file_upload_send_url(
                    response_url,
                    upload_id,
                ):
                    LOGGER.warning(
                        "파일 업로드 응답 대상 차단: %s",
                        summarize_external_request_target(response_url),
                    )
                    return None
                return cast(JsonObject, json.load(resp))
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            exc.close()
            if exc.code == 429 and attempt < EXTERNAL_UPLOAD_MAX_RETRIES:
                sleep_s = get_external_retry_sleep_seconds(
                    attempt,
                    retry_after=exc.headers.get("Retry-After"),
                )
                LOGGER.info(
                    "파일 업로드 전송 재시도(%s/%s): %s -> HTTP %s, 대기=%.1fs",
                    attempt + 1,
                    EXTERNAL_UPLOAD_MAX_RETRIES,
                    request_target,
                    exc.code,
                    sleep_s,
                )
                sleep_with_run_control(sleep_s)
                continue
            LOGGER.info("파일 업로드 전송 실패: HTTP %s (%s)", exc.code, body_text)
            return None
        except urllib.error.URLError as exc:
            is_timeout = isinstance(exc.reason, socket.timeout)
            if is_timeout:
                LOGGER.info("파일 업로드 전송 실패: 타임아웃")
            else:
                LOGGER.info("파일 업로드 전송 실패: %s", exc.reason)
            return None
        except socket.timeout:
            LOGGER.info("파일 업로드 전송 실패: 타임아웃")
            return None
    return None


def upload_external_file_to_notion(
    token: str,
    url: str,
    filename_hint: Optional[str] = None,
    expect_image: bool = True,
    downloaded_file: Optional[tuple[bytes, Optional[str]]] = None,
) -> Optional[str]:
    if not url:
        return None
    request_target = summarize_external_request_target(url)
    if downloaded_file is None:
        payload, content_type = download_file_bytes(
            url,
            require_file_hint=not expect_image,
        )
    else:
        payload, content_type = downloaded_file
    if not payload:
        return None
    if len(payload) > EXTERNAL_DOWNLOAD_MAX_BYTES:
        return None
    validated_metadata = validated_external_upload_metadata(
        payload,
        filename_hint,
        content_type,
        url,
        expect_image,
    )
    if validated_metadata is None:
        LOGGER.info(
            "파일 형식 검증 차단: content_type=%s (%s)",
            content_type,
            request_target,
        )
        return None
    filename, content_type = validated_metadata
    content_sha256 = compute_content_sha256(payload)
    cache_key = (
        normalize_attachment_identity_url(url) or url.strip(),
        content_sha256,
    )
    cached = FILE_UPLOAD_CACHE.get(cache_key)
    if cached:
        return cached
    file_size = len(payload)
    max_bytes = get_workspace_upload_limit(token)
    if max_bytes and file_size > max_bytes and expect_image:
        compressed = compress_image_to_limit(payload, content_type, max_bytes)
        if compressed:
            payload, content_type = compressed
            file_size = len(payload)
            stem, _ = os.path.splitext(filename)
            filename = f"{stem}.jpg"
            validated_compression = validate_external_upload_payload(
                payload,
                filename,
                content_type,
                True,
            )
            if validated_compression is None:
                return None
            content_type, _ = validated_compression
    if max_bytes and file_size > max_bytes:
        LOGGER.info("업로드 용량 초과: %s bytes (limit=%s)", file_size, max_bytes)
        return None
    if file_size > 20 * 1024 * 1024:
        LOGGER.info("업로드 생략(멀티파트 필요): %s bytes", file_size)
        return None

    if not filename:
        filename = sanitize_filename(derive_filename_from_url(url, fallback="file"))
    if content_type.lower() == "image/jpeg":
        stem, ext = os.path.splitext(filename)
        if ext.lower() not in {".jpg", ".jpeg"}:
            filename = f"{stem}.jpg"

    created = create_file_upload(token, filename, content_type)
    if not created:
        return None
    upload_id = created.get("id")
    raw_upload_url = created.get("upload_url")
    if isinstance(raw_upload_url, str):
        raw_upload_url = raw_upload_url.strip("`")
    if not isinstance(upload_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}",
        upload_id,
    ):
        upload_target = (
            summarize_external_request_target(str(raw_upload_url))
            if raw_upload_url
            else "-"
        )
        LOGGER.info(
            "파일 업로드 응답 누락: id=%s url=%s",
            upload_id,
            upload_target,
        )
        return None
    upload_url = (
        f"https://api.notion.com/v1/file_uploads/"
        f"{quote(upload_id, safe='')}/send"
    )
    if raw_upload_url and not is_notion_file_upload_send_url(
        raw_upload_url,
        upload_id,
    ):
        LOGGER.warning(
            "파일 업로드 응답 대상 차단: %s",
            summarize_external_request_target(raw_upload_url),
        )
        return None
    sent = send_file_upload(
        token, upload_url, filename, content_type, payload, part_number=None
    )
    if not sent or sent.get("status") != "uploaded":
        LOGGER.info(
            "파일 업로드 상태 이상: %s (%s)",
            request_target,
            sent.get("status") if sent else "no_response",
        )
        return None
    FILE_UPLOAD_CACHE[cache_key] = upload_id
    return upload_id


def extract_file_upload_id_from_block(
    block: Optional[JsonObject],
) -> str:
    if not isinstance(block, dict):
        return ""
    block_type = str(block.get("type") or "").strip()
    if block_type not in {"image", "file", "pdf"}:
        return ""
    payload = block.get(block_type, {})
    if payload.get("type") != "file_upload":
        return ""
    return str(payload.get("file_upload", {}).get("id") or "").strip()


def build_uploaded_media_state_entry(
    media_type: str,
    source_url: str,
    upload_id: str,
    content_sha256: str,
) -> Optional[JsonObject]:
    clean_type = str(media_type or "").strip()
    clean_source_url = normalize_attachment_identity_url(source_url)
    clean_upload_id = str(upload_id or "").strip()
    clean_content_sha256 = normalize_content_sha256(content_sha256)
    if clean_type not in {"image", "file", "pdf"}:
        return None
    if (
        not clean_source_url
        or not clean_upload_id
        or not clean_content_sha256
    ):
        return None
    return {
        "type": clean_type,
        "source_url": clean_source_url,
        "upload_id": clean_upload_id,
        "content_sha256": clean_content_sha256,
    }


def build_uploaded_attachment_state_entry(
    source_url: str,
    name: str,
    upload_id: str,
    content_sha256: str,
) -> Optional[JsonObject]:
    clean_source_url = normalize_attachment_identity_url(source_url)
    clean_name = str(name or "").strip()
    clean_upload_id = str(upload_id or "").strip()
    clean_content_sha256 = normalize_content_sha256(content_sha256)
    if (
        not clean_source_url
        or not clean_upload_id
        or not clean_content_sha256
    ):
        return None
    return {
        "source_url": clean_source_url,
        "name": clean_name,
        "upload_id": clean_upload_id,
        "content_sha256": clean_content_sha256,
    }


def pop_reusable_uploaded_attachment_id(
    reusable_uploaded_attachments: Optional[
        dict[str, list[JsonObject]]
    ],
    source_url: str,
    content_sha256: str,
) -> Optional[str]:
    if not reusable_uploaded_attachments:
        return None
    key = normalize_attachment_identity_url(source_url)
    if not key:
        return None
    candidates = reusable_uploaded_attachments.get(key) or []
    if not candidates:
        return None
    normalized_content_sha256 = normalize_content_sha256(content_sha256)
    match_index = next(
        (
            index
            for index, candidate in enumerate(candidates)
            if normalize_content_sha256(
                candidate.get("content_sha256")
            )
            == normalized_content_sha256
            and normalized_content_sha256
        ),
        None,
    )
    if match_index is None:
        return None
    candidate = candidates.pop(match_index)
    upload_id = str(candidate.get("upload_id") or "").strip()
    if not candidates:
        reusable_uploaded_attachments.pop(key, None)
    return upload_id or None


def collect_attachment_content_state(
    attachments: list[JsonObject],
    *,
    unavailable_source_urls: Optional[list[str]] = None,
) -> list[JsonObject]:
    if not attachments or not should_upload_files_to_notion():
        return []
    state: list[JsonObject] = []
    for attachment in attachments:
        if attachment.get("type") != "external":
            continue
        url = str(
            attachment.get("external", {}).get("url") or ""
        ).strip()
        name = str(
            attachment.get("name")
            or extract_attachment_name(attachment)
        ).strip()
        if (
            not is_image_name_or_url(name, url)
            or not is_allowed_external_download_url(url)
        ):
            continue
        payload, content_type = download_file_bytes(
            url,
            require_file_hint=False,
        )
        if not payload:
            raise_if_external_download_stopped()
            cache_external_preflight_download(
                url,
                False,
                (b"", content_type),
            )
            if unavailable_source_urls is not None:
                unavailable_source_urls.append(
                    normalize_attachment_identity_url(url)
                )
            LOGGER.warning(
                "첨부 콘텐츠 원문 보존: 다운로드할 수 없는 외부 파일 (%s)",
                summarize_external_request_target(url),
            )
            continue
        cache_external_preflight_download(
            url,
            False,
            (payload, content_type),
        )
        if validated_external_upload_metadata(
            payload,
            name,
            content_type,
            url,
            True,
        ) is None:
            continue
        state.append(
            {
                "source_url": normalize_attachment_identity_url(url),
                "name": name,
                "content_sha256": compute_content_sha256(payload),
            }
        )
    return state


def collect_body_media_content_state(
    blocks: list[JsonObject],
    *,
    unavailable_media: Optional[list[tuple[str, str]]] = None,
) -> list[JsonObject]:
    if not blocks or not should_upload_files_to_notion():
        return []
    state: list[JsonObject] = []
    for block in blocks:
        block_type = str(block.get("type") or "")
        if block_type == "image":
            image = block.get("image", {})
            if image.get("type") != "external":
                continue
            url = str(
                image.get("external", {}).get("url") or ""
            ).strip()
            if not url or not is_allowed_external_download_url(url):
                continue
            payload, content_type = download_file_bytes(
                url,
                require_file_hint=False,
            )
            if not payload:
                raise_if_external_download_stopped()
                cache_external_preflight_download(
                    url,
                    False,
                    (b"", content_type),
                )
                if unavailable_media is not None:
                    unavailable_media.append(
                        (
                            "image",
                            normalize_attachment_identity_url(url),
                        )
                    )
                LOGGER.warning(
                    "본문 이미지 원문 보존: 다운로드할 수 없는 외부 파일 (%s)",
                    summarize_external_request_target(url),
                )
                continue
            cache_external_preflight_download(
                url,
                False,
                (payload, content_type),
            )
            if validated_external_upload_metadata(
                payload,
                derive_filename_from_url(url, fallback="image"),
                content_type,
                url,
                True,
            ) is None:
                continue
            state.append(
                {
                    "type": "image",
                    "source_url": normalize_attachment_identity_url(
                        url
                    ),
                    "content_sha256": compute_content_sha256(payload),
                }
            )
            continue
        if block_type != "embed":
            continue
        embed = block.get("embed", {})
        url = str(embed.get("url") or "").strip()
        if (
            not url
            or not is_embed_file_candidate(url)
            or not is_allowed_external_download_url(
                url,
                require_file_hint=True,
            )
        ):
            continue
        payload, content_type = download_file_bytes(
            url,
            require_file_hint=True,
        )
        if not payload:
            raise_if_external_download_stopped()
            cache_external_preflight_download(
                url,
                True,
                (b"", content_type),
            )
            if unavailable_media is not None:
                unavailable_media.append(
                    (
                        "pdf" if is_pdf_name_or_url(
                            derive_filename_from_url(
                                url,
                                fallback="file",
                            ),
                            url,
                        ) else "file",
                        normalize_attachment_identity_url(url),
                    )
                )
            LOGGER.warning(
                "본문 파일 원문 보존: 다운로드할 수 없는 외부 파일 (%s)",
                summarize_external_request_target(url),
            )
            continue
        cache_external_preflight_download(
            url,
            True,
            (payload, content_type),
        )
        filename = derive_filename_from_url(url, fallback="file")
        if validated_external_upload_metadata(
            payload,
            filename,
            content_type,
            url,
            False,
        ) is None:
            continue
        state.append(
            {
                "type": (
                    "pdf"
                    if is_pdf_name_or_url(filename, url)
                    else "file"
                ),
                "source_url": normalize_attachment_identity_url(url),
                "content_sha256": compute_content_sha256(payload),
            }
        )
    return state


def prepare_attachments_for_sync(
    token: str,
    attachments: list[JsonObject],
    reusable_uploaded_attachments: Optional[
        dict[str, list[JsonObject]]
    ] = None,
) -> tuple[list[JsonObject], list[JsonObject]]:
    if not attachments or not should_upload_files_to_notion():
        return attachments, []
    updated: list[JsonObject] = []
    state: list[JsonObject] = []
    for attachment in attachments:
        if attachment.get("type") != "external":
            updated.append(attachment)
            continue
        url = attachment.get("external", {}).get("url") or ""
        name = attachment.get("name") or extract_attachment_name(attachment)
        if not is_image_name_or_url(name, url):
            updated.append(attachment)
            continue
        identity_url = normalize_attachment_identity_url(url)
        existing_candidates = (
            reusable_uploaded_attachments.get(identity_url) or []
            if reusable_uploaded_attachments and identity_url
            else []
        )
        cached_download = pop_external_preflight_download(
            url,
            False,
        )
        if cached_download is None:
            payload, content_type = download_file_bytes(
                url,
                require_file_hint=False,
            )
        else:
            payload, content_type = cached_download
        if not payload:
            if existing_candidates:
                raise RuntimeError(
                    "기존 첨부 콘텐츠를 검증할 수 없습니다"
                )
            updated.append(attachment)
            continue
        content_sha256 = compute_content_sha256(payload)
        reused_upload_id = pop_reusable_uploaded_attachment_id(
            reusable_uploaded_attachments,
            url,
            content_sha256,
        )
        if reused_upload_id:
            updated.append(
                {
                    "name": name,
                    "type": "file_upload",
                    "file_upload": {"id": reused_upload_id},
                }
            )
            state_entry = build_uploaded_attachment_state_entry(
                url,
                name,
                reused_upload_id,
                content_sha256,
            )
            if state_entry:
                state.append(state_entry)
            continue
        upload_id = upload_external_file_to_notion(
            token,
            url,
            name,
            expect_image=True,
            downloaded_file=(payload, content_type),
        )
        if upload_id:
            updated.append(
                {"name": name, "type": "file_upload", "file_upload": {"id": upload_id}}
            )
            state_entry = build_uploaded_attachment_state_entry(
                url,
                name,
                upload_id,
                content_sha256,
            )
            if state_entry:
                state.append(state_entry)
        else:
            updated.append(attachment)
    return updated, state


def pop_reusable_uploaded_media_block(
    reusable_uploaded_media: Optional[
        dict[tuple[str, str], list[JsonObject]]
    ],
    media_type: str,
    source_url: str,
    content_sha256: str,
) -> Optional[JsonObject]:
    if not reusable_uploaded_media:
        return None
    key = (
        media_type,
        normalize_attachment_identity_url(source_url),
    )
    candidates = reusable_uploaded_media.get(key) or []
    if not candidates:
        return None
    normalized_content_sha256 = normalize_content_sha256(content_sha256)
    match_index = next(
        (
            index
            for index, candidate in enumerate(candidates)
            if normalize_content_sha256(
                candidate.get("content_sha256")
            )
            == normalized_content_sha256
            and normalized_content_sha256
        ),
        None,
    )
    if match_index is None:
        return None
    candidate = candidates.pop(match_index)
    if not candidates:
        reusable_uploaded_media.pop(key, None)
    block = candidate.get("block")
    return block if isinstance(block, dict) else None


def is_valid_reusable_uploaded_media_block(
    block: Optional[JsonObject],
    expected_type: str,
) -> bool:
    if not isinstance(block, dict):
        return False
    if str(block.get("type") or "") != expected_type:
        return False
    payload = block.get(expected_type, {})
    if payload.get("type") != "file_upload":
        return False
    upload_id = str(payload.get("file_upload", {}).get("id") or "").strip()
    return bool(upload_id)


def prepare_body_blocks_for_sync(
    token: str,
    blocks: list[JsonObject],
    reusable_uploaded_media: Optional[
        dict[tuple[str, str], list[JsonObject]]
    ] = None,
) -> tuple[
    list[JsonObject],
    list[JsonObject],
    list[JsonObject],
]:
    if not blocks or not should_upload_files_to_notion():
        return blocks, blocks, []
    updated: list[JsonObject] = []
    hash_blocks: list[JsonObject] = []
    media_state: list[JsonObject] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type == "image":
            image = block.get("image", {})
            if image.get("type") != "external":
                updated.append(block)
                hash_blocks.append(block)
                continue
            url = image.get("external", {}).get("url") or ""
            if not url:
                updated.append(block)
                hash_blocks.append(block)
                continue
            if not is_allowed_external_download_url(url):
                LOGGER.info(
                    "본문 이미지 업로드 생략: 허용되지 않은 외부 URL (%s)",
                    summarize_external_request_target(url),
                )
                updated.append(block)
                hash_blocks.append(block)
                continue
            identity_url = normalize_attachment_identity_url(url)
            reuse_key = ("image", identity_url)
            existing_candidates = (
                reusable_uploaded_media.get(reuse_key) or []
                if reusable_uploaded_media and identity_url
                else []
            )
            cached_download = pop_external_preflight_download(
                url,
                False,
            )
            if cached_download is None:
                payload, content_type = download_file_bytes(
                    url,
                    require_file_hint=False,
                )
            else:
                payload, content_type = cached_download
            if not payload:
                if existing_candidates:
                    raise RuntimeError(
                        "기존 본문 이미지 콘텐츠를 검증할 수 없습니다"
                    )
                updated.append(block)
                hash_blocks.append(block)
                continue
            content_sha256 = compute_content_sha256(payload)
            reused_block = pop_reusable_uploaded_media_block(
                reusable_uploaded_media,
                "image",
                url,
                content_sha256,
            )
            if reused_block:
                if not is_valid_reusable_uploaded_media_block(reused_block, "image"):
                    LOGGER.info(
                        "본문 이미지 재사용 생략: 재사용 블록 타입 불일치 (%s)",
                        reused_block.get("type"),
                    )
                    reused_block = None
            if reused_block:
                # 이전 실행에서 이미 업로드된 이미지면 현재 블록을 재사용해, 부분 성공 뒤 다음 실행에서 중복 업로드를 줄인다.
                # 이때 캡션은 단방향 추가가 아니라 현재 원본 블록 상태에 맞춰 동기화해야 해시와 실제 본문이 어긋나지 않는다.
                if image.get("caption"):
                    reused_block.setdefault("image", {})["caption"] = image["caption"]
                else:
                    reused_block.setdefault("image", {}).pop("caption", None)
                updated.append(reused_block)
                hash_blocks.append(
                    build_uploaded_image_hash_block(
                        url,
                        image.get("caption"),
                        content_sha256,
                    )
                )
                state_entry = build_uploaded_media_state_entry(
                    "image",
                    url,
                    extract_file_upload_id_from_block(reused_block),
                    content_sha256,
                )
                if state_entry:
                    media_state.append(state_entry)
                continue
            filename = derive_filename_from_url(url, fallback="image")
            upload_id = upload_external_file_to_notion(
                token,
                url,
                filename,
                expect_image=True,
                downloaded_file=(payload, content_type),
            )
            if not upload_id:
                updated.append(block)
                hash_blocks.append(block)
                continue
            new_block: JsonObject = {
                "object": "block",
                "type": "image",
                "image": {"type": "file_upload", "file_upload": {"id": upload_id}},
            }
            if image.get("caption"):
                new_block["image"]["caption"] = image["caption"]
            updated.append(new_block)
            hash_blocks.append(
                build_uploaded_image_hash_block(
                    url,
                    image.get("caption"),
                    content_sha256,
                )
            )
            state_entry = build_uploaded_media_state_entry(
                "image",
                url,
                upload_id,
                content_sha256,
            )
            if state_entry:
                media_state.append(state_entry)
            continue
        if block_type == "embed":
            embed = block.get("embed", {})
            url = embed.get("url") or ""
            if not url or not is_embed_file_candidate(url):
                updated.append(block)
                hash_blocks.append(block)
                continue
            # 임베드도 도메인과 파일 신호가 둘 다 맞을 때만 내려받아 업로드한다.
            if not is_allowed_external_download_url(url, require_file_hint=True):
                LOGGER.info(
                    "본문 임베드 업로드 생략: 허용되지 않은 외부 URL (%s)",
                    summarize_external_request_target(url),
                )
                updated.append(block)
                hash_blocks.append(block)
                continue
            filename = derive_filename_from_url(url, fallback="file")
            media_type = "pdf" if is_pdf_name_or_url(filename, url) else "file"
            identity_url = normalize_attachment_identity_url(url)
            reuse_key = (media_type, identity_url)
            existing_candidates = (
                reusable_uploaded_media.get(reuse_key) or []
                if reusable_uploaded_media and identity_url
                else []
            )
            cached_download = pop_external_preflight_download(
                url,
                True,
            )
            if cached_download is None:
                payload, content_type = download_file_bytes(
                    url,
                    require_file_hint=True,
                )
            else:
                payload, content_type = cached_download
            if not payload:
                if existing_candidates:
                    raise RuntimeError(
                        "기존 본문 파일 콘텐츠를 검증할 수 없습니다"
                    )
                updated.append(block)
                hash_blocks.append(block)
                continue
            content_sha256 = compute_content_sha256(payload)
            reused_block = pop_reusable_uploaded_media_block(
                reusable_uploaded_media,
                media_type,
                url,
                content_sha256,
            )
            if reused_block:
                if not is_valid_reusable_uploaded_media_block(reused_block, media_type):
                    LOGGER.info(
                        "본문 임베드 재사용 생략: 재사용 블록 타입 불일치 (%s -> %s)",
                        media_type,
                        reused_block.get("type"),
                    )
                    reused_block = None
            if reused_block:
                updated.append(reused_block)
                hash_blocks.append(
                    build_uploaded_file_hash_block(
                        url,
                        as_pdf=media_type == "pdf",
                        content_sha256=content_sha256,
                    )
                )
                state_entry = build_uploaded_media_state_entry(
                    media_type,
                    url,
                    extract_file_upload_id_from_block(reused_block),
                    content_sha256,
                )
                if state_entry:
                    media_state.append(state_entry)
                continue
            upload_id = upload_external_file_to_notion(
                token,
                url,
                filename,
                expect_image=False,
                downloaded_file=(payload, content_type),
            )
            if not upload_id:
                updated.append(block)
                hash_blocks.append(block)
                continue
            if media_type == "pdf":
                updated.append(build_pdf_block(upload_id))
                hash_blocks.append(
                    build_uploaded_file_hash_block(
                        url,
                        as_pdf=True,
                        content_sha256=content_sha256,
                    )
                )
            else:
                updated.append(build_file_block(upload_id))
                hash_blocks.append(
                    build_uploaded_file_hash_block(
                        url,
                        as_pdf=False,
                        content_sha256=content_sha256,
                    )
                )
            state_entry = build_uploaded_media_state_entry(
                media_type,
                url,
                upload_id,
                content_sha256,
            )
            if state_entry:
                media_state.append(state_entry)
            continue
        updated.append(block)
        hash_blocks.append(block)
    return updated, hash_blocks, media_state


def normalize_notion_object_id(value: str) -> str:
    return value.replace("-", "").strip().lower()


def resolve_notion_data_source_id(
    token: str,
    database_id: str,
    *,
    refresh: bool = False,
) -> str:
    explicit_id = get_notion_data_source_id() or ""
    cache_key = (database_id, explicit_id)
    cached_id = NOTION_DATA_SOURCE_ID_CACHE.get(cache_key)
    if cached_id and not refresh:
        return cached_id

    url = f"https://api.notion.com/v1/databases/{database_id}"
    database = run_database_request_with_object_not_found_retry(
        lambda: notion_request("GET", url, token),
        method="GET",
        database_id=database_id,
        action_name="데이터베이스 컨테이너 조회",
    )
    raw_data_sources = database.get("data_sources")
    if not isinstance(raw_data_sources, list):
        raise NotionDataSourceResolutionError(
            "Notion 데이터베이스 응답에 data_sources 배열이 없습니다. "
            "Notion-Version과 데이터베이스 공유 권한을 확인하세요."
        )

    data_source_ids: dict[str, str] = {}
    for data_source in raw_data_sources:
        if not isinstance(data_source, dict):
            continue
        data_source_id = data_source.get("id")
        if not isinstance(data_source_id, str) or not data_source_id.strip():
            continue
        normalized_id = normalize_notion_object_id(data_source_id)
        if normalized_id:
            data_source_ids.setdefault(normalized_id, data_source_id.strip())

    if not data_source_ids:
        raise NotionDataSourceResolutionError(
            "Notion 데이터베이스에서 사용할 수 있는 데이터 소스를 찾지 못했습니다."
        )

    if explicit_id:
        selected_id = data_source_ids.get(normalize_notion_object_id(explicit_id))
        if not selected_id:
            raise NotionDataSourceResolutionError(
                "NOTION_DATA_SOURCE_ID가 대상 데이터베이스의 데이터 소스가 아닙니다."
            )
    elif len(data_source_ids) == 1:
        selected_id = next(iter(data_source_ids.values()))
    else:
        raise NotionDataSourceResolutionError(
            "대상 데이터베이스에 데이터 소스가 여러 개입니다. "
            "NOTION_DATA_SOURCE_ID로 하나를 명시하세요."
        )

    NOTION_DATA_SOURCE_ID_CACHE[cache_key] = selected_id
    return selected_id


def fetch_database(token: str, database_id: str) -> JsonObject:
    data_source_id = resolve_notion_data_source_id(token, database_id)
    return fetch_data_source(token, database_id, data_source_id)


def fetch_data_source(
    token: str,
    database_id: str,
    data_source_id: str,
) -> JsonObject:
    url = f"https://api.notion.com/v1/data_sources/{data_source_id}"
    return run_database_request_with_object_not_found_retry(
        lambda: notion_request("GET", url, token),
        method="GET",
        database_id=database_id,
        action_name="데이터 소스 조회",
    )


def update_database(
    token: str,
    database_id: str,
    properties: JsonObject,
    allow_schema_changes: Optional[bool] = None,
    *,
    resolved_data_source_id: str = "",
) -> JsonObject:
    migration_allowed = (
        should_allow_notion_schema_migration()
        if allow_schema_changes is None
        else allow_schema_changes
    )
    if not migration_allowed:
        raise NotionSchemaMigrationRequired(
            "Notion 스키마 변경은 정기 실행에서 비활성화되어 있습니다. "
            "NOTION_SCHEMA_MIGRATION=1로 명시적 마이그레이션을 실행하세요."
        )
    data_source_id = (
        resolved_data_source_id
        or resolve_notion_data_source_id(token, database_id)
    )
    url = f"https://api.notion.com/v1/data_sources/{data_source_id}"
    payload = {"properties": properties}
    return run_database_request_with_object_not_found_retry(
        lambda: notion_request("PATCH", url, token, payload),
        method="PATCH",
        database_id=database_id,
        action_name="데이터 소스 속성 수정",
    )


def destination_schema_definitions() -> dict[str, tuple[str, JsonObject]]:
    return {
        TITLE_PROPERTY: ("title", {"title": {}}),
        TOP_PROPERTY: ("checkbox", {"checkbox": {}}),
        DATE_PROPERTY: ("date", {"date": {}}),
        AUTHOR_PROPERTY: (
            "select",
            {"select": {"options": []}},
        ),
        URL_PROPERTY: ("url", {"url": {}}),
        TYPE_PROPERTY: (
            "select",
            {
                "select": {
                    "options": [
                        {"name": name}
                        for name in (*TYPE_TAGS, FALLBACK_TYPE)
                    ]
                }
            },
        ),
        SYNC_OWNER_PROPERTY: ("rich_text", {"rich_text": {}}),
        SOURCE_KEY_PROPERTY: ("rich_text", {"rich_text": {}}),
        NOTICE_ID_PROPERTY: ("rich_text", {"rich_text": {}}),
        SYNC_GENERATION_PROPERTY: ("rich_text", {"rich_text": {}}),
        SYNC_STATUS_PROPERTY: ("rich_text", {"rich_text": {}}),
        SYNC_OPERATION_PROPERTY: ("rich_text", {"rich_text": {}}),
        ATTACHMENT_PROPERTY: ("files", {"files": {}}),
        ATTACHMENT_STATE_PROPERTY: ("rich_text", {"rich_text": {}}),
        BODY_HASH_PROPERTY: ("rich_text", {"rich_text": {}}),
        BODY_MEDIA_STATE_PROPERTY: ("rich_text", {"rich_text": {}}),
        CLASSIFICATION_PROPERTY: (
            "select",
            {"select": {"options": []}},
        ),
        VIEWS_PROPERTY: ("number", {"number": {}}),
    }


def schema_properties(database: JsonObject) -> JsonObject:
    properties = database.get("properties")
    if not isinstance(properties, dict):
        raise RuntimeError("Notion 데이터 소스 properties가 올바르지 않습니다")
    if not all(
        isinstance(name, str) and isinstance(prop, dict)
        for name, prop in properties.items()
    ):
        raise RuntimeError("Notion 데이터 소스 속성 형식이 올바르지 않습니다")
    return cast(JsonObject, properties)


def schema_property_ids(properties: JsonObject) -> dict[str, str]:
    ids: dict[str, str] = {}
    seen: set[str] = set()
    for name, prop in properties.items():
        property_id = str(prop.get("id") or "").strip()
        if not property_id:
            raise RuntimeError(
                f"Notion 속성 ID가 누락되었습니다: {name}"
            )
        if property_id in seen:
            raise RuntimeError(
                "Notion 속성 ID가 중복되었습니다"
            )
        supplied_name = prop.get("name")
        if isinstance(supplied_name, str) and supplied_name != name:
            raise RuntimeError(
                f"Notion 속성 이름이 응답 키와 다릅니다: {name}"
            )
        ids[name] = property_id
        seen.add(property_id)
    return ids


def destination_schema_patch(database: JsonObject) -> JsonObject:
    properties = schema_properties(database)
    property_ids = schema_property_ids(properties)
    definitions = destination_schema_definitions()
    issues: list[str] = []
    title_entries = [
        name
        for name, prop in properties.items()
        if prop.get("type") == "title"
    ]
    title_property = properties.get(TITLE_PROPERTY)
    if title_property is not None:
        if title_property.get("type") != "title":
            issues.append(f"{TITLE_PROPERTY}:title 아님")
        if title_entries != [TITLE_PROPERTY]:
            issues.append("title 속성이 하나가 아닙니다")
    elif len(title_entries) != 1:
        issues.append("title 속성을 하나만 식별할 수 있어야 합니다")

    patch: JsonObject = {}
    if title_property is None and len(title_entries) == 1:
        patch[property_ids[title_entries[0]]] = {
            "name": TITLE_PROPERTY
        }
    for property_name, (expected_type, payload) in definitions.items():
        prop = properties.get(property_name)
        if prop is None:
            if property_name != TITLE_PROPERTY:
                patch[property_name] = json.loads(
                    json.dumps(payload)
                )
            continue
        actual_type = str(prop.get("type") or "")
        if actual_type != expected_type:
            issues.append(
                f"{property_name}:{actual_type or '미지정'}->{expected_type}"
            )
    if issues:
        raise RuntimeError(
            "Notion 스키마 변경 전 검증에 실패했습니다: "
            + ", ".join(issues)
        )
    return patch


def destination_schema_fingerprint(database: JsonObject) -> str:
    properties = schema_properties(database)
    schema_property_ids(properties)
    return compute_content_sha256(
        json.dumps(
            properties,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def ensure_destination_schema(
    token: str,
    database_id: str,
    database: JsonObject,
) -> JsonObject:
    planned_patch = destination_schema_patch(database)
    if not planned_patch:
        validate_destination_schema(database)
        return database
    initial_fingerprint = destination_schema_fingerprint(database)
    data_source_id = resolve_notion_data_source_id(
        token,
        database_id,
        refresh=True,
    )
    current = fetch_data_source(
        token,
        database_id,
        data_source_id,
    )
    if destination_schema_fingerprint(current) != initial_fingerprint:
        raise RuntimeError(
            "Notion 스키마가 변경 계획 생성 이후 달라졌습니다"
        )
    if destination_schema_patch(current) != planned_patch:
        raise RuntimeError(
            "Notion 스키마 변경 계획이 재검증 결과와 다릅니다"
        )
    confirmed_data_source_id = resolve_notion_data_source_id(
        token,
        database_id,
        refresh=True,
    )
    if confirmed_data_source_id != data_source_id:
        raise RuntimeError(
            "Notion 데이터 소스 대상이 적용 직전에 변경되었습니다"
        )
    final_current = fetch_data_source(
        token,
        database_id,
        data_source_id,
    )
    if (
        destination_schema_fingerprint(final_current)
        != initial_fingerprint
        or destination_schema_patch(final_current)
        != planned_patch
    ):
        raise RuntimeError(
            "Notion 스키마가 적용 직전에 변경되었습니다"
        )
    before_ids = schema_property_ids(
        schema_properties(final_current)
    )
    update_database(
        token,
        database_id,
        planned_patch,
        resolved_data_source_id=data_source_id,
    )
    updated = fetch_data_source(
        token,
        database_id,
        data_source_id,
    )
    final_data_source_id = resolve_notion_data_source_id(
        token,
        database_id,
        refresh=True,
    )
    if final_data_source_id != data_source_id:
        raise RuntimeError(
            "Notion 데이터 소스 대상이 적용 중 변경되었습니다"
        )
    validate_destination_schema(updated)
    after_ids = schema_property_ids(schema_properties(updated))
    title_source = next(
        (
            name
            for name, prop in schema_properties(final_current).items()
            if prop.get("type") == "title"
        ),
        "",
    )
    for name, property_id in before_ids.items():
        expected_name = (
            TITLE_PROPERTY
            if name == title_source
            else name
        )
        if after_ids.get(expected_name) != property_id:
            raise RuntimeError(
                "Notion 스키마 변경 후 기존 속성 ID가 보존되지 않았습니다"
            )
    return updated


def ensure_title_property(
    token: str,
    database_id: str,
    database: JsonObject,
) -> JsonObject:
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


def ensure_top_property(
    token: str,
    database_id: str,
    database: JsonObject,
) -> JsonObject:
    prop = database.get("properties", {}).get(TOP_PROPERTY)
    if prop:
        if prop.get("type") != "checkbox":
            raise RuntimeError(
                f"Notion 속성 타입 불일치: {TOP_PROPERTY} (checkbox 아님)"
            )
        return database
    LOGGER.info("Notion 속성 추가: %s", TOP_PROPERTY)
    return update_database(token, database_id, {TOP_PROPERTY: {"checkbox": {}}})


def ensure_date_property(
    token: str,
    database_id: str,
    database: JsonObject,
) -> JsonObject:
    prop = database.get("properties", {}).get(DATE_PROPERTY)
    if prop:
        if prop.get("type") != "date":
            raise RuntimeError(
                f"Notion 속성 타입 불일치: {DATE_PROPERTY} (date 아님)"
            )
        return database
    LOGGER.info("Notion 속성 추가: %s", DATE_PROPERTY)
    return update_database(token, database_id, {DATE_PROPERTY: {"date": {}}})


def ensure_author_property(
    token: str,
    database_id: str,
    database: JsonObject,
) -> JsonObject:
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


def ensure_classification_property(
    token: str,
    database_id: str,
    database: JsonObject,
) -> JsonObject:
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


def ensure_views_property(
    token: str,
    database_id: str,
    database: JsonObject,
) -> JsonObject:
    prop = database.get("properties", {}).get(VIEWS_PROPERTY)
    if prop:
        if prop.get("type") != "number":
            raise RuntimeError(
                f"Notion 속성 타입 불일치: {VIEWS_PROPERTY} (number 아님)"
            )
        return database
    LOGGER.info("Notion 속성 추가: %s", VIEWS_PROPERTY)
    return update_database(token, database_id, {VIEWS_PROPERTY: {"number": {}}})


def ensure_url_property(
    token: str,
    database_id: str,
    database: JsonObject,
) -> JsonObject:
    prop = database.get("properties", {}).get(URL_PROPERTY)
    if prop:
        if prop.get("type") != "url":
            raise RuntimeError(f"Notion 속성 타입 불일치: {URL_PROPERTY} (url 아님)")
        return database
    LOGGER.info("Notion 속성 추가: %s", URL_PROPERTY)
    return update_database(token, database_id, {URL_PROPERTY: {"url": {}}})


def ensure_type_property(
    token: str,
    database_id: str,
    database: JsonObject,
) -> JsonObject:
    prop = database.get("properties", {}).get(TYPE_PROPERTY)
    if prop:
        if prop.get("type") != "select":
            raise RuntimeError(f"Notion 속성 타입 불일치: {TYPE_PROPERTY} (select 아님)")
        return database
    LOGGER.info("Notion 속성 추가: %s", TYPE_PROPERTY)
    options = [{"name": name} for name in (*TYPE_TAGS, FALLBACK_TYPE)]
    return update_database(token, database_id, {TYPE_PROPERTY: {"select": {"options": options}}})


def ensure_attachment_property(
    token: str,
    database_id: str,
    database: JsonObject,
) -> JsonObject:
    prop = database.get("properties", {}).get(ATTACHMENT_PROPERTY)
    if prop:
        if prop.get("type") != "files":
            raise RuntimeError(
                f"Notion 속성 타입 불일치: {ATTACHMENT_PROPERTY} (files 아님)"
            )
        return database
    LOGGER.info("Notion 속성 추가: %s", ATTACHMENT_PROPERTY)
    return update_database(token, database_id, {ATTACHMENT_PROPERTY: {"files": {}}})


def ensure_attachment_state_property(
    token: str,
    database_id: str,
    database: JsonObject,
) -> JsonObject:
    prop = database.get("properties", {}).get(ATTACHMENT_STATE_PROPERTY)
    if prop:
        if prop.get("type") != "rich_text":
            raise RuntimeError(
                f"Notion 속성 타입 불일치: {ATTACHMENT_STATE_PROPERTY} (rich_text 아님)"
            )
        return database
    LOGGER.info("Notion 속성 추가: %s", ATTACHMENT_STATE_PROPERTY)
    return update_database(
        token,
        database_id,
        {ATTACHMENT_STATE_PROPERTY: {"rich_text": {}}},
    )


def ensure_body_hash_property(
    token: str,
    database_id: str,
    database: JsonObject,
) -> JsonObject:
    prop = database.get("properties", {}).get(BODY_HASH_PROPERTY)
    if prop:
        if prop.get("type") != "rich_text":
            raise RuntimeError(
                f"Notion 속성 타입 불일치: {BODY_HASH_PROPERTY} (rich_text 아님)"
            )
        return database
    LOGGER.info("Notion 속성 추가: %s", BODY_HASH_PROPERTY)
    return update_database(token, database_id, {BODY_HASH_PROPERTY: {"rich_text": {}}})


def ensure_body_media_state_property(
    token: str,
    database_id: str,
    database: JsonObject,
) -> JsonObject:
    prop = database.get("properties", {}).get(BODY_MEDIA_STATE_PROPERTY)
    if prop:
        if prop.get("type") != "rich_text":
            raise RuntimeError(
                f"Notion 속성 타입 불일치: {BODY_MEDIA_STATE_PROPERTY} (rich_text 아님)"
            )
        return database
    LOGGER.info("Notion 속성 추가: %s", BODY_MEDIA_STATE_PROPERTY)
    return update_database(token, database_id, {BODY_MEDIA_STATE_PROPERTY: {"rich_text": {}}})


def ensure_rich_text_property(
    token: str,
    database_id: str,
    database: JsonObject,
    property_name: str,
) -> JsonObject:
    prop = database.get("properties", {}).get(property_name)
    if prop:
        if prop.get("type") != "rich_text":
            raise RuntimeError(
                f"Notion 속성 타입 불일치: {property_name} (rich_text 아님)"
            )
        return database
    LOGGER.info("Notion 속성 추가: %s", property_name)
    return update_database(token, database_id, {property_name: {"rich_text": {}}})


def ensure_sync_metadata_properties(
    token: str,
    database_id: str,
    database: JsonObject,
) -> JsonObject:
    for property_name in (
        SYNC_OWNER_PROPERTY,
        SOURCE_KEY_PROPERTY,
        NOTICE_ID_PROPERTY,
        SYNC_GENERATION_PROPERTY,
        SYNC_STATUS_PROPERTY,
        SYNC_OPERATION_PROPERTY,
    ):
        database = ensure_rich_text_property(
            token,
            database_id,
            database,
            property_name,
        )
    return database


def ensure_required_properties(
    token: str,
    database_id: str,
    database: JsonObject,
) -> JsonObject:
    database = ensure_title_property(token, database_id, database)
    database = ensure_top_property(token, database_id, database)
    database = ensure_date_property(token, database_id, database)
    database = ensure_author_property(token, database_id, database)
    database = ensure_url_property(token, database_id, database)
    database = ensure_type_property(token, database_id, database)
    database = ensure_sync_metadata_properties(token, database_id, database)
    return database


def validate_destination_schema(database: JsonObject) -> None:
    expected_types = {
        name: definition[0]
        for name, definition in destination_schema_definitions().items()
    }
    properties = database.get("properties")
    if not isinstance(properties, dict):
        raise RuntimeError("Notion 데이터 소스 properties가 올바르지 않습니다")
    issues: list[str] = []
    for property_name, expected_type in expected_types.items():
        prop = properties.get(property_name)
        if not isinstance(prop, dict):
            issues.append(f"{property_name}:누락")
            continue
        actual_type = str(prop.get("type") or "")
        if actual_type != expected_type:
            issues.append(
                f"{property_name}:{actual_type or '미지정'}->{expected_type}"
            )
    if issues:
        raise RuntimeError(
            "Notion 대상 스키마가 필요한 구성과 다릅니다: " + ", ".join(issues)
        )


def validate_optional_property_type(
    database: JsonObject,
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
def get_select_options(
    database: JsonObject,
    property_name: str,
) -> list[JsonObject]:
    prop = database.get("properties", {}).get(property_name)
    if not prop:
        raise RuntimeError(f"Notion 속성 누락: {property_name}")
    if prop.get("type") != "select":
        raise RuntimeError(f"Notion 속성 타입 오류: {property_name} (select 아님)")
    return cast(
        list[JsonObject],
        prop.get("select", {}).get("options", []),
    )


def sanitize_select_options(
    options: list[JsonObject],
) -> list[JsonObject]:
    sanitized: list[JsonObject] = []
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
    options_cache: list[JsonObject],
) -> list[JsonObject]:
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
    options_cache: list[JsonObject],
    desired_names: set[str],
) -> list[JsonObject]:
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


def query_database(
    token: str,
    database_id: str,
    filter_payload: JsonObject,
) -> list[JsonObject]:
    payload = {"filter": filter_payload}
    data = query_database_page(token, database_id, payload)
    return cast(list[JsonObject], data.get("results", []))


def query_database_page(
    token: str,
    database_id: str,
    payload: JsonObject,
) -> JsonObject:
    data_source_id = resolve_notion_data_source_id(token, database_id)
    url = f"https://api.notion.com/v1/data_sources/{data_source_id}/query"
    return run_database_request_with_object_not_found_retry(
        lambda: notion_request("POST", url, token, payload),
        method="POST",
        database_id=database_id,
        action_name="데이터베이스 쿼리",
    )


def append_block_children(
    token: str,
    block_id: str,
    children: list[JsonObject],
) -> JsonObject:
    url = f"https://api.notion.com/v1/blocks/{block_id}/children"
    payload = {"children": children}
    return notion_request("PATCH", url, token, payload)


def list_block_children(
    token: str,
    block_id: str,
) -> list[JsonObject]:
    base_url = f"https://api.notion.com/v1/blocks/{block_id}/children"
    results: list[JsonObject] = []
    cursor: Optional[str] = None
    seen_cursors: set[str] = set()
    while True:
        check_run_control()
        params: dict[str, Any] = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        url = f"{base_url}?{urlencode(params)}"
        data = notion_request("GET", url, token)
        results.extend(
            cast(list[JsonObject], data.get("results", []))
        )
        if not data.get("has_more"):
            break
        next_cursor = str(data.get("next_cursor") or "").strip()
        if not next_cursor or next_cursor in seen_cursors:
            raise RuntimeError(
                "Notion 블록 페이지네이션 커서가 누락되거나 반복되었습니다"
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor
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
                "블록 DELETE 실패 -> in_trash 폴백: %s (HTTP %s)",
                block_id,
                exc.status_code,
            )
            notion_request("PATCH", url, token, {"in_trash": True})
            return
        raise


def archive_page(token: str, page_id: str) -> None:
    notion_request(
        "PATCH",
        f"https://api.notion.com/v1/pages/{page_id}",
        token,
        {"in_trash": True},
    )


def build_icon() -> JsonObject:
    return {"type": "emoji", "emoji": PAGE_ICON_EMOJI}


def create_page(
    token: str,
    database_id: str,
    properties: JsonObject,
) -> str:
    data_source_id = resolve_notion_data_source_id(token, database_id)
    payload = {
        "parent": {
            "type": "data_source_id",
            "data_source_id": data_source_id,
        },
        "properties": properties,
        "icon": build_icon(),
    }
    data = notion_request("POST", "https://api.notion.com/v1/pages", token, payload)
    page_id = data.get("id")
    if not isinstance(page_id, str) or not page_id:
        raise RuntimeError("Notion 페이지 생성 응답에 id가 없습니다.")
    return page_id


def update_page(
    token: str,
    page_id: str,
    properties: JsonObject,
) -> None:
    payload = {"properties": properties, "icon": build_icon()}
    notion_request("PATCH", f"https://api.notion.com/v1/pages/{page_id}", token, payload)


def retrieve_page(token: str, page_id: str) -> JsonObject:
    page = notion_request(
        "GET",
        f"https://api.notion.com/v1/pages/{page_id}",
        token,
    )
    returned_page_id = str(page.get("id") or "").strip()
    if returned_page_id != page_id:
        raise RuntimeError(
            "Notion 페이지 재조회 응답 ID가 요청과 다릅니다"
        )
    properties = page.get("properties")
    if not isinstance(properties, dict):
        raise RuntimeError(
            "Notion 페이지 재조회 응답 속성이 올바르지 않습니다"
        )
    return page
