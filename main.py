import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from html import unescape
import importlib.util
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

NOTION_API_VERSION = "2022-06-28"
BASE_URL = "https://www.sogang.ac.kr/ko/scholarship-notice"
DEFAULT_QUERY = {"introPkId": "All", "option": "TITLE"}
USER_AGENT = "Mozilla/5.0 (compatible; ScholarshipCrawler/1.0)"
PAGE_ICON_EMOJI = "🌱"
TITLE_PROPERTY = "제목"
AUTHOR_PROPERTY = "작성자"
DATE_PROPERTY = "작성일"
TOP_PROPERTY = "TOP"
URL_PROPERTY = "URL"
VIEWS_PROPERTY = "조회수"
TYPE_PROPERTY = "유형"
LOGGER = logging.getLogger("scholarship-crawler")
BASE_SITE = "https://www.sogang.ac.kr"
DATE_PATTERN = re.compile(
    r"\d{4}[.\-]\d{2}[.\-]\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?"
)
DATE_TIME_PATTERN = re.compile(r"\d{4}[.\-]\d{2}[.\-]\d{2}\s+\d{2}:\d{2}(?::\d{2})?")
DATE_TIME_JS_PATTERN = r"\d{4}[.\-]\d{2}[.\-]\d{2}\s+\d{2}:\d{2}(?::\d{2})?"
DETAIL_PATH_PATTERN = re.compile(r"/detail/\d+")
LIST_ROW_SELECTOR = "tr[data-v-6debbb14], table tbody tr"
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


def normalize_date_key(date_text: Optional[str]) -> str:
    if not date_text:
        return ""
    match = re.search(r"\d{4}-\d{2}-\d{2}", date_text)
    if match:
        return match.group(0)
    return date_text[:10]


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


def is_detail_url(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    path = parsed.path or ""
    if DETAIL_PATH_PATTERN.search(path):
        return True
    qs = parse_qs(parsed.query)
    return "bbsConfigFk" in qs


def get_bbs_config_fk() -> str:
    return os.environ.get("BBS_CONFIG_FK", "141")


def build_detail_url(detail_id: str) -> str:
    return f"{BASE_SITE}/ko/detail/{detail_id}?bbsConfigFk={get_bbs_config_fk()}"


def parse_int(value: str) -> Optional[int]:
    digits = re.sub(r"[^0-9]", "", value)
    if not digits:
        return None
    return int(digits)


def parse_rows(html_text: str) -> list[dict]:
    row_pattern = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
    rows = row_pattern.findall(html_text)
    items = []

    for row_html in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.DOTALL)
        if not cells:
            continue

        cleaned = [clean_text(cell) for cell in cells]

        if len(cleaned) < 5:
            continue

        num_or_top = cleaned[0]
        title = cleaned[1]
        author = cleaned[2]
        date_text = cleaned[-2]
        views_text = cleaned[-1]

        date_iso = parse_datetime(date_text)
        views = parse_int(views_text)
        if not date_iso or views is None or not title:
            continue

        top = num_or_top.strip().upper() == "TOP"
        detail_url = extract_detail_url_from_row_html(row_html)

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


def build_list_url(page: int) -> str:
    query = dict(DEFAULT_QUERY)
    query["page"] = str(page)
    return f"{BASE_URL}?{urlencode(query)}"


def extract_detail_url_from_row_html(row_html: str) -> Optional[str]:
    for match in re.finditer(r'href="([^"]+)"', row_html):
        href = unescape(match.group(1))
        candidate = normalize_detail_url(href)
        if candidate and is_detail_url(candidate):
            return candidate
    match = re.search(r"/detail/(\d+)", row_html)
    if match:
        return normalize_detail_url(build_detail_url(match.group(1)))
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


def extract_list_rows(page) -> list[dict]:
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
        if not title or views is None:
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
            DATE_TIME_JS_PATTERN,
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


def extract_detail_id_from_row(row) -> Optional[str]:
    for key in ("data-id", "data-no", "data-board-id", "data-article-id", "data-detail-id"):
        value = row.get_attribute(key)
        if value and value.isdigit():
            return value
    try:
        dataset = row.evaluate("row => ({...row.dataset})")
        for value in dataset.values():
            if isinstance(value, str) and value.isdigit():
                return value
    except Exception:
        return None
    return None


def extract_written_at_from_page(page) -> Optional[str]:
    label = page.locator("text=작성일").or_(page.locator("text=등록일"))
    for idx in range(label.count()):
        label_node = label.nth(idx)
        try:
            container_text = label_node.locator("xpath=..").inner_text()
        except Exception:
            container_text = ""
        match = DATE_TIME_PATTERN.search(container_text)
        if match:
            return parse_datetime(match.group(0))
        try:
            sibling_texts = label_node.locator("xpath=following-sibling::*").all_inner_texts()
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


def fetch_written_at_via_playwright(
    page,
    list_url: str,
    detail_url: str,
) -> Optional[str]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    written_at = None
    try:
        page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
        if not wait_for_written_at(page):
            LOGGER.info("작성일 로드 대기 실패: %s", detail_url)
        written_at = extract_written_at_from_page(page)
        if not written_at:
            written_at = extract_written_at_from_detail(page.content())
    except PlaywrightTimeoutError:
        LOGGER.info("상세 페이지 로드 실패: %s", detail_url)
    finally:
        return_to_list_page(page, list_url)
    return written_at


def fetch_detail_for_row(
    page,
    list_url: str,
    row_index: int,
    detail_url: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    if detail_url:
        detail_url = normalize_detail_url(detail_url) or detail_url
        written_at = fetch_written_at_from_url(detail_url)
        if written_at:
            return written_at, detail_url
        written_at = fetch_written_at_via_playwright(page, list_url, detail_url)
        return written_at, detail_url

    rows = page.locator(LIST_ROW_SELECTOR)
    if row_index >= rows.count():
        return None, None

    row = rows.nth(row_index)
    row.scroll_into_view_if_needed()
    detail_id = extract_detail_id_from_row(row)
    if detail_id:
        detail_url = normalize_detail_url(build_detail_url(detail_id))
        written_at = fetch_written_at_from_url(detail_url)
        if written_at:
            return written_at, detail_url
    row.click()

    detail_url = wait_for_detail_url(page, list_url)
    if not detail_url:
        LOGGER.info("상세 URL 전환 실패: row %s", row_index)
        return_to_list_page(page, list_url)
        return None, None

    normalized_detail_url = normalize_detail_url(detail_url) or detail_url
    written_at = fetch_written_at_from_url(normalized_detail_url)
    if not written_at:
        if not wait_for_written_at(page):
            LOGGER.info("작성일 로드 대기 실패: %s", detail_url)
        written_at = extract_written_at_from_page(page)
        if not written_at:
            written_at = extract_written_at_from_detail(page.content())
    return_to_list_page(page, list_url)
    return written_at, normalized_detail_url


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


def crawl_top_items() -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    except ImportError as exc:
        LOGGER.info("Playwright 미설치: HTTP 모드로 전환")
        return crawl_top_items_http()

    items = []
    seen = set()
    browser_name = os.environ.get("BROWSER", "chromium")
    headless_raw = os.environ.get("HEADLESS", "1").strip().lower()
    headless = headless_raw not in {"0", "false", "no", "off"}
    user_agent = os.environ.get("USER_AGENT", USER_AGENT)

    with sync_playwright() as playwright:
        launcher = get_browser_launcher(playwright, browser_name)
        browser = launcher.launch(headless=headless)
        context = browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()

        page_number = 1
        fallback_to_http = False
        while True:
            url = build_list_url(page_number)
            LOGGER.info("페이지 로드 시작: %s", url)
            if not goto_list_page(page, url):
                LOGGER.info("페이지 %s 로드 실패", page_number)
                if page_number == 1:
                    LOGGER.info("Playwright 페이지 로드 실패: HTTP 모드로 전환")
                    fallback_to_http = True
                break

            page_items = extract_list_rows(page)
            LOGGER.info("페이지 %s 항목 수: %s", page_number, len(page_items))
            if not page_items:
                break

            top_items = [item for item in page_items if item.get("top")]
            has_non_top = any(not item.get("top") for item in page_items)
            new_top = 0
            for item in top_items:
                written_at, detail_url = fetch_detail_for_row(
                    page,
                    url,
                    item["row_index"],
                    item.get("detail_url"),
                )
                if written_at:
                    item["date"] = written_at
                if detail_url:
                    item["url"] = normalize_detail_url(detail_url)
                key = item.get("url") or f"{item['title']}|{item.get('date') or ''}"
                if key in seen:
                    continue
                seen.add(key)
                items.append(item)
                new_top += 1

            LOGGER.info("페이지 %s 신규 TOP 수: %s", page_number, new_top)
            if has_non_top:
                LOGGER.info("페이지 %s에서 비TOP 발견, 다음 페이지 탐색 중단", page_number)
                break
            page_number += 1

        browser.close()

    if fallback_to_http:
        return crawl_top_items_http()
    return items


def crawl_top_items_http() -> list[dict]:
    items = []
    seen = set()
    page_number = 1

    while True:
        url = build_list_url(page_number)
        LOGGER.info("페이지 로드 시작(HTTP): %s", url)
        html_text = fetch_html(url)
        if not html_text:
            LOGGER.info("페이지 %s 로드 실패(HTTP)", page_number)
            break
        page_items = parse_rows(html_text)
        LOGGER.info("페이지 %s 항목 수(HTTP): %s", page_number, len(page_items))
        if not page_items:
            break

        top_items = [item for item in page_items if item.get("top")]
        has_non_top = any(not item.get("top") for item in page_items)
        new_top = 0
        for item in top_items:
            if item.get("url"):
                written_at = fetch_written_at_from_url(item["url"])
                if written_at:
                    item["date"] = written_at
            key = item.get("url") or f"{item['title']}|{item.get('date') or ''}"
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
            new_top += 1

        LOGGER.info("페이지 %s 신규 TOP 수(HTTP): %s", page_number, new_top)
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
        req.add_header("Notion-Version", NOTION_API_VERSION)
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
            raise RuntimeError(f"Notion API error: HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            if attempt < max_retries:
                LOGGER.info(
                    "Notion API 재시도(%s/%s): %s",
                    attempt + 1,
                    max_retries,
                    exc.reason,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue
            raise RuntimeError(f"Notion API error: {exc.reason}") from exc


def fetch_html(url: str) -> Optional[str]:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        LOGGER.info("상세 HTML 요청 실패: %s (HTTP %s)", url, exc.code)
    except urllib.error.URLError as exc:
        LOGGER.info("상세 HTML 요청 실패: %s (%s)", url, exc.reason)
    return None


def fetch_written_at_from_url(detail_url: str) -> Optional[str]:
    html_text = fetch_html(detail_url)
    if not html_text:
        return None
    return extract_written_at_from_detail(html_text)


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
    match = re.match(r"\s*\[([^\]]+)\]", title)
    if match:
        label = match.group(1).strip()
        if label in TYPE_TAGS:
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
    LOGGER.info(
        "환경: Python=%s, Playwright=%s",
        python_version,
        "설치됨" if playwright_installed else "미설치",
    )
    LOGGER.info(
        "환경: BROWSER=%s, HEADLESS=%s, bbsConfigFk=%s",
        browser,
        "1" if headless else "0",
        get_bbs_config_fk(),
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


def query_database(token: str, database_id: str, filter_payload: dict) -> list[dict]:
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    payload = {"filter": filter_payload}
    data = notion_request("POST", url, token, payload)
    return data.get("results", [])


def build_properties(item: dict, has_views_property: bool) -> dict:
    props = {
        TITLE_PROPERTY: {"title": [{"text": {"content": item["title"]}}]},
        TOP_PROPERTY: {"checkbox": item["top"]},
    }

    if item.get("date"):
        props[DATE_PROPERTY] = {"date": {"start": item["date"]}}
    if item.get("author"):
        props[AUTHOR_PROPERTY] = {"select": {"name": item["author"]}}
    if item.get("type"):
        props[TYPE_PROPERTY] = {"select": {"name": item["type"]}}
    if has_views_property and item.get("views") is not None:
        props[VIEWS_PROPERTY] = {"number": item["views"]}
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


def find_existing_page(
    token: str,
    database_id: str,
    detail_url: Optional[str],
    title: str,
    date_iso: Optional[str],
) -> Optional[str]:
    if detail_url:
        results = query_database(
            token,
            database_id,
            {"property": URL_PROPERTY, "url": {"equals": detail_url}},
        )
        if len(results) == 1:
            return results[0]["id"]
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
            return results[0]["id"]
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
            return results[0]["id"]
    return None


def build_icon() -> dict:
    return {"type": "emoji", "emoji": PAGE_ICON_EMOJI}


def create_page(token: str, database_id: str, properties: dict) -> None:
    payload = {
        "parent": {"database_id": database_id},
        "properties": properties,
        "icon": build_icon(),
    }
    notion_request("POST", "https://api.notion.com/v1/pages", token, payload)


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

    notion_token = os.environ.get("NOTION_TOKEN")
    database_id = os.environ.get("NOTION_DB_ID")

    if not notion_token or not database_id:
        raise RuntimeError("NOTION_TOKEN and NOTION_DB_ID must be set (env or .env)")

    html_path = resolve_html_path()
    if html_path is not None:
        if not html_path.exists():
            raise RuntimeError(f"HTML file not found: {html_path}")
        html_text = html_path.read_text(encoding="utf-8", errors="replace")
        items = parse_rows(html_text)
    else:
        items = crawl_top_items()

    if not items:
        raise RuntimeError("No items parsed from source")

    database = fetch_database(notion_token, database_id)
    database = ensure_url_property(notion_token, database_id, database)
    database = ensure_type_property(notion_token, database_id, database)
    validate_required_properties(database)
    author_options = get_select_options(database, AUTHOR_PROPERTY)
    type_options = get_select_options(database, TYPE_PROPERTY)
    has_views_property = validate_optional_property_type(database, VIEWS_PROPERTY, "number")

    created = 0
    updated = 0

    current_top_urls: set[str] = set()
    current_top_dates: dict[str, set[str]] = {}
    for item in items:
        if item.get("url"):
            normalized_url = normalize_detail_url(item["url"])
            if normalized_url:
                item["url"] = normalized_url
                current_top_urls.add(normalized_url)
        item["type"] = extract_type_from_title(item["title"])
        label = f"{item['title']} ({item.get('date') or '날짜없음'})"
        date_key = normalize_date_key(item.get("date"))
        current_top_dates.setdefault(item["title"], set()).add(date_key)
        LOGGER.info("처리 시작: %s", label)
        if item.get("author"):
            author_options = ensure_select_option(
                notion_token,
                database_id,
                AUTHOR_PROPERTY,
                item["author"],
                author_options,
            )
        type_options = ensure_select_option(
            notion_token,
            database_id,
            TYPE_PROPERTY,
            item["type"],
            type_options,
        )
        properties = build_properties(item, has_views_property)
        page_id = find_existing_page(
            notion_token,
            database_id,
            item.get("url"),
            item["title"],
            item.get("date"),
        )
        if page_id:
            update_page(notion_token, page_id, properties)
            updated += 1
            LOGGER.info("업데이트 완료: %s", label)
        else:
            create_page(notion_token, database_id, properties)
            created += 1
            LOGGER.info("생성 완료: %s", label)

    LOGGER.info("기존 TOP 정리 시작")
    disabled = disable_missing_top(notion_token, database_id, current_top_urls, current_top_dates)
    LOGGER.info("TOP 해제 수: %s", disabled)

    LOGGER.info("TOP 항목 수: %s", len(items))
    LOGGER.info("생성: %s", created)
    LOGGER.info("업데이트: %s", updated)


if __name__ == "__main__":
    main()
