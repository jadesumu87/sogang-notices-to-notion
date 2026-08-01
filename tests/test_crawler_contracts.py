import json
import os
import sys
import unittest
import urllib.error
from email.message import Message
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import crawler
from models import (
    CrawlReport,
    FailureCategory,
    ListPageResult,
    SourceCrawlResult,
    SourceSpec,
    SourceStatus,
)
from validation import validate_crawl_report


NORMAL_ENTRY = {
    "pkId": "1001",
    "title": "정상 공지",
    "regDate": "20260727120000",
    "isTop": "N",
    "userName": "교무처",
    "viewCount": 12,
}
NORMAL_DETAIL = {
    "title": "정상 공지",
    "regDate": "20260727120000",
    "userName": "교무처",
    "viewCount": 12,
    "content": "<p>본문</p>",
    "fileValue1": "",
}


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        status: int = 200,
        content_type: str = "application/json",
        content_length: str | None = None,
    ):
        self.body = body
        self.status = status
        self.url = "https://www.sogang.ac.kr/api/test"
        self.offset = 0
        self.read_sizes = []
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def geturl(self):
        return self.url

    def read(self, size=-1):
        if size < 0:
            raise AssertionError("unbounded response read")
        self.read_sizes.append(size)
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class FakeOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append((request.full_url, timeout))
        if not self.outcomes:
            raise AssertionError("unexpected transport call")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RedirectingOpener:
    def __init__(self, before_redirect, target_url, response):
        self.before_redirect = before_redirect
        self.target_url = target_url
        self.response = response
        self.calls = []
        self.redirect_calls = []

    def open(self, request, timeout=None):
        self.calls.append((request.full_url, timeout))
        if not self.before_redirect(self.target_url):
            raise crawler.ExternalDownloadRunStoppedError(
                "external_redirect_stopped"
            )
        self.redirect_calls.append(self.target_url)
        self.response.url = self.target_url
        return self.response


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def json_response(payload) -> FakeResponse:
    return FakeResponse(
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


def list_page(entries) -> ListPageResult:
    values = list(entries)
    return ListPageResult(
        ok=True,
        entries=values,
        valid_empty=not values,
        status_code=200,
        terminal_verified=not values,
    )


class CrawlerContractTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "SITE_RESPONSE_MAX_BYTES": "4096",
                "SITE_MIN_REQUEST_INTERVAL_SECONDS": "0",
                "CRAWL_HARD_PAGE_LIMIT": "10",
                "BBS_PAGE_SIZE": "20",
            },
        )
        self.env.start()
        self.network = patch.object(
            crawler.urllib.request,
            "urlopen",
            side_effect=AssertionError("real network is disabled"),
        )
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.env.stop()

    def test_api_page_size_defaults_and_caps_match_verified_server_limit(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(crawler.get_bbs_page_size(), 500)
        cases = (
            ("invalid", 500),
            ("0", 1),
            ("200", 200),
            ("999", 500),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                with patch.dict(os.environ, {"BBS_PAGE_SIZE": raw}):
                    self.assertEqual(crawler.get_bbs_page_size(), expected)

    def fetch_list_with_transport(self, outcomes):
        opener = FakeOpener(outcomes)
        with (
            patch.object(
                crawler,
                "is_safe_external_download_target",
                return_value=True,
            ),
            patch.object(
                crawler,
                "build_external_download_opener",
                return_value=opener,
            ),
        ):
            result = crawler.fetch_bbs_list_result(1, 20, config_fk="141")
        return result, opener

    def test_redirect_hop_consumes_source_budget_and_site_spacing(self):
        source_url = "https://www.sogang.ac.kr/api/source"
        target_url = "https://www.sogang.ac.kr/api/target"
        clock = FakeClock()
        holder = {}

        def build_opener(before_redirect=None):
            opener = RedirectingOpener(
                before_redirect,
                target_url,
                FakeResponse(b"ok", content_type="text/plain"),
            )
            holder["opener"] = opener
            return opener

        crawler.NEXT_SITE_REQUEST_AT = 0.0
        with (
            patch.dict(
                os.environ,
                {"SITE_MIN_REQUEST_INTERVAL_SECONDS": "1"},
            ),
            patch.object(
                crawler,
                "is_safe_external_download_target",
                return_value=True,
            ),
            patch.object(
                crawler,
                "build_external_download_opener",
                side_effect=build_opener,
            ),
            patch.object(
                crawler.time,
                "monotonic",
                side_effect=clock.monotonic,
            ),
            patch.object(
                crawler,
                "sleep_with_run_control",
                side_effect=clock.sleep,
            ),
        ):
            with crawler.source_request_budget_scope(
                max_seconds_cap=10,
                max_requests_cap=2,
            ) as budget:
                result = crawler.fetch_site_result(
                    source_url,
                    "테스트",
                )

        self.assertTrue(result.ok)
        self.assertEqual(budget.actual_requests, 2)
        self.assertEqual(clock.sleeps, [1.0])
        self.assertEqual(
            holder["opener"].redirect_calls,
            [target_url],
        )

    def test_redirect_hop_stops_before_request_when_budget_is_exhausted(self):
        source_url = "https://www.sogang.ac.kr/api/source"
        target_url = "https://www.sogang.ac.kr/api/target"
        holder = {}

        def build_opener(before_redirect=None):
            opener = RedirectingOpener(
                before_redirect,
                target_url,
                FakeResponse(b"unexpected"),
            )
            holder["opener"] = opener
            return opener

        crawler.NEXT_SITE_REQUEST_AT = 0.0
        with (
            patch.object(
                crawler,
                "is_safe_external_download_target",
                return_value=True,
            ),
            patch.object(
                crawler,
                "build_external_download_opener",
                side_effect=build_opener,
            ),
        ):
            with crawler.source_request_budget_scope(
                max_requests_cap=1,
            ) as budget:
                result = crawler.fetch_site_result(
                    source_url,
                    "테스트",
                )

        self.assertFalse(result.ok)
        self.assertEqual(
            result.category,
            FailureCategory.SOURCE_PARTIAL,
        )
        self.assertEqual(
            result.error,
            "source_request_budget_exceeded:1",
        )
        self.assertEqual(budget.actual_requests, 1)
        self.assertEqual(holder["opener"].redirect_calls, [])

    def test_retry_after_longer_than_source_budget_stops_without_sleep(self):
        source_url = "https://www.sogang.ac.kr/api/source"
        headers = Message()
        headers["Retry-After"] = "30"
        opener = FakeOpener(
            [
                urllib.error.HTTPError(
                    source_url,
                    503,
                    "test",
                    headers,
                    BytesIO(b""),
                )
            ]
        )
        clock = FakeClock()
        crawler.NEXT_SITE_REQUEST_AT = 0.0
        with (
            patch.object(
                crawler,
                "is_safe_external_download_target",
                return_value=True,
            ),
            patch.object(
                crawler,
                "build_external_download_opener",
                return_value=opener,
            ),
            patch.object(
                crawler.time,
                "monotonic",
                side_effect=clock.monotonic,
            ),
            patch.object(
                crawler,
                "sleep_with_run_control",
                side_effect=clock.sleep,
            ),
        ):
            with crawler.source_request_budget_scope(
                max_seconds_cap=2,
                max_requests_cap=10,
            ) as budget:
                result = crawler.fetch_site_result(
                    source_url,
                    "테스트",
                )

        self.assertFalse(result.ok)
        self.assertEqual(
            result.category,
            FailureCategory.SOURCE_PARTIAL,
        )
        self.assertEqual(
            result.error,
            "source_time_budget_exceeded:2",
        )
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(budget.actual_requests, 1)
        self.assertEqual(clock.sleeps, [])

    def crawl_with_pages(
        self,
        pages,
        *,
        known_ids=None,
        incremental=False,
    ):
        source = SourceSpec(
            config_fk="141",
            classification="장학공지",
            list_url="https://www.sogang.ac.kr/ko/scholarship-notice",
        )
        page_map = {
            index: value
            for index, value in enumerate(pages, start=1)
        }

        def fetch_page(page_num, page_size, config_fk=None):
            return page_map[page_num]

        with (
            patch.object(
                crawler,
                "fetch_bbs_list_result",
                side_effect=fetch_page,
            ),
            patch.object(
                crawler,
                "fetch_bbs_detail",
                return_value=dict(NORMAL_DETAIL),
            ),
            patch.object(
                crawler,
                "get_detail_html_fallback_reason",
                return_value=None,
            ),
            patch.object(
                crawler,
                "extract_body_blocks_from_html",
                return_value=[
                    {
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [
                                {
                                    "type": "text",
                                    "text": {"content": "본문"},
                                }
                            ]
                        },
                    }
                ],
            ),
            patch.object(
                crawler,
                "extract_attachments_from_api_data",
                return_value=[],
            ),
        ):
            return crawler.crawl_top_items_api_result(
                source,
                include_non_top=True,
                non_top_max_pages=0,
                known_ids=known_ids,
                incremental=incremental,
            )

    def test_fetch_list_accepts_normal_response(self):
        result, opener = self.fetch_list_with_transport(
            [json_response({"data": {"list": [NORMAL_ENTRY]}})]
        )

        self.assertTrue(result.ok)
        self.assertFalse(result.valid_empty)
        self.assertEqual(result.entries, [NORMAL_ENTRY])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(opener.calls), 1)

    def test_fetch_list_accepts_current_official_pagination_fields(self):
        result, opener = self.fetch_list_with_transport(
            [
                json_response(
                    {
                        "data": {
                            "total": 3441,
                            "list": [NORMAL_ENTRY],
                            "pageNum": 1,
                            "pageSize": 20,
                            "hasNextPage": False,
                            "isLastPage": True,
                        }
                    }
                )
            ]
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.total_count, 3441)
        self.assertFalse(result.has_more)
        self.assertTrue(result.terminal_verified)
        self.assertEqual(len(opener.calls), 1)

    def test_fetch_list_rejects_conflicting_terminal_metadata(self):
        result, opener = self.fetch_list_with_transport(
            [
                json_response(
                    {
                        "data": {
                            "total": 1,
                            "list": [NORMAL_ENTRY],
                            "pageNum": 1,
                            "pageSize": 20,
                            "hasNextPage": True,
                            "isLastPage": True,
                        }
                    }
                )
            ]
        )

        self.assertFalse(result.ok)
        self.assertEqual(
            result.category,
            FailureCategory.SOURCE_CONTRACT,
        )
        self.assertEqual(result.error, "pagination_terminal_mismatch")
        self.assertEqual(len(opener.calls), 1)

    def test_fetch_list_accepts_confirmed_empty_response(self):
        result, opener = self.fetch_list_with_transport(
            [json_response({"data": {"list": []}})]
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.valid_empty)
        self.assertEqual(result.entries, [])
        self.assertEqual(len(opener.calls), 1)

    def test_fetch_list_classifies_404_as_upstream_failure(self):
        headers = Message()
        error = urllib.error.HTTPError(
            "https://www.sogang.ac.kr/api/test",
            404,
            "Not Found",
            headers,
            None,
        )
        result, opener = self.fetch_list_with_transport([error])
        error.close()

        self.assertFalse(result.ok)
        self.assertEqual(result.status_code, 404)
        self.assertEqual(result.category, FailureCategory.SOURCE_UPSTREAM)
        self.assertEqual(result.error, "HTTP 404")
        self.assertEqual(len(opener.calls), 1)

    def test_fetch_list_rejects_200_nginx_error_html(self):
        response = FakeResponse(
            b"<html><title>502 Bad Gateway</title><body>nginx</body></html>",
            status=200,
            content_type="text/html",
        )
        result, opener = self.fetch_list_with_transport([response])

        self.assertFalse(result.ok)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.category, FailureCategory.SOURCE_CONTRACT)
        self.assertEqual(result.error, "unexpected_json_content_type")
        self.assertEqual(len(opener.calls), 1)

    def test_fetch_list_rejects_invalid_json(self):
        result, opener = self.fetch_list_with_transport(
            [FakeResponse(b'{"data": {"list": ')]
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.category, FailureCategory.SOURCE_CONTRACT)
        self.assertEqual(result.error, "invalid_json")
        self.assertEqual(len(opener.calls), 1)

    def test_fetch_list_rejects_empty_object_and_missing_fields(self):
        for payload in ({}, {"data": {}}, {"unexpected": {"list": []}}):
            with self.subTest(payload=payload):
                result, opener = self.fetch_list_with_transport(
                    [json_response(payload)]
                )

                self.assertFalse(result.ok)
                self.assertEqual(result.category, FailureCategory.SOURCE_CONTRACT)
                self.assertEqual(result.error, "missing_data_list")
                self.assertEqual(len(opener.calls), 1)

    def test_fetch_list_rejects_contract_type_changes(self):
        cases = (
            ({"data": []}, "missing_data_list"),
            ({"data": {"list": {}}}, "invalid_data_list"),
            ({"data": {"list": [NORMAL_ENTRY, "not-an-object"]}}, "invalid_data_list"),
        )
        for payload, expected_error in cases:
            with self.subTest(payload=payload):
                result, opener = self.fetch_list_with_transport(
                    [json_response(payload)]
                )

                self.assertFalse(result.ok)
                self.assertEqual(result.category, FailureCategory.SOURCE_CONTRACT)
                self.assertEqual(result.error, expected_error)
                self.assertEqual(len(opener.calls), 1)

    def test_fetch_list_rejects_declared_and_streaming_oversize_responses(self):
        cases = (
            FakeResponse(
                b"",
                content_length="2048",
            ),
            FakeResponse(b"x" * 1025),
        )
        for response in cases:
            with self.subTest(content_length=response.headers.get("Content-Length")):
                with patch.dict(os.environ, {"SITE_RESPONSE_MAX_BYTES": "1024"}):
                    result, opener = self.fetch_list_with_transport([response])

                self.assertFalse(result.ok)
                self.assertEqual(result.category, FailureCategory.SECURITY_POLICY)
                self.assertTrue(result.error.startswith("response_too_large:"))
                self.assertEqual(len(opener.calls), 1)
        self.assertEqual(cases[0].read_sizes, [])
        self.assertEqual(cases[1].read_sizes, [1025])

    def test_crawl_normal_result_is_write_safe(self):
        result = self.crawl_with_pages(
            [
                list_page([NORMAL_ENTRY]),
                list_page([]),
            ]
        )

        self.assertEqual(result.status, SourceStatus.SUCCESS)
        self.assertTrue(result.write_safe)
        self.assertEqual(result.observed_ids, ["1001"])
        self.assertEqual(result.observed_count, 1)
        self.assertEqual(len(result.items), 1)

    def test_crawl_accepts_top_entry_after_non_top_entry(self):
        top_entry = {
            **NORMAL_ENTRY,
            "pkId": "1002",
            "isTop": "Y",
        }
        result = self.crawl_with_pages(
            [
                list_page([NORMAL_ENTRY, top_entry]),
                list_page([]),
            ]
        )

        self.assertTrue(result.write_safe)
        self.assertEqual(result.observed_ids, ["1001", "1002"])
        self.assertEqual(
            [item["top"] for item in result.items],
            [False, True],
        )

    def test_crawl_confirmed_empty_result_is_write_safe(self):
        result = self.crawl_with_pages([list_page([])])

        self.assertEqual(result.status, SourceStatus.VALID_EMPTY)
        self.assertTrue(result.write_safe)
        self.assertEqual(result.items, [])
        self.assertEqual(result.observed_count, 0)

    def test_confirmed_empty_requires_explicit_write_authorization_even_without_history(self):
        result = self.crawl_with_pages([list_page([])])
        report = validate_crawl_report(CrawlReport(sources=[result]), {})

        self.assertFalse(report.write_safe)
        self.assertEqual(
            [issue.code for issue in report.issues],
            ["unexpected_empty_source"],
        )

    def test_crawl_first_page_failure_is_not_write_safe(self):
        result = self.crawl_with_pages(
            [
                ListPageResult(
                    ok=False,
                    category=FailureCategory.SOURCE_UPSTREAM,
                    error="HTTP 404",
                    status_code=404,
                )
            ]
        )

        self.assertEqual(result.status, SourceStatus.FAILED)
        self.assertFalse(result.write_safe)
        self.assertEqual(result.category, FailureCategory.SOURCE_UPSTREAM)
        self.assertEqual(result.error, "HTTP 404")

    def test_crawl_repeated_page_is_partial_and_not_write_safe(self):
        result = self.crawl_with_pages(
            [
                list_page([NORMAL_ENTRY]),
                list_page([NORMAL_ENTRY]),
            ]
        )

        self.assertEqual(result.status, SourceStatus.PARTIAL)
        self.assertFalse(result.write_safe)
        self.assertEqual(result.category, FailureCategory.SOURCE_PARTIAL)
        self.assertEqual(result.error, "repeated_page:2")

    def test_crawl_missing_checkpoint_is_partial_and_not_write_safe(self):
        with patch.dict(os.environ, {"CRAWL_HARD_PAGE_LIMIT": "1"}):
            result = self.crawl_with_pages(
                [list_page([NORMAL_ENTRY])],
                known_ids={"9999"},
                incremental=True,
            )

        self.assertEqual(result.status, SourceStatus.PARTIAL)
        self.assertFalse(result.write_safe)
        self.assertFalse(result.checkpoint_found)
        self.assertEqual(result.error, "hard_page_limit_reached:1")

    def test_incremental_checkpoint_ignores_known_pinned_top_and_scans_burst_pages(self):
        pinned = {
            **NORMAL_ENTRY,
            "pkId": "9000",
            "isTop": "Y",
        }
        first_page = [pinned] + [
            {**NORMAL_ENTRY, "pkId": str(9000 + index)}
            for index in range(1, 20)
        ]
        second_page = [
            {**NORMAL_ENTRY, "pkId": str(9000 + index)}
            for index in range(20, 31)
        ]

        result = self.crawl_with_pages(
            [
                list_page(first_page),
                list_page(second_page),
                list_page([]),
            ],
            known_ids={"9000"},
            incremental=True,
        )

        self.assertEqual(result.status, SourceStatus.SUCCESS)
        self.assertTrue(result.write_safe)
        self.assertEqual(result.pages_scanned, 3)
        self.assertEqual(len(result.items), 30)
        self.assertNotIn(
            "9000",
            [
                item["url"].rsplit("/", 1)[-1].split("?", 1)[0]
                for item in result.items
            ],
        )

    def test_crawl_missing_row_id_is_partial_and_not_write_safe(self):
        entry = dict(NORMAL_ENTRY)
        entry.pop("pkId")
        result = self.crawl_with_pages(
            [
                list_page([entry]),
                list_page([]),
            ]
        )

        self.assertEqual(result.status, SourceStatus.PARTIAL)
        self.assertFalse(result.write_safe)
        self.assertEqual(result.rejected_count, 0)
        self.assertEqual(result.error, "list_entry_contract:pkId_type")
        self.assertEqual(result.items, [])

    def test_api_terminal_total_must_equal_unique_non_top_observations(self):
        entries = [
            {**NORMAL_ENTRY, "pkId": str(1000 + index)}
            for index in range(1, 5)
        ]
        pages = [
            ListPageResult(
                ok=True,
                entries=entries[:2],
                total_count=5,
                terminal_verified=False,
            ),
            ListPageResult(
                ok=True,
                entries=entries[2:],
                total_count=5,
                has_more=False,
                terminal_verified=True,
            ),
        ]

        result = self.crawl_with_pages(pages)

        self.assertFalse(result.write_safe)
        self.assertEqual(result.error, "pagination_total_mismatch")

        for page in pages:
            page.total_count = 4
        result = self.crawl_with_pages(pages)
        self.assertTrue(result.write_safe)

    def test_api_entry_schema_rejects_boolean_unknown_and_blank_fields(self):
        cases = (
            ({**NORMAL_ENTRY, "isTop": True}, "isTop_enum"),
            ({**NORMAL_ENTRY, "isTop": "UNKNOWN"}, "isTop_enum"),
            ({**NORMAL_ENTRY, "isTop": None}, "isTop_enum"),
            ({**NORMAL_ENTRY, "isTop": " Y "}, "isTop_enum"),
            ({**NORMAL_ENTRY, "title": "   "}, "title_type"),
            ({**NORMAL_ENTRY, "regDate": None}, "regDate_type"),
            ({**NORMAL_ENTRY, "pkId": True}, "pkId_type"),
        )
        for entry, expected in cases:
            with self.subTest(expected=expected):
                result = self.crawl_with_pages([list_page([entry])])
                self.assertFalse(result.write_safe)
                self.assertEqual(
                    result.error,
                    f"list_entry_contract:{expected}",
                )

    def test_api_request_budget_cannot_be_reported_as_complete(self):
        with patch.dict(os.environ, {"API_MAX_REQUESTS": "1"}):
            result = self.crawl_with_pages([list_page([NORMAL_ENTRY])])

        self.assertFalse(result.write_safe)
        self.assertEqual(result.error, "api_request_budget_exceeded:1")

    def test_partial_html_detail_recovery_without_body_is_not_write_safe(self):
        source = SourceSpec(
            config_fk="141",
            classification="장학공지",
            list_url="https://www.sogang.ac.kr/ko/scholarship-notice",
        )
        detail = dict(NORMAL_DETAIL)
        detail["content"] = ""
        with (
            patch.object(
                crawler,
                "fetch_bbs_list_result",
                side_effect=lambda page_num, page_size, config_fk=None: (
                    list_page([NORMAL_ENTRY])
                    if page_num == 1
                    else list_page([])
                ),
            ),
            patch.object(
                crawler,
                "fetch_bbs_detail",
                return_value=detail,
            ),
            patch.object(
                crawler,
                "get_detail_html_fallback_reason",
                return_value="body_missing",
            ),
            patch.object(
                crawler,
                "fetch_detail_metadata_with_html_fallback",
                return_value=(
                    "2026-07-27T12:00:00+09:00",
                    [],
                    [],
                    "html_fallback_partial",
                    crawler.ATTACHMENTS_STATUS_KNOWN,
                    crawler.BODY_STATUS_UNKNOWN,
                ),
            ),
            patch.object(
                crawler,
                "extract_body_blocks_from_html",
                return_value=[],
            ),
            patch.object(
                crawler,
                "extract_attachments_from_api_data",
                return_value=[],
            ),
        ):
            result = crawler.crawl_top_items_api_result(
                source,
                include_non_top=True,
                non_top_max_pages=0,
            )

        self.assertEqual(result.status, SourceStatus.PARTIAL)
        self.assertFalse(result.write_safe)
        self.assertEqual(result.detail_failures, 1)
        self.assertEqual(result.items[0]["completeness"], "partial")

    def test_unverified_fallback_items_never_become_write_safe(self):
        source = SourceSpec(
            config_fk="141",
            classification="장학공지",
            list_url="https://www.sogang.ac.kr/ko/scholarship-notice",
        )
        original = SourceCrawlResult(
            source=source,
            status=SourceStatus.FAILED,
            category=FailureCategory.SOURCE_UPSTREAM,
            error="HTTP 503",
            pages_scanned=0,
        )
        item = {
            "title": "정상 형태처럼 보이는 공지",
            "url": "https://www.sogang.ac.kr/ko/detail/1001",
            "date": "2026-07-27T12:00:00+09:00",
            "top": False,
        }

        result = crawler.build_result_from_fallback_items(
            source,
            [item],
            "playwright_or_http",
            original,
        )

        self.assertEqual(result.status, SourceStatus.PARTIAL)
        self.assertFalse(result.write_safe)
        self.assertEqual(result.category, FailureCategory.SOURCE_PARTIAL)
        self.assertIn("fallback_unconfirmed", result.error)


if __name__ == "__main__":
    unittest.main()
