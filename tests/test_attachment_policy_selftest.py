import hashlib
import os
import sys
import unittest
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import crawler as crawler_module
from crawler import (
    ATTACHMENTS_STATUS_KNOWN,
    ATTACHMENTS_STATUS_UNKNOWN,
    DetailSignals,
    JsonObject,
    JsonObjectList,
    LOGGER,
    apply_item_attachments,
    build_detail_signals,
    classify_attachment_status_from_api_detail,
    classify_attachment_status_from_signals,
    extract_attachments_from_api_data,
    extract_attachments_from_detail,
    extract_attachments_from_page,
    fetch_detail_for_row,
    fetch_detail_metadata_from_url,
    fetch_detail_metadata_via_playwright,
    get_detail_html_fallback_reason,
    should_retry_detail_fetch,
)
from utils import is_attachment_candidate, normalize_file_url


def should_require_browser() -> bool:
    raw = os.environ.get("REQUIRE_BROWSER_TESTS", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def run_attachment_policy_selftest() -> None:
    LOGGER.info("첨부파일 정책 셀프테스트 시작")
    keys = ("ATTACHMENT_ALLOWED_DOMAINS",)
    original_env = {key: os.environ.get(key) for key in keys}
    os.environ["ATTACHMENT_ALLOWED_DOMAINS"] = "sogang.ac.kr"
    notion_upload_backup = None
    notion_download_backup = None
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
                "첨부파일 정책 셀프테스트 실패: HTML=%s, API=%s, 엄격 허용=%s, 페이지=%s",
                len(html_attachments),
                len(api_attachments),
                int(strict_allowed),
                len(page_attachments),
            )
            raise RuntimeError("첨부파일 정책 셀프테스트 실패")
        import notion_client as notion_client_module
        from notion_client import (
            NotionRequestError,
            prepare_attachments_for_sync,
            prepare_body_blocks_for_sync,
        )
        import sync as sync_module
        from utils import build_pdf_block, compute_body_hash, normalize_body_blocks_for_hash

        notion_upload_backup = notion_client_module.upload_external_file_to_notion
        notion_download_backup = notion_client_module.download_file_bytes

        def selftest_file_payload(url: str) -> bytes:
            return f"selftest:{url}".encode("utf-8")

        def selftest_file_hash(url: str) -> str:
            return hashlib.sha256(selftest_file_payload(url)).hexdigest()

        def fake_download_file(
            url: str,
            require_file_hint: bool = False,
            max_bytes: int = notion_client_module.EXTERNAL_DOWNLOAD_MAX_BYTES,
        ) -> tuple[Optional[bytes], Optional[str]]:
            payload = selftest_file_payload(url)
            if len(payload) > max_bytes:
                return None, None
            return (
                payload,
                "application/pdf" if require_file_hint else "image/jpeg",
            )

        notion_client_module.download_file_bytes = fake_download_file

        def fake_upload_success(
            token: str,
            url: str,
            filename_hint: Optional[str] = None,
            expect_image: bool = True,
            downloaded_file: Optional[
                tuple[bytes, Optional[str]]
            ] = None,
        ) -> Optional[str]:
            suffix = "image" if expect_image else "file"
            return f"{suffix}-{filename_hint or ''}"

        notion_client_module.upload_external_file_to_notion = fake_upload_success
        allowed_image_url = (
            "https://www.sogang.ac.kr/file-fe-prd/board/1/"
            "test.jpg?sg=test.jpg"
        )
        allowed_file_url = (
            "https://www.sogang.ac.kr/file-fe-prd/board/1/"
            "test.pdf?sg=test.pdf"
        )
        allowed_blocks = [
            {
                "object": "block",
                "type": "image",
                "image": {
                    "type": "external",
                    "external": {
                        "url": allowed_image_url
                    },
                    "caption": [{"type": "text", "text": {"content": "sample"}}],
                },
            },
            {
                "object": "block",
                "type": "embed",
                "embed": {
                    "url": allowed_file_url
                },
            },
        ]
        prepared_blocks, prepared_hash_blocks, prepared_media_state = prepare_body_blocks_for_sync(
            "selftest-token", allowed_blocks
        )
        allowed_media_content_state = [
            {
                "type": "image",
                "source_url": allowed_image_url,
                "content_sha256": selftest_file_hash(
                    allowed_image_url
                ),
            },
            {
                "type": "pdf",
                "source_url": allowed_file_url,
                "content_sha256": selftest_file_hash(
                    allowed_file_url
                ),
            },
        ]
        desired_hash = compute_body_hash(
            normalize_body_blocks_for_hash(
                allowed_blocks,
                True,
                media_content_state=allowed_media_content_state,
            ),
            image_mode="upload-files-v1",
        )
        actual_hash = compute_body_hash(
            prepared_hash_blocks,
            image_mode="upload-files-v1",
        )
        if (
            prepared_blocks[0].get("image", {}).get("type") != "file_upload"
            or prepared_blocks[1].get("type") != "pdf"
            or prepared_media_state
            != [
                {
                    "type": "image",
                    "source_url": "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg",
                    "upload_id": "image-test.jpg",
                    "content_sha256": selftest_file_hash(
                        "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg"
                    ),
                },
                {
                    "type": "pdf",
                    "source_url": "https://www.sogang.ac.kr/file-fe-prd/board/1/test.pdf?sg=test.pdf",
                    "upload_id": "file-test.pdf",
                    "content_sha256": selftest_file_hash(
                        "https://www.sogang.ac.kr/file-fe-prd/board/1/test.pdf?sg=test.pdf"
                    ),
                },
            ]
            or desired_hash != actual_hash
        ):
            raise RuntimeError("본문 업로드 셀프테스트 실패(허용 목록/해시)")

        def fake_upload_partial(
            token: str,
            url: str,
            filename_hint: Optional[str] = None,
            expect_image: bool = True,
            downloaded_file: Optional[
                tuple[bytes, Optional[str]]
            ] = None,
        ) -> Optional[str]:
            if url.endswith("test.pdf?sg=test.pdf"):
                return None
            suffix = "image" if expect_image else "file"
            return f"{suffix}-{filename_hint or ''}"

        notion_client_module.upload_external_file_to_notion = fake_upload_partial
        partial_blocks, partial_hash_blocks, partial_media_state = prepare_body_blocks_for_sync(
            "selftest-token", allowed_blocks
        )
        partial_hash = compute_body_hash(
            partial_hash_blocks,
            image_mode="upload-files-v1",
        )
        if (
            partial_blocks[0].get("image", {}).get("type") != "file_upload"
            or partial_blocks[1].get("type") != "embed"
            or partial_media_state
            != [
                {
                    "type": "image",
                    "source_url": "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg",
                    "upload_id": "image-test.jpg",
                    "content_sha256": selftest_file_hash(
                        "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg"
                    ),
                }
            ]
            or partial_hash == desired_hash
        ):
            raise RuntimeError("본문 업로드 셀프테스트 실패(부분 실패 재시도)")

        def fake_upload_should_not_run(
            token: str,
            url: str,
            filename_hint: Optional[str] = None,
            expect_image: bool = True,
            downloaded_file: Optional[
                tuple[bytes, Optional[str]]
            ] = None,
        ) -> Optional[str]:
            raise RuntimeError("재사용 가능한 업로드 블록이 있는데 업로드가 다시 호출됨")

        notion_client_module.upload_external_file_to_notion = fake_upload_should_not_run
        reusable_image = {
            "object": "block",
            "type": "image",
            "image": {"type": "file_upload", "file_upload": {"id": "reused-test-image"}},
        }
        reusable_pdf = build_pdf_block("reused-test-pdf")
        reused_blocks, reused_hash_blocks, reused_media_state = prepare_body_blocks_for_sync(
            "selftest-token",
            allowed_blocks,
            reusable_uploaded_media={
                (
                    "image",
                    "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg",
                ): [
                    {
                        "block": reusable_image,
                        "content_sha256": selftest_file_hash(
                            "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg"
                        ),
                    }
                ],
                (
                    "pdf",
                    "https://www.sogang.ac.kr/file-fe-prd/board/1/test.pdf?sg=test.pdf",
                ): [
                    {
                        "block": reusable_pdf,
                        "content_sha256": selftest_file_hash(
                            "https://www.sogang.ac.kr/file-fe-prd/board/1/test.pdf?sg=test.pdf"
                        ),
                    }
                ],
            },
        )
        reused_hash = compute_body_hash(
            reused_hash_blocks,
            image_mode="upload-files-v1",
        )
        if (
            reused_blocks[0].get("image", {}).get("file_upload", {}).get("id") != "reused-test-image"
            or reused_blocks[0].get("image", {}).get("caption", [{}])[0].get("text", {}).get("content") != "sample"
            or
            reused_blocks[1].get("type") != "pdf"
            or reused_blocks[1].get("pdf", {}).get("file_upload", {}).get("id") != "reused-test-pdf"
            or reused_media_state
            != [
                {
                    "type": "image",
                    "source_url": "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg",
                    "upload_id": "reused-test-image",
                    "content_sha256": selftest_file_hash(
                        "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg"
                    ),
                },
                {
                    "type": "pdf",
                    "source_url": "https://www.sogang.ac.kr/file-fe-prd/board/1/test.pdf?sg=test.pdf",
                    "upload_id": "reused-test-pdf",
                    "content_sha256": selftest_file_hash(
                        "https://www.sogang.ac.kr/file-fe-prd/board/1/test.pdf?sg=test.pdf"
                    ),
                },
            ]
            or reused_hash != desired_hash
        ):
            raise RuntimeError("본문 업로드 셀프테스트 실패(기존 업로드 재사용)")

        captioned_reusable_image = {
            "object": "block",
            "type": "image",
            "image": {
                "type": "file_upload",
                "file_upload": {"id": "caption-test-image"},
                "caption": [{"type": "text", "text": {"content": "old caption"}}],
            },
        }
        (
            caption_removed_blocks,
            caption_removed_hash_blocks,
            caption_removed_media_state,
        ) = prepare_body_blocks_for_sync(
            "selftest-token",
            [
                {
                    "object": "block",
                    "type": "image",
                    "image": {
                        "type": "external",
                        "external": {
                            "url": "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg"
                        },
                    },
                }
            ],
            reusable_uploaded_media={
                (
                    "image",
                    "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg",
                ): [
                    {
                        "block": captioned_reusable_image,
                        "content_sha256": selftest_file_hash(
                            "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg"
                        ),
                    }
                ],
            },
        )
        if (
            "caption" in caption_removed_blocks[0].get("image", {})
            or "caption" in caption_removed_hash_blocks[0].get("image", {})
            or caption_removed_media_state
            != [
                {
                    "type": "image",
                    "source_url": "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg",
                    "upload_id": "caption-test-image",
                    "content_sha256": selftest_file_hash(
                        "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg"
                    ),
                }
            ]
        ):
            raise RuntimeError("본문 업로드 셀프테스트 실패(캡션 제거 반영)")

        reused_attachments, reused_attachment_state = prepare_attachments_for_sync(
            "selftest-token",
            [
                {
                    "name": "sample.jpg",
                    "type": "external",
                    "external": {
                        "url": "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg"
                    },
                }
            ],
            reusable_uploaded_attachments={
                "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg": [
                    {
                        "upload_id": "attachment-upload-1",
                        "content_sha256": selftest_file_hash(
                            "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg"
                        ),
                    }
                ]
            },
        )
        if (
            reused_attachments
            != [
                {
                    "name": "sample.jpg",
                    "type": "file_upload",
                    "file_upload": {"id": "attachment-upload-1"},
                }
            ]
            or reused_attachment_state
            != [
                {
                    "source_url": "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg",
                    "name": "sample.jpg",
                    "upload_id": "attachment-upload-1",
                    "content_sha256": selftest_file_hash(
                        "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg"
                    ),
                }
            ]
        ):
            raise RuntimeError("첨부 업로드 셀프테스트 실패(기존 업로드 재사용)")

        notion_client_module.upload_external_file_to_notion = fake_upload_success
        blocked_blocks = [
            {
                "object": "block",
                "type": "image",
                "image": {
                    "type": "external",
                    "external": {"url": "https://example.com/test.jpg"},
                },
            },
            {
                "object": "block",
                "type": "embed",
                "embed": {"url": "https://example.com/test.pdf"},
            },
        ]
        blocked_prepared, blocked_hash_blocks, blocked_media_state = prepare_body_blocks_for_sync(
            "selftest-token", blocked_blocks
        )
        if (
            blocked_prepared != blocked_blocks
            or blocked_hash_blocks != blocked_blocks
            or blocked_media_state
        ):
            raise RuntimeError("본문 업로드 셀프테스트 실패(차단 URL 유지)")
        if get_detail_html_fallback_reason(
            {
                "title": "",
                "regDate": "20260422103030",
                "content": "<p>fragment body</p>",
                "fileValue1": "",
            },
            entry_title="목록 제목",
        ):
            raise RuntimeError("상세 보완 셀프테스트 실패(URL 조각과 본문·제목 보완)")
        original_list_block_children: Callable[
            [str, str],
            JsonObjectList,
        ] = getattr(sync_module, "list_block_children")
        original_retrieve_page: Callable[
            [str, str],
            JsonObject,
        ] = getattr(sync_module, "retrieve_page")
        try:
            def fake_list_block_children(
                _token: str,
                _page_id: str,
            ) -> JsonObjectList:
                return [
                    {
                        "id": "quote-user",
                        "type": "quote",
                        "quote": {
                            "rich_text": [{"plain_text": "사용자 인용 블록"}]
                        },
                    },
                    {
                        "id": "quote-sync",
                        "type": "quote",
                        "quote": {
                            "rich_text": [{"plain_text": "본문 컨테이너"}]
                        },
                    },
                ]

            setattr(
                sync_module,
                "list_block_children",
                fake_list_block_children,
            )
            setattr(
                sync_module,
                "retrieve_page",
                lambda _token, _page_id: {
                    "id": "selftest-page",
                    "properties": {},
                },
            )
            if sync_module.find_sync_container_block("selftest-token", "selftest-page"):
                raise RuntimeError("본문 업로드 셀프테스트 실패(덮어쓰기 컨테이너 추정)")
        finally:
            setattr(
                sync_module,
                "list_block_children",
                original_list_block_children,
            )
            setattr(
                sync_module,
                "retrieve_page",
                original_retrieve_page,
            )
        original_find_sync_container_block: Callable[
            [str, str],
            Optional[JsonObject],
        ] = sync_module.find_sync_container_block
        original_list_block_children = getattr(
            sync_module,
            "list_block_children",
        )
        try:
            setattr(
                sync_module,
                "find_sync_container_block",
                lambda _token, _page_id: {"id": "sync-container"},
            )

            def fake_misaligned_uploaded_children(
                _token: str,
                _page_id: str,
            ) -> JsonObjectList:
                return [
                    {
                        "id": "file-1",
                        "type": "file",
                        "file": {
                            "type": "file",
                            "file": {
                                "url": "https://s3.us-west-2.amazonaws.com/secure.notion-static.com/file-1/test.bin?X-Amz-Signature=abc"
                            },
                        },
                    },
                    {
                        "id": "image-1",
                        "type": "image",
                        "image": {
                            "type": "file",
                            "file": {
                                "url": "https://s3.us-west-2.amazonaws.com/secure.notion-static.com/image-1/test.jpg?X-Amz-Signature=abc"
                            },
                        },
                    },
                    {
                        "id": "pdf-1",
                        "type": "pdf",
                        "pdf": {
                            "type": "file",
                            "file": {
                                "url": "https://s3.us-west-2.amazonaws.com/secure.notion-static.com/pdf-1/test.pdf?X-Amz-Signature=abc"
                            },
                        },
                    },
                ]

            setattr(
                sync_module,
                "list_block_children",
                fake_misaligned_uploaded_children,
            )
            if sync_module.extract_existing_uploaded_media_blocks(
                "selftest-token",
                "selftest-page",
                [
                    {
                        "type": "image",
                        "source_url": "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg",
                        "upload_id": "image-upload-state",
                        "block_id": "image-1",
                    },
                    {
                        "type": "pdf",
                        "source_url": "https://www.sogang.ac.kr/file-fe-prd/board/1/test.pdf?sg=test.pdf",
                        "upload_id": "pdf-upload-state",
                        "block_id": "pdf-1",
                    },
                ],
            ):
                raise RuntimeError("본문 업로드 셀프테스트 실패(재사용 안전 차단)")

            def fake_uploaded_image_child(
                _token: str,
                _page_id: str,
            ) -> JsonObjectList:
                return [
                    {
                        "id": "image-raw",
                        "type": "image",
                        "has_children": False,
                        "in_trash": False,
                        "created_time": "2026-04-22T00:00:00.000Z",
                        "image": {
                            "type": "file",
                            "file": {
                                "url": "https://s3.us-west-2.amazonaws.com/secure.notion-static.com/image-raw/test.jpg?X-Amz-Signature=abc"
                            },
                            "caption": [
                                {"type": "text", "text": {"content": "caption kept"}}
                            ],
                        },
                    }
                ]

            setattr(
                sync_module,
                "list_block_children",
                fake_uploaded_image_child,
            )
            sanitized_reusable = sync_module.extract_existing_uploaded_media_blocks(
                "selftest-token",
                "selftest-page",
                [
                    {
                        "type": "image",
                        "source_url": "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg",
                        "upload_id": "image-upload-sanitized",
                        "block_id": "image-raw",
                        "content_sha256": selftest_file_hash(
                            "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg"
                        ),
                    }
                ],
            )
            expected_reusable = {
                (
                    "image",
                    "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg",
                ): [
                    {
                        "block": {
                            "object": "block",
                            "type": "image",
                            "image": {
                                "type": "file_upload",
                                "file_upload": {"id": "image-upload-sanitized"},
                                "caption": [
                                    {"type": "text", "text": {"content": "caption kept"}}
                                ],
                            },
                        },
                        "content_sha256": selftest_file_hash(
                            "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg"
                        ),
                    }
                ]
            }
            if sanitized_reusable != expected_reusable:
                raise RuntimeError("본문 업로드 셀프테스트 실패(재사용 블록 정제)")

            def fake_reordered_same_type_children(
                _token: str,
                _page_id: str,
            ) -> JsonObjectList:
                return [
                    {
                        "id": "image-b",
                        "type": "image",
                        "image": {
                            "type": "file",
                            "file": {
                                "url": "https://s3.us-west-2.amazonaws.com/secure.notion-static.com/image-b/B.jpg?X-Amz-Signature=abc"
                            },
                        },
                    },
                    {
                        "id": "image-a",
                        "type": "image",
                        "image": {
                            "type": "file",
                            "file": {
                                "url": "https://s3.us-west-2.amazonaws.com/secure.notion-static.com/image-a/A.jpg?X-Amz-Signature=abc"
                            },
                        },
                    },
                ]

            setattr(
                sync_module,
                "list_block_children",
                fake_reordered_same_type_children,
            )
            reordered_reusable = sync_module.extract_existing_uploaded_media_blocks(
                "selftest-token",
                "selftest-page",
                [
                    {
                        "type": "image",
                        "source_url": "https://www.sogang.ac.kr/file-fe-prd/board/1/A.jpg?sg=A.jpg",
                        "upload_id": "image-upload-a",
                        "block_id": "image-a",
                        "content_sha256": selftest_file_hash(
                            "https://www.sogang.ac.kr/file-fe-prd/board/1/A.jpg?sg=A.jpg"
                        ),
                    },
                    {
                        "type": "image",
                        "source_url": "https://www.sogang.ac.kr/file-fe-prd/board/1/B.jpg?sg=B.jpg",
                        "upload_id": "image-upload-b",
                        "block_id": "image-b",
                        "content_sha256": selftest_file_hash(
                            "https://www.sogang.ac.kr/file-fe-prd/board/1/B.jpg?sg=B.jpg"
                        ),
                    },
                ],
            )
            if reordered_reusable != {
                (
                    "image",
                    "https://www.sogang.ac.kr/file-fe-prd/board/1/A.jpg?sg=A.jpg",
                ): [
                    {
                        "block": {
                            "object": "block",
                            "type": "image",
                            "image": {
                                "type": "file_upload",
                                "file_upload": {"id": "image-upload-a"},
                            },
                        },
                        "content_sha256": selftest_file_hash(
                            "https://www.sogang.ac.kr/file-fe-prd/board/1/A.jpg?sg=A.jpg"
                        ),
                    }
                ],
                (
                    "image",
                    "https://www.sogang.ac.kr/file-fe-prd/board/1/B.jpg?sg=B.jpg",
                ): [
                    {
                        "block": {
                            "object": "block",
                            "type": "image",
                            "image": {
                                "type": "file_upload",
                                "file_upload": {"id": "image-upload-b"},
                            },
                        },
                        "content_sha256": selftest_file_hash(
                            "https://www.sogang.ac.kr/file-fe-prd/board/1/B.jpg?sg=B.jpg"
                        ),
                    }
                ],
            }:
                raise RuntimeError("본문 업로드 셀프테스트 실패(업로드 ID 기반 재사용)")

            def fake_same_shape_but_stale_ids(
                _token: str,
                _page_id: str,
            ) -> JsonObjectList:
                return [
                    {
                        "id": "image-current",
                        "type": "image",
                        "image": {
                            "type": "file",
                            "file": {
                                "url": "https://s3.us-west-2.amazonaws.com/secure.notion-static.com/image-current/test.jpg?X-Amz-Signature=abc"
                            },
                        },
                    },
                    {
                        "id": "pdf-current",
                        "type": "pdf",
                        "pdf": {
                            "type": "file",
                            "file": {
                                "url": "https://s3.us-west-2.amazonaws.com/secure.notion-static.com/pdf-current/test.pdf?X-Amz-Signature=abc"
                            },
                        },
                    },
                ]

            setattr(
                sync_module,
                "list_block_children",
                fake_same_shape_but_stale_ids,
            )
            if sync_module.extract_existing_uploaded_media_blocks(
                "selftest-token",
                "selftest-page",
                [
                    {
                        "type": "image",
                        "source_url": "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg",
                        "upload_id": "image-upload-stale",
                        "block_id": "image-stale",
                    },
                    {
                        "type": "pdf",
                        "source_url": "https://www.sogang.ac.kr/file-fe-prd/board/1/test.pdf?sg=test.pdf",
                        "upload_id": "pdf-upload-stale",
                        "block_id": "pdf-stale",
                    },
                ],
            ):
                raise RuntimeError("본문 업로드 셀프테스트 실패(수동 편집 후 오래된 블록 ID 차단)")

            def fake_same_block_id_but_changed_hosted_file(
                _token: str,
                _page_id: str,
            ) -> JsonObjectList:
                return [
                    {
                        "id": "image-current",
                        "type": "image",
                        "image": {
                            "type": "file",
                            "file": {
                                "url": "https://s3.us-west-2.amazonaws.com/secure.notion-static.com/image-current/changed.jpg?X-Amz-Signature=abc"
                            },
                        },
                    }
                ]

            setattr(
                sync_module,
                "list_block_children",
                fake_same_block_id_but_changed_hosted_file,
            )
            if sync_module.extract_existing_uploaded_media_blocks(
                "selftest-token",
                "selftest-page",
                [
                    {
                        "type": "image",
                        "source_url": "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg",
                        "upload_id": "image-upload-current",
                        "block_id": "image-current",
                        "hosted_file_key": "s3.us-west-2.amazonaws.com/secure.notion-static.com/image-current/original.jpg",
                    }
                ],
            ):
                raise RuntimeError("본문 업로드 셀프테스트 실패(동일 블록 ID의 다른 호스팅 파일 차단)")

            valid_attachment_reuse = sync_module.extract_existing_uploaded_attachment_ids(
                {
                    "첨부파일": {
                        "files": [
                            {
                                "name": "sample.jpg",
                                "type": "file",
                                "file": {
                                    "url": "https://s3.us-west-2.amazonaws.com/secure.notion-static.com/attachment-upload-1/sample.jpg?X-Amz-Signature=abc"
                                },
                            }
                        ]
                    }
                },
                [
                    {
                        "source_url": "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg",
                        "name": "sample.jpg",
                        "upload_id": "attachment-upload-1",
                        "hosted_file_key": "s3.us-west-2.amazonaws.com/secure.notion-static.com/attachment-upload-1/sample.jpg",
                        "content_sha256": selftest_file_hash(
                            "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg"
                        ),
                    }
                ],
            )
            if valid_attachment_reuse != {
                "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg": [
                    {
                        "upload_id": "attachment-upload-1",
                        "content_sha256": selftest_file_hash(
                            "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg"
                        ),
                    }
                ]
            }:
                raise RuntimeError("첨부 업로드 셀프테스트 실패(현재 첨부 검증)")

            mixed_attachment_reuse = sync_module.extract_existing_uploaded_attachment_ids(
                {
                    "첨부파일": {
                        "files": [
                            {
                                "name": "sample.jpg",
                                "type": "file",
                                "file": {
                                    "url": "https://s3.us-west-2.amazonaws.com/secure.notion-static.com/attachment-upload-1/sample.jpg?X-Amz-Signature=abc"
                                },
                            },
                            {
                                "name": "sample.pdf",
                                "type": "external",
                                "external": {
                                    "url": "https://www.sogang.ac.kr/file-fe-prd/board/1/test.pdf?sg=test.pdf"
                                },
                            },
                        ]
                    }
                },
                [
                    {
                        "source_url": "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg",
                        "name": "sample.jpg",
                        "upload_id": "attachment-upload-1",
                        "hosted_file_key": "s3.us-west-2.amazonaws.com/secure.notion-static.com/attachment-upload-1/sample.jpg",
                        "content_sha256": selftest_file_hash(
                            "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg"
                        ),
                    }
                ],
            )
            if mixed_attachment_reuse != {
                "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg": [
                    {
                        "upload_id": "attachment-upload-1",
                        "content_sha256": selftest_file_hash(
                            "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg"
                        ),
                    }
                ]
            }:
                raise RuntimeError("첨부 업로드 셀프테스트 실패(혼합 상태 부분 재사용)")

            if sync_module.extract_existing_uploaded_attachment_ids(
                {
                    "첨부파일": {
                        "files": [
                            {
                                "name": "sample.jpg",
                                "type": "file",
                                "file": {
                                    "url": "https://s3.us-west-2.amazonaws.com/secure.notion-static.com/attachment-upload-current/sample.jpg?X-Amz-Signature=abc"
                                },
                            }
                        ]
                    }
                },
                [
                    {
                        "source_url": "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg",
                        "name": "sample.jpg",
                        "upload_id": "attachment-upload-stale",
                        "hosted_file_key": "s3.us-west-2.amazonaws.com/secure.notion-static.com/attachment-upload-stale/sample.jpg",
                    }
                ],
            ):
                raise RuntimeError("첨부 업로드 셀프테스트 실패(오래된 상태 차단)")

            attachment_removed_item = {
                "title": "첨부 제거 테스트",
                "top": False,
                "attachments_status": ATTACHMENTS_STATUS_KNOWN,
            }
            sync_module.normalize_item_attachments(attachment_removed_item)
            if (
                attachment_removed_item.get("attachments") != []
                or sync_module.build_properties(
                    attachment_removed_item,
                    has_views_property=False,
                    has_attachments_property=True,
                    has_classification_property=False,
                ).get("첨부파일")
                != {"files": []}
            ):
                raise RuntimeError("첨부 업로드 셀프테스트 실패(빈 첨부 제거)")

            attachment_unknown_item = {
                "title": "첨부 미확인 테스트",
                "top": False,
                "attachments_status": ATTACHMENTS_STATUS_UNKNOWN,
            }
            sync_module.normalize_item_attachments(attachment_unknown_item)
            if (
                "attachments" in attachment_unknown_item
                or sync_module.build_properties(
                    attachment_unknown_item,
                    has_views_property=False,
                    has_attachments_property=True,
                    has_classification_property=False,
                ).get("첨부파일")
                is not None
            ):
                raise RuntimeError("첨부 업로드 셀프테스트 실패(미확인 첨부 보존)")

            if classify_attachment_status_from_signals(
                [],
                {
                    "has_html": True,
                    "valid_detail": True,
                    "has_attachment_label": True,
                    "has_attachment_container": True,
                    "has_attachment_link": False,
                },
            ) != ATTACHMENTS_STATUS_KNOWN or classify_attachment_status_from_signals(
                [],
                {
                    "has_html": True,
                    "valid_detail": True,
                    "has_attachment_label": True,
                    "has_attachment_container": True,
                    "has_attachment_link": True,
                },
            ) != ATTACHMENTS_STATUS_UNKNOWN:
                raise RuntimeError("첨부 업로드 셀프테스트 실패(첨부 상태 분류)")

            sg_pdf_html = (
                '<div>첨부파일</div>'
                '<a href="https://www.sogang.ac.kr/files/sample.pdf?sg=sample.pdf">'
                "sample.pdf</a>"
            )
            sg_pdf_signals = build_detail_signals(sg_pdf_html)
            if (
                not sg_pdf_signals.get("has_attachment_link")
                or classify_attachment_status_from_signals([], sg_pdf_signals)
                != ATTACHMENTS_STATUS_UNKNOWN
            ):
                raise RuntimeError("첨부 업로드 셀프테스트 실패(.pdf sg 신호 감지)")

            api_sg_pdf_detail = {
                "title": "첨부 API 테스트",
                "regDate": "20260423120000",
                "content": sg_pdf_html,
            }
            api_sg_pdf_reason = get_detail_html_fallback_reason(
                api_sg_pdf_detail,
                entry_title="첨부 API 테스트",
            )
            api_sg_pdf_status = classify_attachment_status_from_api_detail(
                api_sg_pdf_detail,
                [],
                api_sg_pdf_reason,
                ATTACHMENTS_STATUS_UNKNOWN,
            )
            api_sg_pdf_item = {"title": "첨부 API 테스트", "top": False}
            apply_item_attachments(api_sg_pdf_item, [], api_sg_pdf_status)
            sync_module.normalize_item_attachments(api_sg_pdf_item)
            if (
                "attachment_missing" not in str(api_sg_pdf_reason or "")
                or api_sg_pdf_status != ATTACHMENTS_STATUS_UNKNOWN
                or sync_module.build_properties(
                    api_sg_pdf_item,
                    has_views_property=False,
                    has_attachments_property=True,
                    has_classification_property=False,
                ).get("첨부파일")
                is not None
            ):
                raise RuntimeError("첨부 업로드 셀프테스트 실패(API 본문의 첨부 보존)")

            original_fetch_detail_metadata_from_url = fetch_detail_metadata_from_url
            original_fetch_detail_metadata_via_playwright = (
                fetch_detail_metadata_via_playwright
            )
            original_should_retry_detail_fetch = should_retry_detail_fetch
            try:
                def fake_fetch_detail_metadata_from_url(
                    _detail_url: str,
                ) -> tuple[
                    Optional[str],
                    JsonObjectList,
                    JsonObjectList,
                    DetailSignals,
                ]:
                    return (
                        None,
                        [],
                        [],
                        {
                            "has_html": True,
                            "valid_detail": True,
                            "has_attachment_label": True,
                            "has_attachment_container": True,
                            "has_attachment_link": False,
                        },
                    )

                def fake_fetch_detail_metadata_via_playwright_unknown(
                    _page: Any,
                    _list_url: str,
                    _detail_url: str,
                ) -> tuple[
                    Optional[str],
                    JsonObjectList,
                    JsonObjectList,
                    Optional[str],
                ]:
                    return (None, [], [], ATTACHMENTS_STATUS_UNKNOWN)

                def fake_should_retry_detail_fetch(
                    _written_at: Optional[str],
                    _attachments: JsonObjectList,
                    _body_blocks: JsonObjectList,
                    _signals: DetailSignals,
                ) -> bool:
                    return True

                crawler_module.fetch_detail_metadata_from_url = (
                    fake_fetch_detail_metadata_from_url
                )
                crawler_module.fetch_detail_metadata_via_playwright = (
                    fake_fetch_detail_metadata_via_playwright_unknown
                )
                crawler_module.should_retry_detail_fetch = fake_should_retry_detail_fetch
                _, _, merged_attachments, _, merged_status = fetch_detail_for_row(
                    None,
                    "https://www.sogang.ac.kr/ko/notice",
                    0,
                    "https://www.sogang.ac.kr/ko/detail/123456?bbsConfigFk=141",
                )
                merged_item = {"title": "첨부 병합 테스트", "top": False}
                apply_item_attachments(merged_item, merged_attachments, merged_status)
                sync_module.normalize_item_attachments(merged_item)
                if (
                    merged_status != ATTACHMENTS_STATUS_UNKNOWN
                    or "attachments" in merged_item
                    or sync_module.build_properties(
                        merged_item,
                        has_views_property=False,
                        has_attachments_property=True,
                        has_classification_property=False,
                    ).get("첨부파일")
                    is not None
                ):
                    raise RuntimeError("첨부 업로드 셀프테스트 실패(Playwright 미확인 상태 병합)")

                def fake_fetch_detail_metadata_via_playwright_unavailable(
                    _page: Any,
                    _list_url: str,
                    _detail_url: str,
                ) -> tuple[
                    Optional[str],
                    JsonObjectList,
                    JsonObjectList,
                    Optional[str],
                ]:
                    return (None, [], [], None)

                crawler_module.fetch_detail_metadata_via_playwright = (
                    fake_fetch_detail_metadata_via_playwright_unavailable
                )
                _, _, unavailable_attachments, _, unavailable_status = fetch_detail_for_row(
                    None,
                    "https://www.sogang.ac.kr/ko/notice",
                    0,
                    "https://www.sogang.ac.kr/ko/detail/123456?bbsConfigFk=141",
                )
                unavailable_item = {"title": "첨부 병합 유지 테스트", "top": False}
                apply_item_attachments(
                    unavailable_item,
                    unavailable_attachments,
                    unavailable_status,
                )
                sync_module.normalize_item_attachments(unavailable_item)
                if (
                    unavailable_status != ATTACHMENTS_STATUS_KNOWN
                    or unavailable_item.get("attachments") != []
                    or sync_module.build_properties(
                        unavailable_item,
                        has_views_property=False,
                        has_attachments_property=True,
                        has_classification_property=False,
                    ).get("첨부파일")
                    != {"files": []}
                ):
                    raise RuntimeError("첨부 업로드 셀프테스트 실패(Playwright 상태 유지)")
            finally:
                crawler_module.fetch_detail_metadata_from_url = (
                    original_fetch_detail_metadata_from_url
                )
                crawler_module.fetch_detail_metadata_via_playwright = (
                    original_fetch_detail_metadata_via_playwright
                )
                crawler_module.should_retry_detail_fetch = (
                    original_should_retry_detail_fetch
                )

            def fake_top_level_failure(
                _token: str,
                _page_id: str,
            ) -> JsonObjectList:
                raise NotionRequestError(
                    "selftest-root-failure",
                    status_code=500,
                )

            setattr(
                sync_module,
                "find_sync_container_block",
                original_find_sync_container_block,
            )
            setattr(
                sync_module,
                "list_block_children",
                fake_top_level_failure,
            )
            if sync_module.extract_existing_uploaded_media_blocks(
                "selftest-token",
                "selftest-page",
                [
                    {
                        "type": "image",
                        "source_url": "https://www.sogang.ac.kr/file-fe-prd/board/1/test.jpg?sg=test.jpg",
                        "upload_id": "image-upload-sanitized",
                    }
                ],
            ):
                raise RuntimeError("본문 업로드 셀프테스트 실패(보조 컨테이너 조회)")
        finally:
            setattr(
                sync_module,
                "find_sync_container_block",
                original_find_sync_container_block,
            )
            setattr(
                sync_module,
                "list_block_children",
                original_list_block_children,
            )
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            if should_require_browser():
                raise RuntimeError(
                    "Playwright 브라우저 필수 셀프테스트를 실행할 수 없습니다"
                ) from exc
            LOGGER.info("Playwright 미설치: Playwright 셀프테스트 생략")
        else:
            pw_attachments: JsonObjectList = []
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=True)
                except Exception as exc:
                    if should_require_browser():
                        raise RuntimeError(
                            "Playwright 브라우저 필수 셀프테스트 실행 실패"
                        ) from exc
                    LOGGER.info(
                        "Playwright 브라우저 실행 실패: %s (셀프테스트 생략)",
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
        if notion_upload_backup is not None:
            import notion_client as notion_client_module

            notion_client_module.upload_external_file_to_notion = notion_upload_backup
            if notion_download_backup is not None:
                notion_client_module.download_file_bytes = notion_download_backup
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

class AttachmentPolicySelftestTests(unittest.TestCase):
    def test_attachment_policy(self) -> None:
        run_attachment_policy_selftest()


if __name__ == "__main__":
    unittest.main()
