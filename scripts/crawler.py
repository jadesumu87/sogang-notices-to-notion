import json
import hashlib
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, TypedDict
from urllib.parse import parse_qs, urlencode, urlparse

from common import (
    ATTACHMENTS_STATUS_KNOWN as _ATTACHMENTS_STATUS_KNOWN,
    ATTACHMENTS_STATUS_UNKNOWN as _ATTACHMENTS_STATUS_UNKNOWN,
    ensure_item_title,
    extract_detail_id_from_text,
    extract_list_rows,
    get_browser_launcher,
    is_detail_url,
)
from log import LOGGER
from notion_client import (
    ExternalDownloadRunStoppedError,
    UnsafeExternalDownloadError,
    build_external_download_opener,
)
from run_control import (
    check_run_control,
    get_destination_state_reserve_seconds,
    remaining_run_seconds,
    sleep_with_run_control,
)
from models import (
    CrawlReport,
    FallbackDetailResult,
    FallbackPageResult,
    FailureCategory,
    ItemCompleteness,
    ListPageResult,
    SiteFetchResult,
    SourceCrawlResult,
    SourceSpec,
    SourceStatus,
)
from bbs_parser import (
    detect_attachment_container,
    detect_loading_shell,
    extract_attachments_from_detail,
    extract_attachments_from_page,
    extract_body_blocks_from_html,
    extract_detail_id_from_row,
    extract_written_at_from_detail,
    extract_written_at_from_page,
    parse_rows,
    inspect_body_content,
)
from settings import (
    BASE_URL,
    BBS_API_BASE,
    BBS_LIST_API_URL,
    DATE_TIME_JS_PATTERN,
    DATE_TIME_PATTERN,
    DEFAULT_QUERY,
    LIST_ROW_SELECTOR,
    USER_AGENT,
    build_detail_url,
    get_attachment_allowed_domains,
    get_attachment_max_count,
    get_bbs_config_fk,
    get_bbs_config_fks,
    get_classification_for_config,
    get_config_list_url_map,
    get_list_base_url,
    get_non_top_max_pages,
    get_optional_config_fks,
    should_include_non_top,
)
from utils import (
    build_site_headers,
    is_allowed_attachment_host,
    is_public_network_address,
    is_safe_external_download_target,
    normalize_date_key,
    normalize_detail_url,
    normalize_file_url,
    normalize_title_key,
    parse_compact_datetime,
    parse_int,
    replace_body_image_urls,
    resolve_public_network_address_info,
)

JsonObject = dict[str, Any]
JsonObjectList = list[JsonObject]
ATTACHMENTS_STATUS_KNOWN: str = _ATTACHMENTS_STATUS_KNOWN
ATTACHMENTS_STATUS_UNKNOWN: str = _ATTACHMENTS_STATUS_UNKNOWN
SITE_FETCH_MAX_RETRIES = 3
SITE_RESPONSE_MAX_BYTES = 10 * 1024 * 1024
NEXT_SITE_REQUEST_AT = 0.0
NEXT_PLAYWRIGHT_REQUEST_AT_BY_HOST: dict[str, float] = {}
PLAYWRIGHT_REQUEST_SLOT_LOCK = threading.Lock()
PLAYWRIGHT_NETWORK_RESOURCE_TYPES = frozenset(
    {"document", "script", "xhr", "fetch"}
)
BODY_STATUS_PRESENT = "present"
BODY_STATUS_CONFIRMED_EMPTY = "confirmed_empty"
BODY_STATUS_UNKNOWN = "unknown"


class DetailSignals(TypedDict, total=False):
    has_html: bool
    valid_detail: bool
    has_error_marker: bool
    has_loading_shell: bool
    has_attachment_label: bool
    has_attachment_container: bool
    has_attachment_link: bool
    has_body_container: bool
    body_has_content: bool


class SourceAccessBlocked(RuntimeError):
    failure_origin = "source"
    failure_kind = "access_block"

    def __init__(
        self,
        error: str,
        status_code: Optional[int] = None,
        retry_after_seconds: Optional[float] = None,
    ) -> None:
        super().__init__(error)
        self.error = error
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class SourceRequestBudget:
    def __init__(
        self,
        max_seconds_cap: Optional[float] = None,
        max_requests_cap: Optional[int] = None,
    ) -> None:
        self.started_at = time.monotonic()
        self.max_actual_requests = self._integer_env(
            "SOURCE_MAX_REQUESTS",
            3000,
            1,
            25000,
        )
        if max_requests_cap is not None:
            self.max_actual_requests = min(
                self.max_actual_requests,
                max(1, int(max_requests_cap)),
            )
        self.max_seconds = self._float_env(
            "SOURCE_MAX_SECONDS",
            480.0,
            10.0,
            3600.0,
        )
        self.exhausted_reason = ""
        if max_seconds_cap is not None:
            if max_seconds_cap <= 0:
                self.max_seconds = 0.0
                self.exhausted_reason = "source_fair_share_exhausted"
            else:
                self.max_seconds = min(
                    self.max_seconds,
                    max_seconds_cap,
                )
        remaining = remaining_run_seconds()
        if remaining is not None:
            available = (
                remaining - get_destination_state_reserve_seconds()
            )
            if available <= 0:
                self.max_seconds = 0.0
                self.exhausted_reason = "destination_reserve_reached"
            else:
                self.max_seconds = min(self.max_seconds, available)
        self.actual_requests = 0
        self.api_logical_requests = 0
        self.fallback_logical_requests = 0

    @staticmethod
    def _integer_env(
        name: str,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        try:
            value = int(os.environ.get(name, str(default)).strip())
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
        try:
            value = float(os.environ.get(name, str(default)).strip())
        except ValueError:
            return default
        return min(maximum, max(minimum, value))

    def check_time(self) -> bool:
        if self.exhausted_reason in {
            "destination_reserve_reached",
            "source_fair_share_exhausted",
        }:
            return False
        if time.monotonic() - self.started_at >= self.max_seconds:
            self.exhaust_time()
            return False
        return True

    def exhaust_time(self) -> None:
        self.exhausted_reason = (
            f"source_time_budget_exceeded:{int(self.max_seconds)}"
        )

    def remaining_seconds(self) -> float:
        if not self.check_time():
            return 0.0
        return max(
            0.0,
            self.max_seconds - (time.monotonic() - self.started_at),
        )

    def can_consume_actual(self) -> bool:
        if not self.check_time():
            return False
        if self.actual_requests >= self.max_actual_requests:
            self.exhausted_reason = (
                "source_request_budget_exceeded:"
                f"{self.max_actual_requests}"
            )
            return False
        return True

    def consume_actual(self) -> bool:
        if not self.can_consume_actual():
            return False
        self.actual_requests += 1
        return True

    def consume_logical(self, kind: str, kind_limit: int) -> bool:
        if not self.check_time():
            return False
        if kind == "api":
            if self.api_logical_requests >= kind_limit:
                self.exhausted_reason = (
                    f"api_request_budget_exceeded:{kind_limit}"
                )
                return False
            self.api_logical_requests += 1
            return True
        if self.fallback_logical_requests >= kind_limit:
            self.exhausted_reason = (
                f"fallback_request_budget_exceeded:{kind_limit}"
            )
            return False
        self.fallback_logical_requests += 1
        return True


CURRENT_SOURCE_REQUEST_BUDGET: ContextVar[
    Optional[SourceRequestBudget]
] = ContextVar("source_request_budget", default=None)


@contextmanager
def source_request_budget_scope(
    max_seconds_cap: Optional[float] = None,
    max_requests_cap: Optional[int] = None,
) -> Iterator[SourceRequestBudget]:
    budget = SourceRequestBudget(
        max_seconds_cap=max_seconds_cap,
        max_requests_cap=max_requests_cap,
    )
    token = CURRENT_SOURCE_REQUEST_BUDGET.set(budget)
    try:
        yield budget
    finally:
        CURRENT_SOURCE_REQUEST_BUDGET.reset(token)


def playwright_address_set(
    address_info: tuple[Any, ...],
) -> frozenset[str]:
    addresses = {
        str(entry[4][0])
        for entry in address_info
        if len(entry) > 4 and entry[4]
    }
    if not addresses or not all(
        is_public_network_address(address) for address in addresses
    ):
        return frozenset()
    return frozenset(addresses)


def select_playwright_pinned_address(
    address_info: tuple[Any, ...],
) -> str:
    public_addresses = playwright_address_set(address_info)
    if not public_addresses:
        return ""
    ipv4_addresses = sorted(
        address for address in public_addresses if ":" not in address
    )
    if ipv4_addresses:
        return ipv4_addresses[0]
    return sorted(public_addresses)[0]


def wait_for_playwright_request_slot(hostname: str) -> bool:
    with PLAYWRIGHT_REQUEST_SLOT_LOCK:
        now = time.monotonic()
        next_request_at = NEXT_PLAYWRIGHT_REQUEST_AT_BY_HOST.get(
            hostname,
            0.0,
        )
        delay = next_request_at - now
        if delay > 0:
            if not sleep_with_source_request_budget(delay):
                return False
            now = time.monotonic()
        NEXT_PLAYWRIGHT_REQUEST_AT_BY_HOST[hostname] = (
            now + get_site_min_request_interval_seconds()
        )
    return True


class PlaywrightNetworkGuard:
    def __init__(
        self,
        approved_host: str,
        pinned_address_info: tuple[Any, ...],
        request_budget: Optional[SourceRequestBudget],
        resolver: Optional[
            Callable[[str, int], tuple[Any, ...]]
        ] = None,
    ) -> None:
        self.approved_host = approved_host.strip().lower().rstrip(".")
        self.pinned_addresses = playwright_address_set(
            pinned_address_info
        )
        if (
            not self.approved_host
            or not is_allowed_attachment_host(
                self.approved_host,
                ("sogang.ac.kr",),
            )
            or not self.pinned_addresses
        ):
            raise ValueError("Playwright 수집 대상이 안전하지 않습니다")
        self.request_budget = request_budget
        self.resolver = resolver or resolve_public_network_address_info
        self.security_error = ""

    def _validated_host(self, url: str) -> str:
        try:
            parsed = urlparse(str(url or ""))
            port = parsed.port
        except ValueError:
            return ""
        hostname = (parsed.hostname or "").strip().lower().rstrip(".")
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or hostname != self.approved_host
        ):
            return ""
        current_addresses = playwright_address_set(
            self.resolver(hostname, 443)
        )
        if (
            not current_addresses
            or current_addresses != self.pinned_addresses
        ):
            return ""
        return hostname

    def validate_navigation_url(self, url: str, error: str) -> bool:
        if self._validated_host(url):
            return True
        self.security_error = error
        return False

    def handle_route(self, route: Any, request: Any) -> None:
        resource_type = str(request.resource_type or "").strip().lower()
        if resource_type not in PLAYWRIGHT_NETWORK_RESOURCE_TYPES:
            route.abort()
            return
        hostname = self._validated_host(str(request.url or ""))
        if not hostname:
            try:
                request_host = (
                    urlparse(str(request.url or "")).hostname or ""
                ).strip().lower().rstrip(".")
            except ValueError:
                request_host = ""
            if (
                resource_type == "document"
                or request_host == self.approved_host
            ):
                self.security_error = "fallback_browser_unsafe_request"
            route.abort()
            return
        if (
            self.request_budget is not None
            and not self.request_budget.can_consume_actual()
        ):
            route.abort()
            return
        if not wait_for_playwright_request_slot(hostname):
            route.abort()
            return
        if (
            self.request_budget is not None
            and not self.request_budget.consume_actual()
        ):
            route.abort()
            return
        route.continue_()


def playwright_security_result(
    source: SourceSpec,
    original: SourceCrawlResult,
    error: str,
) -> SourceCrawlResult:
    return SourceCrawlResult(
        source=source,
        status=SourceStatus.FAILED,
        method="fallback_playwright",
        category=FailureCategory.SECURITY_POLICY,
        error=error,
        fallback_from_error=original.error,
        termination_reason="page_error",
    )


def playwright_source_partial_result(
    source: SourceSpec,
    original: SourceCrawlResult,
    error: str,
) -> SourceCrawlResult:
    return SourceCrawlResult(
        source=source,
        status=SourceStatus.DEGRADED,
        method="fallback_playwright",
        category=FailureCategory.SOURCE_PARTIAL,
        error=error,
        fallback_from_error=original.error,
        termination_reason="request_budget",
    )



def log_attachments(
    label: str,
    attachments: JsonObjectList,
) -> None:
    if not attachments:
        return
    LOGGER.info("첨부파일 추출: %s (총 %s개)", label, len(attachments))
    for attachment in attachments:
        url = attachment.get("external", {}).get("url") or ""
        name = attachment.get("name") or ""
        LOGGER.info("첨부파일 링크: %s (%s)", url, name)


def cap_attachments(
    attachments: JsonObjectList,
    label: str,
) -> JsonObjectList:
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


def classify_attachment_status_from_signals(
    attachments: JsonObjectList,
    signals: DetailSignals,
) -> str:
    if attachments:
        return ATTACHMENTS_STATUS_KNOWN
    if not signals.get("has_html") or not signals.get("valid_detail"):
        return ATTACHMENTS_STATUS_UNKNOWN
    if signals.get("has_attachment_link"):
        return ATTACHMENTS_STATUS_UNKNOWN
    if signals.get("has_attachment_container"):
        return ATTACHMENTS_STATUS_KNOWN
    return ATTACHMENTS_STATUS_UNKNOWN


def classify_attachment_status_from_api_detail(
    detail: Optional[JsonObject],
    attachments: JsonObjectList,
    fallback_reason: Optional[str],
    fallback_attachment_status: str,
) -> str:
    if attachments:
        return ATTACHMENTS_STATUS_KNOWN
    if not isinstance(detail, dict) or not detail:
        return fallback_attachment_status

    content_html = str(detail.get("content") or "")
    has_attachment_hint = bool(
        content_html and detect_attachment_evidence_from_html(content_html)
    )
    attachment_field_keys = api_attachment_field_keys(detail)
    has_file_value = any(
        str(detail.get(key) or "").strip()
        for key in attachment_field_keys
    )
    if has_attachment_hint or has_file_value:
        return (
            ATTACHMENTS_STATUS_KNOWN
            if fallback_attachment_status == ATTACHMENTS_STATUS_KNOWN
            else ATTACHMENTS_STATUS_UNKNOWN
        )
    if fallback_reason and "attachment_missing" in fallback_reason.split(","):
        return fallback_attachment_status
    if (
        fallback_reason
        and "attachment_schema_missing" in fallback_reason.split(",")
    ):
        return fallback_attachment_status
    if attachment_field_keys:
        return ATTACHMENTS_STATUS_KNOWN
    return ATTACHMENTS_STATUS_UNKNOWN


def apply_item_attachments(
    item: JsonObject,
    attachments: JsonObjectList,
    attachment_status: str,
) -> None:
    status = (
        ATTACHMENTS_STATUS_KNOWN
        if attachments or attachment_status == ATTACHMENTS_STATUS_KNOWN
        else ATTACHMENTS_STATUS_UNKNOWN
    )
    item["attachments_status"] = status
    if status != ATTACHMENTS_STATUS_KNOWN:
        item.pop("attachments", None)
        return

    capped = cap_attachments(attachments, item["title"]) if attachments else []
    if len(capped) != len(attachments):
        item["attachments_status"] = ATTACHMENTS_STATUS_UNKNOWN
        item["attachments_truncated"] = True
        item["attachment_observed_count"] = len(attachments)
        item["attachment_limit"] = len(capped)
        item.pop("attachments", None)
        return
    item["attachments"] = capped
    if capped:
        log_attachments(item["title"], capped)


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


def is_retryable_site_status(status_code: int) -> bool:
    return status_code in {429, 500, 502, 503, 504}


def get_site_retry_sleep_seconds(
    attempt: int,
    retry_after: Optional[str] = None,
) -> float:
    header_delay: float = parse_retry_after_seconds(retry_after) or 0.0
    backoff_delay = min(2.0**attempt, 8.0)
    return min(max(header_delay, backoff_delay), 60.0)


def get_site_response_max_bytes() -> int:
    raw = os.environ.get("SITE_RESPONSE_MAX_BYTES", str(SITE_RESPONSE_MAX_BYTES)).strip()
    try:
        value = int(raw)
    except ValueError:
        return SITE_RESPONSE_MAX_BYTES
    return max(1024, value)


def get_site_min_request_interval_seconds() -> float:
    raw = os.environ.get("SITE_MIN_REQUEST_INTERVAL_SECONDS", "1.0").strip()
    try:
        value = float(raw)
    except ValueError:
        return 1.0
    return min(5.0, max(0.0, value))


def sleep_with_source_request_budget(seconds: float) -> bool:
    delay = max(0.0, seconds)
    request_budget = CURRENT_SOURCE_REQUEST_BUDGET.get()
    if request_budget is not None:
        remaining = request_budget.remaining_seconds()
        if delay >= remaining:
            request_budget.exhaust_time()
            return False
    sleep_with_run_control(delay)
    return request_budget is None or request_budget.check_time()


def wait_for_site_request_slot() -> bool:
    global NEXT_SITE_REQUEST_AT
    now = time.monotonic()
    delay = NEXT_SITE_REQUEST_AT - now
    if delay > 0:
        if not sleep_with_source_request_budget(delay):
            return False
        now = time.monotonic()
    NEXT_SITE_REQUEST_AT = now + get_site_min_request_interval_seconds()
    return True


def reserve_site_request() -> bool:
    request_budget = CURRENT_SOURCE_REQUEST_BUDGET.get()
    if (
        request_budget is not None
        and not request_budget.can_consume_actual()
    ):
        return False
    if not wait_for_site_request_slot():
        return False
    return (
        request_budget is None
        or request_budget.consume_actual()
    )


def source_budget_fetch_result(
    url: str,
    attempts: int,
) -> SiteFetchResult:
    request_budget = CURRENT_SOURCE_REQUEST_BUDGET.get()
    return SiteFetchResult(
        ok=False,
        final_url=url,
        category=FailureCategory.SOURCE_PARTIAL,
        error=(
            request_budget.exhausted_reason
            if request_budget is not None
            else "source_request_budget_exceeded"
        ),
        attempts=attempts,
    )


def read_site_response_bytes(response: Any, max_bytes: int) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            declared = int(content_length)
        except ValueError:
            declared = 0
        if declared > max_bytes:
            raise ValueError(f"response_too_large:{declared}>{max_bytes}")
    data: bytes = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"response_too_large:{len(data)}>{max_bytes}")
    return data


def fetch_site_result(url: str, label: str) -> SiteFetchResult:
    max_bytes = get_site_response_max_bytes()
    if not is_safe_external_download_target(url):
        return SiteFetchResult(
            ok=False,
            final_url=url,
            category=FailureCategory.SECURITY_POLICY,
            error="unsafe_site_target",
            attempts=0,
        )
    opener = build_external_download_opener(
        before_redirect=lambda _url: reserve_site_request()
    )
    for attempt in range(SITE_FETCH_MAX_RETRIES + 1):
        check_run_control()
        if not reserve_site_request():
            return source_budget_fetch_result(url, attempt)
        req = urllib.request.Request(url, headers=build_site_headers())
        try:
            with opener.open(req, timeout=30) as resp:
                try:
                    body = read_site_response_bytes(resp, max_bytes)
                except ValueError as exc:
                    LOGGER.warning("%s 응답 차단: %s (%s)", label, url, exc)
                    return SiteFetchResult(
                        ok=False,
                        status_code=getattr(resp, "status", None),
                        final_url=str(resp.geturl() or url),
                        category=FailureCategory.SECURITY_POLICY,
                        error=str(exc),
                        attempts=attempt + 1,
                    )
                return SiteFetchResult(
                    ok=True,
                    status_code=getattr(resp, "status", 200),
                    body=body,
                    content_type=str(resp.headers.get("Content-Type") or ""),
                    final_url=str(resp.geturl() or url),
                    attempts=attempt + 1,
                )
        except UnsafeExternalDownloadError:
            return SiteFetchResult(
                ok=False,
                final_url=url,
                category=FailureCategory.SECURITY_POLICY,
                error="unsafe_redirect_target",
                attempts=attempt + 1,
            )
        except ExternalDownloadRunStoppedError:
            return source_budget_fetch_result(url, attempt + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in {403, 429}:
                retry_after_seconds = parse_retry_after_seconds(
                    exc.headers.get("Retry-After")
                )
                return SiteFetchResult(
                    ok=False,
                    status_code=exc.code,
                    final_url=url,
                    category=FailureCategory.SECURITY_POLICY,
                    error=(
                        "rate_limited"
                        if exc.code == 429
                        else "access_forbidden"
                    ),
                    attempts=attempt + 1,
                    retry_after_seconds=retry_after_seconds,
                )
            if is_retryable_site_status(exc.code) and attempt < SITE_FETCH_MAX_RETRIES:
                sleep_s = get_site_retry_sleep_seconds(
                    attempt,
                    retry_after=exc.headers.get("Retry-After"),
                )
                LOGGER.info(
                    "%s 재시도(%s/%s): %s -> HTTP %s, 대기=%.1fs",
                    label,
                    attempt + 1,
                    SITE_FETCH_MAX_RETRIES,
                    url,
                    exc.code,
                    sleep_s,
                )
                if not sleep_with_source_request_budget(sleep_s):
                    return source_budget_fetch_result(
                        url,
                        attempt + 1,
                    )
                continue
            LOGGER.info("%s 실패: %s (HTTP %s)", label, url, exc.code)
            return SiteFetchResult(
                ok=False,
                status_code=exc.code,
                final_url=url,
                category=FailureCategory.SOURCE_UPSTREAM,
                error=f"HTTP {exc.code}",
                attempts=attempt + 1,
            )
        except urllib.error.URLError as exc:
            is_timeout = isinstance(exc.reason, socket.timeout)
            if attempt < SITE_FETCH_MAX_RETRIES and is_timeout:
                sleep_s = get_site_retry_sleep_seconds(attempt)
                LOGGER.info(
                    "%s 재시도(%s/%s): %s -> 타임아웃, 대기=%.1fs",
                    label,
                    attempt + 1,
                    SITE_FETCH_MAX_RETRIES,
                    url,
                    sleep_s,
                )
                if not sleep_with_source_request_budget(sleep_s):
                    return source_budget_fetch_result(
                        url,
                        attempt + 1,
                    )
                continue
            if is_timeout:
                LOGGER.info("%s 실패: %s (타임아웃)", label, url)
            else:
                LOGGER.info("%s 실패: %s (%s)", label, url, exc.reason)
            return SiteFetchResult(
                ok=False,
                final_url=url,
                category=FailureCategory.NETWORK,
                error="timeout" if is_timeout else str(exc.reason),
                attempts=attempt + 1,
            )
        except socket.timeout:
            if attempt < SITE_FETCH_MAX_RETRIES:
                sleep_s = get_site_retry_sleep_seconds(attempt)
                LOGGER.info(
                    "%s 재시도(%s/%s): %s -> 타임아웃, 대기=%.1fs",
                    label,
                    attempt + 1,
                    SITE_FETCH_MAX_RETRIES,
                    url,
                    sleep_s,
                )
                if not sleep_with_source_request_budget(sleep_s):
                    return source_budget_fetch_result(
                        url,
                        attempt + 1,
                    )
                continue
            LOGGER.info("%s 실패: %s (타임아웃)", label, url)
            return SiteFetchResult(
                ok=False,
                final_url=url,
                category=FailureCategory.NETWORK,
                error="timeout",
                attempts=attempt + 1,
            )
    return SiteFetchResult(
        ok=False,
        final_url=url,
        category=FailureCategory.INTERNAL,
        error="site_fetch_exhausted",
        attempts=SITE_FETCH_MAX_RETRIES + 1,
    )


def fetch_site_bytes(url: str, label: str) -> Optional[bytes]:
    result = fetch_site_result(url, label)
    return result.body if result.ok else None


def fetch_site_json_result(url: str) -> tuple[SiteFetchResult, Optional[dict[str, Any]]]:
    result = fetch_site_result(url, "API 요청")
    if not result.ok or result.body is None:
        return result, None
    if "application/json" not in result.content_type.lower():
        visible_text = visible_html_text(
            result.body.decode("utf-8", errors="replace")
        ).lower()
        result.ok = False
        if any(
            marker in visible_text
            for marker in (
                "access denied",
                "captcha",
                "cloudflare",
                "too many requests",
            )
        ):
            result.category = FailureCategory.SECURITY_POLICY
            result.error = "access_challenge"
        else:
            result.category = FailureCategory.SOURCE_CONTRACT
            result.error = "unexpected_json_content_type"
        return result, None
    try:
        text = result.body.decode("utf-8", errors="replace")
        payload = json.loads(text)
    except json.JSONDecodeError:
        LOGGER.info("API 응답 파싱 실패: %s", url)
        result.ok = False
        result.category = FailureCategory.SOURCE_CONTRACT
        result.error = "invalid_json"
        return result, None
    if not isinstance(payload, dict):
        result.ok = False
        result.category = FailureCategory.SOURCE_CONTRACT
        result.error = "json_root_not_object"
        return result, None
    return result, payload


def fetch_site_json(url: str) -> Optional[JsonObject]:
    result, payload = fetch_site_json_result(url)
    return payload if result.ok else None


def pagination_value(
    payload: JsonObject,
    names: tuple[str, ...],
) -> object:
    containers = [payload]
    for key in ("pagination", "pageInfo", "pageable"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            containers.append(nested)
    for container in containers:
        for name in names:
            if name in container:
                return container[name]
    return None


def pagination_int(
    payload: JsonObject,
    names: tuple[str, ...],
) -> Optional[int]:
    value = pagination_value(payload, names)
    if isinstance(value, bool):
        return None
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def pagination_bool(
    payload: JsonObject,
    names: tuple[str, ...],
) -> Optional[bool]:
    value = pagination_value(payload, names)
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    return None


def fetch_bbs_list_result(
    page_num: int,
    page_size: int = 20,
    config_fk: Optional[str] = None,
) -> ListPageResult:
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
    fetch_result, data = fetch_site_json_result(url)
    if not fetch_result.ok or data is None:
        return ListPageResult(
            ok=False,
            category=fetch_result.category,
            error=fetch_result.error,
            status_code=fetch_result.status_code,
            retry_after_seconds=fetch_result.retry_after_seconds,
        )
    data_object = data.get("data")
    if not isinstance(data_object, dict) or "list" not in data_object:
        return ListPageResult(
            ok=False,
            category=FailureCategory.SOURCE_CONTRACT,
            error="missing_data_list",
            status_code=fetch_result.status_code,
        )
    entries = data_object.get("list")
    if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
        return ListPageResult(
            ok=False,
            category=FailureCategory.SOURCE_CONTRACT,
            error="invalid_data_list",
            status_code=fetch_result.status_code,
        )
    effective_page = pagination_int(
        data_object,
        ("pageNum", "pageNumber", "currentPage", "page"),
    )
    if effective_page is not None and effective_page != page_num:
        return ListPageResult(
            ok=False,
            category=FailureCategory.SOURCE_CONTRACT,
            error=f"page_number_mismatch:{effective_page}!={page_num}",
            status_code=fetch_result.status_code,
            requested_page=page_num,
            effective_page=effective_page,
        )
    effective_page_size = pagination_int(
        data_object,
        ("pageSize", "size", "limit"),
    )
    if (
        effective_page_size is not None
        and effective_page_size != page_size
    ):
        return ListPageResult(
            ok=False,
            category=FailureCategory.SOURCE_CONTRACT,
            error=(
                f"page_size_mismatch:{effective_page_size}!={page_size}"
            ),
            status_code=fetch_result.status_code,
            requested_page=page_num,
            effective_page=effective_page or page_num,
            page_size=effective_page_size,
        )
    total_count = pagination_int(
        data_object,
        ("totalCount", "totalElements"),
    )
    explicit_has_more = pagination_bool(
        data_object,
        ("hasMore", "hasNext", "next"),
    )
    has_more = explicit_has_more
    terminal_verified = has_more is False
    return ListPageResult(
        ok=True,
        entries=entries,
        valid_empty=not entries,
        status_code=fetch_result.status_code,
        requested_page=page_num,
        effective_page=effective_page or page_num,
        page_size=effective_page_size or page_size,
        total_count=total_count,
        has_more=has_more,
        terminal_verified=terminal_verified,
    )


def fetch_bbs_list(
    page_num: int,
    page_size: int = 20,
    config_fk: Optional[str] = None,
) -> JsonObjectList:
    entries: JsonObjectList = fetch_bbs_list_result(
        page_num,
        page_size,
        config_fk,
    ).entries
    return entries


def fetch_bbs_detail(
    pk_id: str,
    config_fk: Optional[str] = None,
) -> Optional[JsonObject]:
    config_fk = (config_fk or get_bbs_config_fk()).strip()
    params = {"pkId": pk_id}
    if config_fk:
        params["bbsConfigFk"] = config_fk
    url = f"{BBS_API_BASE}?{urlencode(params)}"
    fetch_result, data = fetch_site_json_result(url)
    if not fetch_result.ok:
        if fetch_result.category == FailureCategory.SECURITY_POLICY:
            raise SourceAccessBlocked(
                fetch_result.error or "source_access_blocked",
                fetch_result.status_code,
                fetch_result.retry_after_seconds,
            )
        return None
    if not data:
        return None
    detail = data.get("data")
    if not isinstance(detail, dict):
        return None
    return detail


def get_detail_html_fallback_reason(
    detail: object,
    entry_title: str = "",
) -> Optional[str]:
    if detail is None:
        return "api_missing"
    if not isinstance(detail, dict):
        return "api_invalid"
    reasons: list[str] = []
    detail_title = normalize_title_key(str(detail.get("title") or ""))
    list_title = normalize_title_key(entry_title)
    if not detail_title and not list_title:
        reasons.append("title_missing")
    if not parse_compact_datetime(detail.get("regDate")):
        reasons.append("date_missing")
    content_html = str(detail.get("content") or "")
    body_blocks = extract_body_blocks_from_html(content_html) if content_html else []
    if not body_blocks:
        reasons.append("body_missing")
    attachments = extract_attachments_from_api_data(detail)
    if not api_attachment_field_keys(detail):
        reasons.append("attachment_schema_missing")
    if (
        not attachments
        and content_html
        and detect_attachment_evidence_from_html(content_html)
    ):
        reasons.append("attachment_missing")
    if not reasons:
        return None
    return ",".join(reasons)


def fetch_detail_metadata_with_html_fallback(
    pk_id: str,
    detail_url: str,
    reason: str,
) -> tuple[
    Optional[str],
    JsonObjectList,
    JsonObjectList,
    str,
    str,
    str,
]:
    LOGGER.warning("상세 API 보완 조회: %s (%s) -> HTML 폴백 시도", pk_id, reason)
    written_at, attachments, body_blocks, signals = fetch_detail_metadata_from_url(detail_url)
    (
        confirmed_written_at,
        confirmed_attachments,
        confirmed_body_blocks,
        confirmed_signals,
    ) = fetch_detail_metadata_from_url(detail_url)
    first_signature = json.dumps(
        {
            "written_at": written_at,
            "attachments": attachments,
            "body_blocks": body_blocks,
            "signals": signals,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    confirmed_signature = json.dumps(
        {
            "written_at": confirmed_written_at,
            "attachments": confirmed_attachments,
            "body_blocks": confirmed_body_blocks,
            "signals": confirmed_signals,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if first_signature != confirmed_signature:
        return (
            None,
            [],
            [],
            "detail_unstable",
            ATTACHMENTS_STATUS_UNKNOWN,
            BODY_STATUS_UNKNOWN,
        )
    attachment_status = classify_attachment_status_from_signals(attachments, signals)
    body_status = classify_body_status(body_blocks, signals)
    if attachment_status == ATTACHMENTS_STATUS_KNOWN and not attachments:
        attachment_status = ATTACHMENTS_STATUS_UNKNOWN
    if body_status == BODY_STATUS_CONFIRMED_EMPTY and not body_blocks:
        body_status = BODY_STATUS_UNKNOWN
    if written_at or attachments or body_blocks:
        LOGGER.info(
            "상세 HTML 폴백 성공: %s (작성일=%s, 첨부=%s, 본문=%s)",
            pk_id,
            "Y" if written_at else "N",
            len(attachments),
            len(body_blocks),
        )
        status = "html_fallback" if reason == "api_missing" else "html_fallback_partial"
        return (
            written_at,
            attachments,
            body_blocks,
            status,
            attachment_status,
            body_status,
        )
    LOGGER.warning("상세 HTML 폴백 실패: %s (%s)", pk_id, detail_url)
    status = "detail_missing" if reason == "api_missing" else "detail_incomplete"
    return (
        None,
        [],
        [],
        status,
        ATTACHMENTS_STATUS_UNKNOWN,
        BODY_STATUS_UNKNOWN,
    )


def extract_attachments_from_api_data(
    data: JsonObject,
) -> JsonObjectList:
    attachments: JsonObjectList = []
    seen: set[str] = set()
    allowed_domains = get_attachment_allowed_domains()
    for key in api_attachment_field_keys(data):
        raw = data.get(key)
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


def api_attachment_field_keys(data: JsonObject) -> list[str]:
    indexed: list[tuple[int, str]] = []
    for key in data:
        match = re.fullmatch(r"fileValue(\d+)", str(key))
        if not match:
            continue
        indexed.append((int(match.group(1)), str(key)))
    return [key for _, key in sorted(indexed)]


def api_has_explicit_empty_attachment_fields(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    keys = api_attachment_field_keys(data)
    return bool(keys) and not any(
        str(data.get(key) or "").strip()
        for key in keys
    )


def api_detail_signature(detail: object) -> str:
    if not isinstance(detail, dict):
        return "null"
    payload = {
        "title": detail.get("title"),
        "regDate": detail.get("regDate"),
        "userName": detail.get("userName"),
        "content": detail.get("content"),
        "attachments": [
            (key, detail.get(key))
            for key in api_attachment_field_keys(detail)
        ],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def build_list_url(page: int, base_url: Optional[str] = None) -> str:
    query = dict(DEFAULT_QUERY)
    query["page"] = str(page)
    base_url = base_url or BASE_URL
    return f"{base_url}?{urlencode(query)}"


def return_to_list_page(page: Any, list_url: str) -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        page.go_back()
        page.wait_for_selector(LIST_ROW_SELECTOR, timeout=30000)
    except PlaywrightTimeoutError:
        if not goto_list_page(page, list_url):
            LOGGER.info("목록 복귀 실패: %s", list_url)


def wait_for_written_at(page: Any, timeout_ms: int = 30000) -> bool:
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


def wait_for_detail_url(page: Any, list_url: str) -> Optional[str]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        page.wait_for_url(lambda url: is_detail_url(url) and url != list_url, timeout=30000)
    except PlaywrightTimeoutError:
        return None
    current_url = str(page.url or "").strip()
    return current_url or None

def fetch_detail_metadata_via_playwright(
    page: Any,
    list_url: str,
    detail_url: str,
) -> tuple[
    Optional[str],
    JsonObjectList,
    JsonObjectList,
    Optional[str],
]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    written_at = None
    attachments: JsonObjectList = []
    body_blocks: JsonObjectList = []
    attachment_status: Optional[str] = None
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
        html_text = page.content()
        written_at = extract_written_at_from_page(page)
        if not written_at:
            written_at = extract_written_at_from_detail(html_text)
        attachments = extract_attachments_from_page(page)
        if not attachments:
            attachments = extract_attachments_from_detail(html_text)
        body_blocks = extract_body_blocks_from_html(html_text)
        attachment_status = classify_attachment_status_from_signals(
            attachments,
            build_detail_signals(html_text),
        )
        if attachments and body_blocks:
            body_blocks = replace_body_image_urls(body_blocks, attachments)
    except PlaywrightTimeoutError:
        LOGGER.info("상세 페이지 로드 실패: %s", detail_url)
    finally:
        return_to_list_page(page, list_url)
    return written_at, attachments, body_blocks, attachment_status


def merge_playwright_attachment_result(
    attachments: JsonObjectList,
    attachment_status: str,
    pw_attachments: JsonObjectList,
    pw_attachment_status: Optional[str],
) -> tuple[JsonObjectList, str]:
    merged_attachments = pw_attachments if pw_attachments else attachments
    merged_attachment_status = (
        pw_attachment_status
        if pw_attachment_status is not None
        else attachment_status
    )
    return merged_attachments, merged_attachment_status


def fetch_detail_for_row(
    page: Any,
    list_url: str,
    row_index: int,
    detail_url: Optional[str],
    config_fk: Optional[str] = None,
) -> tuple[
    Optional[str],
    Optional[str],
    JsonObjectList,
    JsonObjectList,
    str,
]:
    if detail_url:
        detail_url = normalize_detail_url(detail_url)
        if detail_url and not is_detail_url(detail_url):
            LOGGER.info("상세 URL 경로 아님: %s", detail_url)
            detail_url = None
    if detail_url:
        written_at, attachments, body_blocks, signals = fetch_detail_metadata_from_url(
            detail_url
        )
        attachment_status = classify_attachment_status_from_signals(attachments, signals)
        if should_retry_detail_fetch(written_at, attachments, body_blocks, signals):
            pw_written_at, pw_attachments, pw_body_blocks, pw_attachment_status = (
                fetch_detail_metadata_via_playwright(page, list_url, detail_url)
            )
            if not written_at and pw_written_at:
                written_at = pw_written_at
            attachments, attachment_status = merge_playwright_attachment_result(
                attachments,
                attachment_status,
                pw_attachments,
                pw_attachment_status,
            )
            if pw_body_blocks:
                body_blocks = pw_body_blocks
        return written_at, detail_url, attachments, body_blocks, attachment_status

    rows = page.locator(LIST_ROW_SELECTOR)
    if row_index >= rows.count():
        return None, None, [], [], ATTACHMENTS_STATUS_UNKNOWN

    row = rows.nth(row_index)
    row.scroll_into_view_if_needed()
    detail_id = extract_detail_id_from_row(row)
    if detail_id:
        normalized_detail_url = normalize_detail_url(build_detail_url(detail_id, config_fk))
        if normalized_detail_url:
            written_at, attachments, body_blocks, signals = fetch_detail_metadata_from_url(
                normalized_detail_url
            )
            attachment_status = classify_attachment_status_from_signals(
                attachments,
                signals,
            )
            if should_retry_detail_fetch(written_at, attachments, body_blocks, signals):
                pw_written_at, pw_attachments, pw_body_blocks, pw_attachment_status = (
                    fetch_detail_metadata_via_playwright(
                        page,
                        list_url,
                        normalized_detail_url,
                    )
                )
                if not written_at and pw_written_at:
                    written_at = pw_written_at
                attachments, attachment_status = merge_playwright_attachment_result(
                    attachments,
                    attachment_status,
                    pw_attachments,
                    pw_attachment_status,
                )
                if pw_body_blocks:
                    body_blocks = pw_body_blocks
            if written_at or attachments or body_blocks:
                return (
                    written_at,
                    normalized_detail_url,
                    attachments,
                    body_blocks,
                    attachment_status,
                )
    row.click()

    detail_url = wait_for_detail_url(page, list_url)
    if not detail_url:
        LOGGER.info("상세 URL 전환 실패: row %s", row_index)
        return_to_list_page(page, list_url)
        return None, None, [], [], ATTACHMENTS_STATUS_UNKNOWN

    normalized_detail_url = normalize_detail_url(detail_url)
    if not normalized_detail_url:
        LOGGER.warning("허용되지 않은 상세 URL 차단: %s", detail_url)
        return_to_list_page(page, list_url)
        return None, None, [], [], ATTACHMENTS_STATUS_UNKNOWN
    written_at, attachments, body_blocks, signals = fetch_detail_metadata_from_url(
        normalized_detail_url
    )
    attachment_status = classify_attachment_status_from_signals(attachments, signals)
    if not wait_for_written_at(page):
        LOGGER.info("작성일 로드 대기 실패: %s", detail_url)
    html_text = page.content()
    if not written_at:
        written_at = extract_written_at_from_page(page)
        if not written_at:
            written_at = extract_written_at_from_detail(html_text)
    page_attachments = extract_attachments_from_page(page)
    if page_attachments:
        attachments = page_attachments
    elif not attachments:
        attachments = extract_attachments_from_detail(html_text)
    page_blocks = extract_body_blocks_from_html(html_text)
    if page_blocks:
        body_blocks = page_blocks
    attachment_status = classify_attachment_status_from_signals(
        attachments,
        build_detail_signals(html_text),
    )
    if attachments and body_blocks:
        body_blocks = replace_body_image_urls(body_blocks, attachments)
    return_to_list_page(page, list_url)
    return written_at, normalized_detail_url, attachments, body_blocks, attachment_status


def goto_list_page(page: Any, url: str) -> bool:
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


def build_source_spec(config_fk: str) -> SourceSpec:
    classification = get_classification_for_config(config_fk)
    list_url = get_config_list_url_map().get(config_fk)
    if not classification:
        raise RuntimeError(f"출처 분류 매핑 누락: {config_fk}")
    if not list_url or not normalize_detail_url(list_url):
        raise RuntimeError(f"출처 목록 URL 매핑 오류: {config_fk}")
    return SourceSpec(
        config_fk=config_fk,
        classification=classification,
        list_url=list_url,
        required=config_fk not in get_optional_config_fks(),
    )


def get_hard_page_limit() -> int:
    raw = os.environ.get("CRAWL_HARD_PAGE_LIMIT", "100").strip()
    try:
        value = int(raw)
    except ValueError:
        return 100
    return min(1000, max(1, value))


def get_api_request_budget() -> int:
    raw = os.environ.get("API_MAX_REQUESTS", "2500").strip()
    try:
        value = int(raw)
    except ValueError:
        return 2500
    return min(25000, max(1, value))


def get_api_time_budget_seconds() -> float:
    raw = os.environ.get("API_MAX_SECONDS", "840").strip()
    try:
        value = float(raw)
    except ValueError:
        return 840.0
    return min(3600.0, max(10.0, value))


def get_backfill_detail_limit() -> int:
    raw = os.environ.get("BACKFILL_DETAIL_LIMIT", "100").strip()
    try:
        value = int(raw)
    except ValueError:
        return 100
    return min(1000, max(1, value))


def get_incremental_checkpoint_overlap_pages() -> int:
    raw = os.environ.get(
        "INCREMENTAL_CHECKPOINT_OVERLAP_PAGES",
        "2",
    ).strip()
    try:
        value = int(raw)
    except ValueError:
        return 2
    return min(10, max(1, value))


def is_plausible_notice_datetime(value: object) -> bool:
    try:
        parsed = datetime.fromisoformat(
            str(value or "").replace("Z", "+00:00")
        )
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return False
    normalized = parsed.astimezone(timezone.utc)
    return (
        datetime(2000, 1, 1, tzinfo=timezone.utc)
        <= normalized
        <= datetime.now(timezone.utc) + timedelta(days=1)
    )


def validate_api_list_entry(entry: object) -> Optional[str]:
    if not isinstance(entry, dict):
        return "entry_not_object"
    pk_id = entry.get("pkId")
    if isinstance(pk_id, bool) or not isinstance(pk_id, (str, int)):
        return "pkId_type"
    if not str(pk_id).strip().isdigit():
        return "pkId_value"
    title = entry.get("title")
    if not isinstance(title, str) or not normalize_title_key(title):
        return "title_type"
    reg_date = entry.get("regDate")
    if not isinstance(reg_date, str) or not parse_compact_datetime(reg_date):
        return "regDate_type"
    is_top = entry.get("isTop")
    if not isinstance(is_top, str) or is_top not in {"Y", "N"}:
        return "isTop_enum"
    return None


def api_list_page_signature(page: ListPageResult) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "entries": [
                    {
                        "pkId": entry.get("pkId"),
                        "title": entry.get("title"),
                        "regDate": entry.get("regDate"),
                        "isTop": entry.get("isTop"),
                    }
                    for entry in page.entries
                ],
                "total_count": page.total_count,
                "has_more": page.has_more,
                "terminal_verified": page.terminal_verified,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def crawl_top_items_api_result(
    source: SourceSpec,
    include_non_top: bool,
    non_top_max_pages: int,
    known_ids: Optional[set[str]] = None,
    incremental: bool = False,
    reconcile_mode: bool = False,
    refresh_known_ids: Optional[set[str]] = None,
    resume_page: int = 1,
    resume_anchor_ids: Optional[set[str]] = None,
) -> SourceCrawlResult:
    config_fk = source.config_fk
    items: JsonObjectList = []
    seen: set[str] = set()
    observed_ids: list[str] = []
    observed_id_set: set[str] = set()
    raw_observed_ids: set[str] = set()
    top_urls: set[str] = set()
    top_dates: dict[str, set[str]] = {}
    observed_top_ids: set[str] = set()
    page_fingerprints: set[str] = set()
    page_id_sequences: set[tuple[tuple[str, bool], ...]] = set()
    stable_page_signatures: dict[int, str] = {}
    seen_identities: dict[str, tuple[bool, str, str, str]] = {}
    detail_failures = 0
    rejected_count = 0
    pages_scanned = 0
    known_ids = known_ids or set()
    refresh_known_ids = refresh_known_ids or set()
    refreshed_known_ids: list[str] = []
    checkpoint_found = not incremental or not known_ids
    terminal_error = ""
    terminal_category = FailureCategory.NONE
    terminal_reached = False
    termination_reason = ""
    retry_after_seconds: Optional[float] = None
    page_number = 1
    previous_page_count: Optional[int] = None
    expected_total_count: Optional[int] = None
    fetched_detail_count = 0
    first_page_top_verified = False
    backfill_detail_limit = get_backfill_detail_limit()
    resume_page = max(1, resume_page)
    resume_anchor_ids = resume_anchor_ids or set()
    resume_active = bool(resume_page > 2 and resume_anchor_ids)
    resume_search_start = max(2, resume_page - 2)
    resume_search_end = resume_page + 2
    resume_anchor_found = not resume_active
    resume_jump_done = not resume_active
    next_resume_page = 1
    next_anchor_ids: list[str] = []
    checkpoint_page_number: Optional[int] = None
    checkpoint_overlap_pages = 0
    checkpoint_overlap_required = (
        get_incremental_checkpoint_overlap_pages()
    )
    required_refresh_ids = refresh_known_ids & known_ids
    classification = source.classification
    page_size_raw = os.environ.get("BBS_PAGE_SIZE", "20")
    try:
        page_size = max(1, int(page_size_raw))
    except ValueError:
        page_size = 20
    hard_page_limit = get_hard_page_limit()
    request_count = 0
    started_at = time.monotonic()
    max_requests = get_api_request_budget()
    time_budget = get_api_time_budget_seconds()

    def consume_api_budget() -> bool:
        nonlocal request_count, terminal_error
        nonlocal terminal_category, termination_reason
        check_run_control()
        shared_budget = CURRENT_SOURCE_REQUEST_BUDGET.get()
        if shared_budget is not None:
            if shared_budget.consume_logical("api", max_requests):
                request_count += 1
                return True
            terminal_error = shared_budget.exhausted_reason
            terminal_category = FailureCategory.SOURCE_PARTIAL
            termination_reason = (
                "time_budget"
                if "time_budget" in terminal_error
                else "request_budget"
            )
            return False
        if request_count >= max_requests:
            terminal_error = f"api_request_budget_exceeded:{max_requests}"
            terminal_category = FailureCategory.SOURCE_PARTIAL
            termination_reason = "request_budget"
            return False
        if time.monotonic() - started_at >= time_budget:
            terminal_error = f"api_time_budget_exceeded:{int(time_budget)}"
            terminal_category = FailureCategory.SOURCE_PARTIAL
            termination_reason = "time_budget"
            return False
        request_count += 1
        return True

    while True:
        check_run_control()
        if pages_scanned >= hard_page_limit:
            terminal_error = f"hard_page_limit_reached:{hard_page_limit}"
            terminal_category = FailureCategory.SOURCE_PARTIAL
            termination_reason = "hard_cap"
            break
        if (
            not incremental
            and include_non_top
            and non_top_max_pages > 0
            and page_number > non_top_max_pages
        ):
            terminal_error = (
                f"configured_page_limit_reached:{non_top_max_pages}"
            )
            terminal_category = FailureCategory.SOURCE_PARTIAL
            termination_reason = "configured_cap"
            break
        if not consume_api_budget():
            break
        LOGGER.info("페이지 로드 시작(API): %s", page_number)
        page_result = fetch_bbs_list_result(page_number, page_size, config_fk=config_fk)
        if not page_result.ok:
            terminal_error = page_result.error or "list_fetch_failed"
            terminal_category = page_result.category
            termination_reason = "page_error"
            retry_after_seconds = page_result.retry_after_seconds
            if page_number == 1:
                return SourceCrawlResult(
                    source=source,
                    status=SourceStatus.FAILED,
                    method="api",
                    category=terminal_category,
                    error=terminal_error,
                    pages_scanned=0,
                    checkpoint_found=checkpoint_found,
                    termination_reason=termination_reason,
                    retry_after_seconds=retry_after_seconds,
                )
            break
        page_entries = page_result.entries
        pages_scanned += 1
        if page_result.total_count is not None:
            if expected_total_count is None:
                expected_total_count = page_result.total_count
            elif expected_total_count != page_result.total_count:
                terminal_error = "pagination_total_changed"
                terminal_category = FailureCategory.SOURCE_CONTRACT
                termination_reason = "page_error"
                break
            page_ids = {
                str(entry.get("pkId") or "").strip()
                for entry in page_entries
                if (
                    str(entry.get("pkId") or "").strip()
                    and str(entry.get("isTop") or "").upper() != "Y"
                )
            }
            if page_result.total_count < len(raw_observed_ids | page_ids):
                terminal_error = "pagination_total_below_observed"
                terminal_category = FailureCategory.SOURCE_CONTRACT
                termination_reason = "page_error"
                break
        LOGGER.info("페이지 %s 항목 수(API): %s", page_number, len(page_entries))
        page_non_top_ids = {
            str(entry.get("pkId") or "").strip()
            for entry in page_entries
            if (
                str(entry.get("pkId") or "").strip()
                and str(entry.get("isTop") or "").upper() != "Y"
            )
        }
        if (
            resume_active
            and page_number >= resume_search_start
            and page_non_top_ids & resume_anchor_ids
        ):
            resume_anchor_found = True
        if (
            resume_active
            and not resume_anchor_found
            and page_number > resume_search_end
        ):
            terminal_error = "backfill_resume_anchor_missing"
            terminal_category = FailureCategory.SOURCE_PARTIAL
            termination_reason = "resume_error"
            break
        if not page_entries:
            if resume_active and not resume_anchor_found:
                terminal_error = "backfill_resume_anchor_missing"
                terminal_category = FailureCategory.SOURCE_PARTIAL
                termination_reason = "resume_error"
                break
            terminal_confirmed = page_result.terminal_verified
            if not consume_api_budget():
                break
            confirmation = fetch_bbs_list_result(
                page_number,
                page_size,
                config_fk=config_fk,
            )
            if not confirmation.ok:
                terminal_error = (
                    confirmation.error
                    or "empty_page_confirmation_failed"
                )
                terminal_category = confirmation.category
                retry_after_seconds = confirmation.retry_after_seconds
                termination_reason = "page_error"
                break
            if confirmation.entries:
                terminal_error = (
                    f"empty_page_confirmation_mismatch:{page_number}"
                )
                terminal_category = FailureCategory.SOURCE_PARTIAL
                termination_reason = "page_error"
                break
            if (
                page_result.total_count != confirmation.total_count
                or page_result.has_more != confirmation.has_more
            ):
                terminal_error = (
                    f"empty_page_metadata_changed:{page_number}"
                )
                terminal_category = FailureCategory.SOURCE_CONTRACT
                termination_reason = "page_error"
                break
            terminal_confirmed = bool(
                terminal_confirmed
                or confirmation.terminal_verified
                or page_number == 1
                or (
                    previous_page_count is not None
                    and previous_page_count < page_size
                )
            )
            if not page_entries and not terminal_confirmed:
                terminal_error = f"unverified_empty_page:{page_number}"
                terminal_category = FailureCategory.SOURCE_PARTIAL
                termination_reason = "page_error"
                break
            if (
                not page_entries
                and page_number == 1
                and expected_total_count not in {None, 0}
            ):
                terminal_error = "pagination_total_mismatch"
                terminal_category = FailureCategory.SOURCE_CONTRACT
                termination_reason = "page_error"
                break
            if not page_entries and page_number == 1:
                return SourceCrawlResult(
                    source=source,
                    status=SourceStatus.VALID_EMPTY,
                    method="api",
                    pages_scanned=1,
                    observed_count=0,
                    checkpoint_found=True,
                    terminal_reached=True,
                    termination_reason="natural_end",
                    full_snapshot=reconcile_mode,
                    reconcile_complete=reconcile_mode,
                    coverage_complete=reconcile_mode,
                    top_snapshot_verified=True,
                )
            if not page_entries:
                if (
                    include_non_top
                    and expected_total_count is not None
                    and len(raw_observed_ids | known_ids)
                    < expected_total_count
                ):
                    terminal_error = "pagination_total_mismatch"
                    terminal_category = FailureCategory.SOURCE_CONTRACT
                    termination_reason = "page_error"
                    break
                checkpoint_found = True
                terminal_reached = True
                termination_reason = "natural_end"
                break
        raw_observed_ids.update(
            str(entry.get("pkId") or "").strip()
            for entry in page_entries
            if (
                str(entry.get("pkId") or "").strip()
                and str(entry.get("isTop") or "").upper() != "Y"
            )
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                [
                    (
                        str(entry.get("pkId") or ""),
                        str(entry.get("title") or ""),
                        str(entry.get("regDate") or ""),
                    )
                    for entry in page_entries
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if fingerprint in page_fingerprints:
            terminal_error = f"repeated_page:{page_number}"
            terminal_category = FailureCategory.SOURCE_PARTIAL
            termination_reason = "page_error"
            break
        page_fingerprints.add(fingerprint)
        schema_error = next(
            (
                error
                for entry in page_entries
                if (error := validate_api_list_entry(entry))
            ),
            None,
        )
        if schema_error:
            terminal_error = f"list_entry_contract:{schema_error}"
            terminal_category = FailureCategory.SOURCE_CONTRACT
            termination_reason = "page_error"
            break
        stable_page_signatures[page_number] = api_list_page_signature(
            page_result
        )
        id_sequence = tuple(
            (
                str(entry.get("pkId") or "").strip(),
                str(entry.get("isTop") or "").upper() == "Y",
            )
            for entry in page_entries
        )
        if id_sequence in page_id_sequences:
            terminal_error = f"repeated_page_ids:{page_number}"
            terminal_category = FailureCategory.SOURCE_PARTIAL
            termination_reason = "page_error"
            break
        page_id_sequences.add(id_sequence)
        non_top_seen_on_page = False
        invalid_top_order = False
        for entry in page_entries:
            entry_top = str(entry.get("isTop") or "").upper() == "Y"
            if entry_top and non_top_seen_on_page:
                invalid_top_order = True
                break
            if not entry_top:
                non_top_seen_on_page = True
        if invalid_top_order:
            terminal_error = f"top_order_violation:{page_number}"
            terminal_category = FailureCategory.SOURCE_CONTRACT
            termination_reason = "page_error"
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
        page_has_checkpoint = False
        page_has_unknown_notice = False
        page_contract_failed = False

        for entry in entries_to_process:
            check_run_control()
            pk_id = str(entry.get("pkId") or "").strip()
            if not pk_id:
                rejected_count += 1
                continue
            if pk_id not in observed_id_set:
                observed_id_set.add(pk_id)
                observed_ids.append(pk_id)
            detail_url = normalize_detail_url(
                build_detail_url(pk_id, config_fk)
            ) or build_detail_url(pk_id, config_fk)
            title_from_list = normalize_title_key(str(entry.get("title") or ""))
            date_from_list = parse_compact_datetime(entry.get("regDate"))
            top = str(entry.get("isTop", "")).upper() == "Y"
            identity = (
                top,
                title_from_list,
                str(date_from_list or ""),
                detail_url,
            )
            existing_identity = seen_identities.get(pk_id)
            repeated_top = existing_identity is not None and top
            if existing_identity is not None:
                if existing_identity != identity or not top:
                    terminal_error = f"notice_identity_collision:{pk_id}"
                    terminal_category = FailureCategory.SOURCE_CONTRACT
                    termination_reason = "page_error"
                    page_contract_failed = True
                    break
            else:
                seen_identities[pk_id] = identity
                if pk_id not in known_ids:
                    page_has_unknown_notice = True
            if top:
                observed_top_ids.add(pk_id)
                top_urls.add(detail_url)
                if title_from_list:
                    top_dates.setdefault(title_from_list, set()).add(
                        normalize_date_key(date_from_list)
                    )
            if repeated_top:
                continue
            if incremental and pk_id in known_ids and not top:
                page_has_checkpoint = True
            if (
                incremental
                and pk_id in known_ids
                and pk_id not in refresh_known_ids
            ):
                continue
            if not consume_api_budget():
                page_contract_failed = True
                break
            try:
                detail = fetch_bbs_detail(pk_id, config_fk=config_fk)
            except SourceAccessBlocked as exc:
                terminal_error = exc.error
                terminal_category = FailureCategory.SECURITY_POLICY
                termination_reason = "detail_error"
                retry_after_seconds = exc.retry_after_seconds
                page_contract_failed = True
                break
            if not consume_api_budget():
                page_contract_failed = True
                break
            try:
                detail_confirmation = fetch_bbs_detail(
                    pk_id,
                    config_fk=config_fk,
                )
            except SourceAccessBlocked as exc:
                terminal_error = exc.error
                terminal_category = FailureCategory.SECURITY_POLICY
                termination_reason = "detail_error"
                retry_after_seconds = exc.retry_after_seconds
                page_contract_failed = True
                break
            if api_detail_signature(detail_confirmation) != api_detail_signature(
                detail
            ):
                detail_failures += 1
                terminal_error = f"detail_snapshot_changed:{pk_id}"
                terminal_category = FailureCategory.SOURCE_PARTIAL
                termination_reason = "detail_error"
                page_contract_failed = True
                break
            detail_fetch_status = "api"
            fallback_written_at: Optional[str] = None
            fallback_attachments: JsonObjectList = []
            fallback_body_blocks: JsonObjectList = []
            fallback_attachment_status = ATTACHMENTS_STATUS_UNKNOWN
            fallback_body_status = BODY_STATUS_UNKNOWN
            fallback_reason = get_detail_html_fallback_reason(
                detail,
                entry_title=str(entry.get("title") or ""),
            )
            if fallback_reason:
                if not consume_api_budget():
                    page_contract_failed = True
                    break
                detail = detail if isinstance(detail, dict) else {}
                try:
                    (
                        fallback_written_at,
                        fallback_attachments,
                        fallback_body_blocks,
                        detail_fetch_status,
                        fallback_attachment_status,
                        fallback_body_status,
                    ) = fetch_detail_metadata_with_html_fallback(
                        pk_id,
                        detail_url,
                        fallback_reason,
                    )
                except SourceAccessBlocked as exc:
                    terminal_error = exc.error
                    terminal_category = FailureCategory.SECURITY_POLICY
                    termination_reason = "detail_error"
                    retry_after_seconds = exc.retry_after_seconds
                    page_contract_failed = True
                    break

            detail_data = detail if isinstance(detail, dict) else {}

            title = normalize_title_key(detail_data.get("title") or entry.get("title") or "")
            author = detail_data.get("userName") or entry.get("userName") or entry.get("userNickName") or ""
            written_at = parse_compact_datetime(detail_data.get("regDate") or entry.get("regDate"))
            if not written_at and fallback_written_at:
                written_at = fallback_written_at
            views_raw = detail_data.get("viewCount", entry.get("viewCount"))
            views = parse_int(str(views_raw)) if views_raw is not None else None
            if not include_non_top and not top:
                continue

            attachments = extract_attachments_from_api_data(detail_data or entry)
            if not attachments and fallback_attachments:
                attachments = fallback_attachments
            attachment_status = classify_attachment_status_from_api_detail(
                detail_data,
                attachments,
                fallback_reason,
                fallback_attachment_status,
            )
            fallback_empty_attachment_confirmed = bool(
                fallback_reason
                and "attachment_schema_missing"
                in fallback_reason.split(",")
                and fallback_attachment_status
                == ATTACHMENTS_STATUS_KNOWN
                and not fallback_attachments
            )
            if (
                not attachments
                and attachment_status == ATTACHMENTS_STATUS_KNOWN
                and not fallback_empty_attachment_confirmed
            ):
                confirmation_data = (
                    detail_confirmation
                    if isinstance(detail_confirmation, dict)
                    else {}
                )
                confirmation_title = normalize_title_key(
                    str(confirmation_data.get("title") or "")
                )
                confirmation_date = parse_compact_datetime(
                    confirmation_data.get("regDate")
                )
                if (
                    not api_has_explicit_empty_attachment_fields(
                        confirmation_data
                    )
                    or (
                        confirmation_title
                        and title_from_list
                        and confirmation_title != title_from_list
                    )
                    or (
                        confirmation_date
                        and date_from_list
                        and confirmation_date != date_from_list
                    )
                ):
                    attachment_status = ATTACHMENTS_STATUS_UNKNOWN
            content_html = detail_data.get("content") or ""
            body_blocks = extract_body_blocks_from_html(content_html) if content_html else []
            if not body_blocks and fallback_body_blocks:
                body_blocks = fallback_body_blocks
            body_status = (
                BODY_STATUS_PRESENT
                if body_blocks
                else fallback_body_status
            )
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
            if detail_fetch_status != "api":
                item["detail_fetch_status"] = detail_fetch_status
            if body_blocks:
                item["body_blocks"] = body_blocks
            item["body_status"] = body_status
            if classification:
                item["classification"] = classification
            ensure_item_title(item, body_blocks, detail_url)
            apply_item_attachments(item, attachments, attachment_status)
            detail_title = normalize_title_key(
                str(detail_data.get("title") or "")
            )
            detail_date = parse_compact_datetime(detail_data.get("regDate"))
            item_complete = bool(
                (title_from_list or detail_title)
                and is_plausible_notice_datetime(item.get("date"))
                and body_status
                in {BODY_STATUS_PRESENT, BODY_STATUS_CONFIRMED_EMPTY}
                and item.get("attachments_status")
                == ATTACHMENTS_STATUS_KNOWN
            )
            if (
                detail_title
                and title_from_list
                and detail_title != title_from_list
            ) or (
                detail_date
                and date_from_list
                and detail_date != date_from_list
            ):
                item_complete = False
                item["identity_mismatch"] = True
            item["completeness"] = (
                ItemCompleteness.COMPLETE.value
                if item_complete
                else ItemCompleteness.PARTIAL.value
            )
            if not item_complete:
                detail_failures += 1

            key = detail_url or f"{item['title']}|{written_at or ''}"
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
            new_count += 1
            fetched_detail_count += 1
            if pk_id in known_ids:
                refreshed_known_ids.append(pk_id)

        if page_contract_failed:
            break
        LOGGER.info("페이지 %s 신규 수집 수(API): %s", page_number, new_count)
        if page_has_checkpoint:
            checkpoint_found = True
            if checkpoint_page_number is None:
                checkpoint_page_number = page_number
        if page_result.terminal_verified:
            if (
                include_non_top
                and expected_total_count is not None
                and len(
                    raw_observed_ids
                    | (known_ids - observed_top_ids)
                )
                < expected_total_count
            ):
                terminal_error = "pagination_total_mismatch"
                terminal_category = FailureCategory.SOURCE_CONTRACT
                termination_reason = "page_error"
                break
            checkpoint_found = True
            terminal_reached = True
            termination_reason = "natural_end"
            break
        if not include_non_top:
            has_non_top = any(
                str(entry.get("isTop", "")).upper() != "Y" for entry in page_entries
            )
            if has_non_top:
                checkpoint_found = True
                terminal_reached = True
                termination_reason = "non_top_boundary"
                LOGGER.info("페이지 %s에서 비TOP 발견, 다음 페이지 탐색 중단(API)", page_number)
                break
        if (
            reconcile_mode
            and include_non_top
            and fetched_detail_count >= backfill_detail_limit
        ):
            checkpoint_found = True
            terminal_reached = True
            termination_reason = "backfill_window"
            next_resume_page = page_number + 1
            next_anchor_ids = sorted(page_non_top_ids)
            break
        if (
            resume_active
            and not resume_jump_done
            and checkpoint_found
        ):
            resume_search_end += max(0, page_number - 1)
            page_number = max(
                page_number + 1,
                resume_search_start,
            )
            resume_jump_done = True
            previous_page_count = None
            continue
        if (
            incremental
            and not reconcile_mode
            and checkpoint_page_number is not None
            and page_number > checkpoint_page_number
        ):
            if page_has_unknown_notice:
                checkpoint_overlap_pages = 0
            elif page_has_checkpoint:
                checkpoint_overlap_pages += 1
            else:
                checkpoint_overlap_pages = 0
            if (
                checkpoint_overlap_pages
                >= checkpoint_overlap_required
                and required_refresh_ids.issubset(
                    set(refreshed_known_ids)
                )
                and (
                    expected_total_count is None
                    or len(
                        raw_observed_ids
                        | (known_ids - observed_top_ids)
                    )
                    >= expected_total_count
                )
            ):
                terminal_reached = True
                termination_reason = "incremental_checkpoint"
                break
        previous_page_count = len(page_entries)
        page_number += 1

    if terminal_reached and not terminal_error:
        for stable_page_number, expected_signature in stable_page_signatures.items():
            if not consume_api_budget():
                break
            confirmation = fetch_bbs_list_result(
                stable_page_number,
                page_size,
                config_fk=config_fk,
            )
            schema_error = next(
                (
                    error
                    for entry in confirmation.entries
                    if (error := validate_api_list_entry(entry))
                ),
                None,
            )
            if (
                not confirmation.ok
                or schema_error
                or api_list_page_signature(confirmation)
                != expected_signature
            ):
                terminal_error = (
                    f"api_snapshot_changed:{stable_page_number}"
                )
                terminal_category = FailureCategory.SOURCE_PARTIAL
                if not confirmation.ok:
                    terminal_category = confirmation.category
                    retry_after_seconds = (
                        confirmation.retry_after_seconds
                    )
                termination_reason = "snapshot_changed"
                break
            if stable_page_number == 1:
                first_page_top_verified = True

    status = SourceStatus.SUCCESS
    category = terminal_category
    error = terminal_error
    if terminal_error or detail_failures or rejected_count or not checkpoint_found:
        status = SourceStatus.DEGRADED
        if category == FailureCategory.NONE:
            category = FailureCategory.SOURCE_PARTIAL
        if not error:
            if not checkpoint_found:
                error = "incremental_checkpoint_not_found"
            elif detail_failures:
                error = f"detail_failures:{detail_failures}"
            else:
                error = f"rejected_entries:{rejected_count}"
    return SourceCrawlResult(
        source=source,
        status=status,
        items=items,
        method="api",
        pages_scanned=pages_scanned,
        observed_count=(
            len(set(observed_ids) | known_ids)
            if resume_active and termination_reason == "natural_end"
            else len(observed_ids)
        ),
        observed_ids=observed_ids,
        refreshed_known_ids=refreshed_known_ids,
        top_urls=sorted(top_urls),
        top_dates={key: sorted(values) for key, values in top_dates.items()},
        category=category,
        error=error,
        detail_failures=detail_failures,
        rejected_count=rejected_count,
        checkpoint_found=checkpoint_found,
        terminal_reached=terminal_reached,
        termination_reason=termination_reason,
        full_snapshot=(
            reconcile_mode
            and len(stable_page_signatures) <= 1
            and status == SourceStatus.SUCCESS
            and termination_reason
            in {"natural_end", "non_top_boundary"}
        ),
        reconcile_complete=(
            reconcile_mode
            and len(stable_page_signatures) <= 1
            and status == SourceStatus.SUCCESS
            and termination_reason
            in {"natural_end", "non_top_boundary"}
        ),
        coverage_complete=(
            reconcile_mode
            and status == SourceStatus.SUCCESS
            and termination_reason
            in {"natural_end", "non_top_boundary"}
        ),
        top_snapshot_verified=(
            first_page_top_verified
            and status == SourceStatus.SUCCESS
        ),
        retry_after_seconds=retry_after_seconds,
        backfill_resume_page=next_resume_page,
        backfill_anchor_ids=next_anchor_ids,
    )


def crawl_top_items_api(
    config_fk: str,
    include_non_top: bool,
    non_top_max_pages: int,
) -> JsonObjectList:
    items: JsonObjectList = crawl_top_items_api_result(
        build_source_spec(config_fk),
        include_non_top,
        non_top_max_pages,
    ).items
    return items


def crawl_top_items_playwright(
    config_fk: str,
    include_non_top: bool,
    non_top_max_pages: int,
) -> JsonObjectList:
    source = build_source_spec(config_fk)
    original = SourceCrawlResult(
        source=source,
        status=SourceStatus.FAILED,
        method="playwright_legacy_entry",
        category=FailureCategory.SOURCE_UPSTREAM,
        error="playwright_legacy_entry",
    )
    items: JsonObjectList = crawl_top_items_playwright_result(
        source,
        include_non_top,
        non_top_max_pages,
        set(),
        False,
        original,
    ).items
    return items


def build_result_from_fallback_items(
    source: SourceSpec,
    items: JsonObjectList,
    method: str,
    original: SourceCrawlResult,
) -> SourceCrawlResult:
    if not items:
        return original
    observed_ids: list[str] = []
    top_urls: set[str] = set()
    top_dates: dict[str, set[str]] = {}
    detail_failures = 0
    for item in items:
        detail_id = extract_detail_id_from_text(str(item.get("url") or ""))
        if detail_id:
            observed_ids.append(detail_id)
        if item.get("top") and item.get("url"):
            top_urls.add(str(item["url"]))
            title = str(item.get("title") or "").strip()
            if title:
                top_dates.setdefault(title, set()).add(
                    normalize_date_key(item.get("date"))
                )
        if not item.get("title") or not item.get("url") or not item.get("date"):
            detail_failures += 1
    status = SourceStatus.DEGRADED
    return SourceCrawlResult(
        source=source,
        status=status,
        items=items,
        method=method,
        observed_count=len(items),
        observed_ids=observed_ids,
        top_urls=sorted(top_urls),
        top_dates={key: sorted(values) for key, values in top_dates.items()},
        category=FailureCategory.SOURCE_PARTIAL,
        error=(
            f"fallback_unconfirmed:{original.error or 'partial'}"
            if detail_failures == 0
            else f"detail_failures:{detail_failures}"
        ),
        detail_failures=detail_failures,
    )


class SogangSourceAdapter:
    def crawl(
        self,
        source: SourceSpec,
        known_ids: Optional[set[str]] = None,
        incremental: bool = False,
        source_state: Optional[dict[str, Any]] = None,
        reconcile_mode: bool = False,
        refresh_known_ids: Optional[set[str]] = None,
        resume_page: int = 1,
        resume_anchor_ids: Optional[set[str]] = None,
    ) -> SourceCrawlResult:
        source_cooldown_until = str(
            (source_state or {}).get("source_circuit_open_until") or ""
        )
        try:
            source_cooldown = datetime.fromisoformat(
                source_cooldown_until.replace("Z", "+00:00")
            )
            if source_cooldown.tzinfo is None:
                source_cooldown = source_cooldown.replace(
                    tzinfo=timezone.utc
                )
        except ValueError:
            source_cooldown = None
        if (
            source_cooldown is not None
            and source_cooldown.astimezone(timezone.utc)
            > datetime.now(timezone.utc)
        ):
            return SourceCrawlResult(
                source=source,
                status=SourceStatus.FAILED,
                method="source_circuit_open",
                category=FailureCategory.SECURITY_POLICY,
                error="source_circuit_open",
                termination_reason="circuit_open",
            )
        if CURRENT_SOURCE_REQUEST_BUDGET.get() is None:
            with source_request_budget_scope():
                return self._crawl_active(
                    source,
                    known_ids,
                    incremental,
                    source_state,
                    reconcile_mode,
                    refresh_known_ids,
                    resume_page,
                    resume_anchor_ids,
                )
        return self._crawl_active(
            source,
            known_ids,
            incremental,
            source_state,
            reconcile_mode,
            refresh_known_ids,
            resume_page,
            resume_anchor_ids,
        )

    def _crawl_active(
        self,
        source: SourceSpec,
        known_ids: Optional[set[str]],
        incremental: bool,
        source_state: Optional[dict[str, Any]],
        reconcile_mode: bool,
        refresh_known_ids: Optional[set[str]],
        resume_page: int,
        resume_anchor_ids: Optional[set[str]],
    ) -> SourceCrawlResult:
        include_non_top = should_include_non_top()
        non_top_max_pages = get_non_top_max_pages()
        api_result = crawl_top_items_api_result(
            source,
            include_non_top,
            non_top_max_pages,
            known_ids=known_ids,
            incremental=incremental,
            reconcile_mode=reconcile_mode,
            refresh_known_ids=refresh_known_ids,
            resume_page=resume_page,
            resume_anchor_ids=resume_anchor_ids,
        )
        if api_result.write_safe:
            return api_result
        if api_result.category == FailureCategory.SECURITY_POLICY:
            api_result.method = "api_source_circuit_open"
            return api_result
        cooldown_until = str(
            (source_state or {}).get("fallback_circuit_open_until") or ""
        )
        try:
            cooldown = datetime.fromisoformat(
                cooldown_until.replace("Z", "+00:00")
            )
            if cooldown.tzinfo is None:
                cooldown = cooldown.replace(tzinfo=timezone.utc)
        except ValueError:
            cooldown = None
        if (
            cooldown is not None
            and cooldown.astimezone(timezone.utc)
            > datetime.now(timezone.utc)
        ):
            api_result.method = "api_fallback_circuit_open"
            api_result.error = ";".join(
                value
                for value in (
                    api_result.error,
                    "fallback_circuit_open",
                )
                if value
            )
            return api_result
        return crawl_top_items_playwright_result(
            source,
            include_non_top,
            non_top_max_pages,
            known_ids or set(),
            incremental,
            api_result,
            reconcile_mode,
            refresh_known_ids,
            resume_page,
            resume_anchor_ids,
        )


def crawl_sources(
    known_ids_by_source: Optional[dict[str, set[str]]] = None,
    source_state_by_source: Optional[dict[str, dict[str, Any]]] = None,
    incremental: bool = False,
    incremental_by_source: Optional[dict[str, bool]] = None,
    reconcile_mode: bool = False,
    reconcile_mode_by_source: Optional[dict[str, bool]] = None,
    refresh_ids_by_source: Optional[dict[str, set[str]]] = None,
    resume_page_by_source: Optional[dict[str, int]] = None,
    resume_anchor_ids_by_source: Optional[dict[str, set[str]]] = None,
) -> CrawlReport:
    include_non_top = should_include_non_top()
    non_top_max_pages = get_non_top_max_pages()
    if include_non_top:
        limit_label = "제한없음" if non_top_max_pages <= 0 else str(non_top_max_pages)
        LOGGER.info("비TOP 포함 모드: 최대 페이지=%s", limit_label)

    config_fks = get_bbs_config_fks()
    if not config_fks:
        return CrawlReport(sources=[])
    if len(config_fks) != len(set(config_fks)):
        raise RuntimeError("BBS_CONFIG_FKS에 중복 출처가 있습니다")

    adapter = SogangSourceAdapter()
    sources = [build_source_spec(config_fk) for config_fk in config_fks]
    source_order = {
        source.config_fk: index
        for index, source in enumerate(sources)
    }
    execution_sources = sorted(
        sources,
        key=lambda source: (
            not bool(
                (source_state_by_source or {})
                .get(source.config_fk, {})
                .get("backfill_active")
            ),
            not bool(
                (reconcile_mode_by_source or {}).get(
                    source.config_fk,
                    reconcile_mode,
                )
            ),
            source_order[source.config_fk],
        ),
    )
    results_by_source: dict[str, SourceCrawlResult] = {}
    blocked_hosts: dict[str, tuple[str, Optional[float]]] = {}
    now = datetime.now(timezone.utc)
    for source in sources:
        source_state = (source_state_by_source or {}).get(
            source.config_fk,
            {},
        )
        raw_until = str(source_state.get("source_circuit_open_until") or "")
        try:
            open_until = datetime.fromisoformat(
                raw_until.replace("Z", "+00:00")
            )
            if open_until.tzinfo is None:
                open_until = open_until.replace(tzinfo=timezone.utc)
        except ValueError:
            open_until = None
        host = (urlparse(source.list_url).hostname or "").lower()
        circuit_reason = str(
            source_state.get("source_circuit_reason") or ""
        )
        if (
            host
            and open_until is not None
            and open_until > now
            and is_host_wide_access_block(circuit_reason)
        ):
            blocked_hosts[host] = (
                circuit_reason,
                max(0.0, (open_until - now).total_seconds()),
            )

    request_limit = SourceRequestBudget._integer_env(
        "SOURCE_MAX_REQUESTS",
        3000,
        1,
        25000,
    )
    request_share = max(1, request_limit // len(execution_sources))
    for index, source in enumerate(execution_sources):
        remaining_sources = len(execution_sources) - index
        remaining_seconds = remaining_run_seconds()
        fair_seconds: Optional[float] = None
        if remaining_seconds is not None:
            fair_seconds = max(
                0.0,
                (
                    remaining_seconds
                    - get_destination_state_reserve_seconds()
                )
                / remaining_sources,
            )
        with source_request_budget_scope(
            max_seconds_cap=fair_seconds,
            max_requests_cap=request_share,
        ):
            check_run_control()
            config_fk = source.config_fk
            source_reconcile_mode = (
                reconcile_mode_by_source or {}
            ).get(config_fk, reconcile_mode)
            host = (urlparse(source.list_url).hostname or "").lower()
            LOGGER.info(
                "수집 설정: bbsConfigFk=%s, 분류=%s",
                config_fk,
                source.classification or "없음",
            )
            blocked = blocked_hosts.get(host)
            if blocked is not None:
                reason, retry_after_seconds = blocked
                result = SourceCrawlResult(
                    source=source,
                    status=SourceStatus.FAILED,
                    method="host_circuit_open",
                    category=FailureCategory.SECURITY_POLICY,
                    error=reason,
                    termination_reason="circuit_open",
                    retry_after_seconds=retry_after_seconds,
                )
            else:
                result = adapter.crawl(
                    source,
                    known_ids=(known_ids_by_source or {}).get(
                        config_fk,
                        set(),
                    ),
                    incremental=(
                        (incremental_by_source or {}).get(
                            config_fk,
                            incremental,
                        )
                    ),
                    source_state=(source_state_by_source or {}).get(
                        config_fk,
                        {},
                    ),
                    reconcile_mode=source_reconcile_mode,
                    refresh_known_ids=(
                        refresh_ids_by_source or {}
                    ).get(config_fk, set()),
                    resume_page=(
                        resume_page_by_source or {}
                    ).get(config_fk, 1),
                    resume_anchor_ids=(
                        resume_anchor_ids_by_source or {}
                    ).get(config_fk, set()),
                )
                if (
                    host
                    and result.category
                    == FailureCategory.SECURITY_POLICY
                    and is_host_wide_access_block(result.error)
                ):
                    blocked_hosts[host] = (
                        result.error or "host_access_blocked",
                        result.retry_after_seconds,
                    )
            result.reconcile_requested = source_reconcile_mode
            results_by_source[config_fk] = result
            LOGGER.info(
                "출처 수집 결과: bbsConfigFk=%s, 상태=%s, 방식=%s, 항목=%s, 관측=%s",
                config_fk,
                result.status.value,
                result.method,
                len(result.items),
                result.observed_count,
            )
    return CrawlReport(
        sources=[
            results_by_source[source.config_fk]
            for source in sources
        ]
    )


def is_host_wide_access_block(error: str) -> bool:
    normalized = str(error or "").strip().lower()
    return normalized in {
        "rate_limited",
        "access_challenge",
        "fallback_http_access_challenge",
        "fallback_browser_access_challenge",
        "fallback_browser_http_429",
    }


def crawl_top_items() -> JsonObjectList:
    items: JsonObjectList = crawl_sources().items
    return items


def crawl_top_items_http(
    config_fk: str,
    include_non_top: bool,
    non_top_max_pages: int,
) -> JsonObjectList:
    items: JsonObjectList = []
    seen = set()
    page_number = 1
    base_url = get_list_base_url(config_fk)
    classification = get_classification_for_config(config_fk)

    while True:
        check_run_control()
        if page_number > get_hard_page_limit():
            LOGGER.warning("HTTP 페이지 절대 상한 도달: %s", page_number - 1)
            break
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
            body_blocks: JsonObjectList = []
            attachments: JsonObjectList = []
            attachment_status = ATTACHMENTS_STATUS_UNKNOWN
            if item.get("url"):
                written_at, attachments, body_blocks, signals = fetch_detail_metadata_from_url(
                    item["url"]
                )
                attachment_status = classify_attachment_status_from_signals(
                    attachments,
                    signals,
                )
                if written_at:
                    item["date"] = written_at
                if body_blocks:
                    item["body_blocks"] = body_blocks
            if classification:
                item["classification"] = classification
            ensure_item_title(item, body_blocks, item.get("url"))
            apply_item_attachments(item, attachments, attachment_status)
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

def fetch_html(url: str) -> Optional[str]:
    result = fetch_site_result(url, "상세 HTML 요청")
    if not result.ok and result.category == FailureCategory.SECURITY_POLICY:
        raise SourceAccessBlocked(
            result.error or "source_access_blocked",
            result.status_code,
            result.retry_after_seconds,
        )
    if not result.ok or result.body is None:
        return None
    content_type = result.content_type.lower()
    if content_type and not any(
        expected in content_type
        for expected in ("text/html", "application/xhtml+xml")
    ):
        return None
    body: bytes = result.body
    return body.decode("utf-8", errors="replace")


def strip_html_for_attachment_text(html_text: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", html_text or "")).strip()


def detect_attachment_evidence_from_html(html_text: str) -> bool:
    if not html_text:
        return False
    return bool(extract_attachments_from_detail(html_text))


def build_detail_signals(html_text: str) -> DetailSignals:
    normalized = strip_html_for_attachment_text(html_text).lower()
    error_markers = (
        "404 not found",
        "page you have requested is not available",
        "service unavailable",
        "bad gateway",
        "access denied",
    )
    has_error_marker = any(marker in normalized for marker in error_markers)
    body_container_seen, body_has_content = inspect_body_content(html_text)
    attachment_container_seen = detect_attachment_container(html_text)
    has_loading_shell = detect_loading_shell(html_text)
    has_detail_evidence = bool(
        DATE_TIME_PATTERN.search(html_text)
        or body_container_seen
        or attachment_container_seen
        or "/ko/detail/" in html_text
    )
    return {
        "has_html": True,
        "valid_detail": (
            has_detail_evidence
            and not has_error_marker
            and not has_loading_shell
        ),
        "has_error_marker": has_error_marker,
        "has_loading_shell": has_loading_shell,
        "has_attachment_label": attachment_container_seen,
        "has_attachment_container": attachment_container_seen,
        "has_attachment_link": detect_attachment_evidence_from_html(html_text),
        "has_body_container": body_container_seen,
        "body_has_content": body_has_content,
    }


def classify_body_status(
    body_blocks: JsonObjectList,
    signals: DetailSignals,
) -> str:
    if body_blocks:
        return BODY_STATUS_PRESENT
    if (
        signals.get("valid_detail")
        and signals.get("has_body_container")
        and not signals.get("body_has_content")
    ):
        return BODY_STATUS_CONFIRMED_EMPTY
    return BODY_STATUS_UNKNOWN


def should_retry_detail_fetch(
    written_at: Optional[str],
    attachments: JsonObjectList,
    body_blocks: JsonObjectList,
    signals: DetailSignals,
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
        "상세 재시도 판단: %s (사유=%s, 작성일=%s, 첨부=%s, 본문블록=%s, 첨부라벨=%s, 첨부링크=%s, 본문영역=%s, 본문내용=%s)",
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
) -> tuple[
    Optional[str],
    JsonObjectList,
    JsonObjectList,
    DetailSignals,
]:
    html_text = fetch_html(detail_url)
    if not html_text:
        return None, [], [], {
            "has_html": False,
            "valid_detail": False,
            "has_error_marker": False,
            "has_attachment_label": False,
            "has_attachment_link": False,
            "has_body_container": False,
            "body_has_content": False,
        }
    signals = build_detail_signals(html_text)
    if not signals.get("valid_detail"):
        LOGGER.warning("상세 HTML 유효성 검증 실패: %s", detail_url)
        return None, [], [], signals
    if signals.get("has_attachment_label"):
        LOGGER.info("첨부파일 HTML 감지: %s", detail_url)
    written_at = extract_written_at_from_detail(html_text)
    attachments = extract_attachments_from_detail(html_text)
    body_blocks = extract_body_blocks_from_html(html_text)
    if attachments and body_blocks:
        body_blocks = replace_body_image_urls(body_blocks, attachments)
    return written_at, attachments, body_blocks, signals


FALLBACK_EMPTY_MARKERS = (
    "등록된 게시물이 없습니다",
    "등록된 글이 없습니다",
    "등록된 정보가 없습니다",
    "검색 결과가 없습니다",
    "조회된 데이터가 없습니다",
    "no data",
)
FALLBACK_ERROR_MARKERS = (
    "the page you have requested is not available",
    "service unavailable",
    "bad gateway",
    "access denied",
    "captcha",
    "cloudflare",
    "로그인이 필요",
)


def get_fallback_request_budget() -> int:
    raw = os.environ.get("FALLBACK_MAX_REQUESTS", "5000").strip()
    try:
        value = int(raw)
    except ValueError:
        return 5000
    return min(10000, max(1, value))


def get_fallback_time_budget_seconds() -> float:
    raw = os.environ.get("FALLBACK_MAX_SECONDS", "600").strip()
    try:
        value = float(raw)
    except ValueError:
        return 600.0
    return min(1200.0, max(10.0, value))


def get_fallback_min_interval_seconds() -> float:
    raw = os.environ.get(
        "FALLBACK_MIN_INTERVAL_SECONDS",
        "1.0",
    ).strip()
    try:
        value = float(raw)
    except ValueError:
        return 1.0
    return min(5.0, max(0.0, value))


def get_fallback_jitter_seconds() -> float:
    raw = os.environ.get("FALLBACK_JITTER_SECONDS", "0.5").strip()
    try:
        value = float(raw)
    except ValueError:
        return 0.5
    return min(2.0, max(0.0, value))


def extract_detail_title_from_html(html_text: str) -> str:
    candidates: list[str] = []
    for pattern in (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        r'<h[12][^>]*class=["\'][^"\']*(?:title|subject)[^"\']*["\'][^>]*>(.*?)</h[12]>',
        r"<h1[^>]*>(.*?)</h1>",
    ):
        for match in re.finditer(
            pattern,
            html_text,
            re.IGNORECASE | re.DOTALL,
        ):
            candidate = normalize_title_key(
                strip_html_for_attachment_text(unescape(match.group(1)))
            )
            if candidate:
                candidates.append(candidate)
    return candidates[0] if candidates else ""


class FallbackVisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hidden_stack: list[bool] = []
        self.parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, Optional[str]]],
    ) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        parent_hidden = self.hidden_stack[-1] if self.hidden_stack else False
        style = attrs_dict.get("style", "").replace(" ", "").lower()
        hidden = bool(
            parent_hidden
            or tag in {"script", "style", "template"}
            or "hidden" in attrs_dict
            or attrs_dict.get("aria-hidden", "").lower() == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        )
        self.hidden_stack.append(hidden)

    def handle_endtag(self, tag: str) -> None:
        if self.hidden_stack:
            self.hidden_stack.pop()

    def handle_data(self, data: str) -> None:
        if not self.hidden_stack or not self.hidden_stack[-1]:
            text = unescape(data).replace("\u00a0", " ").strip()
            if text:
                self.parts.append(text)


class FallbackListEvidenceParser(FallbackVisibleTextParser):
    def __init__(self) -> None:
        super().__init__()
        self.has_table = False
        self.tbody_depth = 0
        self.row_depth = 0
        self.row_parts: list[str] = []
        self.row_texts: list[str] = []
        self.tbody_parts: list[str] = []
        self.raw_row_count = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, Optional[str]]],
    ) -> None:
        super().handle_starttag(tag, attrs)
        if tag == "table" and not self.hidden_stack[-1]:
            self.has_table = True
        if tag == "tbody" and not self.hidden_stack[-1]:
            self.tbody_depth += 1
        if tag == "tr" and self.tbody_depth and not self.hidden_stack[-1]:
            self.row_depth += 1
            self.raw_row_count += 1
            if self.row_depth == 1:
                self.row_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr" and self.row_depth:
            if self.row_depth == 1:
                self.row_texts.append(" ".join(self.row_parts))
                self.row_parts = []
            self.row_depth -= 1
        if tag == "tbody" and self.tbody_depth:
            self.tbody_depth -= 1
        super().handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        visible = not self.hidden_stack or not self.hidden_stack[-1]
        text = unescape(data).replace("\u00a0", " ").strip()
        if visible and text and self.tbody_depth:
            self.tbody_parts.append(text)
            if self.row_depth:
                self.row_parts.append(text)
        super().handle_data(data)

    @property
    def explicit_empty(self) -> bool:
        if not self.has_table or not self.tbody_depth_seen:
            return False
        tbody_text = " ".join(self.tbody_parts).lower()
        if not any(marker in tbody_text for marker in FALLBACK_EMPTY_MARKERS):
            return False
        return all(
            not row
            or any(marker in row.lower() for marker in FALLBACK_EMPTY_MARKERS)
            for row in self.row_texts
        )

    @property
    def tbody_depth_seen(self) -> bool:
        return bool(self.tbody_parts or self.raw_row_count)


def parse_fallback_list_evidence(
    html_text: str,
) -> FallbackListEvidenceParser:
    parser = FallbackListEvidenceParser()
    parser.feed(html_text)
    parser.close()
    return parser


def visible_html_text(html_text: str) -> str:
    parser = FallbackVisibleTextParser()
    parser.feed(html_text)
    parser.close()
    return " ".join(parser.parts)


def fallback_html_has_error(html_text: str) -> bool:
    normalized = visible_html_text(html_text).lower()
    return any(marker in normalized for marker in FALLBACK_ERROR_MARKERS)


def fallback_html_has_access_challenge(html_text: str) -> bool:
    normalized = visible_html_text(html_text).lower()
    return any(
        marker in normalized
        for marker in (
            "access denied",
            "captcha",
            "cloudflare",
            "too many requests",
        )
    )


def fallback_html_has_list_container(html_text: str) -> bool:
    evidence = parse_fallback_list_evidence(html_text)
    return evidence.has_table and evidence.tbody_depth_seen


def fallback_html_is_explicit_empty(html_text: str) -> bool:
    return parse_fallback_list_evidence(html_text).explicit_empty


def fallback_raw_row_count(html_text: str) -> int:
    return parse_fallback_list_evidence(html_text).raw_row_count


def fallback_list_url_matches(
    final_url: str,
    source: SourceSpec,
    requested_page: int,
) -> bool:
    parsed = urlparse(final_url)
    expected = urlparse(source.list_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected.hostname
        or parsed.path.rstrip("/") != expected.path.rstrip("/")
    ):
        return False
    values = parse_qs(parsed.query).get("page", [])
    return len(values) == 1 and values[0] == str(requested_page)


def fallback_detail_url_matches(
    url: str,
    notice_id: str,
    source_id: str,
) -> bool:
    normalized = normalize_detail_url(url)
    if not normalized:
        return False
    if extract_detail_id_from_text(normalized) != notice_id:
        return False
    values = parse_qs(urlparse(normalized).query).get("bbsConfigFk", [])
    return len(values) == 1 and values[0] == source_id


def fallback_page_signature(
    page: FallbackPageResult,
) -> tuple[tuple[str, bool, str, str, str], ...]:
    return tuple(
        (
            extract_detail_id_from_text(
                str(entry.get("url") or entry.get("detail_url") or "")
            )
            or "",
            bool(entry.get("top")),
            normalize_title_key(str(entry.get("title") or "")),
            normalize_date_key(str(entry.get("date") or "")),
            normalize_detail_url(
                str(entry.get("url") or entry.get("detail_url") or "")
            )
            or "",
        )
        for entry in page.entries
    )


def fallback_page_verified_for_request(
    page: FallbackPageResult,
    source: SourceSpec,
    requested_page: int,
) -> bool:
    return bool(
        page.ok
        and page.contract_verified
        and page.requested_page == requested_page
        and page.effective_page == requested_page
        and page.source_config_fk == source.config_fk
        and fallback_list_url_matches(
            page.final_url,
            source,
            requested_page,
        )
    )


def fallback_detail_signature(
    detail: FallbackDetailResult,
) -> tuple[str, str, str, str, str, str]:
    return (
        detail.notice_id,
        normalize_detail_url(detail.url) or "",
        normalize_title_key(detail.title),
        normalize_date_key(detail.date),
        json.dumps(
            detail.body_blocks,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        json.dumps(
            [
                {
                    "name": str(attachment.get("name") or ""),
                    "url": str(
                        attachment.get("external", {}).get("url") or ""
                    ),
                }
                for attachment in detail.attachments
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def fallback_detail_has_destructive_empty(
    detail: FallbackDetailResult,
) -> bool:
    return bool(
        (
            detail.body_status == BODY_STATUS_CONFIRMED_EMPTY
            and not detail.body_blocks
        )
        or (
            detail.attachments_status == ATTACHMENTS_STATUS_KNOWN
            and not detail.attachments
        )
    )


def fetch_fallback_http_page(
    source: SourceSpec,
    requested_page: int,
) -> FallbackPageResult:
    url = build_list_url(requested_page, source.list_url)
    result = fetch_site_result(url, "폴백 목록 HTML 요청")
    if not result.ok or result.body is None:
        return FallbackPageResult(
            ok=False,
            requested_page=requested_page,
            source_config_fk=source.config_fk,
            final_url=result.final_url,
            category=result.category,
            error=result.error or "fallback_list_fetch_failed",
            retry_after_seconds=result.retry_after_seconds,
        )
    content_type = result.content_type.lower()
    if content_type and not any(
        value in content_type
        for value in ("text/html", "application/xhtml+xml")
    ):
        return FallbackPageResult(
            ok=False,
            requested_page=requested_page,
            source_config_fk=source.config_fk,
            final_url=result.final_url,
            category=FailureCategory.SOURCE_CONTRACT,
            error="fallback_list_content_type",
        )
    html_text = result.body.decode("utf-8", errors="replace")
    if fallback_html_has_access_challenge(html_text):
        return FallbackPageResult(
            ok=False,
            requested_page=requested_page,
            source_config_fk=source.config_fk,
            final_url=result.final_url or url,
            category=FailureCategory.SECURITY_POLICY,
            error="fallback_http_access_challenge",
        )
    entries = parse_rows(html_text, source.config_fk)
    raw_count = fallback_raw_row_count(html_text)
    explicit_empty = fallback_html_is_explicit_empty(html_text)
    contract_verified = bool(
        fallback_list_url_matches(
            result.final_url or url,
            source,
            requested_page,
        )
        and fallback_html_has_list_container(html_text)
        and not fallback_html_has_error(html_text)
        and (
            (entries and raw_count == len(entries))
            or (not entries and explicit_empty)
        )
    )
    return FallbackPageResult(
        ok=contract_verified,
        requested_page=requested_page,
        effective_page=requested_page,
        source_config_fk=source.config_fk,
        entries=entries,
        final_url=result.final_url or url,
        contract_verified=contract_verified,
        explicit_empty=explicit_empty,
        raw_entry_count=raw_count,
        category=(
            FailureCategory.NONE
            if contract_verified
            else FailureCategory.SOURCE_CONTRACT
        ),
        error="" if contract_verified else "fallback_list_contract_invalid",
    )


def fetch_fallback_http_detail(
    source: SourceSpec,
    entry: JsonObject,
    _requested_page: int,
) -> FallbackDetailResult:
    raw_url = str(entry.get("url") or entry.get("detail_url") or "")
    notice_id = extract_detail_id_from_text(raw_url) or ""
    normalized_url = normalize_detail_url(raw_url) or ""
    if not fallback_detail_url_matches(
        normalized_url,
        notice_id,
        source.config_fk,
    ):
        return FallbackDetailResult(
            ok=False,
            notice_id=notice_id,
            url=normalized_url,
            category=FailureCategory.SOURCE_CONTRACT,
            error="fallback_detail_identity_invalid",
        )
    result = fetch_site_result(normalized_url, "폴백 상세 HTML 요청")
    final_url = normalize_detail_url(result.final_url or normalized_url) or ""
    if (
        not result.ok
        or result.body is None
        or not fallback_detail_url_matches(
            final_url,
            notice_id,
            source.config_fk,
        )
    ):
        return FallbackDetailResult(
            ok=False,
            notice_id=notice_id,
            url=final_url or normalized_url,
            category=(
                result.category
                if not result.ok
                else FailureCategory.SOURCE_CONTRACT
            ),
            error=result.error or "fallback_detail_redirect_mismatch",
            retry_after_seconds=result.retry_after_seconds,
        )
    html_text = result.body.decode("utf-8", errors="replace")
    if fallback_html_has_access_challenge(html_text):
        return FallbackDetailResult(
            ok=False,
            notice_id=notice_id,
            url=final_url,
            category=FailureCategory.SECURITY_POLICY,
            error="fallback_http_access_challenge",
        )
    signals = build_detail_signals(html_text)
    if fallback_html_has_error(html_text) or not signals.get("valid_detail"):
        return FallbackDetailResult(
            ok=False,
            notice_id=notice_id,
            url=final_url,
            category=FailureCategory.SOURCE_CONTRACT,
            error="fallback_detail_contract_invalid",
        )
    attachments = extract_attachments_from_detail(html_text)
    body_blocks = extract_body_blocks_from_html(html_text)
    if attachments and body_blocks:
        body_blocks = replace_body_image_urls(body_blocks, attachments)
    attachments_status = classify_attachment_status_from_signals(
        attachments,
        signals,
    )
    body_status = classify_body_status(body_blocks, signals)
    title = extract_detail_title_from_html(html_text)
    date = extract_written_at_from_detail(html_text) or ""
    ok = bool(
        title
        and is_plausible_notice_datetime(date)
        and body_status
        in {BODY_STATUS_PRESENT, BODY_STATUS_CONFIRMED_EMPTY}
        and attachments_status == ATTACHMENTS_STATUS_KNOWN
    )
    return FallbackDetailResult(
        ok=ok,
        notice_id=notice_id,
        url=final_url,
        title=title,
        date=date,
        body_blocks=body_blocks,
        body_status=body_status,
        attachments=attachments,
        attachments_status=attachments_status,
        category=(
            FailureCategory.NONE
            if ok
            else FailureCategory.SOURCE_PARTIAL
        ),
        error="" if ok else "fallback_detail_incomplete",
    )


def crawl_fallback_with_fetchers(
    source: SourceSpec,
    include_non_top: bool,
    non_top_max_pages: int,
    known_ids: set[str],
    incremental: bool,
    method: str,
    original: SourceCrawlResult,
    fetch_page: Callable[[int], FallbackPageResult],
    fetch_detail: Callable[[JsonObject, int], FallbackDetailResult],
    reconcile_mode: bool = False,
    refresh_known_ids: Optional[set[str]] = None,
    resume_page: int = 1,
    resume_anchor_ids: Optional[set[str]] = None,
) -> SourceCrawlResult:
    items: JsonObjectList = []
    observed_ids: list[str] = []
    observed_id_set: set[str] = set()
    top_urls: set[str] = set()
    top_dates: dict[str, set[str]] = {}
    seen_identity: dict[str, tuple[bool, str, str, str]] = {}
    page_sequences: set[tuple[tuple[str, bool], ...]] = set()
    page_signatures: dict[
        int,
        tuple[tuple[str, bool, str, str, str], ...],
    ] = {}
    pages_scanned = 0
    detail_failures = 0
    rejected_count = 0
    terminal_reached = False
    termination_reason = ""
    terminal_error = ""
    terminal_category = FailureCategory.NONE
    retry_after_seconds: Optional[float] = None
    checkpoint_found = not incremental or not known_ids
    request_count = 0
    started_at = time.monotonic()
    last_request_at = started_at
    max_requests = get_fallback_request_budget()
    time_budget = get_fallback_time_budget_seconds()
    min_interval = get_fallback_min_interval_seconds()
    jitter_max = get_fallback_jitter_seconds()
    backfill_detail_limit = get_backfill_detail_limit()
    fetched_detail_count = 0
    first_page_top_verified = False
    refresh_known_ids = refresh_known_ids or set()
    refreshed_known_ids: list[str] = []
    resume_page = max(1, resume_page)
    resume_anchor_ids = resume_anchor_ids or set()
    resume_active = bool(resume_page > 2 and resume_anchor_ids)
    resume_search_start = max(2, resume_page - 2)
    resume_search_end = resume_page + 2
    resume_anchor_found = not resume_active
    resume_jump_done = not resume_active
    next_resume_page = 1
    next_anchor_ids: list[str] = []
    checkpoint_page_number: Optional[int] = None
    checkpoint_overlap_pages = 0
    checkpoint_overlap_required = (
        get_incremental_checkpoint_overlap_pages()
    )
    required_refresh_ids = refresh_known_ids & known_ids

    def consume_budget(label: str) -> bool:
        nonlocal request_count, last_request_at, terminal_error
        nonlocal terminal_category, termination_reason
        check_run_control()
        shared_budget = CURRENT_SOURCE_REQUEST_BUDGET.get()
        if shared_budget is not None:
            if not shared_budget.consume_logical(
                "fallback",
                max_requests,
            ):
                terminal_error = shared_budget.exhausted_reason
                terminal_category = FailureCategory.SOURCE_PARTIAL
                termination_reason = (
                    "time_budget"
                    if "time_budget" in terminal_error
                    else "request_budget"
                )
                return False
        else:
            if request_count >= max_requests:
                terminal_error = (
                    f"fallback_request_budget_exceeded:{max_requests}"
                )
                terminal_category = FailureCategory.SOURCE_PARTIAL
                termination_reason = "request_budget"
                return False
            if time.monotonic() - started_at >= time_budget:
                terminal_error = (
                    f"fallback_time_budget_exceeded:{int(time_budget)}"
                )
                terminal_category = FailureCategory.SOURCE_PARTIAL
                termination_reason = "time_budget"
                return False
        jitter = 0.0
        if jitter_max:
            digest = hashlib.sha256(
                f"{source.config_fk}:{label}:{request_count}".encode("utf-8")
            ).digest()
            jitter = int.from_bytes(digest[:2], "big") / 65535 * jitter_max
        delay = min_interval + jitter - (time.monotonic() - last_request_at)
        if delay > 0:
            sleep_with_run_control(delay)
        request_count += 1
        last_request_at = time.monotonic()
        return True

    page_number = 1
    while True:
        check_run_control()
        if pages_scanned >= get_hard_page_limit():
            terminal_error = (
                f"fallback_hard_page_limit_reached:{get_hard_page_limit()}"
            )
            terminal_category = FailureCategory.SOURCE_PARTIAL
            termination_reason = "hard_cap"
            break
        if (
            not incremental
            and include_non_top
            and non_top_max_pages > 0
            and page_number > non_top_max_pages
        ):
            terminal_error = (
                f"fallback_configured_page_limit_reached:"
                f"{non_top_max_pages}"
            )
            terminal_category = FailureCategory.SOURCE_PARTIAL
            termination_reason = "configured_cap"
            break
        if not consume_budget(f"page:{page_number}"):
            break
        page = fetch_page(page_number)
        if not fallback_page_verified_for_request(
            page,
            source,
            page_number,
        ):
            terminal_error = page.error or "fallback_page_unverified"
            terminal_category = (
                page.category
                if page.category != FailureCategory.NONE
                else FailureCategory.SOURCE_CONTRACT
            )
            retry_after_seconds = page.retry_after_seconds
            termination_reason = "page_error"
            break
        pages_scanned += 1
        page_non_top_ids = {
            notice_id
            for entry in page.entries
            if not bool(entry.get("top"))
            and (
                notice_id := extract_detail_id_from_text(
                    str(
                        entry.get("url")
                        or entry.get("detail_url")
                        or ""
                    )
                )
            )
        }
        if (
            resume_active
            and page_number >= resume_search_start
            and page_non_top_ids & resume_anchor_ids
        ):
            resume_anchor_found = True
        if (
            resume_active
            and not resume_anchor_found
            and page_number > resume_search_end
        ):
            terminal_error = "backfill_resume_anchor_missing"
            terminal_category = FailureCategory.SOURCE_PARTIAL
            termination_reason = "resume_error"
            break
        if not page.entries:
            if resume_active and not resume_anchor_found:
                terminal_error = "backfill_resume_anchor_missing"
                terminal_category = FailureCategory.SOURCE_PARTIAL
                termination_reason = "resume_error"
                break
            if not page.explicit_empty:
                terminal_error = f"fallback_empty_unverified:{page_number}"
                terminal_category = FailureCategory.SOURCE_CONTRACT
                termination_reason = "page_error"
                break
            if not consume_budget(f"empty-confirmation:{page_number}"):
                break
            confirmation = fetch_page(page_number)
            if (
                not fallback_page_verified_for_request(
                    confirmation,
                    source,
                    page_number,
                )
                or confirmation.entries
                or not confirmation.explicit_empty
            ):
                terminal_error = (
                    f"fallback_empty_confirmation_mismatch:{page_number}"
                )
                terminal_category = (
                    confirmation.category
                    if confirmation.category
                    != FailureCategory.NONE
                    else FailureCategory.SOURCE_CONTRACT
                )
                retry_after_seconds = (
                    confirmation.retry_after_seconds
                )
                termination_reason = "page_error"
                break
            if page_number == 1:
                first_page_top_verified = True
            terminal_reached = True
            termination_reason = "natural_end"
            checkpoint_found = True
            break
        id_sequence: list[tuple[str, bool]] = []
        non_top_seen = False
        page_contract_failed = False
        page_has_checkpoint = False
        page_has_unknown_notice = False
        for entry in page.entries:
            top = bool(entry.get("top"))
            if top and non_top_seen:
                terminal_error = f"fallback_top_order_violation:{page_number}"
                terminal_category = FailureCategory.SOURCE_CONTRACT
                termination_reason = "page_error"
                page_contract_failed = True
                break
            if not top:
                non_top_seen = True
            raw_url = str(
                entry.get("url")
                or entry.get("detail_url")
                or ""
            )
            notice_id = extract_detail_id_from_text(raw_url) or ""
            normalized_url = normalize_detail_url(raw_url) or ""
            title = normalize_title_key(str(entry.get("title") or ""))
            date = str(entry.get("date") or "")
            if (
                not notice_id
                or not title
                or not is_plausible_notice_datetime(date)
                or not fallback_detail_url_matches(
                    normalized_url,
                    notice_id,
                    source.config_fk,
                )
            ):
                rejected_count += 1
                page_contract_failed = True
                terminal_error = f"fallback_row_identity_invalid:{page_number}"
                terminal_category = FailureCategory.SOURCE_CONTRACT
                termination_reason = "page_error"
                break
            identity = (top, title, normalize_date_key(date), normalized_url)
            id_sequence.append((notice_id, top))
            previous_identity = seen_identity.get(notice_id)
            if previous_identity is not None:
                if previous_identity != identity or not top:
                    terminal_error = (
                        f"fallback_notice_identity_collision:{notice_id}"
                    )
                    terminal_category = FailureCategory.SOURCE_CONTRACT
                    termination_reason = "page_error"
                    page_contract_failed = True
                    break
                continue
            seen_identity[notice_id] = identity
            if notice_id not in known_ids:
                page_has_unknown_notice = True
            observed_id_set.add(notice_id)
            observed_ids.append(notice_id)
            if top:
                top_urls.add(normalized_url)
                top_dates.setdefault(title, set()).add(
                    normalize_date_key(date)
                )
            if incremental and notice_id in known_ids and not top:
                page_has_checkpoint = True
            if (
                incremental
                and notice_id in known_ids
                and notice_id not in refresh_known_ids
            ):
                if not top:
                    checkpoint_found = True
                continue
            if not include_non_top and not top:
                continue
            if not consume_budget(f"detail:{notice_id}"):
                page_contract_failed = True
                break
            detail = fetch_detail(entry, page_number)
            if (
                not detail.ok
                or detail.notice_id != notice_id
                or not fallback_detail_url_matches(
                    detail.url,
                    notice_id,
                    source.config_fk,
                )
                or normalize_title_key(detail.title) != title
                or normalize_date_key(detail.date)
                != normalize_date_key(date)
            ):
                detail_failures += 1
                page_contract_failed = True
                terminal_error = (
                    detail.error
                    or f"fallback_detail_identity_mismatch:{notice_id}"
                )
                terminal_category = (
                    detail.category
                    if detail.category != FailureCategory.NONE
                    else FailureCategory.SOURCE_PARTIAL
                )
                retry_after_seconds = detail.retry_after_seconds
                termination_reason = "detail_error"
                break
            if not consume_budget(
                f"detail-stability-confirmation:{notice_id}"
            ):
                page_contract_failed = True
                break
            detail_confirmation = fetch_detail(entry, page_number)
            if (
                not detail_confirmation.ok
                or detail_confirmation.notice_id != notice_id
                or not fallback_detail_url_matches(
                    detail_confirmation.url,
                    notice_id,
                    source.config_fk,
                )
                or fallback_detail_signature(detail_confirmation)
                != fallback_detail_signature(detail)
                or fallback_detail_has_destructive_empty(detail)
            ):
                detail_failures += 1
                page_contract_failed = True
                terminal_error = (
                    f"fallback_detail_unstable:{notice_id}"
                )
                terminal_category = (
                    detail_confirmation.category
                    if not detail_confirmation.ok
                    and detail_confirmation.category
                    != FailureCategory.NONE
                    else FailureCategory.SOURCE_PARTIAL
                )
                retry_after_seconds = (
                    detail_confirmation.retry_after_seconds
                )
                termination_reason = "detail_error"
                break
            item = {
                "notice_id": notice_id,
                "title": title,
                "author": entry.get("author") or "",
                "date": detail.date,
                "views": entry.get("views"),
                "top": top,
                "url": detail.url,
                "classification": source.classification,
                "body_status": detail.body_status,
            }
            if detail.body_blocks:
                item["body_blocks"] = detail.body_blocks
            apply_item_attachments(
                item,
                detail.attachments,
                detail.attachments_status,
            )
            item_complete = bool(
                (
                    detail.body_status == BODY_STATUS_PRESENT
                    and detail.body_blocks
                )
                or (
                    detail.body_status == BODY_STATUS_CONFIRMED_EMPTY
                    and not detail.body_blocks
                )
            ) and (
                item.get("attachments_status")
                == ATTACHMENTS_STATUS_KNOWN
                and not item.get("attachments_truncated")
            )
            item["completeness"] = (
                ItemCompleteness.COMPLETE.value
                if item_complete
                else ItemCompleteness.PARTIAL.value
            )
            if not item_complete:
                detail_failures += 1
                page_contract_failed = True
                terminal_error = f"fallback_detail_incomplete:{notice_id}"
                terminal_category = FailureCategory.SOURCE_PARTIAL
                termination_reason = "detail_error"
                break
            items.append(item)
            fetched_detail_count += 1
            if notice_id in known_ids:
                refreshed_known_ids.append(notice_id)
        if page_contract_failed:
            break
        sequence = tuple(id_sequence)
        if sequence in page_sequences:
            terminal_error = f"fallback_repeated_page:{page_number}"
            terminal_category = FailureCategory.SOURCE_PARTIAL
            termination_reason = "page_error"
            break
        page_sequences.add(sequence)
        page_signatures[page_number] = fallback_page_signature(page)
        if page_has_checkpoint:
            checkpoint_found = True
            if checkpoint_page_number is None:
                checkpoint_page_number = page_number
        if (
            reconcile_mode
            and include_non_top
            and fetched_detail_count >= backfill_detail_limit
        ):
            terminal_reached = True
            checkpoint_found = True
            termination_reason = "backfill_window"
            next_resume_page = page_number + 1
            next_anchor_ids = sorted(page_non_top_ids)
            break
        if not include_non_top and any(
            not bool(entry.get("top")) for entry in page.entries
        ):
            terminal_reached = True
            checkpoint_found = True
            termination_reason = "non_top_boundary"
            break
        if (
            resume_active
            and not resume_jump_done
            and checkpoint_found
        ):
            resume_search_end += max(0, page_number - 1)
            page_number = max(
                page_number + 1,
                resume_search_start,
            )
            resume_jump_done = True
            continue
        if (
            incremental
            and not reconcile_mode
            and checkpoint_page_number is not None
            and page_number > checkpoint_page_number
        ):
            if page_has_unknown_notice:
                checkpoint_overlap_pages = 0
            elif page_has_checkpoint:
                checkpoint_overlap_pages += 1
            else:
                checkpoint_overlap_pages = 0
            if (
                checkpoint_overlap_pages
                >= checkpoint_overlap_required
                and required_refresh_ids.issubset(
                    set(refreshed_known_ids)
                )
            ):
                terminal_reached = True
                termination_reason = "incremental_checkpoint"
                break
        page_number += 1

    if terminal_reached and not terminal_error:
        for verified_page_number, expected_signature in page_signatures.items():
            if not consume_budget(
                f"stability-confirmation:{verified_page_number}"
            ):
                break
            confirmation = fetch_page(verified_page_number)
            if (
                not fallback_page_verified_for_request(
                    confirmation,
                    source,
                    verified_page_number,
                )
                or fallback_page_signature(confirmation)
                != expected_signature
            ):
                terminal_error = (
                    "fallback_snapshot_changed:"
                    f"{verified_page_number}"
                )
                terminal_category = (
                    confirmation.category
                    if not confirmation.ok
                    and confirmation.category
                    != FailureCategory.NONE
                    else FailureCategory.SOURCE_PARTIAL
                )
                retry_after_seconds = (
                    confirmation.retry_after_seconds
                )
                termination_reason = "snapshot_changed"
                break
            if verified_page_number == 1:
                first_page_top_verified = True

    if terminal_reached and not terminal_error and not detail_failures and not rejected_count:
        status = (
            SourceStatus.VALID_EMPTY
            if not observed_ids
            else SourceStatus.SUCCESS
        )
        category = FailureCategory.NONE
    else:
        status = SourceStatus.DEGRADED
        category = (
            terminal_category
            if terminal_category != FailureCategory.NONE
            else FailureCategory.SOURCE_PARTIAL
        )
        if not terminal_error:
            terminal_error = "fallback_scope_unverified"
    return SourceCrawlResult(
        source=source,
        status=status,
        items=items,
        method=method,
        pages_scanned=pages_scanned,
        observed_count=(
            len(set(observed_ids) | known_ids)
            if resume_active and termination_reason == "natural_end"
            else len(observed_ids)
        ),
        observed_ids=observed_ids,
        refreshed_known_ids=refreshed_known_ids,
        top_urls=sorted(top_urls),
        top_dates={key: sorted(values) for key, values in top_dates.items()},
        category=category,
        error=terminal_error,
        detail_failures=detail_failures,
        rejected_count=rejected_count,
        checkpoint_found=checkpoint_found,
        terminal_reached=terminal_reached,
        list_contract_valid=not bool(
            terminal_error
            and (
                "contract" in terminal_error
                or "identity" in terminal_error
                or "page_" in termination_reason
            )
        ),
        fallback_from_error=original.error,
        termination_reason=termination_reason,
        full_snapshot=(
            reconcile_mode
            and len(page_signatures) <= 1
            and terminal_reached
            and termination_reason
            in {"natural_end", "non_top_boundary"}
            and not terminal_error
        ),
        reconcile_complete=(
            reconcile_mode
            and len(page_signatures) <= 1
            and terminal_reached
            and termination_reason
            in {"natural_end", "non_top_boundary"}
            and not terminal_error
        ),
        coverage_complete=(
            reconcile_mode
            and terminal_reached
            and termination_reason
            in {"natural_end", "non_top_boundary"}
            and not terminal_error
        ),
        top_snapshot_verified=(
            first_page_top_verified
            and terminal_reached
            and not terminal_error
        ),
        retry_after_seconds=retry_after_seconds,
        backfill_resume_page=next_resume_page,
        backfill_anchor_ids=next_anchor_ids,
    )


def crawl_top_items_http_result(
    source: SourceSpec,
    include_non_top: bool,
    non_top_max_pages: int,
    known_ids: set[str],
    incremental: bool,
    original: SourceCrawlResult,
    reconcile_mode: bool = False,
    refresh_known_ids: Optional[set[str]] = None,
    resume_page: int = 1,
    resume_anchor_ids: Optional[set[str]] = None,
) -> SourceCrawlResult:
    return crawl_fallback_with_fetchers(
        source,
        include_non_top,
        non_top_max_pages,
        known_ids,
        incremental,
        "fallback_http",
        original,
        lambda page_number: fetch_fallback_http_page(
            source,
            page_number,
        ),
        lambda entry, page_number: fetch_fallback_http_detail(
            source,
            entry,
            page_number,
        ),
        reconcile_mode,
        refresh_known_ids,
        resume_page,
        resume_anchor_ids,
    )


def crawl_top_items_playwright_result(
    source: SourceSpec,
    include_non_top: bool,
    non_top_max_pages: int,
    known_ids: set[str],
    incremental: bool,
    original: SourceCrawlResult,
    reconcile_mode: bool = False,
    refresh_known_ids: Optional[set[str]] = None,
    resume_page: int = 1,
    resume_anchor_ids: Optional[set[str]] = None,
) -> SourceCrawlResult:
    if CURRENT_SOURCE_REQUEST_BUDGET.get() is None:
        with source_request_budget_scope():
            return crawl_top_items_playwright_result(
                source,
                include_non_top,
                non_top_max_pages,
                known_ids,
                incremental,
                original,
                reconcile_mode,
                refresh_known_ids,
                resume_page,
                resume_anchor_ids,
            )
    try:
        import playwright.sync_api as playwright_sync_api
        from playwright.sync_api import (
            TimeoutError as PlaywrightTimeoutError,
            sync_playwright,
        )
        PlaywrightError = getattr(
            playwright_sync_api,
            "Error",
            RuntimeError,
        )
    except ImportError:
        return crawl_top_items_http_result(
            source,
            include_non_top,
            non_top_max_pages,
            known_ids,
            incremental,
            original,
            reconcile_mode,
            refresh_known_ids,
            resume_page,
            resume_anchor_ids,
        )

    browser_name = os.environ.get("BROWSER", "chromium")
    if browser_name.strip().lower() != "chromium":
        return crawl_top_items_http_result(
            source,
            include_non_top,
            non_top_max_pages,
            known_ids,
            incremental,
            original,
            reconcile_mode,
            refresh_known_ids,
            resume_page,
            resume_anchor_ids,
        )
    headless = os.environ.get("HEADLESS", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    try:
        parsed_source_url = urlparse(source.list_url)
        source_port = parsed_source_url.port
    except ValueError:
        return playwright_security_result(
            source,
            original,
            "fallback_browser_unsafe_source",
        )
    source_host = (
        parsed_source_url.hostname or ""
    ).strip().lower().rstrip(".")
    if (
        parsed_source_url.scheme != "https"
        or parsed_source_url.username is not None
        or parsed_source_url.password is not None
        or source_port not in {None, 443}
        or not is_allowed_attachment_host(
            source_host,
            ("sogang.ac.kr",),
        )
    ):
        return playwright_security_result(
            source,
            original,
            "fallback_browser_unsafe_source",
        )
    pinned_address_info = resolve_public_network_address_info(
        source_host,
        443,
    )
    try:
        network_guard = PlaywrightNetworkGuard(
            source_host,
            pinned_address_info,
            CURRENT_SOURCE_REQUEST_BUDGET.get(),
        )
    except ValueError:
        return playwright_security_result(
            source,
            original,
            "fallback_browser_unsafe_source",
        )
    if not network_guard.validate_navigation_url(
        source.list_url,
        "fallback_browser_unsafe_source",
    ):
        return playwright_security_result(
            source,
            original,
            network_guard.security_error,
        )
    pinned_address = select_playwright_pinned_address(
        pinned_address_info
    )
    resolver_address = (
        f"[{pinned_address}]"
        if ":" in pinned_address
        else pinned_address
    )
    launch_args = [
        "--disable-quic",
        "--disable-background-networking",
        "--no-pings",
        (
            "--host-resolver-rules="
            f"MAP {source_host} {resolver_address}, "
            "MAP * ~NOTFOUND"
        ),
    ]
    with sync_playwright() as playwright:
        try:
            launcher = get_browser_launcher(playwright, browser_name)
            browser = launcher.launch(
                headless=headless,
                args=launch_args,
            )
        except PlaywrightError:
            return crawl_top_items_http_result(
                source,
                include_non_top,
                non_top_max_pages,
                known_ids,
                incremental,
                original,
                reconcile_mode,
                refresh_known_ids,
                resume_page,
                resume_anchor_ids,
            )
        browser_result: Optional[SourceCrawlResult] = None
        try:
            context = browser.new_context(
                user_agent=os.environ.get("USER_AGENT", USER_AGENT),
                viewport={"width": 1920, "height": 1080},
                service_workers="block",
            )
            request_budget = CURRENT_SOURCE_REQUEST_BUDGET.get()

            def route_request(route: Any, request: Any) -> None:
                network_guard.handle_route(route, request)

            context.route("**/*", route_request)
            if hasattr(context, "route_web_socket"):
                context.route_web_socket(
                    "**/*",
                    lambda web_socket: web_socket.close(),
                )
            page = context.new_page()

            def browser_page(page_number: int) -> FallbackPageResult:
                url = build_list_url(page_number, source.list_url)
                if not network_guard.validate_navigation_url(
                    url,
                    "fallback_browser_unsafe_request",
                ):
                    return FallbackPageResult(
                        ok=False,
                        requested_page=page_number,
                        source_config_fk=source.config_fk,
                        final_url=url,
                        category=FailureCategory.SECURITY_POLICY,
                        error=network_guard.security_error,
                    )
                try:
                    response = page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )
                    page.wait_for_load_state(
                        "networkidle",
                        timeout=15000,
                    )
                except PlaywrightTimeoutError:
                    if network_guard.security_error:
                        return FallbackPageResult(
                            ok=False,
                            requested_page=page_number,
                            source_config_fk=source.config_fk,
                            final_url=str(page.url or url),
                            category=FailureCategory.SECURITY_POLICY,
                            error=network_guard.security_error,
                        )
                    if (
                        request_budget is not None
                        and request_budget.exhausted_reason
                    ):
                        return FallbackPageResult(
                            ok=False,
                            requested_page=page_number,
                            source_config_fk=source.config_fk,
                            final_url=str(page.url or url),
                            category=FailureCategory.SOURCE_PARTIAL,
                            error=request_budget.exhausted_reason,
                        )
                    return FallbackPageResult(
                        ok=False,
                        requested_page=page_number,
                        source_config_fk=source.config_fk,
                        category=FailureCategory.NETWORK,
                        error="fallback_browser_list_timeout",
                    )
                except Exception:
                    if network_guard.security_error:
                        return FallbackPageResult(
                            ok=False,
                            requested_page=page_number,
                            source_config_fk=source.config_fk,
                            final_url=str(page.url or url),
                            category=FailureCategory.SECURITY_POLICY,
                            error=network_guard.security_error,
                        )
                    if (
                        request_budget is not None
                        and request_budget.exhausted_reason
                    ):
                        return FallbackPageResult(
                            ok=False,
                            requested_page=page_number,
                            source_config_fk=source.config_fk,
                            final_url=str(page.url or url),
                            category=FailureCategory.SOURCE_PARTIAL,
                            error=request_budget.exhausted_reason,
                        )
                    raise
                if network_guard.security_error:
                    return FallbackPageResult(
                        ok=False,
                        requested_page=page_number,
                        source_config_fk=source.config_fk,
                        final_url=str(page.url or url),
                        category=FailureCategory.SECURITY_POLICY,
                        error=network_guard.security_error,
                    )
                if (
                    request_budget is not None
                    and request_budget.exhausted_reason
                ):
                    return FallbackPageResult(
                        ok=False,
                        requested_page=page_number,
                        source_config_fk=source.config_fk,
                        category=FailureCategory.SOURCE_PARTIAL,
                        error=request_budget.exhausted_reason,
                    )
                if not network_guard.validate_navigation_url(
                    page.url,
                    "fallback_browser_unsafe_redirect",
                ):
                    return FallbackPageResult(
                        ok=False,
                        requested_page=page_number,
                        source_config_fk=source.config_fk,
                        final_url=str(page.url or url),
                        category=FailureCategory.SECURITY_POLICY,
                        error=network_guard.security_error,
                    )
                if response is not None and response.status >= 400:
                    retry_after_seconds = (
                        parse_retry_after_seconds(
                            response.headers.get("retry-after")
                        )
                        if response.status == 429
                        else None
                    )
                    return FallbackPageResult(
                        ok=False,
                        requested_page=page_number,
                        source_config_fk=source.config_fk,
                        final_url=page.url,
                        category=(
                            FailureCategory.SECURITY_POLICY
                            if response.status in {403, 429}
                            else FailureCategory.SOURCE_UPSTREAM
                        ),
                        error=f"fallback_browser_http_{response.status}",
                        retry_after_seconds=retry_after_seconds,
                    )
                html_text = page.content()
                if fallback_html_has_access_challenge(html_text):
                    return FallbackPageResult(
                        ok=False,
                        requested_page=page_number,
                        source_config_fk=source.config_fk,
                        final_url=page.url,
                        category=FailureCategory.SECURITY_POLICY,
                        error="fallback_browser_access_challenge",
                    )
                entries = extract_list_rows(page, source.config_fk)
                raw_count = page.locator(LIST_ROW_SELECTOR).count()
                explicit_empty = fallback_html_is_explicit_empty(html_text)
                contract_verified = bool(
                    fallback_list_url_matches(
                        page.url,
                        source,
                        page_number,
                    )
                    and fallback_html_has_list_container(html_text)
                    and not fallback_html_has_error(html_text)
                    and (
                        (entries and len(entries) == raw_count)
                        or (
                            not entries
                            and explicit_empty
                        )
                    )
                )
                return FallbackPageResult(
                    ok=contract_verified,
                    requested_page=page_number,
                    effective_page=page_number,
                    source_config_fk=source.config_fk,
                    entries=entries,
                    final_url=page.url,
                    contract_verified=contract_verified,
                    explicit_empty=explicit_empty,
                    raw_entry_count=raw_count,
                    category=(
                        FailureCategory.NONE
                        if contract_verified
                        else FailureCategory.SOURCE_CONTRACT
                    ),
                    error=(
                        ""
                        if contract_verified
                        else "fallback_browser_list_contract_invalid"
                    ),
                )

            def browser_detail(
                entry: JsonObject,
                _page_number: int,
            ) -> FallbackDetailResult:
                raw_url = str(entry.get("detail_url") or "")
                notice_id = extract_detail_id_from_text(raw_url) or ""
                if not fallback_detail_url_matches(
                    raw_url,
                    notice_id,
                    source.config_fk,
                ):
                    return FallbackDetailResult(
                        ok=False,
                        notice_id=notice_id,
                        url=raw_url,
                        category=FailureCategory.SOURCE_CONTRACT,
                        error="fallback_browser_detail_identity_invalid",
                    )
                if not network_guard.validate_navigation_url(
                    raw_url,
                    "fallback_browser_unsafe_request",
                ):
                    return FallbackDetailResult(
                        ok=False,
                        notice_id=notice_id,
                        url=raw_url,
                        category=FailureCategory.SECURITY_POLICY,
                        error=network_guard.security_error,
                    )
                try:
                    response = page.goto(
                        raw_url,
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )
                    page.wait_for_load_state(
                        "networkidle",
                        timeout=15000,
                    )
                except PlaywrightTimeoutError:
                    if network_guard.security_error:
                        return FallbackDetailResult(
                            ok=False,
                            notice_id=notice_id,
                            url=str(page.url or raw_url),
                            category=FailureCategory.SECURITY_POLICY,
                            error=network_guard.security_error,
                        )
                    if (
                        request_budget is not None
                        and request_budget.exhausted_reason
                    ):
                        return FallbackDetailResult(
                            ok=False,
                            notice_id=notice_id,
                            url=str(page.url or raw_url),
                            category=FailureCategory.SOURCE_PARTIAL,
                            error=request_budget.exhausted_reason,
                        )
                    return FallbackDetailResult(
                        ok=False,
                        notice_id=notice_id,
                        url=raw_url,
                        category=FailureCategory.NETWORK,
                        error="fallback_browser_detail_timeout",
                    )
                except Exception:
                    if network_guard.security_error:
                        return FallbackDetailResult(
                            ok=False,
                            notice_id=notice_id,
                            url=str(page.url or raw_url),
                            category=FailureCategory.SECURITY_POLICY,
                            error=network_guard.security_error,
                        )
                    if (
                        request_budget is not None
                        and request_budget.exhausted_reason
                    ):
                        return FallbackDetailResult(
                            ok=False,
                            notice_id=notice_id,
                            url=str(page.url or raw_url),
                            category=FailureCategory.SOURCE_PARTIAL,
                            error=request_budget.exhausted_reason,
                        )
                    raise
                if network_guard.security_error:
                    return FallbackDetailResult(
                        ok=False,
                        notice_id=notice_id,
                        url=str(page.url or raw_url),
                        category=FailureCategory.SECURITY_POLICY,
                        error=network_guard.security_error,
                    )
                if (
                    request_budget is not None
                    and request_budget.exhausted_reason
                ):
                    return FallbackDetailResult(
                        ok=False,
                        notice_id=notice_id,
                        url=raw_url,
                        category=FailureCategory.SOURCE_PARTIAL,
                        error=request_budget.exhausted_reason,
                    )
                if not network_guard.validate_navigation_url(
                    page.url,
                    "fallback_browser_unsafe_redirect",
                ):
                    return FallbackDetailResult(
                        ok=False,
                        notice_id=notice_id,
                        url=str(page.url or raw_url),
                        category=FailureCategory.SECURITY_POLICY,
                        error=network_guard.security_error,
                    )
                final_url = normalize_detail_url(page.url) or ""
                if (
                    response is not None
                    and response.status >= 400
                ):
                    retry_after_seconds = (
                        parse_retry_after_seconds(
                            response.headers.get("retry-after")
                        )
                        if response.status == 429
                        else None
                    )
                    return FallbackDetailResult(
                        ok=False,
                        notice_id=notice_id,
                        url=final_url or raw_url,
                        category=(
                            FailureCategory.SECURITY_POLICY
                            if response.status in {403, 429}
                            else FailureCategory.SOURCE_UPSTREAM
                        ),
                        error=f"fallback_browser_http_{response.status}",
                        retry_after_seconds=retry_after_seconds,
                    )
                if not fallback_detail_url_matches(
                    final_url,
                    notice_id,
                    source.config_fk,
                ):
                    return FallbackDetailResult(
                        ok=False,
                        notice_id=notice_id,
                        url=final_url,
                        category=FailureCategory.SOURCE_CONTRACT,
                        error="fallback_browser_detail_redirect_mismatch",
                    )
                html_text = page.content()
                if fallback_html_has_access_challenge(html_text):
                    return FallbackDetailResult(
                        ok=False,
                        notice_id=notice_id,
                        url=final_url,
                        category=FailureCategory.SECURITY_POLICY,
                        error="fallback_browser_access_challenge",
                    )
                signals = build_detail_signals(html_text)
                attachments = extract_attachments_from_page(page)
                if not attachments:
                    attachments = extract_attachments_from_detail(html_text)
                body_blocks = extract_body_blocks_from_html(html_text)
                if attachments and body_blocks:
                    body_blocks = replace_body_image_urls(
                        body_blocks,
                        attachments,
                    )
                attachments_status = classify_attachment_status_from_signals(
                    attachments,
                    signals,
                )
                body_status = classify_body_status(body_blocks, signals)
                title = extract_detail_title_from_html(html_text)
                date = (
                    extract_written_at_from_page(page)
                    or extract_written_at_from_detail(html_text)
                    or ""
                )
                ok = bool(
                    not fallback_html_has_error(html_text)
                    and signals.get("valid_detail")
                    and title
                    and is_plausible_notice_datetime(date)
                    and body_status
                    in {
                        BODY_STATUS_PRESENT,
                        BODY_STATUS_CONFIRMED_EMPTY,
                    }
                    and attachments_status
                    == ATTACHMENTS_STATUS_KNOWN
                )
                return FallbackDetailResult(
                    ok=ok,
                    notice_id=notice_id,
                    url=final_url,
                    title=title,
                    date=date,
                    body_blocks=body_blocks,
                    body_status=body_status,
                    attachments=attachments,
                    attachments_status=attachments_status,
                    category=(
                        FailureCategory.NONE
                        if ok
                        else FailureCategory.SOURCE_PARTIAL
                    ),
                    error=(
                        ""
                        if ok
                        else "fallback_browser_detail_incomplete"
                    ),
                )

            browser_result = crawl_fallback_with_fetchers(
                source,
                include_non_top,
                non_top_max_pages,
                known_ids,
                incremental,
                "fallback_playwright",
                original,
                browser_page,
                browser_detail,
                reconcile_mode,
                refresh_known_ids,
                resume_page,
                resume_anchor_ids,
            )
        except PlaywrightError as exc:
            browser_result = SourceCrawlResult(
                source=source,
                status=SourceStatus.FAILED,
                method="fallback_playwright",
                category=FailureCategory.NETWORK,
                error=f"fallback_browser_exception:{type(exc).__name__}",
                fallback_from_error=original.error,
                termination_reason="page_error",
            )
        finally:
            browser.close()
        if browser_result.write_safe:
            return browser_result
        if browser_result.category == FailureCategory.SECURITY_POLICY:
            return browser_result
        http_result = crawl_top_items_http_result(
            source,
            include_non_top,
            non_top_max_pages,
            known_ids,
            incremental,
            original,
            reconcile_mode,
            refresh_known_ids,
            resume_page,
            resume_anchor_ids,
        )
        http_result.fallback_from_error = ";".join(
            value
            for value in (
                f"api:{original.error}" if original.error else "",
                (
                    f"playwright:{browser_result.error}"
                    if browser_result.error
                    else ""
                ),
            )
            if value
        )
        if not http_result.write_safe:
            http_result.error = ";".join(
                value
                for value in (
                    http_result.error,
                    (
                        f"playwright_failed:{browser_result.error}"
                        if browser_result.error
                        else ""
                    ),
                )
                if value
            )
        return http_result
