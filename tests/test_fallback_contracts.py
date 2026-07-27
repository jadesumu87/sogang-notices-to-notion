import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import crawler
from models import (
    CrawlReport,
    FallbackDetailResult,
    FallbackPageResult,
    FailureCategory,
    SourceCrawlResult,
    SourceSpec,
    SourceStatus,
    SiteFetchResult,
)
from run_state import default_run_state, update_state_from_report


DATE = "2026-07-27T12:00:00+09:00"
BODY = [
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
]


class FallbackContractTests(unittest.TestCase):
    def setUp(self):
        self.source = SourceSpec(
            config_fk="141",
            classification="장학공지",
            list_url="https://www.sogang.ac.kr/ko/scholarship-notice",
        )
        self.original = SourceCrawlResult(
            source=self.source,
            status=SourceStatus.FAILED,
            method="api",
            category=FailureCategory.SOURCE_UPSTREAM,
            error="api_failed",
        )
        self.env = patch.dict(
            os.environ,
            {
                "FALLBACK_MAX_REQUESTS": "100",
                "FALLBACK_MAX_SECONDS": "60",
                "FALLBACK_MIN_INTERVAL_SECONDS": "0",
                "FALLBACK_JITTER_SECONDS": "0",
                "CRAWL_HARD_PAGE_LIMIT": "10",
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def entry(
        self,
        notice_id: str,
        *,
        top: bool = False,
        title: str | None = None,
        source_id: str = "141",
    ):
        return {
            "title": title or f"공지 {notice_id}",
            "date": DATE,
            "top": top,
            "url": (
                "https://www.sogang.ac.kr/ko/detail/"
                f"{notice_id}?bbsConfigFk={source_id}"
            ),
        }

    def page(
        self,
        number: int,
        entries,
        *,
        ok: bool = True,
        explicit_empty: bool = False,
        error: str = "",
    ):
        values = list(entries)
        return FallbackPageResult(
            ok=ok,
            requested_page=number,
            effective_page=number,
            source_config_fk=self.source.config_fk,
            entries=values,
            final_url=(
                f"{self.source.list_url}?page={number}"
            ),
            contract_verified=ok,
            explicit_empty=explicit_empty,
            raw_entry_count=len(values),
            category=(
                FailureCategory.NONE
                if ok
                else FailureCategory.NETWORK
            ),
            error=error,
        )

    def detail(
        self,
        entry,
        *,
        ok: bool = True,
        title: str | None = None,
        date: str = DATE,
        notice_id: str | None = None,
        url: str | None = None,
        body_status: str = crawler.BODY_STATUS_PRESENT,
        body_blocks=None,
        attachments_status: str = crawler.ATTACHMENTS_STATUS_KNOWN,
        attachments=None,
        error: str = "",
    ):
        entry_id = crawler.extract_detail_id_from_text(entry["url"]) or ""
        return FallbackDetailResult(
            ok=ok,
            notice_id=notice_id if notice_id is not None else entry_id,
            url=url or entry["url"],
            title=title or entry["title"],
            date=date,
            body_blocks=BODY if body_blocks is None else body_blocks,
            body_status=body_status,
            attachments=(
                [
                    {
                        "name": "attachment.pdf",
                        "type": "external",
                        "external": {
                            "url": (
                                "https://www.sogang.ac.kr/"
                                "file-fe-prd/board/attachment.pdf"
                            )
                        },
                    }
                ]
                if attachments is None
                else attachments
            ),
            attachments_status=attachments_status,
            category=(
                FailureCategory.NONE
                if ok
                else FailureCategory.SOURCE_PARTIAL
            ),
            error=error,
        )

    def crawl(
        self,
        pages,
        details=None,
        *,
        include_non_top=True,
        incremental=False,
        known_ids=None,
    ):
        page_values = dict(pages)
        detail_values = details or {}

        def fetch_page(number):
            return page_values[number]

        def fetch_detail(entry, number):
            notice_id = crawler.extract_detail_id_from_text(entry["url"]) or ""
            value = detail_values.get(notice_id)
            if value is not None:
                return value
            return self.detail(entry)

        return crawler.crawl_fallback_with_fetchers(
            self.source,
            include_non_top,
            0,
            known_ids or set(),
            incremental,
            "fallback_http",
            self.original,
            fetch_page,
            fetch_detail,
        )

    def test_verified_fallback_reaches_natural_end_and_is_write_safe(self):
        first = self.entry("1001")
        result = self.crawl(
            {
                1: self.page(1, [first]),
                2: self.page(2, [], explicit_empty=True),
            }
        )

        self.assertTrue(result.write_safe)
        self.assertEqual(result.termination_reason, "natural_end")
        self.assertEqual(result.fallback_from_error, "api_failed")
        self.assertEqual([item["notice_id"] for item in result.items], ["1001"])

    def test_missing_selector_waf_and_unverified_empty_are_not_write_safe(self):
        for failure in (
            self.page(1, [], ok=False, error="fallback_list_contract_invalid"),
            self.page(1, [], ok=False, error="access_denied"),
            self.page(1, [], explicit_empty=False),
        ):
            with self.subTest(error=failure.error):
                result = self.crawl({1: failure})
                self.assertFalse(result.write_safe)
                self.assertEqual(result.termination_reason, "page_error")

    def test_second_page_timeout_and_detail_failure_preserve_all_writes(self):
        first = self.entry("1001")
        timeout = self.page(
            2,
            [],
            ok=False,
            error="fallback_browser_list_timeout",
        )
        result = self.crawl(
            {
                1: self.page(1, [first]),
                2: timeout,
            }
        )
        self.assertFalse(result.write_safe)
        self.assertEqual(len(result.items), 1)

        second = self.entry("1002")
        failed_detail = self.detail(
            second,
            ok=False,
            error="fallback_detail_timeout",
        )
        result = self.crawl(
            {
                1: self.page(1, [first, second]),
            },
            {"1002": failed_detail},
        )
        self.assertFalse(result.write_safe)
        self.assertEqual(result.detail_failures, 1)

    def test_request_budget_stops_before_scope_can_be_claimed_complete(self):
        first = self.entry("1001")
        with patch.dict(os.environ, {"FALLBACK_MAX_REQUESTS": "2"}):
            result = self.crawl(
                {
                    1: self.page(1, [first]),
                    2: self.page(2, [], explicit_empty=True),
                }
            )

        self.assertFalse(result.write_safe)
        self.assertEqual(result.termination_reason, "request_budget")

    def test_repeated_page_and_identity_transitions_are_rejected(self):
        pinned = self.entry("1001", top=True)
        result = self.crawl(
            {
                1: self.page(1, [pinned]),
                2: self.page(2, [pinned]),
            }
        )
        self.assertFalse(result.write_safe)
        self.assertIn("repeated_page", result.error)

        normal = self.entry("1002", top=False)
        promoted = self.entry("1002", top=True)
        result = self.crawl(
            {
                1: self.page(1, [normal]),
                2: self.page(2, [promoted]),
            }
        )
        self.assertFalse(result.write_safe)
        self.assertIn("identity_collision", result.error)

    def test_pinned_top_repetition_with_new_non_top_rows_is_supported(self):
        pinned = self.entry("1001", top=True)
        first = self.entry("1002")
        second = self.entry("1003")
        result = self.crawl(
            {
                1: self.page(1, [pinned, first]),
                2: self.page(2, [pinned, second]),
                3: self.page(3, [], explicit_empty=True),
            }
        )

        self.assertTrue(result.write_safe)
        self.assertEqual(result.observed_ids, ["1001", "1002", "1003"])

    def test_detail_id_url_title_and_date_mismatches_are_rejected(self):
        first = self.entry("1001")
        cases = (
            self.detail(first, notice_id="9999"),
            self.detail(
                first,
                url=(
                    "https://www.sogang.ac.kr/ko/detail/"
                    "1001?bbsConfigFk=2"
                ),
            ),
            self.detail(first, title="다른 공지"),
            self.detail(first, date="2026-07-26T12:00:00+09:00"),
        )
        for detail in cases:
            with self.subTest(detail=detail):
                result = self.crawl(
                    {1: self.page(1, [first])},
                    {"1001": detail},
                )
                self.assertFalse(result.write_safe)
                self.assertEqual(result.detail_failures, 1)

    def test_known_checkpoint_still_scans_to_verified_end(self):
        pinned = self.entry("1001", top=True)
        known = self.entry("1002")
        new = self.entry("1003")
        result = self.crawl(
            {
                1: self.page(1, [pinned, known]),
                2: self.page(2, [new]),
                3: self.page(3, [], explicit_empty=True),
            },
            incremental=True,
            known_ids={"1001", "1002"},
        )

        self.assertTrue(result.write_safe)
        self.assertEqual([item["notice_id"] for item in result.items], ["1003"])
        self.assertTrue(result.checkpoint_found)

    def test_css_or_script_text_cannot_confirm_empty_body_or_attachments(self):
        signals = crawler.build_detail_signals(
            "<html><style>.tiptap{display:block}</style>"
            "<script>const label='첨부파일'</script>"
            "<div>작성일 2026-07-27</div></html>"
        )

        self.assertFalse(signals["has_body_container"])
        self.assertFalse(signals["has_attachment_container"])
        self.assertEqual(
            crawler.classify_body_status([], signals),
            crawler.BODY_STATUS_UNKNOWN,
        )
        self.assertEqual(
            crawler.classify_attachment_status_from_signals([], signals),
            crawler.ATTACHMENTS_STATUS_UNKNOWN,
        )

    def test_actual_empty_dom_containers_are_explicit_evidence(self):
        signals = crawler.build_detail_signals(
            "<html><div>작성일 2026-07-27</div>"
            '<div class="tiptap"></div>'
            '<section class="attachment-list"><span>첨부파일</span></section>'
            "</html>"
        )

        self.assertEqual(
            crawler.classify_body_status([], signals),
            crawler.BODY_STATUS_CONFIRMED_EMPTY,
        )
        self.assertEqual(
            crawler.classify_attachment_status_from_signals([], signals),
            crawler.ATTACHMENTS_STATUS_KNOWN,
        )

    def test_non_anchor_download_text_is_not_attachment_evidence(self):
        signals = crawler.build_detail_signals(
            "<html><div>작성일 2026-07-27</div>"
            '<div class="tiptap"><iframe '
            'src="https://example.com/download/player"></iframe></div>'
            "</html>"
        )

        self.assertFalse(signals["has_attachment_link"])
        self.assertNotIn(
            "attachment_missing",
            crawler.get_detail_html_fallback_reason(
                {
                    "title": "공지",
                    "regDate": "20260727120000",
                    "content": '<iframe src="/download/player"></iframe>',
                },
                "공지",
            )
            or "",
        )

    def test_partial_loading_shell_cannot_be_promoted_to_empty_detail(self):
        html = (
            "<html><h1>공지 1001</h1><div>작성일 2026-07-27 12:00</div>"
            '<div class="skeleton"><div class="tiptap"></div></div>'
            "<div>첨부파일</div></html>"
        )
        signals = crawler.build_detail_signals(html)

        self.assertTrue(signals["has_loading_shell"])
        self.assertFalse(signals["valid_detail"])
        self.assertEqual(
            crawler.classify_attachment_status_from_signals([], signals),
            crawler.ATTACHMENTS_STATUS_UNKNOWN,
        )

    def test_empty_marker_requires_visible_tbody_evidence(self):
        script_only = (
            "<table><tbody></tbody></table>"
            "<script>const emptyLabel='no data'</script>"
        )
        visible = (
            "<table><tbody><tr><td>등록된 정보가 없습니다.</td></tr>"
            "</tbody></table>"
        )

        self.assertFalse(
            crawler.fallback_html_is_explicit_empty(script_only)
        )
        self.assertTrue(crawler.fallback_html_is_explicit_empty(visible))

    def test_empty_page_and_page_snapshot_require_independent_stability(self):
        first = self.entry("1001")
        calls = {1: 0}

        def transient_empty(number):
            calls[number] = calls.get(number, 0) + 1
            if calls[number] == 1:
                return self.page(number, [], explicit_empty=True)
            return self.page(number, [first])

        result = crawler.crawl_fallback_with_fetchers(
            self.source,
            True,
            0,
            set(),
            False,
            "fallback_http",
            self.original,
            transient_empty,
            lambda entry, number: self.detail(entry),
        )
        self.assertFalse(result.write_safe)
        self.assertIn("empty_confirmation_mismatch", result.error)

        calls = {1: 0, 2: 0}

        def shifting_pages(number):
            calls[number] += 1
            if number == 1 and calls[number] > 1:
                return self.page(1, [self.entry("1002")])
            if number == 1:
                return self.page(1, [first])
            return self.page(2, [], explicit_empty=True)

        result = crawler.crawl_fallback_with_fetchers(
            self.source,
            True,
            0,
            set(),
            False,
            "fallback_http",
            self.original,
            shifting_pages,
            lambda entry, number: self.detail(entry),
        )
        self.assertFalse(result.write_safe)
        self.assertIn("snapshot_changed", result.error)

    def test_empty_detail_requires_matching_independent_confirmation(self):
        entry = self.entry("1001")
        empty = self.detail(
            entry,
            body_status=crawler.BODY_STATUS_CONFIRMED_EMPTY,
            body_blocks=[],
        )
        filled = self.detail(entry)
        detail_calls = 0

        def fetch_detail(current, number):
            nonlocal detail_calls
            detail_calls += 1
            return empty if detail_calls == 1 else filled

        result = crawler.crawl_fallback_with_fetchers(
            self.source,
            True,
            0,
            set(),
            False,
            "fallback_http",
            self.original,
            lambda number: (
                self.page(1, [entry])
                if number == 1
                else self.page(2, [], explicit_empty=True)
            ),
            fetch_detail,
        )

        self.assertFalse(result.write_safe)
        self.assertIn("detail_unstable", result.error)

    def test_http_loading_shell_detail_is_rejected(self):
        entry = self.entry("1001")
        html = (
            "<html><h1>공지 1001</h1>"
            "<div>작성일 2026-07-27 12:00</div>"
            '<div class="skeleton"><div class="tiptap"></div></div>'
            "<div>첨부파일</div></html>"
        )
        with patch.object(
            crawler,
            "fetch_site_result",
            return_value=SiteFetchResult(
                ok=True,
                status_code=200,
                body=html.encode("utf-8"),
                content_type="text/html",
                final_url=entry["url"],
            ),
        ):
            result = crawler.fetch_fallback_http_detail(
                self.source,
                entry,
                1,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "fallback_detail_contract_invalid")

    def test_adapter_never_merges_two_partial_collectors(self):
        api_item = {"title": "API partial"}
        fallback_item = {"title": "fallback partial"}
        api_result = SourceCrawlResult(
            source=self.source,
            status=SourceStatus.DEGRADED,
            items=[api_item],
            method="api",
            category=FailureCategory.SOURCE_PARTIAL,
            error="api_partial",
        )
        fallback_result = SourceCrawlResult(
            source=self.source,
            status=SourceStatus.DEGRADED,
            items=[fallback_item],
            method="fallback_http",
            category=FailureCategory.SOURCE_PARTIAL,
            error="fallback_partial",
        )
        with (
            patch.object(
                crawler,
                "crawl_top_items_api_result",
                return_value=api_result,
            ),
            patch.object(
                crawler,
                "crawl_top_items_playwright_result",
                return_value=fallback_result,
            ) as fallback,
        ):
            result = crawler.SogangSourceAdapter().crawl(self.source)

        fallback.assert_called_once()
        self.assertEqual(result.items, [fallback_item])
        self.assertFalse(result.write_safe)

    def test_fallback_circuit_opens_and_success_resets_it(self):
        state = default_run_state()
        failed = SourceCrawlResult(
            source=self.source,
            status=SourceStatus.DEGRADED,
            method="fallback_http",
            category=FailureCategory.SOURCE_PARTIAL,
            error="fallback_partial",
        )
        with patch.dict(
            os.environ,
            {"FALLBACK_CIRCUIT_FAILURE_THRESHOLD": "1"},
        ):
            update_state_from_report(
                state,
                CrawlReport(sources=[failed]),
                full_reconcile=False,
            )

        source_state = state["sources"]["141"]
        self.assertIn("fallback_circuit_open_until", source_state)
        api_result = SourceCrawlResult(
            source=self.source,
            status=SourceStatus.FAILED,
            method="api",
            category=FailureCategory.SOURCE_UPSTREAM,
            error="api_failed",
        )
        with (
            patch.object(
                crawler,
                "crawl_top_items_api_result",
                return_value=api_result,
            ),
            patch.object(
                crawler,
                "crawl_top_items_playwright_result",
            ) as fallback,
        ):
            result = crawler.SogangSourceAdapter().crawl(
                self.source,
                source_state=source_state,
            )

        fallback.assert_not_called()
        self.assertEqual(result.method, "api_fallback_circuit_open")

        safe = SourceCrawlResult(
            source=self.source,
            status=SourceStatus.SUCCESS,
            method="api",
            observed_count=1,
            observed_ids=["1001"],
            terminal_reached=True,
            termination_reason="natural_end",
            items=[
                {
                    "title": "공지",
                    "completeness": "complete",
                }
            ],
        )
        update_state_from_report(
            state,
            CrawlReport(sources=[safe]),
            full_reconcile=False,
        )
        self.assertNotIn(
            "fallback_circuit_open_until",
            state["sources"]["141"],
        )

    def test_expired_circuit_allows_fallback_again(self):
        api_result = SourceCrawlResult(
            source=self.source,
            status=SourceStatus.FAILED,
            method="api",
            category=FailureCategory.SOURCE_UPSTREAM,
            error="api_failed",
        )
        expected = Mock(spec=SourceCrawlResult)
        expired = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        with (
            patch.object(
                crawler,
                "crawl_top_items_api_result",
                return_value=api_result,
            ),
            patch.object(
                crawler,
                "crawl_top_items_playwright_result",
                return_value=expected,
            ) as fallback,
        ):
            result = crawler.SogangSourceAdapter().crawl(
                self.source,
                source_state={"fallback_circuit_open_until": expired},
            )

        fallback.assert_called_once()
        self.assertIs(result, expected)


if __name__ == "__main__":
    unittest.main()
