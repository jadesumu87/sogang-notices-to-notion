import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from html import unescape
from html.parser import HTMLParser
import importlib.util
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse, urljoin

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
ATTACHMENT_PROPERTY = "첨부파일"
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
ATTACHMENT_EXT_PATTERN = re.compile(
    r"\.(pdf|hwp|hwpx|docx?|xlsx?|pptx?|zip|rar|7z|txt|csv|jpg|jpeg|png|gif|bmp)(?:$|\\?)",
    re.IGNORECASE,
)
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
    parsed = urlparse(absolute)
    if parsed.scheme in {"javascript", "mailto", "tel", "data"}:
        return None
    return urlunparse(parsed._replace(fragment=""))


def is_attachment_candidate(url: str, text: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if host and not host.endswith("sogang.ac.kr"):
        return False
    if ATTACHMENT_EXT_PATTERN.search(url) or ATTACHMENT_EXT_PATTERN.search(text):
        return True
    lowered_url = url.lower()
    if any(hint in lowered_url for hint in ATTACHMENT_HINTS):
        return True
    if "/file-fe-prd/board/" in parsed.path:
        return True
    return False


def log_attachments(label: str, attachments: list[dict]) -> None:
    if not attachments:
        return
    LOGGER.info("첨부파일 추출: %s (총 %s개)", label, len(attachments))
    for attachment in attachments:
        url = attachment.get("external", {}).get("url") or ""
        name = attachment.get("name") or ""
        LOGGER.info("첨부파일 링크: %s (%s)", url, name)


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
    for segment in segments:
        text = segment.get("text", "")
        if not text or (text.isspace() and "\u00a0" not in text):
            continue
        annotations = segment.get("annotations", DEFAULT_ANNOTATIONS)
        link = segment.get("link")
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
        return urljoin(BASE_SITE, raw_url)
    return raw_url


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


class TiptapBlockParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_tiptap = False
        self.tiptap_depth = 0
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
        self.void_tags = {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
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
            self.link_stack.append(normalize_link_url(href))
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
        if tag == "li" and self.in_list_item:
            self.flush_block()
            self.in_list_item = False
            self.current_block_type = None
        elif tag == "p" and not self.in_list_item and self.current_block_type == "p":
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

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if not self.in_tiptap:
            return
        if tag == "br":
            self.append_line_break()
        elif tag == "img":
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

    def append_segment(
        self,
        text: str,
        annotations: dict,
        link: Optional[str],
    ) -> None:
        if not text:
            return
        if text.isspace() and "\u00a0" not in text:
            if not self.rich_text:
                return
            if not self.rich_text[-1]["text"].endswith((" ", "\n")):
                self.rich_text[-1]["text"] += " "
            return
        if self.rich_text:
            last = self.rich_text[-1]
            if last.get("annotations") == annotations and last.get("link") == link:
                last["text"] += text
                return
        self.rich_text.append(
            {"text": text, "annotations": annotations, "link": link}
        )

    def append_line_break(self) -> None:
        if self.current_block_type is None and not self.in_list_item:
            self.current_block_type = "p"
        annotations = dict(DEFAULT_ANNOTATIONS)
        annotations["bold"] = self.bold_depth > 0
        annotations["italic"] = self.italic_depth > 0
        annotations["underline"] = self.underline_depth > 0
        annotations["strikethrough"] = self.strike_depth > 0
        annotations["code"] = self.code_depth > 0
        annotations["color"] = self.color_stack[-1] if self.color_stack else "default"
        link = self.link_stack[-1] if self.link_stack else None
        if self.rich_text:
            last = self.rich_text[-1]
            if last.get("annotations") == annotations and last.get("link") == link:
                last["text"] += "\n"
                return
        self.rich_text.append(
            {"text": "\n", "annotations": annotations, "link": link}
        )

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


def extract_body_blocks_from_html(html_text: str) -> list[dict]:
    parser = TiptapBlockParser()
    parser.feed(html_text)
    parser.close()
    return parser.blocks


def chunks(items: list[dict], size: int) -> list[list[dict]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


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


def extract_attachments_from_detail(html_text: str) -> list[dict]:
    attachments: list[dict] = []
    seen_urls: set[str] = set()

    def add_attachment(href: str, text: str) -> None:
        url = normalize_file_url(href)
        if not url or url in seen_urls:
            return
        if not is_attachment_candidate(url, text):
            return
        seen_urls.add(url)
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
            add_attachment(href, text)

    label_matches = list(re.finditer(r"첨부파일", html_text))
    if label_matches:
        for match in label_matches:
            start = max(0, match.start() - 800)
            end = min(len(html_text), match.end() + 6000)
            extract_from_chunk(html_text[start:end], strict=True)
    if not attachments:
        extract_from_chunk(html_text, strict=False)

    return attachments


def extract_attachments_from_page(page) -> list[dict]:
    result = page.evaluate(
        """
        () => {
            const results = [];
            const seen = new Set();
            let labelCount = 0;
            let labelLinkCount = 0;
            const labels = Array.from(document.querySelectorAll("body *"))
                .filter(el => el.textContent && el.textContent.includes("첨부파일"));
            labelCount = labels.length;
            const collectLinks = (root) => {
                const links = root.querySelectorAll("a[href]");
                links.forEach(a => {
                    const href = a.getAttribute("href") || "";
                    const text = (a.textContent || "").trim();
                    if (!href) return;
                    const key = href + "|" + text;
                    if (seen.has(key)) return;
                    seen.add(key);
                    results.push({href, text});
                });
                return links.length;
            };
            for (const label of labels) {
                let node = label;
                for (let i = 0; i < 6 && node; i += 1) {
                    const count = collectLinks(node);
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
            return {links: results, labelCount, labelLinkCount};
        }
        """
    )
    candidates = result.get("links", []) if isinstance(result, dict) else []
    label_count = result.get("labelCount", 0) if isinstance(result, dict) else 0
    label_link_count = result.get("labelLinkCount", 0) if isinstance(result, dict) else 0
    attachments: list[dict] = []
    seen_urls: set[str] = set()
    for candidate in candidates:
        href = candidate.get("href", "")
        text = candidate.get("text", "")
        url = normalize_file_url(href)
        if not url or url in seen_urls:
            continue
        if not is_attachment_candidate(url, text):
            continue
        seen_urls.add(url)
        name = text
        if not name:
            params = parse_qs(urlparse(url).query)
            name = params.get("sg", [""])[0]
        if not name:
            name = Path(urlparse(url).path).name or "첨부파일"
        attachments.append({"name": name, "type": "external", "external": {"url": url}})
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


def build_list_url(page: int) -> str:
    query = dict(DEFAULT_QUERY)
    query["page"] = str(page)
    return f"{BASE_URL}?{urlencode(query)}"


def extract_detail_url_from_row_html(row_html: str) -> Optional[str]:
    for match in re.finditer(r'href="([^"]+)"', row_html):
        href = unescape(match.group(1))
        candidate = normalize_detail_url(href)
        if candidate and is_detail_path_url(candidate):
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
                if candidate and is_detail_path_url(candidate):
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
) -> tuple[Optional[str], Optional[str], list[dict], list[dict]]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    if detail_url:
        detail_url = normalize_detail_url(detail_url) or detail_url
        if detail_url and not is_detail_path_url(detail_url):
            LOGGER.info("상세 URL 경로 아님: %s", detail_url)
            detail_url = None
    if detail_url:
        written_at, attachments, body_blocks = fetch_detail_metadata_from_url(detail_url)
        if not attachments or not body_blocks:
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
        detail_url = normalize_detail_url(build_detail_url(detail_id))
        written_at, attachments, body_blocks = fetch_detail_metadata_from_url(detail_url)
        if not attachments or not body_blocks:
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
    written_at, attachments, body_blocks = fetch_detail_metadata_from_url(normalized_detail_url)
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
                written_at, detail_url, attachments, body_blocks = fetch_detail_for_row(
                    page,
                    url,
                    item["row_index"],
                    item.get("detail_url"),
                )
                if written_at:
                    item["date"] = written_at
                if detail_url:
                    item["url"] = normalize_detail_url(detail_url)
                if attachments:
                    item["attachments"] = attachments
                    log_attachments(item["title"], attachments)
                if body_blocks:
                    item["body_blocks"] = body_blocks
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
                written_at, attachments, body_blocks = fetch_detail_metadata_from_url(
                    item["url"]
                )
                if written_at:
                    item["date"] = written_at
                if attachments:
                    item["attachments"] = attachments
                    log_attachments(item["title"], attachments)
                if body_blocks:
                    item["body_blocks"] = body_blocks
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
        except TimeoutError as exc:
            if attempt < max_retries:
                LOGGER.info(
                    "Notion API 재시도(%s/%s): timeout",
                    attempt + 1,
                    max_retries,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue
            raise RuntimeError("Notion API error: timeout") from exc
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


def fetch_detail_metadata_from_url(detail_url: str) -> tuple[Optional[str], list[dict], list[dict]]:
    html_text = fetch_html(detail_url)
    if not html_text:
        return None, [], []
    if "첨부파일" in html_text:
        LOGGER.info("첨부파일 HTML 감지: %s", detail_url)
    written_at = extract_written_at_from_detail(html_text)
    attachments = extract_attachments_from_detail(html_text)
    body_blocks = extract_body_blocks_from_html(html_text)
    return written_at, attachments, body_blocks


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
    notion_request("DELETE", url, token)


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


def sync_page_body_blocks(token: str, page_id: str, blocks: list[dict]) -> None:
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
    children = list_block_children(token, page_id)
    for block in children:
        block_id = block.get("id")
        if block_id:
            try:
                delete_block(token, block_id)
            except RuntimeError as exc:
                LOGGER.info("블록 삭제 실패: %s (%s)", block_id, exc)
    container_payload = build_container_block(container_rich_text)
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
) -> dict:
    title_text = {"content": item["title"]}
    if item.get("url"):
        title_text["link"] = {"url": item["url"]}
    props = {
        TITLE_PROPERTY: {"title": [{"text": title_text}]},
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
    database = ensure_attachment_property(notion_token, database_id, database)
    validate_required_properties(database)
    author_options = get_select_options(database, AUTHOR_PROPERTY)
    type_options = get_select_options(database, TYPE_PROPERTY)
    has_views_property = validate_optional_property_type(database, VIEWS_PROPERTY, "number")
    has_attachments_property = validate_optional_property_type(
        database, ATTACHMENT_PROPERTY, "files"
    )

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
        properties = build_properties(item, has_views_property, has_attachments_property)
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
            page_id = create_page(notion_token, database_id, properties)
            created += 1
            LOGGER.info("생성 완료: %s", label)
        body_blocks = item.get("body_blocks", [])
        if page_id and body_blocks:
            sync_page_body_blocks(notion_token, page_id, body_blocks)

    LOGGER.info("기존 TOP 정리 시작")
    disabled = disable_missing_top(notion_token, database_id, current_top_urls, current_top_dates)
    LOGGER.info("TOP 해제 수: %s", disabled)

    LOGGER.info("TOP 항목 수: %s", len(items))
    LOGGER.info("생성: %s", created)
    LOGGER.info("업데이트: %s", updated)


if __name__ == "__main__":
    main()
