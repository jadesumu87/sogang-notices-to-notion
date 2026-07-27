import copy
import json
import os
import socket
import sys
import unittest
import urllib.error
import urllib.request
from email.message import Message
from io import BytesIO
from pathlib import Path
from unittest.mock import call, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import notion_client
import settings


def make_png_payload():
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (2, 2), (32, 64, 96)).save(buffer, format="PNG")
    return buffer.getvalue()


SINGLE_SOURCE_DATABASE = {
    "object": "database",
    "id": "database-id",
    "data_sources": [{"id": "source-a", "name": "공지"}],
}
MULTI_SOURCE_DATABASE = {
    "object": "database",
    "id": "database-id",
    "data_sources": [
        {"id": "source-a", "name": "공지"},
        {"id": "source-b", "name": "보관"},
    ],
}


def complete_schema_database(title_name=None):
    title_name = title_name or settings.TITLE_PROPERTY
    properties = {}
    for index, (
        name,
        (property_type, _),
    ) in enumerate(
        notion_client.destination_schema_definitions().items()
    ):
        actual_name = (
            title_name
            if name == settings.TITLE_PROPERTY
            else name
        )
        properties[actual_name] = {
            "id": f"property-{index}",
            "name": actual_name,
            "type": property_type,
        }
    return {
        "id": "source-a",
        "properties": properties,
    }


class JsonResponse(BytesIO):
    def __init__(
        self,
        payload,
        response_url="https://api.notion.com/v1/test",
    ):
        super().__init__(json.dumps(payload).encode("utf-8"))
        self.response_url = response_url

    def geturl(self):
        return self.response_url


def build_http_error(status_code: int, payload=None) -> urllib.error.HTTPError:
    headers = Message()
    body = payload or {
        "object": "error",
        "status": status_code,
        "code": "internal_server_error",
        "message": "temporary failure",
    }
    return urllib.error.HTTPError(
        "https://api.notion.com/v1/test",
        status_code,
        "test error",
        headers,
        BytesIO(json.dumps(body).encode("utf-8")),
    )


class NotionProtocolTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "NOTION_API_VERSION": "2026-03-11",
                "NOTION_DATA_SOURCE_ID": "",
                "NOTION_SCHEMA_MIGRATION": "0",
            },
        )
        self.env.start()
        notion_client.NOTION_DATA_SOURCE_ID_CACHE.clear()
        notion_client.FILE_UPLOAD_CACHE.clear()

    def tearDown(self):
        notion_client.NOTION_DATA_SOURCE_ID_CACHE.clear()
        notion_client.FILE_UPLOAD_CACHE.clear()
        self.env.stop()

    def test_default_api_version_is_2026_03_11(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(settings.get_notion_api_version(), "2026-03-11")

    def test_unsupported_api_version_is_rejected(self):
        with (
            patch.dict(
                os.environ,
                {"NOTION_API_VERSION": "2022-06-28"},
            ),
            self.assertRaises(ValueError),
        ):
            settings.get_notion_api_version()

    def test_single_data_source_is_discovered_and_cached(self):
        with patch.object(
            notion_client,
            "notion_request",
            return_value=SINGLE_SOURCE_DATABASE,
        ) as request:
            first = notion_client.resolve_notion_data_source_id("token", "database-id")
            second = notion_client.resolve_notion_data_source_id("token", "database-id")

        self.assertEqual(first, "source-a")
        self.assertEqual(second, "source-a")
        request.assert_called_once_with(
            "GET",
            "https://api.notion.com/v1/databases/database-id",
            "token",
        )

    def test_data_source_refresh_bypasses_cached_membership(self):
        moved_database = {
            **SINGLE_SOURCE_DATABASE,
            "data_sources": [{"id": "source-b", "name": "공지"}],
        }
        with patch.object(
            notion_client,
            "notion_request",
            side_effect=[
                SINGLE_SOURCE_DATABASE,
                moved_database,
            ],
        ) as request:
            first = notion_client.resolve_notion_data_source_id(
                "token",
                "database-id",
            )
            refreshed = notion_client.resolve_notion_data_source_id(
                "token",
                "database-id",
                refresh=True,
            )

        self.assertEqual(first, "source-a")
        self.assertEqual(refreshed, "source-b")
        self.assertEqual(request.call_count, 2)

    def test_multiple_data_sources_require_explicit_selection(self):
        with (
            patch.object(
                notion_client,
                "notion_request",
                return_value=MULTI_SOURCE_DATABASE,
            ),
            self.assertRaises(notion_client.NotionDataSourceResolutionError),
        ):
            notion_client.resolve_notion_data_source_id("token", "database-id")

    def test_explicit_data_source_is_validated_against_database(self):
        with (
            patch.dict(os.environ, {"NOTION_DATA_SOURCE_ID": "sourceb"}),
            patch.object(
                notion_client,
                "notion_request",
                return_value=MULTI_SOURCE_DATABASE,
            ),
        ):
            selected = notion_client.resolve_notion_data_source_id(
                "token",
                "database-id",
            )

        self.assertEqual(selected, "source-b")

    def test_unknown_explicit_data_source_is_rejected(self):
        with (
            patch.dict(os.environ, {"NOTION_DATA_SOURCE_ID": "source-c"}),
            patch.object(
                notion_client,
                "notion_request",
                return_value=MULTI_SOURCE_DATABASE,
            ),
            self.assertRaises(notion_client.NotionDataSourceResolutionError),
        ):
            notion_client.resolve_notion_data_source_id("token", "database-id")

    def test_missing_data_sources_array_is_rejected(self):
        with (
            patch.object(
                notion_client,
                "notion_request",
                return_value={"object": "database", "id": "database-id"},
            ),
            self.assertRaises(notion_client.NotionDataSourceResolutionError),
        ):
            notion_client.resolve_notion_data_source_id("token", "database-id")

    def test_fetch_database_returns_data_source_schema(self):
        schema = {
            "object": "data_source",
            "id": "source-a",
            "properties": {"공지사항": {"type": "title"}},
        }
        with patch.object(
            notion_client,
            "notion_request",
            side_effect=[SINGLE_SOURCE_DATABASE, schema],
        ) as request:
            result = notion_client.fetch_database("token", "database-id")

        self.assertEqual(result, schema)
        self.assertEqual(
            request.call_args_list,
            [
                call(
                    "GET",
                    "https://api.notion.com/v1/databases/database-id",
                    "token",
                ),
                call(
                    "GET",
                    "https://api.notion.com/v1/data_sources/source-a",
                    "token",
                ),
            ],
        )

    def test_block_pagination_rejects_missing_or_repeated_cursor(self):
        for responses in (
            [
                {
                    "results": [],
                    "has_more": True,
                    "next_cursor": None,
                }
            ],
            [
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
            ],
        ):
            with self.subTest(responses=len(responses)):
                with (
                    patch.object(
                        notion_client,
                        "notion_request",
                        side_effect=responses,
                    ),
                    patch.object(
                        notion_client,
                        "check_run_control",
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "커서가 누락되거나 반복",
                    ),
                ):
                    notion_client.list_block_children(
                        "token",
                        "page-id",
                    )

    def test_query_uses_data_source_endpoint(self):
        result = {"results": [{"id": "page-id"}], "has_more": False}
        with patch.object(
            notion_client,
            "notion_request",
            side_effect=[SINGLE_SOURCE_DATABASE, result],
        ) as request:
            actual = notion_client.query_database_page(
                "token",
                "database-id",
                {"page_size": 100},
            )

        self.assertEqual(actual, result)
        self.assertEqual(
            request.call_args_list[-1],
            call(
                "POST",
                "https://api.notion.com/v1/data_sources/source-a/query",
                "token",
                {"page_size": 100},
            ),
        )

    def test_schema_update_is_disabled_during_regular_runs(self):
        with (
            patch.object(notion_client, "notion_request") as request,
            self.assertRaises(notion_client.NotionSchemaMigrationRequired),
        ):
            notion_client.update_database(
                "token",
                "database-id",
                {"신규": {"rich_text": {}}},
            )

        request.assert_not_called()

    def test_explicit_schema_update_uses_data_source_endpoint(self):
        updated = {
            "object": "data_source",
            "id": "source-a",
            "properties": {"신규": {"type": "rich_text", "rich_text": {}}},
        }
        with patch.object(
            notion_client,
            "notion_request",
            side_effect=[SINGLE_SOURCE_DATABASE, updated],
        ) as request:
            result = notion_client.update_database(
                "token",
                "database-id",
                {"신규": {"rich_text": {}}},
                allow_schema_changes=True,
            )

        self.assertEqual(result, updated)
        self.assertEqual(
            request.call_args_list[-1],
            call(
                "PATCH",
                "https://api.notion.com/v1/data_sources/source-a",
                "token",
                {"properties": {"신규": {"rich_text": {}}}},
            ),
        )

    def test_sync_metadata_schema_is_validated_without_writes(self):
        properties = {
            name: {"type": "rich_text", "rich_text": {}}
            for name in (
                settings.SYNC_OWNER_PROPERTY,
                settings.SOURCE_KEY_PROPERTY,
                settings.NOTICE_ID_PROPERTY,
                settings.SYNC_GENERATION_PROPERTY,
                settings.SYNC_STATUS_PROPERTY,
                settings.SYNC_OPERATION_PROPERTY,
            )
        }
        database = {"properties": properties}
        with patch.object(notion_client, "notion_request") as request:
            result = notion_client.ensure_sync_metadata_properties(
                "token",
                "database-id",
                database,
            )

        self.assertIs(result, database)
        request.assert_not_called()

    def test_sync_metadata_wrong_type_is_rejected(self):
        database = {
            "properties": {
                settings.SYNC_OWNER_PROPERTY: {"type": "number", "number": {}}
            }
        }
        with self.assertRaises(RuntimeError):
            notion_client.ensure_sync_metadata_properties(
                "token",
                "database-id",
                database,
            )

    def test_sync_metadata_migration_requires_flag(self):
        with self.assertRaises(notion_client.NotionSchemaMigrationRequired):
            notion_client.ensure_sync_metadata_properties(
                "token",
                "database-id",
                {"properties": {}},
            )

    def test_sync_metadata_migration_updates_all_properties_when_enabled(self):
        database = {"object": "data_source", "id": "source-a", "properties": {}}
        patch_payloads = []

        def request(method, url, token, payload=None):
            if method == "GET":
                return SINGLE_SOURCE_DATABASE
            patch_payloads.append(payload)
            for name in payload["properties"]:
                database["properties"][name] = {"type": "rich_text", "rich_text": {}}
            return database

        with (
            patch.dict(os.environ, {"NOTION_SCHEMA_MIGRATION": "1"}),
            patch.object(notion_client, "notion_request", side_effect=request),
        ):
            result = notion_client.ensure_sync_metadata_properties(
                "token",
                "database-id",
                database,
            )

        self.assertIs(result, database)
        self.assertEqual(len(patch_payloads), 6)
        self.assertEqual(
            set(database["properties"]),
            {
                settings.SYNC_OWNER_PROPERTY,
                settings.SOURCE_KEY_PROPERTY,
                settings.NOTICE_ID_PROPERTY,
                settings.SYNC_GENERATION_PROPERTY,
                settings.SYNC_STATUS_PROPERTY,
                settings.SYNC_OPERATION_PROPERTY,
            },
        )

    def test_required_schema_includes_sync_metadata_validation(self):
        database = {
            "properties": {
                settings.TITLE_PROPERTY: {"type": "title", "title": {}},
                settings.TOP_PROPERTY: {"type": "checkbox", "checkbox": {}},
                settings.DATE_PROPERTY: {"type": "date", "date": {}},
                settings.AUTHOR_PROPERTY: {"type": "select", "select": {}},
                settings.URL_PROPERTY: {"type": "url", "url": {}},
                settings.TYPE_PROPERTY: {"type": "select", "select": {}},
                **{
                    name: {"type": "rich_text", "rich_text": {}}
                    for name in (
                        settings.SYNC_OWNER_PROPERTY,
                        settings.SOURCE_KEY_PROPERTY,
                        settings.NOTICE_ID_PROPERTY,
                        settings.SYNC_GENERATION_PROPERTY,
                        settings.SYNC_STATUS_PROPERTY,
                        settings.SYNC_OPERATION_PROPERTY,
                    )
                },
            }
        }
        with patch.object(notion_client, "notion_request") as request:
            result = notion_client.ensure_required_properties(
                "token",
                "database-id",
                database,
            )

        self.assertIs(result, database)
        request.assert_not_called()

    def test_schema_migration_validates_all_types_before_write(self):
        database = complete_schema_database()
        del database["properties"][settings.TOP_PROPERTY]
        database["properties"][settings.VIEWS_PROPERTY]["type"] = (
            "checkbox"
        )

        with (
            patch.object(notion_client, "update_database") as update,
            self.assertRaisesRegex(
                RuntimeError,
                settings.VIEWS_PROPERTY,
            ),
        ):
            notion_client.ensure_destination_schema(
                "token",
                "database-id",
                database,
            )

        update.assert_not_called()

    def test_schema_migration_uses_one_verified_patch(self):
        database = complete_schema_database(title_name="이름")
        del database["properties"][settings.TOP_PROPERTY]
        updated = complete_schema_database()
        updated["properties"][settings.TOP_PROPERTY]["id"] = (
            "new-top-property"
        )
        current = copy.deepcopy(database)

        with (
            patch.object(
                notion_client,
                "resolve_notion_data_source_id",
                return_value="source-a",
            ) as resolve,
            patch.object(
                notion_client,
                "fetch_data_source",
                side_effect=[
                    current,
                    copy.deepcopy(current),
                    updated,
                ],
            ),
            patch.object(
                notion_client,
                "update_database",
                return_value=updated,
            ) as update,
        ):
            result = notion_client.ensure_destination_schema(
                "token",
                "database-id",
                database,
            )

        self.assertIs(result, updated)
        self.assertEqual(resolve.call_count, 3)
        update.assert_called_once()
        patch_payload = update.call_args.args[2]
        self.assertEqual(
            patch_payload["property-0"],
            {"name": settings.TITLE_PROPERTY},
        )
        self.assertEqual(
            patch_payload[settings.TOP_PROPERTY],
            {"checkbox": {}},
        )

    def test_schema_migration_stops_when_data_source_changes(self):
        database = complete_schema_database(title_name="이름")
        del database["properties"][settings.TOP_PROPERTY]

        with (
            patch.object(
                notion_client,
                "resolve_notion_data_source_id",
                side_effect=["source-a", "source-b"],
            ),
            patch.object(
                notion_client,
                "fetch_data_source",
                return_value=copy.deepcopy(database),
            ),
            patch.object(notion_client, "update_database") as update,
            self.assertRaisesRegex(
                RuntimeError,
                "적용 직전에 변경",
            ),
        ):
            notion_client.ensure_destination_schema(
                "token",
                "database-id",
                database,
            )

        update.assert_not_called()

    def test_schema_migration_stops_on_last_moment_schema_change(self):
        database = complete_schema_database(title_name="이름")
        del database["properties"][settings.TOP_PROPERTY]
        changed = copy.deepcopy(database)
        changed["properties"][settings.CLASSIFICATION_PROPERTY][
            "select"
        ] = {"options": [{"name": "변경됨"}]}

        with (
            patch.object(
                notion_client,
                "resolve_notion_data_source_id",
                return_value="source-a",
            ),
            patch.object(
                notion_client,
                "fetch_data_source",
                side_effect=[copy.deepcopy(database), changed],
            ),
            patch.object(notion_client, "update_database") as update,
            self.assertRaisesRegex(
                RuntimeError,
                "적용 직전에 변경",
            ),
        ):
            notion_client.ensure_destination_schema(
                "token",
                "database-id",
                database,
            )

        update.assert_not_called()

    def test_create_page_uses_data_source_parent(self):
        with (
            patch.object(
                notion_client,
                "resolve_notion_data_source_id",
                return_value="source-a",
            ),
            patch.object(
                notion_client,
                "notion_request",
                return_value={"id": "page-id"},
            ) as request,
        ):
            page_id = notion_client.create_page(
                "token",
                "database-id",
                {"공지사항": {"title": []}},
            )

        self.assertEqual(page_id, "page-id")
        self.assertEqual(
            request.call_args.args[3]["parent"],
            {"type": "data_source_id", "data_source_id": "source-a"},
        )

    def test_retrieve_page_validates_id_and_properties(self):
        page = {"id": "page-id", "properties": {"제목": {"title": []}}}
        with patch.object(
            notion_client,
            "notion_request",
            return_value=page,
        ) as request:
            result = notion_client.retrieve_page("token", "page-id")

        self.assertIs(result, page)
        request.assert_called_once_with(
            "GET",
            "https://api.notion.com/v1/pages/page-id",
            "token",
        )

    def test_retrieve_page_rejects_mismatched_response_id(self):
        with (
            patch.object(
                notion_client,
                "notion_request",
                return_value={"id": "other", "properties": {}},
            ),
            self.assertRaisesRegex(RuntimeError, "ID가 요청과 다릅니다"),
        ):
            notion_client.retrieve_page("token", "page-id")

    def test_retrieve_page_rejects_missing_properties(self):
        with (
            patch.object(
                notion_client,
                "notion_request",
                return_value={"id": "page-id"},
            ),
            self.assertRaisesRegex(RuntimeError, "속성이 올바르지 않습니다"),
        ):
            notion_client.retrieve_page("token", "page-id")

    def test_archive_page_uses_in_trash(self):
        with patch.object(
            notion_client,
            "notion_request",
            return_value={},
        ) as request:
            notion_client.archive_page("token", "page-id")

        request.assert_called_once_with(
            "PATCH",
            "https://api.notion.com/v1/pages/page-id",
            "token",
            {"in_trash": True},
        )

    def test_delete_block_fallback_uses_in_trash(self):
        failure = notion_client.NotionRequestError(
            "delete failed",
            status_code=500,
        )
        with patch.object(
            notion_client,
            "notion_request",
            side_effect=[failure, {}],
        ) as request:
            notion_client.delete_block("token", "block-id")

        self.assertEqual(
            request.call_args_list[-1],
            call(
                "PATCH",
                "https://api.notion.com/v1/blocks/block-id",
                "token",
                {"in_trash": True},
            ),
        )

    def test_payload_limits_are_enforced_before_network(self):
        payloads = (
            {"items": [{}] * 101},
            {"items": tuple({} for _ in range(101))},
            {"external": {"url": "x" * 2001}},
            {"content": "x" * notion_client.NOTION_MAX_REQUEST_BYTES},
        )
        for payload in payloads:
            with self.subTest(payload_type=next(iter(payload))):
                with (
                    patch.object(notion_client, "open_notion_request") as urlopen,
                    self.assertRaises(notion_client.NotionPayloadError),
                ):
                    notion_client.notion_request(
                        "POST",
                        "https://api.notion.com/v1/pages",
                        "token",
                        payload,
                    )
                urlopen.assert_not_called()

    def test_payload_limit_boundaries_are_accepted(self):
        size_overhead = len(b'{"content":""}')
        exact_size_payload = notion_client.encode_notion_payload(
            {
                "content": "x"
                * (notion_client.NOTION_MAX_REQUEST_BYTES - size_overhead)
            }
        )
        encoded = notion_client.encode_notion_payload(
            {
                "items": [{}] * 100,
                "external": {"url": "x" * 2000},
            }
        )
        self.assertEqual(
            len(exact_size_payload),
            notion_client.NOTION_MAX_REQUEST_BYTES,
        )
        self.assertIsInstance(encoded, bytes)

    def test_notion_api_rejects_noncanonical_targets(self):
        targets = (
            "http://api.notion.com/v1/pages/page-id",
            "https://attacker.example/v1/pages/page-id",
            "https://api.notion.com:444/v1/pages/page-id",
            "https://user@api.notion.com/v1/pages/page-id",
            "https://api.notion.com/v1/pages/page-id#fragment",
        )
        with patch.object(
            notion_client,
            "open_notion_request",
        ) as open_request:
            for target in targets:
                with (
                    self.subTest(target=target),
                    self.assertRaises(notion_client.NotionRequestError),
                ):
                    notion_client.notion_request("GET", target, "token")

        open_request.assert_not_called()

    def test_notion_redirect_handler_refuses_all_redirects(self):
        handler = notion_client.NoNotionRedirectHandler()
        opener = notion_client.build_notion_opener()
        redirect_handlers = [
            current
            for current in opener.handlers
            if isinstance(current, urllib.request.HTTPRedirectHandler)
        ]
        self.assertEqual(len(redirect_handlers), 1)
        self.assertIsInstance(
            redirect_handlers[0],
            notion_client.NoNotionRedirectHandler,
        )
        request = urllib.request.Request(
            "https://api.notion.com/v1/test"
        )
        for status_code in (301, 302, 303, 307, 308):
            with self.subTest(status_code=status_code):
                redirected = handler.redirect_request(
                    request,
                    BytesIO(),
                    status_code,
                    "redirect",
                    Message(),
                    "https://attacker.example/collect",
                )
                self.assertIsNone(redirected)

    def test_notion_api_does_not_retry_redirect_response(self):
        with (
            patch.object(notion_client, "wait_for_notion_request_slot"),
            patch.object(
                notion_client,
                "open_notion_request",
                side_effect=build_http_error(302),
            ) as open_request,
            self.assertRaises(notion_client.NotionRequestError),
        ):
            notion_client.notion_request(
                "GET",
                "https://api.notion.com/v1/pages/page-id",
                "token",
            )

        self.assertEqual(open_request.call_count, 1)

    def test_notion_api_rejects_unsafe_response_target(self):
        with (
            patch.object(notion_client, "wait_for_notion_request_slot"),
            patch.object(
                notion_client,
                "open_notion_request",
                return_value=JsonResponse(
                    {"ok": True},
                    "https://attacker.example/collect",
                ),
            ) as open_request,
            self.assertRaises(notion_client.NotionRequestError) as raised,
        ):
            notion_client.notion_request(
                "GET",
                "https://api.notion.com/v1/pages/page-id",
                "token",
            )

        self.assertEqual(raised.exception.reason, "unsafe_response_target")
        self.assertEqual(open_request.call_count, 1)

    def test_safe_get_retries_transient_server_error(self):
        with (
            patch.object(notion_client, "wait_for_notion_request_slot"),
            patch.object(notion_client.time, "sleep"),
            patch.object(
                notion_client,
                "open_notion_request",
                side_effect=[build_http_error(503), JsonResponse({"ok": True})],
            ) as urlopen,
        ):
            result = notion_client.notion_request(
                "GET",
                "https://api.notion.com/v1/pages/page-id",
                "token",
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        for request_call in urlopen.call_args_list:
            request = request_call.args[0]
            self.assertEqual(
                request.unredirected_hdrs.get("Authorization"),
                "Bearer token",
            )
            self.assertNotIn("Authorization", request.headers)

    def test_read_post_retries_timeout(self):
        with (
            patch.object(notion_client, "wait_for_notion_request_slot"),
            patch.object(notion_client.time, "sleep"),
            patch.object(
                notion_client,
                "open_notion_request",
                side_effect=[socket.timeout(), JsonResponse({"results": []})],
            ) as urlopen,
        ):
            result = notion_client.notion_request(
                "POST",
                "https://api.notion.com/v1/data_sources/source-a/query",
                "token",
                {"page_size": 100},
            )

        self.assertEqual(result, {"results": []})
        self.assertEqual(urlopen.call_count, 2)

    def test_safe_patch_retries_transient_server_error(self):
        with (
            patch.object(notion_client, "wait_for_notion_request_slot"),
            patch.object(notion_client.time, "sleep"),
            patch.object(
                notion_client,
                "open_notion_request",
                side_effect=[build_http_error(500), JsonResponse({"id": "page-id"})],
            ) as urlopen,
        ):
            result = notion_client.notion_request(
                "PATCH",
                "https://api.notion.com/v1/pages/page-id",
                "token",
                {"properties": {}},
            )

        self.assertEqual(result, {"id": "page-id"})
        self.assertEqual(urlopen.call_count, 2)

    def test_create_page_does_not_retry_server_error(self):
        with (
            patch.object(notion_client, "wait_for_notion_request_slot"),
            patch.object(notion_client.time, "sleep"),
            patch.object(
                notion_client,
                "open_notion_request",
                side_effect=build_http_error(500),
            ) as urlopen,
            self.assertRaises(notion_client.NotionRequestError),
        ):
            notion_client.notion_request(
                "POST",
                "https://api.notion.com/v1/pages",
                "token",
                {"parent": {"data_source_id": "source-a"}},
            )

        self.assertEqual(urlopen.call_count, 1)

    def test_block_append_does_not_retry_timeout(self):
        with (
            patch.object(notion_client, "wait_for_notion_request_slot"),
            patch.object(notion_client.time, "sleep"),
            patch.object(
                notion_client,
                "open_notion_request",
                side_effect=socket.timeout(),
            ) as urlopen,
            self.assertRaises(notion_client.NotionRequestError),
        ):
            notion_client.notion_request(
                "PATCH",
                "https://api.notion.com/v1/blocks/block-id/children",
                "token",
                {"children": []},
            )

        self.assertEqual(urlopen.call_count, 1)

    def test_file_upload_creation_does_not_retry_server_error(self):
        with (
            patch.object(notion_client, "wait_for_notion_request_slot"),
            patch.object(notion_client.time, "sleep"),
            patch.object(
                notion_client,
                "open_notion_request",
                side_effect=build_http_error(500),
            ) as urlopen,
        ):
            result = notion_client.create_file_upload(
                "token",
                "notice.pdf",
                "application/pdf",
            )

        self.assertIsNone(result)
        self.assertEqual(urlopen.call_count, 1)

    def test_file_upload_transfer_does_not_retry_timeout(self):
        with (
            patch.object(notion_client.time, "sleep"),
            patch.object(
                notion_client,
                "open_notion_request",
                side_effect=socket.timeout(),
            ) as urlopen,
        ):
            result = notion_client.send_file_upload(
                "token",
                "https://api.notion.com/v1/file_uploads/upload-id/send",
                "notice.pdf",
                "application/pdf",
                b"payload",
            )

        self.assertIsNone(result)
        self.assertEqual(urlopen.call_count, 1)

    def test_file_upload_transfer_does_not_retry_server_error(self):
        with (
            patch.object(notion_client.time, "sleep"),
            patch.object(
                notion_client,
                "open_notion_request",
                side_effect=build_http_error(503),
            ) as urlopen,
        ):
            result = notion_client.send_file_upload(
                "token",
                "https://api.notion.com/v1/file_uploads/upload-id/send",
                "notice.pdf",
                "application/pdf",
                b"payload",
            )

        self.assertIsNone(result)
        self.assertEqual(urlopen.call_count, 1)

    def test_file_upload_transfer_rejects_noncanonical_targets(self):
        targets = (
            "http://api.notion.com/v1/file_uploads/upload-id/send",
            "https://attacker.example/v1/file_uploads/upload-id/send",
            "https://api.notion.com:444/v1/file_uploads/upload-id/send",
            "https://user@api.notion.com/v1/file_uploads/upload-id/send",
            "https://api.notion.com/v1/file_uploads/a%2Fb/send",
            "https://api.notion.com/v1/file_uploads/upload-id/send?token=x",
            "https://api.notion.com/v1/file_uploads/upload-id/send#fragment",
        )
        with patch.object(
            notion_client,
            "open_notion_request",
        ) as urlopen:
            results = [
                notion_client.send_file_upload(
                    "token",
                    target,
                    "notice.pdf",
                    "application/pdf",
                    b"payload",
                )
                for target in targets
            ]

        self.assertEqual(results, [None] * len(targets))
        urlopen.assert_not_called()

    def test_file_upload_authorization_is_not_redirected(self):
        with patch.object(
            notion_client,
            "open_notion_request",
            return_value=JsonResponse(
                {"status": "uploaded"},
                (
                    "https://api.notion.com/v1/file_uploads/"
                    "upload-id/send"
                ),
            ),
        ) as urlopen:
            result = notion_client.send_file_upload(
                "example-token",
                "https://api.notion.com/v1/file_uploads/upload-id/send",
                "notice.pdf",
                "application/pdf",
                b"payload",
            )

        self.assertEqual(result, {"status": "uploaded"})
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.unredirected_hdrs.get("Authorization"),
            "Bearer example-token",
        )
        self.assertNotIn("Authorization", request.headers)

    def test_file_upload_transfer_does_not_follow_redirect(self):
        with patch.object(
            notion_client,
            "open_notion_request",
            side_effect=build_http_error(302),
        ) as open_request:
            result = notion_client.send_file_upload(
                "example-token",
                "https://api.notion.com/v1/file_uploads/upload-id/send",
                "notice.pdf",
                "application/pdf",
                b"payload",
            )

        self.assertIsNone(result)
        self.assertEqual(open_request.call_count, 1)

    def test_file_upload_rejects_unsafe_response_targets(self):
        response_urls = (
            "https://attacker.example/collect",
            "https://api.notion.com/v1/file_uploads/other-id/send",
            "https://api.notion.com/v1/file_uploads/upload-id/send?token=x",
        )
        for response_url in response_urls:
            with (
                self.subTest(response_url=response_url),
                patch.object(
                    notion_client,
                    "open_notion_request",
                    return_value=JsonResponse(
                        {"status": "uploaded"},
                        response_url,
                    ),
                ) as open_request,
            ):
                result = notion_client.send_file_upload(
                    "example-token",
                    (
                        "https://api.notion.com/v1/file_uploads/"
                        "upload-id/send"
                    ),
                    "notice.pdf",
                    "application/pdf",
                    b"payload",
                )

            self.assertIsNone(result)
            self.assertEqual(open_request.call_count, 1)

    def test_file_upload_response_url_must_match_created_id(self):
        source_url = "https://www.sogang.ac.kr/file.png"
        with (
            patch.object(
                notion_client,
                "download_file_bytes",
                return_value=(make_png_payload(), "image/png"),
            ),
            patch.object(
                notion_client,
                "get_workspace_upload_limit",
                return_value=None,
            ),
            patch.object(
                notion_client,
                "create_file_upload",
                return_value={
                    "id": "upload-id",
                    "upload_url": (
                        "https://api.notion.com/v1/file_uploads/"
                        "other-id/send"
                    ),
                },
            ),
            patch.object(notion_client, "send_file_upload") as send,
        ):
            result = notion_client.upload_external_file_to_notion(
                "token",
                source_url,
            )

        self.assertIsNone(result)
        send.assert_not_called()

    def test_file_upload_response_url_matching_created_id_is_sent(self):
        source_url = "https://www.sogang.ac.kr/file.png"
        upload_url = (
            "https://api.notion.com/v1/file_uploads/upload-id/send"
        )
        with (
            patch.object(
                notion_client,
                "download_file_bytes",
                return_value=(make_png_payload(), "image/png"),
            ),
            patch.object(
                notion_client,
                "get_workspace_upload_limit",
                return_value=None,
            ),
            patch.object(
                notion_client,
                "create_file_upload",
                return_value={
                    "id": "upload-id",
                    "upload_url": upload_url,
                },
            ),
            patch.object(
                notion_client,
                "send_file_upload",
                return_value={"status": "uploaded"},
            ) as send,
        ):
            result = notion_client.upload_external_file_to_notion(
                "token",
                source_url,
            )

        self.assertEqual(result, "upload-id")
        self.assertEqual(send.call_args.args[1], upload_url)

    def test_block_append_does_not_retry_server_error(self):
        with (
            patch.object(notion_client, "wait_for_notion_request_slot"),
            patch.object(notion_client.time, "sleep"),
            patch.object(
                notion_client,
                "open_notion_request",
                side_effect=build_http_error(503),
            ) as urlopen,
            self.assertRaises(notion_client.NotionRequestError),
        ):
            notion_client.notion_request(
                "PATCH",
                "https://api.notion.com/v1/blocks/block-id/children",
                "token",
                {"children": []},
            )

        self.assertEqual(urlopen.call_count, 1)

    def test_rate_limit_retries_even_for_create_request(self):
        with (
            patch.object(notion_client, "wait_for_notion_request_slot"),
            patch.object(notion_client.time, "sleep"),
            patch.object(
                notion_client,
                "open_notion_request",
                side_effect=[build_http_error(429), JsonResponse({"id": "page-id"})],
            ) as urlopen,
        ):
            result = notion_client.notion_request(
                "POST",
                "https://api.notion.com/v1/pages",
                "token",
                {"parent": {"data_source_id": "source-a"}},
            )

        self.assertEqual(result, {"id": "page-id"})
        self.assertEqual(urlopen.call_count, 2)


if __name__ == "__main__":
    unittest.main()
