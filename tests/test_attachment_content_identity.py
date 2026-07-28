import hashlib
import os
import sys
import unittest
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import notion_client
import sync
import sync_engine
import utils
from settings import ATTACHMENT_PROPERTY


class AttachmentContentIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        notion_client.FILE_UPLOAD_CACHE.clear()
        self.old_url = (
            "https://www.sogang.ac.kr/file-fe-prd/board/image.jpg"
            "?fileId=77&signature=old"
        )
        self.new_url = (
            "https://www.sogang.ac.kr/file-fe-prd/board/image.jpg"
            "?fileId=77&signature=new"
        )
        from PIL import Image

        old_buffer = BytesIO()
        Image.new("RGB", (2, 2), (10, 20, 30)).save(
            old_buffer,
            format="JPEG",
        )
        new_buffer = BytesIO()
        Image.new("RGB", (2, 2), (40, 50, 60)).save(
            new_buffer,
            format="JPEG",
        )
        self.old_bytes = old_buffer.getvalue()
        self.new_bytes = new_buffer.getvalue()
        self.old_hash = hashlib.sha256(self.old_bytes).hexdigest()
        self.new_hash = hashlib.sha256(self.new_bytes).hexdigest()

    def tearDown(self) -> None:
        notion_client.FILE_UPLOAD_CACHE.clear()

    def attachment(self, url: str) -> dict[str, Any]:
        return {
            "name": "image.jpg",
            "type": "external",
            "external": {"url": url},
        }

    def reusable_attachments(
        self,
        content_sha256: str,
    ) -> dict[str, list[dict[str, Any]]]:
        properties = {
            ATTACHMENT_PROPERTY: {
                "type": "files",
                "files": [
                    {
                        "name": "image.jpg",
                        "type": "file_upload",
                        "file_upload": {"id": "old-upload"},
                    }
                ],
            }
        }
        state = [
            {
                "source_url": self.old_url,
                "name": "image.jpg",
                "upload_id": "old-upload",
                "content_sha256": content_sha256,
            }
        ]
        return sync.extract_existing_uploaded_attachment_ids(properties, state)

    def test_signed_url_rotation_reuses_upload_only_after_content_match(self) -> None:
        reusable = self.reusable_attachments(self.old_hash)

        with (
            patch.object(
                notion_client,
                "download_file_bytes",
                return_value=(self.old_bytes, "image/jpeg"),
            ) as download,
            patch.object(
                notion_client,
                "upload_external_file_to_notion",
            ) as upload,
        ):
            attachments, state = notion_client.prepare_attachments_for_sync(
                "token",
                [self.attachment(self.new_url)],
                reusable_uploaded_attachments=reusable,
            )

        download.assert_called_once_with(
            self.new_url,
            require_file_hint=False,
        )
        upload.assert_not_called()
        self.assertEqual(
            attachments,
            [
                {
                    "name": "image.jpg",
                    "type": "file_upload",
                    "file_upload": {"id": "old-upload"},
                }
            ],
        )
        self.assertEqual(state[0]["content_sha256"], self.old_hash)
        self.assertEqual(
            state[0]["source_url"],
            utils.normalize_attachment_identity_url(self.new_url),
        )

    def test_same_normalized_url_with_changed_bytes_uploads_replacement(self) -> None:
        reusable = self.reusable_attachments(self.old_hash)

        with (
            patch.object(
                notion_client,
                "download_file_bytes",
                return_value=(self.new_bytes, "image/jpeg"),
            ) as download,
            patch.object(
                notion_client,
                "upload_external_file_to_notion",
                return_value="new-upload",
            ) as upload,
        ):
            attachments, state = notion_client.prepare_attachments_for_sync(
                "token",
                [self.attachment(self.new_url)],
                reusable_uploaded_attachments=reusable,
            )

        download.assert_called_once_with(
            self.new_url,
            require_file_hint=False,
        )
        upload.assert_called_once_with(
            "token",
            self.new_url,
            "image.jpg",
            expect_image=True,
            downloaded_file=(self.new_bytes, "image/jpeg"),
        )
        self.assertEqual(
            attachments[0]["file_upload"]["id"],
            "new-upload",
        )
        self.assertEqual(state[0]["content_sha256"], self.new_hash)

    def test_legacy_state_without_hash_is_not_reused(self) -> None:
        reusable = self.reusable_attachments("")

        with (
            patch.object(
                notion_client,
                "download_file_bytes",
                return_value=(self.old_bytes, "image/jpeg"),
            ),
            patch.object(
                notion_client,
                "upload_external_file_to_notion",
                return_value="migrated-upload",
            ) as upload,
        ):
            attachments, state = notion_client.prepare_attachments_for_sync(
                "token",
                [self.attachment(self.new_url)],
                reusable_uploaded_attachments=reusable,
            )

        upload.assert_called_once()
        self.assertEqual(
            attachments[0]["file_upload"]["id"],
            "migrated-upload",
        )
        self.assertEqual(state[0]["content_sha256"], self.old_hash)

    def test_operation_identity_uses_attachment_content_hash(self) -> None:
        item = {
            "source_id": "2",
            "notice_id": "77",
            "title": "콘텐츠 교체",
            "url": "https://www.sogang.ac.kr/ko/detail/77?bbsConfigFk=2",
            "attachments": [self.attachment(self.new_url)],
            "body_blocks": [],
        }
        old_state = [
            {
                "source_url": self.old_url,
                "name": "image.jpg",
                "upload_id": "old-upload",
                "content_sha256": self.old_hash,
            }
        ]
        rotated_state = [
            {
                **old_state[0],
                "source_url": self.new_url,
            }
        ]
        changed_state = [
            {
                **rotated_state[0],
                "upload_id": "new-upload",
                "content_sha256": self.new_hash,
            }
        ]

        old_identity = sync_engine.operation_id_for_item(
            item,
            attachment_state=old_state,
        )
        rotated_identity = sync_engine.operation_id_for_item(
            item,
            attachment_state=rotated_state,
        )
        changed_identity = sync_engine.operation_id_for_item(
            item,
            attachment_state=changed_state,
        )

        self.assertEqual(old_identity, rotated_identity)
        self.assertNotEqual(old_identity, changed_identity)

    def test_preflight_and_prepared_attachment_operation_identity_match(
        self,
    ) -> None:
        source_attachment = self.attachment(self.new_url)
        item = {
            "source_id": "2",
            "notice_id": "77",
            "title": "준비 전후 동일성",
            "url": "https://www.sogang.ac.kr/ko/detail/77?bbsConfigFk=2",
            "attachments": [source_attachment],
            "body_blocks": [],
        }
        state = [
            {
                "source_url": self.new_url,
                "name": "image.jpg",
                "upload_id": "new-upload",
                "content_sha256": self.new_hash,
            }
        ]
        preflight_identity = sync_engine.operation_id_for_item(
            item,
            attachment_state=state,
        )
        prepared_item = {
            **item,
            "attachments": [
                {
                    "name": "image.jpg",
                    "type": "file_upload",
                    "file_upload": {"id": "new-upload"},
                }
            ],
        }
        apply_identity = sync_engine.operation_id_for_item(
            prepared_item,
            attachment_state=state,
            attachment_entries=[source_attachment],
        )

        self.assertEqual(preflight_identity, apply_identity)

    def test_rejected_attachment_snapshot_preserves_operation_identity(
        self,
    ) -> None:
        source_attachment = self.attachment(self.new_url)
        item = {
            "source_id": "2",
            "notice_id": "77",
            "title": "차단 형식 보존",
            "url": "https://www.sogang.ac.kr/ko/detail/77?bbsConfigFk=2",
            "attachments": [source_attachment],
            "body_blocks": [],
        }
        rejected_payload = b"%PDF-1.6\n1 0 obj\n<<>>\nendobj\n%%EOF"

        with (
            notion_client.external_download_run_scope(force_new=True),
            patch.object(
                notion_client,
                "download_file_bytes",
                return_value=(rejected_payload, "application/pdf"),
            ) as download,
            patch.object(notion_client, "create_file_upload") as create,
        ):
            preflight_state = notion_client.collect_attachment_content_state(
                [source_attachment]
            )
            prepared, applied_state = (
                notion_client.prepare_attachments_for_sync(
                    "token",
                    [source_attachment],
                )
            )

        download.assert_called_once()
        create.assert_not_called()
        self.assertEqual(preflight_state, [])
        self.assertEqual(applied_state, [])
        self.assertEqual(prepared, [source_attachment])
        self.assertEqual(
            sync_engine.operation_id_for_item(
                item,
                attachment_state=preflight_state,
            ),
            sync_engine.operation_id_for_item(
                {**item, "attachments": prepared},
                attachment_state=applied_state,
                attachment_entries=[source_attachment],
            ),
        )

    def test_file_upload_cache_key_includes_content_hash(self) -> None:
        with (
            patch.object(
                notion_client,
                "download_file_bytes",
                side_effect=[
                    (self.old_bytes, "image/jpeg"),
                    (self.new_bytes, "image/jpeg"),
                ],
            ),
            patch.object(
                notion_client,
                "get_workspace_upload_limit",
                return_value=None,
            ),
            patch.object(
                notion_client,
                "create_file_upload",
                side_effect=[
                    {"id": "old-upload"},
                    {"id": "new-upload"},
                ],
            ) as create,
            patch.object(
                notion_client,
                "send_file_upload",
                return_value={"status": "uploaded"},
            ),
        ):
            old_upload = notion_client.upload_external_file_to_notion(
                "token",
                self.old_url,
            )
            new_upload = notion_client.upload_external_file_to_notion(
                "token",
                self.new_url,
            )

        self.assertEqual(old_upload, "old-upload")
        self.assertEqual(new_upload, "new-upload")
        self.assertEqual(create.call_count, 2)

    def test_preflight_cache_counts_repeated_url_bytes(self) -> None:
        cache = notion_client.ExternalPreflightDownloadCache(
            max_bytes=5
        )
        cache.add(
            self.old_url,
            False,
            (b"123", "image/jpeg"),
        )

        with self.assertRaisesRegex(RuntimeError, "캐시 용량"):
            cache.add(
                self.old_url,
                False,
                (b"456", "image/jpeg"),
            )

        self.assertEqual(cache.total_bytes, 3)
        self.assertEqual(
            cache.pop(self.old_url, False),
            (b"123", "image/jpeg"),
        )
        self.assertEqual(cache.total_bytes, 0)

    def test_preflight_cache_limit_fails_before_destination_write(
        self,
    ) -> None:
        item = {
            "source_id": "2",
            "notice_id": "77",
            "title": "캐시 상한",
            "url": "https://www.sogang.ac.kr/ko/detail/77?bbsConfigFk=2",
            "attachments": [
                self.attachment(self.old_url),
                self.attachment(self.old_url),
            ],
            "body_blocks": [],
        }
        context = sync_engine.DestinationContext(
            token="token",
            database_id="database",
        )

        with (
            patch.object(
                notion_client,
                "EXTERNAL_PREFLIGHT_CACHE_MAX_BYTES",
                5,
            ),
            patch.object(
                notion_client,
                "download_file_bytes",
                return_value=(b"123", "image/jpeg"),
            ),
            patch.object(
                sync_engine,
                "find_existing_page",
                return_value=None,
            ),
            patch.object(sync_engine, "create_page") as create,
            patch.object(sync_engine, "update_page") as update,
            notion_client.external_download_run_scope(force_new=True),
            self.assertRaisesRegex(RuntimeError, "캐시 용량"),
        ):
            sync_engine.resolve_destination_preflight(
                context,
                [item],
            )

        create.assert_not_called()
        update.assert_not_called()

    def test_preflight_snapshot_drives_same_pending_and_committed_operation(
        self,
    ) -> None:
        item = {
            "source_id": "2",
            "notice_id": "77",
            "title": "작업 ID 일치",
            "url": "https://www.sogang.ac.kr/ko/detail/77?bbsConfigFk=2",
            "top": False,
            "attachments": [self.attachment(self.new_url)],
            "body_blocks": [],
        }
        context = sync_engine.DestinationContext(
            token="token",
            database_id="database",
            has_views_property=False,
            has_attachments_property=True,
            has_classification_property=False,
        )
        counters = sync_engine.SyncCounters()
        with (
            notion_client.external_download_run_scope(force_new=True),
            patch.object(
                notion_client,
                "download_file_bytes",
                return_value=(self.new_bytes, "image/jpeg"),
            ) as download,
            patch.object(
                notion_client,
                "upload_external_file_to_notion",
                return_value="new-upload",
            ),
            patch.object(
                sync_engine,
                "find_existing_page",
                return_value=None,
            ),
            patch.object(
                sync_engine,
                "create_page",
                return_value="page-77",
            ) as create,
            patch.object(sync_engine, "verify_committed_item"),
            patch.object(sync_engine, "update_page") as update,
            patch.object(
                sync_engine,
                "enrich_attachment_state_with_page",
                side_effect=lambda _token, _page_id, state: state,
            ),
            patch.object(sync_engine, "check_run_control"),
        ):
            preflight = sync_engine.resolve_destination_preflight(
                context,
                [item],
            )[0]
            sync_engine.apply_item(
                context,
                item,
                counters,
                existing_page=None,
                existing_page_resolved=True,
                expected_operation_id=preflight.operation_id,
                expected_body_media_reuse_status=(
                    preflight.body_media_reuse_status
                ),
            )

        download.assert_called_once()
        pending_properties = create.call_args.args[2]
        committed_properties = update.call_args_list[-1].args[2]
        self.assertEqual(
            sync.rich_text_value_from_payload(
                pending_properties[sync.SYNC_OPERATION_PROPERTY]
            ),
            preflight.operation_id,
        )
        self.assertEqual(
            sync.rich_text_value_from_payload(
                committed_properties[sync.SYNC_OPERATION_PROPERTY]
            ),
            preflight.operation_id,
        )


class BodyMediaContentIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        notion_client.FILE_UPLOAD_CACHE.clear()
        self.old_url = (
            "https://www.sogang.ac.kr/file-fe-prd/board/body.jpg"
            "?fileId=88&signature=old"
        )
        self.new_url = (
            "https://www.sogang.ac.kr/file-fe-prd/board/body.jpg"
            "?fileId=88&signature=new"
        )
        self.old_bytes = b"old-body-image"
        self.new_bytes = b"new-body-image"
        self.old_hash = hashlib.sha256(self.old_bytes).hexdigest()
        self.new_hash = hashlib.sha256(self.new_bytes).hexdigest()
        self.block = {
            "object": "block",
            "type": "image",
            "image": {
                "type": "external",
                "external": {"url": self.new_url},
            },
        }
        self.uploaded_block = {
            "object": "block",
            "type": "image",
            "image": {
                "type": "file_upload",
                "file_upload": {"id": "old-body-upload"},
            },
        }

    def tearDown(self) -> None:
        notion_client.FILE_UPLOAD_CACHE.clear()

    def reusable_media(
        self,
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        return {
            (
                "image",
                utils.normalize_attachment_identity_url(self.old_url),
            ): [
                {
                    "block": self.uploaded_block,
                    "content_sha256": self.old_hash,
                }
            ]
        }

    def hosted_block(
        self,
        media_type: str,
        hosted_path: str,
    ) -> dict[str, Any]:
        return {
            "id": f"{media_type}-block",
            "type": media_type,
            media_type: {
                "type": "file",
                "file": {
                    "url": (
                        "https://notionusercontent.com/"
                        f"{hosted_path}?signature=current"
                    )
                },
            },
        }

    def media_state(
        self,
        media_type: str,
        hosted_path: str,
    ) -> list[dict[str, Any]]:
        return [
            {
                "type": media_type,
                "source_url": self.old_url,
                "upload_id": f"{media_type}-upload",
                "block_id": f"{media_type}-block",
                "hosted_file_key": (
                    f"notionusercontent.com/{hosted_path}"
                ),
                "content_sha256": self.old_hash,
            }
        ]

    def test_hosted_file_drift_is_explicit_for_every_media_type(
        self,
    ) -> None:
        for media_type in ("image", "file", "pdf"):
            with (
                self.subTest(media_type=media_type),
                patch.object(
                    sync,
                    "find_sync_container_block",
                    return_value={"id": "container"},
                ),
                patch.object(
                    sync,
                    "list_block_children",
                    return_value=[
                        self.hosted_block(
                            media_type,
                            "changed/path",
                        )
                    ],
                ),
            ):
                reusable, status = (
                    sync.inspect_existing_uploaded_media_blocks(
                        "token",
                        "page",
                        self.media_state(
                            media_type,
                            "original/path",
                        ),
                    )
                )

            self.assertEqual(reusable, {})
            self.assertEqual(status, "drift")

    def test_hosted_signed_query_rotation_remains_valid(self) -> None:
        with (
            patch.object(
                sync,
                "find_sync_container_block",
                return_value={"id": "container"},
            ),
            patch.object(
                sync,
                "list_block_children",
                return_value=[
                    self.hosted_block("image", "stable/path")
                ],
            ),
        ):
            reusable, status = (
                sync.inspect_existing_uploaded_media_blocks(
                    "token",
                    "page",
                    self.media_state("image", "stable/path"),
                )
            )

        self.assertEqual(status, "valid")
        self.assertEqual(
            reusable[
                (
                    "image",
                    utils.normalize_attachment_identity_url(
                        self.old_url
                    ),
                )
            ][0]["content_sha256"],
            self.old_hash,
        )

    def test_verified_legacy_media_without_upload_id_is_drift(self) -> None:
        state = self.media_state("image", "stable/path")
        state[0].pop("upload_id")
        with (
            patch.object(
                sync,
                "find_sync_container_block",
                return_value={"id": "container"},
            ),
            patch.object(
                sync,
                "list_block_children",
                return_value=[
                    self.hosted_block("image", "stable/path")
                ],
            ),
        ):
            reusable, status = (
                sync.inspect_existing_uploaded_media_blocks(
                    "token",
                    "page",
                    state,
                )
            )

        self.assertEqual(reusable, {})
        self.assertEqual(status, "drift")

    def test_ambiguous_legacy_media_without_upload_id_is_unavailable(
        self,
    ) -> None:
        state = self.media_state("image", "stable/path")
        state[0].pop("upload_id")
        state[0].pop("hosted_file_key")
        with (
            patch.object(
                sync,
                "find_sync_container_block",
                return_value={"id": "container"},
            ),
            patch.object(
                sync,
                "list_block_children",
                return_value=[
                    self.hosted_block("image", "stable/path")
                ],
            ),
        ):
            reusable, status = (
                sync.inspect_existing_uploaded_media_blocks(
                    "token",
                    "page",
                    state,
                )
            )

        self.assertEqual(reusable, {})
        self.assertEqual(status, "unavailable")

    def test_hosted_media_read_failure_is_unavailable(self) -> None:
        with patch.object(
            sync,
            "find_sync_container_block",
            side_effect=notion_client.NotionRequestError(
                "temporary"
            ),
        ):
            reusable, status = (
                sync.inspect_existing_uploaded_media_blocks(
                    "token",
                    "page",
                    self.media_state("image", "stable/path"),
                )
            )

        self.assertEqual(reusable, {})
        self.assertEqual(status, "unavailable")

    def test_rotated_body_url_reuses_only_matching_content(self) -> None:
        with (
            patch.object(
                notion_client,
                "download_file_bytes",
                return_value=(self.old_bytes, "image/jpeg"),
            ) as download,
            patch.object(
                notion_client,
                "upload_external_file_to_notion",
            ) as upload,
        ):
            blocks, hash_blocks, state = (
                notion_client.prepare_body_blocks_for_sync(
                    "token",
                    [self.block],
                    reusable_uploaded_media=self.reusable_media(),
                )
            )

        download.assert_called_once_with(
            self.new_url,
            require_file_hint=False,
        )
        upload.assert_not_called()
        self.assertEqual(
            blocks[0]["image"]["file_upload"]["id"],
            "old-body-upload",
        )
        self.assertEqual(
            hash_blocks[0]["image"]["content_sha256"],
            self.old_hash,
        )
        self.assertEqual(state[0]["content_sha256"], self.old_hash)

    def test_changed_body_bytes_create_new_upload_and_hash(self) -> None:
        with (
            patch.object(
                notion_client,
                "download_file_bytes",
                return_value=(self.new_bytes, "image/jpeg"),
            ),
            patch.object(
                notion_client,
                "upload_external_file_to_notion",
                return_value="new-body-upload",
            ) as upload,
        ):
            blocks, hash_blocks, state = (
                notion_client.prepare_body_blocks_for_sync(
                    "token",
                    [self.block],
                    reusable_uploaded_media=self.reusable_media(),
                )
            )

        upload.assert_called_once_with(
            "token",
            self.new_url,
            "body.jpg",
            expect_image=True,
            downloaded_file=(self.new_bytes, "image/jpeg"),
        )
        self.assertEqual(
            blocks[0]["image"]["file_upload"]["id"],
            "new-body-upload",
        )
        self.assertEqual(
            hash_blocks[0]["image"]["content_sha256"],
            self.new_hash,
        )
        self.assertEqual(state[0]["content_sha256"], self.new_hash)

    def test_rejected_body_file_snapshot_preserves_operation_identity(
        self,
    ) -> None:
        url = (
            "https://scc.sogang.ac.kr/Download3"
            "?pathStr=1&fileName=notice.pdf"
        )
        block = {
            "object": "block",
            "type": "embed",
            "embed": {"url": url},
        }
        item = {
            "source_id": "2",
            "notice_id": "77",
            "title": "차단 본문 파일 보존",
            "url": "https://www.sogang.ac.kr/ko/detail/77?bbsConfigFk=2",
            "attachments": [],
            "body_blocks": [block],
        }
        rejected_payload = b"%PDF-1.6\r1 0 obj\r<<>>\rendobj"

        with (
            notion_client.external_download_run_scope(force_new=True),
            patch.object(
                notion_client,
                "download_file_bytes",
                return_value=(rejected_payload, "application/pdf"),
            ) as download,
            patch.object(notion_client, "create_file_upload") as create,
        ):
            preflight_state = (
                notion_client.collect_body_media_content_state([block])
            )
            blocks, hash_blocks, applied_state = (
                notion_client.prepare_body_blocks_for_sync(
                    "token",
                    [block],
                )
            )

        download.assert_called_once()
        create.assert_not_called()
        self.assertEqual(preflight_state, [])
        self.assertEqual(applied_state, [])
        self.assertEqual(blocks, [block])
        self.assertEqual(hash_blocks, [block])
        self.assertEqual(
            sync_engine.operation_id_for_item(
                item,
                body_media_state=preflight_state,
            ),
            sync_engine.operation_id_for_item(
                item,
                body_media_state=applied_state,
            ),
        )

    def test_rejected_large_body_image_preserves_external_block_identity(
        self,
    ) -> None:
        from PIL import Image

        url = (
            "https://www.sogang.ac.kr/dataview/board/141/"
            "large-poster.jpg"
        )
        block = {
            "object": "block",
            "type": "image",
            "image": {
                "type": "external",
                "external": {"url": url},
            },
        }
        item = {
            "source_id": "141",
            "notice_id": "549836",
            "title": "대형 이미지 본문 보존",
            "url": (
                "https://www.sogang.ac.kr/ko/detail/549836"
                "?bbsConfigFk=141"
            ),
            "attachments": [],
            "body_blocks": [block],
        }
        buffer = BytesIO()
        Image.new("RGB", (100, 100), (10, 20, 30)).save(
            buffer,
            format="JPEG",
        )
        payload = buffer.getvalue()

        with (
            patch.dict(
                os.environ,
                {
                    "IMAGE_MAX_PIXELS": "9999",
                    "IMAGE_MAX_DIMENSION": "100",
                },
            ),
            notion_client.external_download_run_scope(force_new=True),
            patch.object(
                notion_client,
                "download_file_bytes",
                return_value=(payload, "image/jpeg"),
            ) as download,
            patch.object(notion_client, "create_file_upload") as create,
        ):
            preflight_state = (
                notion_client.collect_body_media_content_state([block])
            )
            blocks, hash_blocks, applied_state = (
                notion_client.prepare_body_blocks_for_sync(
                    "token",
                    [block],
                )
            )

        download.assert_called_once()
        create.assert_not_called()
        self.assertEqual(preflight_state, [])
        self.assertEqual(applied_state, [])
        self.assertEqual(blocks, [block])
        self.assertEqual(hash_blocks, [block])
        self.assertEqual(
            sync_engine.operation_id_for_item(
                item,
                body_media_state=preflight_state,
            ),
            sync_engine.operation_id_for_item(
                item,
                body_media_state=applied_state,
            ),
        )


if __name__ == "__main__":
    unittest.main()
