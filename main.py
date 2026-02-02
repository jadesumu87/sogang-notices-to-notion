import os

from crawler import crawl_top_items, run_attachment_policy_selftest
from log import LOGGER, log_environment_info, setup_logging
from notion_client import (
    create_page,
    ensure_attachment_property,
    ensure_body_hash_property,
    ensure_classification_property,
    ensure_required_properties,
    ensure_select_options_batch,
    ensure_views_property,
    fetch_database,
    get_select_options,
    prepare_attachments_for_sync,
    prepare_body_blocks_for_sync,
    update_page,
)
from bbs_parser import ensure_item_title, parse_rows
from settings import (
    AUTHOR_PROPERTY,
    BODY_HASH_IMAGE_MODE_UPLOAD,
    BODY_HASH_PROPERTY,
    CLASSIFICATION_PROPERTY,
    TYPE_PROPERTY,
    get_bbs_config_fk,
    get_classification_for_config,
    get_sync_mode,
    load_dotenv,
    resolve_html_path,
    should_dedupe_on_start,
    should_run_attachment_selftest,
    should_upload_files_to_notion,
)
from sync import (
    build_properties,
    dedupe_database_by_url,
    disable_missing_top,
    extract_rich_text_value,
    extract_type_from_title,
    find_existing_page,
    sync_page_body_blocks,
)
from utils import compute_body_hash, has_image_blocks, normalize_body_blocks_for_hash
from utils import normalize_date_key, normalize_detail_url

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
    database = ensure_required_properties(notion_token, database_id, database)
    database = ensure_attachment_property(notion_token, database_id, database)
    database = ensure_body_hash_property(notion_token, database_id, database)
    database = ensure_classification_property(notion_token, database_id, database)
    database = ensure_views_property(notion_token, database_id, database)
    if should_dedupe_on_start():
        archived = dedupe_database_by_url(notion_token, database_id)
        if archived:
            LOGGER.info("URL 중복 정리 수: %s", archived)
    author_options = get_select_options(database, AUTHOR_PROPERTY)
    type_options = get_select_options(database, TYPE_PROPERTY)
    author_options = ensure_select_options_batch(
        notion_token, database_id, AUTHOR_PROPERTY, author_options, author_values
    )
    type_options = ensure_select_options_batch(
        notion_token, database_id, TYPE_PROPERTY, type_options, type_values
    )
    if classification_values:
        classification_options = get_select_options(database, CLASSIFICATION_PROPERTY)
        classification_options = ensure_select_options_batch(
            notion_token,
            database_id,
            CLASSIFICATION_PROPERTY,
            classification_options,
            classification_values,
        )
    has_classification_property = True
    has_views_property = True
    has_attachments_property = True
    has_body_hash_property = True
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
        attachment_count = len(item.get("attachments") or [])
        if upload_files and has_attachments_property and item.get("attachments"):
            item["attachments"] = prepare_attachments_for_sync(
                notion_token, item["attachments"]
            )
            attachment_count = len(item.get("attachments") or [])
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
        action = "업데이트" if page_id else "생성"
        if page_id:
            update_page(notion_token, page_id, properties)
            updated += 1
        else:
            page_id = create_page(notion_token, database_id, properties)
            created += 1
        body_state = "없음"
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
                    body_state = "변경"
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
                    body_state = "유지"
            else:
                blocks_for_sync = prepare_body_blocks_for_sync(
                    notion_token, body_blocks
                )
                sync_page_body_blocks(
                    notion_token, page_id, blocks_for_sync, sync_mode=sync_mode
                )
                body_updated += 1
                body_state = "동기화"
        LOGGER.info(
            "처리 완료: %s (상태=%s, 본문=%s, 첨부=%s)",
            label,
            action,
            body_state,
            attachment_count,
        )

    LOGGER.info("기존 TOP 정리 시작")
    disabled = disable_missing_top(notion_token, database_id, current_top_urls, current_top_dates)
    LOGGER.info("TOP 해제 수: %s", disabled)

    LOGGER.info("수집 항목 수: %s", len(items))
    LOGGER.info("생성: %s", created)
    LOGGER.info("업데이트: %s", updated)
    LOGGER.info("본문 변경: %s", body_updated)


if __name__ == "__main__":
    main()
