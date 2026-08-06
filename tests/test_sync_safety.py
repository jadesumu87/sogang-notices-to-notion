import copy
import json
import socket
import sys
import unittest
import urllib.request
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sync
import sync_engine
import notion_client
import settings
import utils
from models import (
    CrawlReport,
    DestinationConsistencyError,
    MutationKind,
    SourceCrawlResult,
    SourceSpec,
    SourceStatus,
    SyncCounters,
)


def paragraph_block(text: str = "") -> dict:
    return {
        "type": "paragraph",
        "paragraph": {
            "rich_text": (
                [{"type": "text", "text": {"content": text}}] if text else []
            )
        },
    }


def jpeg_payload() -> bytes:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (2, 2), (10, 20, 30)).save(
        buffer,
        format="JPEG",
    )
    return buffer.getvalue()


def managed_container(
    block_id: str,
    generation_id: str,
    part: int = 1,
    total: int = 1,
    content_hash: str = "",
    body_rich_text=None,
) -> dict:
    marker = sync.ensure_sync_marker_in_rich_text(
        [],
        generation_id,
        part,
        total,
        content_hash,
    )
    return {
        "id": block_id,
        "type": "quote",
        "quote": {
            "rich_text": marker + list(body_rich_text or [])
        },
        "has_children": True,
    }


def rich_text_property(value: str) -> dict:
    return {
        "type": "rich_text",
        "rich_text": [{"plain_text": value}],
    }


def successful_body_sync(*_args, **kwargs) -> str:
    generation_id = str(kwargs["generation_id"])
    manifest_out = kwargs.get("manifest_out")
    if isinstance(manifest_out, dict):
        manifest_out.update(
            {
                "v": 2,
                "g": generation_id,
                "s": "committed",
                "op": str(kwargs.get("operation_id") or generation_id),
                "t": 1,
                "p": [
                    {
                        "i": "new-body",
                        "n": 1,
                        "h": "a" * 64,
                    }
                ],
                "o": [],
            }
        )
    return generation_id


def complete_destination_schema() -> dict:
    expected = {
        settings.TITLE_PROPERTY: "title",
        settings.TOP_PROPERTY: "checkbox",
        settings.DATE_PROPERTY: "date",
        settings.AUTHOR_PROPERTY: "select",
        settings.URL_PROPERTY: "url",
        settings.TYPE_PROPERTY: "select",
        settings.SYNC_OWNER_PROPERTY: "rich_text",
        settings.SOURCE_KEY_PROPERTY: "rich_text",
        settings.NOTICE_ID_PROPERTY: "rich_text",
        settings.SYNC_GENERATION_PROPERTY: "rich_text",
        settings.SYNC_STATUS_PROPERTY: "rich_text",
        settings.SYNC_OPERATION_PROPERTY: "rich_text",
        settings.ATTACHMENT_PROPERTY: "files",
        settings.ATTACHMENT_STATE_PROPERTY: "rich_text",
        settings.BODY_HASH_PROPERTY: "rich_text",
        settings.BODY_MEDIA_STATE_PROPERTY: "rich_text",
        settings.CLASSIFICATION_PROPERTY: "select",
        settings.VIEWS_PROPERTY: "number",
    }
    return {
        name: {"type": property_type, property_type: {}}
        for name, property_type in expected.items()
    }


def notion_read_properties(properties: dict) -> dict:
    normalized = copy.deepcopy(properties)
    for property_value in normalized.values():
        for field_name in ("title", "rich_text"):
            for part in property_value.get(field_name, []):
                if "plain_text" in part:
                    continue
                part["plain_text"] = str(
                    part.get("text", {}).get("content") or ""
                )
    return normalized


def managed_page(page_id: str, source_id: str, notice_id: str) -> dict:
    return {
        "id": page_id,
        "properties": {
            sync.SYNC_OWNER_PROPERTY: rich_text_property(sync.SYNC_OWNER_VALUE),
            sync.SOURCE_KEY_PROPERTY: rich_text_property(source_id),
            sync.NOTICE_ID_PROPERTY: rich_text_property(notice_id),
            sync.TITLE_PROPERTY: {
                "type": "title",
                "title": [{"plain_text": f"공지 {notice_id}"}],
            },
            sync.TOP_PROPERTY: {"type": "checkbox", "checkbox": True},
        },
    }


def source_result(
    source_id: str,
    status: SourceStatus,
    items=None,
) -> SourceCrawlResult:
    classification = "장학공지" if source_id == "141" else "학사공지"
    source = SourceSpec(
        config_fk=source_id,
        classification=classification,
        list_url=f"https://www.sogang.ac.kr/source/{source_id}",
    )
    values = [
        {
            **item,
            "completeness": item.get("completeness", "complete"),
            "body_status": item.get(
                "body_status",
                "present"
                if item.get("body_blocks")
                else "confirmed_empty",
            ),
            "attachments_status": item.get(
                "attachments_status",
                "known",
            ),
        }
        for item in list(items or [])
    ]
    return SourceCrawlResult(
        source=source,
        status=status,
        items=values,
        observed_count=len(values),
        observed_ids=[
            str(item.get("notice_id") or "") for item in values
        ],
        terminal_reached=status in {
            SourceStatus.SUCCESS,
            SourceStatus.VALID_EMPTY,
        },
        termination_reason=(
            "natural_end"
            if status
            in {SourceStatus.SUCCESS, SourceStatus.VALID_EMPTY}
            else ""
        ),
        full_snapshot=status in {
            SourceStatus.SUCCESS,
            SourceStatus.VALID_EMPTY,
        },
    )


class StatefulBlockStore:
    def __init__(self, creation_mode: str = "success"):
        self.creation_mode = creation_mode
        self.root_id = "page"
        self.old_id = "old-managed"
        self.manual_id = "manual-block"
        self.root_blocks = [
            {
                "id": self.manual_id,
                "type": "paragraph",
                "paragraph": {"rich_text": [{"plain_text": "manual"}]},
            },
            managed_container(self.old_id, "old-generation"),
        ]
        self.children = {self.old_id: [paragraph_block("old")]}
        self.properties = {
            sync.SYNC_GENERATION_PROPERTY: {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": "old-generation"},
                    }
                ]
            }
        }
        self.deleted_ids = []
        self.events = []
        self.append_count = 0
        self.root_append_count = 0
        self.child_response_lost = False
        self.old_present_during_append = False
        self.child_batch_sizes = []
        self.root_payloads = []

    def list_children(self, token: str, parent_id: str) -> list[dict]:
        self.events.append(("list", parent_id))
        if parent_id == self.root_id:
            return copy.deepcopy(self.root_blocks)
        return copy.deepcopy(self.children.get(parent_id, []))

    def append_children(
        self,
        token: str,
        parent_id: str,
        blocks: list[dict],
    ) -> dict:
        self.events.append(("append", parent_id))
        self.append_count += 1
        if parent_id != self.root_id:
            self.child_batch_sizes.append(len(blocks))
            existing = self.children.setdefault(parent_id, [])
            appended = copy.deepcopy(blocks)
            for index, child in enumerate(
                appended,
                start=len(existing) + 1,
            ):
                child.setdefault(
                    "id",
                    f"{parent_id}-child-{index}",
                )
            existing.extend(appended)
            if (
                self.creation_mode == "child_response_loss"
                and not self.child_response_lost
            ):
                self.child_response_lost = True
                raise RuntimeError("child append response lost")
            return {"results": copy.deepcopy(appended)}
        self.old_present_during_append = any(
            block.get("id") == self.old_id for block in self.root_blocks
        )
        if self.creation_mode == "append_failure":
            raise RuntimeError("append failed")
        if self.creation_mode == "missing_creation":
            return {}
        payload = copy.deepcopy(blocks[0])
        self.root_payloads.append(copy.deepcopy(payload))
        child_blocks = payload.get("quote", {}).pop(
            "children",
            [],
        )
        self.root_append_count += 1
        block_id = f"new-managed-{self.root_append_count}"
        payload["id"] = block_id
        payload["has_children"] = bool(child_blocks)
        self.root_blocks.append(payload)
        self.children[block_id] = (
            [paragraph_block("corrupt")]
            if self.creation_mode == "verification_failure"
            else [
                {
                    "type": "heading_2",
                    "heading_2": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {"content": "corrupt"},
                            }
                        ]
                    },
                }
            ]
            if self.creation_mode == "same_type_corruption"
            else copy.deepcopy(child_blocks)
        )
        for index, child in enumerate(self.children[block_id], start=1):
            child.setdefault("id", f"{block_id}-child-{index}")
        return {"results": [copy.deepcopy(payload)]}

    def delete(self, token: str, block_id: str) -> None:
        self.events.append(("delete", block_id))
        self.deleted_ids.append(block_id)
        self.root_blocks = [
            block
            for block in self.root_blocks
            if block.get("id") != block_id
        ]
        self.children.pop(block_id, None)

    def retrieve(self, token: str, page_id: str) -> dict:
        self.events.append(("retrieve", page_id))
        return {
            "id": page_id,
            "properties": notion_read_properties(self.properties),
        }

    def update(
        self,
        token: str,
        page_id: str,
        properties: dict,
    ) -> None:
        self.events.append(("update", page_id))
        self.properties.update(copy.deepcopy(properties))

    def root_ids(self) -> list[str]:
        return [
            str(block.get("id") or "")
            for block in self.root_blocks
        ]


class SyncSafetyTests(unittest.TestCase):
    def test_property_comparison_accepts_notion_minute_precision_date(self):
        existing = {
            sync.DATE_PROPERTY: {
                "type": "date",
                "date": {
                    "start": "2026-07-13T10:08:00.000+09:00"
                },
            }
        }
        desired = {
            sync.DATE_PROPERTY: {
                "date": {"start": "2026-07-13T10:08:24+09:00"}
            }
        }

        self.assertEqual(
            sync.filter_changed_properties(existing, desired),
            {},
        )

    def test_property_comparison_rejects_different_notion_date_minute(self):
        existing = {
            sync.DATE_PROPERTY: {
                "type": "date",
                "date": {
                    "start": "2026-07-13T10:08:00.000+09:00"
                },
            }
        }
        desired = {
            sync.DATE_PROPERTY: {
                "date": {"start": "2026-07-13T10:09:00+09:00"}
            }
        }

        self.assertEqual(
            sync.filter_changed_properties(existing, desired),
            desired,
        )

    def test_property_comparison_accepts_equivalent_date_timezone(self):
        existing = {
            sync.DATE_PROPERTY: {
                "type": "date",
                "date": {"start": "2026-07-13T01:08:00.000Z"},
            }
        }
        desired = {
            sync.DATE_PROPERTY: {
                "date": {"start": "2026-07-13T10:08:59+09:00"}
            }
        }

        self.assertEqual(
            sync.filter_changed_properties(existing, desired),
            {},
        )

    def test_committed_readback_accepts_notion_truncated_date_seconds(self):
        item = {
            "source_id": "141",
            "notice_id": "550491",
            "title": "[교외] 가송재단 장학생 선발 안내",
            "author": "학생지원팀",
            "date": "2026-07-13T10:08:24+09:00",
            "views": 1252,
            "top": True,
            "url": (
                "https://www.sogang.ac.kr/ko/detail/550491"
                "?bbsConfigFk=141"
            ),
            "type": "교외",
            "classification": "장학공지",
        }
        operation_id = sync_engine.operation_id_for_item(item)
        committed_item = {
            **item,
            "operation_id": operation_id,
            "generation_id": "generation",
            "sync_status": "committed",
        }
        properties = notion_read_properties(
            sync_engine.build_properties(
                committed_item,
                True,
                True,
                True,
            )
        )
        properties[sync.DATE_PROPERTY]["date"]["start"] = (
            "2026-07-13T10:08:00.000+09:00"
        )
        page = {
            "id": "page-550491",
            "properties": properties,
        }

        reasons = sync_engine.committed_item_readback_reasons(
            "token",
            page,
            item,
            sync_engine.DestinationContext("token", "database"),
            "page-550491",
            operation_id,
            "generation",
            "",
            [],
            [],
            False,
        )

        self.assertEqual(reasons, [])

    def setUp(self):
        self.network_guards = [
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=AssertionError("network access"),
            ),
            patch.object(
                urllib.request.OpenerDirector,
                "open",
                side_effect=AssertionError("network access"),
            ),
            patch.object(
                socket,
                "create_connection",
                side_effect=AssertionError("network access"),
            ),
            patch.object(
                socket,
                "socket",
                side_effect=AssertionError("network access"),
            ),
        ]
        for guard in self.network_guards:
            guard.start()

    def tearDown(self):
        for guard in reversed(self.network_guards):
            guard.stop()

    def run_body_sync(
        self,
        store: StatefulBlockStore,
        generation_id: str = "new-generation",
        blocks: list[dict] | None = None,
        allow_untracked_recovery: bool = False,
    ) -> str:
        blocks = blocks or [
            {
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [
                        {"type": "text", "text": {"content": "new body"}}
                    ]
                },
            }
        ]
        with (
            patch.object(
                sync,
                "list_block_children",
                side_effect=store.list_children,
            ),
            patch.object(
                sync,
                "append_block_children",
                side_effect=store.append_children,
            ),
            patch.object(sync, "delete_block", side_effect=store.delete),
            patch.object(
                sync,
                "retrieve_page",
                side_effect=store.retrieve,
            ),
            patch.object(
                sync,
                "update_page",
                side_effect=store.update,
            ),
        ):
            return sync.sync_page_body_blocks(
                "token",
                store.root_id,
                blocks,
                generation_id=generation_id,
                allow_untracked_recovery=allow_untracked_recovery,
            )

    def test_body_generation_failures_preserve_old_and_manual_blocks(self):
        cases = (
            "append_failure",
            "missing_creation",
            "verification_failure",
            "same_type_corruption",
        )
        for creation_mode in cases:
            with self.subTest(creation_mode=creation_mode):
                store = StatefulBlockStore(creation_mode)
                with self.assertRaises(RuntimeError):
                    self.run_body_sync(store)

                self.assertIn(store.old_id, store.root_ids())
                self.assertIn(store.manual_id, store.root_ids())
                self.assertNotIn(store.old_id, store.deleted_ids)
                self.assertNotIn(store.manual_id, store.deleted_ids)

    def test_verified_failure_retry_reuses_generation_without_duplicate(self):
        store = StatefulBlockStore("verification_failure")
        with self.assertRaisesRegex(RuntimeError, "세대 검증 실패"):
            self.run_body_sync(store)
        failed_candidate = "new-managed-1"
        self.assertIn(failed_candidate, store.root_ids())

        store.creation_mode = "success"
        result = self.run_body_sync(store)

        self.assertEqual(result, "new-generation")
        self.assertNotIn(failed_candidate, store.root_ids())
        self.assertNotIn(store.old_id, store.root_ids())
        self.assertIn(failed_candidate, store.deleted_ids)
        self.assertEqual(
            len(
                [
                    block
                    for block in store.root_blocks
                    if block.get("type") == "quote"
                ]
            ),
            1,
        )

    def test_body_generation_deletes_old_only_after_success(self):
        store = StatefulBlockStore()

        result = self.run_body_sync(store)

        self.assertEqual(result, "new-generation")
        self.assertTrue(store.old_present_during_append)
        self.assertNotIn(store.old_id, store.root_ids())
        self.assertIn(store.manual_id, store.root_ids())
        self.assertIn(store.old_id, store.deleted_ids)
        self.assertNotIn(store.manual_id, store.deleted_ids)
        append_index = store.events.index(("append", store.root_id))
        verify_index = store.events.index(("list", "new-managed-1"))
        delete_index = store.events.index(("delete", store.old_id))
        self.assertLess(append_index, delete_index)
        self.assertLess(verify_index, delete_index)

    def test_quote_creation_nests_children_in_quote_payload(self):
        store = StatefulBlockStore()

        self.run_body_sync(
            store,
            blocks=[
                paragraph_block("첫 문단"),
                paragraph_block("둘째 문단"),
            ],
        )

        payload = store.root_payloads[0]
        self.assertNotIn("children", payload)
        self.assertEqual(
            payload["quote"]["children"],
            [paragraph_block("둘째 문단")],
        )

    def test_default_block_color_matches_omitted_source_color(self):
        for block_type in sync.DEFAULT_COLOR_BLOCK_TYPES:
            with self.subTest(block_type=block_type):
                source = {
                    "type": block_type,
                    block_type: {},
                }
                notion = copy.deepcopy(source)
                notion[block_type]["color"] = "default"

                self.assertEqual(
                    sync.block_content_signature(
                        "token",
                        source,
                        False,
                    ),
                    sync.block_content_signature(
                        "token",
                        notion,
                        False,
                    ),
                )
                notion[block_type]["color"] = "red"
                self.assertNotEqual(
                    sync.block_content_signature(
                        "token",
                        source,
                        False,
                    ),
                    sync.block_content_signature(
                        "token",
                        notion,
                        False,
                    ),
                )

    def test_split_body_parts_preserves_first_real_block_after_empty_prefix(self):
        visible = paragraph_block("첫 문단")
        heading = {
            "type": "heading_2",
            "heading_2": {
                "rich_text": [
                    {"type": "text", "text": {"content": "제목"}}
                ]
            },
        }
        for empty_count in (1, 2):
            with self.subTest(
                empty_count=empty_count,
                first_type="paragraph",
            ):
                rich_text, body_chunks = (
                    sync.split_body_container_parts(
                        [
                            *[paragraph_block() for _ in range(empty_count)],
                            visible,
                            heading,
                        ]
                    )
                )
                self.assertEqual(
                    rich_text,
                    visible["paragraph"]["rich_text"],
                )
                self.assertEqual(body_chunks, [[heading]])
            with self.subTest(
                empty_count=empty_count,
                first_type="heading",
            ):
                rich_text, body_chunks = (
                    sync.split_body_container_parts(
                        [
                            *[paragraph_block() for _ in range(empty_count)],
                            heading,
                        ]
                    )
                )
                self.assertEqual(
                    rich_text,
                    utils.build_space_rich_text(),
                )
                self.assertEqual(body_chunks, [[heading]])

    def test_body_quote_keeps_visible_rich_text_byte_structure(self):
        rich_text = [
            {
                "type": "text",
                "text": {
                    "content": "독자 본문",
                    "link": {"url": "https://www.sogang.ac.kr"},
                },
                "annotations": {
                    **utils.DEFAULT_ANNOTATIONS,
                    "bold": True,
                },
            }
        ]
        blocks = [
            {
                "type": "paragraph",
                "paragraph": {"rich_text": copy.deepcopy(rich_text)},
            },
            {
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [
                        {"type": "text", "text": {"content": "하위 본문"}}
                    ]
                },
            },
        ]
        store = StatefulBlockStore()

        self.run_body_sync(store, blocks=blocks)

        quotes = [
            block
            for block in store.root_blocks
            if block.get("type") == "quote"
        ]
        self.assertEqual(len(quotes), 1)
        self.assertEqual(
            quotes[0]["quote"]["rich_text"],
            rich_text,
        )
        visible_text = sync.rich_text_plain_text(
            quotes[0]["quote"]["rich_text"]
        )
        for internal_value in (
            sync.SYNC_CONTAINER_MARKER,
            "GENERATION:",
            "CONTENT_SHA256:",
        ):
            self.assertNotIn(internal_value, visible_text)

    def test_single_quote_media_uses_every_child_batch(self):
        remaining: list[dict] = []
        media_specs = {
            50: ("part-two", "upload-two"),
            100: ("part-three", "upload-three"),
        }
        for index in range(101):
            if index in media_specs:
                _, upload_id = media_specs[index]
                remaining.append(
                    {
                        "type": "image",
                        "image": {
                            "type": "file_upload",
                            "file_upload": {"id": upload_id},
                        },
                    }
                )
            else:
                remaining.append(paragraph_block(f"본문 {index}"))
        store = StatefulBlockStore()
        generation_id = "multipart-generation"
        self.run_body_sync(
            store,
            generation_id=generation_id,
            blocks=[paragraph_block("첫 문단"), *remaining],
        )
        media_state = [
            {
                "type": "image",
                "source_url": (
                    "https://www.sogang.ac.kr/file-fe-prd/board/"
                    f"{name}.png"
                ),
                "upload_id": upload_id,
                "generation_id": generation_id,
            }
            for name, upload_id in media_specs.values()
        ]
        with (
            patch.object(
                sync,
                "list_block_children",
                side_effect=store.list_children,
            ),
            patch.object(
                sync,
                "retrieve_page",
                side_effect=store.retrieve,
            ),
        ):
            enriched = sync.enrich_body_media_state_with_block_ids(
                "token",
                store.root_id,
                media_state,
                generation_id,
            )
            reusable, status = (
                sync.inspect_existing_uploaded_media_blocks(
                    "token",
                    store.root_id,
                    enriched,
                )
            )
            self.assertTrue(
                sync.is_body_generation_current(
                    "token",
                    store.root_id,
                    generation_id,
                )
            )

        manifest = sync.extract_body_generation_manifest(
            notion_read_properties(store.properties)
        )
        self.assertEqual(manifest["t"], 1)
        self.assertEqual(len(manifest["p"]), 1)
        self.assertEqual(
            [entry["block_id"] for entry in enriched],
            [
                "new-managed-1-child-51",
                "new-managed-1-child-101",
            ],
        )
        self.assertEqual(status, "valid")
        self.assertEqual(sum(map(len, reusable.values())), 2)
        quotes = [
            block
            for block in store.root_blocks
            if block.get("type") == "quote"
        ]
        self.assertEqual(len(quotes), 1)
        self.assertEqual(
            len(store.children[quotes[0]["id"]]),
            101,
        )
        self.assertFalse(
            sync.has_sync_marker(
                quotes[0]["quote"]["rich_text"]
            )
        )

    def test_single_quote_preserves_child_boundaries(self):
        for child_count in (51, 101, 166):
            with self.subTest(child_count=child_count):
                store = StatefulBlockStore()
                self.run_body_sync(
                    store,
                    generation_id=f"boundary-{child_count}",
                    blocks=[
                        paragraph_block("첫 문단"),
                        *[
                            paragraph_block(f"본문 {index}")
                            for index in range(child_count)
                        ],
                    ],
                )
                quotes = [
                    block
                    for block in store.root_blocks
                    if block.get("type") == "quote"
                ]
                manifest = sync.extract_body_generation_manifest(
                    notion_read_properties(store.properties)
                )

                self.assertEqual(len(quotes), 1)
                self.assertEqual(
                    len(store.children[quotes[0]["id"]]),
                    child_count,
                )
                self.assertEqual(manifest["t"], 1)
                self.assertEqual(len(manifest["p"]), 1)

    def test_child_append_response_loss_converges_without_duplicates(self):
        store = StatefulBlockStore("child_response_loss")
        blocks = [
            paragraph_block("첫 문단"),
            *[
                paragraph_block(f"본문 {index}")
                for index in range(166)
            ],
        ]

        result = self.run_body_sync(
            store,
            generation_id="response-loss-generation",
            blocks=blocks,
        )

        quotes = [
            block
            for block in store.root_blocks
            if block.get("type") == "quote"
        ]
        self.assertEqual(result, "response-loss-generation")
        self.assertTrue(store.child_response_lost)
        self.assertEqual(len(quotes), 1)
        self.assertEqual(len(store.children[quotes[0]["id"]]), 166)
        self.assertEqual(
            [
                sync.rich_text_plain_text(
                    block["paragraph"]["rich_text"]
                )
                for block in store.children[quotes[0]["id"]]
            ],
            [f"본문 {index}" for index in range(166)],
        )

    def test_pending_child_prefix_resumes_without_duplicate(self):
        generation_id = "pending-prefix-generation"
        expected_children = [
            paragraph_block(f"본문 {index}")
            for index in range(166)
        ]
        visible = paragraph_block("첫 문단")
        candidate_id = "partial-generation"
        store = StatefulBlockStore()
        store.root_blocks = [
            {
                "id": candidate_id,
                "type": "quote",
                "quote": {
                    "rich_text": copy.deepcopy(
                        visible["paragraph"]["rich_text"]
                    )
                },
                "has_children": True,
            }
        ]
        store.children = {
            candidate_id: copy.deepcopy(expected_children[:1])
        }
        for index, child in enumerate(
            store.children[candidate_id],
            start=1,
        ):
            child["id"] = f"{candidate_id}-child-{index}"
        prefix_hash = sync.sync_container_content_hash(
            "token",
            visible["paragraph"]["rich_text"],
            expected_children[:1],
            False,
        )
        store.properties = {
            sync.SYNC_GENERATION_PROPERTY: (
                sync.body_generation_property_payload(
                    {
                        "v": 2,
                        "g": generation_id,
                        "s": "pending",
                        "op": generation_id,
                        "t": 1,
                        "p": [
                            {
                                "i": candidate_id,
                                "n": 1,
                                "h": prefix_hash,
                            }
                        ],
                        "o": [],
                    }
                )
            )
        }

        result = self.run_body_sync(
            store,
            generation_id=generation_id,
            blocks=[visible, *expected_children],
        )

        self.assertEqual(result, generation_id)
        self.assertEqual(store.root_append_count, 0)
        self.assertEqual(
            len(store.children[candidate_id]),
            166,
        )
        self.assertEqual(
            store.child_batch_sizes,
            [49, 50, 50, 16],
        )
        self.assertEqual(
            [
                sync.rich_text_plain_text(
                    block["paragraph"]["rich_text"]
                )
                for block in store.children[candidate_id]
            ],
            [f"본문 {index}" for index in range(166)],
        )

    def test_managed_pending_without_manifest_reuses_exact_legacy_quote(self):
        visible = paragraph_block("레거시 본문")
        child = {
            "type": "heading_2",
            "heading_2": {
                "rich_text": [
                    {"type": "text", "text": {"content": "하위 본문"}}
                ]
            },
        }
        store = StatefulBlockStore()
        store.root_blocks = [
            {
                "id": store.old_id,
                "type": "quote",
                "quote": {
                    "rich_text": copy.deepcopy(
                        visible["paragraph"]["rich_text"]
                    )
                },
                "has_children": True,
            }
        ]
        store.children = {store.old_id: [copy.deepcopy(child)]}
        store.properties = {
            sync.SYNC_OWNER_PROPERTY: rich_text_property(
                sync.SYNC_OWNER_VALUE
            ),
            sync.SOURCE_KEY_PROPERTY: rich_text_property("141"),
            sync.NOTICE_ID_PROPERTY: rich_text_property("1001"),
            sync.SYNC_STATUS_PROPERTY: rich_text_property("pending"),
            sync.SYNC_OPERATION_PROPERTY: rich_text_property(
                "crash-seam-generation"
            ),
        }

        result = self.run_body_sync(
            store,
            generation_id="crash-seam-generation",
            blocks=[visible, child],
            allow_untracked_recovery=True,
        )

        manifest = sync.extract_body_generation_manifest(
            notion_read_properties(store.properties)
        )
        self.assertEqual(result, "crash-seam-generation")
        self.assertEqual(store.root_append_count, 0)
        self.assertEqual(store.deleted_ids, [])
        self.assertEqual(store.root_ids(), [store.old_id])
        self.assertEqual(manifest["s"], "committed")
        self.assertEqual(manifest["p"][0]["i"], store.old_id)

    def test_pending_without_manifest_rejects_partial_quote_before_write(self):
        visible = paragraph_block("레거시 본문")
        first_child = paragraph_block("첫 부분")
        second_child = paragraph_block("둘째 부분")
        store = StatefulBlockStore()
        store.root_blocks = [
            {
                "id": store.old_id,
                "type": "quote",
                "quote": {
                    "rich_text": copy.deepcopy(
                        visible["paragraph"]["rich_text"]
                    )
                },
                "has_children": True,
            }
        ]
        store.children = {
            store.old_id: [copy.deepcopy(first_child)]
        }
        store.properties = {
            sync.SYNC_OWNER_PROPERTY: rich_text_property(
                sync.SYNC_OWNER_VALUE
            ),
            sync.SOURCE_KEY_PROPERTY: rich_text_property("141"),
            sync.NOTICE_ID_PROPERTY: rich_text_property("1001"),
            sync.SYNC_STATUS_PROPERTY: rich_text_property("pending"),
            sync.SYNC_OPERATION_PROPERTY: rich_text_property(
                "crash-seam-generation"
            ),
        }

        with self.assertRaisesRegex(
            RuntimeError,
            "정확히 일치하지 않습니다",
        ):
            self.run_body_sync(
                store,
                generation_id="crash-seam-generation",
                blocks=[visible, first_child, second_child],
                allow_untracked_recovery=True,
            )

        self.assertEqual(store.root_append_count, 0)
        self.assertNotIn(
            sync.SYNC_GENERATION_PROPERTY,
            store.properties,
        )
        self.assertFalse(
            any(event[0] == "update" for event in store.events)
        )





    def test_body_generation_nonce_retries_and_rotates(self):
        committed = {
            "v": 2,
            "g": "committed-generation",
            "s": "committed",
            "op": "old-operation",
            "t": 1,
            "p": [
                {
                    "i": "old-block",
                    "n": 1,
                    "h": "a" * 64,
                }
            ],
            "o": [],
        }
        first = sync_engine.next_body_generation_id(
            "same-operation",
            "b" * 64,
            committed,
            "b" * 64,
        )
        pending = {
            **committed,
            "g": first,
            "s": "pending",
            "op": "same-operation",
        }
        retry = sync_engine.next_body_generation_id(
            "same-operation",
            "b" * 64,
            pending,
            "b" * 64,
        )
        next_drift = sync_engine.next_body_generation_id(
            "same-operation",
            "b" * 64,
            {
                **committed,
                "g": first,
                "op": "same-operation",
            },
            "b" * 64,
        )

        self.assertEqual(retry, first)
        self.assertNotEqual(first, "b" * 64)
        self.assertNotEqual(next_drift, first)



    def test_invalid_same_generation_is_preserved_when_replacement_append_fails(self):
        store = StatefulBlockStore("append_failure")
        store.properties[sync.SYNC_GENERATION_PROPERTY] = {
            "rich_text": [
                {
                    "type": "text",
                    "text": {"content": "new-generation"},
                }
            ]
        }
        store.root_blocks = [
            block
            for block in store.root_blocks
            if block.get("id") != store.old_id
        ] + [managed_container(store.old_id, "new-generation")]
        store.children[store.old_id] = [
            {
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {"content": "corrupt"},
                        }
                    ]
                },
            }
        ]

        with self.assertRaisesRegex(RuntimeError, "append failed"):
            self.run_body_sync(store)

        self.assertIn(store.old_id, store.root_ids())
        self.assertNotIn(store.old_id, store.deleted_ids)
        self.assertIn(store.manual_id, store.root_ids())

    def test_unsealed_same_generation_is_replaced_after_verified_append(self):
        store = StatefulBlockStore()
        store.properties[sync.SYNC_GENERATION_PROPERTY] = {
            "rich_text": [
                {
                    "type": "text",
                    "text": {"content": "new-generation"},
                }
            ]
        }
        store.root_blocks = [
            block
            for block in store.root_blocks
            if block.get("id") != store.old_id
        ] + [managed_container(store.old_id, "new-generation")]
        store.children[store.old_id] = [
            {
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {"content": "new body"},
                        }
                    ]
                },
            }
        ]

        result = self.run_body_sync(store)

        self.assertEqual(result, "new-generation")
        self.assertIn(store.old_id, store.deleted_ids)
        self.assertNotIn(store.old_id, store.root_ids())
        self.assertIn("new-managed-1", store.root_ids())

    def test_manifest_authenticated_legacy_marker_remains_managed(self):
        legacy = {
            "id": "legacy",
            "type": "quote",
            "quote": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": (
                                f"{sync.LEGACY_SYNC_CONTAINER_MARKER}\n"
                                "사용자 본문"
                            )
                        },
                    }
                ]
            },
        }
        with (
            patch.object(
                sync,
                "load_body_generation_manifest",
                return_value={
                    "v": 1,
                    "g": "legacy",
                    "s": "legacy",
                    "op": "",
                    "t": 0,
                    "p": [],
                    "o": [],
                },
            ),
            patch.object(
                sync,
                "list_block_children",
                return_value=[legacy],
            ),
        ):
            self.assertEqual(
                sync.list_sync_container_blocks("token", "page"),
                [legacy],
            )

    def test_same_body_generation_rerun_converges_without_duplicate(self):
        store = StatefulBlockStore()

        first = self.run_body_sync(store)
        append_count_after_first = store.append_count
        second = self.run_body_sync(store)

        self.assertEqual(first, "new-generation")
        self.assertEqual(second, "new-generation")
        self.assertEqual(store.append_count, append_count_after_first)
        generation_blocks = [
            block
            for block in store.root_blocks
            if block.get("type") == "quote"
            and block.get("id") != store.old_id
        ]
        self.assertEqual(len(generation_blocks), 1)
        self.assertNotIn(
            sync.SYNC_CONTAINER_MARKER,
            sync.rich_text_plain_text(
                generation_blocks[0]["quote"]["rich_text"]
            ),
        )
        manifest = sync.extract_body_generation_manifest(
            notion_read_properties(store.properties)
        )
        self.assertEqual(manifest["g"], "new-generation")
        self.assertEqual(manifest["s"], "committed")
        self.assertIn(store.manual_id, store.root_ids())

    def test_body_generation_accepts_notion_equivalent_link_encoding(self):
        source_url = (
            "https://www.sogang.ac.kr/ko/detail/547784"
            "?bbsConfigFk=2&text=%ED%95%99%EC%82%AC+%EC%A7%80%EC%9B%90"
            "&redirect=/ko/academic-support/notices?page=1%26option=TITLE"
        )
        notion_url = (
            "https://www.sogang.ac.kr/ko/detail/547784"
            "?bbsConfigFk=2&text=%ED%95%99%EC%82%AC%20%EC%A7%80%EC%9B%90"
            "&redirect=%2Fko%2Facademic-support%2Fnotices"
            "%3Fpage%3D1%26option%3DTITLE"
        )
        visible = paragraph_block("첫 문단")
        linked = paragraph_block("관련 안내")
        linked["paragraph"]["rich_text"][0]["text"]["link"] = {
            "url": source_url
        }
        store = StatefulBlockStore()
        append_children = store.append_children

        def append_with_notion_link_encoding(
            token: str,
            parent_id: str,
            blocks: list[dict],
        ) -> dict:
            response = append_children(token, parent_id, blocks)
            if parent_id == store.root_id:
                candidate_id = str(response["results"][0]["id"])
                store.children[candidate_id][0]["paragraph"]["rich_text"][0][
                    "text"
                ]["link"] = {"url": notion_url}
            return response

        store.append_children = append_with_notion_link_encoding

        result = self.run_body_sync(
            store,
            generation_id="notion-link-normalization",
            blocks=[visible, linked],
        )

        manifest = sync.extract_body_generation_manifest(
            notion_read_properties(store.properties)
        )
        candidate_id = str(manifest["p"][0]["i"])
        candidate = next(
            block
            for block in store.root_blocks
            if block.get("id") == candidate_id
        )
        with patch.object(
            sync,
            "list_block_children",
            side_effect=store.list_children,
        ):
            actual_hash = sync.sync_container_actual_hash(
                "token",
                candidate,
            )

        self.assertEqual(result, "notion-link-normalization")
        self.assertEqual(manifest["s"], "committed")
        self.assertEqual(manifest["p"][0]["h"], actual_hash)
        self.assertNotIn(store.old_id, store.root_ids())

    def test_body_generation_accepts_notion_root_link_slash(self):
        source_url = "https://www.kosaf.go.kr"
        notion_url = "https://www.kosaf.go.kr/"
        visible = paragraph_block("첫 문단")
        linked = paragraph_block("관련 안내")
        linked["paragraph"]["rich_text"][0]["text"]["link"] = {
            "url": source_url
        }
        store = StatefulBlockStore()
        append_children = store.append_children

        def append_with_notion_root_slash(
            token: str,
            parent_id: str,
            blocks: list[dict],
        ) -> dict:
            response = append_children(token, parent_id, blocks)
            if parent_id == store.root_id:
                candidate_id = str(response["results"][0]["id"])
                store.children[candidate_id][0]["paragraph"]["rich_text"][0][
                    "text"
                ]["link"] = {"url": notion_url}
            return response

        store.append_children = append_with_notion_root_slash

        result = self.run_body_sync(
            store,
            generation_id="notion-root-link-slash",
            blocks=[visible, linked],
        )

        manifest = sync.extract_body_generation_manifest(
            notion_read_properties(store.properties)
        )
        self.assertEqual(result, "notion-root-link-slash")
        self.assertEqual(manifest["s"], "committed")
        self.assertEqual(store.root_append_count, 1)
        self.assertNotIn(store.old_id, store.root_ids())

    def test_notion_link_normalization_preserves_non_root_path_slash(self):
        self.assertNotEqual(
            sync.normalize_notion_link_identity(
                "https://www.kosaf.go.kr/path"
            ),
            sync.normalize_notion_link_identity(
                "https://www.kosaf.go.kr/path/"
            ),
        )

    def test_pending_notion_link_failure_recovers_without_duplicate(self):
        source_url = (
            "https://www.sogang.ac.kr/ko/detail/548926"
            "?bbsConfigFk=2&text=%EA%B5%90%EC%9C%A1+%EC%95%88%EB%82%B4"
            "&redirect=/ko/academic-support/notices?page=1%26option=TITLE"
        )
        notion_url = (
            "https://www.sogang.ac.kr/ko/detail/548926"
            "?bbsConfigFk=2&text=%EA%B5%90%EC%9C%A1%20%EC%95%88%EB%82%B4"
            "&redirect=%2Fko%2Facademic-support%2Fnotices"
            "%3Fpage%3D1%26option%3DTITLE"
        )
        visible = paragraph_block("첫 문단")
        linked = paragraph_block("관련 안내")
        linked["paragraph"]["rich_text"][0]["text"]["link"] = {
            "url": source_url
        }
        failed_linked = copy.deepcopy(linked)
        failed_linked["paragraph"]["rich_text"][0]["text"]["link"] = {
            "url": notion_url
        }
        failed_id = "failed-managed"
        store = StatefulBlockStore()
        store.root_blocks = [
            {
                "id": store.manual_id,
                "type": "paragraph",
                "paragraph": {"rich_text": [{"plain_text": "manual"}]},
            },
            {
                "id": failed_id,
                "type": "quote",
                "quote": {
                    "rich_text": copy.deepcopy(
                        visible["paragraph"]["rich_text"]
                    ),
                    "color": "default",
                },
                "has_children": True,
            },
        ]
        store.children = {failed_id: [failed_linked]}
        with patch.object(
            sync,
            "list_block_children",
            side_effect=store.list_children,
        ):
            failed_hash = sync.sync_container_actual_hash(
                "token",
                store.root_blocks[1],
            )
        generation_id = "notion-link-pending-retry"
        store.properties[sync.SYNC_GENERATION_PROPERTY] = (
            sync.body_generation_property_payload(
                {
                    "v": 2,
                    "g": generation_id,
                    "s": "pending",
                    "op": generation_id,
                    "t": 1,
                    "p": [],
                    "o": [{"i": failed_id, "h": failed_hash}],
                }
            )
        )
        append_children = store.append_children

        def append_with_notion_link_encoding(
            token: str,
            parent_id: str,
            blocks: list[dict],
        ) -> dict:
            response = append_children(token, parent_id, blocks)
            if parent_id == store.root_id:
                candidate_id = str(response["results"][0]["id"])
                store.children[candidate_id][0]["paragraph"]["rich_text"][0][
                    "text"
                ]["link"] = {"url": notion_url}
            return response

        store.append_children = append_with_notion_link_encoding

        result = self.run_body_sync(
            store,
            generation_id=generation_id,
            blocks=[visible, linked],
        )

        manifest = sync.extract_body_generation_manifest(
            notion_read_properties(store.properties)
        )
        quote_ids = [
            str(block.get("id") or "")
            for block in store.root_blocks
            if block.get("type") == "quote"
        ]
        candidate_id = str(manifest["p"][0]["i"])

        self.assertEqual(result, generation_id)
        self.assertEqual(manifest["s"], "committed")
        self.assertEqual(quote_ids, [candidate_id])
        self.assertNotEqual(candidate_id, failed_id)
        self.assertIn(failed_id, store.deleted_ids)
        self.assertIn(store.manual_id, store.root_ids())
        self.assertLess(
            store.events.index(("list", candidate_id)),
            store.events.index(("delete", failed_id)),
        )

    def test_body_generation_rejects_different_link_query(self):
        expected_link = "https://www.sogang.ac.kr/ko/detail/1?bbsConfigFk=2"
        changed_link = "https://www.sogang.ac.kr/ko/detail/1?bbsConfigFk=141"
        expected = paragraph_block("관련 안내")
        expected["paragraph"]["rich_text"][0]["text"]["link"] = {
            "url": expected_link
        }
        actual = copy.deepcopy(expected)
        actual["paragraph"]["rich_text"][0]["text"]["link"] = {
            "url": changed_link
        }
        container = {
            "id": "candidate",
            "type": "quote",
            "quote": {
                "rich_text": copy.deepcopy(
                    paragraph_block("첫 문단")["paragraph"]["rich_text"]
                )
            },
            "has_children": True,
        }

        with patch.object(
            sync,
            "list_block_children",
            return_value=[actual],
        ):
            prefix_length = sync.sync_container_prefix_length(
                "token",
                container,
                container["quote"]["rich_text"],
                [expected],
            )

        self.assertIsNone(prefix_length)
        self.assertNotEqual(
            sync.normalize_notion_link_identity(
                "https://example.com/?value=%FF"
            ),
            sync.normalize_notion_link_identity(
                "https://example.com/?value=%FE"
            ),
        )

    def test_generation_manifest_reads_plain_legacy_and_v2_state(self):
        legacy = sync.parse_body_generation_manifest(
            "legacy-generation"
        )
        self.assertEqual(legacy["v"], 1)
        self.assertEqual(legacy["g"], "legacy-generation")
        manifest = {
            "v": 2,
            "g": "current-generation",
            "s": "committed",
            "op": "operation",
            "t": 1,
            "p": [
                {
                    "i": "block",
                    "n": 1,
                    "h": "a" * 64,
                }
            ],
            "o": [],
        }
        serialized = sync.serialize_body_generation_manifest(
            manifest
        )

        self.assertEqual(
            sync.parse_body_generation_manifest(serialized),
            manifest,
        )

    def test_body_generation_current_requires_complete_managed_generation(self):
        content_hash = sync.sync_container_content_hash(
            "token",
            [],
            [],
            False,
        )
        complete = {
            "id": "generation-1",
            "type": "quote",
            "quote": {"rich_text": []},
            "has_children": False,
        }
        manifest = {
            "v": 2,
            "g": "stable-generation",
            "s": "committed",
            "op": "operation",
            "t": 1,
            "p": [
                {"i": "generation-1", "n": 1, "h": content_hash},
            ],
            "o": [],
        }
        with (
            patch.object(
                sync,
                "load_body_generation_manifest",
                return_value=manifest,
            ),
            patch.object(
                sync,
                "body_generation_blocks_from_manifest",
                return_value=[(1, complete)],
            ),
            patch.object(
                sync,
                "list_block_children",
                side_effect=[[complete], []],
            ),
        ):
            self.assertTrue(
                sync.is_body_generation_current(
                    "token",
                    "page",
                    "stable-generation",
                )
            )
        multipart_manifest = {
            **manifest,
            "t": 2,
            "p": [
                {"i": "generation-1", "n": 1, "h": content_hash},
                {"i": "generation-2", "n": 2, "h": content_hash},
            ],
        }
        with patch.object(
            sync,
            "load_body_generation_manifest",
            return_value=multipart_manifest,
        ):
            self.assertFalse(
                sync.is_body_generation_current(
                    "token",
                    "page",
                    "stable-generation",
                )
            )
        with patch.object(
            sync,
            "load_body_generation_manifest",
            return_value=None,
        ):
            self.assertFalse(
                sync.is_body_generation_current(
                    "token",
                    "page",
                    "stable-generation",
                )
            )

    def test_tampered_managed_body_child_is_not_current(self):
        expected_children = [paragraph_block("정상 본문")]
        content_hash = sync.sync_container_content_hash(
            "token",
            [],
            expected_children,
            False,
        )
        container = {
            "id": "generation-1",
            "type": "quote",
            "quote": {"rich_text": []},
            "has_children": True,
        }
        manifest = {
            "v": 2,
            "g": "stable-generation",
            "s": "committed",
            "op": "operation",
            "t": 1,
            "p": [
                {"i": "generation-1", "n": 1, "h": content_hash}
            ],
            "o": [],
        }
        with (
            patch.object(
                sync,
                "load_body_generation_manifest",
                return_value=manifest,
            ),
            patch.object(
                sync,
                "body_generation_blocks_from_manifest",
                return_value=[(1, container)],
            ),
            patch.object(
                sync,
                "list_block_children",
                side_effect=[[container], expected_children],
            ),
        ):
            self.assertTrue(
                sync.is_body_generation_current(
                    "token",
                    "page",
                    "stable-generation",
                )
            )
        with (
            patch.object(
                sync,
                "load_body_generation_manifest",
                return_value=manifest,
            ),
            patch.object(
                sync,
                "body_generation_blocks_from_manifest",
                return_value=[(1, container)],
            ),
            patch.object(
                sync,
                "list_block_children",
                side_effect=[
                    [container],
                    [paragraph_block("변조된 본문")],
                ],
            ),
        ):
            self.assertFalse(
                sync.is_body_generation_current(
                    "token",
                    "page",
                    "stable-generation",
                )
            )

    def test_disable_missing_top_queries_owner_and_source_scope(self):
        selected = managed_page("selected", "141", "old")
        selected_readback = copy.deepcopy(selected)
        selected_readback["properties"][sync.TOP_PROPERTY] = {
            "type": "checkbox",
            "checkbox": False,
        }
        other_source = managed_page("other-source", "2", "old")
        manual = {
            "id": "manual",
            "properties": {
                sync.NOTICE_ID_PROPERTY: rich_text_property("old"),
            },
        }
        with (
            patch.object(
                sync,
                "query_database_page",
                return_value={
                    "results": [selected, other_source, manual],
                    "has_more": False,
                },
            ) as query,
            patch.object(sync, "update_page") as update,
            patch.object(
                sync,
                "retrieve_page",
                side_effect=[selected, selected_readback],
            ),
        ):
            disabled = sync.disable_missing_top(
                "token",
                "database",
                "141",
                set(),
            )

        self.assertEqual(disabled, 1)
        update.assert_called_once_with(
            "token",
            "selected",
            {sync.TOP_PROPERTY: {"checkbox": False}},
        )
        payload = query.call_args.args[2]
        filters = payload["filter"]["and"]
        self.assertEqual(len(filters), 3)
        by_property = {
            entry["property"]: entry for entry in filters
        }
        self.assertEqual(
            by_property[sync.SYNC_OWNER_PROPERTY]["rich_text"]["equals"],
            sync.SYNC_OWNER_VALUE,
        )
        self.assertEqual(
            by_property[sync.SOURCE_KEY_PROPERTY]["rich_text"]["equals"],
            "141",
        )
        self.assertTrue(
            by_property[sync.TOP_PROPERTY]["checkbox"]["equals"]
        )

    def test_disable_missing_top_requires_false_readback(self):
        selected = managed_page("selected", "141", "old")
        with (
            patch.object(
                sync,
                "query_database_page",
                return_value={
                    "results": [selected],
                    "has_more": False,
                },
            ),
            patch.object(sync, "update_page") as update,
            patch.object(
                sync,
                "retrieve_page",
                return_value=selected,
            ),
            patch.object(
                sync,
                "TOP_COMMIT_READBACK_DELAYS",
                (0.0,),
            ),
            self.assertRaisesRegex(
                sync.DestinationConsistencyError,
                "TOP 해제 재조회 검증",
            ),
        ):
            sync.disable_missing_top(
                "token",
                "database",
                "141",
                set(),
            )

        update.assert_called_once()

    def test_pending_page_inspection_is_paginated_and_owner_scoped(self):
        first = managed_page("pending-1", "141", "1")
        second = managed_page("pending-2", "2", "2")
        committed = managed_page("committed", "141", "3")
        manual = {
            "id": "manual",
            "properties": {
                sync.SYNC_STATUS_PROPERTY: rich_text_property("pending")
            },
        }
        for page in (first, second):
            page["properties"][sync.SYNC_STATUS_PROPERTY] = (
                rich_text_property("pending")
            )
        committed["properties"][sync.SYNC_STATUS_PROPERTY] = (
            rich_text_property("committed")
        )
        with (
            patch.object(
                sync,
                "query_database_page",
                side_effect=[
                    {
                        "results": [first, committed, manual],
                        "has_more": True,
                        "next_cursor": "cursor-2",
                    },
                    {
                        "results": [second],
                        "has_more": False,
                    },
                ],
            ) as query,
            patch.object(sync, "check_run_control"),
            patch.object(sync, "update_page") as update,
        ):
            pending_pages = sync.inspect_pending_pages(
                "token",
                "database",
            )

        self.assertEqual(
            [page["id"] for page in pending_pages],
            ["pending-1", "pending-2"],
        )
        self.assertEqual(query.call_count, 2)
        first_payload = query.call_args_list[0].args[2]
        second_payload = query.call_args_list[1].args[2]
        filters = {
            entry["property"]: entry
            for entry in first_payload["filter"]["and"]
        }
        self.assertEqual(
            filters[sync.SYNC_OWNER_PROPERTY]["rich_text"]["equals"],
            sync.SYNC_OWNER_VALUE,
        )
        self.assertEqual(
            filters[sync.SYNC_STATUS_PROPERTY]["rich_text"]["equals"],
            "pending",
        )
        self.assertEqual(second_payload["start_cursor"], "cursor-2")
        update.assert_not_called()

    def test_managed_page_fingerprint_detects_metadata_change_without_timestamp(self):
        page = managed_page("page-1", "141", "1")
        changed = copy.deepcopy(page)
        changed["properties"][sync.SYNC_STATUS_PROPERTY] = (
            rich_text_property("pending")
        )

        self.assertNotEqual(
            sync.managed_page_fingerprint(page),
            sync.managed_page_fingerprint(changed),
        )

    def test_preflight_batch_validation_logs_each_item_progress(self):
        context = sync_engine.DestinationContext("token", "database")
        entries = [
            sync_engine.DestinationPreflight(
                item={"source_id": "141", "notice_id": "1"},
                existing_page=None,
                operation_id="operation-1",
                shrink_key="141:1",
                shrink_candidate=None,
            ),
            sync_engine.DestinationPreflight(
                item={"source_id": "2", "notice_id": "2"},
                existing_page=None,
                operation_id="operation-2",
                shrink_key="2:2",
                shrink_candidate=None,
            ),
        ]

        with (
            patch.object(
                sync_engine,
                "validate_destination_preflight_entry",
            ) as validate,
            self.assertLogs(sync_engine.LOGGER, level="INFO") as logs,
        ):
            sync_engine.validate_destination_preflight_entries(
                context,
                entries,
            )

        self.assertEqual(validate.call_count, 2)
        output = "\n".join(logs.output)
        self.assertIn(
            "목적지 적용 전 일괄검증 진행: "
            "1/2, 출처=141, 공지=1",
            output,
        )
        self.assertIn(
            "목적지 적용 전 일괄검증 진행: "
            "2/2, 출처=2, 공지=2",
            output,
        )
        self.assertIn(
            "목적지 적용 전 일괄검증 완료: 항목=2",
            output,
        )

    def test_untracked_quote_is_detected_before_destination_mutation(self):
        item = {
            "source_id": "2",
            "notice_id": "200",
            "title": "공지 200",
            "url": (
                "https://www.sogang.ac.kr/ko/detail/200"
                "?bbsConfigFk=2"
            ),
            "top": False,
            "body_status": "present",
            "body_blocks": [paragraph_block("새 본문")],
        }
        existing = managed_page("page-200", "2", "200")
        context = sync_engine.DestinationContext(
            "token",
            "database",
        )
        with (
            patch.object(
                sync_engine,
                "find_existing_page",
                return_value=existing,
            ),
            patch.object(
                sync_engine,
                "top_level_quote_state",
                return_value=[
                    {
                        "id": "manual-quote",
                        "managed": False,
                        "content_hash": "a" * 64,
                    }
                ],
            ),
            patch.object(sync_engine, "create_page") as create,
            patch.object(sync_engine, "update_page") as update,
            patch.object(
                sync_engine,
                "sync_page_body_blocks",
            ) as replace_body,
            self.assertRaisesRegex(
                DestinationConsistencyError,
                "관리되지 않는 최상위 인용",
            ),
        ):
            sync_engine.resolve_destination_preflight(
                context,
                [item],
            )

        create.assert_not_called()
        update.assert_not_called()
        replace_body.assert_not_called()

    def test_pending_untracked_quote_requires_exact_body_in_preflight(self):
        body_blocks = [paragraph_block("복구 본문")]
        container_rich_text, body_chunks = (
            sync.split_body_container_parts(body_blocks)
        )
        expected_hash = sync.sync_container_content_hash(
            "token",
            container_rich_text,
            [
                child
                for chunk in body_chunks
                for child in chunk
            ],
            False,
        )
        page = managed_page("page-200", "2", "200")

        with patch.object(
            sync_engine,
            "top_level_quote_state",
            return_value=[
                {
                    "id": "legacy-quote",
                    "managed": False,
                    "content_hash": expected_hash,
                }
            ],
        ):
            fingerprint = (
                sync_engine.destination_quote_fingerprint(
                    "token",
                    page,
                    allow_untracked_recovery=True,
                    expected_body_blocks=body_blocks,
                )
            )

        self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")

        with (
            patch.object(
                sync_engine,
                "top_level_quote_state",
                return_value=[
                    {
                        "id": "legacy-quote",
                        "managed": False,
                        "content_hash": "0" * 64,
                    }
                ],
            ),
            self.assertRaisesRegex(
                DestinationConsistencyError,
                "정확히 일치하지 않습니다",
            ),
        ):
            sync_engine.destination_quote_fingerprint(
                "token",
                page,
                allow_untracked_recovery=True,
                expected_body_blocks=body_blocks,
            )

    def test_untracked_quote_detection_preserves_non_quote_blocks(self):
        page = managed_page("page-200", "2", "200")
        page["properties"][sync.SYNC_GENERATION_PROPERTY] = (
            rich_text_property(
                json.dumps(
                    {
                        "v": 2,
                        "g": "generation",
                        "s": "committed",
                        "op": "operation",
                        "t": 1,
                        "p": [
                            {
                                "i": "managed-quote",
                                "n": 1,
                                "h": "a" * 64,
                            }
                        ],
                    },
                    separators=(",", ":"),
                )
            )
        )
        roots = [
            {"id": "manual", "type": "paragraph", "paragraph": {}},
            {"id": "managed-quote", "type": "quote", "quote": {"rich_text": []}},
            {"id": "manual-quote", "type": "quote", "quote": {"rich_text": []}},
        ]
        with patch.object(
            sync,
            "list_block_children",
            return_value=roots,
        ):
            untracked = sync.untracked_top_level_quote_ids(
                "token",
                page,
            )

        self.assertEqual(untracked, ["manual-quote"])

    def test_pending_page_inspection_validates_all_ids(self):
        valid = managed_page("pending-1", "141", "1")
        missing = managed_page("", "141", "2")
        for page in (valid, missing):
            page["properties"][sync.SYNC_STATUS_PROPERTY] = (
                rich_text_property("pending")
            )
        with (
            patch.object(
                sync,
                "query_database_page",
                return_value={
                    "results": [valid, missing],
                    "has_more": False,
                },
            ),
            patch.object(sync, "check_run_control"),
            patch.object(sync, "update_page") as update,
            self.assertRaisesRegex(RuntimeError, "ID가 누락"),
        ):
            sync.inspect_pending_pages("token", "database")

        update.assert_not_called()

    def test_notion_page_queries_reject_repeated_cursor(self):
        responses = [
            {
                "results": [],
                "has_more": True,
                "next_cursor": "same",
            },
            {
                "results": [],
                "has_more": True,
                "next_cursor": "same",
            },
        ]
        for query in (
            lambda: list(
                sync.iter_top_pages(
                    "token",
                    "database",
                    "141",
                )
            ),
            lambda: sync.inspect_pending_pages(
                "token",
                "database",
            ),
        ):
            with self.subTest(query=query):
                with (
                    patch.object(
                        sync,
                        "query_database_page",
                        side_effect=copy.deepcopy(responses),
                    ),
                    patch.object(sync, "check_run_control"),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "커서가 누락되거나 반복",
                    ),
                ):
                    query()

    def test_schema_only_destination_setup_skips_pending_recovery(self):
        def keep_database(_token, _database_id, database):
            return database

        with (
            patch.object(
                sync_engine,
                "fetch_database",
                return_value={
                    "properties": complete_destination_schema()
                },
            ) as fetch,
            patch.object(
                sync_engine,
                "ensure_destination_schema",
                side_effect=keep_database,
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
            ) as recover,
        ):
            context = sync_engine.prepare_destination(
                "token",
                "database",
                [],
                recover_pending=False,
            )

        recover.assert_not_called()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(context.pending_page_ids, ())

    def test_pending_context_inspection_is_read_only(self):
        database = {"properties": complete_destination_schema()}
        pending = managed_page("pending-page", "2", "548926")

        with (
            patch.object(
                sync_engine,
                "fetch_database",
                return_value=database,
            ),
            patch.object(
                sync_engine,
                "validate_destination_schema",
            ) as validate,
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[pending],
            ),
            patch.object(
                sync_engine,
                "ensure_destination_schema",
            ) as ensure,
        ):
            context = sync_engine.inspect_destination_pending_context(
                "token",
                "database",
            )

        validate.assert_called_once_with(database)
        ensure.assert_not_called()
        self.assertEqual(
            context.pending_page_ids,
            ("pending-page",),
        )
        self.assertEqual(
            context.pending_page_sources,
            {"pending-page": "2"},
        )
        self.assertEqual(
            context.pending_page_notices,
            {"pending-page": "548926"},
        )

    def test_disable_missing_top_threshold_failure_writes_nothing(self):
        pages = [
            managed_page(f"page-{index}", "141", f"notice-{index}")
            for index in range(6)
        ]
        with (
            patch.object(sync, "iter_top_pages", return_value=iter(pages)),
            patch.object(sync, "get_top_disable_max_count", return_value=2),
            patch.object(sync, "get_top_disable_max_ratio", return_value=0.4),
            patch.object(sync, "update_page") as update,
            self.assertRaisesRegex(RuntimeError, "안전 한도 초과"),
        ):
            sync.disable_missing_top(
                "token",
                "database",
                "141",
                set(),
            )

        update.assert_not_called()

    def test_top_disable_revalidates_all_candidates_before_first_patch(self):
        first = managed_page("page-1", "141", "1")
        second = managed_page("page-2", "141", "2")
        with (
            patch.object(
                sync,
                "query_database_page",
                return_value={
                    "results": [first],
                    "has_more": False,
                },
            ),
            patch.object(sync, "update_page") as update,
            self.assertRaisesRegex(RuntimeError, "적용 직전 대상"),
        ):
            sync.disable_missing_top(
                "token",
                "database",
                "141",
                set(),
                eligible_notice_ids={"1", "2"},
                planned_candidates=[first, second],
                total_top_count=2,
            )

        update.assert_not_called()

    def test_top_candidate_is_reread_immediately_before_each_patch(self):
        first = managed_page("page-1", "141", "1")
        first_readback = copy.deepcopy(first)
        first_readback["properties"][sync.TOP_PROPERTY] = {
            "type": "checkbox",
            "checkbox": False,
        }
        second = managed_page("page-2", "141", "2")
        changed_second = copy.deepcopy(second)
        changed_second["last_edited_time"] = "2026-07-27T00:01:00Z"
        with (
            patch.object(
                sync,
                "query_database_page",
                return_value={
                    "results": [first, second],
                    "has_more": False,
                },
            ),
            patch.object(
                sync,
                "retrieve_page",
                side_effect=[first, first_readback, changed_second],
            ),
            patch.object(sync, "update_page") as update,
            self.assertRaisesRegex(
                sync.DestinationConsistencyError,
                "적용 직전에 변경",
            ),
        ):
            sync.disable_missing_top(
                "token",
                "database",
                "141",
                set(),
                eligible_notice_ids={"1", "2"},
                planned_candidates=[first, second],
                total_top_count=2,
            )

        update.assert_called_once_with(
            "token",
            "page-1",
            {sync.TOP_PROPERTY: {"checkbox": False}},
        )

    def test_find_existing_page_duplicate_raises_without_archive(self):
        with (
            patch.object(
                sync,
                "query_existing_pages_with_stage_log",
                return_value=[{"id": "first"}, {"id": "second"}],
            ),
            self.assertRaisesRegex(RuntimeError, "식별자 충돌"),
        ):
            sync.find_existing_page(
                "token",
                "database",
                "https://www.sogang.ac.kr/ko/detail/1",
                "공지",
                "2026-07-27",
                source_id="141",
                notice_id="1",
            )


    def test_unmanaged_exact_url_blocks_regular_creation(self):
        with (
            patch.object(
                sync,
                "query_existing_pages_with_stage_log",
                side_effect=[[], [{"id": "legacy", "properties": {}}]],
            ) as query,
            self.assertRaisesRegex(RuntimeError, "비관리 페이지"),
        ):
            sync.find_existing_page(
                "token",
                "database",
                "https://www.sogang.ac.kr/ko/detail/1",
                "공지",
                "2026-07-27",
                source_id="141",
                notice_id="1",
            )
        self.assertEqual(query.call_count, 2)

    def test_missing_url_does_not_fall_back_to_title_or_date(self):
        with patch.object(
            sync,
            "query_existing_pages_with_stage_log",
            return_value=[],
        ) as query:
            result = sync.find_existing_page(
                "token",
                "database",
                None,
                "공지",
                "2026-07-27",
                source_id="141",
                notice_id="1",
            )

        self.assertIsNone(result)
        self.assertEqual(query.call_count, 1)

    def test_build_dry_run_plan_calls_no_write_function(self):
        item = {
            "notice_id": "200",
            "title": "[학사] 테스트",
            "url": "https://www.sogang.ac.kr/ko/detail/200?bbsConfigFk=2",
            "classification": "학사공지",
            "top": True,
            "body_blocks": [],
        }
        report = CrawlReport(
            sources=[source_result("2", SourceStatus.SUCCESS, [item])]
        )
        schema = complete_destination_schema()
        with (
            patch.object(
                sync_engine,
                "fetch_database",
                return_value={"properties": schema},
            ),
            patch.object(
                sync_engine,
                "find_existing_page",
                return_value=None,
            ),
            patch.object(
                sync_engine,
                "inspect_missing_top",
                return_value=([], []),
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
            patch.object(sync_engine, "create_page") as create,
            patch.object(sync_engine, "update_page") as update,
            patch.object(sync_engine, "sync_page_body_blocks") as replace_body,
            patch.object(sync_engine, "disable_missing_top") as disable_top,
            patch.object(sync, "delete_block") as delete_block,
        ):
            plan = sync_engine.build_dry_run_plan(
                "run-id",
                report,
                token="token",
                database_id="database",
                full_reconcile=True,
            )

        self.assertEqual(plan.write_count, 1)
        self.assertEqual(plan.actions[0].kind, MutationKind.CREATE)
        create.assert_not_called()
        update.assert_not_called()
        replace_body.assert_not_called()
        disable_top.assert_not_called()
        delete_block.assert_not_called()

    def test_dry_run_marks_quarantine_and_preserves_other_source_action(self):
        quarantined_result = source_result(
            "2",
            SourceStatus.SUCCESS,
            [
                {
                    "notice_id": "2",
                    "title": "공지 2",
                    "url": "https://www.sogang.ac.kr/ko/detail/2",
                    "top": False,
                    "body_blocks": [],
                }
            ],
        )
        healthy_result = source_result(
            "141",
            SourceStatus.SUCCESS,
            [
                {
                    "notice_id": "141",
                    "title": "공지 141",
                    "url": "https://www.sogang.ac.kr/ko/detail/141",
                    "top": False,
                    "body_blocks": [],
                }
            ],
        )
        report = CrawlReport(
            sources=[quarantined_result, healthy_result]
        )
        prepared = [
            *sync_engine.prepare_source_items(quarantined_result),
            *sync_engine.prepare_source_items(healthy_result),
        ]
        preflight = [
            sync_engine.DestinationPreflight(
                item=prepared[0],
                existing_page=None,
                operation_id="operation-2",
                shrink_key="2:2",
                shrink_candidate=None,
            ),
            sync_engine.DestinationPreflight(
                item=prepared[1],
                existing_page=None,
                operation_id="operation-141",
                shrink_key="141:141",
                shrink_candidate=None,
            ),
        ]
        pending = managed_page("pending-1", "2", "1")

        with (
            patch.object(
                sync_engine,
                "fetch_database",
                return_value={
                    "properties": complete_destination_schema()
                },
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[pending],
            ),
            patch.object(
                sync_engine,
                "resolve_destination_preflight",
                return_value=preflight,
            ),
        ):
            plan = sync_engine.build_dry_run_plan(
                "run-quarantine",
                report,
                token="token",
                database_id="database",
            )

        self.assertEqual(plan.quarantined_source_ids, ["2"])
        self.assertTrue(
            any(
                action.kind == MutationKind.CREATE
                and action.source_id == "141"
                for action in plan.actions
            )
        )
        self.assertFalse(
            any(
                action.kind == MutationKind.CREATE
                and action.source_id == "2"
                for action in plan.actions
            )
        )
        self.assertTrue(
            any(
                action.kind == MutationKind.CONFLICT
                and action.source_id == "2"
                and action.reason
                in {
                    "source_pending_quarantine",
                    "pending_page_outside_current_scope",
                }
                for action in plan.actions
            )
        )

    def test_dry_run_isolates_media_deferred_notice(self):
        result = source_result(
            "2",
            SourceStatus.SUCCESS,
            [
                {
                    "notice_id": "200",
                    "title": "공지 200",
                    "url": "https://www.sogang.ac.kr/ko/detail/200",
                    "top": False,
                    "body_blocks": [],
                },
                {
                    "notice_id": "201",
                    "title": "공지 201",
                    "url": "https://www.sogang.ac.kr/ko/detail/201",
                    "top": False,
                    "body_blocks": [],
                },
            ],
        )
        report = CrawlReport(sources=[result])
        prepared = sync_engine.prepare_source_items(result)
        preflight = [
            sync_engine.DestinationPreflight(
                item=prepared[0],
                existing_page=managed_page("page-200", "2", "200"),
                operation_id="operation-200",
                shrink_key="2:200",
                shrink_candidate=None,
                deferred_reason="body_media_content_unavailable",
            ),
            sync_engine.DestinationPreflight(
                item=prepared[1],
                existing_page=None,
                operation_id="operation-201",
                shrink_key="2:201",
                shrink_candidate=None,
            ),
        ]

        with (
            patch.object(
                sync_engine,
                "fetch_database",
                return_value={
                    "properties": complete_destination_schema()
                },
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
            patch.object(
                sync_engine,
                "resolve_destination_preflight",
                return_value=preflight,
            ),
        ):
            plan = sync_engine.build_dry_run_plan(
                "run-media-isolated",
                report,
                token="token",
                database_id="database",
            )

        self.assertEqual(plan.write_count, 1)
        self.assertTrue(
            any(
                action.kind == MutationKind.CONFLICT
                and action.notice_id == "200"
                and action.reason == "body_media_content_unavailable"
                for action in plan.actions
            )
        )
        self.assertTrue(
            any(
                action.kind == MutationKind.CREATE
                and action.notice_id == "201"
                for action in plan.actions
            )
        )

    def test_dry_run_emits_noop_when_body_and_properties_are_unchanged(self):
        body_blocks = [paragraph_block("동일 본문")]
        item = {
            "notice_id": "200",
            "title": "[학사] 테스트",
            "url": "https://www.sogang.ac.kr/ko/detail/200?bbsConfigFk=2",
            "classification": "학사공지",
            "top": False,
            "body_blocks": body_blocks,
        }
        report = CrawlReport(
            sources=[source_result("2", SourceStatus.SUCCESS, [item])]
        )
        body_hash = sync_engine.compute_body_hash(
            sync_engine.normalize_body_blocks_for_hash(
                body_blocks,
                sync_engine.should_upload_files_to_notion(),
            ),
        )
        existing = {
            "id": "page-200",
            "properties": {
                sync_engine.BODY_HASH_PROPERTY: rich_text_property(body_hash),
                sync_engine.ATTACHMENT_STATE_PROPERTY: (
                    rich_text_property("[]")
                ),
            },
        }
        schema = complete_destination_schema()
        with (
            patch.object(
                sync_engine,
                "fetch_database",
                return_value={"properties": schema},
            ),
            patch.object(
                sync_engine,
                "find_existing_page",
                return_value=existing,
            ),
            patch.object(
                sync_engine,
                "build_properties",
                return_value={},
            ),
            patch.object(
                sync_engine,
                "is_body_generation_current",
                return_value=True,
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
            patch.object(
                sync_engine,
                "destination_quote_fingerprint",
                return_value="quote-state",
            ),
        ):
            plan = sync_engine.build_dry_run_plan(
                "run-id",
                report,
                token="token",
                database_id="database",
            )

        self.assertEqual(len(plan.actions), 1)
        self.assertEqual(plan.actions[0].kind, MutationKind.NOOP)
        self.assertEqual(plan.actions[0].reason, "unchanged")

    def test_dry_run_forces_body_replacement_on_hosted_media_drift(self):
        body_url = (
            "https://www.sogang.ac.kr/file-fe-prd/board/body.jpg"
            "?fileId=88&signature=current"
        )
        body_bytes = jpeg_payload()
        content_sha256 = utils.compute_content_sha256(body_bytes)
        body_blocks = [
            {
                "type": "image",
                "image": {
                    "type": "external",
                    "external": {"url": body_url},
                },
            }
        ]
        content_state = [
            {
                "type": "image",
                "source_url": body_url,
                "content_sha256": content_sha256,
            }
        ]
        media_state = [
            {
                **content_state[0],
                "upload_id": "old-upload",
                "block_id": "old-block",
                "hosted_file_key": "notionusercontent.com/original",
            }
        ]
        body_hash = sync_engine.compute_body_hash(
            sync_engine.normalize_body_blocks_for_hash(
                body_blocks,
                True,
                media_content_state=content_state,
            ),
            image_mode=sync_engine.BODY_HASH_IMAGE_MODE_UPLOAD,
        )
        item = {
            "notice_id": "200",
            "title": "[학사] 미디어 드리프트",
            "url": "https://www.sogang.ac.kr/ko/detail/200?bbsConfigFk=2",
            "classification": "학사공지",
            "top": False,
            "body_status": "present",
            "body_blocks": body_blocks,
        }
        report = CrawlReport(
            sources=[source_result("2", SourceStatus.SUCCESS, [item])]
        )
        existing = managed_page("page-200", "2", "200")
        existing["properties"][sync_engine.BODY_HASH_PROPERTY] = (
            rich_text_property(body_hash)
        )
        existing["properties"][sync_engine.BODY_MEDIA_STATE_PROPERTY] = (
            rich_text_property(
                json.dumps(media_state, separators=(",", ":"))
            )
        )
        existing["properties"][sync_engine.ATTACHMENT_STATE_PROPERTY] = (
            rich_text_property("[]")
        )
        schema = complete_destination_schema()
        with (
            patch.object(
                sync_engine,
                "fetch_database",
                return_value={"properties": schema},
            ),
            patch.object(
                sync_engine,
                "find_existing_page",
                return_value=existing,
            ),
            patch.object(
                notion_client,
                "download_file_bytes",
                return_value=(body_bytes, "image/jpeg"),
            ),
            patch.object(
                sync_engine,
                "inspect_existing_uploaded_media_blocks",
                return_value=({}, "drift"),
            ),
            patch.object(
                sync_engine,
                "build_properties",
                return_value={},
            ),
            patch.object(
                sync_engine,
                "is_body_generation_current",
                return_value=True,
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
            patch.object(
                sync_engine,
                "destination_quote_fingerprint",
                return_value="quote-state",
            ),
            patch.object(sync_engine, "create_page") as create,
            patch.object(sync_engine, "update_page") as update,
            patch.object(sync_engine, "sync_page_body_blocks") as replace,
        ):
            plan = sync_engine.build_dry_run_plan(
                "run-id",
                report,
                token="token",
                database_id="database",
            )

        self.assertEqual(len(plan.actions), 1)
        self.assertEqual(
            plan.actions[0].kind,
            MutationKind.REPLACE_BODY,
        )
        create.assert_not_called()
        update.assert_not_called()
        replace.assert_not_called()

    def test_apply_forces_body_replacement_on_hosted_media_drift(self):
        body_url = (
            "https://www.sogang.ac.kr/file-fe-prd/board/body.jpg"
            "?fileId=88&signature=current"
        )
        body_bytes = jpeg_payload()
        content_sha256 = utils.compute_content_sha256(body_bytes)
        body_blocks = [
            {
                "type": "image",
                "image": {
                    "type": "external",
                    "external": {"url": body_url},
                },
            }
        ]
        content_state = [
            {
                "type": "image",
                "source_url": body_url,
                "content_sha256": content_sha256,
            }
        ]
        media_state = [
            {
                **content_state[0],
                "upload_id": "old-upload",
                "block_id": "old-block",
                "hosted_file_key": "notionusercontent.com/original",
            }
        ]
        body_hash = sync_engine.compute_body_hash(
            sync_engine.normalize_body_blocks_for_hash(
                body_blocks,
                True,
                media_content_state=content_state,
            ),
            image_mode=sync_engine.BODY_HASH_IMAGE_MODE_UPLOAD,
        )
        item = {
            "source_id": "2",
            "notice_id": "200",
            "title": "[학사] 미디어 드리프트",
            "url": "https://www.sogang.ac.kr/ko/detail/200?bbsConfigFk=2",
            "classification": "학사공지",
            "top": False,
            "body_status": "present",
            "body_blocks": body_blocks,
        }
        existing = managed_page("page-200", "2", "200")
        existing["properties"][sync_engine.BODY_HASH_PROPERTY] = (
            rich_text_property(body_hash)
        )
        existing["properties"][sync_engine.BODY_MEDIA_STATE_PROPERTY] = (
            rich_text_property(
                json.dumps(media_state, separators=(",", ":"))
            )
        )
        context = sync_engine.DestinationContext(
            "token",
            "database",
            has_views_property=False,
            has_attachments_property=False,
            has_classification_property=True,
        )
        counters = SyncCounters()
        with (
            patch.object(sync_engine, "check_run_control"),
            patch.object(
                sync_engine,
                "inspect_existing_uploaded_media_blocks",
                return_value=({}, "drift"),
            ),
            patch.object(
                notion_client,
                "download_file_bytes",
                return_value=(body_bytes, "image/jpeg"),
            ),
            patch.object(
                notion_client,
                "upload_external_file_to_notion",
                return_value="new-upload",
            ),
            patch.object(
                sync_engine,
                "build_properties",
                return_value={},
            ),
            patch.object(
                sync_engine,
                "is_body_generation_current",
                return_value=True,
            ) as current,
            patch.object(
                sync_engine,
                "sync_page_body_blocks",
                side_effect=successful_body_sync,
            ) as replace,
            patch.object(
                sync_engine,
                "enrich_body_media_state_with_block_ids",
                side_effect=lambda _token, _page_id, state, _generation: state,
            ),
            patch.object(sync_engine, "verify_committed_item"),
            patch.object(sync_engine, "update_page"),
        ):
            sync_engine.apply_item(
                context,
                item,
                counters,
                existing_page=existing,
                existing_page_resolved=True,
            )

        current.assert_not_called()
        replace.assert_called_once()
        self.assertNotEqual(
            replace.call_args.kwargs["generation_id"],
            body_hash,
        )
        self.assertEqual(counters.body_updates, 1)

    def test_unavailable_body_media_validation_fails_before_write(self):
        item = {
            "source_id": "2",
            "notice_id": "200",
            "title": "[학사] 검증 불가",
            "url": "https://www.sogang.ac.kr/ko/detail/200?bbsConfigFk=2",
            "classification": "학사공지",
            "top": False,
            "body_status": "present",
            "body_blocks": [
                {
                    "type": "image",
                    "image": {
                        "type": "external",
                        "external": {
                            "url": (
                                "https://www.sogang.ac.kr/"
                                "file-fe-prd/board/body.jpg"
                            )
                        },
                    },
                }
            ],
        }
        existing = managed_page("page-200", "2", "200")
        existing["properties"][sync_engine.BODY_MEDIA_STATE_PROPERTY] = (
            rich_text_property(
                json.dumps(
                    [
                        {
                            "type": "image",
                            "source_url": item["body_blocks"][0][
                                "image"
                            ]["external"]["url"],
                            "upload_id": "old-upload",
                            "block_id": "old-block",
                            "hosted_file_key": (
                                "notionusercontent.com/original"
                            ),
                            "content_sha256": (
                                utils.compute_content_sha256(b"image")
                            ),
                        }
                    ],
                    separators=(",", ":"),
                )
            )
        )
        context = sync_engine.DestinationContext(
            "token",
            "database",
            has_views_property=False,
            has_attachments_property=False,
            has_classification_property=True,
        )
        with (
            patch.object(
                sync_engine,
                "inspect_existing_uploaded_media_blocks",
                return_value=({}, "unavailable"),
            ),
            patch.object(sync_engine, "create_page") as create,
            patch.object(sync_engine, "update_page") as update,
            patch.object(sync_engine, "sync_page_body_blocks") as replace,
            self.assertRaisesRegex(RuntimeError, "검증할 수 없습니다"),
        ):
            sync_engine.apply_item(
                context,
                item,
                SyncCounters(),
                existing_page=existing,
                existing_page_resolved=True,
            )

        create.assert_not_called()
        update.assert_not_called()
        replace.assert_not_called()

    def test_preflight_defers_existing_upload_when_source_media_is_unavailable(
        self,
    ):
        body_url = (
            "https://www.sogang.ac.kr/file-fe-prd/board/body.jpg"
        )
        item = {
            "source_id": "2",
            "notice_id": "200",
            "title": "[학사] 검증 불가",
            "url": "https://www.sogang.ac.kr/ko/detail/200?bbsConfigFk=2",
            "classification": "학사공지",
            "top": False,
            "body_status": "present",
            "body_blocks": [
                {
                    "type": "image",
                    "image": {
                        "type": "external",
                        "external": {"url": body_url},
                    },
                }
            ],
        }
        existing = managed_page("page-200", "2", "200")
        existing["properties"][sync_engine.BODY_MEDIA_STATE_PROPERTY] = (
            rich_text_property(
                json.dumps(
                    [
                        {
                            "type": "image",
                            "source_url": body_url,
                            "upload_id": "old-upload",
                            "block_id": "old-block",
                            "hosted_file_key": (
                                "notionusercontent.com/original"
                            ),
                            "content_sha256": (
                                utils.compute_content_sha256(b"image")
                            ),
                        }
                    ],
                    separators=(",", ":"),
                )
            )
        )
        context = sync_engine.DestinationContext(
            "token",
            "database",
            has_attachments_property=False,
        )

        with (
            notion_client.external_download_run_scope(force_new=True),
            patch.object(
                sync_engine,
                "find_existing_page",
                return_value=existing,
            ),
            patch.object(
                notion_client,
                "download_file_bytes",
                return_value=(None, None),
            ),
            patch.object(
                sync_engine,
                "inspect_existing_uploaded_media_blocks",
                return_value=({}, "valid"),
            ),
            patch.object(
                sync_engine,
                "destination_quote_fingerprint",
            ) as quote_fingerprint,
            patch.object(
                sync_engine,
                "shrink_candidate_for_item",
            ) as shrink_candidate,
        ):
            preflight = sync_engine.resolve_destination_preflight(
                context,
                [item],
                atomic_recheck=False,
            )[0]

        self.assertEqual(
            preflight.deferred_reason,
            "body_media_content_unavailable",
        )
        quote_fingerprint.assert_not_called()
        shrink_candidate.assert_not_called()

    def test_preflight_preserves_unavailable_media_for_new_notice(self):
        body_url = (
            "https://www.sogang.ac.kr/file-fe-prd/board/body.jpg"
        )
        item = {
            "source_id": "2",
            "notice_id": "200",
            "title": "[학사] 신규 원문 보존",
            "url": "https://www.sogang.ac.kr/ko/detail/200?bbsConfigFk=2",
            "classification": "학사공지",
            "top": False,
            "body_status": "present",
            "body_blocks": [
                {
                    "type": "image",
                    "image": {
                        "type": "external",
                        "external": {"url": body_url},
                    },
                }
            ],
        }
        context = sync_engine.DestinationContext(
            "token",
            "database",
            has_attachments_property=False,
        )

        with (
            notion_client.external_download_run_scope(force_new=True),
            patch.object(
                sync_engine,
                "find_existing_page",
                return_value=None,
            ),
            patch.object(
                notion_client,
                "download_file_bytes",
                return_value=(None, None),
            ),
        ):
            preflight = sync_engine.resolve_destination_preflight(
                context,
                [item],
                atomic_recheck=False,
            )[0]

        self.assertEqual(preflight.deferred_reason, "")
        self.assertEqual(preflight.body_media_content_state, [])

    def test_preflight_defers_existing_attachment_when_source_is_unavailable(
        self,
    ):
        attachment_url = (
            "https://www.sogang.ac.kr/file-fe-prd/board/image.jpg"
        )
        item = {
            "source_id": "2",
            "notice_id": "200",
            "title": "[학사] 첨부 검증 불가",
            "url": "https://www.sogang.ac.kr/ko/detail/200?bbsConfigFk=2",
            "classification": "학사공지",
            "top": False,
            "body_status": "confirmed_empty",
            "body_blocks": [],
            "attachments": [
                {
                    "name": "image.jpg",
                    "type": "external",
                    "external": {"url": attachment_url},
                }
            ],
        }
        existing = managed_page("page-200", "2", "200")
        existing["properties"][sync_engine.ATTACHMENT_STATE_PROPERTY] = (
            rich_text_property(
                json.dumps(
                    [
                        {
                            "source_url": attachment_url,
                            "name": "image.jpg",
                            "occurrence": 1,
                            "upload_id": "old-upload",
                            "hosted_file_key": (
                                "notionusercontent.com/original"
                            ),
                            "content_sha256": (
                                utils.compute_content_sha256(b"image")
                            ),
                        }
                    ],
                    separators=(",", ":"),
                )
            )
        )
        context = sync_engine.DestinationContext(
            "token",
            "database",
        )

        with (
            notion_client.external_download_run_scope(force_new=True),
            patch.object(
                sync_engine,
                "find_existing_page",
                return_value=existing,
            ),
            patch.object(
                notion_client,
                "download_file_bytes",
                return_value=(None, None),
            ),
            patch.object(
                sync_engine,
                "destination_quote_fingerprint",
            ) as quote_fingerprint,
            patch.object(
                sync_engine,
                "shrink_candidate_for_item",
            ) as shrink_candidate,
        ):
            preflight = sync_engine.resolve_destination_preflight(
                context,
                [item],
                atomic_recheck=False,
            )[0]

        self.assertEqual(
            preflight.deferred_reason,
            "attachment_content_unavailable",
        )
        quote_fingerprint.assert_not_called()
        shrink_candidate.assert_not_called()

    def test_media_deferred_item_does_not_block_other_notice(self):
        deferred_item = {
            "notice_id": "200",
            "title": "공지 200",
            "url": "https://www.sogang.ac.kr/ko/detail/200",
            "top": False,
            "body_blocks": [],
        }
        healthy_item = {
            "notice_id": "201",
            "title": "공지 201",
            "url": "https://www.sogang.ac.kr/ko/detail/201",
            "top": False,
            "body_blocks": [],
        }
        result = source_result(
            "2",
            SourceStatus.SUCCESS,
            [deferred_item, healthy_item],
        )
        report = CrawlReport(sources=[result])
        prepared = sync_engine.prepare_source_items(result)
        preflight = [
            sync_engine.DestinationPreflight(
                item=prepared[0],
                existing_page=managed_page("page-200", "2", "200"),
                operation_id="operation-200",
                shrink_key="2:200",
                shrink_candidate=None,
                deferred_reason="body_media_content_unavailable",
            ),
            sync_engine.DestinationPreflight(
                item=prepared[1],
                existing_page=None,
                operation_id="operation-201",
                shrink_key="2:201",
                shrink_candidate=None,
            ),
        ]
        context = sync_engine.DestinationContext("token", "database")
        applied: list[str] = []

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=context,
            ),
            patch.object(
                sync_engine,
                "resolve_destination_preflight",
                return_value=preflight,
            ),
            patch.object(
                sync_engine,
                "validate_destination_preflight_entries",
            ) as validate_entries,
            patch.object(
                sync_engine,
                "refresh_destination_preflight_entry",
                return_value=None,
            ),
            patch.object(
                sync_engine,
                "apply_item",
                side_effect=lambda _context, item, *_args, **_kwargs: (
                    applied.append(str(item["notice_id"]))
                ),
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
        ):
            counters = sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                run_id="run-media-isolated",
            )

        self.assertEqual(applied, ["201"])
        validated = validate_entries.call_args.args[1]
        self.assertEqual(
            [entry.item["notice_id"] for entry in validated],
            ["201"],
        )
        self.assertEqual(counters.media_deferred, 1)
        self.assertEqual(
            counters.unresolved_pending_notices,
            {"2": ["200"]},
        )
        self.assertEqual(counters.quarantined_source_ids, [])

    def test_safe_source_results_selects_only_successful_source(self):
        partial = source_result("141", SourceStatus.PARTIAL)
        success = source_result("2", SourceStatus.SUCCESS)
        report = CrawlReport(sources=[partial, success])

        selected = sync_engine.safe_source_results(report)

        self.assertEqual(
            [result.source.config_fk for result in selected],
            ["2"],
        )

    def test_apply_report_never_prepares_partial_source(self):
        partial = source_result("141", SourceStatus.PARTIAL)
        success = source_result("2", SourceStatus.SUCCESS)
        report = CrawlReport(sources=[partial, success])
        prepared_source_ids = []

        def prepare_source_items(result):
            prepared_source_ids.append(result.source.config_fk)
            return []

        with (
            patch.object(
                sync_engine,
                "prepare_source_items",
                side_effect=prepare_source_items,
            ),
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=sync_engine.DestinationContext(
                    "token",
                    "database",
                ),
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
        ):
            counters = sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                run_id="partial-source",
            )

        self.assertEqual(prepared_source_ids, ["2"])
        self.assertEqual(counters.writes, 0)

    def test_unchanged_apply_item_performs_no_notion_write(self):
        item = {
            "source_id": "2",
            "notice_id": "200",
            "title": "[학사] 테스트",
            "url": "https://www.sogang.ac.kr/ko/detail/200?bbsConfigFk=2",
            "date": "2026-07-27T00:00:00+09:00",
            "classification": "학사공지",
            "type": "학사",
            "views": 200,
            "top": False,
            "body_blocks": [],
        }
        operation_id = sync_engine.operation_id_for_item(item)
        properties = sync.build_properties(
            {**item, "views": 100},
            True,
            False,
            True,
        )
        properties[sync.SYNC_STATUS_PROPERTY] = {
            "rich_text": sync_engine.build_rich_text_chunks("committed")
        }
        properties[sync.SYNC_OPERATION_PROPERTY] = {
            "rich_text": sync_engine.build_rich_text_chunks(operation_id)
        }
        properties[sync.SYNC_GENERATION_PROPERTY] = {
            "rich_text": sync_engine.build_rich_text_chunks(operation_id)
        }
        properties = notion_read_properties(properties)
        existing = {"id": "page-200", "properties": properties}
        context = sync_engine.DestinationContext(
            token="token",
            database_id="database",
            has_views_property=True,
            has_attachments_property=False,
            has_classification_property=True,
        )
        counters = SyncCounters()
        with (
            patch.object(
                sync_engine,
                "find_existing_page",
                return_value=existing,
            ),
            patch.object(sync_engine, "check_run_control"),
            patch.object(sync_engine, "create_page") as create,
            patch.object(sync_engine, "update_page") as update,
            patch.object(sync_engine, "sync_page_body_blocks") as replace_body,
            patch.object(
                sync_engine,
                "verify_committed_item",
            ) as verify,
        ):
            sync_engine.apply_item(
                context,
                item,
                counters,
                force_commit_readback=True,
            )

        create.assert_not_called()
        update.assert_not_called()
        replace_body.assert_not_called()
        verify.assert_called_once()
        self.assertEqual(counters.writes, 0)
        self.assertEqual(counters.unchanged, 1)

    def test_operation_id_ignores_view_count_only_changes(self):
        item = {
            "source_id": "2",
            "notice_id": "view-only",
            "title": "조회수 정책",
            "date": "2026-08-01T00:00:00+09:00",
            "author": "작성자",
            "views": 100,
            "top": False,
            "classification": "학사공지",
            "url": (
                "https://www.sogang.ac.kr/ko/detail/view-only"
                "?bbsConfigFk=2"
            ),
        }

        self.assertEqual(
            sync_engine.operation_id_for_item(item),
            sync_engine.operation_id_for_item({**item, "views": 999}),
        )

    def test_existing_page_readback_ignores_view_count_only_change(self):
        item = {
            "source_id": "2",
            "notice_id": "view-readback",
            "title": "조회수 재조회",
            "date": "2026-08-01T00:00:00+09:00",
            "author": "작성자",
            "views": 200,
            "top": False,
            "classification": "학사공지",
            "type": "학사",
            "url": (
                "https://www.sogang.ac.kr/ko/detail/view-readback"
                "?bbsConfigFk=2"
            ),
        }
        operation_id = sync_engine.operation_id_for_item(item)
        stored_item = {
            **item,
            "views": 100,
            "operation_id": operation_id,
            "generation_id": "generation",
            "sync_status": "committed",
        }
        page = {
            "id": "page-view-readback",
            "properties": notion_read_properties(
                sync_engine.build_properties(
                    stored_item,
                    True,
                    True,
                    True,
                )
            ),
        }

        reasons = sync_engine.committed_item_readback_reasons(
            "token",
            page,
            item,
            sync_engine.DestinationContext("token", "database"),
            "page-view-readback",
            operation_id,
            "generation",
            "",
            [],
            [],
            False,
            True,
        )

        self.assertEqual(reasons, [])

    def test_invalid_body_payload_stops_before_upload_or_page_write(self):
        invalid_bodies = {
            "rich_text": [
                {
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {"content": str(index)},
                            }
                            for index in range(101)
                        ]
                    },
                }
            ],
            "table_children": [
                paragraph_block("표 본문"),
                {
                    "type": "table",
                    "table": {
                        "children": [
                            {
                                "type": "table_row",
                                "table_row": {"cells": [[]]},
                            }
                            for _ in range(101)
                        ]
                    },
                },
            ],
        }
        context = sync_engine.DestinationContext(
            "token",
            "database",
            has_views_property=False,
            has_attachments_property=True,
            has_classification_property=False,
        )
        for label, body_blocks in invalid_bodies.items():
            with self.subTest(label=label):
                item = {
                    "source_id": "2",
                    "notice_id": f"invalid-{label}",
                    "title": f"공지 invalid-{label}",
                    "top": False,
                    "body_status": "present",
                    "body_blocks": body_blocks,
                    "attachments": [],
                }
                counters = SyncCounters()
                with (
                    patch.object(sync_engine, "check_run_control"),
                    patch.object(
                        sync_engine,
                        "should_upload_files_to_notion",
                        return_value=True,
                    ),
                    patch.object(
                        sync_engine,
                        "prepare_attachments_for_sync",
                    ) as prepare_attachments,
                    patch.object(
                        sync_engine,
                        "prepare_body_blocks_for_sync",
                    ) as prepare_body,
                    patch.object(sync_engine, "create_page") as create,
                    patch.object(sync_engine, "update_page") as update,
                    patch.object(
                        sync_engine,
                        "sync_page_body_blocks",
                    ) as replace_body,
                    self.assertRaisesRegex(
                        notion_client.NotionPayloadError,
                        "배열 한도 초과",
                    ),
                ):
                    sync_engine.apply_item(
                        context,
                        item,
                        counters,
                        existing_page=None,
                        existing_page_resolved=True,
                    )

                prepare_attachments.assert_not_called()
                prepare_body.assert_not_called()
                create.assert_not_called()
                update.assert_not_called()
                replace_body.assert_not_called()
                self.assertEqual(counters.writes, 0)

    def test_invalid_later_item_stops_entire_report_before_destination_access(
        self,
    ):
        invalid_bodies = {
            "rich_text": [
                {
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {"content": str(index)},
                            }
                            for index in range(101)
                        ]
                    },
                }
            ],
            "table_children": [
                paragraph_block("표 본문"),
                {
                    "type": "table",
                    "table": {
                        "children": [
                            {
                                "type": "table_row",
                                "table_row": {"cells": [[]]},
                            }
                            for _ in range(101)
                        ]
                    },
                },
            ],
        }
        for label, invalid_body in invalid_bodies.items():
            with self.subTest(label=label):
                report = CrawlReport(
                    sources=[
                        source_result(
                            "2",
                            SourceStatus.SUCCESS,
                            [
                                {
                                    "notice_id": "valid-first",
                                    "title": "정상 공지",
                                    "url": (
                                        "https://www.sogang.ac.kr/ko/detail/"
                                        "valid-first?bbsConfigFk=2"
                                    ),
                                    "top": False,
                                    "body_blocks": [
                                        paragraph_block("정상 본문")
                                    ],
                                    "attachments": [
                                        {
                                            "name": "image.png",
                                            "type": "external",
                                            "external": {
                                                "url": (
                                                    "https://www.sogang.ac.kr/"
                                                    "image.png"
                                                )
                                            },
                                        }
                                    ],
                                },
                                {
                                    "notice_id": f"invalid-{label}",
                                    "title": "잘못된 공지",
                                    "url": (
                                        "https://www.sogang.ac.kr/ko/detail/"
                                        f"invalid-{label}?bbsConfigFk=2"
                                    ),
                                    "top": False,
                                    "body_blocks": invalid_body,
                                    "attachments": [],
                                },
                            ],
                        )
                    ]
                )
                with (
                    patch.object(
                        sync_engine,
                        "prepare_destination",
                    ) as prepare_destination,
                    patch.object(
                        sync_engine,
                        "prepare_attachments_for_sync",
                    ) as prepare_attachments,
                    patch.object(
                        sync_engine,
                        "prepare_body_blocks_for_sync",
                    ) as prepare_body,
                    patch.object(sync_engine, "create_page") as create,
                    patch.object(sync_engine, "update_page") as update,
                    patch.object(
                        sync_engine,
                        "sync_page_body_blocks",
                    ) as replace_body,
                    patch.object(
                        notion_client,
                        "create_file_upload",
                    ) as create_upload,
                    patch.object(
                        notion_client,
                        "send_file_upload",
                    ) as send_upload,
                    self.assertRaisesRegex(
                        notion_client.NotionPayloadError,
                        "배열 한도 초과",
                    ),
                ):
                    sync_engine.apply_report(
                        "token",
                        "database",
                        report,
                        False,
                    )

                prepare_destination.assert_not_called()
                prepare_attachments.assert_not_called()
                prepare_body.assert_not_called()
                create.assert_not_called()
                update.assert_not_called()
                replace_body.assert_not_called()
                create_upload.assert_not_called()
                send_upload.assert_not_called()

    def test_body_failure_leaves_staged_page_pending(self):
        body_blocks = [paragraph_block("새 본문")]
        item = {
            "source_id": "2",
            "notice_id": "body-failure",
            "title": "공지 body-failure",
            "top": True,
            "body_status": "present",
            "body_blocks": body_blocks,
        }
        operation_id = sync_engine.operation_id_for_item(item)
        existing = managed_page(
            "page-body-failure",
            "2",
            "body-failure",
        )
        existing["properties"][sync.SYNC_STATUS_PROPERTY] = (
            rich_text_property("committed")
        )
        existing["properties"][sync.SYNC_OPERATION_PROPERTY] = (
            rich_text_property(operation_id)
        )
        existing["properties"][sync_engine.BODY_HASH_PROPERTY] = (
            rich_text_property("old-hash")
        )
        context = sync_engine.DestinationContext(
            "token",
            "database",
            has_views_property=False,
            has_attachments_property=False,
            has_classification_property=False,
        )
        counters = SyncCounters()
        with (
            patch.object(sync_engine, "check_run_control"),
            patch.object(
                sync_engine,
                "prepare_body_blocks_for_sync",
                return_value=(body_blocks, body_blocks, []),
            ),
            patch.object(
                sync_engine,
                "sync_page_body_blocks",
                side_effect=RuntimeError("본문 교체 실패"),
            ),
            patch.object(sync_engine, "update_page") as update,
            self.assertRaisesRegex(RuntimeError, "본문 교체 실패"),
        ):
            sync_engine.apply_item(
                context,
                item,
                counters,
                existing_page=existing,
                existing_page_resolved=True,
            )

        self.assertEqual(update.call_count, 1)
        self.assertEqual(
            sync.rich_text_value_from_payload(
                update.call_args_list[0]
                .args[2][sync.SYNC_STATUS_PROPERTY]
            ),
            "pending",
        )
        self.assertEqual(counters.property_updates, 1)
        self.assertEqual(counters.writes, 1)

    def test_sync_failure_is_not_overwritten_by_cleanup(self):
        body_blocks = [paragraph_block("새 본문")]
        item = {
            "source_id": "2",
            "notice_id": "quarantine-failure",
            "title": "공지 quarantine-failure",
            "top": True,
            "body_status": "present",
            "body_blocks": body_blocks,
        }
        operation_id = sync_engine.operation_id_for_item(item)
        existing = managed_page(
            "page-quarantine-failure",
            "2",
            "quarantine-failure",
        )
        existing["properties"][sync.SYNC_STATUS_PROPERTY] = (
            rich_text_property("committed")
        )
        existing["properties"][sync.SYNC_OPERATION_PROPERTY] = (
            rich_text_property(operation_id)
        )
        existing["properties"][sync_engine.BODY_HASH_PROPERTY] = (
            rich_text_property("old-hash")
        )
        context = sync_engine.DestinationContext(
            "token",
            "database",
            has_views_property=False,
            has_attachments_property=False,
            has_classification_property=False,
        )
        with (
            patch.object(sync_engine, "check_run_control"),
            patch.object(
                sync_engine,
                "prepare_body_blocks_for_sync",
                return_value=(body_blocks, body_blocks, []),
            ),
            patch.object(
                sync_engine,
                "sync_page_body_blocks",
                side_effect=RuntimeError("원래 동기화 실패"),
            ),
            patch.object(
                sync_engine,
                "update_page",
            ) as update,
            self.assertRaisesRegex(RuntimeError, "원래 동기화 실패"),
        ):
            sync_engine.apply_item(
                context,
                item,
                SyncCounters(),
                existing_page=existing,
                existing_page_resolved=True,
            )

        update.assert_called_once()

    def test_property_update_commits_after_pending_stage(self):
        item = {
            "source_id": "2",
            "notice_id": "property-stage",
            "title": "새 제목",
            "top": True,
            "body_blocks": [],
        }
        operation_id = sync_engine.operation_id_for_item(item)
        existing = managed_page(
            "page-property-stage",
            "2",
            "property-stage",
        )
        existing["properties"][sync.SYNC_STATUS_PROPERTY] = (
            rich_text_property("committed")
        )
        existing["properties"][sync.SYNC_OPERATION_PROPERTY] = (
            rich_text_property(operation_id)
        )
        context = sync_engine.DestinationContext(
            "token",
            "database",
            has_views_property=False,
            has_attachments_property=False,
            has_classification_property=False,
        )
        counters = SyncCounters()
        with (
            patch.object(sync_engine, "check_run_control"),
            patch.object(sync_engine, "verify_committed_item"),
            patch.object(sync_engine, "update_page") as update,
        ):
            sync_engine.apply_item(
                context,
                item,
                counters,
                existing_page=existing,
                existing_page_resolved=True,
            )

        self.assertEqual(update.call_count, 2)
        self.assertEqual(
            sync.rich_text_value_from_payload(
                update.call_args_list[0]
                .args[2][sync.SYNC_STATUS_PROPERTY]
            ),
            "pending",
        )
        self.assertEqual(
            sync.rich_text_value_from_payload(
                update.call_args_list[1]
                .args[2][sync.SYNC_STATUS_PROPERTY]
            ),
            "committed",
        )
        self.assertEqual(counters.property_updates, 2)
        self.assertEqual(counters.writes, 2)

    def test_mutation_requires_committed_readback_before_operation_record(self):
        item = {
            "source_id": "2",
            "notice_id": "readback-stale",
            "title": "새 제목",
            "top": True,
            "body_blocks": [],
        }
        operation_id = sync_engine.operation_id_for_item(item)
        existing = managed_page(
            "page-readback-stale",
            "2",
            "readback-stale",
        )
        existing["properties"][sync.SYNC_STATUS_PROPERTY] = (
            rich_text_property("committed")
        )
        existing["properties"][sync.SYNC_OPERATION_PROPERTY] = (
            rich_text_property(operation_id)
        )
        context = sync_engine.DestinationContext(
            "token",
            "database",
            has_views_property=False,
            has_attachments_property=False,
            has_classification_property=False,
        )
        counters = SyncCounters()
        with (
            patch.object(sync_engine, "check_run_control"),
            patch.object(sync_engine, "update_page") as update,
            patch.object(
                sync_engine,
                "retrieve_page",
                return_value=existing,
            ),
            patch.object(
                sync_engine,
                "COMMIT_READBACK_DELAYS",
                (0.0,),
            ),
            self.assertRaisesRegex(
                sync_engine.DestinationConsistencyError,
                "확정 상태 재조회 검증",
            ),
        ):
            sync_engine.apply_item(
                context,
                item,
                counters,
                existing_page=existing,
                existing_page_resolved=True,
            )

        self.assertEqual(update.call_count, 2)
    def test_committed_readback_accepts_matching_identity_and_metadata(self):
        item = {
            "source_id": "2",
            "notice_id": "readback-valid",
            "title": "정상 커밋",
            "url": (
                "https://www.sogang.ac.kr/ko/detail/readback-valid"
                "?bbsConfigFk=2"
            ),
            "top": False,
            "body_blocks": [],
        }
        operation_id = sync_engine.operation_id_for_item(item)
        generation_id = operation_id
        committed_item = {
            **item,
            "operation_id": operation_id,
            "generation_id": generation_id,
            "sync_status": "committed",
        }
        page = {
            "id": "page-readback-valid",
            "properties": notion_read_properties(
                sync_engine.build_properties(
                    committed_item,
                    False,
                    False,
                    False,
                )
            ),
        }
        context = sync_engine.DestinationContext(
            "token",
            "database",
            has_views_property=False,
            has_attachments_property=False,
            has_classification_property=False,
        )
        with (
            patch.object(
                sync_engine,
                "retrieve_page",
                return_value=page,
            ) as retrieve,
            patch.object(sync_engine, "check_run_control"),
        ):
            sync_engine.verify_committed_item(
                "token",
                item,
                context,
                "page-readback-valid",
                operation_id,
                generation_id,
                "",
                [],
                [],
                False,
            )

        retrieve.assert_called_once_with(
            "token",
            "page-readback-valid",
        )

    def test_committed_readback_preserves_body_media_generation(self):
        body_blocks = [paragraph_block("미디어 본문")]
        body_hash = sync_engine.compute_body_hash(body_blocks)
        item = {
            "source_id": "2",
            "notice_id": "readback-media",
            "title": "미디어 커밋",
            "url": (
                "https://www.sogang.ac.kr/ko/detail/readback-media"
                "?bbsConfigFk=2"
            ),
            "top": False,
            "body_status": "present",
            "body_blocks": body_blocks,
        }
        operation_id = sync_engine.operation_id_for_item(item)
        body_media_state = [
            {
                "type": "image",
                "source_url": (
                    "https://www.sogang.ac.kr/file-fe-prd/board/image.png"
                ),
                "upload_id": "upload-1",
                "content_sha256": "a" * 64,
                "generation_id": body_hash,
            }
        ]
        committed_item = {
            **item,
            "operation_id": operation_id,
            "generation_id": body_hash,
            "sync_status": "committed",
        }
        properties = notion_read_properties(
            sync_engine.build_properties(
                committed_item,
                False,
                False,
                False,
            )
        )
        properties[sync.BODY_HASH_PROPERTY] = rich_text_property(
            body_hash
        )
        properties[sync.BODY_MEDIA_STATE_PROPERTY] = rich_text_property(
            json.dumps(
                body_media_state,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        page = {
            "id": "page-readback-media",
            "properties": properties,
        }
        context = sync_engine.DestinationContext(
            "token",
            "database",
            has_views_property=False,
            has_attachments_property=False,
            has_classification_property=False,
        )
        with patch.object(
            sync_engine,
            "is_body_generation_current",
            return_value=True,
        ):
            reasons = sync_engine.committed_item_readback_reasons(
                "token",
                page,
                item,
                context,
                "page-readback-media",
                operation_id,
                body_hash,
                body_hash,
                [],
                body_media_state,
                False,
            )

        self.assertEqual(reasons, [])

    def test_committed_readback_rejects_tampered_body_generation(self):
        body_blocks = [paragraph_block("본문")]
        item = {
            "source_id": "2",
            "notice_id": "readback-body",
            "title": "본문 커밋",
            "top": False,
            "body_status": "present",
            "body_blocks": body_blocks,
        }
        operation_id = sync_engine.operation_id_for_item(item)
        body_hash = sync_engine.compute_body_hash(body_blocks)
        committed_item = {
            **item,
            "operation_id": operation_id,
            "generation_id": body_hash,
            "sync_status": "committed",
        }
        properties = notion_read_properties(
            sync_engine.build_properties(
                committed_item,
                False,
                False,
                False,
            )
        )
        properties[sync_engine.BODY_HASH_PROPERTY] = (
            rich_text_property(body_hash)
        )
        page = {
            "id": "page-readback-body",
            "properties": properties,
        }
        context = sync_engine.DestinationContext(
            "token",
            "database",
            has_views_property=False,
            has_attachments_property=False,
            has_classification_property=False,
        )
        with (
            patch.object(
                sync_engine,
                "retrieve_page",
                return_value=page,
            ),
            patch.object(
                sync_engine,
                "is_body_generation_current",
                return_value=False,
            ),
            patch.object(sync_engine, "check_run_control"),
            patch.object(
                sync_engine,
                "COMMIT_READBACK_DELAYS",
                (0.0,),
            ),
            self.assertRaisesRegex(
                sync_engine.DestinationConsistencyError,
                "body_generation",
            ),
        ):
            sync_engine.verify_committed_item(
                "token",
                item,
                context,
                "page-readback-body",
                operation_id,
                body_hash,
                body_hash,
                [],
                [],
                False,
            )

    def test_operation_id_uses_stable_attachment_and_body_identity(self):
        def item(
            attachment_file_id: str,
            body_file_id: str,
            signature: str,
        ) -> dict:
            attachment_url = (
                "https://www.sogang.ac.kr/file-fe-prd/board/image.jpg"
                f"?fileId={attachment_file_id}&signature={signature}"
            )
            body_url = (
                "https://www.sogang.ac.kr/file-fe-prd/board/body.jpg"
                f"?fileId={body_file_id}&signature={signature}"
            )
            return {
                "source_id": "2",
                "notice_id": "stable-operation",
                "title": "안정 작업",
                "url": (
                    "https://www.sogang.ac.kr/ko/detail/stable-operation"
                    "?bbsConfigFk=2"
                ),
                "top": False,
                "attachments": [
                    {
                        "name": "image.jpg",
                        "type": "external",
                        "external": {"url": attachment_url},
                    }
                ],
                "body_blocks": [
                    {
                        "type": "image",
                        "image": {
                            "type": "external",
                            "external": {"url": body_url},
                        },
                    }
                ],
            }

        old_signature = sync_engine.operation_id_for_item(
            item("77", "88", "old")
        )
        new_signature = sync_engine.operation_id_for_item(
            item("77", "88", "new")
        )
        changed_attachment = sync_engine.operation_id_for_item(
            item("78", "88", "new")
        )
        changed_body = sync_engine.operation_id_for_item(
            item("77", "89", "new")
        )

        self.assertEqual(old_signature, new_signature)
        self.assertNotEqual(old_signature, changed_attachment)
        self.assertNotEqual(old_signature, changed_body)

    def test_missing_nonempty_body_generation_forces_resync(self):
        body_blocks = [paragraph_block("동일 본문")]
        body_hash = sync_engine.compute_body_hash(body_blocks)
        item = {
            "source_id": "2",
            "notice_id": "body-drift",
            "title": "본문 드리프트",
            "url": (
                "https://www.sogang.ac.kr/ko/detail/body-drift"
                "?bbsConfigFk=2"
            ),
            "classification": "학사공지",
            "type": "학사",
            "top": False,
            "body_status": "present",
            "body_blocks": body_blocks,
        }
        existing = managed_page("page-body-drift", "2", "body-drift")
        existing["properties"][sync_engine.BODY_HASH_PROPERTY] = (
            rich_text_property(body_hash)
        )
        context = sync_engine.DestinationContext(
            "token",
            "database",
            has_views_property=False,
            has_attachments_property=False,
            has_classification_property=True,
        )
        counters = SyncCounters()

        with (
            patch.object(sync_engine, "check_run_control"),
            patch.object(
                sync_engine,
                "is_body_generation_current",
                return_value=False,
            ) as generation_current,
            patch.object(
                sync_engine,
                "sync_page_body_blocks",
                side_effect=successful_body_sync,
            ) as replace_body,
            patch.object(sync_engine, "verify_committed_item"),
            patch.object(sync_engine, "update_page"),
        ):
            sync_engine.apply_item(
                context,
                item,
                counters,
                existing_page=existing,
                existing_page_resolved=True,
            )

        generation_current.assert_called_once_with(
            "token",
            "page-body-drift",
            body_hash,
        )
        replace_body.assert_called_once()
        self.assertEqual(counters.body_updates, 1)

    def test_tampered_managed_body_child_forces_apply_resync(self):
        body_blocks = [
            {
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {"content": "정상 본문"},
                        }
                    ]
                },
            }
        ]
        body_hash = sync_engine.compute_body_hash(body_blocks)
        item = {
            "source_id": "2",
            "notice_id": "body-tamper",
            "title": "본문 변조",
            "url": (
                "https://www.sogang.ac.kr/ko/detail/body-tamper"
                "?bbsConfigFk=2"
            ),
            "classification": "학사공지",
            "type": "학사",
            "top": False,
            "body_status": "present",
            "body_blocks": body_blocks,
        }
        existing = managed_page("page-body-tamper", "2", "body-tamper")
        existing["properties"][sync_engine.BODY_HASH_PROPERTY] = (
            rich_text_property(body_hash)
        )
        context = sync_engine.DestinationContext(
            "token",
            "database",
            has_views_property=False,
            has_attachments_property=False,
            has_classification_property=True,
        )
        counters = SyncCounters()

        with (
            patch.object(sync_engine, "check_run_control"),
            patch.object(
                sync_engine,
                "inspect_existing_uploaded_media_blocks",
                return_value=({}, "valid"),
            ),
            patch.object(
                sync_engine,
                "shrink_candidate_for_item",
                return_value=None,
            ),
            patch.object(
                sync_engine,
                "is_body_generation_current",
                return_value=False,
            ),
            patch.object(
                sync_engine,
                "sync_page_body_blocks",
                side_effect=successful_body_sync,
            ) as replace_body,
            patch.object(sync_engine, "verify_committed_item"),
            patch.object(sync_engine, "update_page"),
        ):
            sync_engine.apply_item(
                context,
                item,
                counters,
                existing_page=existing,
                existing_page_resolved=True,
            )

        replace_body.assert_called_once()
        self.assertEqual(counters.body_updates, 1)

    def test_opaque_attachment_reread_converges_without_second_write(self):
        source_url = (
            "https://www.sogang.ac.kr/file-fe-prd/board/1/sample.jpg"
            "?sg=sample.jpg"
        )
        opaque_url = (
            "https://prod-files-secure.s3.us-west-2.amazonaws.com/"
            "opaque/asset?X-Amz-Signature=first"
        )

        def make_item() -> dict:
            return {
                "source_id": "2",
                "notice_id": "201",
                "title": "[학사] 첨부 테스트",
                "url": "https://www.sogang.ac.kr/ko/detail/201?bbsConfigFk=2",
                "classification": "학사공지",
                "type": "학사",
                "top": False,
                "body_blocks": [],
                "attachments": [
                    {
                        "name": "sample.jpg",
                        "type": "external",
                        "external": {"url": source_url},
                    }
                ],
            }

        opaque_files = {
            sync.ATTACHMENT_PROPERTY: {
                "type": "files",
                "files": [
                    {
                        "name": "sample.jpg",
                        "type": "file",
                        "file": {
                            "url": opaque_url,
                            "expiry_time": "2026-07-28T00:00:00Z",
                        },
                    }
                ],
            }
        }
        context = sync_engine.DestinationContext(
            token="token",
            database_id="database",
            has_views_property=False,
            has_attachments_property=True,
            has_classification_property=True,
        )
        first_counters = SyncCounters()
        second_counters = SyncCounters()
        with (
            patch.object(sync_engine, "check_run_control"),
            patch.object(
                sync_engine,
                "find_existing_page",
                return_value=None,
            ) as find_page,
            patch.object(
                sync_engine,
                "create_page",
                return_value="page-201",
            ) as create,
            patch.object(sync_engine, "verify_committed_item"),
            patch.object(sync_engine, "update_page") as update,
            patch.object(
                sync,
                "notion_request",
                return_value={"properties": opaque_files},
            ),
            patch.object(
                notion_client,
                "download_file_bytes",
                return_value=(b"opaque-image", "image/jpeg"),
            ),
            patch.object(
                notion_client,
                "upload_external_file_to_notion",
                return_value="upload-opaque",
            ) as upload,
        ):
            sync_engine.apply_item(context, make_item(), first_counters)
            created_properties = copy.deepcopy(create.call_args.args[2])
            for update_call in update.call_args_list:
                created_properties.update(copy.deepcopy(update_call.args[2]))
            created_properties[sync.ATTACHMENT_PROPERTY] = copy.deepcopy(
                opaque_files[sync.ATTACHMENT_PROPERTY]
            )
            existing_page = {
                "id": "page-201",
                "properties": notion_read_properties(created_properties),
            }
            find_page.return_value = existing_page
            create.reset_mock()
            update.reset_mock()

            sync_engine.apply_item(context, make_item(), second_counters)

        self.assertEqual(upload.call_count, 1)
        create.assert_not_called()
        update.assert_not_called()
        self.assertEqual(second_counters.writes, 0)
        stored_state = sync.extract_attachment_state(
            existing_page["properties"]
        )
        self.assertEqual(
            stored_state[0]["hosted_file_key"],
            "prod-files-secure.s3.us-west-2.amazonaws.com/opaque/asset",
        )

    def test_same_name_opaque_attachments_reuse_by_occurrence(self):
        first_source = (
            "https://www.sogang.ac.kr/file-fe-prd/board/1/first.jpg"
            "?sg=same.jpg"
        )
        second_source = (
            "https://www.sogang.ac.kr/file-fe-prd/board/1/second.jpg"
            "?sg=same.jpg"
        )
        first_hash = utils.compute_content_sha256(b"first")
        second_hash = utils.compute_content_sha256(b"second")
        state = [
            {
                "source_url": first_source,
                "name": "same.jpg",
                "upload_id": "upload-first",
                "content_sha256": first_hash,
            },
            {
                "source_url": second_source,
                "name": "same.jpg",
                "upload_id": "upload-second",
                "content_sha256": second_hash,
            },
        ]
        properties = {
            sync.ATTACHMENT_PROPERTY: {
                "type": "files",
                "files": [
                    {
                        "name": "same.jpg",
                        "type": "file",
                        "file": {
                            "url": "https://notionusercontent.com/opaque/first?sig=1"
                        },
                    },
                    {
                        "name": "same.jpg",
                        "type": "file",
                        "file": {
                            "url": "https://notionusercontent.com/opaque/second?sig=2"
                        },
                    },
                ],
            }
        }

        enriched = sync.enrich_attachment_state_with_properties(
            properties,
            state,
            allow_opaque_binding=True,
        )
        reusable = sync.extract_existing_uploaded_attachment_ids(
            properties,
            enriched,
        )

        self.assertEqual(
            [entry["occurrence"] for entry in enriched],
            [1, 2],
        )
        self.assertEqual(
            [entry["hosted_file_key"] for entry in enriched],
            [
                "notionusercontent.com/opaque/first",
                "notionusercontent.com/opaque/second",
            ],
        )
        self.assertEqual(
            reusable,
            {
                first_source: [
                    {
                        "upload_id": "upload-first",
                        "content_sha256": first_hash,
                    }
                ],
                second_source: [
                    {
                        "upload_id": "upload-second",
                        "content_sha256": second_hash,
                    }
                ],
            },
        )

    def test_body_image_replacement_preserves_same_name_occurrences(self):
        body_blocks = [
            {
                "type": "image",
                "image": {
                    "type": "external",
                    "external": {
                        "url": (
                            "https://www.sogang.ac.kr/dataview/board/"
                            "0000000001same.jpg"
                        )
                    },
                },
            },
            {
                "type": "image",
                "image": {
                    "type": "external",
                    "external": {
                        "url": (
                            "https://www.sogang.ac.kr/dataview/board/"
                            "0000000002same.jpg"
                        )
                    },
                },
            },
        ]
        attachments = [
            {
                "name": "same.jpg",
                "type": "external",
                "external": {
                    "url": "https://www.sogang.ac.kr/files/first.jpg"
                },
            },
            {
                "name": "same.jpg",
                "type": "external",
                "external": {
                    "url": "https://www.sogang.ac.kr/files/second.jpg"
                },
            },
        ]

        replaced = utils.replace_body_image_urls(body_blocks, attachments)

        self.assertEqual(
            [
                block["image"]["external"]["url"]
                for block in replaced
            ],
            [
                "https://www.sogang.ac.kr/files/first.jpg",
                "https://www.sogang.ac.kr/files/second.jpg",
            ],
        )

    def test_body_image_replacement_leaves_ambiguous_collision_unchanged(self):
        original_url = (
            "https://www.sogang.ac.kr/dataview/board/"
            "0000000001same.jpg"
        )
        body_blocks = [
            {
                "type": "image",
                "image": {
                    "type": "external",
                    "external": {"url": original_url},
                },
            }
        ]
        attachments = [
            {
                "name": "same.jpg",
                "type": "external",
                "external": {
                    "url": "https://www.sogang.ac.kr/files/first.jpg"
                },
            },
            {
                "name": "same.jpg",
                "type": "external",
                "external": {
                    "url": "https://www.sogang.ac.kr/files/second.jpg"
                },
            },
        ]

        replaced = utils.replace_body_image_urls(body_blocks, attachments)

        self.assertEqual(
            replaced[0]["image"]["external"]["url"],
            original_url,
        )

    def test_apply_report_all_known_noop_succeeds(self):
        result = source_result("2", SourceStatus.SUCCESS, [])
        report = CrawlReport(sources=[result])
        context = sync_engine.DestinationContext("token", "database")

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=context,
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
        ):
            counters = sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                run_id="run-noop",
            )

        self.assertEqual(counters.writes, 0)
        self.assertEqual(counters.created, 0)

    def test_uncovered_pending_page_quarantines_only_its_source(self):
        result = source_result("2", SourceStatus.SUCCESS, [])
        report = CrawlReport(sources=[result])
        context = sync_engine.DestinationContext(
            "token",
            "database",
            pending_page_ids=("pending-1",),
            pending_page_sources={"pending-1": "2"},
            pending_page_notices={"pending-1": "1"},
        )
        pending = managed_page("pending-1", "2", "1")

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=context,
            ),
            patch.object(sync_engine, "apply_item") as apply_item,
            patch.object(sync_engine, "update_page") as update_page,
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[pending],
            ),
        ):
            counters = sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                run_id="run-recovery",
            )

        apply_item.assert_not_called()
        update_page.assert_not_called()
        self.assertEqual(counters.quarantined_source_ids, ["2"])
        self.assertEqual(
            counters.unresolved_pending_page_ids,
            ["pending-1"],
        )
        hold_key = sync_engine.destination_hold_key("2", "1")
        self.assertEqual(counters.destination_hold_count, 1)
        self.assertEqual(counters.repeated_destination_hold_count, 0)
        self.assertEqual(
            counters.destination_hold_observations[hold_key],
            {
                "candidate_id": "",
                "reason": "pending_refresh",
            },
        )

    def test_destination_hold_escalates_only_on_next_logical_run(self):
        result = source_result("2", SourceStatus.SUCCESS, [])
        report = CrawlReport(sources=[result])
        context = sync_engine.DestinationContext(
            "token",
            "database",
            pending_page_ids=("pending-1",),
            pending_page_sources={"pending-1": "2"},
            pending_page_notices={"pending-1": "1"},
        )
        pending = managed_page("pending-1", "2", "1")
        hold_key = sync_engine.destination_hold_key("2", "1")
        state = {
            "runs": [
                {
                    "run_id": "run-1",
                    "run_attempt": "1",
                    "execution_id": "run-1:1",
                }
            ],
            "sources": {},
            "destination_holds": {
                hold_key: {
                    "candidate_id": "",
                    "reason": "pending_refresh",
                    "observations": 1,
                    "last_observed_at": (
                        datetime.now(timezone.utc).isoformat()
                    ),
                    "last_observed_run_id": "run-1:1",
                    "last_observed_logical_run_id": "run-1",
                }
            },
        }

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=context,
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[pending],
            ),
        ):
            rerun = sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                previous_state=state,
                run_id="run-1:2",
                logical_run_id="run-1",
            )
            next_run = sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                previous_state=state,
                run_id="run-2:1",
                logical_run_id="run-2",
            )

        self.assertEqual(rerun.destination_hold_count, 1)
        self.assertEqual(rerun.repeated_destination_hold_count, 0)
        self.assertEqual(next_run.destination_hold_count, 1)
        self.assertEqual(
            next_run.repeated_destination_hold_count,
            1,
        )

    def test_destination_hold_requires_same_condition_to_escalate(self):
        hold_key = sync_engine.destination_hold_key("2", "1")
        candidate_id = "a" * 64
        state = {
            "runs": [
                {
                    "run_id": "run-1",
                    "run_attempt": "1",
                    "execution_id": "run-1:1",
                }
            ],
            "destination_holds": {
                hold_key: {
                    "candidate_id": candidate_id,
                    "reason": "destructive_change_confirmation",
                    "observations": 1,
                    "last_observed_at": (
                        datetime.now(timezone.utc).isoformat()
                    ),
                    "last_observed_run_id": "run-1:1",
                    "last_observed_logical_run_id": "run-1",
                }
            },
        }

        self.assertTrue(
            sync_engine.destination_hold_repeated(
                state,
                hold_key,
                "run-2",
                candidate_id,
                "destructive_change_confirmation",
            )
        )
        self.assertFalse(
            sync_engine.destination_hold_repeated(
                state,
                hold_key,
                "run-2",
                "b" * 64,
                "destructive_change_confirmation",
            )
        )
        self.assertFalse(
            sync_engine.destination_hold_repeated(
                state,
                hold_key,
                "run-2",
                "",
                "pending_refresh",
            )
        )

    def test_pending_quarantine_allows_other_source_items(self):
        quarantined_item = {
            "notice_id": "2",
            "title": "공지 2",
            "url": "https://www.sogang.ac.kr/ko/detail/2",
            "top": False,
            "body_blocks": [],
        }
        healthy_item = {
            "notice_id": "141",
            "title": "공지 141",
            "url": "https://www.sogang.ac.kr/ko/detail/141",
            "top": False,
            "body_blocks": [],
        }
        quarantined_result = source_result(
            "2",
            SourceStatus.SUCCESS,
            [quarantined_item],
        )
        healthy_result = source_result(
            "141",
            SourceStatus.SUCCESS,
            [healthy_item],
        )
        quarantined_result.top_snapshot_verified = True
        healthy_result.top_snapshot_verified = True
        report = CrawlReport(
            sources=[quarantined_result, healthy_result]
        )
        prepared_quarantined = sync_engine.prepare_source_items(
            quarantined_result
        )[0]
        prepared_healthy = sync_engine.prepare_source_items(
            healthy_result
        )[0]
        preflight = [
            sync_engine.DestinationPreflight(
                item=prepared_quarantined,
                existing_page=None,
                operation_id="operation-2",
                shrink_key="2:2",
                shrink_candidate=None,
            ),
            sync_engine.DestinationPreflight(
                item=prepared_healthy,
                existing_page=None,
                operation_id="operation-141",
                shrink_key="141:141",
                shrink_candidate=None,
            ),
        ]
        context = sync_engine.DestinationContext(
            "token",
            "database",
            pending_page_ids=("pending-1",),
            pending_page_sources={"pending-1": "2"},
            pending_page_notices={"pending-1": "1"},
        )
        pending = managed_page("pending-1", "2", "1")
        applied: list[str] = []
        top_inspected_sources: list[str] = []

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=context,
            ),
            patch.object(
                sync_engine,
                "resolve_destination_preflight",
                return_value=preflight,
            ),
            patch.object(
                sync_engine,
                "validate_destination_preflight_entries",
            ) as validate_entries,
            patch.object(
                sync_engine,
                "validate_destination_preflight_entry",
                return_value=None,
            ),
            patch.object(
                sync_engine,
                "apply_item",
                side_effect=lambda _context, item, *_args, **_kwargs: (
                    applied.append(str(item["source_id"]))
                ),
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[pending],
            ),
            patch.object(
                sync_engine,
                "inspect_missing_top",
                side_effect=lambda _token, _database, source_id, _ids: (
                    top_inspected_sources.append(source_id)
                    or ([], [])
                ),
            ),
            patch.object(
                sync_engine,
                "disable_missing_top",
                return_value=0,
            ) as disable_top,
        ):
            counters = sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                run_id="run-isolated",
            )

        self.assertEqual(applied, ["141"])
        validated = validate_entries.call_args.args[1]
        self.assertEqual(
            [entry.item["source_id"] for entry in validated],
            ["141"],
        )
        self.assertEqual(counters.quarantined_source_ids, ["2"])
        self.assertEqual(
            counters.unresolved_pending_page_ids,
            ["pending-1"],
        )
        self.assertTrue(top_inspected_sources)
        self.assertEqual(set(top_inspected_sources), {"141"})
        self.assertEqual(
            disable_top.call_args.args[2],
            "141",
        )

    def test_pending_without_source_metadata_remains_global_fail_closed(self):
        result = source_result("2", SourceStatus.SUCCESS, [])
        report = CrawlReport(sources=[result])
        context = sync_engine.DestinationContext(
            "token",
            "database",
            pending_page_ids=("pending-1",),
        )

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=context,
            ),
            patch.object(sync_engine, "apply_item") as apply_item,
            self.assertRaisesRegex(
                sync_engine.DestinationConsistencyError,
                "전역 차단",
            ),
        ):
            sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                run_id="run-invalid-pending",
            )

        apply_item.assert_not_called()

    def test_pending_from_unconfigured_source_remains_global_fail_closed(self):
        result = source_result("2", SourceStatus.SUCCESS, [])
        report = CrawlReport(sources=[result])
        context = sync_engine.DestinationContext(
            "token",
            "database",
            pending_page_ids=("pending-1",),
            pending_page_sources={"pending-1": "999"},
            pending_page_notices={"pending-1": "1"},
        )

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=context,
            ),
            patch.object(sync_engine, "apply_item") as apply_item,
            self.assertRaisesRegex(
                sync_engine.DestinationConsistencyError,
                "설정에 없는 출처",
            ),
        ):
            sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                run_id="run-unknown-pending",
            )

        apply_item.assert_not_called()

    def test_duplicate_pending_identity_remains_global_fail_closed(self):
        with self.assertRaisesRegex(
            sync_engine.DestinationConsistencyError,
            "중복",
        ):
            sync_engine.pending_page_context(
                [
                    managed_page("pending-1", "2", "1"),
                    managed_page("pending-2", "2", "1"),
                ]
            )

    def test_pending_shrink_gate_quarantines_entire_source(self):
        pending_item = {
            "notice_id": "1",
            "title": "공지 1",
            "url": "https://www.sogang.ac.kr/ko/detail/1",
            "top": False,
            "body_blocks": [],
        }
        sibling_item = {
            "notice_id": "2",
            "title": "공지 2",
            "url": "https://www.sogang.ac.kr/ko/detail/2",
            "top": False,
            "body_blocks": [],
        }
        healthy_item = {
            "notice_id": "141",
            "title": "공지 141",
            "url": "https://www.sogang.ac.kr/ko/detail/141",
            "top": False,
            "body_blocks": [],
        }
        quarantined_result = source_result(
            "2",
            SourceStatus.SUCCESS,
            [pending_item, sibling_item],
        )
        healthy_result = source_result(
            "141",
            SourceStatus.SUCCESS,
            [healthy_item],
        )
        report = CrawlReport(
            sources=[quarantined_result, healthy_result]
        )
        prepared = [
            *sync_engine.prepare_source_items(quarantined_result),
            *sync_engine.prepare_source_items(healthy_result),
        ]
        candidate_id = "c" * 64
        existing = managed_page("pending-1", "2", "1")
        preflight = [
            sync_engine.DestinationPreflight(
                item=prepared[0],
                existing_page=existing,
                operation_id="operation-1",
                shrink_key="2:1",
                shrink_candidate={
                    "candidate_id": candidate_id,
                    "reasons": ["body_shrink"],
                },
            ),
            sync_engine.DestinationPreflight(
                item=prepared[1],
                existing_page=None,
                operation_id="operation-2",
                shrink_key="2:2",
                shrink_candidate=None,
            ),
            sync_engine.DestinationPreflight(
                item=prepared[2],
                existing_page=None,
                operation_id="operation-141",
                shrink_key="141:141",
                shrink_candidate=None,
            ),
        ]
        context = sync_engine.DestinationContext(
            "token",
            "database",
            pending_page_ids=("pending-1",),
            pending_page_sources={"pending-1": "2"},
            pending_page_notices={"pending-1": "1"},
        )
        applied: list[str] = []

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=context,
            ),
            patch.object(
                sync_engine,
                "resolve_destination_preflight",
                return_value=preflight,
            ),
            patch.object(
                sync_engine,
                "validate_destination_preflight_entries",
            ),
            patch.object(
                sync_engine,
                "validate_destination_preflight_entry",
                return_value=None,
            ),
            patch.object(
                sync_engine,
                "apply_item",
                side_effect=lambda _context, item, *_args, **_kwargs: (
                    applied.append(str(item["source_id"]))
                ),
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[existing],
            ),
        ):
            counters = sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                previous_state={},
                run_id="run-shrink-quarantine",
            )

        self.assertEqual(applied, ["141"])
        self.assertEqual(counters.quarantined_source_ids, ["2"])
        self.assertEqual(
            counters.shrink_candidate_observations,
            {
                "2:1": {
                    "candidate_id": candidate_id,
                    "reasons": ["body_shrink"],
                }
            },
        )
        hold_key = sync_engine.destination_hold_key("2", "1")
        self.assertEqual(counters.destination_hold_count, 1)
        self.assertEqual(
            counters.destination_hold_observations[hold_key],
            {
                "candidate_id": candidate_id,
                "reason": "destructive_change_confirmation",
            },
        )

    def test_apply_report_counts_recovered_pending_without_cleanup_write(self):
        item = {
            "notice_id": "1",
            "title": "공지 1",
            "url": (
                "https://www.sogang.ac.kr/ko/detail/1"
                "?bbsConfigFk=2"
            ),
            "classification": "학사공지",
            "top": True,
            "body_status": "confirmed_empty",
            "body_blocks": [],
            "attachments": [],
        }
        result = source_result("2", SourceStatus.SUCCESS, [item])
        report = CrawlReport(sources=[result])
        context = sync_engine.DestinationContext(
            "token",
            "database",
            pending_page_ids=("pending-1",),
            pending_page_sources={"pending-1": "2"},
            pending_page_notices={"pending-1": "1"},
        )
        existing = managed_page("pending-1", "2", "1")
        existing["properties"][sync_engine.BODY_HASH_PROPERTY] = (
            rich_text_property(sync_engine.compute_body_hash([]))
        )

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=context,
            ),
            patch.object(
                sync_engine,
                "find_existing_page",
                return_value=existing,
            ),
            patch.object(
                sync_engine,
                "retrieve_page",
                return_value=existing,
            ),
            patch.object(
                sync_engine,
                "destination_quote_fingerprint",
                return_value="quote-state",
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
            patch.object(sync_engine, "apply_item") as apply_item,
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
            patch.object(sync_engine, "update_page") as update_page,
        ):
            counters = sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                run_id="run-recovery",
            )

        apply_item.assert_called_once()
        update_page.assert_not_called()
        self.assertEqual(counters.pending_seen, 1)
        self.assertEqual(counters.pending_recovered, 1)
        self.assertEqual(counters.property_updates, 0)
        self.assertEqual(counters.writes, 0)

    def test_late_preflight_target_change_causes_zero_mutation(self):
        items = [
            {
                "notice_id": str(value),
                "title": f"공지 {value}",
                "url": (
                    "https://www.sogang.ac.kr/ko/detail/"
                    f"{value}?bbsConfigFk=2"
                ),
                "classification": "학사공지",
                "top": False,
                "body_blocks": [],
            }
            for value in (1, 2)
        ]
        report = CrawlReport(
            sources=[source_result("2", SourceStatus.SUCCESS, items)]
        )
        context = sync_engine.DestinationContext("token", "database")
        changed = managed_page("late-page", "2", "2")

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=context,
            ),
            patch.object(
                sync_engine,
                "find_existing_page",
                side_effect=[None, None, None, changed],
            ),
            patch.object(sync_engine, "apply_item") as apply_item,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "적용 직전 대상",
            ):
                sync_engine.apply_report(
                    "token",
                    "database",
                    report,
                    False,
                    run_id="run-preflight",
                )

        apply_item.assert_not_called()

    def test_same_page_manual_edit_after_preflight_causes_zero_mutation(self):
        item = {
            "notice_id": "1",
            "title": "공지 1",
            "url": "https://www.sogang.ac.kr/ko/detail/1?bbsConfigFk=2",
            "classification": "학사공지",
            "top": False,
            "body_blocks": [],
        }
        report = CrawlReport(
            sources=[source_result("2", SourceStatus.SUCCESS, [item])]
        )
        initial = managed_page("page-1", "2", "1")
        initial["last_edited_time"] = "2026-07-27T00:00:00.000Z"
        initial["properties"][sync_engine.BODY_HASH_PROPERTY] = (
            rich_text_property(sync_engine.compute_body_hash([]))
        )
        edited = copy.deepcopy(initial)
        edited["last_edited_time"] = "2026-07-27T00:01:00.000Z"
        context = sync_engine.DestinationContext("token", "database")

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=context,
            ),
            patch.object(
                sync_engine,
                "find_existing_page",
                side_effect=[initial, initial],
            ),
            patch.object(
                sync_engine,
                "retrieve_page",
                return_value=edited,
            ),
            patch.object(
                sync_engine,
                "destination_quote_fingerprint",
                return_value="quote-state",
            ),
            patch.object(sync_engine, "apply_item") as apply_item,
            patch.object(sync_engine, "create_page") as create,
            patch.object(sync_engine, "update_page") as update,
            self.assertRaisesRegex(RuntimeError, "적용 직전 대상"),
        ):
            sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                run_id="run-manual-edit",
            )

        apply_item.assert_not_called()
        create.assert_not_called()
        update.assert_not_called()

    def test_upload_then_existing_page_edit_stops_before_first_write(self):
        body_blocks = [paragraph_block("업로드 본문")]
        item = {
            "notice_id": "1",
            "title": "공지 1",
            "url": (
                "https://www.sogang.ac.kr/ko/detail/1"
                "?bbsConfigFk=2"
            ),
            "classification": "학사공지",
            "top": False,
            "body_status": "present",
            "body_blocks": body_blocks,
        }
        report = CrawlReport(
            sources=[source_result("2", SourceStatus.SUCCESS, [item])]
        )
        initial = managed_page("page-1", "2", "1")
        initial["last_edited_time"] = "2026-07-27T00:00:00.000Z"
        edited = copy.deepcopy(initial)
        edited["last_edited_time"] = "2026-07-27T00:01:00.000Z"
        upload_finished = False

        def find_page(*_args, **_kwargs):
            return edited if upload_finished else initial

        def prepare_body(*_args, **_kwargs):
            nonlocal upload_finished
            upload_finished = True
            return body_blocks, body_blocks, []

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=sync_engine.DestinationContext(
                    "token",
                    "database",
                    has_attachments_property=False,
                ),
            ),
            patch.object(
                sync_engine,
                "find_existing_page",
                side_effect=find_page,
            ),
            patch.object(
                sync_engine,
                "retrieve_page",
                return_value=initial,
            ),
            patch.object(
                sync_engine,
                "should_upload_files_to_notion",
                return_value=True,
            ),
            patch.object(
                sync_engine,
                "collect_body_media_content_state",
                return_value=[],
            ),
            patch.object(
                sync_engine,
                "shrink_candidate_for_item",
                return_value=None,
            ),
            patch.object(
                sync_engine,
                "inspect_existing_uploaded_media_blocks",
                return_value=({}, "valid"),
            ),
            patch.object(
                sync_engine,
                "prepare_body_blocks_for_sync",
                side_effect=prepare_body,
            ) as upload,
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
            patch.object(
                sync_engine,
                "destination_quote_fingerprint",
                return_value="quote-state",
            ),
            patch.object(sync_engine, "create_page") as create,
            patch.object(sync_engine, "update_page") as update,
            patch.object(
                sync_engine,
                "sync_page_body_blocks",
            ) as replace_body,
            self.assertRaisesRegex(
                RuntimeError,
                "적용 직전 대상",
            ),
        ):
            sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                run_id="run-upload-race",
            )

        upload.assert_called_once()
        create.assert_not_called()
        update.assert_not_called()
        replace_body.assert_not_called()

    def test_upload_then_new_identity_appearance_stops_before_create(self):
        body_blocks = [paragraph_block("신규 업로드 본문")]
        item = {
            "notice_id": "1",
            "title": "공지 1",
            "url": (
                "https://www.sogang.ac.kr/ko/detail/1"
                "?bbsConfigFk=2"
            ),
            "classification": "학사공지",
            "top": False,
            "body_status": "present",
            "body_blocks": body_blocks,
        }
        report = CrawlReport(
            sources=[source_result("2", SourceStatus.SUCCESS, [item])]
        )
        appeared = managed_page("page-1", "2", "1")
        upload_finished = False

        def find_page(*_args, **_kwargs):
            return appeared if upload_finished else None

        def prepare_body(*_args, **_kwargs):
            nonlocal upload_finished
            upload_finished = True
            return body_blocks, body_blocks, []

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=sync_engine.DestinationContext(
                    "token",
                    "database",
                    has_attachments_property=False,
                ),
            ),
            patch.object(
                sync_engine,
                "find_existing_page",
                side_effect=find_page,
            ),
            patch.object(
                sync_engine,
                "should_upload_files_to_notion",
                return_value=True,
            ),
            patch.object(
                sync_engine,
                "collect_body_media_content_state",
                return_value=[],
            ),
            patch.object(
                sync_engine,
                "prepare_body_blocks_for_sync",
                side_effect=prepare_body,
            ) as upload,
            patch.object(sync_engine, "create_page") as create,
            patch.object(sync_engine, "update_page") as update,
            patch.object(
                sync_engine,
                "sync_page_body_blocks",
            ) as replace_body,
            self.assertRaisesRegex(
                RuntimeError,
                "적용 직전 대상",
            ),
        ):
            sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                run_id="run-create-race",
            )

        upload.assert_called_once()
        create.assert_not_called()
        update.assert_not_called()
        replace_body.assert_not_called()

    def test_existing_top_is_not_disabled_by_general_item_update(self):
        item = {
            "source_id": "2",
            "notice_id": "1",
            "title": "공지 1",
            "url": "https://www.sogang.ac.kr/ko/detail/1?bbsConfigFk=2",
            "classification": "학사공지",
            "type": "학사",
            "top": False,
            "body_blocks": [],
        }
        existing = managed_page("page-1", "2", "1")
        context = sync_engine.DestinationContext(
            "token",
            "database",
            has_views_property=False,
            has_attachments_property=False,
            has_classification_property=True,
        )
        counters = SyncCounters()

        with (
            patch.object(sync_engine, "check_run_control"),
            patch.object(sync_engine, "verify_committed_item"),
            patch.object(sync_engine, "update_page") as update,
        ):
            sync_engine.apply_item(
                context,
                item,
                counters,
                existing_page=existing,
                existing_page_resolved=True,
            )

        self.assertGreater(update.call_count, 0)
        self.assertTrue(
            all(
                sync.TOP_PROPERTY not in call.args[2]
                for call in update.call_args_list
            )
        )

    def test_top_reconcile_uses_observed_top_urls_and_two_runs(self):
        result = source_result("2", SourceStatus.SUCCESS, [])
        result.top_snapshot_verified = True
        result.top_urls = [
            "https://www.sogang.ac.kr/ko/detail/1001?bbsConfigFk=2"
        ]
        report = CrawlReport(sources=[result])
        candidate = managed_page("page-999", "2", "999")
        context = sync_engine.DestinationContext("token", "database")
        now = datetime.now(timezone.utc).isoformat()
        state = {
            "runs": [{"run_id": "run-1"}],
            "sources": {
                "2": {
                    "top_absence_counts": {"999": 1},
                    "top_absence_last_run_id": "run-1",
                    "top_absence_last_observed_at": now,
                }
            },
        }

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=context,
            ),
            patch.object(
                sync_engine,
                "inspect_missing_top",
                return_value=([candidate], [candidate]),
            ) as plan_missing,
            patch.object(
                sync_engine,
                "disable_missing_top",
                return_value=1,
            ) as disable,
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
        ):
            counters = sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                previous_state=state,
                run_id="run-2",
            )

        self.assertEqual(counters.top_disabled, 1)
        self.assertEqual(
            plan_missing.call_args_list[0].args[3],
            {"1001"},
        )
        self.assertEqual(
            disable.call_args.kwargs["eligible_notice_ids"],
            {"999"},
        )

    def test_first_mass_top_absence_is_observed_without_disable(self):
        result = source_result("2", SourceStatus.SUCCESS, [])
        result.top_snapshot_verified = True
        report = CrawlReport(sources=[result])
        candidates = [
            managed_page(f"page-{value}", "2", str(value))
            for value in range(1, 6)
        ]
        context = sync_engine.DestinationContext("token", "database")

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=context,
            ),
            patch.object(
                sync_engine,
                "inspect_missing_top",
                return_value=(candidates, candidates),
            ),
            patch.object(
                sync_engine,
                "disable_missing_top",
                return_value=0,
            ) as disable,
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
        ):
            counters = sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                run_id="run-first-absence",
            )

        self.assertEqual(
            counters.top_absence_observations["2"],
            ["1", "2", "3", "4", "5"],
        )
        self.assertEqual(
            disable.call_args.kwargs["eligible_notice_ids"],
            set(),
        )

    def test_shrink_candidate_requires_next_consecutive_run(self):
        attachment = {
            "name": "keep.pdf",
            "type": "external",
            "external": {
                "url": "https://www.sogang.ac.kr/files/keep.pdf"
            },
        }
        item = {
            "notice_id": "200",
            "title": "공지 200",
            "url": "https://www.sogang.ac.kr/ko/detail/200?bbsConfigFk=2",
            "classification": "학사공지",
            "top": False,
            "body_blocks": [],
            "attachments": [attachment],
        }
        report = CrawlReport(
            sources=[source_result("2", SourceStatus.SUCCESS, [item])]
        )
        empty_hash = sync_engine.compute_body_hash([])
        existing = managed_page("page-200", "2", "200")
        existing["properties"][sync.ATTACHMENT_PROPERTY] = {
            "type": "files",
            "files": [
                attachment,
                {
                    "name": "remove.pdf",
                    "type": "external",
                    "external": {
                        "url": (
                            "https://www.sogang.ac.kr/files/remove.pdf"
                        )
                    },
                },
            ],
        }
        existing["properties"][sync_engine.BODY_HASH_PROPERTY] = (
            rich_text_property(empty_hash)
        )
        context = sync_engine.DestinationContext("token", "database")

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=context,
            ),
            patch.object(
                sync_engine,
                "find_existing_page",
                return_value=existing,
            ),
            patch.object(
                sync_engine,
                "retrieve_page",
                return_value=existing,
            ),
            patch.object(
                sync_engine,
                "destination_quote_fingerprint",
                return_value="quote-state",
            ),
            patch.object(sync_engine, "apply_item") as apply_item,
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
        ):
            first = sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                run_id="run-1",
            )

        apply_item.assert_not_called()
        candidate = first.shrink_candidate_observations["2:200"]
        state = {
            "runs": [{"run_id": "run-1"}],
            "sources": {},
            "shrink_candidates": {
                "2:200": {
                    **candidate,
                    "observations": 1,
                    "last_observed_run_id": "run-1",
                    "last_observed_at": (
                        datetime.now(timezone.utc).isoformat()
                    ),
                }
            },
        }
        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=context,
            ),
            patch.object(
                sync_engine,
                "find_existing_page",
                return_value=existing,
            ),
            patch.object(
                sync_engine,
                "retrieve_page",
                return_value=existing,
            ),
            patch.object(
                sync_engine,
                "destination_quote_fingerprint",
                return_value="quote-state",
            ),
            patch.object(sync_engine, "apply_item") as apply_item,
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
        ):
            second = sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                previous_state=state,
                run_id="run-2",
            )

        apply_item.assert_called_once()
        self.assertIn("2:200", second.shrink_candidate_clears)

    def test_same_count_attachment_replacement_is_quarantined(self):
        existing = managed_page("page-201", "2", "201")
        existing["properties"][sync.ATTACHMENT_PROPERTY] = {
            "type": "files",
            "files": [
                {
                    "name": "notice.pdf",
                    "type": "external",
                    "external": {
                        "url": "https://www.sogang.ac.kr/files/original.pdf"
                    },
                }
            ],
        }
        item = {
            "source_id": "2",
            "notice_id": "201",
            "attachments": [
                {
                    "name": "notice.pdf",
                    "type": "external",
                    "external": {
                        "url": "https://www.sogang.ac.kr/files/replaced.pdf"
                    },
                }
            ],
        }

        candidate = sync_engine.shrink_candidate_for_item(
            "token",
            item,
            existing,
        )

        self.assertIsNotNone(candidate)
        self.assertIn(
            "attachment_identity_changed",
            candidate["reasons"],
        )

    def test_stale_attachment_state_cannot_hide_manual_file_replacement(self):
        existing = managed_page("page-201-state", "2", "201-state")
        existing["properties"][sync.ATTACHMENT_PROPERTY] = {
            "type": "files",
            "files": [
                {
                    "name": "notice.jpg",
                    "type": "file",
                    "file": {
                        "url": (
                            "https://notionusercontent.com/current/manual"
                            "?signature=current"
                        )
                    },
                }
            ],
        }
        existing["properties"][sync.ATTACHMENT_STATE_PROPERTY] = (
            rich_text_property(
                json.dumps(
                    [
                        {
                            "source_url": (
                                "https://www.sogang.ac.kr/files/original.jpg"
                                "?fileId=77"
                            ),
                            "name": "notice.jpg",
                            "upload_id": "upload-original",
                            "hosted_file_key": (
                                "notionusercontent.com/original/upload"
                            ),
                        }
                    ]
                )
            )
        )
        item = {
            "source_id": "2",
            "notice_id": "201-state",
            "attachments": [
                {
                    "name": "notice.jpg",
                    "type": "external",
                    "external": {
                        "url": (
                            "https://www.sogang.ac.kr/files/original.jpg"
                            "?fileId=77"
                        )
                    },
                }
            ],
        }

        candidate = sync_engine.shrink_candidate_for_item(
            "token",
            item,
            existing,
        )

        self.assertIsNotNone(candidate)
        self.assertIn(
            "attachment_identity_changed",
            candidate["reasons"],
        )

    def test_uploaded_attachment_state_without_hosted_key_is_unverified(self):
        existing = managed_page("page-201-unverified", "2", "201-unverified")
        existing["properties"][sync.ATTACHMENT_PROPERTY] = {
            "type": "files",
            "files": [
                {
                    "name": "notice.jpg",
                    "type": "file",
                    "file": {
                        "url": (
                            "https://notionusercontent.com/current/file"
                            "?signature=current"
                        )
                    },
                }
            ],
        }
        existing["properties"][sync.ATTACHMENT_STATE_PROPERTY] = (
            rich_text_property(
                json.dumps(
                    [
                        {
                            "source_url": (
                                "https://www.sogang.ac.kr/files/original.jpg"
                                "?fileId=77"
                            ),
                            "name": "notice.jpg",
                            "upload_id": "upload-original",
                        }
                    ]
                )
            )
        )
        item = {
            "source_id": "2",
            "notice_id": "201-unverified",
            "attachments": [
                {
                    "name": "notice.jpg",
                    "type": "external",
                    "external": {
                        "url": (
                            "https://www.sogang.ac.kr/files/original.jpg"
                            "?fileId=77"
                        )
                    },
                }
            ],
        }

        candidate = sync_engine.shrink_candidate_for_item(
            "token",
            item,
            existing,
        )

        self.assertIsNotNone(candidate)
        self.assertIn(
            "attachment_identity_unverified",
            candidate["reasons"],
        )

    def test_same_length_body_replacement_is_quarantined(self):
        existing = managed_page("page-202", "2", "202")
        original = [paragraph_block("정상본문")]
        replacement = [paragraph_block("오염본문")]
        existing["properties"][sync_engine.BODY_HASH_PROPERTY] = (
            rich_text_property(
                sync_engine.compute_body_hash(original)
            )
        )
        item = {
            "source_id": "2",
            "notice_id": "202",
            "body_status": "present",
            "body_blocks": replacement,
        }

        candidate = sync_engine.shrink_candidate_for_item(
            "token",
            item,
            existing,
        )

        self.assertIsNotNone(candidate)
        self.assertIn("body_hash_changed", candidate["reasons"])

    def test_malformed_existing_attachment_property_fails_closed(self):
        existing = managed_page("page-203", "2", "203")
        existing["properties"][sync.ATTACHMENT_PROPERTY] = {
            "type": "files",
            "files": {"unexpected": "shape"},
        }
        item = {
            "source_id": "2",
            "notice_id": "203",
            "attachments": [],
        }

        with self.assertRaisesRegex(RuntimeError, "Notion 첨부 속성"):
            sync_engine.shrink_candidate_for_item(
                "token",
                item,
                existing,
            )

    def test_rotating_attachment_signature_keeps_stable_identity(self):
        existing = managed_page("page-204", "2", "204")
        existing["properties"][sync.ATTACHMENT_PROPERTY] = {
            "type": "files",
            "files": [
                {
                    "name": "notice.pdf",
                    "type": "external",
                    "external": {
                        "url": (
                            "https://www.sogang.ac.kr/files/download"
                            "?fileId=77&signature=old"
                        )
                    },
                }
            ],
        }
        item = {
            "source_id": "2",
            "notice_id": "204",
            "attachments": [
                {
                    "name": "notice.pdf",
                    "type": "external",
                    "external": {
                        "url": (
                            "https://www.sogang.ac.kr/files/download"
                            "?fileId=77&signature=new"
                        )
                    },
                }
            ],
        }

        self.assertIsNone(
            sync_engine.shrink_candidate_for_item(
                "token",
                item,
                existing,
            )
        )

    def test_stable_attachment_id_change_is_quarantined(self):
        existing = managed_page("page-205", "2", "205")
        existing["properties"][sync.ATTACHMENT_PROPERTY] = {
            "type": "files",
            "files": [
                {
                    "name": "notice.pdf",
                    "type": "external",
                    "external": {
                        "url": (
                            "https://www.sogang.ac.kr/files/download"
                            "?fileId=77&signature=old"
                        )
                    },
                }
            ],
        }
        item = {
            "source_id": "2",
            "notice_id": "205",
            "attachments": [
                {
                    "name": "notice.pdf",
                    "type": "external",
                    "external": {
                        "url": (
                            "https://www.sogang.ac.kr/files/download"
                            "?fileId=78&signature=new"
                        )
                    },
                }
            ],
        }

        candidate = sync_engine.shrink_candidate_for_item(
            "token",
            item,
            existing,
        )

        self.assertIsNotNone(candidate)
        self.assertIn(
            "attachment_identity_changed",
            candidate["reasons"],
        )

    def test_item_commit_readback_rejects_trashed_page(self):
        item = {
            "source_id": "2",
            "notice_id": "200",
            "title": "공지 200",
            "url": "https://www.sogang.ac.kr/ko/detail/200",
            "top": False,
        }
        page = managed_page("page-200", "2", "200")
        page["in_trash"] = True

        with patch.object(
            sync_engine,
            "build_properties",
            return_value={},
        ):
            reasons = sync_engine.committed_item_readback_reasons(
                "token",
                page,
                item,
                sync_engine.DestinationContext("token", "database"),
                "page-200",
                "operation",
                "generation",
                "",
                [],
                [],
                False,
            )

        self.assertIn("in_trash", reasons)

    def test_top_commit_readback_rejects_trashed_page(self):
        page = managed_page("page-200", "2", "200")
        page["properties"][sync.TOP_PROPERTY] = {
            "type": "checkbox",
            "checkbox": False,
        }
        page["in_trash"] = True

        with (
            patch.object(sync, "retrieve_page", return_value=page),
            patch.object(
                sync,
                "TOP_COMMIT_READBACK_DELAYS",
                (0.0,),
            ),
            self.assertRaisesRegex(RuntimeError, "in_trash"),
        ):
            sync.verify_top_disabled(
                "token",
                "page-200",
                "2",
                "200",
            )

    def test_final_pending_scan_rejects_unknown_source(self):
        report = CrawlReport(
            sources=[source_result("2", SourceStatus.SUCCESS, [])]
        )
        late_pending = managed_page("late", "999", "1")

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=sync_engine.DestinationContext(
                    "token",
                    "database",
                ),
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[late_pending],
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "설정에 없는 출처",
            ),
        ):
            sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                run_id="100:1",
                logical_run_id="100",
            )

    def test_pending_disappearance_without_commit_readback_fails(self):
        report = CrawlReport(
            sources=[source_result("2", SourceStatus.SUCCESS, [])]
        )
        context = sync_engine.DestinationContext(
            "token",
            "database",
            pending_page_ids=("pending",),
            pending_page_sources={"pending": "2"},
            pending_page_notices={"pending": "1"},
        )

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=context,
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "최종 상태 재확인 없이",
            ),
        ):
            sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                run_id="100:1",
                logical_run_id="100",
            )

    def test_state_pending_notice_forces_commit_readback_before_clear(self):
        item = {
            "notice_id": "200",
            "title": "공지 200",
            "url": "https://www.sogang.ac.kr/ko/detail/200",
            "top": False,
            "body_blocks": [],
        }
        result = source_result("2", SourceStatus.SUCCESS, [item])
        report = CrawlReport(sources=[result])
        prepared = sync_engine.prepare_source_items(result)[0]
        existing = managed_page("page-200", "2", "200")
        preflight = sync_engine.DestinationPreflight(
            item=prepared,
            existing_page=existing,
            operation_id="operation",
            shrink_key="2:200",
            shrink_candidate=None,
        )
        state = {
            "sources": {
                "2": {"pending_notice_ids": ["200"]}
            }
        }

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=sync_engine.DestinationContext(
                    "token",
                    "database",
                ),
            ),
            patch.object(
                sync_engine,
                "resolve_destination_preflight",
                return_value=[preflight],
            ),
            patch.object(
                sync_engine,
                "validate_destination_preflight_entries",
                return_value=None,
            ),
            patch.object(
                sync_engine,
                "validate_destination_preflight_entry",
                return_value=existing,
            ),
            patch.object(
                sync_engine,
                "retrieve_page",
                return_value=existing,
            ),
            patch.object(
                sync_engine,
                "apply_item",
            ) as apply_item,
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
        ):
            counters = sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                previous_state=state,
                run_id="100:1",
                logical_run_id="100",
            )

        self.assertTrue(
            apply_item.call_args.kwargs["force_commit_readback"]
        )
        self.assertEqual(
            counters.recovered_pending_notices,
            {"2": ["200"]},
        )

    def test_top_plan_is_built_after_item_commit(self):
        item = {
            "notice_id": "200",
            "title": "공지 200",
            "url": "https://www.sogang.ac.kr/ko/detail/200",
            "top": True,
            "body_blocks": [],
        }
        result = source_result("2", SourceStatus.SUCCESS, [item])
        result.top_snapshot_verified = True
        report = CrawlReport(sources=[result])
        prepared = sync_engine.prepare_source_items(result)[0]
        preflight = sync_engine.DestinationPreflight(
            item=prepared,
            existing_page=None,
            operation_id="operation",
            shrink_key="2:200",
            shrink_candidate=None,
        )
        events: list[str] = []

        def inspect_top(*_args):
            self.assertTrue(events)
            self.assertEqual(events[0], "item")
            events.append("top")
            return [], []

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=sync_engine.DestinationContext(
                    "token",
                    "database",
                ),
            ),
            patch.object(
                sync_engine,
                "resolve_destination_preflight",
                return_value=[preflight],
            ),
            patch.object(
                sync_engine,
                "validate_destination_preflight_entries",
                return_value=None,
            ),
            patch.object(
                sync_engine,
                "validate_destination_preflight_entry",
                return_value=None,
            ),
            patch.object(
                sync_engine,
                "apply_item",
                side_effect=lambda *_args, **_kwargs: events.append(
                    "item"
                ),
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
            patch.object(
                sync_engine,
                "inspect_missing_top",
                side_effect=inspect_top,
            ),
            patch.object(
                sync_engine,
                "disable_missing_top",
                return_value=0,
            ),
        ):
            sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                run_id="100:1",
                logical_run_id="100",
            )

        self.assertEqual(events, ["item", "top", "top"])

    def test_top_double_read_blocks_external_drift(self):
        result = source_result("2", SourceStatus.SUCCESS, [])
        result.top_snapshot_verified = True
        report = CrawlReport(sources=[result])
        first = managed_page("page-1", "2", "1")
        second = copy.deepcopy(first)
        first["last_edited_time"] = "2026-07-27T00:00:00Z"
        second["last_edited_time"] = "2026-07-27T00:01:00Z"

        with (
            patch.object(
                sync_engine,
                "prepare_destination",
                return_value=sync_engine.DestinationContext(
                    "token",
                    "database",
                ),
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
            patch.object(
                sync_engine,
                "inspect_missing_top",
                side_effect=[
                    ([first], [first]),
                    ([second], [second]),
                ],
            ),
            patch.object(
                sync_engine,
                "disable_missing_top",
            ) as disable,
            self.assertRaisesRegex(RuntimeError, "연속 검증"),
        ):
            sync_engine.apply_report(
                "token",
                "database",
                report,
                False,
                run_id="100:1",
                logical_run_id="100",
            )

        disable.assert_not_called()

    def test_same_logical_run_attempt_cannot_confirm_destructive_gate(self):
        now = datetime.now(timezone.utc).isoformat()
        state = {
            "runs": [
                {
                    "run_id": "100",
                    "run_attempt": "1",
                    "execution_id": "100:1",
                }
            ]
        }
        observation = {
            "last_observed_run_id": "100:1",
            "last_observed_logical_run_id": "100",
            "last_observed_at": now,
        }

        self.assertFalse(
            sync_engine.recent_consecutive_observation(
                state,
                observation,
                "last_observed_run_id",
                "last_observed_at",
                current_logical_run_id="100",
                logical_run_id_key=(
                    "last_observed_logical_run_id"
                ),
            )
        )
        self.assertTrue(
            sync_engine.recent_consecutive_observation(
                state,
                observation,
                "last_observed_run_id",
                "last_observed_at",
                current_logical_run_id="101",
                logical_run_id_key=(
                    "last_observed_logical_run_id"
                ),
            )
        )

    def test_dry_run_detects_stale_generation(self):
        body_blocks = [paragraph_block("본문")]
        item = {
            "notice_id": "200",
            "title": "공지 200",
            "url": "https://www.sogang.ac.kr/ko/detail/200",
            "top": False,
            "body_blocks": body_blocks,
        }
        result = source_result("2", SourceStatus.SUCCESS, [item])
        report = CrawlReport(sources=[result])
        body_hash = sync_engine.compute_body_hash(
            sync_engine.normalize_body_blocks_for_hash(
                body_blocks,
                sync_engine.should_upload_files_to_notion(),
            )
        )
        existing = managed_page("page-200", "2", "200")
        existing["properties"][sync_engine.BODY_HASH_PROPERTY] = (
            rich_text_property(body_hash)
        )
        existing["properties"][sync_engine.SYNC_GENERATION_PROPERTY] = (
            rich_text_property("stale")
        )

        with (
            patch.object(
                sync_engine,
                "fetch_database",
                return_value={
                    "properties": complete_destination_schema()
                },
            ),
            patch.object(
                sync_engine,
                "find_existing_page",
                return_value=existing,
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
            patch.object(
                sync_engine,
                "is_body_generation_current",
                return_value=False,
            ),
            patch.object(
                sync_engine,
                "build_properties",
                return_value={},
            ),
            patch.object(
                sync_engine,
                "destination_quote_fingerprint",
                return_value="quote-state",
            ),
        ):
            plan = sync_engine.build_dry_run_plan(
                "100:1",
                report,
                "token",
                "database",
                logical_run_id="100",
            )

        replacements = [
            action
            for action in plan.actions
            if action.kind == MutationKind.REPLACE_BODY
        ]
        self.assertEqual(len(replacements), 1)
        self.assertEqual(
            replacements[0].reason,
            "body_hash_changed",
        )

    def test_dry_run_final_pending_scan_rejects_unknown_source(self):
        report = CrawlReport(
            sources=[source_result("2", SourceStatus.SUCCESS, [])]
        )
        late_pending = managed_page("late", "999", "1")

        with (
            patch.object(
                sync_engine,
                "fetch_database",
                return_value={
                    "properties": complete_destination_schema()
                },
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                side_effect=[[], [late_pending]],
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "설정에 없는 출처",
            ),
        ):
            sync_engine.build_dry_run_plan(
                "100:1",
                report,
                "token",
                "database",
                logical_run_id="100",
            )

    def test_dry_run_top_double_read_blocks_external_drift(self):
        result = source_result("2", SourceStatus.SUCCESS, [])
        result.top_snapshot_verified = True
        report = CrawlReport(sources=[result])
        first = managed_page("page-1", "2", "1")
        second = copy.deepcopy(first)
        first["last_edited_time"] = "2026-07-27T00:00:00Z"
        second["last_edited_time"] = "2026-07-27T00:01:00Z"

        with (
            patch.object(
                sync_engine,
                "fetch_database",
                return_value={
                    "properties": complete_destination_schema()
                },
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
            patch.object(
                sync_engine,
                "inspect_missing_top",
                side_effect=[
                    ([first], [first]),
                    ([second], [second]),
                ],
            ),
            self.assertRaisesRegex(RuntimeError, "연속 검증"),
        ):
            sync_engine.build_dry_run_plan(
                "100:1",
                report,
                "token",
                "database",
                logical_run_id="100",
            )

    def test_dry_run_detects_stale_attachment_state_generation(self):
        item = {
            "notice_id": "200",
            "title": "공지 200",
            "url": "https://www.sogang.ac.kr/ko/detail/200",
            "top": False,
            "body_status": "confirmed_empty",
            "body_blocks": [],
            "attachments": [
                {
                    "name": "image.png",
                    "type": "external",
                    "external": {
                        "url": "https://www.sogang.ac.kr/image.png"
                    },
                }
            ],
        }
        result = source_result("2", SourceStatus.SUCCESS, [item])
        report = CrawlReport(sources=[result])
        prepared = sync_engine.prepare_source_items(result)[0]
        content_state = [
            {
                "source_url": "https://www.sogang.ac.kr/image.png",
                "name": "image.png",
                "occurrence": 1,
                "content_sha256": "a" * 64,
            }
        ]
        attachment_state = [
            {
                **content_state[0],
                "upload_id": "upload",
                "hosted_file_key": "hosted",
                "generation_id": "stale",
            }
        ]
        existing = managed_page("page-200", "2", "200")
        existing["properties"][sync_engine.BODY_HASH_PROPERTY] = (
            rich_text_property(sync_engine.compute_body_hash([]))
        )
        existing["properties"][sync_engine.ATTACHMENT_PROPERTY] = {
            "type": "files",
            "files": [
                {
                    "name": "image.png",
                    "type": "file",
                    "file": {"url": "https://notion.so/hosted"},
                }
            ],
        }
        existing["properties"][sync_engine.ATTACHMENT_STATE_PROPERTY] = (
            rich_text_property(
                json.dumps(
                    attachment_state,
                    separators=(",", ":"),
                )
            )
        )
        preflight = sync_engine.DestinationPreflight(
            item=prepared,
            existing_page=existing,
            operation_id="operation",
            shrink_key="2:200",
            shrink_candidate=None,
            attachment_content_state=content_state,
        )

        with (
            patch.object(
                sync_engine,
                "fetch_database",
                return_value={
                    "properties": complete_destination_schema()
                },
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
            patch.object(
                sync_engine,
                "resolve_destination_preflight",
                return_value=[preflight],
            ),
            patch.object(
                sync_engine,
                "build_properties",
                return_value={},
            ),
            patch.object(
                sync_engine,
                "is_empty_body_generation_current",
                return_value=True,
            ),
            patch.object(
                sync_engine,
                "extract_existing_uploaded_attachment_ids",
                return_value={"image": [{"upload_id": "upload"}]},
            ),
            patch.object(
                sync_engine,
                "should_upload_files_to_notion",
                return_value=True,
            ),
        ):
            plan = sync_engine.build_dry_run_plan(
                "100:1",
                report,
                "token",
                "database",
                logical_run_id="100",
            )

        self.assertEqual(
            plan.actions[0].kind,
            MutationKind.UPDATE_PROPERTIES,
        )
        self.assertIn(
            sync_engine.ATTACHMENT_STATE_PROPERTY,
            plan.actions[0].reason,
        )

    def test_dry_run_detects_stale_body_media_generation(self):
        body_blocks = [
            {
                "type": "image",
                "image": {
                    "type": "external",
                    "external": {
                        "url": "https://www.sogang.ac.kr/body.png"
                    },
                },
            }
        ]
        item = {
            "notice_id": "200",
            "title": "공지 200",
            "url": "https://www.sogang.ac.kr/ko/detail/200",
            "top": False,
            "body_status": "present",
            "body_blocks": body_blocks,
        }
        result = source_result("2", SourceStatus.SUCCESS, [item])
        report = CrawlReport(sources=[result])
        prepared = sync_engine.prepare_source_items(result)[0]
        content_state = [
            {
                "type": "image",
                "source_url": "https://www.sogang.ac.kr/body.png",
                "content_sha256": "b" * 64,
            }
        ]
        media_state = [
            {
                **content_state[0],
                "upload_id": "upload",
                "block_id": "block",
                "hosted_file_key": "hosted",
                "generation_id": "stale",
            }
        ]
        body_hash = sync_engine.compute_body_hash(
            sync_engine.normalize_body_blocks_for_hash(
                body_blocks,
                True,
                media_content_state=content_state,
            ),
            image_mode=sync_engine.BODY_HASH_IMAGE_MODE_UPLOAD,
        )
        existing = managed_page("page-200", "2", "200")
        existing["properties"][sync_engine.BODY_HASH_PROPERTY] = (
            rich_text_property(body_hash)
        )
        existing["properties"][sync_engine.BODY_MEDIA_STATE_PROPERTY] = (
            rich_text_property(
                json.dumps(media_state, separators=(",", ":"))
            )
        )
        preflight = sync_engine.DestinationPreflight(
            item=prepared,
            existing_page=existing,
            operation_id="operation",
            shrink_key="2:200",
            shrink_candidate=None,
            body_media_content_state=content_state,
        )

        with (
            patch.object(
                sync_engine,
                "fetch_database",
                return_value={
                    "properties": complete_destination_schema()
                },
            ),
            patch.object(
                sync_engine,
                "inspect_pending_pages",
                return_value=[],
            ),
            patch.object(
                sync_engine,
                "resolve_destination_preflight",
                return_value=[preflight],
            ),
            patch.object(
                sync_engine,
                "build_properties",
                return_value={},
            ),
            patch.object(
                sync_engine,
                "is_body_generation_current",
                return_value=True,
            ),
            patch.object(
                sync_engine,
                "should_upload_files_to_notion",
                return_value=True,
            ),
        ):
            plan = sync_engine.build_dry_run_plan(
                "100:1",
                report,
                "token",
                "database",
                logical_run_id="100",
            )

        self.assertEqual(
            plan.actions[0].kind,
            MutationKind.UPDATE_PROPERTIES,
        )
        self.assertIn(
            sync_engine.BODY_MEDIA_STATE_PROPERTY,
            plan.actions[0].reason,
        )


if __name__ == "__main__":
    unittest.main()
