import os
import re
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

DEFAULT_NOTION_API_VERSION = "2026-03-11"
BASE_URL = "https://www.sogang.ac.kr/ko/scholarship-notice"
ACADEMIC_BASE_URL = "https://www.sogang.ac.kr/ko/academic-support/notices"
DEFAULT_QUERY = {"introPkId": "All", "option": "TITLE"}
USER_AGENT = "Mozilla/5.0 (compatible; SogangNoticesCrawler/1.0)"
PAGE_ICON_EMOJI = "📢"
TITLE_PROPERTY = "공지사항"
AUTHOR_PROPERTY = "작성자"
DATE_PROPERTY = "작성일"
TOP_PROPERTY = "TOP"
URL_PROPERTY = "URL"
VIEWS_PROPERTY = "조회수"
ATTACHMENT_PROPERTY = "첨부파일"
ATTACHMENT_STATE_PROPERTY = "첨부 상태"
TYPE_PROPERTY = "유형"
CLASSIFICATION_PROPERTY = "분류"
BODY_HASH_PROPERTY = "본문 해시"
BODY_MEDIA_STATE_PROPERTY = "본문 미디어 상태"
SYNC_OWNER_PROPERTY = "동기화 소유자"
SOURCE_KEY_PROPERTY = "출처 ID"
NOTICE_ID_PROPERTY = "공지 ID"
SYNC_GENERATION_PROPERTY = "본문 세대"
SYNC_STATUS_PROPERTY = "동기화 상태"
SYNC_OPERATION_PROPERTY = "작업 ID"
SYNC_OWNER_VALUE = "sogang-notices-to-notion/v1"
BODY_HASH_IMAGE_MODE_UPLOAD = "upload-files-v1"
SYNC_CONTAINER_MARKER = "[SOGANG_NOTICES_TO_NOTION_SYNC_V2]"
LEGACY_SYNC_CONTAINER_MARKER = "[SYNC_CONTAINER]"
BASE_SITE = "https://www.sogang.ac.kr"
BBS_API_BASE = f"{BASE_SITE}/api/api/v1/mainKo/BbsData"
BBS_LIST_API_URL = f"{BBS_API_BASE}/boardListMultiConfigId"
DATE_PATTERN = re.compile(r"\d{4}[.\-]\d{2}[.\-]\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?")
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
    r"\.(pdf|hwp|hwpx|docx?|xlsx?|pptx?|zip|rar|7z|txt|csv|jpg|jpeg|png|gif|bmp)(?:$|\?)",
    re.IGNORECASE,
)
IMAGE_EXT_PATTERN = re.compile(
    r"\.(jpg|jpeg|png|gif|bmp|webp|svg)(?:$|\?)",
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
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                    value = value[1:-1]
                os.environ.setdefault(key, value)
    except FileNotFoundError:
        return


def get_notion_api_version() -> str:
    configured = os.environ.get(
        "NOTION_API_VERSION",
        DEFAULT_NOTION_API_VERSION,
    ).strip()
    if configured != DEFAULT_NOTION_API_VERSION:
        raise ValueError(
            "지원하는 NOTION_API_VERSION은 "
            f"{DEFAULT_NOTION_API_VERSION}입니다"
        )
    return DEFAULT_NOTION_API_VERSION


def get_notion_data_source_id() -> Optional[str]:
    value = os.environ.get("NOTION_DATA_SOURCE_ID", "").strip()
    return value or None


def should_allow_notion_schema_migration() -> bool:
    raw = os.environ.get("NOTION_SCHEMA_MIGRATION", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def should_run_notion_schema_migration_only() -> bool:
    raw = (
        os.environ.get(
            "NOTION_SCHEMA_MIGRATION_ONLY",
            "0",
        )
        .strip()
        .lower()
    )
    return raw in {"1", "true", "yes", "on"}


def is_writer_context_confirmed() -> bool:
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    workflow_ref = os.environ.get("GITHUB_WORKFLOW_REF", "").strip()
    event_name = os.environ.get("GITHUB_EVENT_NAME", "").strip()
    expected_workflow_ref = (
        f"{repository}/.github/workflows/crawler.yml@refs/heads/main"
    )
    return bool(
        os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"
        and repository
        and workflow_ref == expected_workflow_ref
        and event_name in {"schedule", "workflow_dispatch"}
        and os.environ.get("GITHUB_REF", "").strip() == "refs/heads/main"
        and re.fullmatch(
            r"[0-9]+",
            os.environ.get("GITHUB_RUN_ID", "").strip(),
        )
        is not None
        and re.fullmatch(
            r"[1-9][0-9]*",
            os.environ.get("GITHUB_RUN_ATTEMPT", "").strip(),
        )
        is not None
        and re.fullmatch(
            r"[0-9a-f]{40}",
            os.environ.get("GITHUB_SHA", "").strip().lower(),
        )
        is not None
    )


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


def should_upload_files_to_notion() -> bool:
    raw = os.environ.get("NOTION_UPLOAD_FILES", "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def should_include_non_top() -> bool:
    raw = os.environ.get("INCLUDE_NON_TOP", "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def get_non_top_max_pages() -> int:
    raw = os.environ.get("NON_TOP_MAX_PAGES", "0").strip()
    try:
        value = int(raw)
    except ValueError:
        return 0
    return max(0, value)


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
        parts = re.split(r"[,\s]+", raw)
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


def should_run_dry_run() -> bool:
    raw = os.environ.get("SYNC_DRY_RUN", "1").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        "SYNC_DRY_RUN은 1/0, true/false, yes/no 또는 on/off여야 합니다"
    )


def get_run_state_path() -> Path:
    return Path(os.environ.get("CRAWLER_STATE_PATH", ".runtime/run-state.json"))


def get_snapshot_path() -> Path:
    return Path(os.environ.get("CRAWLER_SNAPSHOT_PATH", ".runtime/snapshot.json"))


def get_incident_path() -> Path:
    return Path(os.environ.get("CRAWLER_INCIDENT_PATH", ".runtime/incident.json"))


def should_use_incremental_crawl() -> bool:
    raw = os.environ.get("INCREMENTAL_CRAWL", "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def get_full_reconcile_local_hour() -> int:
    raw = os.environ.get("FULL_RECONCILE_LOCAL_HOUR", "7").strip()
    try:
        value = int(raw)
    except ValueError:
        return 7
    return min(23, max(0, value))


def get_top_disable_max_count() -> int:
    raw = os.environ.get("TOP_DISABLE_MAX_COUNT", "20").strip()
    try:
        value = int(raw)
    except ValueError:
        return 20
    return max(0, value)


def get_top_disable_max_ratio() -> float:
    raw = os.environ.get("TOP_DISABLE_MAX_RATIO", "0.5").strip()
    try:
        value = float(raw)
    except ValueError:
        return 0.5
    return min(1.0, max(0.0, value))


def get_optional_config_fks() -> set[str]:
    raw = os.environ.get("BBS_OPTIONAL_CONFIG_FKS", "").strip()
    return {part for part in re.split(r"[,\s]+", raw) if part}


def resolve_html_path() -> Optional[Path]:
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    env_path = os.environ.get("HTML_PATH")
    if env_path:
        return Path(env_path)
    return None
