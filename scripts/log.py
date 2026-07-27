import importlib.util
import logging
import os
import re
import sys
from urllib.parse import urlsplit

from settings import (
    get_bbs_config_fks,
    get_config_classification_map,
    get_notion_api_version,
    should_upload_files_to_notion,
)

LOGGER = logging.getLogger("sogang-notices-crawler")

URL_IN_LOG_PATTERN = re.compile(r"https?://[^\s<>'\"]+")
SENSITIVE_HEADER_PATTERN = re.compile(
    r"(?i)(?P<key>[\"']?(?:authorization|cookie)[\"']?)"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\r\n]+)"
)
SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?P<key>[\"']?(?:notion[_-]?token|access[_-]?token|token|secret|signature|sig|password|api[_-]?key|request[_-]?id|database[_-]?id|data[_-]?source[_-]?id|page[_-]?id)[\"']?)"
    r"(?P<separator>\s*[:=]\s*)(?P<quote>[\"']?)[^\s,;&}\]\"']+(?P=quote)"
)
NOTION_OBJECT_ID_PATTERN = re.compile(
    r"(?i)(?<![0-9a-f])(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})(?![0-9a-f])"
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"\b(?:secret_[A-Za-z0-9_-]{8,}|ntn_[A-Za-z0-9_-]{8,}|github_pat_[A-Za-z0-9_]{8,})\b"
    ),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{8,}|sk-[A-Za-z0-9_-]{8,})\b"),
)


def summarize_url_for_log(url: str) -> str:
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return "[invalid-url]"
    host = parsed.hostname or "-"
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port:
        host = f"{host}:{port}"
    path = parsed.path or "/"
    if len(path) > 240:
        path = f"{path[:237]}..."
    return f"{host}{path}"


def redact_sensitive_urls(value: str) -> str:
    redacted = URL_IN_LOG_PATTERN.sub(
        lambda match: summarize_url_for_log(match.group(0)),
        str(value),
    )
    redacted = SENSITIVE_HEADER_PATTERN.sub(
        lambda match: (
            f"{match.group('key')}"
            f"{match.group('separator')}"
            + (
                f"{match.group('value')[0]}[REDACTED]"
                f"{match.group('value')[-1]}"
                if (
                    len(match.group("value")) >= 2
                    and match.group("value")[0] in {"\"", "'"}
                    and match.group("value")[-1]
                    == match.group("value")[0]
                )
                else "[REDACTED]"
            )
        ),
        redacted,
    )
    redacted = SENSITIVE_ASSIGNMENT_PATTERN.sub(
        lambda match: (
            f"{match.group('key')}"
            f"{match.group('separator')}"
            f"{match.group('quote')}[REDACTED]{match.group('quote')}"
        ),
        redacted,
    )
    for pattern in SECRET_VALUE_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    redacted = NOTION_OBJECT_ID_PATTERN.sub("[ID]", redacted)
    return redacted


class SensitiveLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            message = str(record.msg)
        record.msg = redact_sensitive_urls(message)
        record.args = ()
        if record.exc_info:
            record.exc_text = redact_sensitive_urls(
                logging.Formatter().formatException(record.exc_info)
            )
        if record.stack_info:
            record.stack_info = redact_sensitive_urls(record.stack_info)
        return True


LOGGER.addFilter(SensitiveLogFilter())


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def log_environment_info() -> None:
    python_version = (
        f"{sys.version_info.major}."
        f"{sys.version_info.minor}."
        f"{sys.version_info.micro}"
    )
    playwright_installed = importlib.util.find_spec("playwright") is not None
    browser = os.environ.get("BROWSER", "chromium")
    headless_raw = os.environ.get("HEADLESS", "1").strip().lower()
    headless = headless_raw not in {"0", "false", "no", "off"}
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
        "환경: BROWSER=%s, HEADLESS=%s, BBS_CONFIG_FKS=%s",
        browser,
        "1" if headless else "0",
        config_label,
    )
    if class_label:
        LOGGER.info("환경: BBS_CONFIG_CLASSIFY=%s", class_label)
    LOGGER.info(
        "환경: NOTION_VERSION=%s, NOTION_UPLOAD_FILES=%s",
        get_notion_api_version(),
        "1" if upload_files else "0",
    )
