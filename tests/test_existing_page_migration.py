import copy
import io
import json
import sys
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import migrate_existing_pages as migration
import sync


def rich_text_property(value: str = "") -> dict:
    return {
        "type": "rich_text",
        "rich_text": (
            [{"type": "text", "text": {"content": value}, "plain_text": value}]
            if value
            else []
        ),
    }


def make_page(
    page_id: str = "page-1",
    source_id: str = "141",
    notice_id: str = "1001",
    data_source_id: str = "data-source",
) -> dict:
    return {
        "object": "page",
        "id": page_id,
        "created_time": "2026-01-01T00:00:00.000Z",
        "last_edited_time": "2026-01-01T00:00:00.000Z",
        "created_by": {"id": "user"},
        "last_edited_by": {"id": "user"},
        "in_trash": False,
        "icon": {"type": "emoji", "emoji": "📣"},
        "cover": None,
        "parent": {
            "type": "data_source_id",
            "data_source_id": data_source_id,
        },
        "properties": {
            sync.TITLE_PROPERTY: {
                "type": "title",
                "title": [{"plain_text": f"공지 {notice_id}"}],
            },
            sync.TOP_PROPERTY: {"type": "checkbox", "checkbox": True},
            sync.URL_PROPERTY: {
                "type": "url",
                "url": (
                    f"https://www.sogang.ac.kr/ko/detail/{notice_id}"
                    f"?bbsConfigFk={source_id}"
                ),
            },
            sync.SYNC_OWNER_PROPERTY: rich_text_property(),
            sync.SOURCE_KEY_PROPERTY: rich_text_property(),
            sync.NOTICE_ID_PROPERTY: rich_text_property(),
            sync.SYNC_GENERATION_PROPERTY: rich_text_property(),
            sync.SYNC_STATUS_PROPERTY: rich_text_property(),
            sync.SYNC_OPERATION_PROPERTY: rich_text_property(),
        },
        "url": f"https://www.notion.so/{page_id}",
        "public_url": None,
    }


def paragraph(block_id: str, text: str) -> dict:
    return {
        "id": block_id,
        "type": "paragraph",
        "has_children": False,
        "paragraph": {
            "rich_text": [{"plain_text": text}],
            "color": "default",
        },
    }


def quote(
    block_id: str = "quote-1",
    marker: str = "legacy",
) -> dict:
    content = (
        f"{sync.LEGACY_SYNC_CONTAINER_MARKER}\n기존 본문"
        if marker == "legacy"
        else "기존 본문"
    )
    return {
        "id": block_id,
        "type": "quote",
        "has_children": True,
        "quote": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {"content": content},
                    "plain_text": content,
                }
            ],
            "color": "default",
        },
    }


class NotionStore:
    def __init__(self, *, with_quote: bool = True) -> None:
        self.data_source_id = "data-source"
        self.pages = {"page-1": make_page()}
        self.roots = {
            "page-1": [
                paragraph("manual-1", "수동 메모"),
                *([quote()] if with_quote else []),
                paragraph("manual-2", "수동 꼬리말"),
            ]
        }
        self.children = {
            "quote-1": [
                paragraph("body-1", "본문 문단"),
                paragraph("body-2", "첨부 설명"),
            ]
        }
        self.patch_payloads: list[dict] = []
        self.corrupt_after_patch = False
        self.before_patch = None
        self.schema_properties = {
            name: {
                "id": f"schema-{index}",
                "name": name,
                "type": "rich_text",
                "rich_text": {},
            }
            for index, name in enumerate(migration.SYNC_PROPERTY_NAMES)
        }

    def query(
        self,
        method: str,
        url: str,
        token: str,
        payload: dict | None = None,
    ) -> dict:
        self.assert_token(token)
        if method == "GET" and "/data_sources/" in url:
            return {"properties": copy.deepcopy(self.schema_properties)}
        if method == "POST" and "/data_sources/" in url:
            return {
                "results": copy.deepcopy(list(self.pages.values())),
                "has_more": False,
                "next_cursor": None,
            }
        if method == "PATCH" and "/pages/" in url:
            page_id = url.rsplit("/", 1)[-1]
            if self.before_patch is not None:
                callback = self.before_patch
                self.before_patch = None
                callback()
            if not isinstance(payload, dict):
                raise AssertionError("PATCH 본문이 없습니다")
            if set(payload) != {"properties"}:
                raise AssertionError("properties-only PATCH가 아닙니다")
            properties = copy.deepcopy(payload["properties"])
            self.patch_payloads.append(copy.deepcopy(payload))
            for name, value in properties.items():
                self.pages[page_id]["properties"][name] = self.read_property(value)
            self.pages[page_id]["last_edited_time"] = (
                f"2026-01-01T00:00:0{len(self.patch_payloads)}.000Z"
            )
            self.pages[page_id]["last_edited_by"] = {"id": "integration"}
            if self.corrupt_after_patch and any(
                value.get("rich_text") for value in properties.values()
            ):
                self.pages[page_id]["properties"][
                    sync.SYNC_STATUS_PROPERTY
                ] = rich_text_property("corrupt")
            return copy.deepcopy(self.pages[page_id])
        raise AssertionError(f"예상하지 못한 요청: {method} {url}")

    def retrieve(self, token: str, page_id: str) -> dict:
        self.assert_token(token)
        return copy.deepcopy(self.pages[page_id])

    def list_children(self, token: str, block_id: str) -> list[dict]:
        self.assert_token(token)
        if block_id in self.roots:
            return copy.deepcopy(self.roots[block_id])
        return copy.deepcopy(self.children.get(block_id, []))

    def read_property(self, value: dict) -> dict:
        normalized = copy.deepcopy(value)
        normalized.setdefault("type", "rich_text")
        for part in normalized.get("rich_text", []):
            content = str(part.get("text", {}).get("content") or "")
            part["plain_text"] = content
        return normalized

    def assert_token(self, token: str) -> None:
        if token != "token":
            raise AssertionError("잘못된 토큰")

    @contextmanager
    def patched(self) -> Iterator[None]:
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(migration, "notion_request", side_effect=self.query)
            )
            stack.enter_context(
                patch.object(migration, "retrieve_page", side_effect=self.retrieve)
            )
            stack.enter_context(
                patch.object(
                    migration,
                    "list_block_children",
                    side_effect=self.list_children,
                )
            )
            stack.enter_context(
                patch.object(sync, "retrieve_page", side_effect=self.retrieve)
            )
            stack.enter_context(
                patch.object(
                    sync,
                    "list_block_children",
                    side_effect=self.list_children,
                )
            )
            yield


class ExistingPageMigrationTests(unittest.TestCase):
    def build_plan(self, store: NotionStore) -> dict:
        return migration.build_migration_plan(
            "token",
            store.data_source_id,
            ["page-1"],
        )

    def apply_plan(
        self,
        plan: dict,
        confirmation: str | None = None,
    ) -> dict:
        return migration.apply_migration_plan(
            "token",
            plan,
            confirmation if confirmation is not None else plan["confirmation"],
            migration.LOCAL_WRITE_CONFIRMATION,
        )

    def test_plan_apply_preserves_body_and_registers_current_manifest(self) -> None:
        store = NotionStore()
        before_roots = copy.deepcopy(store.roots)
        before_children = copy.deepcopy(store.children)
        before_icon = copy.deepcopy(store.pages["page-1"]["icon"])
        before_top = copy.deepcopy(
            store.pages["page-1"]["properties"][sync.TOP_PROPERTY]
        )

        with store.patched():
            plan = self.build_plan(store)
            entry = plan["pages"][0]
            self.assertEqual(plan["selection"], "explicit_pages")
            self.assertEqual(plan["page_id_allowlist"], ["page-1"])
            self.assertEqual(entry["source_id"], "141")
            self.assertEqual(entry["notice_id"], "1001")
            self.assertEqual(entry["title"], "공지 1001")
            self.assertEqual(
                entry["url"],
                "https://www.sogang.ac.kr/ko/detail/1001?bbsConfigFk=141",
            )
            self.assertEqual(entry["quote_id"], "quote-1")
            self.assertRegex(entry["quote_hash"], r"^[0-9a-f]{64}$")
            self.assertEqual(entry["quote_marker"], "legacy")
            self.assertEqual(entry["quote_preview"], "기존 본문")

            result = self.apply_plan(plan)

            page = store.retrieve("token", "page-1")
            manifest = sync.extract_body_generation_manifest(page["properties"])
            self.assertTrue(sync.is_managed_page(page, "141", "1001"))
            self.assertIsNotNone(manifest)
            assert manifest is not None
            self.assertTrue(
                sync.is_body_generation_current(
                    "token",
                    "page-1",
                    manifest["g"],
                )
            )

        self.assertEqual(result["applied"], ["page-1"])
        self.assertEqual(result["already_applied"], [])
        self.assertEqual(store.roots, before_roots)
        self.assertEqual(store.children, before_children)
        self.assertEqual(store.pages["page-1"]["icon"], before_icon)
        self.assertEqual(
            store.pages["page-1"]["properties"][sync.TOP_PROPERTY],
            before_top,
        )
        self.assertEqual(len(store.patch_payloads), 1)
        self.assertNotIn("icon", store.patch_payloads[0])
        self.assertEqual(
            set(store.patch_payloads[0]["properties"]),
            {
                sync.SYNC_OWNER_PROPERTY,
                sync.SOURCE_KEY_PROPERTY,
                sync.NOTICE_ID_PROPERTY,
                sync.SYNC_STATUS_PROPERTY,
                sync.SYNC_GENERATION_PROPERTY,
            },
        )

    def test_zero_quote_page_is_allowed_without_body_manifest(self) -> None:
        store = NotionStore(with_quote=False)
        before_roots = copy.deepcopy(store.roots)

        with store.patched():
            plan = self.build_plan(store)
            entry = plan["pages"][0]
            self.assertIsNone(entry["quote_id"])
            self.assertIsNone(entry["quote_hash"])

            self.apply_plan(plan)

            page = store.retrieve("token", "page-1")
            self.assertTrue(sync.is_managed_page(page, "141", "1001"))
            self.assertIsNone(
                sync.extract_body_generation_manifest(page["properties"])
            )

        self.assertEqual(store.roots, before_roots)
        self.assertNotIn(
            sync.SYNC_GENERATION_PROPERTY,
            store.patch_payloads[0]["properties"],
        )

    def test_reapplying_same_plan_is_idempotent(self) -> None:
        store = NotionStore()

        with store.patched():
            plan = self.build_plan(store)
            first = self.apply_plan(plan)
            second = self.apply_plan(plan)

        self.assertEqual(first["applied"], ["page-1"])
        self.assertEqual(second["applied"], [])
        self.assertEqual(second["already_applied"], ["page-1"])
        self.assertEqual(len(store.patch_payloads), 1)

    def test_wrong_confirmation_blocks_before_any_mutation(self) -> None:
        store = NotionStore()

        with store.patched():
            plan = self.build_plan(store)
            with self.assertRaisesRegex(
                migration.MigrationError,
                "확인 문자열",
            ):
                self.apply_plan(plan, "APPLY")

        self.assertEqual(store.patch_payloads, [])

    def test_url_parent_quote_and_property_races_block_without_mutation(self) -> None:
        cases = ("url", "parent", "quote", "property")
        for case in cases:
            with self.subTest(case=case):
                store = NotionStore()
                with store.patched():
                    plan = self.build_plan(store)
                    if case == "url":
                        store.pages["page-1"]["properties"][
                            sync.URL_PROPERTY
                        ]["url"] = (
                            "https://www.sogang.ac.kr/ko/detail/9999"
                            "?bbsConfigFk=141"
                        )
                    elif case == "parent":
                        store.pages["page-1"]["parent"][
                            "data_source_id"
                        ] = "other-source"
                    elif case == "quote":
                        store.children["quote-1"][0]["paragraph"][
                            "rich_text"
                        ][0]["plain_text"] = "변경된 본문"
                    else:
                        store.pages["page-1"]["properties"][
                            sync.TOP_PROPERTY
                        ]["checkbox"] = False

                    with self.assertRaises(migration.MigrationError):
                        self.apply_plan(plan)

                self.assertEqual(store.patch_payloads, [])

    def test_duplicate_identity_blocks_plan(self) -> None:
        store = NotionStore()
        store.pages["page-2"] = make_page(page_id="page-2")
        store.roots["page-2"] = []

        with store.patched(), self.assertRaisesRegex(
            migration.MigrationError,
            "중복",
        ):
            self.build_plan(store)

        self.assertEqual(store.patch_payloads, [])

    def test_two_top_level_quotes_block_plan(self) -> None:
        store = NotionStore()
        store.roots["page-1"].append(quote("quote-2"))
        store.children["quote-2"] = []

        with store.patched(), self.assertRaisesRegex(
            migration.MigrationError,
            "2개 이상",
        ):
            self.build_plan(store)

        self.assertEqual(store.patch_payloads, [])

    def test_failed_postflight_refuses_to_overwrite_unexpected_metadata(self) -> None:
        store = NotionStore()
        store.corrupt_after_patch = True

        with store.patched():
            plan = self.build_plan(store)
            with self.assertRaisesRegex(
                migration.MigrationError,
                "적용에 실패",
            ):
                self.apply_plan(plan)

        self.assertEqual(len(store.patch_payloads), 1)
        values = migration._sync_values(store.pages["page-1"])
        self.assertEqual(values[sync.SYNC_OWNER_PROPERTY], sync.SYNC_OWNER_VALUE)
        self.assertEqual(values[sync.SOURCE_KEY_PROPERTY], "141")
        self.assertEqual(values[sync.NOTICE_ID_PROPERTY], "1001")
        self.assertTrue(values[sync.SYNC_GENERATION_PROPERTY])
        self.assertEqual(values[sync.SYNC_STATUS_PROPERTY], "corrupt")

    def test_plan_requires_an_explicit_page_allowlist(self) -> None:
        with patch.object(
            migration,
            "notion_request",
            side_effect=AssertionError("외부 요청 금지"),
        ):
            with self.assertRaisesRegex(
                migration.MigrationError,
                "--page-id",
            ):
                migration.build_migration_plan(
                    "token",
                    "data-source",
                    [],
                )

    def test_all_pages_plan_covers_every_active_page(self) -> None:
        store = NotionStore()
        store.pages["page-2"] = make_page(
            page_id="page-2",
            notice_id="1002",
        )
        store.roots["page-2"] = [quote("quote-2")]
        store.children["quote-2"] = [paragraph("body-2", "두 번째 본문")]

        with store.patched():
            plan = migration.build_migration_plan(
                "token",
                store.data_source_id,
                all_pages=True,
            )

        self.assertEqual(plan["selection"], "all_pages")
        self.assertEqual(plan["page_id_allowlist"], ["page-1", "page-2"])
        self.assertEqual(len(plan["pages"]), 2)

    def test_all_pages_apply_blocks_when_scope_changes(self) -> None:
        store = NotionStore()

        with store.patched():
            plan = migration.build_migration_plan(
                "token",
                store.data_source_id,
                all_pages=True,
            )
            store.pages["page-2"] = make_page(
                page_id="page-2",
                notice_id="1002",
            )
            store.roots["page-2"] = []
            with self.assertRaisesRegex(
                migration.MigrationError,
                "범위가 계획 생성 이후 변경",
            ):
                self.apply_plan(plan)

        self.assertEqual(store.patch_payloads, [])

    def test_all_pages_plan_resumes_exact_partial_application(self) -> None:
        store = NotionStore()
        store.pages["page-2"] = make_page(
            page_id="page-2",
            notice_id="1002",
        )
        store.roots["page-2"] = [quote("quote-2")]
        store.children["quote-2"] = [paragraph("body-2", "두 번째 본문")]

        with store.patched():
            first_plan = migration.build_migration_plan(
                "token",
                store.data_source_id,
                all_pages=True,
            )
            first_entry = first_plan["pages"][0]
            migration._patch_properties(
                "token",
                first_entry["page_id"],
                migration._desired_properties(first_entry),
            )
            resumed_plan = migration.build_migration_plan(
                "token",
                store.data_source_id,
                all_pages=True,
            )
            result = self.apply_plan(resumed_plan)

        self.assertEqual(
            resumed_plan["confirmation"],
            first_plan["confirmation"],
        )
        self.assertEqual(result["already_applied"], ["page-1"])
        self.assertEqual(result["applied"], ["page-2"])
        self.assertEqual(len(store.patch_payloads), 2)

    def test_plan_ignores_rotating_notion_hosted_file_urls(self) -> None:
        store = NotionStore()
        store.pages["page-1"]["request_id"] = "first-request"
        store.pages["page-1"]["properties"]["첨부파일"] = {
            "type": "files",
            "files": [
                {
                    "id": "file-1",
                    "name": "안내.pdf",
                    "type": "file",
                    "file": {
                        "url": "https://signed.example/first",
                        "expiry_time": "2026-07-28T10:00:00Z",
                    },
                }
            ],
        }

        with store.patched():
            first_plan = self.build_plan(store)
            store.pages["page-1"]["request_id"] = "second-request"
            store.pages["page-1"]["properties"]["첨부파일"]["files"][0][
                "file"
            ] = {
                "url": "https://signed.example/second",
                "expiry_time": "2026-07-28T11:00:00Z",
            }
            second_plan = self.build_plan(store)

        self.assertEqual(
            second_plan["confirmation"],
            first_plan["confirmation"],
        )
        self.assertEqual(
            second_plan["pages"][0]["page_fingerprint"],
            first_plan["pages"][0]["page_fingerprint"],
        )

    def test_root_fingerprint_uses_stable_content_and_block_identity(self) -> None:
        first = paragraph("manual-1", "내용")
        first["last_edited_time"] = "2026-07-28T10:00:00Z"
        first["last_edited_by"] = {"id": "first-user"}
        second = copy.deepcopy(first)
        second["last_edited_time"] = "2026-07-28T11:00:00Z"
        second["last_edited_by"] = {"id": "second-user"}

        self.assertEqual(
            migration._root_fingerprint([first]),
            migration._root_fingerprint([second]),
        )
        second["paragraph"]["rich_text"][0]["plain_text"] = "변경된 내용"
        self.assertNotEqual(
            migration._root_fingerprint([first]),
            migration._root_fingerprint([second]),
        )
        second = copy.deepcopy(first)
        second["id"] = "manual-2"
        self.assertNotEqual(
            migration._root_fingerprint([first]),
            migration._root_fingerprint([second]),
        )

    def test_all_pages_and_explicit_ids_are_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(
            migration.MigrationError,
            "함께 사용할 수 없습니다",
        ):
            migration.build_migration_plan(
                "token",
                "data-source",
                ["page-1"],
                all_pages=True,
            )

    def test_cli_resolves_data_source_from_database_id(self) -> None:
        plan = {"selection": "all_pages", "pages": [], "confirmation": "confirm"}

        with (
            patch.dict(
                migration.os.environ,
                {
                    "NOTION_TOKEN": "token",
                    "NOTION_DB_ID": "database-id",
                    "NOTION_DATA_SOURCE_ID": "",
                },
                clear=True,
            ),
            patch.object(
                migration,
                "resolve_notion_data_source_id",
                return_value="data-source",
            ) as resolve,
            patch.object(
                migration,
                "build_migration_plan",
                return_value=plan,
            ) as build,
            patch.object(migration, "_write_plan") as write,
        ):
            result = migration.main(["--all-pages"])

        self.assertEqual(result, 0)
        resolve.assert_called_once_with("token", "database-id")
        build.assert_called_once_with(
            "token",
            "data-source",
            [],
            all_pages=True,
        )
        write.assert_called_once_with(plan, None)

    def test_cli_apply_output_contains_counts_not_page_ids(self) -> None:
        output = io.StringIO()
        result = {
            "applied": ["private-page-1"],
            "already_applied": ["private-page-2", "private-page-3"],
            "total": 3,
        }

        with (
            patch.dict(
                migration.os.environ,
                {"NOTION_TOKEN": "token"},
                clear=True,
            ),
            patch.object(migration, "_read_plan", return_value={}),
            patch.object(
                migration,
                "apply_migration_plan",
                return_value=result,
            ),
            patch.object(migration.sys, "stdout", output),
        ):
            status = migration.main(
                [
                    "--apply",
                    "--plan",
                    "plan.json",
                    "--confirm",
                    "confirm",
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "applied": 1,
                "already_applied": 2,
                "total": 3,
            },
        )
        self.assertNotIn("private-page", output.getvalue())

    def test_schema_blocker_is_reported_before_page_query(self) -> None:
        store = NotionStore()
        store.schema_properties.pop(sync.SYNC_STATUS_PROPERTY)
        query_pages = Mock()

        with (
            store.patched(),
            patch.object(
                migration,
                "_query_all_pages",
                query_pages,
            ),
            self.assertRaisesRegex(
                migration.MigrationError,
                "스키마 이관을 먼저",
            ),
        ):
            self.build_plan(store)

        query_pages.assert_not_called()
        self.assertEqual(store.patch_payloads, [])

    def test_wrong_schema_type_is_reported_before_page_query(self) -> None:
        store = NotionStore()
        store.schema_properties[sync.SYNC_OWNER_PROPERTY]["type"] = "title"
        query_pages = Mock()

        with (
            store.patched(),
            patch.object(
                migration,
                "_query_all_pages",
                query_pages,
            ),
            self.assertRaisesRegex(
                migration.MigrationError,
                f"{sync.SYNC_OWNER_PROPERTY}:title",
            ),
        ):
            self.build_plan(store)

        query_pages.assert_not_called()

    def test_missing_or_duplicate_schema_ids_block_before_page_query(
        self,
    ) -> None:
        for case in ("missing", "duplicate"):
            with self.subTest(case=case):
                store = NotionStore()
                if case == "missing":
                    store.schema_properties[
                        sync.SYNC_OWNER_PROPERTY
                    ]["id"] = ""
                else:
                    store.schema_properties[
                        sync.SYNC_OWNER_PROPERTY
                    ]["id"] = store.schema_properties[
                        sync.SOURCE_KEY_PROPERTY
                    ]["id"]
                query_pages = Mock()

                with (
                    store.patched(),
                    patch.object(
                        migration,
                        "_query_all_pages",
                        query_pages,
                    ),
                    self.assertRaisesRegex(
                        migration.MigrationError,
                        "ID",
                    ),
                ):
                    self.build_plan(store)

                query_pages.assert_not_called()

    def test_partially_populated_candidate_is_reported_as_blocker(self) -> None:
        store = NotionStore()
        store.pages["page-1"]["properties"][
            sync.SOURCE_KEY_PROPERTY
        ] = rich_text_property("141")

        with store.patched(), self.assertRaisesRegex(
            migration.MigrationError,
            rf"이관 차단 항목.*{sync.SOURCE_KEY_PROPERTY}",
        ):
            self.build_plan(store)

        self.assertEqual(store.patch_payloads, [])

    def test_existing_sync_marker_quote_is_a_zero_write_blocker(self) -> None:
        store = NotionStore()
        store.roots["page-1"][1]["quote"]["rich_text"] = [
            {
                "type": "text",
                "text": {"content": sync.SYNC_CONTAINER_MARKER},
                "plain_text": sync.SYNC_CONTAINER_MARKER,
            }
        ]

        with store.patched(), self.assertRaisesRegex(
            migration.MigrationError,
            "동기화 표식",
        ):
            self.build_plan(store)

        self.assertEqual(store.patch_payloads, [])

    def test_unmarked_quote_with_manual_blocks_is_a_zero_write_blocker(
        self,
    ) -> None:
        store = NotionStore()
        store.roots["page-1"][1] = quote(marker="none")

        with store.patched(), self.assertRaisesRegex(
            migration.MigrationError,
            "표식 없는 인용 블록과 다른 최상위 블록",
        ):
            self.build_plan(store)

        self.assertEqual(store.patch_payloads, [])

    def test_single_unmarked_quote_can_be_migrated(self) -> None:
        store = NotionStore()
        store.roots["page-1"] = [quote(marker="none")]

        with store.patched():
            plan = self.build_plan(store)
            self.assertEqual(
                plan["pages"][0]["quote_marker"],
                "unmarked",
            )
            self.apply_plan(plan)

        self.assertEqual(len(store.patch_payloads), 1)

    def test_apply_requires_writer_context_or_local_opt_in(self) -> None:
        store = NotionStore()

        with store.patched():
            plan = self.build_plan(store)
            with (
                patch.object(
                    migration,
                    "is_writer_context_confirmed",
                    return_value=False,
                ),
                self.assertRaisesRegex(
                    migration.MigrationError,
                    "--allow-write",
                ),
            ):
                migration.apply_migration_plan(
                    "token",
                    plan,
                    plan["confirmation"],
                )

        self.assertEqual(store.patch_payloads, [])

    def test_final_prepatch_race_causes_zero_mutation(self) -> None:
        store = NotionStore()

        with store.patched():
            plan = self.build_plan(store)
            original = migration._current_entry_state
            calls = 0

            def race_on_final_check(
                token: str,
                data_source_id: str,
                entry: dict,
            ) -> str:
                nonlocal calls
                calls += 1
                if calls == 3:
                    store.pages["page-1"]["properties"][
                        sync.TOP_PROPERTY
                    ]["checkbox"] = False
                return original(token, data_source_id, entry)

            with (
                patch.object(
                    migration,
                    "_current_entry_state",
                    side_effect=race_on_final_check,
                ),
                self.assertRaisesRegex(
                    migration.MigrationError,
                    "계획 이후 변경",
                ),
            ):
                self.apply_plan(plan)

        self.assertEqual(store.patch_payloads, [])

    def test_rollback_requires_exact_applied_state_and_verifies_readback(self) -> None:
        store = NotionStore()

        with store.patched():
            plan = self.build_plan(store)
            entry = plan["pages"][0]
            self.apply_plan(plan)
            migration._rollback_properties(
                "token",
                store.data_source_id,
                entry,
            )
            state = migration._current_entry_state(
                "token",
                store.data_source_id,
                entry,
            )

        self.assertEqual(state, "pending")
        self.assertEqual(len(store.patch_payloads), 2)

    def test_rollback_refuses_body_race_without_patch(self) -> None:
        store = NotionStore()

        with store.patched():
            plan = self.build_plan(store)
            entry = plan["pages"][0]
            self.apply_plan(plan)
            store.children["quote-1"][0]["paragraph"]["rich_text"][0][
                "plain_text"
            ] = "외부 변경"
            with self.assertRaisesRegex(
                migration.MigrationError,
                "계획 이후 변경",
            ):
                migration._rollback_properties(
                    "token",
                    store.data_source_id,
                    entry,
                )

        self.assertEqual(len(store.patch_payloads), 1)

    def test_schema_change_after_plan_blocks_apply_without_mutation(self) -> None:
        store = NotionStore()

        with store.patched():
            plan = self.build_plan(store)
            store.schema_properties[sync.SYNC_STATUS_PROPERTY]["id"] = "changed"
            with self.assertRaisesRegex(
                migration.MigrationError,
                "스키마가 계획 생성 이후 변경",
            ):
                self.apply_plan(plan)

        self.assertEqual(store.patch_payloads, [])


if __name__ == "__main__":
    unittest.main()
